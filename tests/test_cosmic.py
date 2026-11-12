"""The COSMIC window backend, driven over a socketpair against a scripted compositor.

The risk here is the Wayland wire format: a hand-rolled client has no library
to catch a wrong opcode, a wrong word count, or a misaligned string. The fake
compositor below re-encodes its own messages from the same specification, so a
bug in the client's encoder would not be masked by a matching bug in the test
-- the test's bytes are written independently and the client must parse them.
"""

import os
import sys
import unittest

if sys.platform == "win32":  # pragma: no cover - platform guard
    raise unittest.SkipTest(
        "COSMIC's window protocols are spoken over AF_UNIX sockets, which "
        "this platform's socket module does not define"
    )

import socket
import struct
import warnings
from unittest import mock

from pyguitest.backends.cosmic import (
    CosmicBackend,
    HandshakeRefusalWarning,
    _bgr_rows,
    _CosmicConnection,
    _crop_bgra,
    _Toplevel,
    connect_cosmic,
)
from pyguitest.backends.windows import for_compositor
from pyguitest.capabilities import Capability
from pyguitest.errors import (
    CapabilityUnsupported,
    PyGUITestError,
    WindowNotFound,
)
from pyguitest.png import png_size
from pyguitest.session import Compositor

# Object ids, traced through the client's deterministic allocation order:
#   registry=2, first callback=3, info=4, manager=5, list=6, seat=7,
#   wl_output=8, second callback=9, cosmic handle=10, third callback=11.
REGISTRY = 2
INFO = 4
MANAGER = 5
LIST = 6
SEAT = 7
WOUT = 8
COSMIC_HANDLE = 10

# Server-allocated ids (high bit set, per the wire spec's object-id split).
FOREIGN = 0xFF000001


def _pad(length):
    return (4 - (length & 3)) & 3


def _msg(object_id, opcode, *fields):
    """Encode one Wayland message, independently of the code under test."""
    body = bytearray()
    for kind, value in fields:
        if kind in ("u", "o", "n"):
            body += struct.pack("<I", value)
        elif kind == "i":
            body += struct.pack("<i", value)
        elif kind == "s":
            raw = value.encode() + b"\0"
            body += struct.pack("<I", len(raw)) + raw + b"\0" * _pad(len(raw))
        elif kind == "a":
            body += struct.pack("<I", len(value)) + value + b"\0" * _pad(len(value))
        else:
            raise AssertionError(f"bad field kind {kind!r}")
    size = 8 + len(body)
    return struct.pack("<II", object_id, (size << 16) | opcode) + bytes(body)


def _u32s(*values):
    return struct.pack("<" + "I" * len(values), *values)


def _done(callback_id):
    return _msg(callback_id, 0, ("u", 0))


def _handshake_script():
    """The compositor's side of one full handshake, one window, one geometry.

    Order mirrors the client's round-trips: registry globals first, then the
    foreign toplevel and its title/app_id, then the bound wl_output's own
    geometry (with mode, scale and done, which the client reads and discards),
    each flushed by its own callback `done`. The cosmic handle's state and
    geometry then arrive on the refresh cycle, *after* the callbacks -- which
    is exactly what the bounded drain waits for.
    """
    out = bytearray()
    out += _msg(REGISTRY, 0, ("u", 1), ("s", "zcosmic_toplevel_info_v1"), ("u", 3))
    out += _msg(REGISTRY, 0, ("u", 2), ("s", "zcosmic_toplevel_manager_v1"), ("u", 4))
    out += _msg(REGISTRY, 0, ("u", 3), ("s", "ext_foreign_toplevel_list_v1"), ("u", 1))
    out += _msg(REGISTRY, 0, ("u", 4), ("s", "wl_seat"), ("u", 9))
    out += _msg(REGISTRY, 0, ("u", 5), ("s", "wl_output"), ("u", 4))
    out += _done(3)
    out += _msg(LIST, 0, ("u", FOREIGN))
    out += _msg(FOREIGN, 2, ("s", "Editor"))
    out += _msg(FOREIGN, 3, ("s", "org.editor"))
    out += _msg(FOREIGN, 1)  # done
    out += _msg(
        WOUT,
        0,
        ("i", 100),
        ("i", 50),
        ("i", 0),
        ("i", 0),
        ("i", 0),
        ("s", "make"),
        ("s", "model"),
        ("i", 0),
    )  # wl_output.geometry: origin (100, 50)
    out += _msg(WOUT, 1, ("u", 3), ("i", 1920), ("i", 1080), ("i", 60000))  # mode
    out += _msg(WOUT, 3, ("i", 1))  # scale
    out += _msg(WOUT, 2)  # done
    out += _done(9)
    out += _done(11)
    out += _msg(COSMIC_HANDLE, 8, ("a", _u32s(2)))  # state: activated
    out += _msg(
        COSMIC_HANDLE, 9, ("u", WOUT), ("i", 10), ("i", 20), ("i", 800), ("i", 600)
    )  # geometry, output-relative (10, 20) -> absolute (110, 70)
    return bytes(out)


