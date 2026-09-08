#!/usr/bin/env python3
"""One-off live validation of InputCaptureBackend (`inputcapture`).

**Read this before running it.** Unlike every other consent dialog in this
package's examples, *approving* this one does something to your own
session, not just to a spawned application: your desktop's InputCapture
portal will exclusively divert your real pointer -- every physical or
logical device it chooses -- away from your desktop and to this script,
for as long as the capture stays active. This script tries to keep that
window as short as it can (it releases the instant it reads a position),
but the whole point of running it is to deliberately trigger that trade
once, on purpose. Never run this from an agent session or unattended;
run it yourself, at a real keyboard, when you are ready to move your own
mouse to a screen edge on request.

`InputCaptureSession` (python-libei 0.5.0+) and this backend have never
been run against a real portal before this script exists to do it -- see
docs/validation.md's "Not run live" section and the class docstrings in
both repos for why. This is the first live check.

What it does, in order:

1. Negotiates an `inputcapture` session -- a real "Allow this app to see
   your input?"-shaped consent dialog will appear; click Allow.
2. Reads back the zones the portal reports and prints them, so you can see
   what `_perimeter_barriers()` computed a boundary from before trusting
   the result.
3. Asks you to move your pointer to any screen edge, then calls
   `wait_for_pointer_activation()` and waits (up to 30s) for that crossing
   to actually trigger capture.
4. Reports the position the compositor handed back, and how long the wait
   took -- the two questions this script exists to answer: does
   `Activated` actually carry `cursor_position` on this stack (the plan's
   original open question), and does release actually hand control back
   promptly afterward (try moving the mouse again once it prints "done").

    python3 _inputcapture_validate.py
"""

import sys
import time

import pyguitest
from pyguitest import BackendUnavailable, PermissionRequired, PyGUITestError

print(
    "This will ask your desktop's InputCapture portal to exclusively "
    "divert your pointer to this script once you move it to a screen "
    "edge. A consent dialog is about to appear -- click Allow to "
    "continue, or interrupt now (Ctrl-C) to back out."
)
input("Press Enter when ready to continue... ")

try:
    gui = pyguitest.connect(backend="inputcapture")
except (BackendUnavailable, PermissionRequired) as exc:
    sys.exit(f"could not open an inputcapture session: {exc}")

print(f"connected: backend={gui.backend.name}")
print("capabilities offered:", sorted(c.name for c in gui.backend.capabilities))

try:
    zone_set, zones = gui.backend._session.zones()
    print(f"zones (zone_set={zone_set}): {zones}")
    barriers = gui.backend._perimeter_barriers(zones)
    print(f"perimeter barriers about to be armed: {barriers}")

    print(
        "\nMove your pointer to any screen edge now -- top, bottom, left "
        "or right -- to trigger capture. Waiting up to 30s..."
    )
    started = time.monotonic()
    try:
        position = gui.wait_for_pointer_activation(timeout=30.0)
    except PyGUITestError as exc:
        # Raised immediately, without waiting out the timeout, if the
        # compositor refused every barrier just requested -- see the
        # method's own docstring. The first two live runs of this script
        # timed out with nothing to say why, because that check did not
        # exist yet; if this fires now, it is the answer.
        sys.exit(f"\nREFUSED, not merely quiet: {exc}")
    elapsed = time.monotonic() - started

    if position is None:
        print(f"\nTIMED OUT after {elapsed:.1f}s -- nobody crossed an edge in time.")
        print(
            "The compositor accepted every barrier (no PyGUITestError above), "
            "so it is watching the edges -- it just never decided a crossing "
            "happened. Try pressing the pointer firmly against one edge and "
            "holding it there for a couple of seconds, rather than a quick "
            "touch; if that still does nothing, this compositor's InputCapture "
            "implementation may not be triggering barrier crossings at all yet."
        )
    else:
        x, y = position
        print(f"\nACTIVATED after {elapsed:.1f}s -- cursor_position=({x}, {y})")
        print(
            "Released and disabled. Try moving the pointer again now -- it "
            "should respond normally; if it does not, control was not "
            "actually handed back and that is a real bug to report."
        )
finally:
    gui.close()

print("\ndone")
