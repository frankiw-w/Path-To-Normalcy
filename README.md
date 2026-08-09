An easy path to painting details with curves!

Path to normalcy lets you draw directly to a bump map using a Curve-object, wrap a curve onto your mesh draw a path where you want to paint and draw!

# Features
- One click draw using calculated UV coordinates from Curve-object, no more camera projection bullshit!
- Per Curve-object settings: hide curves while you work, come back and redraw them later, nothing's locked in until you're happy.
- Use an image or color ramp as brush, infinite possibilities with images or use a simple preset color ramp.
- Six blend modes plus Raise/Indent/Both, mix and match for exactly the overlap behavior you want.
- Curve looking jittery from Shrinkwrap? Relax Curve smooths it out before painting.
- Draw one or several curves with one click!
- We got draw modes, Stretch and Stamp! Draw continuous seams or stamp buttons, or maybe both?
- Bake straight to a normal map when you're done, or keep it as bump.

# Creators note
I built this for painting seams, stichting and other details directly to a normalmap, without having to deal with Blenders native texture paint or baking from hi-mesh. I guess you can do other stuff with it too but keep in mind it was solely built and tested for this purpose. 

You could possibly use the same output to use as mask for other layers but I haven't dabbled with that yet. 

# IMPORTANT READ BEFORE USE
- Must have UVs! 
- Must set up material with bump map! (highly recommend pairing with Ucupaint)
- Strokes over UV-seams will look like shit!
- Use Shrinkwrap modifier on curve object, curve has to lie against the surface!
- Curve must be a plain flat wire, no Bevel, Extrude, or Fill, or results are undefined.
- Settings are local to each curve!
- Image must be set to non-color inside Blender! There's a check but it will still draw.
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

- **Curve Fidelity**: Resample distance or practically, how tightly bends get traced. Leave alone
  unless a sharp corner looks faceted.
- **Cap Style**: Round or Flat ends.
- **Stretch / Stamp**: Stretch maps your profile once along the whole
  curve. Stamp repeats it at even spacing (rivets, stitching, bolts).
- **Color Ramp/Image**: Chooses source of your cross-section profile.
- **Settings Frame**: Holds the settings that define your profile from Source: Half Width, Strength, Edge Feather, Quality, Direction, and Blend.
- **Blur Pass** *(collapsed by default)*: Optional post-processing pass, applies blur smoothing along the painted area. Comes in 'Uniform' and 'Edges only' flavors, does what it says on the box.
- **Relax Curve** *(collapsed by default)*: Should only be used if your shrinkwrap modifier produces a jittery, segmented or an otherwise non-smooth curve. Applies a Laplacian smoothing on the curve before sampling.

## Choosing Profile Source

- **Color Ramp**: Uses a color ramp to define profile, includes a few presets to get you started. Can be mapped to Full Width or to be Mirrored from centerline.
- **Image**: Uses a grayscale image for sampling, must be set to non-color colorspace inside Blender.

- **Midpoints**: See the Direction remapping note above before creating your own image, using the wrong neutral value there is the most common mistake.

## Raise, Indent, or Both?

**Direction** controls which way your profile pushes:

- **Raise**: always bumps up.
- **Indent**: always carves in.
- **Both**: lets one profile do both at once (values above the middle
  raise, below it indent). Unlocks a few extra Blend Mode options too.

## Stamp Mode

- **Spacing** = distance between repeats.
- **Stamp Size** = how much of that spacing your shape actually fills.
  Smaller than Spacing = a gap between repeats. Equal = no gap.
- **Use Image Ratio** sizes it automatically from your image's own
  proportions, turn it off to set Stamp Size by hand.

## Workflow Tips

- **One curve per stroke.** Settings are local and saved on the Curve object, hide the ones you're not working on and come back to them later without losing anything.
- **Redraw** (next to Draw) re-applies a curve's last stroke with
  current settings, handy after nudging the curve or tweaking a value.
- **Paint Selected / Undo Selected** show up automatically whenever more
  than one curve is selected, letting you refresh your whole set of
  strokes in one click before baking.
- **Undo** here is separate from Blender's own Ctrl+Z. Mixing this with Blender's native Ctrl+Z can leave the two undo histories out of sync, when in doubt, use the addon's own Undo button.
- When you're happy, **bake to a normal map** for export, or leave it as
  a bump map for a purely in-Blender look.
