#!/usr/bin/env python3
"""One-off live validation of LibeiBackend (`eiinput`) on a real desktop.

Written for KDE/KWin first -- `eiinput` had only ever been live-validated
on GNOME Shell (see `docs/validation.md`), and whether KDE's
`xdg-desktop-portal-kde` implements the same `RemoteDesktop`+libei
negotiation the same way was the open question. It runs on either desktop
now: the window half is chosen per compositor (see `_window_backend`),
because naming the wrong one fails before the consent dialog is even
reached.

`connect(backend="eiinput")` negotiates a real `RemoteDesktop` portal
session and will raise a genuine "Allow this app to control your input?"
consent dialog the moment it is constructed -- click Allow when it
appears; this script cannot do that for you, and there is no way around
it (see docs/adr-002-transports.md on why `eiinput` is opt-in and never
auto-selected).

`LibeiBackend` is input-only -- it has no window-management capabilities of
its own, unlike `KdotoolBackend` or `gnomeshell` -- so window discovery and
activation come from a second member named alongside it in one session:
`connect(backend=["eiinput", <window backend>])`. The order is the
precedence, so `eiinput` serves every capability it has and the other
fills in the rest.

*Which* window backend is not the same everywhere, and naming the wrong
one fails immediately with the registry's generic "cannot drive this
session" -- before the consent dialog, so it reads as though `eiinput`
were unavailable when it is not. `windows` covers sway/Hyprland/niri and
KDE (via kdotool); on GNOME it resolves to nothing at all, because Mutter
implements no foreign-toplevel protocol and `for_compositor()` has no
entry for it -- there, the window half is `gnomeshell`.
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
import sys
import time

import pyguitest
from pyguitest import (
    BackendUnavailable,
    Capability,
    Compositor,
    PermissionRequired,
)

if shutil.which("gedit") is None:
    sys.exit(
        "gedit is not installed -- this script needs an ordinary window to "
        "point at. On Fedora: sudo dnf install gedit"
    )


def _window_backend(environment):
    """The registry name serving windows on this compositor.

    Mutter is the exception the hard way: `windows` is the right answer on
    sway, Hyprland, niri and KDE, and on GNOME its factory returns None --
    so naming it there fails with the registry's generic message and looks
    like an `eiinput` problem, which is what sent this script's first GNOME
    run down the wrong path.
    """
    if environment.compositor is Compositor.MUTTER:
        return "gnomeshell"
    return "windows"


environment = pyguitest.detect()
companion = _window_backend(environment)
print(
    f"connecting to eiinput + {companion} on {environment.compositor.value} "
    "-- a consent dialog may appear; click Allow"
)
try:
    # One session naming both backends, in precedence order: `eiinput`
    # serves every injected event, the other serves the window discovery
    # and activation it has none of. This used to be two separate sessions,
    # because naming a backend gave you only that one -- see the note above
    # on why they were paired by hand.
    gui = pyguitest.connect(backend=["eiinput", companion])
except (BackendUnavailable, PermissionRequired) as exc:
    sys.exit(
        f"could not open an eiinput + {companion} session: {exc}\n"
        f"(if {companion!r} is the part that failed, eiinput itself may be "
        "fine -- on GNOME that member needs the pyguitest-window-control "
        "extension installed and enabled)"
    )
input_gui = windows_gui = gui
print(f"forced backend: {gui.backend.name}")
# eiinput's own capabilities, not the session's union: what this script is
# here to report is what *libei* offered, and the union would fold in the
# window capabilities that came from the other member.
eiinput_backend = gui.backend.member("eiinput")
print("capabilities offered:", sorted(c.name for c in eiinput_backend.capabilities))

app = windows_gui.start_app(["gedit", "--new-window"])
try:
    # Matched case-insensitively and against the document name too: a
    # window title is a label, not an identity, and gedit's carries the
    # app name on some desktops and only "Untitled Document" on others.
    # On a miss, say what *was* on screen -- "never opened a window" alone
    # is a dead end when the window is right there under another name.
    window = windows_gui.wait_for_window("(?i)gedit|Untitled Document", timeout=10)
    if window is None:
        seen = ", ".join(repr(w.title) for w in windows_gui.windows()) or "none"
        sys.exit(
            "no gedit window matched within 10s.\n"
            f"windows visible to {companion!r}: {seen}"
        )
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

    if input_gui.supports(Capability.INPUT_SYNC):
        # The point of sync() is that it replaces the time.sleep() calls
        # above, so this measures it rather than merely calling it: a
        # round trip to a compositor on the same machine should land in
        # single-digit milliseconds, orders of magnitude under the 0.3s
        # each guessed sleep costs. A number far from that -- or a False
        # -- is the finding worth reporting.
        print("syncing...")
        started = time.monotonic()
        confirmed = input_gui.sync()
        elapsed = time.monotonic() - started
        print(f"  sync() -> {confirmed} in {elapsed * 1000:.1f}ms")
        if not confirmed:
            print("  NOT confirmed: no PONG arrived within the timeout")
    else:
        print("INPUT_SYNC not offered -- skipping sync (libei 1.4+ needed)")

    print("\nall calls completed without raising")

finally:
    app.stop()
    print("done")
