#!/usr/bin/env python3
"""One-off live validation of X11Backend, forced rather than the composite.

Exercises methods that have never run against a real X server before this
session: move_window, resize_window, minimize_window, window_at,
lower_window, set_window_title, geometry, and is_window_viewable.

Uses xterm rather than gedit deliberately: on GNOME, GTK apps run as native
Wayland clients with no X11 window at all, which X11Backend (forced alone,
with no AT-SPI to fall back on) genuinely cannot see -- the same
"XWayland reaches X11 clients only" limitation documented elsewhere in this
project, not a bug. xterm is a real Xlib client, guaranteed visible here.

Runs `sleep` inside xterm rather than an interactive shell: Fedora's default
bashrc sets the window title itself via an OSC escape sequence on every
prompt, which clobbers whatever -T set the moment the shell draws its first
prompt. No shell, no clobbering.

Only touches the xterm window this script opens itself.

    python3 _x11_validate.py
"""

import shutil
import sys
import time

import pyguitest
from pyguitest import Capability

if shutil.which("xterm") is None:
    sys.exit(
        "xterm is not installed -- this script needs a guaranteed X11 client "
        "to test against. On Fedora: sudo dnf install xterm"
    )

gui = pyguitest.connect(backend="x11")
print(f"forced backend: {gui.backend.name}")

process = gui.start_app(["xterm", "-T", "pyguitest-x11-test", "-e", "sleep", "300"])
try:
    window = gui.wait_for_window("pyguitest-x11-test", timeout=10)
    if window is None:
        sys.exit("xterm never opened a window")
    print(f"found: {window.title!r}")

    backend = gui.backend
    handle = backend._handle(window)
    geom = handle.get_geometry()
    print("raw get_geometry() (relative to immediate parent):", (geom.x, geom.y))
    tree = handle.query_tree()
    print("query_tree().parent:", tree.parent, " geom.root:", geom.root)
    print("are parent and root the same object?", tree.parent == geom.root)
    root_geom = geom.root.get_geometry()
    print(
        "the root window's own get_geometry() (should be x=0, y=0, full screen size):",
        (root_geom.x, root_geom.y, root_geom.width, root_geom.height),
    )
    origin = handle.translate_coords(geom.root, 0, 0)
    print("translate_coords(root, 0, 0):", (origin.x, origin.y))

    print("geometry:", gui.geometry(window))
    print("is_window_viewable:", gui.is_window_viewable(window))

    print("moving to (50, 60)...")
    gui.move_window(window, 50, 60)
    time.sleep(0.3)
    print("geometry after move:", gui.geometry(window))
    input(
        "\n>>> Look at the xterm window now. Is it near the top-left corner "
        "of your screen (small x/y, like (50, 60) would suggest), or "
        "somewhere else? Press Enter here once you've checked to continue.\n"
    )

    print("resizing to 500x400...")
    gui.resize_window(window, 500, 400)
    time.sleep(0.3)
    print("geometry after resize:", gui.geometry(window))

    if gui.supports(Capability.WINDOW_AT_POINT):
        x, y, w, h = gui.geometry(window)
        hit = gui.window_at(x + 10, y + 10)
        print("window_at(inside):", hit.title if hit else None)
        far = gui.window_at(5000, 5000)
        print("window_at(far away):", far.title if far else None)

    print("minimizing...")
    gui.minimize_window(window)
    time.sleep(0.3)
    print("is_window_viewable after minimize:", gui.is_window_viewable(window))
    print("restoring...")
    gui.minimize_window(window, minimized=False)
    time.sleep(0.3)
    print("is_window_viewable after restore:", gui.is_window_viewable(window))

    if gui.supports(Capability.WINDOW_LOWER):
        print("lowering (no visible check, just confirming no exception)...")
        gui.lower_window(window)

    if gui.supports(Capability.WINDOW_TITLE_SET):
        print("setting title to 'pyguitest was here'...")
        gui.set_window_title(window, "pyguitest was here")
        time.sleep(0.3)
        refreshed = gui.find_window("pyguitest was here")
        print("title now:", refreshed.title)

    print("\nall calls completed without raising")

finally:
    process.terminate()
    print("done")
