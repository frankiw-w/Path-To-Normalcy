import math
import json
import os
import time
import bpy
import numpy as np
from bpy.props import PointerProperty, FloatProperty, EnumProperty, BoolProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup, Panel, Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform

# Per-curve undo cache: curve object name -> {"image_name": str,
# "diff": {(px, py): old_value}}. A sparse diff, not a full image copy, and
# keyed by the curve that produced it rather than the image it touched, so
# each curve's last stroke can be undone independently. Single-step: a new
# stroke or an undo both clear that curve's entry.
_backup_cache = {}
_ramp_clipboard = None  # last-copied ramp Texture, for the Copy/Paste operators
_user_ramps = []  # saved ramps, read from disk at registration

# Single-entry cache for the target mesh's UV lookup structures, which are
# expensive to build (a Python loop over every loop triangle, twice) and
# identical between strokes as long as the mesh hasn't changed. Invalidated
# by a depsgraph handler, see _invalidate_uv_lookup below. One entry only,
# so a heavy target mesh isn't held in memory indefinitely once the user
# moves on to a different one.
_uv_lookup_cache = {}

MAX_U_SAMPLES_PER_SPLINE = 4000
MAX_CANDIDATE_PIXELS = 30_000_000
# Margin in pixels around the strip's bounding box.
STRIP_MARGIN_PX = 2
# A cross-section wider than this multiple of the typical one has almost
# certainly landed on both sides of a UV seam.
SEAM_WIDTH_FACTOR = 8.0
# How much further than typical a single edge probe may reach before it is
# treated as having crossed a seam. Tighter than the width test because a
# probe landing on a neighbouring island barely widens the cross-section.
SEAM_PROBE_FACTOR = 2.5
# The two probes from one sample should reach about equally far. UV stretch
# affects both alike, so a lopsided pair means one of them went somewhere
# the other did not.
SEAM_ASYMMETRY = 1.8
# How far the midpoint between two samples may sit from the halfway point
# in UV before the mapping is judged to have broken between them. Generous,
# because a curved surface bows the chord slightly; a seam misses by orders
# of magnitude more than that.
UV_CONTINUITY_FACTOR = 0.25
UV_CONTINUITY_FLOOR_PX = 4.0
# How far a stroke carries past an island edge, in texels, so the shader's
# neighbour lookup finds the stroke continuing rather than open gutter.
SEAM_BLEED_PX = 4.0
# Rows are processed in bands of at most this many pixels, to bound peak
# memory on strokes covering a large part of the texture.
BAND_MAX_PIXELS = 4_000_000
# A cross-section sits perpendicular to the sample's own tangent, which at
# a bend is the average of two segment directions. Its reach measured
# perpendicular to either segment is therefore short by cos(angle), so it
# gets extended by the reciprocal, the standard miter join. Capped, since
# the factor runs away as a turn approaches a full reversal.
MITER_LIMIT = 3.0


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def get_prefs():
    """The addon's preferences, or None if they can't be reached (which can
    happen during registration or in unusual load orders). Every caller
    treats None as "diagnostics off" rather than failing."""
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError, TypeError):
        return None


def timing_enabled():
    prefs = get_prefs()
    return bool(getattr(prefs, "debug_timing", False)) if prefs is not None else False


def seam_debug_enabled():
    prefs = get_prefs()
    return bool(getattr(prefs, "debug_seams", False)) if prefs is not None else False


class StageTimer:
    """Accumulates wall-clock time per named stage across the whole
    operator, including stages entered repeatedly (once per spline). The
    point isn't the absolute seconds, which vary by machine, but the
    proportions between stages, which are a property of the code and hold
    up across machines. Does nothing at all when diagnostics are off."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.stages = {}
        self.order = []
        self.notes = []
        self._t0 = time.perf_counter()

    def mark(self, name):
        """Close out the stage that just ran and attribute the elapsed time
        since the previous mark to `name`."""
        if not self.enabled:
            return
        now = time.perf_counter()
        if name not in self.stages:
            self.stages[name] = 0.0
            self.order.append(name)
        self.stages[name] += now - self._t0
        self._t0 = now

    def skip(self):
        """Discard the time since the last mark without recording it, for
        stretches that don't belong to any stage."""
        if self.enabled:
            self._t0 = time.perf_counter()

    def note(self, text):
        if self.enabled:
            self.notes.append(text)

    def report(self, label, total):
        if not self.enabled:
            return
        print(f"[Path to Normalcy] {label} timing, total {total:.3f}s")
        measured = 0.0
        for name in self.order:
            secs = self.stages[name]
            measured += secs
            pct = (secs / total * 100.0) if total > 0 else 0.0
            print(f"    {name:<28} {secs:8.3f}s  {pct:5.1f}%")
        other = total - measured
        if other > 0.0005:
            pct = (other / total * 100.0) if total > 0 else 0.0
            print(f"    {'(unattributed)':<28} {other:8.3f}s  {pct:5.1f}%")
        for note in self.notes:
            print(f"    - {note}")


# ---------------------------------------------------------------------------
# Curve sampling
# ---------------------------------------------------------------------------

def get_curve_polylines(curve_obj, depsgraph):
    """Evaluate the curve's geometry and return a list of splines, each a
    list of world-space Vector points, in order along the spline."""
    eval_obj = curve_obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    if mesh is None or not mesh.vertices:
        if mesh is not None:
            eval_obj.to_mesh_clear()
        return []

    mesh.transform(curve_obj.matrix_world)

    adjacency = {}
    for edge in mesh.edges:
        a, b = edge.vertices
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    visited = set()
    polylines = []

    def walk(start):
        chain = [start]
        visited.add(start)
        current, prev = start, None
        while True:
            neighbors = [n for n in adjacency.get(current, []) if n != prev]
            nxt = next((n for n in neighbors if n not in visited), None)
            if nxt is None:
                if neighbors and neighbors[0] == start:
                    chain.append(start)
                break
            chain.append(nxt)
            visited.add(nxt)
            prev, current = current, nxt
        return chain

    for v in mesh.vertices:
        if v.index not in visited and len(adjacency.get(v.index, [])) <= 1:
            polylines.append([mesh.vertices[i].co.copy() for i in walk(v.index)])

    for v in mesh.vertices:
        if v.index not in visited:
            polylines.append([mesh.vertices[i].co.copy() for i in walk(v.index)])

    eval_obj.to_mesh_clear()
    return polylines


def resample_with_arclength(points, spacing):
    """Walk a polyline and return (point, u_distance) pairs spaced evenly by
    arc length."""
    if len(points) < 2 or spacing <= 0:
        return [(p, 0.0) for p in points]

    result = [(points[0], 0.0)]
    total = 0.0
    carry = 0.0
    for a, b in zip(points, points[1:]):
        seg = b - a
        seg_len = seg.length
        if seg_len <= 1e-9:
            continue
        pos = spacing - carry
        while pos < seg_len:
            result.append((a.lerp(b, pos / seg_len), total + pos))
            pos += spacing
        carry = seg_len - (pos - spacing)
        total += seg_len
    return result


def relax_polyline(points, iterations, cyclic):
    """Laplacian smoothing: pull each point toward its neighbors' average,
    repeated `iterations` times. Open curves keep both endpoints pinned;
    cyclic curves (points[0] == points[-1], per get_curve_polylines'
    closing-duplicate convention) wrap around with nothing pinned."""
    if iterations <= 0 or len(points) < 3:
        return points
    pts = [p.copy() for p in points]
    n = len(pts)
    for _ in range(iterations):
        new_pts = [p.copy() for p in pts]
        if cyclic:
            ring_n = n - 1  # exclude the closing duplicate from the ring
            for i in range(ring_n):
                prev_p = pts[(i - 1) % ring_n]
                next_p = pts[(i + 1) % ring_n]
                new_pts[i] = pts[i].lerp((prev_p + next_p) * 0.5, 0.5)
            new_pts[-1] = new_pts[0].copy()  # keep the closing duplicate in sync
        else:
            for i in range(1, n - 1):
                new_pts[i] = pts[i].lerp((pts[i - 1] + pts[i + 1]) * 0.5, 0.5)
        pts = new_pts
    return pts


def relax_polylines_if_enabled(polylines, props):
    if not props.relax_curve:
        return polylines
    result = []
    for sp in polylines:
        cyclic = len(sp) >= 3 and (sp[0] - sp[-1]).length < 1e-6
        result.append(relax_polyline(sp, props.relax_iterations, cyclic))
    return result


# ---------------------------------------------------------------------------
# Mesh <-> UV lookups, both directions
# ---------------------------------------------------------------------------

def build_uv_lookup(depsgraph, target_obj):
    eval_obj = target_obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    if mesh is None:
        return None, None
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        eval_obj.to_mesh_clear()
        return None, None

    mesh.calc_loop_triangles()
    mat = target_obj.matrix_world

    flat_verts = []
    tri_index_lists = []
    tri_data = []
    for tri in mesh.loop_triangles:
        co = [mat @ mesh.vertices[i].co for i in tri.vertices]
        uv = [uv_layer.data[li].uv.copy() for li in tri.loops]
        base = len(flat_verts)
        flat_verts.extend(co)
        tri_index_lists.append([base, base + 1, base + 2])
        tri_data.append((co[0], co[1], co[2], uv[0], uv[1], uv[2]))

    eval_obj.to_mesh_clear()

    if not tri_data:
        return None, None

    bvh = BVHTree.FromPolygons(flat_verts, tri_index_lists, all_triangles=True)
    return bvh, tri_data


def get_uv_lookup(depsgraph, target_obj):
    """Both lookup structures for a target mesh, reused from the cache when
    the mesh hasn't changed since last time. Returns
    (bvh, tri_data, was_cached), with the first two None if the mesh can't
    be used (no UV map, no triangles). Building this walks every loop
    triangle in Python, so on a dense target it is the largest fixed cost
    of a stroke, and it produces exactly the same result every time until
    the mesh, its modifiers, its UVs or its transform change."""
    key = target_obj.name
    if _uv_lookup_cache.get("obj_name") == key:
        return (_uv_lookup_cache["bvh"], _uv_lookup_cache["tri_data"], True)

    bvh, tri_data = build_uv_lookup(depsgraph, target_obj)
    if bvh is None:
        return None, None, False

    _uv_lookup_cache.clear()
    _uv_lookup_cache.update({
        "obj_name": key,
        "mesh_name": getattr(target_obj.data, "name", None),
        "bvh": bvh,
        "tri_data": tri_data,
    })
    return bvh, tri_data, False


@bpy.app.handlers.persistent
def _invalidate_uv_lookup(scene, depsgraph):
    """Drop the cached lookup as soon as anything about the cached target
    changes. The cache bakes in the evaluated geometry, the UVs and the
    object's world matrix, so a moved, edited or re-modified target would
    otherwise keep painting against a stale surface. Kept deliberately
    cheap, since this runs on every depsgraph update in the file: the
    common case is an immediate return on an empty cache."""
    if not _uv_lookup_cache:
        return
    obj_name = _uv_lookup_cache.get("obj_name")
    mesh_name = _uv_lookup_cache.get("mesh_name")
    for update in depsgraph.updates:
        name = getattr(update.id, "name", None)
        if name != obj_name and (mesh_name is None or name != mesh_name):
            continue
        if update.is_updated_geometry or update.is_updated_transform:
            _uv_lookup_cache.clear()
            return


@bpy.app.handlers.persistent
def _clear_caches_on_load(*args):
    """Both caches hold references to datablocks from the file that was
    open when they were filled, so neither means anything after a new file
    is loaded."""
    _uv_lookup_cache.clear()
    _backup_cache.clear()


def world_to_uv(bvh, tri_data, point):
    location, normal, index, distance = bvh.find_nearest(point)
    if index is None:
        return None, None, None
    co0, co1, co2, uv0, uv1, uv2 = tri_data[index]
    uv_pt = barycentric_transform(
        location, co0, co1, co2,
        Vector((uv0.x, uv0.y, 0.0)),
        Vector((uv1.x, uv1.y, 0.0)),
        Vector((uv2.x, uv2.y, 0.0)),
    )
    return uv_pt.x, uv_pt.y, normal


# ---------------------------------------------------------------------------
# Image sampling / writing
# ---------------------------------------------------------------------------

def check_image_usable(image, label, may_load=True):
    """Return a human-readable reason this image can't be read or written
    as a flat pixel buffer, or None if it's fine. Without this, an unloaded
    or tiled image reaches np.reshape with a zero or mismatched size and
    raises a bare ValueError at the user.

    With may_load off, no attempt is made to pull pixel data in. The panel
    calls it that way: draw() runs constantly, and reaching for the disk
    from a redraw would stall the interface."""
    if image is None:
        return f"No {label} image assigned"
    if getattr(image, "source", None) == 'TILED':
        return (
            f"The {label} image is a UDIM tile set, which this addon can't "
            f"read as a single image"
        )
    if not image.has_data:
        # Blender loads pixel data lazily, so an image can sit in the file
        # with a perfectly good filepath and still report no data until
        # something asks for it. Ask, then re-check: only a genuine failure
        # (missing file, nothing generated) should be reported.
        if may_load:
            try:
                if image.filepath_raw or image.filepath:
                    image.reload()
                if not image.has_data:
                    len(image.pixels)
            except (RuntimeError, AttributeError):
                pass
        if not image.has_data:
            if not may_load and (image.filepath_raw or image.filepath):
                # Probably just not loaded yet; painting will pull it in.
                return None
            return (
                f"The {label} image has no pixel data (the file may be "
                f"missing from disk, or the image was never generated)"
            )
    w, h = image.size
    if w <= 0 or h <= 0:
        return f"The {label} image has no size"
    if image.channels != 4:
        return (
            f"The {label} image has {image.channels} channels, this addon "
            f"expects 4 (RGBA)"
        )
    return None


