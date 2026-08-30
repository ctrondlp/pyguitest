#!/usr/bin/env python3
"""One-off live validation of X11Backend.is_window_cursor().

Forced rather than composited. WINDOW_CURSOR_QUERY is the fifth tier-6
capability and, per docs/validation.md, the one never run against a real
desktop before this script -- only ever exercised against a fake Xlib in
tests/test_x11.py. It needs python-xlib (`pip install python-xlib`, or the
`x11` extra), which is not installed by default.

What is actually being queried is worth being precise about, since
"is_window_cursor(window, shape)" reads as scoped to `window` and is not,
quite: the underlying request is XTEST's CompareCursor, and pyguitest
always calls it with an explicit cursor object (built from `shape`) rather
than the CurrentCursor sentinel -- reading Xlib.ext.xtest's source (there
is no higher-level documentation to check against) shows this compares
that shape against whatever cursor is *currently displayed on screen*,
system-wide, wherever the pointer physically is. `window` mostly just
needs to be a valid window on the right screen, so this uses whatever
window already exists rather than spawning one -- no app launch, no
window geometry involved, both of which turned out to have their own
open questions on this session's KDE/KWin (see docs/validation.md) that
are deliberately out of scope here.

Standard X cursor-font shape indices, from <X11/cursorfont.h> -- the same
constants X11::GUITest exported and tests/test_x11.py already uses. Five
visually distinct shapes are queried at one plain desktop-background
point, on the theory that a *themed* cursor (Xcursor/Breeze, what any
modern desktop actually shows) may never bitwise-match any of these
classic bitmap font cursors, regardless of which one is queried or what
is actually on screen -- which would be a real, practical limitation of
this capability distinct from whether the protocol call itself works.

    XC_X_CURSOR   0    the default root cursor on very old X setups
    XC_CROSSHAIR  34   crosshair
    XC_HAND2      60   hand
    XC_LEFT_PTR   68   ordinary arrow -- expected here, if anything is
    XC_XTERM      152  text I-beam

The "empty desktop" point is a guess (5, 5) and may not be empty on every
panel layout; this script prints what it finds either way rather than
asserting blindly.

    python3 _cursor_validate.py
"""

import sys
import time

import pyguitest
from pyguitest import Capability

SHAPES = {
    "XC_X_CURSOR": 0,
    "XC_CROSSHAIR": 34,
    "XC_HAND2": 60,
    "XC_LEFT_PTR": 68,
    "XC_XTERM": 152,
}

DESKTOP_POINT = (5, 5)

gui = pyguitest.connect(backend="x11")
print(f"forced backend: {gui.backend.name}")

if not gui.supports(Capability.WINDOW_CURSOR_QUERY):
    sys.exit(
        "WINDOW_CURSOR_QUERY is unsupported -- unexpected for a forced "
        "X11Backend; see x11.py's capabilities property."
    )
if not gui.supports(Capability.WINDOW_LIST):
    sys.exit("WINDOW_LIST is unsupported -- no window to pass as the argument.")
if not gui.supports(Capability.POINTER_MOVE):
    sys.exit("POINTER_MOVE is unsupported -- cannot position the pointer.")

windows = gui.windows()
if not windows:
    sys.exit(
        "no windows at all were found -- open literally anything (even the "
        "desktop's own panel counts) and try again."
    )
window = windows[0]
print(f"using window: {window.title!r} (only needs to be valid, not on-screen)")

gui.move_mouse(*DESKTOP_POINT)
time.sleep(0.3)

print(f"\nat desktop point {DESKTOP_POINT}:")
results = {}
for name, shape in SHAPES.items():
    results[name] = gui.is_window_cursor(window, shape)
    print(f"  is_window_cursor({name}={shape})? {results[name]}")

if any(results.values()):
    print("\nat least one shape matched -- the classic cursor font is in play here.")
else:
    print(
        "\nnone matched, despite an ordinary arrow plainly being what is shown "
        "at that point -- consistent with this desktop using a themed "
        "(Xcursor) cursor that never bitwise-matches any classic X core-font "
        "cursor, regardless of shape or what is actually displayed."
    )

print("\nall calls completed without raising")
