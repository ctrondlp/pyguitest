"""PortalBackend tests against a stand-in Gio connection.

Not exercised against a real xdg-desktop-portal anywhere -- there is no way
to click the consent dialog Start() raises in this environment. This checks
the Python-side request/response plumbing and call construction: the part a
fake connection can actually stand in for.
"""

import sys
import types
import unittest
import warnings
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import BackendUnavailable, PermissionRequired


class FakeVariant:
    """Stands in for GLib.Variant: just remembers what it was built from."""

    def __init__(self, signature, value):
        self.signature = signature
        self.value = value


class FakeReply:
    """Stands in for the GVariant call_sync()/a Response signal carries."""

    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeMainLoop:
    """Stands in for GLib.MainLoop.

    quit() is normally called synchronously, from within signal_subscribe,
    before run() is reached in these tests -- there is no real event loop
    backing this, so run() never actually has anything to wait for.

    `pending_timeout` covers the one case that is not like that: a request
    nobody ever answers. Such a loop fires whatever
    `GLib.timeout_add_seconds` registered, standing in for real GLib firing
    the source once the clock passes it -- which is how a test can tell
    "would have hung forever" from "timed out cleanly".
    """

    pending_timeout = None

    def __init__(self):
        self._quit = False

    def run(self):
        if self._quit:
            return
        callback = FakeMainLoop.pending_timeout
        if callback is not None:
            FakeMainLoop.pending_timeout = None
            callback()
            return
        raise AssertionError("run() called before a synchronous quit()")

    def quit(self):
        self._quit = True


class FakeConnection:
    """Stands in for a Gio.DBusConnection bound to the session bus.

    PortalBackend subscribes to the Response signal *before* issuing the
    call that returns the handle (see `_request` -- doing it the other way
    round is a real race), so this fake has to work in that order too:
    signal_subscribe only records the callback, and call_sync is what
    "fires" the canned response against whichever paths are being watched.

    It deliberately returns a handle that does NOT match the token-derived
    path, mirroring tests/test_portal_dbusmock.py's fake portal, so the
    fallback subscription on the returned handle stays exercised.
    """

    def __init__(self, responses=None):
        self.responses = responses or {
            "CreateSession": (0, {"session_handle": "/session/1"}),
            "SelectDevices": (0, {}),
            "Start": (0, {}),
        }
        self.calls = []
        self._watchers = {}

    def get_unique_name(self):
        return ":1.42"

    def call_sync(
        self,
        bus_name,
        object_path,
        interface,
        method,
        parameters,
        reply_type,
        flags,
        timeout,
        cancellable,
    ):
        args = parameters.value if parameters is not None else None
        self.calls.append((method, args))
        if method not in self.responses:
            return FakeReply(())
        handle = f"/request/{method}"
        code, results = self.responses[method]
        callback = self._watchers.get(handle)
        if callback is not None:
            callback(None, None, handle, None, "Response", FakeReply((code, results)))
        else:
            # Nothing is watching the returned handle yet -- _request
            # subscribes to it right after this returns, so stash the
            # reply and let that subscription collect it.
            self._pending_reply = (handle, code, results)
        return FakeReply((handle,))

    def signal_subscribe(
        self, bus_name, iface, signal, path, arg0, flags, callback, user_data
    ):
        self._watchers[path] = callback
        pending = getattr(self, "_pending_reply", None)
        if pending is not None and pending[0] == path:
            self._pending_reply = None
            callback(
                None, None, path, iface, signal, FakeReply((pending[1], pending[2]))
            )
        return len(self._watchers)

    def signal_unsubscribe(self, subscription_id):
        pass


def install_fake_gi():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **kw: None
    repository = types.ModuleType("gi.repository")

    Gio = types.ModuleType("gi.repository.Gio")
    Gio.BusType = types.SimpleNamespace(SESSION=1)
    Gio.DBusCallFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusSignalFlags = types.SimpleNamespace(NONE=0)
    Gio.bus_get_sync = lambda *a, **kw: FakeConnection()

    GLib = types.ModuleType("gi.repository.GLib")
    GLib.Variant = FakeVariant
    GLib.MainLoop = FakeMainLoop

    # timeout_add_seconds stashes the callback rather than scheduling it;
    # only a test that never quit()s the loop lets it fire.
    def timeout_add_seconds(_seconds, callback):
        FakeMainLoop.pending_timeout = callback
        return 1

    def source_remove(_source_id):
        # Mirrors real GLib: a removed source cannot fire afterwards, which
        # also stops a stashed callback leaking between tests, since
        # request() always removes in a finally.
        FakeMainLoop.pending_timeout = None

    GLib.timeout_add_seconds = timeout_add_seconds
    GLib.source_remove = source_remove

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


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import portal

        self.module = portal
        self.connection = FakeConnection()
        # session_handle injected directly: skips negotiation (and so the
        # consent dialog) for every test except the ones about negotiation
        # itself, below.
        self.gui = portal.PortalBackend(
            connection=self.connection, session_handle="/session/1"
        )


class TestAvailability(PortalTestCase):
    def test_available_when_gi_imports(self):
        self.assertTrue(self.module.available())


class TestCapabilities(PortalTestCase):
    def test_capabilities_are_keyboard_and_pointer_buttons_only(self):
        caps = self.gui.capabilities
        for cap in (
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
        ):
            with self.subTest(cap=cap):
                self.assertIn(cap, caps)

    def test_no_pointer_move_capability(self):
        # Deliberate: absolute positioning needs a ScreenCast stream id this
        # backend does not negotiate. See the module docstring.
        self.assertNotIn(Capability.POINTER_MOVE, self.gui.capabilities)


class TestKeyboard(PortalTestCase):
    def test_press_and_release_key_send_keysyms(self):
        self.gui.press_key("a")
        self.gui.release_key("a")
        method, args = self.connection.calls[0]
        self.assertEqual(method, "NotifyKeyboardKeysym")
        session_handle, options, keysym, state = args
        self.assertEqual(session_handle, "/session/1")
        self.assertEqual(keysym, ord("a"))
        self.assertEqual(state, 1)
        _sh, _opts, _ks, release_state = self.connection.calls[1][1]
        self.assertEqual(release_state, 0)

    def test_named_keys_use_the_verified_keysym_table(self):
        self.gui.press_key("Return")
        _sh, _opts, keysym, _state = self.connection.calls[-1][1]
        self.assertEqual(keysym, 0xFF0D)

    def test_shift_l_matches_x11_keysym_value(self):
        # Cross-check against the same name GUIBackend.MODIFIER_KEYS uses by
        # default, so send_keys() works on this backend with no override.
        self.gui.press_key("Shift_L")
        _sh, _opts, keysym, _state = self.connection.calls[-1][1]
        self.assertEqual(keysym, 0xFFE1)

    def test_unknown_key_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_key("nonexistent")

    def test_type_text_sends_press_then_release_per_character(self):
        self.gui.type_text("ab")
        methods = [c[0] for c in self.connection.calls]
        self.assertEqual(methods, ["NotifyKeyboardKeysym"] * 4)
        states = [c[1][3] for c in self.connection.calls]
        self.assertEqual(states, [1, 0, 1, 0])

    def test_type_text_does_not_warn_about_keymap_safety(self):
        # Unlike uinput/ydotool: keysym injection is keymap-safe by
        # construction, so there is nothing to warn about here.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.gui.type_text("hello")


class TestPointer(PortalTestCase):
    def test_press_button_uses_evdev_codes(self):
        self.gui.press_button(1)
        _sh, _opts, code, state = self.connection.calls[-1][1]
        self.assertEqual(code, 0x110)  # BTN_LEFT
        self.assertEqual(state, 1)

    def test_all_three_buttons_map_correctly(self):
        self.gui.press_button(2)
        self.assertEqual(self.connection.calls[-1][1][2], 0x112)  # BTN_MIDDLE
        self.gui.press_button(3)
        self.assertEqual(self.connection.calls[-1][1][2], 0x111)  # BTN_RIGHT

    def test_unsupported_button_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_button(9)

    def test_scroll_sends_discrete_axis_events(self):
        self.gui.scroll(dy=3)
        method, args = self.connection.calls[-1]
        self.assertEqual(method, "NotifyPointerAxisDiscrete")
        self.assertEqual(args[2], 0)  # vertical
        self.assertEqual(args[3], 3)

    def test_scroll_with_nothing_to_do_sends_nothing(self):
        self.gui.scroll()
        self.assertEqual(self.connection.calls, [])


class TestNegotiation(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import portal

        self.module = portal

    def test_successful_negotiation_calls_all_three_steps_in_order(self):
        connection = FakeConnection()
        gui = self.module.PortalBackend(connection=connection)
        self.assertEqual(gui._session_handle, "/session/1")
        methods = [c[0] for c in connection.calls]
        self.assertEqual(methods, ["CreateSession", "SelectDevices", "Start"])

    def test_every_request_carries_a_handle_token(self):
        # The token is what makes the request path predictable, which is
        # what lets _request subscribe before calling. Without it the
        # subscription cannot be set up in advance at all.
        connection = FakeConnection()
        self.module.PortalBackend(connection=connection)
        for method, args in connection.calls:
            with self.subTest(method=method):
                options = args[-1]
                self.assertIn("handle_token", options)

    def test_subscription_happens_before_the_call_that_returns_the_handle(self):
        # Regression for a real race: subscribing afterwards can miss a
        # fast, non-interactive Response and then block forever. Proven by
        # a connection that answers *only* the pre-subscribed path and
        # never reveals a usable handle.
        class OnlyAnswersPreSubscribed(FakeConnection):
            def call_sync(self, *a, **kw):
                method = a[3]
                self.calls.append((method, a[4].value if a[4] else None))
                if method not in self.responses:
                    return FakeReply(())
                code, results = self.responses[method]
                token = a[4].value[-1]["handle_token"].value
                path = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
                callback = self._watchers.get(path)
                self.assertIsNotNone(
                    callback, f"{method} was called before anything watched {path}"
                )
                callback(None, None, path, None, "Response", FakeReply((code, results)))
                return FakeReply(("/handle/never-watched",))

        connection = OnlyAnswersPreSubscribed()
        connection.assertIsNotNone = self.assertIsNotNone
        gui = self.module.PortalBackend(connection=connection)
        self.assertEqual(gui._session_handle, "/session/1")

    def test_declined_consent_raises_permission_required(self):
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (1, {}),  # 1 == user cancelled, per the portal spec
            }
        )
        with self.assertRaises(PermissionRequired):
            self.module.PortalBackend(connection=connection)

    def test_no_persist_options_are_sent_by_default(self):
        # Persistence is opt-in: a plain session must not quietly ask the
        # portal to remember a standing injection grant.
        connection = FakeConnection()
        gui = self.module.PortalBackend(connection=connection)
        select = next(c for c in connection.calls if c[0] == "SelectDevices")
        options = select[1][-1]
        self.assertNotIn("persist_mode", options)
        self.assertNotIn("restore_token", options)
        self.assertIsNone(gui.restore_token)

    def test_persist_mode_is_forwarded_to_select_devices(self):
        connection = FakeConnection()
        self.module.PortalBackend(
            connection=connection,
            persist_mode=self.module.PERSIST_UNTIL_REVOKED,
        )
        select = next(c for c in connection.calls if c[0] == "SelectDevices")
        self.assertEqual(select[1][-1]["persist_mode"].value, 2)

    def test_restore_token_is_forwarded_to_select_devices(self):
        connection = FakeConnection()
        self.module.PortalBackend(connection=connection, restore_token="tok-abc")
        select = next(c for c in connection.calls if c[0] == "SelectDevices")
        self.assertEqual(select[1][-1]["restore_token"].value, "tok-abc")

    def test_a_new_restore_token_is_exposed_after_start(self):
        # The caller must save this one in place of the old: a portal may
        # answer with a different token, and reusing the original would
        # eventually present a stale one.
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (0, {"restore_token": "tok-next"}),
            }
        )
        gui = self.module.PortalBackend(
            connection=connection,
            persist_mode=self.module.PERSIST_UNTIL_REVOKED,
        )
        self.assertEqual(gui.restore_token, "tok-next")

    def test_nothing_is_written_to_disk(self):
        # The token is a credential; storing it is the caller's decision.
        # Guards against a future convenience-cache creeping in here.
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (0, {"restore_token": "tok-secret"}),
            }
        )
        with mock.patch("builtins.open", side_effect=AssertionError("wrote a file")):
            gui = self.module.PortalBackend(
                connection=connection,
                persist_mode=self.module.PERSIST_UNTIL_REVOKED,
            )
        self.assertEqual(gui.restore_token, "tok-secret")

    def test_missing_pygobject_refuses_with_an_install_hint(self):
        with mock.patch.dict(sys.modules, {"gi": None}):
            with self.assertRaises(BackendUnavailable) as ctx:
                self.module.PortalBackend()
            self.assertIn("PyGObject", str(ctx.exception))


class TestRequestTimeout(unittest.TestCase):
    """A portal that accepts a call and then never answers must not hang.

    This is the failure an unbounded `loop.run()` cannot recover from: no
    Response, no error, no fd to poll, and nothing to interrupt the wait.
    """

    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, FakeMainLoop, "pending_timeout", None)
        from pyguitest.backends import portalrequest

        self.portalrequest = portalrequest

    def _silent_connection(self):
        """A connection whose calls are accepted but never answered."""

        class SilentConnection(FakeConnection):
            def call_sync(self, *args, **kwargs):
                # Returns the handle, as a real portal does, but fires no
                # Response against any subscription.
                return FakeReply(("/org/freedesktop/portal/desktop/request/x/y",))

        return SilentConnection()

    def test_a_request_that_is_never_answered_times_out(self):
        from pyguitest.errors import PortalTimeout

        connection = self._silent_connection()
        modules = (
            sys.modules["gi.repository"].Gio,
            sys.modules["gi.repository"].GLib,
        )
        with self.assertRaises(PortalTimeout) as ctx:
            self.portalrequest.request(
                modules,
                connection,
                "org.freedesktop.portal.RemoteDesktop",
                "Start",
                "(oa{sv})",
                ("/session/1", {}),
                timeout=1,
            )
        self.assertEqual(ctx.exception.method, "Start")
        self.assertEqual(ctx.exception.timeout, 1)

    def test_timeout_none_still_waits_indefinitely(self):
        # The old unconditional behaviour stays reachable for a caller that
        # genuinely wants it -- with no timeout source registered, the fake
        # loop has nothing to fire and reports the would-be hang.
        connection = self._silent_connection()
        modules = (
            sys.modules["gi.repository"].Gio,
            sys.modules["gi.repository"].GLib,
        )
        with self.assertRaises(AssertionError) as ctx:
            self.portalrequest.request(
                modules,
                connection,
                "org.freedesktop.portal.RemoteDesktop",
                "Start",
                "(oa{sv})",
                ("/session/1", {}),
                timeout=None,
            )
        self.assertIn("run() called before a synchronous quit()", str(ctx.exception))

    def test_a_normal_answer_is_unaffected_by_the_timeout(self):
        # The timeout must not disturb the ordinary path: the canned
        # response still arrives, and the source is removed rather than
        # left live to fire into a later request's loop.
        connection = FakeConnection(responses={"Start": (0, {"devices": 2})})
        modules = (
            sys.modules["gi.repository"].Gio,
            sys.modules["gi.repository"].GLib,
        )
        code, results = self.portalrequest.request(
            modules,
            connection,
            "org.freedesktop.portal.RemoteDesktop",
            "Start",
            "(oa{sv})",
            ("/session/1", {}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(results, {"devices": 2})
        self.assertIsNone(FakeMainLoop.pending_timeout)


if __name__ == "__main__":
    unittest.main()
