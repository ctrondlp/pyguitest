#!/usr/bin/env python3
"""Take a screenshot -- the whole desktop, one window, or one rectangle.

Note this is a *new* feature, not a port: X11::GUITest had no screenshot
function at all.

    python3 examples/05_screenshot.py                    # whole desktop
    python3 examples/05_screenshot.py /tmp/shot.png      # to a named file
    python3 examples/05_screenshot.py /tmp/shot.png Editor   # one window

Where the pixels come from depends on the session, and the difference is
visible in the image -- this prints which route it took.
"""

import sys

import pyguitest
from pyguitest import Capability

gui = pyguitest.connect()

if not gui.supports(Capability.SCREEN_CAPTURE):
    sys.exit(
        "Nothing on this session captures without being asked for by name.\n"
        "\n"
        "On GNOME under Wayland that is the normal state, and the answer is\n"
        "the Screenshot portal:\n"
        "\n"
        '    gui = pyguitest.connect(backend="portalcapture")\n'
        "\n"
        "It needs no tool installed, prompts for consent once, and the\n"
        "desktop remembers the grant. It is opt-in purely because of that\n"
        "first prompt, so a plain connect() never reaches it.\n"
        "\n"
        "Elsewhere, install the tool for your compositor: grim (wlroots),\n"
        "spectacle (KDE), or python-xlib for a real X11 session. Note that\n"
        "gnome-screenshot and ImageMagick's import capture by reading the X\n"
        "root window, which is unreadable under Wayland including XWayland,\n"
        "so pyguitest will not select them there."
    )

path = sys.argv[1] if len(sys.argv) > 1 else None
title = sys.argv[2] if len(sys.argv) > 2 else None

window = None
if title is not None:
    if not gui.supports(Capability.WINDOW_LIST):
        sys.exit(f"This desktop cannot list windows, so {title!r} cannot be found.")
    window = gui.find_window(title)
    if gui.supports(Capability.WINDOW_CAPTURE):
        print(f"capturing {window.title!r} directly: only its own pixels")
    elif gui.supports(Capability.WINDOW_GEOMETRY):
        print(
            f"capturing {window.title!r} by cropping a full-screen shot to "
            "its rectangle: anything covering it will be in the image too"
        )
    else:
        sys.exit(
            "This desktop can capture the screen but cannot locate a window "
            "on it -- no WINDOW_CAPTURE and no WINDOW_GEOMETRY."
        )

written = gui.screenshot(path=path, window=window)
print(f"written to {written}")

# The other half of the story: nothing above captures on its own. To have a
# screenshot taken at the moment something goes wrong -- while the app is
# still on screen, which an except: block is too late for -- wrap the work:
#
#     with gui.capture_on_failure("artifacts"):
#         gui.button("Save").click()
#         assert gui.element(name="Saved")
#
# The image path is attached to the exception as .screenshot and the
# original exception is re-raised untouched.