def collect_status(curve_obj, props):
    """Everything wrong with this curve's setup that can be known without
    painting anything. Returns a list of (severity, message), severity
    being 'ERROR' for something that will stop a stroke and 'WARNING' for
    something that will merely surprise.

    These used to be reported by the operator, which meant finding out by
    pressing Draw, and then only seeing the last of them: Blender's status
    bar shows one report per run, so a stroke that raised three notes
    showed one. As settings rather than events, they belong on screen for
    as long as they are true."""
    issues = []
    target = props.target_obj
    if target is None:
        issues.append(('ERROR', "No target mesh set"))
    elif target.type != 'MESH':
        issues.append(('ERROR', "Target must be a mesh object"))
    else:
        mesh = target.data
        if not getattr(mesh, "uv_layers", None) or mesh.uv_layers.active is None:
            issues.append(('ERROR', "Target mesh has no active UV map"))

    problem = check_image_usable(props.target_image, "target", may_load=False)
    if problem:
        issues.append(('ERROR', problem))
    elif props.target_image.colorspace_settings.name != 'Non-Color':
        issues.append((
            'WARNING',
            "Target image is not Non-Color, values may be gamma-shifted",
        ))

    if props.profile_source == 'IMAGE':
        problem = check_image_usable(props.profile_image, "profile", may_load=False)
        if problem:
            issues.append(('ERROR', problem))
        elif props.profile_image.colorspace_settings.name != 'Non-Color':
            issues.append((
                'WARNING',
                "Profile image is not Non-Color, sampled values may be gamma-shifted",
            ))
    elif props.profile_source == 'RAMP' and props.profile_ramp_texture is None:
        issues.append(('ERROR', "No color ramp set up yet, add one first"))

    return issues


def draw_status(layout, curve_obj, props):
    """Render the status block, or nothing at all when there is nothing to
    say. Errors are drawn in alert red, warnings plainly: a wrong colour
    space still paints, and someone may want it that way."""
    issues = collect_status(curve_obj, props)
    if not issues:
        return
    box = layout.box()
    for severity, message in issues:
        row = box.row()
        row.alert = severity == 'ERROR'
        row.label(text=message, icon='ERROR' if severity == 'ERROR' else 'INFO')


def load_image_array(image):
    w, h = image.size
    buf = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(buf)
    return buf.reshape(h, w, 4), w, h


def store_image_array(image, arr):
    image.pixels.foreach_set(arr.reshape(-1))
    image.update()


def raster_tri_lerp(hit, d_buf, u_buf, v_buf, ox, oy, tri, u_vals, v_vals, sx, sy):
    """Rasterize a triangle, interpolating the along-curve and across-curve
    coordinates from its corners instead of deriving them per pixel.

    This is what removes the segment banding. Deriving position by finding
    the nearest segment makes that position jump where two segments' claim
    regions meet: pixels on either side of the boundary project to opposite
    ends of the shared vertex, so the arc-length value steps. Interpolating
    across a quad can't do that, because neighbouring quads share their
    whole boundary cross-section, so the value is continuous by
    construction.

    Where quads overlap (a curve crossing itself, or a cap meeting the
    body) the sample nearest the centerline wins, matching what a
    nearest-point search would have chosen."""
    (ax, ay), (bx, by), (cx, cy) = tri
    h, w = hit.shape
    minx = max(int(math.floor(min(ax, bx, cx) - 1.0)) - ox, 0)
    maxx = min(int(math.ceil(max(ax, bx, cx) + 1.0)) - ox, w - 1)
    miny = max(int(math.floor(min(ay, by, cy) - 1.0)) - oy, 0)
    maxy = min(int(math.ceil(max(ay, by, cy) + 1.0)) - oy, h - 1)
    if minx > maxx or miny > maxy:
        return
    det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(det) < 1e-12:
        return
    inv = 1.0 / det
    X = (np.arange(minx, maxx + 1, dtype=np.float64) + ox + 0.5 + sx)[None, :]
    Y = (np.arange(miny, maxy + 1, dtype=np.float64) + oy + 0.5 + sy)[:, None]
    l0 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) * inv
    l1 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) * inv
    l2 = 1.0 - l0 - l1
    inside = (l0 >= -1e-7) & (l1 >= -1e-7) & (l2 >= -1e-7)
    if not inside.any():
        return
    u = l0 * u_vals[0] + l1 * u_vals[1] + l2 * u_vals[2]
    v = l0 * v_vals[0] + l1 * v_vals[1] + l2 * v_vals[2]
    dist = np.abs(v)

    sub_hit = hit[miny:maxy + 1, minx:maxx + 1]
    sub_d = d_buf[miny:maxy + 1, minx:maxx + 1]
    take = inside & (~sub_hit | (dist < sub_d))
    if not take.any():
        return
    sub_hit |= inside
    np.copyto(sub_d, dist, where=take)
    np.copyto(u_buf[miny:maxy + 1, minx:maxx + 1], u, where=take)
    np.copyto(v_buf[miny:maxy + 1, minx:maxx + 1], v, where=take)


def raster_cap(hit, d_buf, u_buf, v_buf, o_buf, ox, oy, cap, tw, th, sx, sy,
               half_width, feather, cap_style):
    """Fill a cap region past a tip. The tip's own tangent and side probes
    give a local frame, so a pixel's offset from the tip inverts straight
    back into world along-curve and across-curve distances. That makes the
    inclusion test analytic here rather than another polygon fill."""
    cu, cv = cap["center"]
    mat = cap["inv"]
    if mat is None:
        return
    x0, x1, y0, y1 = cap["box"]
    minx = max(x0 - ox, 0)
    maxx = min(x1 - ox, hit.shape[1] - 1)
    miny = max(y0 - oy, 0)
    maxy = min(y1 - oy, hit.shape[0] - 1)
    if minx > maxx or miny > maxy:
        return
    X = (np.arange(minx, maxx + 1, dtype=np.float64) + ox + 0.5 + sx)[None, :] / tw - cu
    Y = (np.arange(miny, maxy + 1, dtype=np.float64) + oy + 0.5 + sy)[:, None] / th - cv
    t = mat[0][0] * X + mat[0][1] * Y
    s = mat[1][0] * X + mat[1][1] * Y

    over = np.maximum(t, 0.0)
    if cap_style == 'ROUND':
        dist = np.hypot(over, s)
        inside = (t >= 0.0) & (dist <= half_width)
    else:
        dist = np.abs(s)
        inside = (t >= 0.0) & (dist <= half_width) & (over <= max(feather, 0.0))
        dist = np.hypot(over, s)
    if not inside.any():
        return

    sub_hit = hit[miny:maxy + 1, minx:maxx + 1]
    sub_d = d_buf[miny:maxy + 1, minx:maxx + 1]
    take = inside & (~sub_hit | (dist < sub_d))
    if not take.any():
        return
    sub_hit |= inside
    np.copyto(sub_d, dist, where=take)
    np.copyto(u_buf[miny:maxy + 1, minx:maxx + 1], np.full(t.shape, cap["u"]), where=take)
    np.copyto(v_buf[miny:maxy + 1, minx:maxx + 1], s, where=take)
    np.copyto(o_buf[miny:maxy + 1, minx:maxx + 1], over, where=take)


def invert_uv_frame(t_uv, s_uv, half_width):
    """Invert the 2x2 map taking world offsets along (tangent, side) to UV
    offsets, so a UV offset can be read back as world distances. Returns
    None where the frame is degenerate and the region should be skipped."""
    a = t_uv[0] / half_width
    c = t_uv[1] / half_width
    b = s_uv[0] / half_width
    d = s_uv[1] / half_width
    det = a * d - b * c
    if abs(det) < 1e-16:
        return None
    inv = 1.0 / det
    return ((d * inv, -b * inv), (-c * inv, a * inv))


def find_seam_parameter(bvh, tri_data, p_a, p_b, uv_a, uv_b, tw, th):
    """Where along the segment between two samples does the mapping break?

    Bisect: at each midpoint, ask which side's island the UV belongs to.
    Returns (t_a, t_b) bracketing the crossing as tightly as the search
    allows, so each side can be extended right up to the island edge."""
    lo, hi = 0.0, 1.0
    delta = p_b - p_a
    for _ in range(24):
        mid = (lo + hi) * 0.5
        u, v, _n = world_to_uv(bvh, tri_data, p_a + delta * mid)
        if u is None:
            break
        d_a = math.hypot((u - uv_a[0]) * tw, (v - uv_a[1]) * th)
        d_b = math.hypot((u - uv_b[0]) * tw, (v - uv_b[1]) * th)
        if d_a <= d_b:
            lo = mid
        else:
            hi = mid
    return lo, hi


