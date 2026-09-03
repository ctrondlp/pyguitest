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

    def unpack(self):
        """Mirrors real GLib.Variant.unpack(): back to a plain Python value."""
        return self.value


class FakeReply:
    """Stands in for the GVariant call_sync() returns."""

    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeConnection:
    """Stands in for the Gio.DBusConnection window_events() subscribes on."""

    def __init__(self):
        self.subscriptions = {}
        self.unsubscribed = []
        self._next_id = 1

    def signal_subscribe(
        self, _bus_name, _iface, _signal, _path, _arg0, _flags, callback, _user_data
    ):
        sub_id = self._next_id
        self._next_id += 1
        self.subscriptions[sub_id] = callback
        return sub_id

    def signal_unsubscribe(self, sub_id):
        self.subscriptions.pop(sub_id, None)
        self.unsubscribed.append(sub_id)

    def deliver(self, change, wid, title):
        """Simulate the extension's WindowEvent signal arriving.

        Real GDBus dispatches to every matching subscription; these tests
        only ever have one live at a time, but broadcasting to all matches
        that rather than assuming it.
        """
        params = FakeVariant("(sus)", (change, wid, title))
        for callback in list(self.subscriptions.values()):
            callback(None, None, None, None, None, params, None)


class FakeLoopDriver:
    """What FakeMainLoop.run() actually does, scripted by each test.

    Stands in for a real GLib main context: nothing here is asynchronous,
    so a test scripts what should "arrive" by pushing onto `scripted`
    (delivered on the connection, exactly like a real WindowEvent signal)
    before calling into window_events()/wait_for_window(). A run() with
    nothing scripted and no pending timeout raises rather than hanging --
    the fake equivalent of what would otherwise be an indefinite block.
    """

    def __init__(self, connection):
        self.connection = connection
        self.scripted = []
        self._timeout_callback = None

    def timeout_add(self, _milliseconds, callback):
        self._timeout_callback = callback
        return 1

    def source_remove(self, _source_id):
        self._timeout_callback = None

    def pump(self):
        if self.scripted:
            self.connection.deliver(*self.scripted.pop(0))
            return
        if self._timeout_callback is not None:
            callback = self._timeout_callback
            self._timeout_callback = None
            callback()
            return
        raise AssertionError(
            "FakeMainLoop.run() was called with nothing scripted -- the "
            "real GLib.MainLoop would block here forever"
        )


class FakeMainLoop:
    """Stands in for GLib.MainLoop, driven by a FakeLoopDriver."""

    def __init__(self, driver):
        self._driver = driver
        self._running = False

    def run(self):
        self._running = True
        self._driver.pump()
        self._running = False

    def quit(self):
        self._running = False

    def is_running(self):
        return self._running


class FakeProxy:
    """Stands in for a Gio.DBusProxy bound to the extension's interface."""

    def __init__(self, windows=None, connection=None):
        self.windows = list(windows) if windows is not None else [_EDITOR, _BROWSER]
        self.calls = []
        self._connection = connection or FakeConnection()

    def get_connection(self):
        return self._connection

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


def _mode(width, height, current=False, name=None):
    """One GetCurrentState mode tuple: (id, w, h, refresh, scale, scales, props)."""
    return (
        name or f"{width}x{height}@60.000",
        width,
        height,
        60.0,
        1.0,
        [1.0, 2.0],
        {"is-current": True} if current else {},
    )


def _monitor(connector, modes):
    """One GetCurrentState monitor: (spec, modes, properties)."""
    return ((connector, "vendor", "product", "serial"), modes, {})


def _logical(connectors, scale=1.0, transform=0, primary=True):
    """One logical monitor: (x, y, scale, transform, primary, specs, props)."""
    return (
        0,
        0,
        scale,
        transform,
        primary,
        [(c, "vendor", "product", "serial") for c in connectors],
        {},
    )