def _recv_all(sock, timeout=0.2):
    """Read everything the client has sent so far, once it goes quiet."""
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except TimeoutError:
        pass
    return data


def _deliver(backend, object_id, opcode, *fields):
    """Dispatch one compositor message straight into the client's handler.

    Encoded by `_msg`, the test's own independent encoder, then handed to
    `_handle` with the header stripped -- which is where `_recv_message`
    hands every real message anyway. Written to the handler rather than to
    the socket because `_make_backend` queues spare `done` callbacks behind
    the handshake: a test cannot say how many of them an earlier call
    consumed, so it cannot say how many reads its own appended message sits
    behind, and reading far enough to find it starves the *next* call of the
    callback it waits for.
    """
    raw = _msg(object_id, opcode, *fields)
    backend._conn._handle(object_id, opcode, raw[8:])


def _make_backend(test, done_count=8):
    """A CosmicBackend over a socketpair, pre-fed the handshake plus spare `done`s.

    The spare `done`s give each read method a callback to complete against.
    """
    server, client = socket.socketpair()
    client.settimeout(2.0)
    script = _handshake_script()
    for i in range(done_count):
        script += _done(12 + i)
    server.sendall(script)
    backend = CosmicBackend(_CosmicConnection(client))
    test.addCleanup(server.close)
    test.addCleanup(backend.close)
    return backend, server


