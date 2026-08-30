"""Window control via a GNOME Shell extension's D-Bus interface.

The gap this fills: Mutter implements no foreign-toplevel protocol, so a pure
Wayland GNOME session (no XWayland at all) has no window placement, minimize,
pid lookup or hit-testing from anywhere in this package -- X11Backend covers
XWayland, AT-SPI covers element automation and window *listing*, but neither
can move, resize or minimize a window without XWayland. A GNOME Shell
extension is the only remaining path: it runs inside the shell process, which
already has direct Meta.Window access, and exposes what it needs over D-Bus.

Written but not run against a live GNOME Shell -- see the extension's own
file for exactly what is and is not verified. This backend's own D-Bus calls
are ordinary PyGObject/Gio, the same mechanism the atspi extra's PyGObject
dependency already provides; nothing new to install for this specifically,
but the extension itself needs installing and enabling by hand -- see
gnome-shell-extension/README.md.
"""

from __future__ import annotations

import os
import re
import tempfile
import time

from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PyGUITestError,
    WindowNotFound,
)
from .base import GUIBackend, Window
from .windows import WindowEvent

__all__ = ["GnomeShellBackend", "available"]

_BUS_NAME = "org.gnome.Shell"
_OBJECT_PATH = "/org/gnome/Shell/Extensions/Pyguitest"
_INTERFACE = "org.gnome.Shell.Extensions.Pyguitest"


