#!/usr/bin/env python3
"""One-off live validation of SwayBackend, forced rather than composited.

docs/validation.md's "Not run live" section has carried sway (and
Hyprland, niri, the same IPC shape) since it was written -- their tests
replay recorded output and stand-ins, never a real compositor. This is
that missing run: connect, list, move, resize, activate, minimize and
subscribe to window_events() against an actual sway, over the real IPC
socket (ipc.py's SwaySocket), the way scripts/headless-sway-session.sh
sets one up.

    ./scripts/headless-sway-session.sh python3 examples/_sway_validate.py
"""

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time

import pyguitest
from pyguitest import Capability
from pyguitest.errors import BackendUnavailable


def _target_pid(window, fallback_pid):
    """window.pid, or `fallback_pid` if the compositor never reported one.

    window_events() builds Window objects straight from a raw IPC event
    payload's "container" dict (backends/windows.py:270) -- unlike
    windows()'s _window() helper, it does not filter down to nodes that
    carry a pid -- so window.pid can genuinely be None. Flagged in PR
    review: os.kill(None, ...) raises TypeError, which
    contextlib.suppress(ProcessLookupError) does not catch, so this must
    be resolved before the kill rather than papered over after it.
    """
    return window.pid if window.pid is not None else fallback_pid


def _settle(get, want, timeout=2.0):
    """Poll `get()` until it equals `want`, or return its last value.

    Not a fixed sleep: confirmed live that gnome-text-editor keeps
    resizing itself for a few hundred ms after mapping (loading its own
    saved window size), unrelated to anything sway or pyguitest do -- a
    single sleep(0.3) caught it mid-settle and looked like a 27px bug in
    move_window() that a longer wait shows was never really there.
    """
    deadline = time.monotonic() + timeout
    value = get()
    while value != want and time.monotonic() < deadline:
        time.sleep(0.1)
        value = get()
    return value


SPAWN_CANDIDATES = ["gnome-text-editor", "gedit", "gnome-calculator"]
spawn_app = next((a for a in SPAWN_CANDIDATES if shutil.which(a)), None)
if spawn_app is None:
    sys.exit(
        f"none of {SPAWN_CANDIDATES} is installed -- this script needs an "
        "ordinary native-Wayland window to open and close. On Fedora: "
        "sudo dnf install gnome-text-editor"
    )

print("connecting to sway...")
try:
    # "windows" is the registered factory name (backends/__init__.py) --
    # it dispatches on the detected compositor, landing on SwayBackend
    # here because SWAYSOCK is set. There is no separate "sway" name to
    # force directly.
    gui = pyguitest.connect(backend="windows")
except BackendUnavailable as exc:
    sys.exit(f"sway unavailable: {exc}")
if gui.backend.name != "sway":
    sys.exit(f"connected, but to {gui.backend.name!r} not 'sway' -- is SWAYSOCK set?")
print(f"forced backend: {gui.backend.name}")
caps = sorted(c.name for c in gui.backend.capabilities)
print("capabilities:", caps)

for needed in (
    Capability.WINDOW_EVENTS,
    Capability.WINDOW_PLACEMENT,
    Capability.WINDOW_RESIZE,
    Capability.WINDOW_ACTIVATE,
    Capability.WINDOW_MINIMIZE,
):
    if not gui.supports(needed):
        sys.exit(f"{needed.name} not declared -- unexpected for a forced SwayBackend")

print(f"\nwindows() before spawning: {len(gui.windows())} open")

