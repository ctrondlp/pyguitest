"""GnomeShellBackend tests against a stand-in Gio/D-Bus proxy.

Not exercised against a real gnome-shell or D-Bus connection anywhere: this
checks the Python-side call construction and reply unpacking, which is the
part unit tests can actually verify. The extension.js side has no test
coverage at all -- there is no way to run gnome-shell in this environment.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PyGUITestError,
    WindowNotFound,
)

# (id, pid, title, x, y, width, height, minimized, focused)
_EDITOR = (1, 100, "Editor", 0, 0, 800, 600, False, True)
_BROWSER = (2, 200, "Browser", 800, 0, 400, 600, False, False)


class FakeVariant:
    """Stands in for GLib.Variant: just remembers what it was built from."""

    def __init__(self, signature, value):
        self.signature = signature
        self.value = value


class FakeReply:
    """Stands in for the GVariant call_sync() returns."""

    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeProxy:
    """Stands in for a Gio.DBusProxy bound to the extension's interface."""

    def __init__(self, windows=None):
        self.windows = list(windows) if windows is not None else [_EDITOR, _BROWSER]
        self.calls = []

    def call_sync(self, method, parameters, flags, timeout, cancellable):
        args = parameters.value if parameters is not None else None
        self.calls.append((method, args))
        handler = getattr(self, f"_{method}")
        return handler(*(args or ()))

    def _ListWindows(self):
        return FakeReply((list(self.windows),))

    def _index(self, wid):
        for i, window in enumerate(self.windows):
            if window[0] == wid:
                return i
        return None

    def _MoveResizeWindow(self, wid, x, y, width, height, mx, my, mw, mh):
        i = self._index(wid)
        if i is None:
            return FakeReply((False,))
        window = list(self.windows[i])
        if mx:
            window[3] = x
        if my:
            window[4] = y
        if mw:
            window[5] = width
        if mh:
            window[6] = height
        self.windows[i] = tuple(window)
        return FakeReply((True,))

    def _ActivateWindow(self, wid):
        i = self._index(wid)
        if i is None:
            return FakeReply((False,))
        self.windows = [(*w[:8], w[0] == wid) for w in self.windows]
        return FakeReply((True,))

    def _MinimizeWindow(self, wid, minimize):
        i = self._index(wid)
        if i is None:
            return FakeReply((False,))
        window = list(self.windows[i])
        window[7] = minimize
        self.windows[i] = tuple(window)
        return FakeReply((True,))

    def _WindowAtPoint(self, x, y):
        for window in reversed(self.windows):
            wid, _pid, _title, wx, wy, ww, wh, minimized, _focused = window
            if minimized:
                continue
            if wx <= x < wx + ww and wy <= y < wy + wh:
                return FakeReply((wid,))
        return FakeReply((0,))


def install_fake_gi():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **kw: None
    repository = types.ModuleType("gi.repository")

    Gio = types.ModuleType("gi.repository.Gio")
    Gio.BusType = types.SimpleNamespace(SESSION=1)
    Gio.DBusProxyFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusCallFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusProxy = types.SimpleNamespace(new_for_bus_sync=lambda *a, **kw: FakeProxy())

    GLib = types.ModuleType("gi.repository.GLib")
    GLib.Variant = FakeVariant

    repository.Gio = Gio
    repository.GLib = GLib
    gi.repository = repository
    return mock.patch.dict(
        sys.modules,
        {
            "gi": gi,
            "gi.repository": repository,
            "gi.repository.Gio": Gio,
            "gi.repository.GLib": GLib,
        },
    )


