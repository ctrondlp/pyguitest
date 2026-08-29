#!/usr/bin/env python3
"""Find a control by a picture of it, when the accessible tree cannot see it.

The escape hatch. AT-SPI is the right tool for anything that exposes
itself -- buttons, entries, menus -- but plenty does not: a canvas, a
video player's overlay, a game, an application drawing its own widgets, or
anything inside a remote-desktop viewer. For those, the only thing left is
the pixels.

It works by capturing the screen and searching that image for a smaller
template image you supply, via ImageMagick's `compare -subimage-search`.

    python3 examples/08_find_by_image.py button.png
    python3 examples/08_find_by_image.py button.png "Text Editor"

Making the template: take a screenshot with example 05, then crop the
control out of it -- tightly, and from *this* machine. A template carries
the theme, font rendering, scaling and colour profile of wherever it was
cropped, so one made on another desktop will usually not match.
"""

import sys

import pyguitest
from pyguitest import Capability, ImageNotFound

if len(sys.argv) < 2:
    sys.exit(__doc__)

template = sys.argv[1]
within_title = sys.argv[2] if len(sys.argv) > 2 else None

gui = pyguitest.connect()

for capability in (Capability.SCREEN_CAPTURE, Capability.IMAGE_LOCATE):
    if not gui.supports(capability):
        sys.exit(
            f"{capability.name} is unavailable. Template matching needs both "
            "a way to capture the screen and ImageMagick's `compare` -- run "
            "`pyguitest doctor` for what to install."
        )

# Narrowing to one window is worth doing when you can: it is faster, and it
# removes the chance of matching an identical control in a different window.
# The match still comes back in screen coordinates either way, so the
# result can be clicked without adding the window's own offset back on.
window = None
if within_title is not None:
    if not gui.supports(Capability.WINDOW_GEOMETRY):
        sys.exit("Restricting to a window needs WINDOW_GEOMETRY; see example 01.")
    window = gui.find_window(within_title)

try:
    match = gui.locate_image(template, within=window)
except ImageNotFound:
    sys.exit(
        f"{template} was not found on screen.\n\n"
        "Usually the template, not the search: it must be cropped from this\n"
        "machine, at this scaling and theme. Check it opens and shows only\n"
        "the control. A threshold can also be passed to accept a looser\n"
        "match -- with none, the single best match is always returned."
    )

print(f"found at {match.x},{match.y} (score {match.score})")

# Aim at the middle rather than the corner: the top-left of a match sits on
# the control's edge, where a click can land on a border or just outside.
if gui.supports(Capability.POINTER_MOVE):
    gui.move_mouse(match.x + match.width // 2, match.y + match.height // 2)
    print("pointer moved to its centre (not clicking, to stay harmless)")
