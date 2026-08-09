Paint to bump/normal maps directly from the viewport using Curve-objects to define your strokes. 

# Features
- One click draw onto UV coordinates, no more camera projection bullshit! 
- Per Curve-object setting, settings are local to individual Curves.
- Use an image or color ramp as brush, infinate possiblities with images or use a simple preset color ramp. 
- Set Blend-mode and Direction, additive, subtractive, min, max and bulldozer in either direction or BOTH! 
- Draw one or several curves with one click!
- Draw modes, Stretch and Stamp! Draw continuous seams or stamp buttons, or maybe both?


# IMPORTANT 
- Use Shrinkwrap modifier on curve object, curve has to lie agianst the surface! 
- Settings are local to each curve! 
- Image must be non-color! It will warn you but still draw. 
- When using Raise or Indent 0 = 0.5 and 1 = 1, mapped to 0.5 midpoint. When using mode "Both" midpoint is simply 0.5. 






# Path to Normalcy: User Guide

## Quick Start

1. **Add a curve** and shape it along the detail you want (a seam, a
   panel line, a row of rivets). A NURBS Path or Bezier curve both work.
2. **Snap it to the surface** with a Shrinkwrap modifier (Project mode
   tends to track the surface more cleanly than Nearest Surface Point).
3. Select the curve, open the **P2N** tab in the N-panel.
4. Set **Target** (the mesh) and **Image** (the texture you're painting
   into, set to **Non-Color** colorspace).
5. Pick a **Source**: a **Color Ramp** (click Presets for an instant
   shape) or an **Image**.
6. Hit **Draw**.

That's it for a single stroke. For more detail, add another curve and
repeat, one curve per detail is the intended workflow, not one curve
doing everything.

## The Panel, Briefly

- **Curve Fidelity**: how tightly bends get traced. Leave it alone
  unless a sharp corner looks faceted.
- **Cap Style**: Round or Flat ends.
- **Relax Curve** *(collapsed by default)*: turn on if your curve looks
  jittery, usually from Shrinkwrap. Not needed otherwise.
- **Stretch / Stamp**: Stretch maps your profile once along the whole
  curve. Stamp repeats it at even spacing (rivets, stitching, bolts).
- **Profile box**: your cross-section shape and its settings (Half
  Width, Strength, Direction, Blend, and so on).
- **Blur Pass** *(collapsed by default)*: optional softening after
  painting. Leave off unless edges look too hard.

## Choosing a Profile

- **Color Ramp**: fastest way to get a real result. Click **Presets**
  for a ready-made shape, or build your own. **Mapping: Mirror** mirrors
  one side you design across both; **Full Width** lets each side differ.
- **Image**: use for anything a ramp can't do, asymmetric shapes,
  texture detail, or repeating patterns along the curve's length. Must
  also be **Non-Color** colorspace. If it uses transparency, turn on
  **Use Alpha**, and make sure the fully-transparent areas are genuinely
  transparent, not just faded, or you'll get artifacts.

## Raise, Indent, or Both?

**Direction** controls which way your profile pushes:

- **Raise**: always bumps up. Simplest option for most detail.
- **Indent**: always carves in.
- **Both**: lets one profile do both at once (values above the middle
  raise, below it indent). Unlocks a few extra Blend Mode options too.

Same profile, either direction, no need to make two versions of it.

## Stamp Mode Cheat Sheet

- **Spacing** = distance between repeats.
- **Stamp Size** = how much of that spacing your shape actually fills.
  Smaller than Spacing = a gap between repeats. Equal = no gap.
- **Use Image Ratio** sizes it automatically from your image's own
  proportions, turn it off to set Stamp Size by hand.

## Workflow Tips

- **One curve per detail.** Each curve remembers its own settings
  completely, hide the ones you're not working on and come back to them
  later without losing anything.
- **Redraw** (next to Draw) re-applies a curve's last stroke with
  current settings, handy after nudging the curve or tweaking a value.
- **Paint Selected / Undo Selected** show up automatically whenever more
  than one curve is selected, letting you refresh your whole set of
  strokes in one click before baking.
- **Undo** here is separate from Blender's own Ctrl+Z. Use the addon's
  Undo button to undo a paint stroke specifically.
- When you're happy, **bake to a normal map** for export, or leave it as
  a bump map for a purely in-Blender look.

## Common Gotchas

- Forgot **Non-Color** on an image? Colors will look subtly wrong;
  you'll get a warning when you paint, but it's easy to miss.
- Curve has **Bevel/Extrude/Fill** turned on? Don't, this addon expects
  a plain flat curve with no thickness.
- Nothing painting? Check **Target**, **Image**, and (for Color Ramp)
  that you've actually added a ramp, Draw stays disabled until all
  three are set.