def _gio():
    """Import Gio (and GLib, for Variant construction), or return None.

    Same PyGObject dependency the atspi extra already needs -- see
    docs/adr-001-dependencies.md -- so this adds nothing new to install,
    just a new use of it.
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


class GnomeShellBackend(GUIBackend):
    """Window control through the pyguitest-window-control shell extension."""

    name = "gnomeshell"

    def __init__(self, proxy=None):
        """Connect to the extension's D-Bus object, or an injected proxy.

        Probes with a real call at construction time rather than waiting for
        the first real use: a DBusProxy for a path nobody exports still
        constructs successfully in PyGObject, so without this a missing or
        disabled extension would surface confusingly late, on whatever the
        caller's first real operation happened to be, instead of here.
        """
        modules = _gio()
        if modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs (see README)"
            )
        self._Gio, self._GLib = modules
        if proxy is not None:
            self._proxy = proxy
        else:
            try:
                self._proxy = self._Gio.DBusProxy.new_for_bus_sync(
                    self._Gio.BusType.SESSION,
                    self._Gio.DBusProxyFlags.NONE,
                    None,
                    _BUS_NAME,
                    _OBJECT_PATH,
                    _INTERFACE,
                    None,
                )
            except Exception as exc:
                raise BackendUnavailable(
                    f"cannot reach the session bus: {exc}"
                ) from exc
        try:
            self._list_windows()
        except Exception as exc:
            raise BackendUnavailable(
                "the pyguitest-window-control GNOME Shell extension is not "
                f"installed or not enabled: {exc}"
            ) from exc
        self._can_capture, self._capture_note = self._probe_capture()

    def _probe_capture(self):
        """Whether this extension and shell can capture, and why not.

        Asked once, at construction, by calling CaptureWindow with id 0 --
        a value the extension reserves as a probe because 0 is never a
        real stable_sequence. Two things can be absent independently: an
        older extension has no CaptureWindow method at all, and a shell
        may lack the Mutter APIs it needs. Both answer here.

        Discovering either at the moment a screenshot was wanted, rather
        than when the backend is built, is exactly the late and confusing
        failure this package's capability negotiation exists to prevent.
        """
        try:
            ok, note = self._call("CaptureWindow", "(us)", (0, ""))
        except Exception as exc:
            # Overwhelmingly the commonest cause is a shell still running
            # an older copy of the extension: on Wayland gnome-shell
            # cannot be restarted in place, so copying the files and
            # running `gnome-extensions enable` changes nothing until the
            # session is logged out and back in. Saying so here saves
            # someone concluding the feature is broken when it has simply
            # not been loaded yet.
            return False, (
                f"the running extension has no CaptureWindow method ({exc}). "
                "If you have just installed or updated the extension, "
                "gnome-shell is still running the old copy -- on Wayland it "
                "cannot reload in place, so log out and back in"
            )
        if not ok:
            return False, (
                note or "the extension reported that this GNOME Shell cannot capture"
            )
        return True, ""

    @property
    def capabilities(self):
        """Window control the extension can provide.

        WINDOW_CAPTURE appears only when the running extension and shell
        actually support it -- see _probe_capture. Not SCREEN_CAPTURE:
        the extension captures a window's own actor and has no
        whole-screen method, so claiming it would promise something
        nothing here can deliver.

        WINDOW_EVENTS, unlike WINDOW_CAPTURE, is not behind its own probe:
        it rides on Meta.Display's `window-created` and Meta.Window's
        `unmanaging`/`notify::title` signals, which have been stable
        Mutter API for far longer than REQUIRED_WINDOW_METHODS' own list.
        The extension logs (not raises) if connecting them ever fails, the
        same as a genuinely incompatible shell would for any other API --
        see PyguitestService.startWatching in extension.js.
        """
        capabilities = {
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_RESIZE,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_MINIMIZE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
            Capability.WINDOW_EVENTS,
        }
        if self._can_capture:
            capabilities.add(Capability.WINDOW_CAPTURE)
        return CapabilitySet(capabilities)

    def _handle(self, window):
        """The extension's window id, for a Window or a raw id."""
        return window.handle if isinstance(window, Window) else window

    def _call(self, method, signature=None, args=None):
        """Call `method` on the extension, unpacking its GVariant reply."""
        parameters = self._GLib.Variant(signature, args) if signature else None
        reply = self._proxy.call_sync(
            method, parameters, self._Gio.DBusCallFlags.NONE, -1, None
        )
        return reply.unpack()

    def _list_windows(self):
        """The extension's raw window tuples.

        Each is (id, pid, title, x, y, width, height, minimized, focused).
        """
        (raw,) = self._call("ListWindows")
        return raw

    def _window(self, raw):
        """Build a Window from one ListWindows tuple."""
        wid, pid, title, *_rest = raw
        return Window(handle=wid, backend=self, title=title, pid=pid or None)

    def windows(self):
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        return [self._window(raw) for raw in self._list_windows()]

    def window_events(self, timeout=None):
        """Yield WindowEvents as the extension reports them.

        The extension emits a `WindowEvent(change, id, title)` D-Bus signal
        off Meta.Display's `window-created` and Meta.Window's `unmanaging`/
        `notify::title` (see PyguitestService.startWatching in
        extension.js); `change` is one of "new", "close", "title" -- the
        same vocabulary Session.wait_for_window/wait_window_close already
        consume from the sway/niri backends, so nothing above this needed
        to change to gain GNOME support.

        `title` travels with every event, "close" included, because by the
        time a "close" signal reaches Python the window is already gone
        from ListWindows -- there is nothing left here to look it up
        against, unlike geometry() or is_window_viewable(), which can
        always ask the extension fresh.

        `timeout`, in seconds, bounds the whole subscription; the generator
        simply ends once it expires rather than raising. None waits
        indefinitely. Repeatedly runs a fresh iteration of a shared
        GLib.MainLoop rather than a single blocking run(), so a `timeout`
        can still cut the wait short between events -- the same reason
        portalrequest.py's `request()` needs a loop per call, just several
        of them here instead of one.
        """
        self.require(Capability.WINDOW_EVENTS)
        connection = self._proxy.get_connection()
        deadline = None if timeout is None else time.monotonic() + timeout
        loop = self._GLib.MainLoop()
        queue: list = []

        def on_signal(_conn, _sender, _path, _iface, _signal, params, *_args):
            queue.append(params.unpack())
            if loop.is_running():
                loop.quit()

        subscription = connection.signal_subscribe(
            _BUS_NAME,
            _INTERFACE,
            "WindowEvent",
            _OBJECT_PATH,
            None,
            self._Gio.DBusSignalFlags.NONE,
            on_signal,
            None,
        )
        try:
            while True:
                if not queue:
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
                    loop.run()
                    if timeout_id is not None and not timed_out:
                        self._GLib.source_remove(timeout_id)
                    if not queue:
                        return
                change, wid, title = queue.pop(0)
                yield WindowEvent(
                    change=change,
                    window=Window(handle=wid, backend=self, title=title),
                )
        finally:
            connection.signal_unsubscribe(subscription)

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` (regex) appears.

        Checks existing windows first, so one already open is not missed --
        the same race a bare window_events() subscription would have.
        `timeout`, in seconds, bounds the total wait; None blocks
        indefinitely. Mirrors SwayBackend/NiriBackend's own override of
        this method for the same reason: Session delegates here rather
        than polling once WINDOW_EVENTS is supported.
        """
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        for existing in self.windows():
            if pattern.search(existing.title):
                return existing
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    def _find(self, window):
        """This window's full raw tuple, or raise WindowNotFound."""
        handle = self._handle(window)
        for raw in self._list_windows():
            if raw[0] == handle:
                return raw
        raise WindowNotFound(f"no window with id {handle!r}")

    def geometry(self, window):
        """A window's (x, y, width, height) in screen coordinates."""
        self.require(Capability.WINDOW_GEOMETRY)
        _id, _pid, _title, x, y, width, height, _minimized, _focused = self._find(
            window
        )
        return (x, y, width, height)

    def is_window_viewable(self, window):
        """Whether `window` is currently mapped and showing.

        Approximated as "not minimized": Mutter has no separate scratchpad-
        style hidden state the way sway does, so minimized is the closest
        available signal, not a literal viewability check the way X11's
        map_state is.
        """
        self.require(Capability.WINDOW_STATE)
        raw = self._find(window)
        return not raw[7]

    def active_window(self):
        """The currently focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        for raw in self._list_windows():
            if raw[8]:
                return self._window(raw)
        return None

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y).

        Returns once the request has been *sent*, not once it has been
        *applied*. On Wayland this is inherently asynchronous: the
        compositor proposes the new geometry via an xdg_toplevel configure
        event, and the window's own client decides when to ack it and
        commit a buffer at the new size. A `geometry()` read immediately
        after this call can still return the old value, and firing several
        move/resize calls back-to-back with no gap can outrun that round
        trip entirely -- observed losing 3-4 of 4 rounds at zero delay,
        against 4/4 once each call was given ~200ms to settle (varies by
        client and system load). Poll `geometry()` until it matches if you
        need to know the change has landed before doing anything else.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        self._moveresize(window, x=x, y=y)

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`.

        Same asynchronous-application caveat as :meth:`move_window`.
        """
        self.require(Capability.WINDOW_RESIZE)
        self._moveresize(window, width=width, height=height)

    def _moveresize(self, window, x=None, y=None, width=None, height=None):
        """Move and/or resize, touching only the axes actually given."""
        handle = self._handle(window)
        args = (
            handle,
            x or 0,
            y or 0,
            width or 0,
            height or 0,
            x is not None,
            y is not None,
            width is not None,
            height is not None,
        )
        (ok,) = self._call("MoveResizeWindow", "(uiiiibbbb)", args)
        if not ok:
            raise WindowNotFound(f"no window with id {handle!r}")

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        handle = self._handle(window)
        (ok,) = self._call("ActivateWindow", "(u)", (handle,))
        if not ok:
            raise WindowNotFound(f"no window with id {handle!r}")

    def minimize_window(self, window, minimized=True):
        """Minimize a window, or restore it when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        handle = self._handle(window)
        (ok,) = self._call("MinimizeWindow", "(ub)", (handle, minimized))
        if not ok:
            raise WindowNotFound(f"no window with id {handle!r}")

    def capture(self, window=None, path=None, region=None):
        """Screenshot one window, through the extension.

        The reason this exists. On GNOME under Wayland every other route
        is closed: gnome-screenshot has not been on the allowlist for the
        Shell's own screenshot interface since GNOME 42, XWayland refuses
        to read the X root, and the Screenshot portal raises a consent
        dialog. This code runs *inside* gnome-shell, so it needs no
        portal and prompts for nothing.

        It reads the window's own actor, so the image is the window's
        content rather than whatever is stacked over those screen
        coordinates -- and an occluded window still comes back whole.

        Only per-window: there is no whole-screen method, hence no
        SCREEN_CAPTURE. `region` is refused rather than silently ignored,
        since cropping here would return a rectangle of the *window*
        while every other backend reads a region as screen-absolute.

        The path is made absolute before it is sent. gnome-shell's
        working directory is not the caller's, so a relative path would
        be written somewhere neither intended; the extension refuses one
        outright, and this makes sure a caller never trips over that.
        """
        # The probe already learned why, at construction; passing it on
        # is the difference between "WINDOW_CAPTURE is unsupported" and a
        # message naming which half is missing -- an extension too old to
        # have CaptureWindow, or a shell without the Mutter APIs. Those
        # need opposite fixes, and the bare form points at neither.
        self.require(Capability.WINDOW_CAPTURE, self._capture_note or None)
        if window is None:
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                "the extension captures a window's own actor and has no "
                "whole-screen method; on GNOME Wayland use "
                'connect(backend="portalcapture") for the desktop',
            )
        if region is not None:
            raise ValueError(
                "the GNOME Shell extension captures a whole window; a "
                "region here would be window-relative, while every other "
                "backend reads one as screen-absolute"
            )
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        path = os.path.abspath(path)
        handle = self._handle(window)
        ok, note = self._call("CaptureWindow", "(us)", (handle, path))
        if not ok:
            raise PyGUITestError(f"capturing window {handle!r} failed: {note}")
        return path

    def window_at(self, x, y, screen=0):
        """The topmost window covering a screen coordinate, or None."""
        self.require(Capability.WINDOW_AT_POINT)
        (wid,) = self._call("WindowAtPoint", "(ii)", (x, y))
        if wid == 0:
            return None
        for raw in self._list_windows():
            if raw[0] == wid:
                return self._window(raw)
        return None