class TestCosmicBackend(unittest.TestCase):
    """The backend contract, exercised against the scripted compositor."""

    def test_windows_lists_titles_and_app_ids(self):
        backend, _ = _make_backend(self)
        windows = backend.windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].title, "Editor")
        self.assertEqual(windows[0].app_id, "org.editor")

    def test_active_window_is_the_activated_one(self):
        backend, _ = _make_backend(self)
        active = backend.active_window()
        self.assertIsNotNone(active)
        self.assertEqual(active.title, "Editor")

    def test_geometry_returns_the_last_geometry_event(self):
        backend, _ = _make_backend(self)
        window = backend.windows()[0]
        self.assertEqual(backend.geometry(window), (110, 70, 800, 600))

    def test_is_window_viewable_is_true_when_not_minimized(self):
        backend, _ = _make_backend(self)
        window = backend.windows()[0]
        self.assertTrue(backend.is_window_viewable(window))

    def test_window_at_hit_tests_geometry(self):
        backend, _ = _make_backend(self)
        self.assertIsNotNone(backend.window_at(200, 150))
        self.assertIsNone(backend.window_at(5000, 5000))

    def test_geometry_for_unknown_handle_raises(self):
        backend, _ = _make_backend(self)
        with self.assertRaises(WindowNotFound):
            backend.geometry(type("W", (), {"handle": 0xDEAD, "backend": backend})())

    def test_activate_sends_the_management_request(self):
        backend, server = _make_backend(self)
        window = backend.windows()[0]
        backend.activate_window(window)
        self.assertIn(
            _msg(MANAGER, 2, ("o", COSMIC_HANDLE), ("o", SEAT)), _recv_all(server)
        )

    def test_minimize_sends_set_minimized(self):
        backend, server = _make_backend(self)
        window = backend.windows()[0]
        backend.minimize_window(window)
        self.assertIn(_msg(MANAGER, 5, ("o", COSMIC_HANDLE)), _recv_all(server))

    def test_restore_sends_unset_minimized(self):
        backend, server = _make_backend(self)
        window = backend.windows()[0]
        backend.minimize_window(window, minimized=False)
        self.assertIn(_msg(MANAGER, 6, ("o", COSMIC_HANDLE)), _recv_all(server))

    def test_cosmic_of_binds_an_unbound_handle_via_one_sync(self):
        # A window from the event path has a toplevel whose management handle
        # is not bound yet (cosmic is None). _cosmic_of must trigger one sync
        # -- which binds it -- rather than raise WindowNotFound for a window
        # that is open and listed.
        backend, _ = _make_backend(self)
        handle = 0xFF000050
        toplevel = _Toplevel(backend._conn._outputs)
        backend._conn._toplevels[handle] = toplevel
        window = backend._window(handle, toplevel)
        self.assertIsNone(toplevel.cosmic)

        def bind():
            toplevel.cosmic = 4242

        with mock.patch.object(backend, "_refresh", side_effect=bind) as refresh:
            self.assertEqual(backend._cosmic_of(window), 4242)
        refresh.assert_called_once_with()

    def test_cosmic_of_still_raises_when_a_sync_cannot_bind(self):
        # A genuinely gone window has no handle even after the sync, and must
        # stay a WindowNotFound rather than loop or succeed.
        backend, _ = _make_backend(self)
        handle = 0xFF000051
        toplevel = _Toplevel(backend._conn._outputs)
        backend._conn._toplevels[handle] = toplevel
        window = backend._window(handle, toplevel)
        with mock.patch.object(backend, "_refresh") as refresh:
            with self.assertRaises(WindowNotFound):
                backend._cosmic_of(window)
        refresh.assert_called_once_with()


class TestOutputOriginChangesAfterTheGeometry(unittest.TestCase):
    """A window's rectangle follows its output when the output moves.

    `wl_output.geometry` is re-sent whenever an output's properties change, so
    moving a monitor in the display settings, or a hotplug that re-lays-out
    the desktop, moves every window on it without any toplevel saying so.
    Adding the origin once, when the toplevel geometry arrived, left the
    cached answer offset by the difference for the rest of the session -- and
    that rectangle is what `window_at` hit-tests and what `click_element`
    aims at, so it reads as clicks landing in the wrong place.
    """

    def test_a_later_output_move_moves_the_window_with_it(self):
        backend, _ = _make_backend(self)
        window = backend.windows()[0]
        self.assertEqual(backend.geometry(window), (110, 70, 800, 600))
        # The same output, re-announced 300px to the right and 25px down.
        _deliver(
            backend,
            WOUT,
            0,
            ("i", 400),
            ("i", 75),
            ("i", 0),
            ("i", 0),
            ("i", 0),
            ("s", "make"),
            ("s", "model"),
            ("i", 0),
        )
        # The toplevel never said anything; its rectangle moved anyway.
        self.assertEqual(backend.geometry(window), (410, 95, 800, 600))

    def test_window_at_hit_tests_the_moved_rectangle(self):
        backend, _ = _make_backend(self)
        self.assertIsNotNone(backend.window_at(200, 150))
        _deliver(
            backend,
            WOUT,
            0,
            ("i", 1100),
            ("i", 50),
            ("i", 0),
            ("i", 0),
            ("i", 0),
            ("s", "make"),
            ("s", "model"),
            ("i", 0),
        )
        self.assertIsNone(backend.window_at(200, 150))
        self.assertIsNotNone(backend.window_at(1200, 150))