def build_strip_geometry(bvh, tri_data, samples, half_width, feather, cap_style, tw, th,
                         seam_debug=False):
    """Walk a resampled spline and produce everything the rasterizer needs:
    a cross-section in UV at every sample, the quads between them, and a
    local frame at each tip for the caps.

    Returns None if the spline can't produce a usable strip."""
    centers, u_dists, sides, tangents = [], [], [], []
    center_uv, edge_uv, miters = [], [], []
    prev_side = None
    diag = [] if seam_debug else None

    for i, (point, u_dist) in enumerate(samples):
        prev_pt = samples[i - 1][0] if i > 0 else point
        next_pt = samples[i + 1][0] if i < len(samples) - 1 else point
        tangent = next_pt - prev_pt
        if tangent.length <= 1e-9:
            continue
        tangent.normalize()
        u0, v0, normal = world_to_uv(bvh, tri_data, point)
        if normal is None:
            continue
        if prev_side is None:
            side = tangent.cross(normal)
        else:
            side = prev_side - tangent * prev_side.dot(tangent)
            side = side - normal * side.dot(normal)
            if side.length <= 1e-9:
                side = tangent.cross(normal)
        if side.length <= 1e-9:
            continue
        side.normalize()
        prev_side = side

        # Miter: how far this cross-section must reach so that its
        # perpendicular distance from the adjacent segments is still a full
        # half-width. On a smooth curve this is 1.0 and changes nothing.
        miter = 1.0
        for other in (prev_pt, next_pt):
            d = other - point
            if d.length > 1e-9:
                align = abs(d.normalized().dot(tangent))
                if align > 1e-3:
                    miter = max(miter, min(1.0 / align, MITER_LIMIT))

        pair = [None, None]
        for k, sign in enumerate((-1.0, 1.0)):
            eu, ev, _ = world_to_uv(
                bvh, tri_data, point + side * (half_width * miter * sign)
            )
            if eu is not None:
                pair[k] = (eu, ev)
        if pair[0] is None or pair[1] is None:
            continue

        centers.append(point)
        u_dists.append(u_dist)
        sides.append(side)
        tangents.append(tangent)
        center_uv.append((u0, v0))
        edge_uv.append(pair)
        miters.append(miter)

    n = len(centers)
    if n < 2:
        return None

    # Check each edge probe against its OWN centre, not the two against
    # each other. A probe that crosses a seam onto a nearby island barely
    # changes the total width, so a test on the total lets it through and
    # the quad then stretches toward a corner sitting on the wrong island,
    # which is the diagonal spur at the end of a run.
    #
    # Distances are divided by the sample's miter factor first, since a
    # mitered cross-section legitimately reaches further and would
    # otherwise look like a bad probe.
    reach = []
    for i, pair in enumerate(edge_uv):
        cu, cv = center_uv[i]
        m = max(miters[i], 1e-6)
        reach.append((
            math.hypot((pair[0][0] - cu) * tw, (pair[0][1] - cv) * th) / m,
            math.hypot((pair[1][0] - cu) * tw, (pair[1][1] - cv) * th) / m,
        ))
    flat = sorted(r for pair in reach for r in pair)
    median_reach = flat[len(flat) // 2]
    probe_limit = max(median_reach * SEAM_PROBE_FACTOR, 1.0)

    widths = [
        math.hypot((p[1][0] - p[0][0]) * tw, (p[1][1] - p[0][1]) * th)
        for p in edge_uv
    ]
    median_w = sorted(widths)[len(widths) // 2]
    seam_limit = max(median_w * SEAM_WIDTH_FACTOR, 1.0)

    ok = []
    for i, w in enumerate(widths):
        left_bad = reach[i][0] > probe_limit
        right_bad = reach[i][1] > probe_limit
        lo = min(reach[i]) if min(reach[i]) > 1e-6 else 1e-6
        lopsided = (max(reach[i]) / lo) > SEAM_ASYMMETRY
        good = (w <= seam_limit) and not left_bad and not right_bad and not lopsided
        ok.append(good)
        if diag is not None and not good:
            diag.append(
                f"sample {i}: reach L={reach[i][0]:.1f}px R={reach[i][1]:.1f}px "
                f"(typical {median_reach:.1f}px, limit {probe_limit:.1f}px), "
                f"width {w:.1f}px"
                + (" LEFT PROBE CROSSED" if left_bad else "")
                + (" RIGHT PROBE CROSSED" if right_bad else "")
                + (" LOPSIDED" if lopsided else "")
                + (" WIDTH" if w > seam_limit else "")
            )

    # Ask the surface whether the mapping actually holds between two
    # samples, rather than guessing from how far apart they landed.
    #
    # Look up the UV of the 3D point halfway between them. If nothing is in
    # the way, it lands near the halfway point in UV too. If a seam sits
    # between them, the island continues elsewhere and the midpoint misses
    # by a mile. This scales itself: no threshold tied to stroke width or
    # texture size, which is what the old distance test kept getting wrong.
    #
    # Without it, two samples either side of a seam still got connected,
    # and the quad between them was rasterized straight across the texture,
    # painting whatever unrelated island happened to lie in its path.
    steps = [
        math.hypot(
            (center_uv[i + 1][0] - center_uv[i][0]) * tw,
            (center_uv[i + 1][1] - center_uv[i][1]) * th,
        )
        for i in range(n - 1)
    ]
    # Tolerance scales with the TYPICAL step, not this one. Scaling by this
    # step would let a jump widen its own allowance: the bigger the seam,
    # the more slack it granted itself, so nothing ever tripped.
    typical_step = sorted(steps)[len(steps) // 2] if steps else 0.0
    tolerance = max(typical_step * UV_CONTINUITY_FACTOR, UV_CONTINUITY_FLOOR_PX)

    continuous = []
    for i in range(n - 1):
        step_px = steps[i]
        mid = (centers[i] + centers[i + 1]) * 0.5
        mu, mv, mnormal = world_to_uv(bvh, tri_data, mid)
        if mu is None:
            continuous.append(False)
            continue
        want_u = (center_uv[i][0] + center_uv[i + 1][0]) * 0.5
        want_v = (center_uv[i][1] + center_uv[i + 1][1]) * 0.5
        miss_px = math.hypot((mu - want_u) * tw, (mv - want_v) * th)
        good_step = miss_px <= tolerance
        continuous.append(good_step)
        if diag is not None and not good_step:
            diag.append(
                f"step {i}->{i+1}: midpoint landed {miss_px:.1f}px from where a "
                f"continuous surface would put it (step {step_px:.1f}px, "
                f"typical {typical_step:.1f}px, limit {tolerance:.1f}px) - bridging the crossing"
            )

    def add_quad(sec_a, u_a, sec_b, u_b):
        """A quad spans two cross-sections, corner order (left, right) at
        the first then (right, left) at the second. Each section is
        (centre_uv, [left_uv, right_uv]); only the edges bound the quad."""
        la, ra = sec_a[1]
        lb, rb = sec_b[1]
        quads.append({
            "pts": (
                (la[0] * tw, la[1] * th), (ra[0] * tw, ra[1] * th),
                (rb[0] * tw, rb[1] * th), (lb[0] * tw, lb[1] * th),
            ),
            "u": (u_a, u_b),
        })

    # Rather than dropping a cross-section whose probe crossed the seam,
    # rebuild the bad side by carrying the offset from the nearest good
    # cross-section on the same stretch of curve. Dropping them was fine
    # when a stroke met a seam head-on, because only one or two straddle
    # it. Meet the seam at a slant and the seam sweeps across the stroke,
    # straddling many in a row, and the stroke lost that whole span.
    #
    # The rebuilt edge lands past the island boundary, in the gutter, which
    # is where the stroke wants to continue anyway.
    run_of = [0] * n
    run = 0
    for i in range(1, n):
        if not continuous[i - 1]:
            run += 1
        run_of[i] = run

    repaired = 0
    for i in range(n):
        if ok[i]:
            continue
        donor = None
        for step in range(1, min(n, 64)):
            for j in (i - step, i + step):
                if 0 <= j < n and ok[j] and run_of[j] == run_of[i]:
                    donor = j
                    break
            if donor is not None:
                break
        if donor is None:
            continue
        cu, cv = center_uv[i]
        gu, gv = center_uv[donor]
        edge_uv[i] = [
            (cu + (edge_uv[donor][k][0] - gu), cv + (edge_uv[donor][k][1] - gv))
            for k in (0, 1)
        ]
        miters[i] = miters[donor]
        ok[i] = True
        repaired += 1
    if diag is not None and repaired:
        diag.append(
            f"rebuilt {repaired} cross-section(s) that straddled a seam, "
            f"using the nearest good one on the same side"
        )

    quads = []
    for i in range(n - 1):
        if not ok[i] or not ok[i + 1]:
            continue
        a, b = edge_uv[i], edge_uv[i + 1]
        if not continuous[i]:
            # A seam sits between these two samples. Rather than leaving a
            # gap, find where the crossing happens and run each side up to
            # its own island edge, then a little past it. Arc length is
            # carried across, so the profile continues rather than
            # restarting on the far side.
            t_a, t_b = find_seam_parameter(
                bvh, tri_data, centers[i], centers[i + 1],
                center_uv[i], center_uv[i + 1], tw, th,
            )
            span_u = u_dists[i + 1] - u_dists[i]
            for t_cross, ref, forward in ((t_a, i, True), (t_b, i + 1, False)):
                # Don't probe near the seam. A cross-section sits across the
                # stroke, so unless the curve meets the seam at a right
                # angle one of its two probes lands on the far island and
                # the whole section gets thrown out. Carry the last good
                # cross-section forward in UV instead: no probes, so no
                # angle at which this stops working.
                nb = ref - 1 if forward else ref + 1
                if not (0 <= nb < n):
                    continue
                dx = center_uv[ref][0] - center_uv[nb][0]
                dy = center_uv[ref][1] - center_uv[nb][1]
                dir_px = math.hypot(dx * tw, dy * th)
                if dir_px < 1e-9:
                    continue

                # Reach to the crossing, then far enough past it to cover a
                # seam met at a slant, plus the bleed. Anything beyond the
                # island edge lands in the gutter, which is exactly where
                # the shader wants to find the stroke continuing.
                gap_frac = t_cross if forward else (1.0 - t_cross)
                advance = gap_frac * typical_step + median_reach + SEAM_BLEED_PX
                scale = advance / dir_px
                ou, ov = dx * scale, dy * scale

                (cu, cv), pair = center_uv[ref], edge_uv[ref]
                section = (
                    (cu + ou, cv + ov),
                    [(pair[0][0] + ou, pair[0][1] + ov),
                     (pair[1][0] + ou, pair[1][1] + ov)],
                )
                anchor = (center_uv[ref], edge_uv[ref])
                u_here = u_dists[i] + span_u * t_cross
                if forward:
                    add_quad(anchor, u_dists[ref], section, u_here)
                else:
                    add_quad(section, u_here, anchor, u_dists[ref])
                if diag is not None:
                    diag.append(
                        f"  crossing {i}->{i+1}: carried the "
                        f"{'near' if forward else 'far'} side {advance:.1f}px "
                        f"to the island edge and past it"
                    )
            continue
        add_quad((center_uv[i], a), u_dists[i], (center_uv[i + 1], b), u_dists[i + 1])
    if not quads:
        return None

    caps = []
    for idx, tan_sign in ((0, -1.0), (n - 1, 1.0)):
        if not ok[idx]:
            continue
        tip = centers[idx]
        out_dir = tangents[idx] * tan_sign
        cu, cv = center_uv[idx]
        tu, tv, _ = world_to_uv(bvh, tri_data, tip + out_dir * half_width)
        if tu is None:
            continue
        # This probe reaches PAST the end of the curve, so near a seam it is
        # the likeliest of all of them to land on another island. Until now
        # it went straight into the frame unchecked: a bad one inverts to an
        # enormous frame, and the cap then sweeps a band clear across the
        # texture. It reaches the same distance as the side probes, so it
        # can be held to the same standard.
        t_reach = math.hypot((tu - cu) * tw, (tv - cv) * th)
        if t_reach > probe_limit or t_reach < median_reach * 0.1:
            if diag is not None:
                diag.append(
                    f"cap at sample {idx}: tangent probe reached {t_reach:.1f}px "
                    f"(typical {median_reach:.1f}px, limit {probe_limit:.1f}px) "
                    f"- cap dropped, stroke ends flat here"
                )
            continue
        # Side displacement is signed the same way v_signed is measured, so
        # the frame inverts straight into the coordinates the profile wants.
        s_uv = (edge_uv[idx][1][0] - cu, edge_uv[idx][1][1] - cv)
        t_uv = (tu - cu, tv - cv)
        inv = invert_uv_frame(t_uv, s_uv, half_width)
        if inv is None:
            continue
        reach = half_width if cap_style == 'ROUND' else max(feather, 0.0)
        span_u = (abs(t_uv[0]) + abs(s_uv[0])) * (reach / half_width + 1.0)
        span_v = (abs(t_uv[1]) + abs(s_uv[1])) * (reach / half_width + 1.0)
        # Even a probe that passed can be somewhat off; a cap can never
        # legitimately cover more than the stroke's own reach plus its cap
        # extent, so clamp rather than trust the arithmetic.
        max_span = (median_reach * (reach / half_width + 1.0) * 1.5 + 4.0)
        span_u = min(span_u, max_span / tw)
        span_v = min(span_v, max_span / th)
        caps.append({
            "center": (cu, cv),
            "inv": inv,
            "u": u_dists[idx],
            "box": (
                int(math.floor((cu - span_u) * tw)) - 2,
                int(math.ceil((cu + span_u) * tw)) + 2,
                int(math.floor((cv - span_v) * th)) - 2,
                int(math.ceil((cv + span_v) * th)) + 2,
            ),
        })

    xs, ys = [], []
    for q in quads:
        for (qx, qy) in q["pts"]:
            xs.append(qx)
            ys.append(qy)
    for c in caps:
        xs.extend((c["box"][0], c["box"][1]))
        ys.extend((c["box"][2], c["box"][3]))

    bx0 = max(int(math.floor(min(xs))) - STRIP_MARGIN_PX, 0)
    bx1 = min(int(math.ceil(max(xs))) + STRIP_MARGIN_PX, tw - 1)
    by0 = max(int(math.floor(min(ys))) - STRIP_MARGIN_PX, 0)
    by1 = min(int(math.ceil(max(ys))) + STRIP_MARGIN_PX, th - 1)
    if bx0 > bx1 or by0 > by1:
        return None

    # Area estimate, used for the work cap. Summing quad areas double
    # counts anywhere the stroke overlaps itself, which errs toward
    # over-estimating: the safe direction for a limit. It is clamped to the
    # bounding box below, since no arrangement of quads inside a box can
    # cover more pixels than the box holds.
    area = 0.0
    for q in quads:
        p = q["pts"]
        area += 0.5 * abs(
            (p[0][0] * p[1][1] - p[1][0] * p[0][1]) +
            (p[1][0] * p[2][1] - p[2][0] * p[1][1]) +
            (p[2][0] * p[3][1] - p[3][0] * p[2][1]) +
            (p[3][0] * p[0][1] - p[0][0] * p[3][1])
        )
    for c in caps:
        area += (c["box"][1] - c["box"][0]) * (c["box"][3] - c["box"][2]) * 0.5

    area = min(area, float((bx1 - bx0 + 1) * (by1 - by0 + 1)))

    if diag is not None:
        print(f"[Path to Normalcy] seam check: {n} samples, "
              f"{sum(1 for f in ok if not f)} dropped, "
              f"{len(quads)} quads kept of {n - 1} possible")
        for line in diag[:40]:
            print(f"    {line}")
        if len(diag) > 40:
            print(f"    ... and {len(diag) - 40} more")

    return {
        "quads": quads,
        "caps": caps,
        "bbox": (bx0, bx1, by0, by1),
        "area": area,
        "n_samples": n,
        "n_dropped": sum(1 for f in ok if not f),
        "total_length": samples[-1][1],
    }


def rasterize_strip_band(geom, band_y0, band_y1, sub_offset, tw, th,
                         half_width, feather, cap_style):
    """Fill one horizontal band with per-pixel along-curve and across-curve
    coordinates for a single sub-sample position. Banding keeps peak memory
    bounded on strokes that span a large part of the texture."""
    bx0, bx1, _, _ = geom["bbox"]
    w = bx1 - bx0 + 1
    h = band_y1 - band_y0 + 1
    hit = np.zeros((h, w), dtype=bool)
    d_buf = np.full((h, w), np.inf)
    u_buf = np.zeros((h, w))
    v_buf = np.zeros((h, w))
    o_buf = np.zeros((h, w))
    sx, sy = sub_offset

    for q in geom["quads"]:
        p = q["pts"]
        ys = [pt[1] for pt in p]
        if max(ys) < band_y0 - 1 or min(ys) > band_y1 + 1:
            continue
        u0, u1 = q["u"]
        # Corner order is (left@i, right@i, right@i+1, left@i+1).
        raster_tri_lerp(hit, d_buf, u_buf, v_buf, bx0, band_y0,
                        (p[0], p[1], p[2]), (u0, u0, u1),
                        (-half_width, half_width, half_width), sx, sy)
        raster_tri_lerp(hit, d_buf, u_buf, v_buf, bx0, band_y0,
                        (p[0], p[2], p[3]), (u0, u1, u1),
                        (-half_width, half_width, -half_width), sx, sy)

    for c in geom["caps"]:
        if c["box"][3] < band_y0 - 1 or c["box"][2] > band_y1 + 1:
            continue
        raster_cap(hit, d_buf, u_buf, v_buf, o_buf, bx0, band_y0, c, tw, th,
                   sx, sy, half_width, feather, cap_style)

    return hit, u_buf, v_buf, o_buf


def build_ramp_lut(ramp, size=4096):
    """Bake a ColorRamp into a lookup table once per stroke. ramp.evaluate
    is a Python-level call into Blender, so doing it per sub-sample was
    costing more than the entire nearest-segment search on some strokes.
    A CONSTANT ramp is flagged so its lookup stays nearest-neighbour:
    interpolating a deliberate hard step would quietly soften it."""
    ts = np.linspace(0.0, 1.0, size)
    height = np.empty(size, dtype=np.float64)
    coverage = np.empty(size, dtype=np.float64)
    for i, t in enumerate(ts):
        color = ramp.evaluate(float(t))
        height[i] = color[0]
        coverage[i] = color[3]
    return height, coverage, getattr(ramp, "interpolation", "LINEAR") == 'CONSTANT'


def sample_lut_vec(lut, t, nearest=False):
    """Lookup at t in [0,1], vectorized."""
    size = len(lut)
    x = np.clip(t, 0.0, 1.0) * (size - 1)
    if nearest:
        return lut[np.rint(x).astype(np.int64)]
    i0 = np.floor(x).astype(np.int64)
    i1 = np.minimum(i0 + 1, size - 1)
    f = x - i0
    return lut[i0] * (1.0 - f) + lut[i1] * f


def sample_bilinear_vec(arr, w, h, u, v, channel=0):
    """Bilinear sample of a profile image. Clamps to the edge, with the
    endpoints landing on the outermost texel centres."""
    u = np.clip(u, 0.0, 1.0)
    v = np.clip(v, 0.0, 1.0)
    x = u * (w - 1)
    y = v * (h - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = x - x0
    fy = y - y0
    c = arr[:, :, channel]
    top = c[y0, x0] + (c[y0, x1] - c[y0, x0]) * fx
    bottom = c[y1, x0] + (c[y1, x1] - c[y1, x0]) * fx
    return top + (bottom - top) * fy


def sample_profile_vec(props, u_norm, v_norm, profile_arr, pw, ph, ramp_lut):
    """Height and coverage from the chosen profile source. Falls back to
    (0.5, 0.0), a no-op, when there is nothing valid to sample."""
    v_pos = (v_norm + 1.0) * 0.5
    n = len(v_norm)
    height = None
    coverage = np.ones(n, dtype=np.float64)

    if props.profile_source == 'IMAGE' and profile_arr is not None:
        # By default the image's vertical axis runs along the stroke, which
        # is how profile images are usually authored. Rotate 90 swaps to
        # the horizontal axis.
        if props.profile_rotate_90:
            sample_u, sample_v = u_norm, v_pos
        else:
            sample_u, sample_v = v_pos, u_norm
        height = sample_bilinear_vec(profile_arr, pw, ph, sample_u, sample_v, channel=0)
        if props.use_alpha:
            coverage = sample_bilinear_vec(profile_arr, pw, ph, sample_u, sample_v, channel=3)
    elif props.profile_source == 'RAMP' and ramp_lut is not None:
        h_lut, c_lut, nearest = ramp_lut
        t = np.minimum(np.abs(v_norm), 1.0) if props.ramp_mapping == 'MIRROR' else v_pos
        height = sample_lut_vec(h_lut, t, nearest)
        if props.use_alpha:
            coverage = sample_lut_vec(c_lut, t, nearest)

    if height is None:
        return np.full(n, 0.5), np.zeros(n)

    if props.profile_direction == 'RAISE':
        height = 0.5 + 0.5 * height
    elif props.profile_direction == 'INDENT':
        height = 0.5 - 0.5 * height

    return height, coverage


def solve_stamp_layout(props, stamp_size, total_length):
    """Work out where stamps actually land along a spline.

    Spacing is the gap between stamps, edge to edge, so changing the stamp
    size no longer silently changes the visible gap. Overlap Stamp switches
    it back to centre-to-centre, which is what lets stamps sit on top of
    each other for things like weld beads.

    Fit to Curve rounds the number of stamps to the nearest whole one and
    spreads the leftover through the gaps, so a run ends flush with the end
    of the curve without the stamp itself being resized or distorted.

    Returns (period, count, gap, fitted, clipped)."""
    gap = max(props.profile_spacing, 0.0)
    period = gap if props.profile_overlap_stamp else stamp_size + gap
    if period <= 0.0:
        # Centre-to-centre with no spacing has no sensible reading, so treat
        # it as the seamless case rather than tiling infinitely.
        period = stamp_size
    period = max(period, 1e-6)

    if total_length <= 0.0:
        return period, 1, gap, False, False

    if not props.profile_fit_to_curve or gap <= 0.0:
        # Stamps repeat from the start of the curve for as long as one can
        # begin before the end, so the last may run past it. Counting has to
        # match what actually gets drawn, or the readout disagrees with the
        # result.
        count = max(1, int(math.ceil(total_length / period)))
        clipped = (count - 1) * period + stamp_size > total_length + 1e-9
        if props.profile_skip_clipped and clipped and count > 1:
            count -= 1
            clipped = False
        return period, count, period - stamp_size, False, clipped

    span = total_length - stamp_size
    if span <= 0.0:
        return period, 1, 0.0, True, False
    count = max(int(round(span / period)) + 1, 2)
    fitted_period = span / (count - 1)
    if fitted_period < stamp_size and count > 2:
        # Rounding up would have pushed stamps into each other; step back
        # rather than silently overlapping them.
        count -= 1
        fitted_period = span / (count - 1)
    return fitted_period, count, fitted_period - stamp_size, True, False


def uniform_object_scale(obj):
    """The object's scale if it's uniform, else None. Curve length is
    measured on the original data, so a non-uniform scale would make any
    length we report wrong in a way we can't correct with one number."""
    sx, sy, sz = obj.scale
    if abs(sx - sy) > 1e-6 or abs(sy - sz) > 1e-6:
        return None
    return abs(sx)


def curve_display_length(curve_obj):
    """Total length of a curve's splines, for the panel readout. Uses the
    spline's own length calculation rather than evaluating the object, so
    it's cheap enough to call while drawing the UI."""
    scale = uniform_object_scale(curve_obj)
    if scale is None:
        return None
    try:
        return sum(spline.calc_length() for spline in curve_obj.data.splines) * scale
    except (AttributeError, RuntimeError):
        return None


def blend_value_vec(old, sampled, strength, mode, direction):
    """Combine the sampled profile value with what is already in the
    image, according to the chosen blend mode."""
    new_val = old + (sampled - 0.5) * strength
    if mode == 'MIX':
        mixed = old + (sampled - old) * strength
        if direction == 'RAISE':
            return np.maximum(old, mixed)
        if direction == 'INDENT':
            return np.minimum(old, mixed)
        return mixed
    if mode == 'ADD':
        return new_val
    if mode == 'MAX':
        return np.maximum(old, new_val)
    if mode == 'MIN':
        return np.minimum(old, new_val)
    if mode == 'AVERAGE':
        return (old + new_val) / 2.0
    if mode == 'DEVIATION':
        return np.where(np.abs(new_val - 0.5) > np.abs(old - 0.5), new_val, old)
    return old


def normalize_stroke_diff(diff):
    """Accept either the current (px, py, rgb) array form or a dict left
    over from an earlier version's cache, so a stroke made before an
    update can still be undone after it."""
    if diff is None:
        return None
    if isinstance(diff, tuple):
        return diff
    if isinstance(diff, dict):
        if not diff:
            return None
        n = len(diff)
        px = np.empty(n, dtype=np.int32)
        py = np.empty(n, dtype=np.int32)
        rgb = np.empty((n, 3), dtype=np.float32)
        for i, ((x, y), old) in enumerate(diff.items()):
            px[i] = x
            py[i] = y
            if isinstance(old, tuple):
                rgb[i] = old
            else:
                # Older still: only the red channel was recorded.
                rgb[i] = (old, old, old)
        return (px, py, rgb)
    return None


def merge_stroke_diff(existing, px, py, rgb, tw):
    """Combine a new batch of before-values with whatever this curve
    already had recorded, keeping the earliest value for any pixel touched
    more than once. Arrays rather than a dict: at a couple of million
    pixels a dict of tuple keys costs hundreds of megabytes and seconds of
    insert time, which was showing up as most of the write stage."""
    if len(px) == 0:
        return existing
    key = py.astype(np.int64) * tw + px.astype(np.int64)
    _, first = np.unique(key, return_index=True)
    px, py, rgb, key = px[first], py[first], rgb[first], key[first]

    if existing is None or len(existing[0]) == 0:
        return (px, py, rgb)

    old_px, old_py, old_rgb = existing
    old_key = old_py.astype(np.int64) * tw + old_px.astype(np.int64)
    order = np.argsort(old_key)
    sorted_old = old_key[order]
    pos = np.searchsorted(sorted_old, key)
    pos = np.minimum(pos, len(sorted_old) - 1)
    already = sorted_old[pos] == key
    keep = ~already
    return (
        np.concatenate([old_px, px[keep]]),
        np.concatenate([old_py, py[keep]]),
        np.concatenate([old_rgb, rgb[keep]]),
    )


def box_blur_1d(a, radius):
    if radius <= 0:
        return a.copy()
    k = radius * 2 + 1
    padded = np.pad(a, radius, mode='edge')
    csum = np.concatenate(([0.0], np.cumsum(padded)))
    return (csum[k:] - csum[:-k]) / k


def box_blur(arr2d, radius):
    """Separable box blur, no scipy dependency, verified against a point
    spike (spreads evenly across the kernel) and a uniform field (stays
    exactly flat, confirming no edge artifacts from the padding mode)."""
    if radius <= 0:
        return arr2d.copy()
    h = np.apply_along_axis(lambda row: box_blur_1d(row, radius), 1, arr2d)
    v = np.apply_along_axis(lambda col: box_blur_1d(col, radius), 0, h)
    return v


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class CURVEPAINT_OT_stroke(Operator):
    bl_idname = "paint.curve_stroke"
    bl_label = "Paint Stroke From Curve"
    bl_description = "Write profile detail into the target image along the active curve"
    bl_options = {'REGISTER'}

    erase: BoolProperty(
        name="Erase",
        description="Flatten the stroke's area back to neutral instead of painting it",
        default=False,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        if curve_obj is None or curve_obj.type != 'CURVE':
            return False
        props = curve_obj.curve_paint_settings
        if props.target_obj is None or props.target_image is None:
            return False
        if props.profile_source == 'IMAGE' and props.profile_image is None:
            return False
        if props.profile_source == 'RAMP' and props.profile_ramp_texture is None:
            return False
        return True

    def execute(self, context):
        start_time = time.time()
        timer = StageTimer(timing_enabled())
        seam_debug = seam_debug_enabled()
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        target_obj = props.target_obj
        target_image = props.target_image

        if target_obj.type != 'MESH':
            self.report({'ERROR'}, "Target field must be a mesh object")
            return {'CANCELLED'}
        problem = check_image_usable(target_image, "target")
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}
        depsgraph = context.evaluated_depsgraph_get()

        polylines = get_curve_polylines(curve_obj, depsgraph)
        if not polylines:
            self.report({'ERROR'}, "Curve has no evaluated points")
            return {'CANCELLED'}
        polylines = relax_polylines_if_enabled(polylines, props)
        timer.mark("curve evaluation")

        bvh, tri_data, was_cached = get_uv_lookup(depsgraph, target_obj)
        if bvh is None:
            self.report({'ERROR'}, "Target mesh has no active UV map, or no usable geometry")
            return {'CANCELLED'}
        timer.mark("mesh lookup (cached)" if was_cached else "mesh lookup (built)")
        timer.note(
            f"target mesh: {len(tri_data)} triangles, lookup "
            f"{'reused from cache' if was_cached else 'rebuilt this stroke'}"
        )

        profile_arr = pw = ph = None
        if props.profile_source == 'IMAGE':
            problem = check_image_usable(props.profile_image, "profile")
            if problem:
                self.report({'ERROR'}, problem)
                return {'CANCELLED'}
            profile_arr, pw, ph = load_image_array(props.profile_image)

        ramp = None
        if props.profile_source == 'RAMP':
            if props.profile_ramp_texture is None:
                self.report({'ERROR'}, "No color ramp set up yet, add one first")
                return {'CANCELLED'}
            ramp = props.profile_ramp_texture.color_ramp

        target_arr, tw, th = load_image_array(target_image)
        timer.mark("image load")

        spacing = max(props.resample_distance, 0.0001)
        half_width = max(props.profile_half_width, 0.0001)
        feather = half_width * props.edge_feather

        stamp_size = max(props.profile_stamp_size, 0.0001)
        if props.profile_mode == 'STAMP' and props.profile_lock_stamp_ratio and pw and ph:
            # Which image axis runs along the stroke depends on Rotate 90,
            # so the aspect has to follow it. Using the same ratio for both
            # made a rotated stamp wrong by the aspect squared, which on a
            # 4:1 image is 16x out.
            along, across = (pw, ph) if props.profile_rotate_90 else (ph, pw)
            stamp_size = max(2.0 * half_width * (along / across), 0.0001)

        # Erase has no boundary worth softening, and blur would only smear
        # the neutral value back into what it just cleared.
        quality = 1 if self.erase else max(1, int(props.profile_quality))
        if quality <= 1:
            sub_offsets = [(0.0, 0.0)]
        else:
            step = 1.0 / quality
            start_off = -0.5 + step / 2.0
            sub_offsets = [
                (start_off + i * step, start_off + j * step)
                for i in range(quality) for j in range(quality)
            ]

        touched = 0
        # Bake the ramp once per stroke instead of calling into Blender
        # per sub-sample.
        ramp_lut = build_ramp_lut(ramp) if (props.profile_source == 'RAMP' and ramp is not None) else None
        diff_batches = []  # per-spline (px, py, rgb-before) arrays, merged at the end
        spline_skipped_for_size = False

        for spline_points in polylines:
            if len(spline_points) < 2:
                continue

            spline_length = sum((b - a).length for a, b in zip(spline_points, spline_points[1:]))
            eff_spacing = spacing
            if spline_length > 0:
                eff_spacing = max(spacing, spline_length / MAX_U_SAMPLES_PER_SPLINE)

            samples = resample_with_arclength(spline_points, eff_spacing)
            if len(samples) < 2:
                continue
            total_length = samples[-1][1]

            stamp_period, stamp_count, stamp_gap, stamp_fitted, _clipped = solve_stamp_layout(
                props, stamp_size, total_length
            )

            geom = build_strip_geometry(
                bvh, tri_data, samples, half_width, feather, props.cap_style, tw, th,
                seam_debug=seam_debug,
            )
            timer.mark("curve sampling + strip build")
            if geom is None:
                continue
            if geom["n_dropped"]:
                timer.note(
                    f"spline: dropped {geom['n_dropped']} cross-sections that "
                    f"looked like they straddled a UV seam"
                )

            est_samples = geom["area"] * len(sub_offsets)
            if est_samples > MAX_CANDIDATE_PIXELS:
                spline_skipped_for_size = True
                self.report(
                    {'WARNING'},
                    f"Skipped a spline, it would need about {int(est_samples):,} "
                    f"samples (limit {MAX_CANDIDATE_PIXELS:,})",
                )
                continue

            bx0, bx1, by0, by1 = geom["bbox"]
            band_w = bx1 - bx0 + 1
            band_rows = max(1, min(by1 - by0 + 1, BAND_MAX_PIXELS // max(band_w, 1)))
            n_sub = len(sub_offsets)
            spline_touched = 0

            for band_y0 in range(by0, by1 + 1, band_rows):
                band_y1 = min(band_y0 + band_rows - 1, by1)
                bh = band_y1 - band_y0 + 1
                accum_val = np.zeros((bh, band_w))
                accum_w = np.zeros((bh, band_w))

                for sub in sub_offsets:
                    hit, u_buf, v_buf, o_buf = rasterize_strip_band(
                        geom, band_y0, band_y1, sub, tw, th,
                        half_width, feather, props.cap_style,
                    )
                    if not hit.any():
                        continue
                    sel = np.nonzero(hit)
                    v_sel = v_buf[sel]
                    u_sel = u_buf[sel]
                    o_sel = o_buf[sel]

                    if props.cap_style == 'ROUND':
                        near = np.hypot(o_sel, v_sel)
                        keep = near <= half_width
                        edge_dist = half_width - near
                    else:
                        keep = (np.abs(v_sel) <= half_width) & (o_sel <= max(feather, 0.0))
                        edge_dist = np.minimum(
                            half_width - np.abs(v_sel), feather - o_sel
                        )
                    if not keep.any():
                        continue

                    rows = sel[0][keep]
                    cols = sel[1][keep]
                    v_norm = v_sel[keep] / half_width
                    u_here = u_sel[keep]
                    edge_dist = edge_dist[keep]

                    # Not gated on spacing any more. Spacing is now the gap
                    # between stamps, so zero is the perfectly reasonable
                    # request for a seamless run, and gating on it dropped
                    # that case through to continuous mode, stretching one
                    # image over the whole curve.
                    if props.profile_mode == 'STAMP':
                        cycle_pos = np.mod(u_here, stamp_period)
                        in_stamp = cycle_pos < stamp_size
                        if props.profile_skip_clipped and not stamp_fitted:
                            # Drop a final stamp that would run off the end
                            # of the curve rather than showing it cut in half.
                            index = np.floor(u_here / stamp_period)
                            in_stamp &= (index * stamp_period + stamp_size) <= total_length + 1e-6
                        if not in_stamp.any():
                            continue
                        rows = rows[in_stamp]
                        cols = cols[in_stamp]
                        v_norm = v_norm[in_stamp]
                        edge_dist = edge_dist[in_stamp]
                        u_norm = cycle_pos[in_stamp] / stamp_size
                    else:
                        u_norm = (u_here / total_length) if total_length > 0 else np.zeros(len(u_here))

                    if self.erase:
                        # Erase ignores the profile entirely: a stamp's own
                        # softness would only partly erase its own edges and
                        # leave a ghost behind, which reads as the eraser
                        # not working rather than as a soft edge.
                        sampled = np.full(len(rows), 0.5)
                        factor = np.ones(len(rows))
                    else:
                        sampled, coverage = sample_profile_vec(
                            props, u_norm, v_norm, profile_arr, pw, ph, ramp_lut
                        )
                        if feather <= 0:
                            factor = np.ones(len(rows))
                        else:
                            factor = np.clip(edge_dist / feather, 0.0, 1.0)
                        factor = factor * coverage

                    np.add.at(accum_val, (rows, cols), sampled * factor)
                    np.add.at(accum_w, (rows, cols), factor)

                timer.mark("strip raster + profile")

                wr, wc = np.nonzero(accum_w > 0.0)
                if len(wr) == 0:
                    timer.skip()
                    continue
                avg_sampled = accum_val[wr, wc] / accum_w[wr, wc]
                # Divided by every sub-sample offered, not just the ones
                # that landed inside, so a pixel the boundary only clips
                # gets partial strength. That is the anti-aliasing.
                avg_factor = accum_w[wr, wc] / n_sub
                px_arr = (wc + bx0).astype(np.int32)
                py_arr = (wr + band_y0).astype(np.int32)

                before_rgb = target_arr[py_arr, px_arr, 0:3].astype(np.float32).copy()
                diff_batches.append((px_arr, py_arr, before_rgb))

                old = target_arr[py_arr, px_arr, 0].astype(np.float64)
                if self.erase:
                    new = blend_value_vec(old, avg_sampled, avg_factor, 'MIX', 'BOTH')
                else:
                    new = blend_value_vec(
                        old, avg_sampled, props.profile_strength * avg_factor,
                        props.blend_mode, props.profile_direction,
                    )
                new32 = new.astype(np.float32)
                target_arr[py_arr, px_arr, 0] = new32
                target_arr[py_arr, px_arr, 1] = new32
                target_arr[py_arr, px_arr, 2] = new32
                spline_touched += len(wr)
                timer.mark("pixel write")

            touched += spline_touched
            timer.note(
                f"spline: {geom['n_samples']} samples, {len(geom['quads'])} quads, "
                f"{(bx1 - bx0 + 1) * (by1 - by0 + 1)} px bbox, "
                f"{spline_touched} written, {n_sub} sub-sample(s) each"
            )


        if touched == 0:
            if spline_skipped_for_size:
                self.report(
                    {'WARNING'},
                    "Nothing written: the stroke needs more samples than the limit allows, "
                    "try turning Anti-Aliasing down or working with a shorter curve",
                )
            else:
                self.report({'WARNING'}, "Nothing written, is the curve near the target mesh's surface?")
            return {'CANCELLED'}

        stroke_diff = None
        for batch in diff_batches:
            stroke_diff = merge_stroke_diff(stroke_diff, batch[0], batch[1], batch[2], tw)
        _backup_cache[curve_obj.name] = {"image_name": target_image.name, "diff": stroke_diff}
        store_image_array(target_image, target_arr)
        timer.mark("image store")

        elapsed = time.time() - start_time
        print(f"[Path to Normalcy] Draw: {touched} texels touched in {elapsed:.3f}s")
        if elapsed > 0:
            timer.note(f"throughput: {touched / elapsed:,.0f} texels/sec")
        timer.report("Erase" if self.erase else "Draw", elapsed)
        if self.erase:
            self.report({'INFO'}, f"Erased {touched} texels back to neutral ({elapsed:.2f}s)")
        else:
            self.report({'INFO'}, f"Stroke painted from curve ({touched} texels touched, {elapsed:.2f}s)")

        if props.blur_auto_apply and not self.erase:
            bpy.ops.paint.curve_stroke_blur()

        return {'FINISHED'}


class CURVEPAINT_OT_blur(Operator):
    bl_idname = "paint.curve_stroke_blur"
    bl_label = "Blur Stroke"
    bl_description = "Blur the target image along this curve's painted region"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        if curve_obj is None or curve_obj.type != 'CURVE':
            return False
        props = curve_obj.curve_paint_settings
        return props.target_obj is not None and props.target_image is not None

    def execute(self, context):
        start_time = time.time()
        timer = StageTimer(timing_enabled())
        seam_debug = seam_debug_enabled()
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        target_obj = props.target_obj
        target_image = props.target_image

        if target_obj.type != 'MESH':
            self.report({'ERROR'}, "Target field must be a mesh object")
            return {'CANCELLED'}
        problem = check_image_usable(target_image, "target")
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        depsgraph = context.evaluated_depsgraph_get()
        polylines = get_curve_polylines(curve_obj, depsgraph)
        if not polylines:
            self.report({'ERROR'}, "Curve has no evaluated points")
            return {'CANCELLED'}
        polylines = relax_polylines_if_enabled(polylines, props)
        timer.mark("curve evaluation")

        bvh, tri_data, was_cached = get_uv_lookup(depsgraph, target_obj)
        if bvh is None:
            self.report({'ERROR'}, "Target mesh has no active UV map, or no usable geometry")
            return {'CANCELLED'}
        timer.mark("mesh lookup (cached)" if was_cached else "mesh lookup (built)")

        target_arr, tw, th = load_image_array(target_image)
        timer.mark("image load")

        spacing = max(props.resample_distance, 0.0001)
        half_width = max(props.profile_half_width, 0.0001)
        blend_margin = max(props.blend_margin, 0.0)
        extended_half_width = half_width + blend_margin

        # This re-derives "which pixels does this curve affect" the same
        # way the paint operator does, duplicated rather than shared with
        # it for now, to avoid risking the paint path while adding this.
        # The candidate region is extended past the stroke's own boundary
        # by blend_margin, so the blur can actually blend into the
        # surrounding pixels instead of stopping dead at the same edge the
        # Shares the strip builder with the paint pass rather than
        # duplicating it, so the blurred region matches the painted one by
        # construction instead of by two implementations agreeing. The
        # region is widened by blend_margin so the blur can reach into the
        # pixels around the stroke instead of stopping at its edge.
        band_records = []
        for spline_points in polylines:
            if len(spline_points) < 2:
                continue
            spline_length = sum((b - a).length for a, b in zip(spline_points, spline_points[1:]))
            eff_spacing = spacing
            if spline_length > 0:
                eff_spacing = max(spacing, spline_length / MAX_U_SAMPLES_PER_SPLINE)
            samples = resample_with_arclength(spline_points, eff_spacing)
            if len(samples) < 2:
                continue

            geom = build_strip_geometry(
                bvh, tri_data, samples, extended_half_width, 0.0, 'ROUND', tw, th
            )
            timer.mark("curve sampling + strip build")
            if geom is None:
                continue
            if geom["area"] > MAX_CANDIDATE_PIXELS:
                self.report(
                    {'WARNING'},
                    f"Skipped a spline, it would need about {int(geom['area']):,} "
                    f"samples (limit {MAX_CANDIDATE_PIXELS:,})",
                )
                continue

            bx0, bx1, by0, by1 = geom["bbox"]
            band_w = bx1 - bx0 + 1
            band_rows = max(1, min(by1 - by0 + 1, BAND_MAX_PIXELS // max(band_w, 1)))

            for band_y0 in range(by0, by1 + 1, band_rows):
                band_y1 = min(band_y0 + band_rows - 1, by1)
                hit, _u, v_buf, o_buf = rasterize_strip_band(
                    geom, band_y0, band_y1, (0.0, 0.0), tw, th,
                    extended_half_width, 0.0, 'ROUND',
                )
                if not hit.any():
                    continue
                sel = np.nonzero(hit)
                near = np.hypot(o_buf[sel], v_buf[sel])
                keep = near <= extended_half_width
                if not keep.any():
                    continue
                rows = sel[0][keep]
                cols = sel[1][keep]
                near = near[keep]

                if blend_margin > 0.0:
                    w = np.clip((extended_half_width - near) / blend_margin, 0.0, 1.0)
                else:
                    w = np.ones(len(near))
                band_records.append((
                    (cols + bx0).astype(np.int32),
                    (rows + band_y0).astype(np.int32),
                    w,
                ))
            timer.mark("strip raster + weights")
            timer.note(
                f"spline: {geom['n_samples']} samples, {len(geom['quads'])} quads"
            )

        if not band_records:
            self.report({'WARNING'}, "Nothing to blur, no painted region found for this curve")
            return {'CANCELLED'}

        all_px = np.concatenate([r[0] for r in band_records])
        all_py = np.concatenate([r[1] for r in band_records])
        all_w = np.concatenate([r[2] for r in band_records])
        # Where splines overlap, the strongest weight wins, matching what
        # the per-pixel dict did before.
        key = all_py.astype(np.int64) * tw + all_px.astype(np.int64)
        order = np.lexsort((-all_w, key))
        key_s = key[order]
        first = np.ones(len(key_s), dtype=bool)
        first[1:] = key_s[1:] != key_s[:-1]
        pick = order[first]
        all_px = all_px[pick]
        all_py = all_py[pick]
        all_w = all_w[pick]
        touched = len(all_px)

        radius_px = max(1, int(round(props.blur_size)))
        bx0 = max(int(all_px.min()) - radius_px, 0)
        bx1 = min(int(all_px.max()) + radius_px, tw - 1)
        by0 = max(int(all_py.min()) - radius_px, 0)
        by1 = min(int(all_py.max()) + radius_px, th - 1)

        region = target_arr[by0:by1 + 1, bx0:bx1 + 1, 0].astype(np.float64)
        blurred = box_blur(region, radius_px)
        timer.mark("box blur")

        existing = _backup_cache.get(curve_obj.name)
        if existing is not None and existing.get("image_name") == target_image.name:
            stroke_diff = normalize_stroke_diff(existing.get("diff"))
        else:
            stroke_diff = None

        # Vectorized rather than a per-pixel Python loop, same as the paint
        # pass. The dict of tuple keys this replaced was both the slowest
        # part of writing and by far the largest thing held in memory.
        keep_w = all_w > 0.0
        px_arr = all_px[keep_w]
        py_arr = all_py[keep_w]
        w_arr = all_w[keep_w] * props.blur_strength
        if len(px_arr) == 0:
            self.report({'WARNING'}, "Nothing to blur, no painted region found for this curve")
            return {'CANCELLED'}

        before_rgb = target_arr[py_arr, px_arr, 0:3].astype(np.float32).copy()
        stroke_diff = merge_stroke_diff(stroke_diff, px_arr, py_arr, before_rgb, tw)

        old_vals = target_arr[py_arr, px_arr, 0].astype(np.float64)
        blur_vals = blurred[py_arr - by0, px_arr - bx0]
        new_vals = (old_vals + (blur_vals - old_vals) * w_arr).astype(np.float32)
        target_arr[py_arr, px_arr, 0] = new_vals
        target_arr[py_arr, px_arr, 1] = new_vals
        target_arr[py_arr, px_arr, 2] = new_vals

        _backup_cache[curve_obj.name] = {"image_name": target_image.name, "diff": stroke_diff}
        store_image_array(target_image, target_arr)
        timer.mark("pixel write + image store")

        elapsed = time.time() - start_time
        timer.report("Blur", elapsed)
        self.report({'INFO'}, f"Blurred {len(px_arr)} texels")
        return {'FINISHED'}


class CURVEPAINT_OT_undo(Operator):
    bl_idname = "paint.curve_stroke_undo"
    bl_label = "Undo Last Stroke"
    bl_description = (
        "Restore the pixels this curve's last stroke touched (single-step, "
        "specific to the active curve, not tied to Blender's normal undo)"
    )

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE' and curve_obj.name in _backup_cache

    def execute(self, context):
        curve_obj = context.active_object
        entry = _backup_cache.get(curve_obj.name)
        if entry is None:
            self.report({'WARNING'}, "Nothing to undo for this curve")
            return {'CANCELLED'}

        image = bpy.data.images.get(entry["image_name"])
        if image is None:
            del _backup_cache[curve_obj.name]
            self.report({'WARNING'}, "The target image for that stroke no longer exists")
            return {'CANCELLED'}

        problem = check_image_usable(image, "target")
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}

        arr, w, h = load_image_array(image)
        diff = normalize_stroke_diff(entry.get("diff"))
        if diff is None or len(diff[0]) == 0:
            self.report({'WARNING'}, "Nothing recorded to undo for this curve")
            return {'CANCELLED'}
        px_arr, py_arr, rgb = diff
        arr[py_arr, px_arr, 0] = rgb[:, 0]
        arr[py_arr, px_arr, 1] = rgb[:, 1]
        arr[py_arr, px_arr, 2] = rgb[:, 2]
        store_image_array(image, arr)

        del _backup_cache[curve_obj.name]
        self.report({'INFO'}, "Restored this curve's pixels to before its last stroke")
        return {'FINISHED'}


class CURVEPAINT_OT_redraw(Operator):
    bl_idname = "paint.curve_stroke_redraw"
    bl_label = "Redraw"
    bl_description = "Undo this curve's last stroke and immediately repaint it with current settings"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if not CURVEPAINT_OT_stroke.poll(context):
            return False
        curve_obj = context.active_object
        return curve_obj.name in _backup_cache

    def execute(self, context):
        bpy.ops.paint.curve_stroke_undo()
        result = bpy.ops.paint.curve_stroke()
        if 'FINISHED' in result:
            self.report({'INFO'}, "Stroke redrawn with current settings")
        return result


class CURVEPAINT_OT_erase(Operator):
    bl_idname = "paint.curve_stroke_erase"
    bl_label = "Erase Stroke"
    bl_description = (
        "Flatten this curve's area back to neutral. Note that anywhere two "
        "strokes overlap, this clears both"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return CURVEPAINT_OT_stroke.poll(context)

    def execute(self, context):
        return bpy.ops.paint.curve_stroke(erase=True)


class CURVEPAINT_OT_erase_selected(Operator):
    bl_idname = "paint.curve_stroke_erase_selected"
    bl_label = "Erase Selected"
    bl_description = "Flatten every selected curve's area back to neutral"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' for o in context.selected_objects)

    def execute(self, context):
        curves = [o for o in context.selected_objects if o.type == 'CURVE']
        original = context.view_layer.objects.active
        done = 0
        for curve_obj in curves:
            context.view_layer.objects.active = curve_obj
            if not CURVEPAINT_OT_stroke.poll(context):
                continue
            try:
                if 'FINISHED' in bpy.ops.paint.curve_stroke(erase=True):
                    done += 1
            except RuntimeError:
                continue
        context.view_layer.objects.active = original
        if done == 0:
            self.report({'WARNING'}, "Nothing erased, check each curve has a target set")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Erased {done} of {len(curves)} curves")
        return {'FINISHED'}


class CURVEPAINT_OT_paint_selected(Operator):
    bl_idname = "paint.curve_stroke_paint_selected"
    bl_label = "Paint Selected Curves"
    bl_description = "Run Draw for every selected curve, using each curve's own settings"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' for o in context.selected_objects)

    def execute(self, context):
        curve_objs = [o for o in context.selected_objects if o.type == 'CURVE']
        prev_active = context.view_layer.objects.active
        painted = 0
        skipped = 0
        for curve_obj in curve_objs:
            context.view_layer.objects.active = curve_obj
            try:
                result = bpy.ops.paint.curve_stroke()
            except RuntimeError:
                skipped += 1
                continue
            if 'FINISHED' in result:
                painted += 1
            else:
                skipped += 1
        if prev_active is not None:
            context.view_layer.objects.active = prev_active

        if painted == 0:
            self.report({'WARNING'}, "Nothing painted, check selected curves have valid target/profile settings")
            return {'CANCELLED'}
        msg = f"Painted {painted} curve(s)"
        if skipped:
            msg += f", skipped {skipped} not ready"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class CURVEPAINT_OT_undo_selected(Operator):
    bl_idname = "paint.curve_stroke_undo_selected"
    bl_label = "Undo Selected"
    bl_description = "Undo the last stroke for every selected curve that has one"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' and o.name in _backup_cache for o in context.selected_objects)

    def execute(self, context):
        curve_objs = [
            o for o in context.selected_objects if o.type == 'CURVE' and o.name in _backup_cache
        ]
        prev_active = context.view_layer.objects.active
        undone = 0
        for curve_obj in curve_objs:
            context.view_layer.objects.active = curve_obj
            try:
                result = bpy.ops.paint.curve_stroke_undo()
            except RuntimeError:
                continue
            if 'FINISHED' in result:
                undone += 1
        if prev_active is not None:
            context.view_layer.objects.active = prev_active

        if undone == 0:
            self.report({'WARNING'}, "Nothing to undo among selected curves")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Undid {undone} curve(s)")
        return {'FINISHED'}


def normalize_ramp(ramp):
    """Force plain RGB interpolation on a ramp we own.

    Only the red channel is read as height, so if the ramp blends between
    stops in a perceptual or hue-based space, a black-to-white ramp stops
    producing a straight line in red. The profile then comes out subtly
    curved with nothing in the panel to explain why."""
    try:
        ramp.color_mode = 'RGB'
    except (TypeError, AttributeError):
        pass


def user_ramps_path():
    """Where saved ramps live: one file in Blender's own config directory,
    so they follow the user between projects and survive addon updates.
    Never surfaced in the UI, the user picks ramps by name, not by path."""
    folder = bpy.utils.user_resource('CONFIG', path="path_to_normalcy", create=True)
    return os.path.join(folder, "ramps.json")


def load_user_ramps():
    """Read saved ramps into memory. A missing or damaged file is treated
    as 'none saved yet' rather than an error: a broken preset file should
    never stop someone painting."""
    global _user_ramps
    _user_ramps = []
    try:
        path = user_ramps_path()
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            _user_ramps = [r for r in data if isinstance(r, dict) and r.get("name")]
    except (OSError, ValueError, TypeError) as exc:
        print(f"[Path to Normalcy] Could not read saved ramps: {exc}")


def store_user_ramps():
    """Write saved ramps back out. Returns an error string, or None."""
    try:
        with open(user_ramps_path(), 'w', encoding='utf-8') as f:
            json.dump(_user_ramps, f, indent=1)
        return None
    except OSError as exc:
        return f"Could not save: {exc}"


def ramp_to_dict(ramp, name, mapping):
    """Capture a ramp as plain data. Full RGBA per stop, since alpha is
    read as coverage and dropping it would quietly change the profile."""
    return {
        "name": name,
        "mapping": mapping,
        "interpolation": ramp.interpolation,
        "stops": [
            {"pos": float(e.position), "color": [float(c) for c in e.color]}
            for e in ramp.elements
        ],
    }


def dict_to_ramp(data, ramp):
    """Write saved data back into a ramp, replacing whatever was there."""
    stops = data.get("stops") or []
    if not stops:
        return False
    normalize_ramp(ramp)
    while len(ramp.elements) > 1:
        ramp.elements.remove(ramp.elements[-1])
    try:
        ramp.interpolation = data.get("interpolation", 'LINEAR')
    except TypeError:
        ramp.interpolation = 'LINEAR'
    first = stops[0]
    ramp.elements[0].position = float(first["pos"])
    ramp.elements[0].color = tuple(first["color"])
    for stop in stops[1:]:
        elem = ramp.elements.new(float(stop["pos"]))
        elem.color = tuple(stop["color"])
    return True


def find_user_ramp(name):
    for entry in _user_ramps:
        if entry.get("name") == name:
            return entry
    return None


def create_ramp_texture(curve_obj):
    tex = bpy.data.textures.new(f"{curve_obj.name}_CurvePaintRamp", type='BLEND')
    tex.use_color_ramp = True
    tex.use_fake_user = True
    normalize_ramp(tex.color_ramp)
    tex.color_ramp.interpolation = 'LINEAR'
    return tex


class CURVEPAINT_OT_browse_profile_image(Operator, ImportHelper):
    bl_idname = "paint.curve_stroke_browse_image"
    bl_label = "Open Image"
    bl_description = "Browse for an image file on disk and use it as the profile image"
    bl_options = {'REGISTER'}

    filename_ext = ""
    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.bmp;*.tga",
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE'

    def execute(self, context):
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        try:
            img = bpy.data.images.load(self.filepath, check_existing=True)
        except RuntimeError as e:
            self.report({'ERROR'}, f"Could not load image: {e}")
            return {'CANCELLED'}
        props.profile_image = img
        return {'FINISHED'}


class CURVEPAINT_OT_add_ramp(Operator):
    bl_idname = "paint.curve_stroke_add_ramp"
    bl_label = "Add Color Ramp"
    bl_description = "Create a color ramp for this curve's profile"

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE'

    def execute(self, context):
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        props.profile_ramp_texture = create_ramp_texture(curve_obj)
        return {'FINISHED'}


class CURVEPAINT_OT_copy_ramp(Operator):
    bl_idname = "paint.curve_stroke_copy_ramp"
    bl_label = "Copy Ramp"
    bl_description = "Copy this curve's color ramp, to paste onto another curve"

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return (
            curve_obj is not None and curve_obj.type == 'CURVE'
            and curve_obj.curve_paint_settings.profile_ramp_texture is not None
        )

    def execute(self, context):
        global _ramp_clipboard
        _ramp_clipboard = context.active_object.curve_paint_settings.profile_ramp_texture
        self.report({'INFO'}, "Ramp copied")
        return {'FINISHED'}


class CURVEPAINT_OT_paste_ramp(Operator):
    bl_idname = "paint.curve_stroke_paste_ramp"
    bl_label = "Paste Ramp"
    bl_description = "Paste the copied color ramp onto this curve, as an independent copy"

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE' and _ramp_clipboard is not None

    def execute(self, context):
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        new_tex = _ramp_clipboard.copy()
        new_tex.name = f"{curve_obj.name}_CurvePaintRamp"
        new_tex.use_fake_user = True
        props.profile_ramp_texture = new_tex
        self.report({'INFO'}, "Ramp pasted (independent copy)")
        return {'FINISHED'}


# Each preset: (interpolation, [(position, value), ...], mapping). Stops
# authored for Mirror mapping assume position 0 is the curve's centerline,
# position 1 is the profile's outer edge; Full Width presets (Bevel) are
# inherently asymmetric and use the whole 0..1 span directly instead.
# Direction (Raise/Indent/Both) handles the sign separately, so these only
# need to vary shape, not polarity.
RAMP_PRESET_ITEMS = [
    ('SMOOTH', "Smooth", "Soft, rounded peak at the centerline"),
    ('SHARP', "Sharp", "Hard, angular peak at the centerline"),
    ('FLAT_TOP', "Flat Top", "Plateau at the centerline with sloped shoulders"),
    ('STEP', "Step", "Hard-edged band, no falloff until a sudden cutoff"),
    ('RAIL', "Rail", "Twin ridges flanking a neutral channel at the centerline"),
    ('COVED', "Coved", "Flat Top with softly rounded shoulders instead of hard corners"),
    ('BEVEL', "Bevel", "Flat on one side, sloped to neutral on the other (asymmetric, Full Width)"),
]

RAMP_PRESETS = {
    'SMOOTH': ('EASE', [(0.0, 1.0), (1.0, 0.0)], 'MIRROR'),
    'SHARP': ('LINEAR', [(0.0, 1.0), (1.0, 0.0)], 'MIRROR'),
    'FLAT_TOP': ('LINEAR', [(0.0, 1.0), (0.4, 1.0), (1.0, 0.0)], 'MIRROR'),
    'STEP': ('CONSTANT', [(0.0, 1.0), (0.5, 0.0)], 'MIRROR'),
    'RAIL': ('LINEAR', [(0.0, 0.5), (0.5, 1.0), (1.0, 0.5)], 'MIRROR'),
    'COVED': ('EASE', [(0.0, 1.0), (0.4, 1.0), (1.0, 0.0)], 'MIRROR'),
    'BEVEL': ('LINEAR', [(0.0, 1.0), (0.6, 1.0), (1.0, 0.0)], 'FULL'),
}


class CURVEPAINT_OT_apply_user_ramp(Operator):
    bl_idname = "paint.curve_stroke_apply_user_ramp"
    bl_label = "Apply Saved Ramp"
    bl_description = "Replace this curve's color ramp with a saved one"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(name="Name")

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE'

    def execute(self, context):
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings
        entry = find_user_ramp(self.name)
        if entry is None:
            self.report({'ERROR'}, f"No saved ramp named '{self.name}'")
            return {'CANCELLED'}
        if props.profile_ramp_texture is None:
            props.profile_ramp_texture = create_ramp_texture(curve_obj)
        if not dict_to_ramp(entry, props.profile_ramp_texture.color_ramp):
            self.report({'ERROR'}, f"Saved ramp '{self.name}' has no stops")
            return {'CANCELLED'}
        if entry.get("mapping"):
            props.ramp_mapping = entry["mapping"]
        props.profile_source = 'RAMP'
        self.report({'INFO'}, f"Applied saved ramp '{self.name}'")
        return {'FINISHED'}


class CURVEPAINT_OT_save_ramp(Operator):
    bl_idname = "paint.curve_stroke_save_ramp"
    bl_label = "Save Ramp"
    bl_description = "Save this curve's color ramp for reuse in any project"
    bl_options = {'REGISTER'}

    name: StringProperty(name="Name", default="My Ramp")
    overwrite: BoolProperty(
        name="Replace Existing",
        description="A saved ramp with this name already exists, replace it",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return (
            curve_obj is not None and curve_obj.type == 'CURVE'
            and curve_obj.curve_paint_settings.profile_ramp_texture is not None
        )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "name")
        if find_user_ramp(self.name.strip()) is not None:
            layout.prop(self, "overwrite")

    def execute(self, context):
        global _user_ramps
        props = context.active_object.curve_paint_settings
        name = self.name.strip()
        if not name:
            self.report({'ERROR'}, "Give the ramp a name")
            return {'CANCELLED'}

        existing = find_user_ramp(name)
        if existing is not None and not self.overwrite:
            self.report({'WARNING'}, f"'{name}' already exists, tick Replace Existing to overwrite")
            return {'CANCELLED'}

        entry = ramp_to_dict(
            props.profile_ramp_texture.color_ramp, name, props.ramp_mapping
        )
        if existing is not None:
            _user_ramps[_user_ramps.index(existing)] = entry
        else:
            _user_ramps.append(entry)
        _user_ramps.sort(key=lambda r: r.get("name", "").lower())

        problem = store_user_ramps()
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}
        self.report({'INFO'}, f"Saved ramp '{name}'")
        return {'FINISHED'}


class CURVEPAINT_OT_delete_user_ramp(Operator):
    bl_idname = "paint.curve_stroke_delete_user_ramp"
    bl_label = "Delete Saved Ramp"
    bl_description = "Remove this saved ramp"
    bl_options = {'REGISTER'}

    name: StringProperty(name="Name")

    def execute(self, context):
        global _user_ramps
        entry = find_user_ramp(self.name)
        if entry is None:
            return {'CANCELLED'}
        _user_ramps.remove(entry)
        problem = store_user_ramps()
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}
        self.report({'INFO'}, f"Deleted saved ramp '{self.name}'")
        return {'FINISHED'}


class CURVEPAINT_OT_manage_user_ramps(Operator):
    bl_idname = "paint.curve_stroke_manage_user_ramps"
    bl_label = "Manage Saved Ramps"
    bl_description = "Rename or delete saved ramps"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        if not _user_ramps:
            layout.label(text="Nothing saved yet")
            return
        for entry in _user_ramps:
            row = layout.row(align=True)
            row.label(text=entry.get("name", "?"))
            sub = row.row()
            sub.enabled = False
            sub.label(text=entry.get("mapping", ""))
            op = row.operator(
                "paint.curve_stroke_delete_user_ramp", text="", icon='X'
            )
            op.name = entry.get("name", "")

    def execute(self, context):
        return {'FINISHED'}


class CURVEPAINT_MT_ramp_presets(bpy.types.Menu):
    bl_idname = "CURVEPAINT_MT_ramp_presets"
    bl_label = "Presets"

    def draw(self, context):
        layout = self.layout
        curve_obj = context.active_object
        if curve_obj is None or curve_obj.type != 'CURVE':
            layout.label(text="No curve active")
            return
        props = curve_obj.curve_paint_settings
        mapping = props.ramp_mapping

        layout.label(text="Built-in")
        for key, label, _desc in RAMP_PRESET_ITEMS:
            op = layout.operator(
                "paint.curve_stroke_apply_ramp_preset", text=label
            )
            op.preset = key

        # Saved ramps are filtered to the active mapping, since a ramp
        # authored for one reads wrong under the other.
        matching = [r for r in _user_ramps if r.get("mapping") == mapping]
        hidden = len(_user_ramps) - len(matching)
        if matching:
            layout.separator()
            layout.label(text="Saved")
            for entry in matching:
                op = layout.operator(
                    "paint.curve_stroke_apply_user_ramp", text=entry["name"]
                )
                op.name = entry["name"]
        if hidden:
            layout.separator()
            row = layout.row()
            row.enabled = False
            row.label(text=f"{hidden} saved under the other mapping")
        if _user_ramps:
            layout.separator()
            layout.operator(
                "paint.curve_stroke_manage_user_ramps",
                text="Manage Saved...", icon='PREFERENCES',
            )


class CURVEPAINT_OT_apply_ramp_preset(Operator):
    bl_idname = "paint.curve_stroke_apply_ramp_preset"
    bl_label = "Apply Ramp Preset"
    bl_description = "Replace this curve's color ramp with a simple preset shape"
    bl_options = {'REGISTER', 'UNDO'}

    preset: EnumProperty(name="Preset", items=RAMP_PRESET_ITEMS)

    @classmethod
    def poll(cls, context):
        curve_obj = context.active_object
        return curve_obj is not None and curve_obj.type == 'CURVE'

    def execute(self, context):
        curve_obj = context.active_object
        props = curve_obj.curve_paint_settings

        if props.profile_ramp_texture is None:
            props.profile_ramp_texture = create_ramp_texture(curve_obj)

        ramp = props.profile_ramp_texture.color_ramp
        normalize_ramp(ramp)
        interpolation, stops, mapping = RAMP_PRESETS[self.preset]

        while len(ramp.elements) > 1:
            ramp.elements.remove(ramp.elements[-1])

        ramp.interpolation = interpolation
        first_pos, first_val = stops[0]
        ramp.elements[0].position = first_pos
        ramp.elements[0].color = (first_val, first_val, first_val, 1.0)
        for pos, val in stops[1:]:
            elem = ramp.elements.new(pos)
            elem.color = (val, val, val, 1.0)

        props.ramp_mapping = mapping
        props.profile_source = 'RAMP'

        self.report({'INFO'}, f"Applied '{self.preset.title()}' ramp preset")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Properties + Panel
# ---------------------------------------------------------------------------

# Blend Mode's available options depend on Direction. Under Raise or Indent,
# every value already shares one sign, which makes Max/Min/Greatest
# Deviation either exactly duplicate Add or become permanent no-ops, so
# they're only offered under Both, where they're genuinely distinct.
# Defined as module-level constants (not rebuilt inside the callback) since
# Blender's dynamic-items callbacks need the returned strings to stay alive
# beyond the call: recreating them each time risks a use-after-free crash.
_BLEND_MODE_BASE_ITEMS = (
    ('MIX', "Mix", "Blend toward the sampled value"),
    ('ADD', "Add", "Accumulate, raising or lowering"),
    ('AVERAGE', "Average", "Settle toward the midpoint of old and new, gentler than Add for a single crossing"),
)
_BLEND_MODE_BOTH_ONLY_ITEMS = (
    ('MAX', "Lighten (Max)", "Only ever raises; avoids double-stacking raised details"),
    ('MIN', "Darken (Min)", "Only ever lowers; avoids double-stacking grooves"),
    ('DEVIATION', "Greatest Deviation", "Keeps whichever effect is stronger, regardless of direction"),
)
_BLEND_MODE_ALL_ITEMS = _BLEND_MODE_BASE_ITEMS + _BLEND_MODE_BOTH_ONLY_ITEMS


def _blend_mode_items(self, context):
    if self.profile_direction == 'BOTH':
        return _BLEND_MODE_ALL_ITEMS
    return _BLEND_MODE_BASE_ITEMS


class CurvePaintProperties(PropertyGroup):
    """Lives on the curve object itself (Object.curve_paint_settings), so
    each curve remembers its own target and profile settings permanently."""
    target_obj: PointerProperty(
        name="Target",
        description="Mesh object whose UV space we're writing into",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )
    target_image: PointerProperty(
        name="Image",
        description="Image to write height/normal detail into (use Non-Color colorspace)",
        type=bpy.types.Image,
    )
    resample_distance: FloatProperty(
        name="Sample Spacing",
        description=(
            "Distance between samples taken along the curve. Lower values "
            "follow tight bends more closely; on gentle curves they mostly "
            "just cost more time"
        ),
        default=0.01,
        min=0.0001,
        soft_max=0.5,
        unit='LENGTH',
    )
    profile_source: EnumProperty(
        name="Source",
        description="What shapes the cross-section",
        items=[
            ('RAMP', "Color Ramp", "An editable ramp for the cross-section shape, no length variation"),
            ('IMAGE', "Image", "A grayscale image, with alpha as coverage"),
        ],
        default='RAMP',
    )
    profile_image: PointerProperty(
        name="Image",
        description="Grayscale image sampled across the curve; alpha (if present) acts as coverage",
        type=bpy.types.Image,
    )
    use_alpha: BoolProperty(
        name="Use Alpha",
        description=(
            "Treat the profile's alpha channel as a strength multiplier "
            "(coverage): alpha 0 means no effect there regardless of the "
            "height value. When off, alpha is ignored and treated as fully "
            "opaque everywhere"
        ),
        default=True,
    )
    profile_ramp_texture: PointerProperty(
        name="Ramp Host",
        description="Internal, hosts this curve's color ramp",
        type=bpy.types.Texture,
    )
    ramp_mapping: EnumProperty(
        name="Mapping",
        description="How the ramp maps across the profile's width",
        items=[
            ('FULL', "Full Width", "The ramp spans edge to edge, can be asymmetric"),
            ('MIRROR', "Mirrored", "The ramp defines one side (center to edge), reflected for the other"),
        ],
        default='FULL',
    )
    profile_rotate_90: BoolProperty(
        name="Rotate 90°",
        description="Run the image's horizontal axis along the stroke instead of its vertical one",
        default=False,
    )
    profile_half_width: FloatProperty(
        name="Half Width",
        description="Distance from the curve's centerline to each edge of the profile",
        default=0.02,
        min=0.0001,
        soft_max=0.5,
        unit='LENGTH',
    )
    profile_mode: EnumProperty(
        name="Mode",
        items=[
            ('STRETCH', "Stretch", "Map the profile once across the entire curve"),
            ('STAMP', "Stamp", "Repeat the profile at even spacing along the curve"),
        ],
        default='STRETCH',
    )
    profile_spacing: FloatProperty(
        name="Spacing",
        description=(
            "Gap between stamps, measured edge to edge, so resizing a stamp "
            "leaves the gap alone. Set zero for seamless. Overlap Stamp in "
            "Advanced switches this to centre-to-centre"
        ),
        default=0.05,
        min=0.0,
        unit='LENGTH',
    )
    profile_fit_to_curve: BoolProperty(
        name="Fit",
        description=(
            "Round the number of stamps to the nearest whole one and spread "
            "the leftover through the gaps, so the run finishes flush with "
            "the end of the curve. The stamps themselves are never resized"
        ),
        default=False,
    )
    profile_overlap_stamp: BoolProperty(
        name="Overlap Stamp",
        description=(
            "Switch Spacing to measure centre to centre instead of edge to "
            "edge, letting stamps sit on top of one another"
        ),
        default=False,
    )
    profile_skip_clipped: BoolProperty(
        name="Skip Clipped",
        description=(
            "Leave out a final stamp that would run past the end of the "
            "curve, instead of drawing it cut short"
        ),
        default=True,
    )
    profile_lock_stamp_ratio: BoolProperty(
        name="Lock Ratio",
        description="Take the stamp's length from the image's own proportions",
        default=True,
    )
    profile_stamp_size: FloatProperty(
        name="Stamp Size",
        description=(
            "Length of stamp along the curve."
        ),
        default=0.025,
        min=0.0001,
        unit='LENGTH',
    )
    profile_strength: FloatProperty(
        name="Strength",
        default=1.0,
        min=0.0,
        soft_max=4.0,
    )
    profile_quality: EnumProperty(
        name="Anti-Aliasing",
        description=(
            "Softens the stroke's outline by averaging several sample "
            "positions per pixel. Only affects the boundary, so it is worth "
            "enabling for hard-edged profiles on diagonal or curved "
            "strokes, and rarely worth it for soft-edged ones"
        ),
        items=[
            ('1', "Off", "One sample per pixel"),
            ('2', "2×2", "4 samples per pixel, roughly 4x the cost"),
            ('3', "3×3", "9 samples per pixel, roughly 9x the cost"),
        ],
        default='1',
    )
    profile_direction: EnumProperty(
        name="Direction",
        description="Which way the profile's sampled value drives the stroke",
        items=[
            ('RAISE', "Raise", "Profile treated as 0 = no effect, 1 = full raise"),
            ('BOTH', "Both", "Values above 0.5 raise, below 0.5 lower "),
            ('INDENT', "Indent", "Profile treated as 0 = no effect, 1 = full indent"),
        ],
        default='RAISE',
    )
    edge_feather: FloatProperty(
        name="Edge Feather",
        description="Applied as fraction of the half-width over which strength tapers to zero at boundary",
        default=0.06,
        min=0.0,
        max=0.5,
    )
    blend_mode: EnumProperty(
        name="Blend",
        items=_blend_mode_items,
    )
    cap_style: EnumProperty(
        name="Cap Style",
        description="Shape of the curve's start and end",
        items=[
            ('ROUND', "Round", "Rounded, capsule-style ends"),
            ('FLAT', "Flat", "Just flat"),
        ],
        default='ROUND',
    )
    relax_curve: BoolProperty(
        name="Relax Curve",
        description=(
            "Laplacian smoothing applied to the curve before sampling, "
            "averaging points to reduce jitter (e.g. from a Shrinkwrap source)"
        ),
        default=False,
    )
    relax_iterations: IntProperty(
        name="Iterations",
        description="Passes to apply. Higher removes more jitter but can also soften intentional shape",
        default=3,
        min=1,
        max=20,
    )
    blur_size: FloatProperty(
        name="Size (px)",
        description=(
            "How many neighboring pixels blend together. Higher reaches "
            "further and blurs more, but costs more to compute"
        ),
        default=2.0,
        min=0.0,
        soft_max=20.0,
    )
    blend_margin: FloatProperty(
        name="Blend Margin",
        description=(
            "How far past the stroke's own edge the blur reaches, fading "
            "to nothing. Lets the stroke blend into its surroundings "
            "instead of stopping at a hard edge"
        ),
        default=0.01,
        min=0.0,
        soft_max=0.1,
        unit='LENGTH',
    )
    blur_strength: FloatProperty(
        name="Strength",
        description="Overall blend amount toward the blurred result",
        default=1.0,
        min=0.0,
        max=1.0,
    )
    blur_mask_mode: EnumProperty(
        name="Mask",
        description="Where the blur applies within the stroke",
        items=[
            ('EDGES', "Edges Only", "Only blurs near the width boundary, tapering inward"),
            ('UNIFORM', "Uniform", "Blurs evenly across the whole stroke"),
        ],
        default='EDGES',
    )
    blur_edge_threshold: FloatProperty(
        name="Edge Threshold",
        description="Distance from center (as a fraction of half-width) where the edge blur starts",
        default=0.6,
        min=0.0,
        max=0.99,
    )
    blur_auto_apply: BoolProperty(
        name="Auto-Apply After Paint",
        description="Automatically run the blur pass right after Draw",
        default=False,
    )


class CURVEPAINT_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    debug_timing: BoolProperty(
        name="Print Timing Breakdown",
        description=(
            "Print a per-stage timing breakdown to the system console after "
            "each Draw and Blur. Useful when reporting that something is "
            "slow: the proportions between stages say far more than the "
            "total does"
        ),
        default=False,
    )

    debug_seams: BoolProperty(
        name="Print Seam Diagnostics",
        description=(
            "Report, per stroke, which samples were dropped for looking "
            "like they straddle a UV seam and why. Use this when a stroke "
            "grows a stray tail or lands on the wrong UV island"
        ),
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "debug_timing")
        layout.prop(self, "debug_seams")
        col = layout.column(align=True)
        col.label(text="Output goes to the system console:", icon='CONSOLE')
        col.label(text="Window > Toggle System Console (Windows), or launch Blender from a terminal")


def draw_profile_readout(layout, curve_obj, props):
    """The numbers you would otherwise work out by drawing, looking, and
    adjusting: how long the curve is, and how many stamps that comes to."""
    lines = []
    length = curve_display_length(curve_obj)
    if length is None:
        lines.append("L = ? (non-uniform scale)")
    else:
        lines.append(f"L = {length * 100.0:.1f} cm")

    if props.profile_mode == 'STAMP' and length:
        image = props.profile_image
        stamp_size = max(props.profile_stamp_size, 0.0001)
        if props.profile_source == 'IMAGE' and props.profile_lock_stamp_ratio and image is not None:
            w, h = image.size
            if w and h:
                along, across = (w, h) if props.profile_rotate_90 else (h, w)
                stamp_size = max(2.0 * max(props.profile_half_width, 0.0001) * (along / across), 0.0001)
        period, count, gap, fitted, clipped = solve_stamp_layout(props, stamp_size, length)
        detail = f"{count} pcs ({stamp_size * 100.0:.2f} cm)"
        # Only worth reporting the gap when the addon chose it. Otherwise it
        # is just the number already sitting in the Spacing field.
        if fitted:
            detail += f"  {gap * 100.0:.2f} cm gap"
        elif clipped:
            detail += "  clipped"
        lines.append(detail)

    col = layout.column(align=True)
    col.enabled = False
    for line in lines:
        col.label(text=line)


class CURVEPAINT_PT_panel(Panel):
    bl_label = "Path to Normalcy"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "P2N"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        curve_obj = context.active_object

        selected_curves = [o for o in context.selected_objects if o.type == 'CURVE']
        if len(selected_curves) > 1:
            layout.label(text=f"{len(selected_curves)} curves selected", icon='CURVE_DATA')
            row = layout.row(align=True)
            row.operator("paint.curve_stroke_paint_selected", icon='BRUSH_DATA', text="Paint Selected")
            row.operator("paint.curve_stroke_undo_selected", icon='LOOP_BACK', text="Undo Selected")
            erase_header, erase_box = layout.panel("curve_paint_erase_multi", default_closed=True)
            erase_header.label(text="Erase", icon='X')
            if erase_box:
                erase_box.operator(
                    "paint.curve_stroke_erase_selected", icon='X', text="Erase Selected"
                )
                row = erase_box.row(align=True)
                row.label(text="Clears overlaps too", icon='ERROR')
            return

        if curve_obj is None or curve_obj.type != 'CURVE':
            layout.label(text="Select a curve to edit its settings", icon='INFO')
            return

        props = curve_obj.curve_paint_settings

        layout.label(text=f"Curve: {curve_obj.name}", icon='CURVE_DATA')

        draw_status(layout, curve_obj, props)

        col = layout.column(align=True)
        col.prop(props, "target_obj")
        col.prop(props, "target_image")

        layout.prop(props, "resample_distance")

        split = layout.split(factor=0.5, align=True)
        split.label(text="Cap Style")
        split.prop(props, "cap_style", text="")

        layout.row().prop(props, "profile_mode", expand=True)
        layout.row().prop(props, "profile_source", expand=True)

        box = layout.box()
        if props.profile_source == 'IMAGE':
            row = box.row(align=True)
            row.prop(props, "profile_image", text="")
            row.operator("paint.curve_stroke_browse_image", text="", icon='FILEBROWSER')
        elif props.profile_source == 'RAMP':
            split = box.split(factor=0.5, align=True)
            split.label(text="Mapping")
            split.prop(props, "ramp_mapping", text="")
            box.menu("CURVEPAINT_MT_ramp_presets", text="Presets", icon='PRESET')
            split = box.split(factor=0.5, align=True)
            split.operator(
                "paint.curve_stroke_save_ramp", text="Save", icon='FILE_TICK'
            )
            split.label(text="")
            if props.profile_ramp_texture is None:
                row = box.row(align=True)
                row.operator("paint.curve_stroke_add_ramp", icon='ADD', text="Add")
                row.operator("paint.curve_stroke_paste_ramp", icon='PASTEDOWN', text="Paste")
            else:
                box.template_color_ramp(props.profile_ramp_texture, "color_ramp", expand=True)
                row = box.row(align=True)
                row.operator("paint.curve_stroke_copy_ramp", icon='COPYDOWN', text="Copy")
                row.operator("paint.curve_stroke_paste_ramp", icon='PASTEDOWN', text="Paste")

        draw_profile_readout(box, curve_obj, props)

        split = box.split(factor=0.75, align=True)
        split.prop(props, "use_alpha")
        if props.profile_source == 'IMAGE':
            split.prop(props, "profile_rotate_90", text="", icon='CON_ROTLIKE', toggle=True)

        if props.profile_source == 'IMAGE' and props.profile_mode == 'STAMP':
            box.prop(props, "profile_lock_stamp_ratio")

        box.prop(props, "profile_half_width")
        if props.profile_mode == 'STAMP':
            if props.profile_source == 'IMAGE':
                if not props.profile_lock_stamp_ratio:
                    box.prop(props, "profile_stamp_size")
            else:
                box.prop(props, "profile_stamp_size")
            # A quarter of the row for the tick and its label, the rest for
            # the distance field, which still has room for its own.
            split = box.split(factor=0.25, align=True)
            # Fitting works by adjusting the gaps, so with no gap there is
            # nothing for it to adjust.
            tick = split.row(align=True)
            tick.enabled = props.profile_spacing > 0.0
            tick.prop(props, "profile_fit_to_curve", text="Fit")
            split.prop(props, "profile_spacing")
        box.prop(props, "profile_strength")
        box.prop(props, "edge_feather")

        split = box.split(factor=0.5, align=True)
        split.label(text="Anti-Aliasing")
        split.prop(props, "profile_quality", text="")

        split = box.split(factor=0.5, align=True)
        split.label(text="Direction")
        split.prop(props, "profile_direction", text="")

        split = box.split(factor=0.5, align=True)
        split.label(text="Blend")
        split.prop(props, "blend_mode", text="")

        layout.separator()
        row = layout.row(align=True)
        row.operator("paint.curve_stroke", icon='BRUSH_DATA', text="Draw")
        row.operator("paint.curve_stroke_redraw", icon='FILE_REFRESH', text="Redraw")
        layout.operator("paint.curve_stroke_undo", icon='LOOP_BACK')

        blur_header, blur_box = layout.panel("curve_paint_blur_pass", default_closed=True)
        blur_header.label(text="Blur Pass", icon='NODE_TEXTURE')
        if blur_box:
            blur_box.prop(props, "blur_mask_mode")
            if props.blur_mask_mode == 'EDGES':
                blur_box.prop(props, "blur_edge_threshold")
            blur_box.prop(props, "blur_size")
            blur_box.prop(props, "blend_margin")
            blur_box.prop(props, "blur_strength")
            blur_box.prop(props, "blur_auto_apply")
            blur_box.operator("paint.curve_stroke_blur", icon='MOD_SMOOTH')

        relax_header, relax_box = layout.panel("curve_paint_relax", default_closed=True)
        relax_header.label(text="Relax Curve", icon='MOD_SMOOTH')
        if relax_box:
            relax_box.prop(props, "relax_curve")
            row = relax_box.row(align=True)
            row.label(text="For jittery curves", icon='INFO')
            if props.relax_curve:
                relax_box.prop(props, "relax_iterations")

        adv_header, adv_box = layout.panel("curve_paint_advanced", default_closed=True)
        adv_header.label(text="Advanced", icon='PREFERENCES')
        if adv_box:
            adv_box.prop(props, "profile_overlap_stamp")
            sub = adv_box.row()
            # Fitting removes the partial stamp entirely, so there is
            # nothing left for this to skip.
            sub.enabled = not (props.profile_fit_to_curve and props.profile_spacing > 0.0)
            sub.prop(props, "profile_skip_clipped")

        erase_header, erase_box = layout.panel("curve_paint_erase", default_closed=True)
        erase_header.label(text="Erase", icon='X')
        if erase_box:
            erase_box.operator("paint.curve_stroke_erase", icon='X', text="Erase Stroke")
            row = erase_box.row(align=True)
            row.label(text="Clears overlaps too", icon='ERROR')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    CURVEPAINT_AddonPreferences,
    CurvePaintProperties,
    CURVEPAINT_OT_stroke,
    CURVEPAINT_OT_blur,
    CURVEPAINT_OT_undo,
    CURVEPAINT_OT_redraw,
    CURVEPAINT_OT_paint_selected,
    CURVEPAINT_OT_undo_selected,
    CURVEPAINT_OT_erase,
    CURVEPAINT_OT_erase_selected,
    CURVEPAINT_OT_browse_profile_image,
    CURVEPAINT_OT_add_ramp,
    CURVEPAINT_OT_copy_ramp,
    CURVEPAINT_OT_paste_ramp,
    CURVEPAINT_OT_apply_ramp_preset,
    CURVEPAINT_OT_apply_user_ramp,
    CURVEPAINT_OT_save_ramp,
    CURVEPAINT_OT_delete_user_ramp,
    CURVEPAINT_OT_manage_user_ramps,
    CURVEPAINT_MT_ramp_presets,
    CURVEPAINT_PT_panel,
)


def _remove_handler(handler_list, func):
    """Remove by name rather than identity: after a reload the list can
    still hold a function object from the previous module instance, which
    won't compare equal to the freshly imported one."""
    for existing in list(handler_list):
        if getattr(existing, "__name__", None) == func.__name__:
            handler_list.remove(existing)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.curve_paint_settings = PointerProperty(type=CurvePaintProperties)

    load_user_ramps()

    _remove_handler(bpy.app.handlers.depsgraph_update_post, _invalidate_uv_lookup)
    bpy.app.handlers.depsgraph_update_post.append(_invalidate_uv_lookup)
    _remove_handler(bpy.app.handlers.load_post, _clear_caches_on_load)
    bpy.app.handlers.load_post.append(_clear_caches_on_load)


def unregister():
    _remove_handler(bpy.app.handlers.depsgraph_update_post, _invalidate_uv_lookup)
    _remove_handler(bpy.app.handlers.load_post, _clear_caches_on_load)
    _uv_lookup_cache.clear()
    _backup_cache.clear()

    del bpy.types.Object.curve_paint_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