class FakeDisplayConfig:
    """Stands in for a Gio.DBusProxy bound to Mutter's DisplayConfig.

    Shapes transcribed from a live `gdbus call ... GetCurrentState` on
    GNOME Shell 51 rather than from the D-Bus XML, since the nesting is
    what the unpacking has to get right.
    """

    def __init__(self, monitors=None, logical=None, layout_mode=1, fails=None):
        self.monitors = (
            monitors
            if monitors is not None
            else [_monitor("Virtual-1", [_mode(2560, 1600), _mode(1920, 1080, True)])]
        )
        self.logical = logical if logical is not None else [_logical(["Virtual-1"])]
        self.properties = {"layout-mode": layout_mode}
        self.fails = fails
        self.calls = []

    def call_sync(self, method, _parameters, _flags, _timeout, _cancellable):
        self.calls.append(method)
        if self.fails is not None:
            raise self.fails
        return FakeReply((1, self.monitors, self.logical, self.properties))


def install_fake_gi(driver=None, display=None):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **kw: None
    repository = types.ModuleType("gi.repository")

    Gio = types.ModuleType("gi.repository.Gio")
    Gio.BusType = types.SimpleNamespace(SESSION=1)
    Gio.DBusProxyFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusCallFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusSignalFlags = types.SimpleNamespace(NONE=0)
    # Dispatch on the bus name the caller asked for: the backend builds two
    # proxies now, the extension's and Mutter's own DisplayConfig, and
    # handing back the extension fake for both would let a screens() test
    # pass against a proxy that has no GetCurrentState at all.
    display = FakeDisplayConfig() if display is None else display

    def new_for_bus_sync(*args, **_kw):
        name = args[3] if len(args) > 3 else None
        return display if name == "org.gnome.Mutter.DisplayConfig" else FakeProxy()

    Gio.DBusProxy = types.SimpleNamespace(new_for_bus_sync=new_for_bus_sync)

    GLib = types.ModuleType("gi.repository.GLib")
    GLib.Variant = FakeVariant
    if driver is not None:
        GLib.MainLoop = lambda: FakeMainLoop(driver)
        GLib.timeout_add = driver.timeout_add
        GLib.source_remove = driver.source_remove

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
        self.connection = FakeConnection()
        self.driver = FakeLoopDriver(self.connection)
        patcher = install_fake_gi(driver=self.driver)
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import gnomeshell

        self.module = gnomeshell
        self.proxy = FakeProxy(connection=self.connection)
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


