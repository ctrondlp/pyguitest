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

from pyguitest.backends.cosmic import CosmicBackend, _CosmicConnection, connect_cosmic
from pyguitest.backends.windows import for_compositor
from pyguitest.errors import WindowNotFound
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


if __name__ == "__main__":
    unittest.main()
