#!/usr/bin/env bash
# Validate pyguitest-window-control against the LIVE GNOME Shell.
#
# Must run from a real terminal inside the GNOME session. A Flatpak-
# sandboxed shell (DBUS_SESSION_BUS_ADDRESS=unix:path=/run/flatpak/bus)
# has no route to org.gnome.Shell and every D-Bus step below will fail.
#
# Usage:  ./scripts/validate-gnome-extension.sh [--install]
#           --install   copy the source tree over the installed copy first
#                       (needs a logout/login afterwards to take effect)
set -uo pipefail

UUID=pyguitest-window-control@pyguitest.local
SRC="$(cd "$(dirname "$0")/.." && pwd)/gnome-shell-extension/$UUID"
DST="$HOME/.local/share/gnome-shell/extensions/$UUID"
OBJ=/org/gnome/Shell/Extensions/Pyguitest
IFACE=org.gnome.Shell.Extensions.Pyguitest

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33m..\033[0m   %s\n' "$1"; }

case "${DBUS_SESSION_BUS_ADDRESS:-}" in
  */run/flatpak/bus*)
    echo "REFUSING: this is a Flatpak-sandboxed bus with no route to"
    echo "org.gnome.Shell. Run from a real GNOME terminal."; exit 2;;
esac

echo "== 1. install =="
if [ "${1:-}" = "--install" ]; then
  mkdir -p "$DST" && cp "$SRC"/*.js "$SRC"/metadata.json "$DST"/ \
    && ok "copied into $DST" || bad "copy failed"
  note "log out and back in, then re-run WITHOUT --install"
  exit 0
fi
[ -d "$DST" ] && ok "installed at $DST" || { bad "not installed (run with --install)"; exit 1; }

echo "== 2. shell sees a compatible version =="
gnome-shell --version
state=$(gnome-extensions info "$UUID" 2>/dev/null | sed -n 's/ *State: //p')
case "$state" in
  "OUT OF DATE") bad "State=OUT OF DATE -- shell cached old metadata; log out/in";;
  "") bad "shell does not know this extension";;
  *) ok "State=$state";;
esac

echo "== 3. enable =="
gnome-extensions enable "$UUID" 2>&1 && ok "enable returned 0" || bad "enable failed"
sleep 1
state=$(gnome-extensions info "$UUID" 2>/dev/null | sed -n 's/ *State: //p')
[ "$state" = "ACTIVE" ] && ok "State=ACTIVE" || bad "State=$state (see: journalctl -f -o cat /usr/bin/gnome-shell)"

echo "== 4. the compatibility probe's verdict =="
# findMissingApis() logs and refuses to export when the shell is incompatible.
if journalctl --user -b -o cat --since "-2 min" 2>/dev/null | grep -q "incompatible GNOME Shell"; then
  bad "probe rejected this shell -- see journal for the missing APIs"
else
  ok "probe raised no incompatibility"
fi

echo "== 5. D-Bus surface =="
if gdbus introspect --session --dest org.gnome.Shell --object-path "$OBJ" >/dev/null 2>&1; then
  ok "object exported at $OBJ"
else
  bad "no object at $OBJ (probe refused to export, or enable failed)"
  echo; echo "results: $pass passed, $fail failed"; exit 1
fi

for m in ListWindows; do
  gdbus call --session --dest org.gnome.Shell --object-path "$OBJ" \
    --method "$IFACE.$m" >/dev/null 2>&1 \
    && ok "$m responded" || bad "$m failed"
done

if gdbus introspect --session --dest org.gnome.Shell --object-path "$OBJ" 2>/dev/null \
    | grep -q "WindowEvent"; then
  ok "WindowEvent signal is in the introspected interface"
else
  bad "no WindowEvent signal -- shell is running an extension older than 0.3.0-events"
fi

echo "== 6. GnomeShellBackend against the live extension =="
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src" python3 - <<'PY'
import os
import shutil
import signal
import struct
import subprocess
import sys
import time

from pyguitest.backends import gnomeshell
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, WindowNotFound

G = "\033[32mPASS\033[0m"; R = "\033[31mFAIL\033[0m"; Y = "\033[33m..\033[0m  "
rc = 0

try:
    b = gnomeshell.GnomeShellBackend()
except Exception as e:
    print(f"  {R} construct backend: {type(e).__name__}: {e}")
    sys.exit(1)
print(f"  {G} constructed against the live extension")
# capabilities is a property, not a method.
print(f"  {Y} capabilities: {b.capabilities}")

wins = b.windows()
print(f"  {G} windows() -> {len(wins)} window(s)")
if not wins:
    print(f"  {Y} no windows open; open one and re-run to exercise geometry")
    sys.exit(0)

# The D-Bus tuple has no maximized/tiled flag, so a window that will not
# move (typically maximized) cannot be filtered out ahead of time -- only
# discovered by trying. Probe each window with a trivial move and use the
# first one that actually takes it for the rest of the geometry battery,
# rather than picking wins[0] blindly and letting every later check on an
# immovable window pass vacuously (nothing moved -> nothing to undo ->
# "restored" and "composes over N rounds" are trivially true either way).
w = None
before = None
for candidate in wins:
    g = b.geometry(candidate)
    b.move_window(candidate, g[0] + 1, g[1])
    moved = b.geometry(candidate)
    b.move_window(candidate, g[0], g[1])  # undo the probe regardless
    if moved[:2] == (g[0] + 1, g[1]):
        w, before = candidate, g
        break
if w is None:
    print(f"  {Y} no open window accepts repositioning (all maximized/tiled?)"
          f" -- skipping the geometry battery")
    sys.exit(0)
print(f"  {Y} using window: {w}")
print(f"  {Y} geometry before: {before}")

# The one thing the fake-proxy unit tests cannot catch: against real
# Mutter, move must not alter size and resize must not alter position.
b.move_window(w, before[0] + 17, before[1] + 13)
after = b.geometry(w)
if after[2:] == before[2:]:
    print(f"  {G} move_window left size untouched: {before[2:]}")
else:
    print(f"  {R} move_window changed size: {before[2:]} -> {after[2:]}"); rc = 1
if after[:2] == (before[0] + 17, before[1] + 13):
    print(f"  {G} move_window landed where asked: {after[:2]}")
else:
    print(f"  {R} move_window asked for "
          f"{(before[0] + 17, before[1] + 13)}, got back {after[:2]}"); rc = 1

b.resize_window(w, before[2] + 23, before[3] + 19)
after2 = b.geometry(w)
if after2[:2] == after[:2]:
    print(f"  {G} resize_window left position untouched: {after[:2]}")
else:
    print(f"  {R} resize_window moved the window: {after[:2]} -> {after2[:2]}"); rc = 1

# Back-to-back partial updates, repeated: move_resize_frame() used to read
# a pre-move frame rect on an immediate following resize and silently undo
# the move (see the extension's MoveResizeWindow comment for the fix).
#
# Waits for each step to actually land before sending the next, rather
# than firing all four with no gap. That is not a weaker test -- it is the
# only fair one: applying window geometry on Wayland is a round trip
# through the window's own client (xdg_toplevel configure/ack_configure),
# which nothing on the compositor side can shorten. A zero-delay burst
# measured 3-4 of 4 rounds lost to that round trip on this system even
# with the fix in place; it was testing GNOME's client round-trip latency,
# not this extension. GnomeShellBackend.move_window's docstring carries
# the same caveat for real callers.
def wait_for(target, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if b.geometry(w) == target:
            return True
        time.sleep(0.02)
    return False

rounds, bad_rounds = 6, 0
for i in range(rounds):
    b.move_window(w, before[0] + 17, before[1] + 13)
    wait_for((before[0] + 17, before[1] + 13, before[2], before[3]))
    b.resize_window(w, before[2] + 23, before[3] + 19)
    wait_for((before[0] + 17, before[1] + 13, before[2] + 23, before[3] + 19))
    b.move_window(w, before[0], before[1])
    wait_for((before[0], before[1], before[2] + 23, before[3] + 19))
    b.resize_window(w, before[2], before[3])
    landed = wait_for(before)
    if not landed:
        bad_rounds += 1
        print(f"  {R} round {i + 1}: never converged on {before}, "
              f"stuck at {b.geometry(w)}")
if bad_rounds:
    print(f"  {R} {bad_rounds}/{rounds} rounds failed to converge "
          f"within 2s each"); rc = 1
else:
    print(f"  {G} move/resize compose correctly over {rounds} rounds")

restored = b.geometry(w)
if restored != before:
    b.move_window(w, before[0], before[1])
    b.resize_window(w, before[2], before[3])
    restored = b.geometry(w)
print(f"  {G if restored == before else Y} restored geometry: {restored}"
      + ("" if restored == before else f" (wanted {before})"))

# WINDOW_CAPTURE: the id-0 probe at construction (see capabilities above)
# only asks the extension whether it *thinks* it can capture -- it never
# asks for a real image. On Mutter >=51 that probe now passes through
# _captureModern (extension.js): paint_to_content() +
# Shell.Screenshot.composite_to_stream(), live-confirmed by an earlier
# run of this exact check to actually produce a PNG -- but that first
# run also showed the composited image was the actor's full allocation
# (shadow margin included), not the window: +50px on both axes versus
# frame_rect. The crop _captureModern now applies is unvalidated, so
# this check was tightened from "report any mismatch, informationally"
# to actually asserting it: real window-actor capture uses device
# pixels, so a HiDPI/fractional-scale monitor legitimately produces more
# pixels than frame_rect's logical units -- but *uniformly* on both
# axes. A per-axis scale mismatch (what the shadow-margin bug looked
# like) is not that, and is now a real failure. Parsed by hand (PNG
# signature + IHDR) rather than via Pillow: this project deliberately
# pulls in no image library (see backends/capture.py), and the
# validation script should not need one either just to sanity-check its
# own output.
capture_path = None
try:
    capture_path = b.capture(w)
    size = os.path.getsize(capture_path)
    with open(capture_path, "rb") as f:
        header = f.read(24)
    is_png = header[:8] == b"\x89PNG\r\n\x1a\n"
    if is_png and size > 0:
        width, height = struct.unpack(">II", header[16:24])
        g = b.geometry(w)
        scale_x = width / g[2]
        scale_y = height / g[3]
        print(f"  {Y} capture() wrote a {size}-byte PNG: "
              f"{width}x{height} (window geometry: {g[2]}x{g[3]})")
        if abs(scale_x - scale_y) < 0.01:
            note = "" if abs(scale_x - 1.0) < 0.01 else f" ({scale_x:.3g}x display scale)"
            print(f"  {G} captured size tracks frame geometry uniformly{note}")
        else:
            print(f"  {R} captured size does not track frame geometry "
                  f"uniformly: x scale {scale_x:.3g} vs y scale "
                  f"{scale_y:.3g} -- the crop in _captureModern is off")
            rc = 1
    else:
        print(f"  {R} capture() wrote {capture_path} but it is not a "
              f"valid PNG ({size} bytes, header {header[:8]!r})")
        rc = 1
except CapabilityUnsupported as e:
    print(f"  {Y} WINDOW_CAPTURE not declared ({e}) -- skipping the "
          f"capture check")
except Exception as e:
    print(f"  {R} capture() raised: {type(e).__name__}: {e}"); rc = 1
finally:
    if capture_path and os.path.exists(capture_path):
        os.remove(capture_path)

# Window events: WINDOW_EVENTS is declared unconditionally (see
# gnomeshell.py's capabilities docstring), so this much is assertable
# regardless of what happens to be open. Whether an event actually arrives
# is not -- that depends on someone opening or closing a window during the
# few seconds this listens, which this script cannot make happen on its
# own -- so that part stays informational (Y), not asserted (G/R).
window_events_ok = Capability.WINDOW_EVENTS in b.capabilities
if window_events_ok:
    print(f"  {G} WINDOW_EVENTS is declared")
else:
    print(f"  {R} WINDOW_EVENTS is NOT declared "
          f"(startWatching() may have failed -- check the shell's log)"); rc = 1

seen = []
if not window_events_ok:
    print(f"  {Y} skipping the event listen -- WINDOW_EVENTS is not "
          f"declared, and window_events() would just raise")
else:
    # "new" (window-created) has never been confirmed live: every prior run
    # of this script closed a window during the manual-interaction listen
    # below, none opened one, since that depended on a person's timing
    # rather than anything this script controlled. Spawning and then
    # killing a throwaway GUI app makes both "new" and "close"
    # deterministic instead of hoping a human acts inside a few seconds --
    # and once spawned, letting the human listen still run: it exercises
    # whatever this doesn't cover.
    _SPAWN_CANDIDATES = ["gnome-text-editor", "gedit", "gnome-calculator"]
    spawn_app = next((a for a in _SPAWN_CANDIDATES if shutil.which(a)), None)
    if spawn_app is not None:
        print(f"  {Y} spawning {spawn_app!r} to exercise \"new\" "
              f"deterministically")
        launcher = subprocess.Popen([spawn_app])

        # One continuous subscription for the whole spawn-kill-close
        # sequence, rather than a separate window_events() call before and
        # after killing the process. Tearing one down and standing another
        # up is not free -- signal_unsubscribe/signal_subscribe are their
        # own D-Bus round trips -- and the first version of this script
        # did exactly that: killed the process only *after* the "new"
        # listen's subscription had already been torn down, then opened a
        # fresh one afterwards. A "close" that fired in that gap would be
        # lost with nothing to blame but bad luck; observed live as
        # "it opened and closed it, but still errors". One subscription
        # spanning the kill removes the gap instead of narrowing it.
        new_window_id = None
        target_pid = None
        got_close = False
        events = b.window_events(timeout=15)
        try:
            for event in events:
                seen.append(event)
                if event.change == "new" and new_window_id is None:
                    new_window_id = event.window.handle
                    # The process just launched is NOT necessarily the
                    # window's owner. gnome-text-editor (and most modern
                    # GTK/libadwaita apps) is a GApplication: running it
                    # from the command line asks an existing, or freshly
                    # D-Bus-activated, single-instance service to open a
                    # window, and the launcher process this Popen'd can
                    # exit on its own the moment that request is sent --
                    # terminate() on it then kills nothing and leaves the
                    # real window open. The extension's own ListWindows
                    # already reports each window's real pid, so ask it
                    # rather than trust what was launched.
                    for window in b.windows():
                        if window.handle == new_window_id:
                            target_pid = window.pid
                            break
                    if target_pid:
                        try:
                            os.kill(target_pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass  # already gone -- fine, that is the goal
                    # Whether or not that found anything, also try the
                    # launcher -- on an app that is not single-instance
                    # this is the real (and only) owner, and if it already
                    # exited on its own this is a no-op.
                    if launcher.poll() is None:
                        launcher.terminate()
                elif event.change == "close" and event.window.handle == new_window_id:
                    got_close = True
                    break  # got what this was spawned for; stop listening
        finally:
            events.close()
        try:
            launcher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            launcher.kill()
            launcher.wait(timeout=5)

        if new_window_id is not None:
            print(f"  {G} \"new\" event arrived for the spawned window")
        else:
            print(f"  {R} spawned {spawn_app!r} but no \"new\" event "
                  f"arrived"); rc = 1
        if new_window_id is not None and not target_pid:
            print(f"  {Y} could not determine the window's owning pid; "
                  f"\"close\" depends on the launcher process alone")
        if got_close:
            print(f"  {G} \"close\" event arrived after terminating it")
        elif new_window_id is not None:
            print(f"  {R} terminated {spawn_app!r} but no \"close\" event "
                  f"arrived (a window may still be open -- close it by "
                  f"hand)"); rc = 1
    else:
        print(f"  {Y} none of {_SPAWN_CANDIDATES} are installed; \"new\" "
              f"can only be exercised by opening a window yourself below")

    listen_for = 3.0
    print(f"  {Y} listening for window events for {listen_for:.0f}s -- "
          f"open or close a window now to exercise this for real")
    seen += list(b.window_events(timeout=listen_for))

if seen:
    for event in seen:
        print(f"  {G} event: {event.change} -> {event.window!r}")
elif window_events_ok:
    print(f"  {Y} no events arrived (nothing was opened/closed during the "
          f"window -- not a failure on its own)")

# `wins` was captured before the listen above, which exists precisely to
# invite opening/closing a window -- so it can no longer be trusted for
# what follows. Refreshed rather than reused, or the hit-testing loop
# below chases a window that closed during the listen and gets
# WindowNotFound from a live shell for doing exactly what was asked of it.
wins = b.windows()

# Read-only checks.
print(f"  {Y} active_window(): {b.active_window()}")
# `w` was also picked before the listen above, and the script just invited
# closing "a window" without saying which -- if the one closed was w
# itself (observed: the move/resize battery can land on the very window
# someone then closes), asking about it is a legitimate WindowNotFound,
# not a bug, the same way the hit-testing refresh above treats it.
try:
    print(f"  {Y} is_window_viewable(): {b.is_window_viewable(w)}")
except WindowNotFound:
    print(f"  {Y} is_window_viewable(): skipped -- w closed during the "
          f"event listen above")
# Hit-testing, checked properly rather than just printed. For each
# window find a point inside it that lies inside no *other* window --
# there the correct answer is unambiguous regardless of stacking, so a
# wrong answer is a real failure rather than a stacking judgement call.
def covers(g, px, py):
    return g[0] <= px < g[0] + g[2] and g[1] <= py < g[1] + g[3]

geoms = [(win, b.geometry(win)) for win in wins]
checked = 0
for win, g in geoms:
    for fx, fy in ((0.5, 0.5), (0.15, 0.15), (0.85, 0.85), (0.15, 0.85)):
        px = g[0] + int(g[2] * fx)
        py = g[1] + int(g[3] * fy)
        if sum(1 for _, og in geoms if covers(og, px, py)) != 1:
            continue  # ambiguous: more than one window covers it
        got = b.window_at(px, py, None)
        checked += 1
        # Window defines no __eq__, so == is identity and every call
        # builds a fresh object. pyguitest's own code compares .handle
        # (see Session._window_exists); do the same.
        if got is not None and got.handle == win.handle:
            print(f"  {G} window_at({px},{py}) -> correct window")
        else:
            print(f"  {R} window_at({px},{py}) -> {got!r}, expected {win!r}")
            rc = 1
        break
if checked == 0:
    print(f"  {Y} every window is overlapped; no unambiguous hit-test point")

# Informational: at a point the active window covers, the topmost answer
# should normally be that window. Not asserted -- stacking is the shell's
# call, and "active" does not strictly guarantee "raised".
active = b.active_window()
ag = next((g for w2, g in geoms if w2.handle == active.handle), None) if active else None
if ag:
    amid = (ag[0] + ag[2] // 2, ag[1] + ag[3] // 2)
    at = b.window_at(*amid, None)
    same = at is not None and at.handle == active.handle
    flag = G if same else Y
    print(f"  {flag} window_at(centre of active) -> {at!r}"
          + ("" if same else "  (not the active window -- stacking, or a"
                            " hit-test ordering bug)"))

sys.exit(rc)
PY
pyrc=$?
[ "$pyrc" -eq 0 ] && ok "backend exercise" || bad "backend exercise (exit $pyrc)"

echo
echo "results: $pass passed, $fail failed (plus the Python section above)"
[ "$fail" -eq 0 ]
