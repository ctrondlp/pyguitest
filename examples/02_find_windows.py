#!/usr/bin/env python3
"""List open windows and find one by title.

The equivalent of X11::GUITest's FindWindowLike.pl. Titles are matched as
regular expressions, exactly as they were there.

    python3 examples/02_find_windows.py
    python3 examples/02_find_windows.py Firefox
"""

import sys

import pyguitest
from pyguitest import Capability

gui = pyguitest.connect()

if not gui.supports(Capability.WINDOW_LIST):
    sys.exit(
        "This desktop cannot list windows.\n"
        f"{gui.environment.summary()}\n"
        "On GNOME, install the 'atspi' extra; on sway or Hyprland it works "
        "out of the box."
    )

pattern = sys.argv[1] if len(sys.argv) > 1 else "."

for window in gui.find_windows(pattern):
    print(f"{window.title!r}  app_id={window.app_id!r}  pid={window.pid}")

    # Geometry is the capability most often missing: no Wayland protocol
    # carries it, so only compositor IPC and X11 can answer.
    if gui.supports(Capability.WINDOW_GEOMETRY):
        x, y, width, height = gui.geometry(window)
        print(f"    at {x},{y} size {width}x{height}")
