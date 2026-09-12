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
mouse to a screen edge on request. On a VirtualBox guest, switch Mouse
Integration off first (Host+I): with it on, the guest's pointer *is* the
host's mouse, so a barrier on a screen edge can never fire and this script
can only ever time out.

`InputCaptureSession` (python-libei 0.5.0+) and this backend were first run
against a real portal by this script on 2026-09-12, and it reached
activation -- see docs/validation.md's `inputcapture` section for that run
and for the attempts before it that could not have worked. What stays
scarce is the chance to run it: a run needs a user who will approve the
dialog and then drive their own pointer. Re-running it is still worth doing
on a different compositor, or after a change to the barrier arithmetic.

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
   took. Both questions this script was written for are answered on the
   stack it first ran on -- `Activated` does carry `cursor_position` there,
   and release does hand control back promptly (move the mouse again once
   it prints "done") -- so a run now is a re-check rather than a first look
   at either.

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
        print(f"\nTIMED OUT after {elapsed:.1f}s -- no activation arrived.")
        print(
            "Only a *refused* barrier set raises above, and a partial "
            "refusal is silent, so read this as 'no reachable trigger' rather "
            "than 'the compositor is quiet'. Check Mouse Integration is off "
            "(Host+I) on a VirtualBox guest -- with it on, the guest's pointer "
            "is the host's mouse and a screen edge can never fire. Otherwise "
            "press the pointer firmly against one edge and hold it there for a "
            "couple of seconds rather than brushing past: GNOME Shell 51.rc "
            "does activate on a real crossing, so a repeat timeout is worth "
            "reporting rather than reading as 'not implemented yet'."
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