class TestProtocolErrorsReachTheCaller(unittest.TestCase):
    """`wl_display.error` is the compositor's last word, not noise.

    Messages addressed to `wl_display` used to fall off the end of `_handle`
    unread, so the object, the code and the compositor's own sentence were all
    dropped -- and because nothing on the connection is answered after an
    error, the failure surfaced at the next read as the generic
    "the compositor closed the connection".
    """

    def test_the_error_message_is_raised_rather_than_discarded(self):
        backend, _ = _make_backend(self)
        with self.assertRaises(PyGUITestError) as caught:
            _deliver(
                backend,
                1,  # wl_display's fixed object id
                0,  # wl_display.error
                ("o", INFO),
                ("u", 7),
                ("s", "invalid method 9, object zcosmic_toplevel_info_v1"),
            )
        self.assertIn("invalid method 9", str(caught.exception))
        self.assertIn("code 7", str(caught.exception))

    def test_an_unrelated_display_event_is_ignored(self):
        # delete_id is ordinary bookkeeping; ids are never reused here.
        backend, _ = _make_backend(self)
        _deliver(backend, 1, 1, ("u", 11))  # wl_display.delete_id, must not raise


class TestConnectCosmic(unittest.TestCase):
    """The factory's no-backend answers, and its wiring into for_compositor."""

    def test_no_runtime_dir_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(connect_cosmic())

    def test_no_socket_returns_none(self):
        env = {"XDG_RUNTIME_DIR": "/nonexistent", "WAYLAND_DISPLAY": "wayland-0"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsNone(connect_cosmic())

    def test_for_compositor_routes_cosmic(self):
        with mock.patch("pyguitest.backends.cosmic.connect_cosmic") as connect:
            connect.return_value = object()
            backend = for_compositor(Compositor.COSMIC)
            self.assertIs(backend, connect.return_value)
            connect.assert_called_once_with()

    def test_for_compositor_returns_none_on_connection_failure(self):
        with mock.patch(
            "pyguitest.backends.cosmic.connect_cosmic", side_effect=OSError
        ):
            self.assertIsNone(for_compositor(Compositor.COSMIC))

    def test_a_handshake_protocol_error_is_no_backend_not_a_crash(self):
        # `_handle_display` turns `wl_display.error` into a PyGUITestError --
        # the right loud answer on an established connection, and one the
        # except tuple here used to let escape: `connect()`'s best-effort
        # probe crashed on it, and the socket was left open on the way out.
        # The compositor's own sentence survives as a warning.
        fake_sock = mock.Mock()
        env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("pyguitest.backends.cosmic.os.path.exists", return_value=True),
            mock.patch(
                "pyguitest.backends.cosmic.socket.socket",
                return_value=fake_sock,
            ),
            mock.patch(
                "pyguitest.backends.cosmic._CosmicConnection",
                side_effect=PyGUITestError("cosmic-comp refused this client"),
            ),
            self.assertWarns(HandshakeRefusalWarning) as caught,
        ):
            self.assertIsNone(connect_cosmic())
        fake_sock.close.assert_called_once()
        self.assertIn("refused this client", str(caught.warning))

    def test_the_refusal_warning_never_crashes_the_probe(self):
        # pytest suites routinely run with warnings-as-errors; the probe's
        # never-crash contract must survive that filter too, or the error
        # filter quietly brings back the pre-fix crash.
        fake_sock = mock.Mock()
        env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("pyguitest.backends.cosmic.os.path.exists", return_value=True),
            mock.patch(
                "pyguitest.backends.cosmic.socket.socket",
                return_value=fake_sock,
            ),
            mock.patch(
                "pyguitest.backends.cosmic._CosmicConnection",
                side_effect=PyGUITestError("cosmic-comp refused this client"),
            ),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("error")
            self.assertIsNone(connect_cosmic())
        fake_sock.close.assert_called_once()