class TestScreens(unittest.TestCase):
    """Outputs come from Mutter's DisplayConfig, not from the extension.

    The sizes are *derived* -- GetCurrentState reports the panel's pixel
    mode, and the logical monitor's scale and transform, separately -- so
    each arithmetic case gets its own test. Live cross-check on GNOME
    Shell 51: `screens()` reported 1920x1080 while the extension put a
    maximized window at (0, 32, 1920, 1048), i.e. 32 + 1048 = 1080
    exactly, which is the coordinate-space agreement these encode.
    """

    def _gui(self, display=None, **kwargs):
        display = FakeDisplayConfig(**kwargs) if display is None else display
        self.display = display
        patcher = install_fake_gi(display=display)
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import gnomeshell

        self.module = gnomeshell
        self.proxy = FakeProxy()
        return gnomeshell.GnomeShellBackend(proxy=self.proxy)

    def test_screen_info_is_declared(self):
        self.assertIn(Capability.SCREEN_INFO, self._gui().capabilities)

    def test_the_current_mode_is_the_size_reported(self):
        # Two modes are advertised and only one carries is-current; picking
        # the first (2560x1600) would be a plausible, wrong answer.
        (screen,) = self._gui().screens()
        self.assertEqual(screen.size, (1920, 1080))
        self.assertEqual(screen.name, "Virtual-1")
        self.assertEqual(screen.scale, 1.0)
        self.assertEqual(screen.index, 0)

    def test_a_fractional_scale_gives_the_logical_size(self):
        # The whole reason this reads logical monitors: under logical
        # layout mode the monitor occupies mode/scale units of the space
        # geometry() answers in, so reporting 2560x1600 here would put
        # screens() and geometry() in different coordinate systems.
        gui = self._gui(
            monitors=[_monitor("DP-1", [_mode(2560, 1600, True)])],
            logical=[_logical(["DP-1"], scale=1.25)],
        )
        (screen,) = gui.screens()
        self.assertEqual(screen.size, (2048, 1280))
        self.assertEqual(screen.scale, 1.25)

    def test_physical_layout_mode_does_not_divide(self):
        # layout-mode 2: the scale applies to rendering only, and the
        # monitor really does occupy its full pixel mode.
        gui = self._gui(
            monitors=[_monitor("DP-1", [_mode(2560, 1600, True)])],
            logical=[_logical(["DP-1"], scale=2.0)],
            layout_mode=2,
        )
        (screen,) = gui.screens()
        self.assertEqual(screen.size, (2560, 1600))

    def test_a_rotated_monitor_swaps_the_axes(self):
        for transform in (1, 3, 5, 7):
            with self.subTest(transform=transform):
                gui = self._gui(
                    monitors=[_monitor("DP-1", [_mode(1920, 1080, True)])],
                    logical=[_logical(["DP-1"], transform=transform)],
                )
                (screen,) = gui.screens()
                self.assertEqual(screen.size, (1080, 1920))

    def test_an_unrotated_transform_leaves_the_axes_alone(self):
        for transform in (0, 2, 4, 6):
            with self.subTest(transform=transform):
                gui = self._gui(
                    monitors=[_monitor("DP-1", [_mode(1920, 1080, True)])],
                    logical=[_logical(["DP-1"], transform=transform)],
                )
                (screen,) = gui.screens()
                self.assertEqual(screen.size, (1920, 1080))

    def test_a_mirrored_pair_is_one_screen(self):
        # Two panels, one logical monitor: one entry in the global
        # coordinate space, so one Screen, named after the first.
        gui = self._gui(
            monitors=[
                _monitor("DP-1", [_mode(1920, 1080, True)]),
                _monitor("HDMI-1", [_mode(1920, 1080, True)]),
            ],
            logical=[_logical(["DP-1", "HDMI-1"])],
        )
        (screen,) = gui.screens()
        self.assertEqual(screen.name, "DP-1")

    def test_a_panel_with_no_current_mode_is_skipped_and_indices_stay_dense(self):
        # Inventing a size for an output that reports none would be worse
        # than leaving it out; the survivors must still number from zero
        # without a gap, the way NiriBackend.screens() does.
        gui = self._gui(
            monitors=[
                _monitor("DP-1", [_mode(1920, 1080)]),  # nothing is-current
                _monitor("HDMI-1", [_mode(1280, 720, True)]),
            ],
            logical=[_logical(["DP-1"]), _logical(["HDMI-1"])],
        )
        (screen,) = gui.screens()
        self.assertEqual(
            (screen.index, screen.size, screen.name), (0, (1280, 720), "HDMI-1")
        )

    def test_the_extension_is_not_involved(self):
        # This is a Mutter call. Going through the extension's proxy would
        # work here and fail on any shell running an older copy of it.
        gui = self._gui()
        self.proxy.calls.clear()
        gui.screens()
        self.assertEqual(self.proxy.calls, [])
        self.assertEqual(self.display.calls, ["GetCurrentState"])

    def test_the_display_proxy_is_built_once_and_reused(self):
        # Built lazily, so a caller who never asks for outputs pays
        # nothing -- but not rebuilt per call either.
        gui = self._gui()
        gui.screens()
        gui.screens()
        self.assertEqual(self.display.calls, ["GetCurrentState"] * 2)

    def test_a_display_config_failure_is_a_typed_error(self):
        # Not a bare RuntimeError from inside GDBus: every other
        # unsupported operation here raises CapabilityUnsupported.
        gui = self._gui(fails=RuntimeError("GDBus.Error:ServiceUnknown"))
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.screens()
        self.assertIs(ctx.exception.capability, Capability.SCREEN_INFO)


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


