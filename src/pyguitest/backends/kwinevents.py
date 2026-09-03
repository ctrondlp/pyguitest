"""Window events on KDE, via an ad hoc KWin script.

The gap this fills: `kdotool` (this project's KDE window backend) has no
event-subscription mechanism at all -- no `behave`, no watch/wait
subcommand, nothing (checked against `kdotool --help` directly: purely
query/action, unlike `xdotool behave`). Every other window/input/element
capability this package can offer on KDE is already available; this is
the one real gap `Capability.WINDOW_EVENTS` closes.

The GNOME Shell extension (`gnomeshell.py`) exposes its own D-Bus signal,
because it runs inside the shell process and Gio makes that easy. KWin
scripts have no equivalent: introspecting `org.kde.kwin.Scripting` live
turned up `loadScript(path)`/`Script.run()` (load and run an arbitrary
script file at runtime -- no install-and-enable-in-System-Settings step,
unlike a real installed KWin Script or the GNOME extension) and
`callDBus(service, path, interface, method, ...)` *inside* the script
(call an existing D-Bus service -- well-documented; registering a new one
is not). So the architecture inverts relative to GNOME: this backend
hosts the D-Bus service itself, and `_kwin_window_events.js` is the
client, pushing one `Notify(change, id, title)` call per event.

Confirmed live, KDE Plasma 6 / KWin, 2026-09-03: `workspace.windowAdded`,
`.windowRemoved`, and a per-window `captionChanged` all fire correctly
against a real window open/close, each `callDBus` call reaching a
Python-hosted GDBus service. `window.internalId` matches `kdotool`'s own
window-handle format exactly (`{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}`),
cross-checked against a real `kdotool search .` listing -- the detail
that lets a `Window` this yields interoperate with `KdotoolBackend`'s
other operations (geometry, activate, ...) through the composite, since
`CompositeBackend` reads `Window.handle` directly with no ownership
check. See `_kwin_window_events.js`'s own header for the
`workspace.windowList()` vs `workspace.stackingOrder` finding (the former
reliably broke script execution; the latter, a plain property rather
than a method call, did not) and `docs/validation.md` for the full
narrative.

This backend has no window-listing of its own -- `wait_for_window()`
shells out to `kdotool search` for the "does a match already exist"
check, the same tool `KdotoolBackend` wraps, rather than duplicating
window enumeration here.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from importlib import resources

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable
from .base import GUIBackend, Window
from .windows import WindowEvent

__all__ = ["KWinEventsBackend", "available"]

_KWIN_BUS_NAME = "org.kde.KWin"
_SCRIPTING_PATH = "/Scripting"
_SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
_SCRIPT_INTERFACE = "org.kde.kwin.Script"

# Must match _kwin_window_events.js's own BUS_NAME/OBJECT_PATH/INTERFACE.
_BUS_NAME = "org.pyguitest.KWinEvents"
_OBJECT_PATH = "/WindowEvents"
_INTERFACE = "org.pyguitest.KWinEvents"

_IFACE_XML = f"""
<node>
  <interface name="{_INTERFACE}">
    <method name="Notify">
      <arg type="s" direction="in" name="change"/>
      <arg type="s" direction="in" name="id"/>
      <arg type="s" direction="in" name="title"/>
    </method>
  </interface>
</node>"""

_OWN_NAME_TIMEOUT = 5.0
_SUBPROCESS_TIMEOUT = 5.0


def _gio():
    """Import Gio (and GLib, for Variant construction), or return None.

    Same PyGObject dependency the atspi/gnomeshell backends already need
    -- see docs/adr-001-dependencies.md -- so this adds nothing new to
    install, just a new use of it.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    return Gio, GLib


def available():
    """Whether the library this backend needs is importable."""
    return _gio() is not None