class TestCaptureHelpers(unittest.TestCase):
    """The pixel shuffles the capture path depends on, in isolation.

    The capture flow itself is live-verified (probe 7 against a real COSMIC
    desktop); these pin the two pure functions that turn the compositor's
    BGR-order buffer into a PNG, which a live run cannot easily prove are
    colour-correct.
    """

    def test_bgr_rows_reorders_each_pixel_to_rgb(self):
        # Two pixels: (R=10,G=20,B=30) then (R=40,G=50,B=60).
        raw = bytes([10, 20, 30, 0xFF, 40, 50, 60, 0x00])
        self.assertEqual(list(_bgr_rows(raw, 2, 1)), [bytes([10, 20, 30, 40, 50, 60])])

    def test_crop_extracts_the_rectangle(self):
        # 2x2 pixels of [R, G, B, X]; take the bottom-right 1x1.
        raw = bytes([1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0, 10, 11, 12, 0])
        self.assertEqual(_crop_bgra(raw, 2, 2, 1, 1, 1, 1), bytes([10, 11, 12, 0]))

    def test_crop_rejects_a_rectangle_outside_the_buffer(self):
        with self.assertRaises(CapabilityUnsupported):
            _crop_bgra(b"\0" * 16, 2, 2, 1, 1, 2, 1)


class TestCaptureCapabilities(unittest.TestCase):
    """Capture is claimed only when the copy-capture globals were bound."""

    def _backend(self, **attrs):
        state = {
            "_capture_mgr": 0,
            "_shm": 0,
            "_output_capture_mgr": 0,
            "_toplevel_capture_mgr": 0,
            "_outputs": {},
        }
        state.update(attrs)
        return CosmicBackend(mock.Mock(**state))

    def test_capture_is_claimed_when_the_globals_exist(self):
        backend = self._backend(
            _capture_mgr=1,
            _shm=1,
            _output_capture_mgr=1,
            _toplevel_capture_mgr=1,
            _outputs={1: (0, 0)},
        )
        self.assertIn(Capability.SCREEN_CAPTURE, backend.capabilities)
        self.assertIn(Capability.WINDOW_CAPTURE, backend.capabilities)

    def test_capture_is_not_claimed_without_them(self):
        backend = self._backend()
        self.assertNotIn(Capability.SCREEN_CAPTURE, backend.capabilities)
        self.assertNotIn(Capability.WINDOW_CAPTURE, backend.capabilities)

    def test_screen_capture_needs_an_output(self):
        backend = self._backend(
            _capture_mgr=1, _shm=1, _output_capture_mgr=1, _toplevel_capture_mgr=1
        )
        self.assertNotIn(Capability.SCREEN_CAPTURE, backend.capabilities)
        self.assertIn(Capability.WINDOW_CAPTURE, backend.capabilities)


def _output(x, y, width, height):
    return mock.Mock(x=x, y=y, width=width, height=height)


class TestOutputForRegion(unittest.TestCase):
    """capture() picks the output a region actually lands on, not just the first."""

    def test_picks_the_output_that_contains_the_region(self):
        conn = mock.Mock(
            _outputs={
                1: _output(0, 0, 1280, 800),
                2: _output(1280, 0, 1920, 1080),
            }
        )
        backend = CosmicBackend(conn)
        self.assertEqual(
            backend._output_for_region((1280, 10, 100, 100)),
            (2, (1280, 10, 100, 100)),
        )
        self.assertEqual(
            backend._output_for_region((10, 10, 100, 100)),
            (1, (10, 10, 100, 100)),
        )

    def test_one_pixel_of_covering_overhang_is_fitted_to_the_output(self):
        # check_region covers fractional coordinates outward, so an
        # edge-flush region legitimately lands one pixel past the output;
        # the shared fit hands back the visible rectangle instead of
        # refusing the capture the covering exists to serve.
        conn = mock.Mock(_outputs={1: _output(0, 0, 1280, 800)})
        backend = CosmicBackend(conn)
        self.assertEqual(
            backend._output_for_region((1200, 10, 81, 50)),
            (1, (1200, 10, 80, 50)),
        )
        self.assertEqual(
            backend._output_for_region((-1, 5, 50, 50)),
            (1, (0, 5, 49, 50)),
        )

    def test_a_region_crossing_two_outputs_matches_neither(self):
        conn = mock.Mock(
            _outputs={
                1: _output(0, 0, 1280, 800),
                2: _output(1280, 0, 1920, 1080),
            }
        )
        backend = CosmicBackend(conn)
        self.assertIsNone(backend._output_for_region((1200, 10, 200, 100)))

    def test_no_output_contains_a_fully_offscreen_region(self):
        conn = mock.Mock(_outputs={1: _output(0, 0, 1280, 800)})
        backend = CosmicBackend(conn)
        self.assertIsNone(backend._output_for_region((5000, 5000, 10, 10)))


