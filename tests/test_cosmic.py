"""The COSMIC window backend, driven over a socketpair against a scripted compositor.

The risk here is the Wayland wire format: a hand-rolled client has no library
to catch a wrong opcode, a wrong word count, or a misaligned string. The fake
compositor below re-encodes its own messages from the same specification, so a
bug in the client's encoder would not be masked by a matching bug in the test
-- the test's bytes are written independently and the client must parse them.
"""

import sys
import unittest

if sys.platform == "win32":  # pragma: no cover - platform guard
    raise unittest.SkipTest(
        "COSMIC's window protocols are spoken over AF_UNIX sockets, which "
        "this platform's socket module does not define"
    )

import socket
import struct
from unittest import mock

from pyguitest.backends.cosmic import (
    CosmicBackend,
    _bgr_rows,
    _CosmicConnection,
    _crop_bgra,
    connect_cosmic,
)
from pyguitest.backends.windows import for_compositor
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, WindowNotFound
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
        self.assertEqual(backend._output_for_region((1280, 10, 100, 100)), 2)
        self.assertEqual(backend._output_for_region((10, 10, 100, 100)), 1)

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

    def test_a_region_spanning_no_single_output_is_refused(self):
        backend, conn = self._backend()
        with self.assertRaises(CapabilityUnsupported):
            backend.capture(region=(1200, 10, 200, 50))
        conn._create_output_source.assert_not_called()

    def test_no_region_still_defaults_to_the_first_output(self):
        backend, conn = self._backend()
        backend.capture()
        conn._create_output_source.assert_called_once_with(1)


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
