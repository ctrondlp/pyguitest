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

That last sentence is why there is no sample template committed here, and
why this has a demo that makes its own:

    python3 examples/08_find_by_image.py --demo

which generates a synthetic haystack and a template cut from it into
examples/images/, then searches one for the other. No screenshot, no
desktop, no window -- just ImageMagick -- so it is the quickest way to
confirm template matching works at all on this machine before trying it
against a real screen.
"""

import pathlib
import sys

import pyguitest
from pyguitest import Capability, ImageNotFound, png

IMAGES = pathlib.Path(__file__).resolve().parent / "images"
HAYSTACK, TEMPLATE_AT, TEMPLATE_SIZE = (240, 160), (96, 60), (24, 16)


def _pattern(width, height, x0=0, y0=0):
    """Position-unique pixels: every coordinate a different colour.

    A flat image would let `compare` match anywhere, and a demo that passes
    for that reason would be showing nothing. With these, the template can
    only match where it was cut from.
    """
    return [
        bytes(
            channel
            for x in range(x0, x0 + width)
            for channel in ((x * 7) % 256, (y * 11) % 256, (x * y) % 256)
        )
        for y in range(y0, y0 + height)
    ]


def _demo():
    """Generate the pair, search one for the other, and report."""
    IMAGES.mkdir(exist_ok=True)
    haystack = IMAGES / "haystack.png"
    template = IMAGES / "button.png"
    # Written rather than committed: it keeps binary files out of the tree,
    # and generating them here exercises pyguitest's own PNG encoder.
    png.write_rgb(str(haystack), *HAYSTACK, _pattern(*HAYSTACK))
    png.write_rgb(str(template), *TEMPLATE_SIZE, _pattern(*TEMPLATE_SIZE, *TEMPLATE_AT))
    print(
        f"wrote {haystack.name} {HAYSTACK[0]}x{HAYSTACK[1]} and "
        f"{template.name} {TEMPLATE_SIZE[0]}x{TEMPLATE_SIZE[1]}, "
        f"cut from {TEMPLATE_AT}"
    )

    gui = pyguitest.connect(backend="imagesearch")
    if not gui.supports(Capability.IMAGE_LOCATE):
        sys.exit(
            "IMAGE_LOCATE is unavailable -- template matching needs "
            "ImageMagick's `compare`. Run `pyguitest doctor`."
        )
    # locate() searches one file for another, where locate_image() below
    # captures the screen first. Reached through the session's forwarding to
    # its backend, the same route X11Backend.pointer_position uses.
    match = gui.locate(str(haystack), str(template))
    gui.close()

    print(f"found at {match.x},{match.y} (score {match.score})")
    if (match.x, match.y) != TEMPLATE_AT:
        sys.exit(f"expected {TEMPLATE_AT} -- something is wrong with the match")
    print(
        "\nExactly where it was cut from, and a score of 0.0 for an exact\n"
        "match. Against a real screen it will not be 0.0: anti-aliasing and\n"
        "compositing move the pixels slightly, which is what `threshold` is\n"
        "for. See images/README.md for making a template of your own."
    )
    return 0


if len(sys.argv) < 2:
    sys.exit(__doc__)

if sys.argv[1] == "--demo":
    sys.exit(_demo())

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
