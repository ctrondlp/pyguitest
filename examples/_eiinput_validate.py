#!/usr/bin/env python3
"""One-off live validation of LibeiBackend (`eiinput`) on real KDE/KWin.

`eiinput` has only ever been live-validated on GNOME Shell (see
`docs/validation.md`); whether KDE's `xdg-desktop-portal-kde` implements
the same `RemoteDesktop`+libei negotiation the same way is, as of this
script, unconfirmed. That is what this checks.

`connect(backend="eiinput")` negotiates a real `RemoteDesktop` portal
session and will raise a genuine "Allow this app to control your input?"
consent dialog the moment it is constructed -- click Allow when it
appears; this script cannot do that for you, and there is no way around
it (see docs/adr-002-transports.md on why `eiinput` is opt-in and never
auto-selected).

`LibeiBackend` is input-only -- it has no window-management capabilities of
its own, unlike `KdotoolBackend` or `gnomeshell` -- so window discovery and
activation come from `windows`, named alongside it in one session:
`connect(backend=["eiinput", "windows"])`. The order is the precedence, so
`eiinput` serves every capability it has and `windows` fills in the rest.
That still keeps `eiinput` isolated the way `_kdotool_validate.py` isolates
kdotool -- nothing is composed automatically, both members are named -- and
it is what this script wanted all along: it previously opened two separate
sessions and remembered which one to ask for what, because naming a backend
gave you only that one.

Only touches the gedit window this script opens itself. If the pointer
does not visibly move once connected, this is very likely a VirtualBox
host running under Mouse Integration slaving the guest cursor to the
host's absolute position -- see eiinput.py's own docstring -- not evidence
that injection failed silently.

    python3 _eiinput_validate.py
"""

import shutil
import subprocess
import sys
import time

import pyguitest
from pyguitest import BackendUnavailable, Capability, PermissionRequired

if shutil.which("gedit") is None:
    sys.exit(
        "gedit is not installed -- this script needs an ordinary window to "
        "point at. On Fedora: sudo dnf install gedit"
    )

print("connecting to eiinput -- a consent dialog may appear; click Allow")
try:
    # One session naming both backends, in precedence order: `eiinput`
    # serves every injected event, `windows` serves the window discovery and
    # activation it has none of. This used to be two separate sessions,
    # because naming a backend gave you only that one -- see the note below
    # on why they were paired by hand.
    gui = pyguitest.connect(backend=["eiinput", "windows"])
except (BackendUnavailable, PermissionRequired) as exc:
    sys.exit(f"eiinput unavailable: {exc}")
input_gui = windows_gui = gui
print(f"forced backend: {gui.backend.name}")
# eiinput's own capabilities, not the session's union: what this script is
# here to report is what *libei* offered, and the union would fold in the
# window capabilities that came from the other member.
eiinput_backend = gui.backend.member("eiinput")
print("capabilities offered:", sorted(c.name for c in eiinput_backend.capabilities))

process = windows_gui.start_app(["gedit", "--new-window"])
try:
    window = windows_gui.wait_for_window("gedit", timeout=10)
    if window is None:
        sys.exit("gedit never opened a window")
    print(f"found: {window.title!r}")

    windows_gui.activate_window(window)
    time.sleep(0.3)

    x, y, w, h = windows_gui.geometry(window)
    center_x, center_y = x + w // 2, y + h // 2

    if input_gui.supports(Capability.POINTER_MOVE):
        print(f"moving pointer to gedit's centre ({center_x}, {center_y})...")
        input_gui.move_mouse(center_x, center_y)
        time.sleep(0.3)
    else:
        print("POINTER_MOVE not offered -- skipping move_mouse")

    if input_gui.supports(Capability.POINTER_BUTTON):
        print("clicking to focus the document...")
        input_gui.click()
        time.sleep(0.3)
    else:
        print("POINTER_BUTTON not offered -- skipping click")

    if input_gui.supports(Capability.POINTER_SCROLL):
        print("scrolling...")
        input_gui.scroll(dy=1)
        time.sleep(0.3)
    else:
        print("POINTER_SCROLL not offered -- skipping scroll")

    if input_gui.supports(Capability.TEXT_ENTRY):
        print("typing...")
        input_gui.type_text("Hello from eiinput on KDE.\n")
        time.sleep(0.3)
    else:
        print("TEXT_ENTRY not offered -- skipping type_text")

    print("\nall calls completed without raising")

finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    print("done")
