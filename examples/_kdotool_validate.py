#!/usr/bin/env python3
"""One-off live validation of KdotoolBackend, forced rather than the composite.

Exercises methods that have never run against a real KWin before this
session: windows(), geometry() (parsing kdotool's real getwindowgeometry
text), active_window(), window_at(), move_window(), resize_window(),
minimize_window(), and is_window_viewable()'s deliberate refusal.

Uses gedit rather than a dialog tool (zenity, kdialog): a GTK message
dialog is not resizable, which made resize_window() look broken here at
first -- windowsize's own kdotool CLI output showed the same no-op against
that dialog, confirming it is the dialog refusing the request, not the
backend. gedit's regular window has no such restriction and gives a real
resize to check against. Its title is not fully controllable via argv, so
it is matched with a substring regex instead of an exact one.

Only touches the gedit window this script opens itself.

    python3 _kdotool_validate.py
"""

import shutil
import sys
import time

import pyguitest
from pyguitest import Capability
from pyguitest.errors import CapabilityUnsupported

if shutil.which("gedit") is None:
    sys.exit(
        "gedit is not installed -- this script needs an ordinary, resizable "
        "window with a stable title substring. On Fedora: sudo dnf install gedit"
    )

gui = pyguitest.connect(backend="windows")
print(f"forced backend: {gui.backend.name}")

process = gui.start_app(["gedit", "--new-window"])
try:
    window = gui.wait_for_window("gedit", timeout=10)
    if window is None:
        sys.exit("gedit never opened a window")
    print(f"found: {window.title!r}")

    print("windows():", [w.title for w in gui.windows()])

    print("geometry:", gui.geometry(window))

    print("activating...")
    gui.activate_window(window)
    time.sleep(0.3)
    active = gui.active_window()
    print("active_window():", active.title if active else None)
    print(
        "is the activated window the one we opened?",
        active is not None and active.handle == window.handle,
    )

    print("moving to (50, 60)...")
    gui.move_window(window, 50, 60)
    time.sleep(0.3)
    print("geometry after move:", gui.geometry(window))

    before = gui.geometry(window)
    target_w, target_h = before[2] + 137, before[3] + 91
    print(f"resizing to {target_w}x{target_h} (from {before[2]}x{before[3]})...")
    gui.resize_window(window, target_w, target_h)
    time.sleep(0.3)
    after = gui.geometry(window)
    print("geometry after resize:", after)
    print("did it actually change?", (after[2], after[3]) == (target_w, target_h))

    if gui.supports(Capability.WINDOW_AT_POINT):
        x, y, w, h = gui.geometry(window)
        hit = gui.window_at(x + 10, y + 10)
        print("window_at(inside):", hit.title if hit else None)
        far = gui.window_at(5000, 5000)
        print("window_at(far away):", far.title if far else None)

    try:
        gui.is_window_viewable(window)
        print("is_window_viewable: did NOT raise -- unexpected")
    except CapabilityUnsupported as exc:
        print("is_window_viewable: raised CapabilityUnsupported as documented:", exc)

    print("minimizing (no visibility check available, just confirming no exception)...")
    gui.minimize_window(window)
    time.sleep(0.3)
    print("restoring...")
    gui.minimize_window(window, minimized=False)
    time.sleep(0.3)

    print("\nall calls completed without raising")

finally:
    process.terminate()
    print("done")
