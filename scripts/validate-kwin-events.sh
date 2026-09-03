#!/usr/bin/env bash
# Validate the pyguitest-window-events KWin script against a LIVE KWin.
#
# Exercises the same script KWinEventsBackend loads (backends/kwinevents.py),
# but drives KWin's Scripting D-Bus interface directly rather than through
# that backend -- a lower-level check, closer to the metal, complementing
# examples/_kwin_events_validate.py's exercise of the real backend class.
#
# No install step, unlike the GNOME extension: KWin's Scripting D-Bus
# interface (org.kde.kwin.Scripting.loadScript/Script.run) loads an
# arbitrary script file at runtime, so this loads straight from the repo.
#
# Must run from a real terminal inside the KDE session, for the same
# reason the GNOME validator does -- a sandboxed bus has no route to
# org.kde.KWin.
#
# Usage: ./scripts/validate-kwin-events.sh
set -uo pipefail

SCRIPT_JS="$(cd "$(dirname "$0")/.." && pwd)/src/pyguitest/backends/_kwin_window_events.js"

case "${DBUS_SESSION_BUS_ADDRESS:-}" in
  */run/flatpak/bus*)
    echo "REFUSING: this is a Flatpak-sandboxed bus with no route to"
    echo "org.kde.KWin. Run from a real KDE terminal."; exit 2;;
esac

if [ ! -f "$SCRIPT_JS" ]; then
  echo "REFUSING: $SCRIPT_JS not found"; exit 2
fi

echo "== 1. KWin's Scripting interface is reachable =="
if busctl --user introspect org.kde.KWin /Scripting >/dev/null 2>&1; then
  echo "  PASS  org.kde.kwin.Scripting is reachable"
else
  echo "  FAIL  org.kde.KWin /Scripting is not reachable -- not running under KWin?"
  exit 1
fi

echo "== 2. loading, running, and exercising the script =="
python3 - "$SCRIPT_JS" <<'PY'
import shutil
import subprocess
import sys
import time

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except Exception as exc:
    print(f"  FAIL  PyGObject is not importable: {exc}")
    sys.exit(1)

SCRIPT_PATH = sys.argv[1]

G = "\033[32mPASS\033[0m"
R = "\033[31mFAIL\033[0m"
Y = "\033[33m..\033[0m  "
passed = 0
failed = 0


def ok(msg):
    global passed
    print(f"  {G} {msg}")
    passed += 1


def bad(msg):
    global failed
    print(f"  {R} {msg}")
    failed += 1


def note(msg):
    print(f"  {Y} {msg}")


BUS_NAME = "org.pyguitest.KWinEvents"
OBJECT_PATH = "/WindowEvents"
INTERFACE = "org.pyguitest.KWinEvents"
# Must match pyguitest-window-events.js's own BUS_NAME/OBJECT_PATH/INTERFACE.

IFACE_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="Notify">
      <arg type="s" direction="in" name="change"/>
      <arg type="s" direction="in" name="id"/>
      <arg type="s" direction="in" name="title"/>
    </method>
  </interface>
</node>"""

events = []


def handle_method_call(conn, sender, path, iface, method, params, invocation):
    if method == "Notify":
        events.append(params.unpack())
        invocation.return_value(None)


node_info = Gio.DBusNodeInfo.new_for_xml(IFACE_XML)
ctx = GLib.MainContext.default()
registered = []


def on_bus_acquired(conn, name):
    conn.register_object(
        OBJECT_PATH, node_info.interfaces[0], handle_method_call, None, None
    )
    registered.append(True)


Gio.bus_own_name(
    Gio.BusType.SESSION,
    BUS_NAME,
    Gio.BusNameOwnerFlags.NONE,
    on_bus_acquired,
    None,
    None,
)

deadline = time.monotonic() + 3
while not registered and time.monotonic() < deadline:
    ctx.iteration(True)
if not registered:
    bad("D-Bus service never registered")
    sys.exit(1)
ok(f"hosting {BUS_NAME}{OBJECT_PATH}")

kwin = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION,
    Gio.DBusProxyFlags.NONE,
    None,
    "org.kde.KWin",
    "/Scripting",
    "org.kde.kwin.Scripting",
    None,
)

try:
    script_id = kwin.call_sync(
        "loadScript",
        GLib.Variant("(s)", (SCRIPT_PATH,)),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    ).unpack()[0]
except Exception as exc:
    bad(f"loadScript failed: {exc}")
    sys.exit(1)
ok(f"loadScript({SCRIPT_PATH!r}) -> id {script_id}")

script_proxy = Gio.DBusProxy.new_for_bus_sync(
    Gio.BusType.SESSION,
    Gio.DBusProxyFlags.NONE,
    None,
    "org.kde.KWin",
    f"/Scripting/Script{script_id}",
    "org.kde.kwin.Script",
    None,
)
try:
    script_proxy.call_sync("run", None, Gio.DBusCallFlags.NONE, -1, None)
except Exception as exc:
    bad(f"Script.run failed: {exc}")
    sys.exit(1)
ok("script running")

SPAWN_CANDIDATES = ["gnome-text-editor", "gedit", "kate", "gnome-calculator"]
spawn_app = next((a for a in SPAWN_CANDIDATES if shutil.which(a)), None)
if spawn_app is None:
    note(f"none of {SPAWN_CANDIDATES} found on PATH; skipping new/close checks")
else:
    note(f"spawning {spawn_app!r} to exercise \"new\" deterministically")
    proc = subprocess.Popen([spawn_app])

    deadline = time.monotonic() + 12
    got_new = None
    while time.monotonic() < deadline and got_new is None:
        ctx.iteration(True)
        for change, wid, title in events:
            if change == "new":
                got_new = (wid, title)
                break

    if got_new:
        ok(f'"new" event arrived: id={got_new[0]} title={got_new[1]!r}')
    else:
        bad('no "new" event arrived within 12s')

    if got_new and shutil.which("kdotool"):
        try:
            listing = subprocess.run(
                ["kdotool", "search", "."],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            if got_new[0] in listing:
                ok("event id matches a real kdotool window handle")
            else:
                bad(f"event id {got_new[0]!r} not found in kdotool's own listing")
        except Exception as exc:
            note(f"kdotool cross-check skipped: {exc}")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    deadline = time.monotonic() + 8
    got_close = any(c == "close" for c, _wid, _title in events)
    while time.monotonic() < deadline and not got_close:
        ctx.iteration(True)
        got_close = any(c == "close" for c, _wid, _title in events)

    if got_close:
        ok('"close" event arrived after terminating it')
    else:
        bad('no "close" event arrived within 8s (a window may still be open)')

try:
    kwin.call_sync(
        "unloadScript",
        GLib.Variant("(s)", (SCRIPT_PATH,)),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    ok("unloadScript succeeded")
except Exception as exc:
    note(f"unloadScript: {exc}")

print()
print(f"results: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY
