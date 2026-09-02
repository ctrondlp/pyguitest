"""PortalBackend against a real RemoteDesktop portal impersonator.

The tests in test_portal.py replace Gio/GLib entirely with pure-Python
stand-ins (see install_fake_gi() there) -- deliberately, since nothing in
any sandbox can click the real consent dialog. That proves the Python-side
call construction and response handling are correct, but never exercises a
byte actually going over D-Bus.

This file closes that gap the way xdg-desktop-portal's own test suite
closes it: python-dbusmock spins up a real, private dbus-daemon that exists
for the lifetime of one test process and nothing else can reach, registers
a fake org.freedesktop.portal.Desktop service on it that answers
CreateSession/SelectDevices/Start the way an already-approved real portal
would, and PortalBackend talks to that over a real Gio connection --
genuine D-Bus method calls, genuine Request/Response signal subscription,
no monkeypatching of gi at all.

This is NOT a way to skip the dialog on a real desktop -- see the
"Avoiding repeat consent dialogs" section of docs/input.md for what that
means and why a fake bus can't provide it. Every bus this file touches is
one it started itself in a private temp directory and tears down
afterward; it never opens whatever $DBUS_SESSION_BUS_ADDRESS already
pointed at, and that value (if any) is restored on teardown so later
tests in the same process are unaffected.

Skips itself wherever it can't run: needs `python-dbusmock` importable,
`dbus-daemon` on PATH, and real PyGObject (not a stub). The skip happens
inside setUpClass via plain unittest.SkipTest, not module-level
pytest.skip()/importorskip() -- the rest of this suite runs under
plain `python3 -m unittest discover` with no dependencies at all (see the
CONTRIBUTING.md), and that runner does not understand
pytest's collection-time skip outcome: it would report a missing
`dbusmock` as a load *error*, not a skip. `pytest` (used for the dev-extra
workflow) understands unittest.SkipTest natively, so this reads as a clean
skip either way.
"""

from __future__ import annotations

import os
import shutil
import unittest

from pyguitest.backends.portal import PortalBackend
from pyguitest.errors import PermissionRequired

try:
    import dbusmock
except ImportError:
    dbusmock = None

_TestCaseBase = dbusmock.DBusTestCase if dbusmock is not None else unittest.TestCase

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"

_SESSION_REQUEST = "/org/freedesktop/portal/desktop/request/mock/create_session"
_DEVICES_REQUEST = "/org/freedesktop/portal/desktop/request/mock/select_devices"
_START_REQUEST = "/org/freedesktop/portal/desktop/request/mock/start"
_SESSION_HANDLE = "/org/freedesktop/portal/desktop/session/mock"


def _respond_code(request_path: str, result_code: int, results: dict) -> str:
    """AddMethod code: reply with a request handle, then answer it shortly after.

    The real portal always answers with just a request handle immediately,
    and only later -- once a human has answered the dialog -- fires
    Response on that handle. Firing Response asynchronously here (via
    GLib.timeout_add, not inline) matters for the same reason: the caller
    does not call signal_subscribe() until *after* the method call
    returns, so an inline emission would race ahead of the subscription
    and the client would hang in GLib.MainLoop().run() forever.

    It is emitted *repeatedly* rather than once for the same reason, one
    step further on. A single emission after a fixed delay only moves the
    race: it assumes the client gets its subscription registered inside
    that window, which is a bet on how busy the machine is. That bet was
    lost on a 2015-era laptop running the full suite -- the 20ms window
    expired before signal_subscribe(), the Response went to nobody, and the
    client sat in run() until its own 60s timeout, turning a green suite
    red for no reason. Re-emitting until the test is over costs nothing:
    the client ignores every Response after the first (see on_response in
    portalrequest.py), and the mock dies with the test.

    The repeat is unbounded rather than a counted retry deliberately -- a
    counter would need a variable, and a `def` here cannot close over one
    (see the paragraph below). setUp spawns a fresh mock per test and
    tearDown stops it, so "forever" is the length of one test.

    Note this only matters because the mock answers on a *fixed* path. A
    real portal derives the request path from the handle_token the caller
    chose, which is why portalrequest.py subscribes to that derived path
    before making the call at all -- the raceless route, and the one
    production actually depends on.

    Every value this generates is baked in as a literal via repr() at
    generation time (this function runs in the test process, not the mock
    process) rather than referenced as a free variable inside `_respond`.
    That sidesteps a real trap: a `def` written inside code that
    `mockobject.py` runs via `exec(code, globals(), loc)` is not a real
    nested-function scope, so it cannot close over a plain local the way a
    function nested inside another function could -- only `objects` and
    other names that are genuinely global in that process are visible
    inside it.
    """
    return f"""
from gi.repository import GLib

def _respond():
    objects[{request_path!r}].EmitSignal(
        {_REQUEST_INTERFACE!r}, "Response", "ua{{sv}}", [{result_code!r}, {results!r}]
    )
    return True  # keep answering; the mock is torn down after each test

GLib.timeout_add(20, _respond)
ret = {request_path!r}
"""