class KWinEventsBackend(GUIBackend):
    """Window create/close/title-change events, via an ad hoc KWin script."""

    name = "kwinevents"

    def __init__(self, runner=None, connect=True):
        """Host the events service and load the script.

        `connect=False` skips both -- no D-Bus service is hosted, no KWin
        script is loaded, no session bus is touched at all -- leaving
        `self._events` as a plain empty list a test can append to
        directly, and `self._scripting` as a no-op stand-in `close()`
        does nothing to. That is the whole surface `window_events()` and
        `wait_for_window()`'s own logic depends on; nothing about *how*
        `self._events` gets populated is inside what those two methods
        do. Mirrors GnomeShellBackend's `proxy=None` injection in spirit
        -- a real KWin/D-Bus connection is compound state here rather
        than one proxy object, so a boolean gate stands in for it.
        """
        modules = _gio()
        if modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs"
            )
        self._Gio, self._GLib = modules
        self._runner = runner or self._run_kdotool
        self._events: list[tuple[str, str, str]] = []
        self._connection = None
        self._registration_id = None
        self._own_name_id = None
        self._scripting = None
        self._wake = None
        """The active window_events() call's loop.quit, while it is waiting
        on an empty queue -- None otherwise. Sole reason _handle_method_call
        needs no lock: both it and window_events() only ever touch this
        from the same GLib main context's callbacks, never concurrently."""

        if connect:
            self._host_service()
            self._scripting = self._load_and_run_script()
        else:
            self._scripting = _Closer(lambda: None)

    def _host_service(self):
        """Own `_BUS_NAME` and export the Notify method, waiting for both."""
        node_info = self._Gio.DBusNodeInfo.new_for_xml(_IFACE_XML)
        interface_info = node_info.interfaces[0]
        acquired = []

        def on_bus_acquired(connection, _name):
            self._connection = connection
            self._registration_id = connection.register_object(
                _OBJECT_PATH, interface_info, self._handle_method_call, None, None
            )
            acquired.append(True)

        def on_name_lost(_connection, _name):
            acquired.append(False)

        self._own_name_id = self._Gio.bus_own_name(
            self._Gio.BusType.SESSION,
            _BUS_NAME,
            self._Gio.BusNameOwnerFlags.NONE,
            on_bus_acquired,
            None,
            on_name_lost,
        )
        context = self._GLib.MainContext.default()
        deadline = time.monotonic() + _OWN_NAME_TIMEOUT
        while not acquired and time.monotonic() < deadline:
            context.iteration(True)
        if not acquired or acquired[-1] is False:
            raise BackendUnavailable(
                f"could not own {_BUS_NAME} on the session bus -- another "
                "instance may already be running"
            )

    def _handle_method_call(
        self, connection, sender, path, interface, method, params, invocation
    ):
        """The Notify method the KWin script calls, once per event."""
        if method == "Notify":
            change, window_id, title = params.unpack()
            self._events.append((change, window_id, title))
            invocation.return_value(None)
            if self._wake is not None:
                self._wake()

    def _load_and_run_script(self):
        """Load `_kwin_window_events.js` into KWin and start it running.

        Returns a small object exposing `.close()`, which unloads it --
        matching the shape an injected fake needs for tests.
        """
        script_path = str(
            resources.files("pyguitest.backends") / "_kwin_window_events.js"
        )
        kwin = self._Gio.DBusProxy.new_for_bus_sync(
            self._Gio.BusType.SESSION,
            self._Gio.DBusProxyFlags.NONE,
            None,
            _KWIN_BUS_NAME,
            _SCRIPTING_PATH,
            _SCRIPTING_INTERFACE,
            None,
        )
        try:
            script_id = kwin.call_sync(
                "loadScript",
                self._GLib.Variant("(s)", (script_path,)),
                self._Gio.DBusCallFlags.NONE,
                -1,
                None,
            ).unpack()[0]
            script = self._Gio.DBusProxy.new_for_bus_sync(
                self._Gio.BusType.SESSION,
                self._Gio.DBusProxyFlags.NONE,
                None,
                _KWIN_BUS_NAME,
                f"{_SCRIPTING_PATH}/Script{script_id}",
                _SCRIPT_INTERFACE,
                None,
            )
            script.call_sync("run", None, self._Gio.DBusCallFlags.NONE, -1, None)
        except Exception as exc:
            raise BackendUnavailable(
                f"could not load/run the window-events KWin script: {exc}"
            ) from exc

        def unload():
            # Best-effort: the session is going away either way.
            with contextlib.suppress(Exception):
                kwin.call_sync(
                    "unloadScript",
                    self._GLib.Variant("(s)", (script_path,)),
                    self._Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )

        return _Closer(unload)

    def _run_kdotool(self, argv):
        """Run a kdotool command, returning its stripped stdout, or ''."""
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (result.stdout or "").strip()

    @property
    def capabilities(self):
        """Window events only -- everything else on KDE comes from kdotool."""
        return CapabilitySet({Capability.WINDOW_EVENTS})

    def window_events(self, timeout=None):
        """Yield WindowEvents as the KWin script reports them.

        Mirrors GnomeShellBackend's own version of this method closely:
        one GLib.MainLoop for the whole call, woken either by a timeout
        source or by `_handle_method_call` calling `self._wake()` (its
        `on_signal`'s job there) the moment a Notify arrives -- rather
        than a bare `context.iteration(True)` poll, which an earlier
        draft used and had a real bug, caught before it ever ran against
        a live KWin: with nothing else scheduled on the context (exactly
        the state a `connect=False` test backend is in, deliberately, so
        its own event queue can be exercised without a real KWin),
        `iteration(True)` blocks waiting for a source that will never
        fire, silently ignoring `timeout` altogether.

        Already-queued events (typically true in exactly that same test
        case, and also whenever this generator is resumed with events
        that arrived since the last yield) are drained before touching
        the loop at all, so appending straight to `self._events` and
        calling this is enough to exercise the yield/ordering logic with
        no GLib mainloop iteration involved.
        """
        self.require(Capability.WINDOW_EVENTS)
        deadline = None if timeout is None else time.monotonic() + timeout
        loop = self._GLib.MainLoop()
        try:
            while True:
                if not self._events:
                    timeout_id = None
                    timed_out = False

                    def on_timeout():
                        nonlocal timed_out
                        timed_out = True
                        loop.quit()
                        return False

                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        timeout_id = self._GLib.timeout_add(
                            max(0, int(remaining * 1000)), on_timeout
                        )
                    self._wake = loop.quit
                    loop.run()
                    self._wake = None
                    if timeout_id is not None and not timed_out:
                        self._GLib.source_remove(timeout_id)
                    if not self._events:
                        return
                change, window_id, title = self._events.pop(0)
                window = Window(handle=window_id, backend=self, title=title)
                yield WindowEvent(change=change, window=window)
        finally:
            self._wake = None

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` (regex) appears.

        Checks existing windows first via a plain `kdotool search`, so
        one already open is not missed -- the same race a bare
        `window_events()` subscription would have. This backend has no
        `windows()` of its own to check instead, unlike
        GnomeShellBackend/SwayBackend/NiriBackend's own overrides of this
        method, which is why it shells out here rather than calling
        `self.windows()`.
        """
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        for handle, existing_title in self._existing_windows():
            if pattern.search(existing_title):
                return Window(handle=handle, backend=self, title=existing_title)
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    def _existing_windows(self):
        """(handle, title) for every window kdotool currently sees."""
        handles = self._runner(["kdotool", "search", "."]).splitlines()
        result = []
        for handle in handles:
            handle = handle.strip()
            if not handle:
                continue
            title = self._runner(["kdotool", "getwindowname", handle])
            result.append((handle, title))
        return result

    def close(self):
        """Unload the script and release the D-Bus service."""
        if self._scripting is not None:
            self._scripting.close()
            self._scripting = None
        if self._connection is not None and self._registration_id is not None:
            with contextlib.suppress(Exception):
                self._connection.unregister_object(self._registration_id)
            self._registration_id = None
        if self._own_name_id is not None:
            with contextlib.suppress(Exception):
                self._Gio.bus_unown_name(self._own_name_id)
            self._own_name_id = None


class _Closer:
    """Wraps a zero-argument cleanup callable as a `.close()`-able object."""

    def __init__(self, on_close):
        self._on_close = on_close

    def close(self):
        self._on_close()
