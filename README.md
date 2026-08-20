An easy path to painting details with curves!

Path to normalcy lets you draw directly to a bump map using a Curve-object, wrap a curve onto your mesh draw a path where you want to paint and draw!

# Features
- One click draw using calculated UV coordinates from Curve-object, no more camera projection bullshit!
- Per Curve-object settings: hide curves while you work, come back and redraw them later, nothing's locked in until you're happy.
- Use an image or color ramp as brush, infinite possibilities with images or use a simple preset color ramp.
- Save your own color ramps and reuse them in any project, not just this one.
- Six blend modes plus Raise/Indent/Both, mix and match for exactly the overlap behavior you want.
- Curve looking jittery from Shrinkwrap? Relax Curve smooths it out before painting.
- Draw one or several curves with one click!
- We got draw modes, Stretch and Stamp! Draw continuous seams or stamp buttons, or maybe both?
- Fit to Curve works out the spacing for you so a row of stamps ends flush with the end of the curve.
- Erase a stroke back to neutral without touching anything around it.
- Strokes carry across UV seams, no gaps and no shading artifacts at the join.
- Bake straight to a normal map when you're done, or keep it as bump.

# Creators note
I built this for painting seams, stichting and other details directly to a normalmap, without having to deal with Blenders native texture paint or baking from hi-mesh. I guess you can do other stuff with it too but keep in mind it was solely built and tested for this purpose. 

You could possibly use the same output to use as mask for other layers but I haven't dabbled with that yet. 

# IMPORTANT READ BEFORE USE
- Must have UVs! 
- Must set up material with bump map! (highly recommend pairing with Ucupaint)
- Use Shrinkwrap modifier on curve object, curve has to lie against the surface!
- Curve must be a plain flat wire, no Bevel, Extrude, or Fill, or results are undefined.
- Settings are local to each curve!
- Image must be set to non-color inside Blender! The panel will tell you if it isn't, but it will still draw.
- Anything wrong with your setup shows up as a message at the top of the panel, red for something that will stop a stroke, plain for something that will just surprise you.
- "Direction" remaps the source values accordingly:
   - Raise and Indent each remap the source to one half of the 0-1 range: 0.5-1 for Raise, 0-0.5 for Indent.
   - 'Both' uses the full range 0-1


# User Guide

## Quick Start

1. **Add a curve** and shape it along the detail you want (a seam, a
   panel line, a row of rivets). A NURBS Path or Bezier curve both work.
2. **Snap it to the surface** with a Shrinkwrap modifier (Project mode
   tends to track the surface more cleanly than Nearest Surface Point).
3. Select the curve, open the **P2N** tab in the N-panel.
4. Set **Target** (the mesh) and **Image** (the texture you're painting
   onto, your bump map).
5. Pick a **Source**: a **Color Ramp** (click Presets for an instant
   shape) or an **Image**.
6. Hit **Draw**.

The intended workflow is one curve for each stroke/detail, NOT built to use the same Curve for all strokes! Since settings are per object you can duplicate curves to copy settings or create a new curve for defaults.

## The Panel

- **Sample Spacing**: How far apart the curve gets sampled, or practically,
  how tightly bends get traced. Leave alone unless a sharp corner looks
  faceted.
- **Cap Style**: Round or Flat ends.
- **Stretch / Stamp**: Stretch maps your profile once along the whole
  curve. Stamp repeats it at even spacing (rivets, stitching, bolts).
- **Color Ramp/Image**: Chooses source of your cross-section profile.
- **Readout**: Under the ramp or image you get the curve's length, and in
  Stamp mode how many stamps that comes to and how long each one is. Saves
  drawing once just to find out.
- **Settings**: Half Width, Strength, Edge Feather, Anti-Aliasing,
  Direction, and Blend.