class TestCaptureSelectsTheRightOutput(unittest.TestCase):
    """capture(region=...) sources pixels from the output the region is on."""

    def _backend(self):
        conn = mock.Mock(
            _outputs={
                1: _output(0, 0, 1280, 800),
                2: _output(1280, 0, 1920, 1080),
            }
        )
        conn._create_output_source.return_value = 99
        conn._capture_frame.return_value = (100, 100, b"\0" * (100 * 100 * 4))
        return CosmicBackend(conn), conn

    def test_a_region_on_the_second_output_sources_from_it(self):
        backend, conn = self._backend()
        backend.capture(region=(1280, 10, 50, 50))
        conn._create_output_source.assert_called_once_with(2)

    def test_a_region_off_every_output_raises_ValueError(self):
        # Off every screen -- a malformed rectangle that would fail
        # identically on every capture member, so it is a ValueError (which
        # the composite lets propagate uncached), not a cacheable
        # PyGUITestError that would blacklist the member.
        backend, conn = self._backend()
        with self.assertRaises(ValueError):
            backend.capture(region=(5000, 5000, 100, 100))
        conn._create_output_source.assert_not_called()

    def test_a_region_spanning_two_outputs_raises_CaptureRegionUnsupported(self):
        # On-screen but straddling the seam between the two outputs: a valid
        # region COSMIC cannot crop across two output buffers. Not a
        # ValueError (it is not malformed) and not a cacheable mechanism
        # failure -- CaptureRegionUnsupported, so the composite can fall back
        # to a whole-desktop member without blacklisting COSMIC.
        from pyguitest.errors import CaptureRegionUnsupported

        backend, conn = self._backend()
        with self.assertRaises(CaptureRegionUnsupported):
            backend.capture(region=(1200, 10, 200, 50))
        conn._create_output_source.assert_not_called()

    def test_neither_unservable_region_blacklists_the_backend(self):
        # The regression that started this: an out-of-bounds region used to
        # raise CapabilityUnsupported (a PyGUITestError), which the composite
        # caches -- so one bad region permanently disabled COSMIC capture.
        # Neither an off-screen ValueError nor a spanning
        # CaptureRegionUnsupported is a cached mechanism failure, so a later
        # valid capture must still reach COSMIC.
        from pyguitest.backends.composite import CompositeBackend
        from pyguitest.errors import CaptureRegionUnsupported

        member, conn = self._backend()
        composite = CompositeBackend([member])
        with self.assertRaises(ValueError):
            composite.capture(region=(5000, 5000, 100, 100))
        with self.assertRaises(CaptureRegionUnsupported):
            composite.capture(region=(1200, 10, 200, 50))
        # A valid single-output region afterwards is served, not refused by a
        # stale blacklist.
        composite.capture(region=(1280, 10, 50, 50))
        conn._create_output_source.assert_called_once_with(2)

    def test_no_region_still_defaults_to_the_first_output(self):
        backend, conn = self._backend()
        backend.capture()
        conn._create_output_source.assert_called_once_with(1)

    def test_an_edge_flush_covered_region_is_captured_fitted(self):
        # One pixel past the output's far edge -- what covering adds for a
        # fractional edge-flush region -- is fitted and captured rather
        # than refused.
        conn = mock.Mock(_outputs={1: _output(0, 0, 100, 100)})
        conn._create_output_source.return_value = 99
        conn._capture_frame.return_value = (100, 100, b"\0" * (100 * 100 * 4))
        backend = CosmicBackend(conn)
        path = backend.capture(region=(60, 10, 41, 50))
        conn._create_output_source.assert_called_once_with(1)
        self.assertEqual(png_size(path), (40, 50))
        os.unlink(path)


