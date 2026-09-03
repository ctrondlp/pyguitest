#!/usr/bin/env python3
"""One-off live validation of KWinEventsBackend, forced rather than composited.

Capability.WINDOW_EVENTS on KDE: kdotool (KDE's window backend) has no
event-subscription mechanism of its own to poll less crudely (no
`behave`/watch subcommand at all, unlike xdotool) -- this is the one real
gap that leaves open on KDE, and KWinEventsBackend closes it via an ad
hoc KWin script (see kwinevents.py's own docstring for the full mechanism
and what was confirmed live building it).

This backend has no window-listing of its own, so `wait_for_window`
checks existing windows by shelling out to `kdotool search` directly --
exercised below by checking for a window known to already be open
(this terminal) before exercising `new`/`title`/`close` on a freshly
spawned one.

    python3 _kwin_events_validate.py
"""

import shutil
import subprocess
import sys

import pyguitest
from pyguitest import Capability
from pyguitest.errors import BackendUnavailable

if shutil.which("gedit") is None:
    sys.exit(
        "gedit is not installed -- this script needs an ordinary window to "
        "open and close. On Fedora: sudo dnf install gedit"
    )

print("connecting to kwinevents...")
try:
    gui = pyguitest.connect(backend="kwinevents")
except BackendUnavailable as exc:
    sys.exit(f"kwinevents unavailable: {exc}")
print(f"forced backend: {gui.backend.name}")
print("capabilities:", sorted(c.name for c in gui.backend.capabilities))

if not gui.supports(Capability.WINDOW_EVENTS):
    sys.exit("WINDOW_EVENTS not declared -- unexpected for a forced KWinEventsBackend")

print("\nwait_for_window() against an already-open window (this shell)...")
already_open = gui.wait_for_window("bash", timeout=2)
print(
    "found immediately (no event needed)?"
    if already_open is not None
    else "no match (fine if nothing titled 'bash' is open)",
    already_open,
)

process = gui.start_app(["gedit", "--new-window"])
try:
    print("\nwaiting for gedit's window via a live event...")
    window = gui.wait_for_window("gedit", timeout=10)
    if window is None:
        sys.exit("no 'new' event arrived for gedit within 10s")
    print(f"found: handle={window.handle!r} title={window.title!r}")

    print(
        "\nlistening for a title-change event for 5s "
        "(gedit retitles itself once its buffer has content)..."
    )
    saw_title_change = False
    for event in gui.window_events(timeout=5):
        print(f"  event: {event.change} -> {event.window!r}")
        if event.change == "title" and event.window.handle == window.handle:
            saw_title_change = True
            break
    print("saw a title-change event?", saw_title_change)

    print("\nterminating gedit and waiting for its close event...")
    process.terminate()
    got_close = False
    for event in gui.window_events(timeout=8):
        print(f"  event: {event.change} -> {event.window!r}")
        if event.change == "close" and event.window.handle == window.handle:
            got_close = True
            break
    print("saw the close event?", got_close)

    print("\nall calls completed without raising")

finally:
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    gui.close()
    print("done")