- **Blur Pass** *(collapsed by default)*: Optional post-processing pass, applies blur smoothing along the painted area. Comes in 'Uniform' and 'Edges Only' flavors, does what it says on the box.
- **Relax Curve** *(collapsed by default)*: Should only be used if your shrinkwrap modifier produces a jittery, segmented or an otherwise non-smooth curve. Applies a Laplacian smoothing on the curve before sampling.
- **Advanced** *(collapsed by default)*: Overlap Stamp and Skip Clipped, see Stamp Mode below.
- **Erase** *(collapsed by default)*: Flattens this curve's area back to
  neutral. Note it clears whatever else was painted there too, so
  overlapping strokes lose their overlap.

## Choosing Profile Source

- **Color Ramp**: Uses a color ramp to define profile, includes a few presets to get you started. Can be mapped to Full Width or to be Mirrored from centerline.
- **Image**: Uses a grayscale image for sampling, must be set to non-color colorspace inside Blender. The image's vertical axis runs along the stroke by default; the toggle next to Use Alpha swaps it to the horizontal one.

- **Midpoints**: See the Direction remapping note above before creating your own image, using the wrong neutral value there is the most common mistake.

## Saving Color Ramps

Built a ramp you want again later? Hit **Save** under the Presets menu and
give it a name. It goes in your Blender config, not the .blend, so it
turns up in every project.

Saved ramps appear in the Presets menu under the built-in ones. They're
filtered to the Mapping you're currently on, since a ramp built for
Mirrored reads wrong on Full Width; if some are hidden the menu says so.
**Manage Saved** at the bottom of the menu deletes them.

## Raise, Indent, or Both?

**Direction** controls which way your profile pushes:

- **Raise**: always bumps up.
- **Indent**: always carves in.
- **Both**: lets one profile do both at once (values above the middle
  raise, below it indent). Unlocks a few extra Blend Mode options too.

## Stamp Mode

- **Spacing** = the gap between stamps, measured edge to edge. Resize a
  stamp and the gap stays put. Set it to zero for a seamless run.
- **Fit** (the tickbox next to Spacing) = rounds the number of stamps to
  the nearest whole one and spreads the leftover through the gaps, so the
  run ends flush with the end of the curve. Your stamps never get
  stretched, only the gaps move. Needs a gap to work with, so it greys out
  at zero spacing.
- **Stamp Size** = how long each stamp is along the curve.
- **Lock Ratio** sizes it automatically from your image's own
  proportions, turn it off to set Stamp Size by hand.
- **Skip Clipped** *(Advanced, on by default)* = leaves out a final stamp
  that would run off the end of the curve rather than drawing it cut
  short. Irrelevant when Fit is on, so it greys out.
- **Overlap Stamp** *(Advanced)* = switches Spacing to measure centre to
  centre instead, letting stamps sit on top of each other. Handy for weld
  beads, rarely what you want otherwise.

## UV Seams

Strokes crossing a seam are handled: the stroke runs up to the island
edge, picks up on the far island, and carries a few pixels past each edge
so the Bump node doesn't produce a line at the join. Arc length carries
across too, so a stamped run doesn't restart counting on the other side.

Running a stroke *along* a seam rather than across one is not supported
yet, and very shallow crossings can still drop part of the stroke.

## Workflow Tips

- **One curve per stroke.** Settings are local and saved on the Curve object, hide the ones you're not working on and come back to them later without losing anything.
- **Redraw** (next to Draw) re-applies a curve's last stroke with
  current settings, handy after nudging the curve or tweaking a value.
- **Paint Selected / Undo Selected / Erase Selected** show up automatically
  whenever more than one curve is selected, letting you refresh your whole
  set of strokes in one click before baking.
- **Anti-Aliasing is off by default** and that's usually right. It only
  softens the stroke's outline, so it earns its cost on hard-edged
  profiles running diagonally, and does nothing visible on a soft-edged
  one. It costs roughly 4x at 2x2 and 9x at 3x3.
- **Undo** here is separate from Blender's own Ctrl+Z. Mixing this with Blender's native Ctrl+Z can leave the two undo histories out of sync, when in doubt, use the addon's own Undo button.
- **Undo only lasts the session.** Close Blender and the last stroke can't
  be reverted any more, though the curve keeps its settings and can always
  be redrawn.
- When you're happy, **bake to a normal map** for export, or leave it as
  a bump map for a purely in-Blender look.