class TestWindowEvents(GnomeShellTestCase):
    """window_events()/wait_for_window() against the fake main loop above.

    Not exercised against real gnome-shell or GLib -- same limitation the
    module docstring states for everything else here. FakeLoopDriver
    stands in for the event loop deterministically: a test scripts what
    "arrives" before consuming the generator, rather than anything actually
    running concurrently.
    """

    def test_yields_a_new_window_event(self):
        self.driver.scripted.append(("new", 3, "Terminal"))
        events = self.gui.window_events(timeout=1)
        event = next(events)
        self.assertEqual(event.change, "new")
        self.assertEqual(event.window.handle, 3)
        self.assertEqual(event.window.title, "Terminal")
        events.close()

    def test_close_event_carries_the_title_even_though_the_window_is_gone(self):
        # Regression risk: by the time a "close" signal reaches Python the
        # window is already gone from ListWindows, so there is nothing left
        # to look its title up against -- the extension must send it with
        # the event itself, not just the id.
        self.driver.scripted.append(("close", 1, "Editor"))
        event = next(self.gui.window_events(timeout=1))
        self.assertEqual(event.change, "close")
        self.assertEqual(event.window.title, "Editor")

    def test_multiple_events_are_each_yielded_in_order(self):
        self.driver.scripted.extend(
            [("new", 3, "Terminal"), ("title", 1, "Editor - modified")]
        )
        events = self.gui.window_events(timeout=1)
        first = next(events)
        second = next(events)
        self.assertEqual((first.change, first.window.title), ("new", "Terminal"))
        self.assertEqual(
            (second.change, second.window.title), ("title", "Editor - modified")
        )
        events.close()

    def test_unsubscribes_when_the_generator_is_closed(self):
        self.driver.scripted.append(("new", 3, "Terminal"))
        events = self.gui.window_events(timeout=1)
        next(events)
        events.close()
        self.assertEqual(len(self.connection.subscriptions), 0)
        self.assertEqual(len(self.connection.unsubscribed), 1)

    def test_a_timeout_with_nothing_arriving_ends_cleanly(self):
        # No scripted signal, so the driver fires the timeout callback
        # instead of delivering an event -- window_events() must end the
        # generator, not raise.
        self.assertEqual(list(self.gui.window_events(timeout=0.001)), [])

    def test_requires_window_events(self):
        gui = self.module.GnomeShellBackend(
            proxy=CapturingProxy(supported=False, missing="x")
        )
        with mock.patch.object(
            type(gui), "capabilities", new_callable=mock.PropertyMock
        ) as caps:
            caps.return_value = self.module.CapabilitySet(())
            with self.assertRaises(CapabilityUnsupported):
                list(gui.window_events(timeout=0.001))

    def test_wait_for_window_returns_an_existing_match_without_subscribing(self):
        found = self.gui.wait_for_window("Edit")
        self.assertEqual(found.title, "Editor")
        self.assertEqual(self.connection.subscriptions, {})

    def test_wait_for_window_picks_up_a_new_matching_window(self):
        self.driver.scripted.append(("new", 3, "Terminal"))
        found = self.gui.wait_for_window("Term", timeout=1)
        self.assertEqual(found.title, "Terminal")

    def test_wait_for_window_ignores_a_close_event(self):
        self.driver.scripted.extend([("close", 1, "Editor"), ("new", 3, "Terminal")])
        found = self.gui.wait_for_window("Terminal", timeout=1)
        self.assertEqual(found.title, "Terminal")

    def test_wait_for_window_times_out_to_none(self):
        self.assertIsNone(self.gui.wait_for_window("Nope", timeout=0.001))


if __name__ == "__main__":
    unittest.main()