class TestScreens(unittest.TestCase):
    """screens() reports each bound output's size, scale and name."""

    def test_screens_reports_each_output(self):
        def output(width, height, scale, name):
            out = mock.Mock(width=width, height=height, scale=scale)
            out.name = name  # `name` is special to Mock's constructor, not here
            return out

        conn = mock.Mock(
            _outputs={
                7: output(1280, 800, 2, "DP-1"),
                9: output(1920, 1080, 1, "HDMI-1"),
            }
        )
        backend = CosmicBackend(conn)
        screens = backend.screens()
        self.assertEqual(
            [(s.index, s.width, s.height, s.scale, s.name) for s in screens],
            [(0, 1280, 800, 2, "DP-1"), (1, 1920, 1080, 1, "HDMI-1")],
        )


class TestWindowEvents(unittest.TestCase):
    """window_events() drains the connection's recorded change queue."""

    def test_window_events_yields_queued_changes(self):
        toplevel = mock.Mock(title="Editor", app_id="org.editor", closed=False)
        conn = mock.Mock(
            _events=[("new", 5), ("title", 5), ("close", 5)],
            _toplevels={5: toplevel},
        )
        backend = CosmicBackend(conn)
        events = list(backend.window_events(timeout=0))
        self.assertEqual([e.change for e in events], ["new", "title", "close"])
        self.assertEqual([e.window.title for e in events], ["Editor"] * 3)


class TestWaitForWindow(unittest.TestCase):
    """The existing-window scan `wait_for_window` runs before subscribing."""

    def _backend(self, titles):
        toplevels = {
            handle: mock.Mock(title=title, app_id="org.editor", closed=False)
            for handle, title in titles.items()
        }
        return CosmicBackend(mock.Mock(_events=[], _toplevels=toplevels))

    def test_an_existing_window_is_returned_without_waiting(self):
        backend = self._backend({5: "Editor"})
        self.assertEqual(backend.wait_for_window("Edit", timeout=0).handle, 5)

    def test_the_last_of_several_same_titled_windows_wins(self):
        # Session.find_window is find_windows(...)[-1] over this same
        # windows() list, so the first match is the wrong one: the two calls
        # would name different windows for one title.
        backend = self._backend(
            {5: "Untitled Document", 6: "Calculator", 7: "Untitled Document"}
        )
        found = backend.wait_for_window("Untitled Document", timeout=0)
        self.assertEqual(found.handle, 7)


class TestWindowEventsWire(unittest.TestCase):
    """Changes are recorded without `ext_foreign_toplevel_list_v1.finished`.

    cosmic-comp never sends `finished` (verified live, 2026-10-11), so gating
    event recording on it recorded nothing. The initial listing is instead
    separated from real changes by when they arrive: everything up to the end
    of the handshake is pre-existing state, everything after is an event.
    """

    def test_handshake_discards_the_initial_listing(self):
        backend, _ = _make_backend(self, done_count=0)
        self.assertEqual(backend._conn._events, [])

    def test_changes_after_connect_are_yielded_without_finished(self):
        backend, server = _make_backend(self, done_count=0)
        new_handle = 0xFF000002
        server.sendall(
            _msg(LIST, 0, ("u", new_handle))
            + _msg(new_handle, 2, ("s", "Doc"))
            + _msg(new_handle, 0)  # closed
        )
        events = backend.window_events(timeout=0.5)
        self.assertEqual(next(events).change, "new")
        event = next(events)
        self.assertEqual((event.change, event.window.title), ("title", "Doc"))
        self.assertEqual(next(events).change, "close")


if __name__ == "__main__":
    unittest.main()