print(f"\nspawning {spawn_app!r} to exercise everything below deterministically...")
# Matched on the first "new" event, not on this pid: gnome-text-editor and
# friends are D-Bus-activatable GApplications, so the CLI launcher process
# can just send an Activate call and exit while the bus daemon spawns the
# real, differently-pid'd GUI process -- confirmed live, this pid never
# showed up on any event. windows() is confirmed empty above, so the first
# "new" event is unambiguously this spawn regardless of whose pid it is.
process = subprocess.Popen([spawn_app])
try:
    window = None
    # 25s, not 10: confirmed live that a cold private D-Bus session (see
    # headless-sway-session.sh) spends several seconds on portal/gvfs/a11y
    # bus activation churn before a GTK app's window ever appears.
    for event in gui.window_events(timeout=25):
        print(f"  event: {event.change} -> {event.window!r}")
        if event.change == "new":
            window = event.window
            break
    if window is None:
        sys.exit("no 'new' event arrived within 25s")
    print(
        f"found: handle={window.handle!r} title={window.title!r} "
        f"app_id={window.app_id!r}"
    )

    print("\ngeometry() before any move/resize...")
    x0, y0, w0, h0 = gui.geometry(window)
    print(f"  ({x0}, {y0}, {w0}, {h0})")

    print("\nmove_window(200, 150)...")
    gui.move_window(window, 200, 150)
    x1, y1 = _settle(lambda: gui.geometry(window)[:2], (200, 150))
    print(f"  now at ({x1}, {y1})")
    moved = (x1, y1) == (200, 150)
    print("moved to the requested position?", moved)

    print("\nresize_window(640, 480)...")
    gui.resize_window(window, 640, 480)
    w2, h2 = _settle(lambda: gui.geometry(window)[2:], (640, 480))
    print(f"  now sized ({w2}, {h2})")
    resized = (w2, h2) == (640, 480)
    print("resized to the requested dimensions?", resized)

    print("\nactivate_window()...")
    gui.activate_window(window)
    active = gui.active_window()
    print(f"  active_window() -> {active!r}")
    activated = active is not None and active.handle == window.handle
    print("became the active window?", activated)

    print("\nminimize_window(True) (sway: moved to the scratchpad)...")
    gui.minimize_window(window, True)
    time.sleep(0.3)
    visible_after_minimize = gui.is_window_viewable(window)
    print("still viewable after minimize?", visible_after_minimize, "(expect False)")

    print("\nminimize_window(False) (restore from the scratchpad)...")
    gui.minimize_window(window, False)
    time.sleep(0.3)
    visible_after_restore = gui.is_window_viewable(window)
    print("viewable after restore?", visible_after_restore, "(expect True)")

    print("\nwindow_at() over the restored window's own top-left corner...")
    rx, ry, _rw, _rh = gui.geometry(window)
    hit = gui.window_at(rx + 5, ry + 5)
    print(f"  window_at({rx + 5}, {ry + 5}) -> {hit!r}")
    hit_ok = hit is not None and hit.handle == window.handle

    kill_pid = _target_pid(window, process.pid)
    kill_source = "sway-reported" if window.pid is not None else "launcher fallback"
    print(
        f"\nkilling pid={kill_pid} ({kill_source}) and waiting for its close event..."
    )
    # window.pid, from sway's own tree, not process.pid, when sway reports
    # one: see the note above -- a D-Bus-activated launcher can already be
    # gone by now.
    with contextlib.suppress(ProcessLookupError):
        os.kill(kill_pid, signal.SIGTERM)
    got_close = False
    for event in gui.window_events(timeout=8):
        print(f"  event: {event.change} -> {event.window!r}")
        if event.change == "close" and event.window.handle == window.handle:
            got_close = True
            break
    print("saw the close event?", got_close)

    print()
    print("results:")
    print("  moved to requested position:      ", moved)
    print("  resized to requested dimensions:  ", resized)
    print("  became the active window:         ", activated)
    print("  hidden after minimize:            ", not visible_after_minimize)
    print("  visible after restore:            ", visible_after_restore)
    print("  window_at() found the same window:", hit_ok)
    print("  close event arrived:              ", got_close)
    if not all(
        (
            moved,
            resized,
            activated,
            not visible_after_minimize,
            visible_after_restore,
            hit_ok,
            got_close,
        )
    ):
        sys.exit(1)
    print("\nall checks passed")

finally:
    if window is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(_target_pid(window, process.pid), signal.SIGKILL)
    # The launcher process itself: usually already exited (see above), but
    # waited on regardless so it is never left as a zombie.
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    gui.close()
    print("done")