class PortalDBusMockTestCase(_TestCaseBase):
    """Starts a private session bus with a fake RemoteDesktop portal on it."""

    @classmethod
    def setUpClass(cls):
        if dbusmock is None:
            raise unittest.SkipTest(
                "python-dbusmock is not installed (pip install '.[dev]')"
            )
        if shutil.which("dbus-daemon") is None:
            raise unittest.SkipTest(
                "no dbus-daemon on PATH -- cannot start a private bus"
            )
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
        except (ImportError, ValueError) as exc:
            raise unittest.SkipTest(
                f"real PyGObject Gio/GLib not usable: {exc}"
            ) from exc

        cls._saved_bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        cls.start_session_bus()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls._saved_bus_address is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = cls._saved_bus_address

    def setUp(self):
        import dbus  # guaranteed importable: dbusmock declares it as a dependency

        self.mock_process = self.spawn_server(
            _BUS_NAME, _OBJECT_PATH, _INTERFACE, system_bus=False
        )
        self.addCleanup(self._stop_mock)
        self.dbus_con = self.get_dbus()
        self.mock = self.dbus_con.get_object(_BUS_NAME, _OBJECT_PATH)

        # dbus-python cannot guess a signature from an empty {}/[] literal,
        # so these need to be typed explicitly.
        empty_props = dbus.Dictionary({}, signature="sv")
        empty_methods = dbus.Array([], signature="(ssss)")
        for path in (_SESSION_REQUEST, _DEVICES_REQUEST, _START_REQUEST):
            self.mock.AddObject(path, _REQUEST_INTERFACE, empty_props, empty_methods)

    def _stop_mock(self):
        self.mock_process.terminate()
        try:
            self.mock_process.wait(timeout=2)
        except Exception:
            self.mock_process.kill()

    def _wire_step(self, method, in_sig, request_path, code, results):
        self.mock.AddMethod(
            _INTERFACE, method, in_sig, "o", _respond_code(request_path, code, results)
        )

    def _approve_everything(self):
        """The happy path: every step succeeds, like an already-granted session."""
        self._wire_step(
            "CreateSession",
            "a{sv}",
            _SESSION_REQUEST,
            0,
            {"session_handle": _SESSION_HANDLE},
        )
        self._wire_step("SelectDevices", "oa{sv}", _DEVICES_REQUEST, 0, {})
        self._wire_step("Start", "osa{sv}", _START_REQUEST, 0, {})


class TestNegotiationOverRealDBus(PortalDBusMockTestCase):
    def test_successful_negotiation_over_a_real_bus(self):
        self._approve_everything()
        backend = PortalBackend()
        self.assertEqual(backend._session_handle, _SESSION_HANDLE)

    def test_declined_start_raises_permission_required_over_a_real_bus(self):
        self._wire_step(
            "CreateSession",
            "a{sv}",
            _SESSION_REQUEST,
            0,
            {"session_handle": _SESSION_HANDLE},
        )
        self._wire_step("SelectDevices", "oa{sv}", _DEVICES_REQUEST, 0, {})
        self._wire_step("Start", "osa{sv}", _START_REQUEST, 1, {})  # 1 == user declined
        with self.assertRaises(PermissionRequired):
            PortalBackend()