class GnomeShellTestCase(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import gnomeshell

        self.module = gnomeshell
        self.proxy = FakeProxy()
        self.gui = gnomeshell.GnomeShellBackend(proxy=self.proxy)


class TestAvailability(GnomeShellTestCase):
    def test_available_when_gi_imports(self):
        self.assertTrue(self.module.available())


class TestWindows(GnomeShellTestCase):
    def test_windows_lists_titles_and_pids(self):
        windows = self.gui.windows()
        self.assertEqual([w.title for w in windows], ["Editor", "Browser"])
        self.assertEqual(windows[0].pid, 100)

    def test_geometry(self):
        self.assertEqual(self.gui.geometry(self.gui.windows()[1]), (800, 0, 400, 600))

    def test_geometry_for_unknown_window_raises(self):
        with self.assertRaises(WindowNotFound):
            self.gui.geometry(9999)

    def test_is_window_viewable_reflects_minimized(self):
        window = self.gui.windows()[0]
        self.assertTrue(self.gui.is_window_viewable(window))
        self.gui.minimize_window(window)
        self.assertFalse(self.gui.is_window_viewable(window))

    def test_active_window_follows_the_focused_flag(self):
        self.assertEqual(self.gui.active_window().title, "Editor")

    def test_active_window_is_none_when_nothing_is_focused(self):
        self.proxy.windows = [(*_EDITOR[:8], False)]
        self.assertIsNone(self.gui.active_window())


class TestPlacement(GnomeShellTestCase):
    def test_move_window_touches_only_position(self):
        window = self.gui.windows()[0]
        self.gui.move_window(window, 50, 60)
        self.assertEqual(self.gui.geometry(window), (50, 60, 800, 600))

    def test_resize_window_touches_only_size(self):
        window = self.gui.windows()[0]
        self.gui.resize_window(window, 500, 400)
        self.assertEqual(self.gui.geometry(window), (0, 0, 500, 400))

    def test_move_and_resize_send_only_the_relevant_presence_flags(self):
        window = self.gui.windows()[0]
        self.gui.move_window(window, 50, 60)
        method, args = self.proxy.calls[-1]
        self.assertEqual(method, "MoveResizeWindow")
        _wid, _x, _y, _w, _h, move_x, move_y, resize_w, resize_h = args
        self.assertTrue(move_x)
        self.assertTrue(move_y)
        self.assertFalse(resize_w)
        self.assertFalse(resize_h)

    def test_move_window_for_unknown_window_raises(self):
        with self.assertRaises(WindowNotFound):
            self.gui.move_window(9999, 0, 0)

    def test_activate_window(self):
        window = self.gui.windows()[1]
        self.gui.activate_window(window)
        self.assertEqual(self.gui.active_window().title, "Browser")

    def test_activate_unknown_window_raises(self):
        with self.assertRaises(WindowNotFound):
            self.gui.activate_window(9999)

    def test_minimize_and_restore(self):
        window = self.gui.windows()[0]
        self.gui.minimize_window(window)
        self.assertTrue(self.gui.is_window_viewable(window) is False)
        self.gui.minimize_window(window, minimized=False)
        self.assertTrue(self.gui.is_window_viewable(window))

    def test_minimize_unknown_window_raises(self):
        with self.assertRaises(WindowNotFound):
            self.gui.minimize_window(9999)

    def test_hit_test(self):
        self.assertEqual(self.gui.window_at(50, 50).title, "Editor")
        self.assertEqual(self.gui.window_at(900, 50).title, "Browser")
        self.assertIsNone(self.gui.window_at(5000, 5000))

    def test_hit_test_skips_minimized_windows(self):
        window = self.gui.windows()[0]
        self.gui.minimize_window(window)
        self.assertIsNone(self.gui.window_at(50, 50))


class TestCapabilities(GnomeShellTestCase):
    def test_capabilities_cover_window_control(self):
        for cap in (
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_MINIMIZE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
        ):
            with self.subTest(cap=cap):
                self.assertIn(cap, self.gui.capabilities)


class TestUnavailable(unittest.TestCase):
    def test_missing_pygobject_refuses_with_an_install_hint(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            from pyguitest.backends import gnomeshell

            with self.assertRaises(BackendUnavailable) as ctx:
                gnomeshell.GnomeShellBackend()
            self.assertIn("PyGObject", str(ctx.exception))

    def test_extension_not_running_is_reported_clearly(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import gnomeshell

        class BrokenProxy:
            def call_sync(self, *a, **kw):
                raise RuntimeError(
                    "GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod"
                )

        with self.assertRaises(BackendUnavailable) as ctx:
            gnomeshell.GnomeShellBackend(proxy=BrokenProxy())
        self.assertIn("not installed or not enabled", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class CapturingProxy(FakeProxy):
    """A FakeProxy whose extension implements CaptureWindow.

    Mirrors extension.js: id 0 is the capability probe, a relative path is
    refused because gnome-shell's working directory is not the caller's,
    and the reply is (ok, error) so the Python side can report why.
    """

    def __init__(self, windows=None, supported=True, missing=""):
        super().__init__(windows)
        self.supported = supported
        self.missing = missing
        self.written = []

    def _CaptureWindow(self, wid, path):
        if wid == 0:
            return FakeReply((self.supported, self.missing))
        if not self.supported:
            return FakeReply((False, f"this GNOME Shell lacks {self.missing}"))
        if not path or not path.startswith("/"):
            return FakeReply((False, f"path must be absolute, got {path!r}"))
        if self._index(wid) is None:
            return FakeReply((False, f"no window with id {wid}"))
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\npretend pixels")
        self.written.append(path)
        return FakeReply((True, ""))


class TestCaptureCapabilityIsProbed(GnomeShellTestCase):
    """Whether the extension can capture is asked once, at construction.

    Two things can be absent independently -- an older extension with no
    CaptureWindow method, and a shell missing the Mutter APIs it needs --
    and discovering either when a screenshot was wanted, rather than when
    the backend was built, is the late failure capability negotiation
    exists to prevent.
    """

    def _backend(self, proxy):
        return self.module.GnomeShellBackend(proxy=proxy)

    def test_an_older_extension_simply_does_not_offer_it(self):
        # FakeProxy has no _CaptureWindow at all, which is what an
        # extension predating this feature looks like from here.
        gui = self._backend(FakeProxy())
        self.assertNotIn(Capability.WINDOW_CAPTURE, gui.capabilities)
        # And everything else still works.
        self.assertIn(Capability.WINDOW_LIST, gui.capabilities)

    def test_a_capable_extension_offers_it(self):
        gui = self._backend(CapturingProxy())
        self.assertIn(Capability.WINDOW_CAPTURE, gui.capabilities)

    def test_a_shell_missing_the_mutter_apis_does_not(self):
        gui = self._backend(
            CapturingProxy(supported=False, missing="Meta.WindowActor.get_image")
        )
        self.assertNotIn(Capability.WINDOW_CAPTURE, gui.capabilities)

    def test_the_probe_uses_id_zero_and_happens_once(self):
        proxy = CapturingProxy()
        self._backend(proxy)
        probes = [c for c in proxy.calls if c[0] == "CaptureWindow"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][1][0], 0)

    def test_screen_capture_is_never_claimed(self):
        # The extension captures a window's own actor; there is no
        # whole-screen method, so claiming it would promise nothing.
        gui = self._backend(CapturingProxy())
        self.assertNotIn(Capability.SCREEN_CAPTURE, gui.capabilities)


class TestCaptureWindow(GnomeShellTestCase):
    """The one prompt-free capture path on GNOME Wayland."""

    def setUp(self):
        super().setUp()
        self.proxy = CapturingProxy()
        self.gui = self.module.GnomeShellBackend(proxy=self.proxy)

    def _tempname(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_it_writes_the_named_file_and_returns_the_path(self):
        path = self._tempname()
        window = self.gui.windows()[0]
        self.assertEqual(self.gui.capture(window=window, path=path), path)
        with open(path, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\x89PNG"))

    def test_a_relative_path_is_made_absolute_before_it_is_sent(self):
        # gnome-shell's working directory is not the caller's, so a
        # relative path would be written somewhere neither intended. The
        # extension refuses one; this makes sure nobody trips over that.
        window = self.gui.windows()[0]
        path = self.gui.capture(window=window, path="relative.png")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(path.startswith("/"))
        self.assertEqual(self.proxy.written, [path])

    def test_a_temporary_path_is_allocated_when_none_is_given(self):
        window = self.gui.windows()[0]
        path = self.gui.capture(window=window)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(path.endswith(".png"))

    def test_the_whole_screen_is_refused_with_a_pointer_to_the_portal(self):
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.gui.capture()
        self.assertIn("portalcapture", str(caught.exception))

    def test_a_region_is_refused_rather_than_read_window_relative(self):
        # Every other backend reads a region as screen-absolute; cropping
        # here would silently mean something else.
        window = self.gui.windows()[0]
        with self.assertRaises(ValueError):
            self.gui.capture(window=window, region=(0, 0, 10, 10))

    def test_the_extensions_own_reason_is_reported_on_failure(self):
        with self.assertRaises(PyGUITestError) as caught:
            self.gui.capture(window=999, path=self._tempname())
        self.assertIn("no window with id 999", str(caught.exception))

    def test_capture_is_refused_when_the_extension_cannot_do_it(self):
        gui = self.module.GnomeShellBackend(
            proxy=CapturingProxy(supported=False, missing="Meta.WindowActor.get_image")
        )
        with self.assertRaises(CapabilityUnsupported):
            gui.capture(window=gui.windows()[0], path="/tmp/x.png")

    def test_the_error_says_the_extension_is_too_old(self):
        # "WINDOW_CAPTURE is unsupported on gnomeshell" points at neither
        # of the two causes, which need opposite fixes. This one is a
        # shell still running an older copy of the extension -- the
        # commonest case by far, because on Wayland gnome-shell cannot
        # reload in place and installing the files changes nothing until
        # the session restarts.
        gui = self.module.GnomeShellBackend(proxy=FakeProxy())
        with self.assertRaises(CapabilityUnsupported) as caught:
            gui.capture(window=1, path="/tmp/x.png")
        message = str(caught.exception)
        self.assertIn("no CaptureWindow method", message)
        self.assertIn("log out and back in", message)

    def test_the_error_names_the_missing_shell_api(self):
        # The other cause: extension current, shell too old.
        gui = self.module.GnomeShellBackend(
            proxy=CapturingProxy(supported=False, missing="Meta.WindowActor.get_image")
        )
        with self.assertRaises(CapabilityUnsupported) as caught:
            gui.capture(window=1, path="/tmp/x.png")
        self.assertIn("Meta.WindowActor.get_image", str(caught.exception))
