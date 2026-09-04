"""PortalBackend tests against a stand-in Gio connection.

Not exercised against a real xdg-desktop-portal anywhere -- there is no way
to click the consent dialog Start() raises in this environment. This checks
the Python-side request/response plumbing and call construction: the part a
fake connection can actually stand in for.
"""

import contextlib
import os
import sys
import types
import unittest
import warnings
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PermissionRequired,
    PyGUITestError,
)


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
        # (method, object path), kept alongside `calls` -- which records
        # arguments, not destinations -- so a test can tell which session
        # Session.Close() was sent to.
        self.call_paths = []
        self._watchers = {}
        self.unsubscribed = []
        self.clipboard_content = b""
        self.non_blocking = False
        """Hand SelectionRead's descriptor over in non-blocking mode.

        What GNOME actually did, and what made `read()` answer None."""
        self.offered_mimes = None
        """MIME types SelectionRead will answer, or None for "any".

        A real owner advertises what it feels like: xclip offers only
        `UTF8_STRING`, and the portal answered a request for
        `text/plain;charset=utf-8` against it with a reply carrying no fd
        list at all. Setting this reproduces that shape."""
        self.handed_out = []
        """Every fd this fake gave out, so a test can prove they close."""
        self.written_to = []
        """Read ends of the pipes SelectionWrite handed over."""
        self._own = []
        """The fake's own ends, closed by tearDown rather than by the code."""

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
        self.call_paths.append((method, object_path))
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
        self.unsubscribed.append(subscription_id)

    def call_with_unix_fd_list_sync(
        self,
        bus_name,
        object_path,
        interface,
        method,
        parameters,
        reply_type,
        flags,
        timeout,
        fd_list,
        cancellable,
    ):
        """Answer a `(h)` method with an index into a FakeFDList.

        Mirrors the real thing's two-value return. The index is
        deliberately not the fd number -- reading the int and using it as
        a descriptor is the bug this shape exists to prevent, and a fake
        where the two agreed could not catch it.
        """
        args = parameters.value if parameters is not None else None
        self.calls.append((method, args))
        self.call_paths.append((method, object_path))
        if method == "SelectionRead" and self.offered_mimes is not None:
            requested = args[1] if args else None
            if requested not in self.offered_mimes:
                # No fd list, not an error reply -- what GNOME actually did.
                return FakeReply((0,)), None
        read_fd, write_fd = os.pipe()
        if method == "SelectionRead":
            os.write(write_fd, self.clipboard_content)
            os.close(write_fd)
            if self.non_blocking:
                os.set_blocking(read_fd, False)
            self._own.append(read_fd)
            return FakeReply((7,)), FakeFDList({7: read_fd}, self.handed_out)
        # SelectionWrite: the portal hands us the *write* end to fill.
        self.written_to.append(read_fd)
        self._own.append(write_fd)
        return FakeReply((9,)), FakeFDList({9: write_fd}, self.handed_out)


class FakeFDList:
    """Stands in for Gio.UnixFDList: index -> fd, and `get` dups like the real one.

    The dup matters: `g_unix_fd_list_get()` hands the caller an owned
    descriptor, which is why the backend has to close it. `handed_out`
    records the dup actually given away -- not the fake's own end -- so a
    test can prove that exact descriptor was closed.
    """

    def __init__(self, fds, handed_out):
        self._fds = fds
        self._handed_out = handed_out

    def get(self, index):
        fd = os.dup(self._fds[index])
        self._handed_out.append(fd)
        return fd


class FakeContext:
    """Stands in for GLib.MainContext, recording push/pop balance."""

    def __init__(self):
        self.depth = 0

    def push_thread_default(self):
        self.depth += 1

    def pop_thread_default(self):
        self.depth -= 1


class ServiceLoop:
    """A GLib.MainLoop stand-in that a test drives instead of a thread."""

    def __init__(self, context):
        self._context = context
        self.ran = False
        self.quit_called = False

    def get_context(self):
        return self._context

    def run(self):
        self.ran = True

    def quit(self):
        self.quit_called = True


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
    GLib.VariantType = types.SimpleNamespace(new=lambda spec: spec)
    GLib.MainContext = types.SimpleNamespace(new=FakeContext)
    GLib.MainLoop.new = staticmethod(lambda context, running: ServiceLoop(context))

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

    def test_control_characters_resolve_to_the_key_they_mean(self):
        # Regression: a control character has no keysym in the Latin-1
        # range the fallback covers, so "\n" became 0x01000000 | 10 --
        # the Unicode keysym form of U+000A, which names no key on any
        # keymap. type_text("...\n") therefore did nothing where every
        # caller means Enter. X11Backend and uinput both map it already.
        for char, expected in (
            ("\n", 0xFF0D),  # Return
            ("\r", 0xFF0D),
            ("\t", 0xFF09),  # Tab
            ("\b", 0xFF08),  # BackSpace
            ("\x1b", 0xFF1B),  # Escape
        ):
            with self.subTest(char=char):
                self.connection.calls.clear()
                self.gui.press_key(char)
                _sh, _opts, keysym, _state = self.connection.calls[-1][1]
                self.assertEqual(keysym, expected)

    def test_typing_a_trailing_newline_presses_return(self):
        self.gui.type_text("a\n")
        keysyms = [c[1][2] for c in self.connection.calls]
        self.assertEqual(keysyms, [ord("a"), ord("a"), 0xFF0D, 0xFF0D])

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
        # Negated: the portal follows wl_pointer, where positive scrolls
        # down, and this package settled on X11's reading of `dy > 0` as
        # up. See GUIBackend.scroll.
        self.assertEqual(args[3], -3)

    def test_horizontal_scroll_is_not_negated(self):
        # Positive is right in both conventions; only vertical disagreed.
        self.gui.scroll(dx=2)
        method, args = self.connection.calls[-1]
        self.assertEqual(method, "NotifyPointerAxisDiscrete")
        self.assertEqual(args[2], 1)  # horizontal
        self.assertEqual(args[3], 2)

    def test_scroll_with_nothing_to_do_sends_nothing(self):
        self.gui.scroll()
        self.assertEqual(self.connection.calls, [])


class TestClipboard(unittest.TestCase):
    """The Clipboard portal half, which rides the RemoteDesktop session.

    GNOME has no other clipboard path at all: Mutter implements no
    wlr-data-control, so wl-clipboard cannot serve it. The ordering
    constraint is the whole design -- RequestClipboard must land between
    SelectDevices and Start, because the portal binds access at Start.
    """

    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import portal

        self.module = portal
        self.connection = FakeConnection()
        # The fake keeps its own end of every pipe it makes; nothing in
        # the code under test owns those, so this closes them rather than
        # leaking a pair per test through the whole run.
        self.addCleanup(self._close_fake_fds)

    def _close_fake_fds(self):
        for fd in self.connection._own + self.connection.written_to:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _gui(self, clipboard=True):
        gui = self.module.PortalBackend(
            connection=self.connection,
            session_handle="/session/1",
            clipboard=clipboard,
        )
        self.addCleanup(gui.close)
        return gui

    # -- negotiation ---------------------------------------------------

    def test_request_clipboard_lands_between_select_devices_and_start(self):
        # The portal binds clipboard access at Start, so afterwards is too
        # late and the session comes up without it -- silently.
        self.module.PortalBackend(connection=self.connection, clipboard=True).close()
        methods = [m for m, _ in self.connection.calls]
        self.assertLess(
            methods.index("SelectDevices"), methods.index("RequestClipboard")
        )
        self.assertLess(methods.index("RequestClipboard"), methods.index("Start"))

    def test_request_clipboard_goes_to_the_clipboard_interface(self):
        # A different interface on the same object path; sending it to
        # RemoteDesktop would be an UnknownMethod at negotiation time.
        gui = self.module.PortalBackend(connection=self.connection, clipboard=True)
        self.addCleanup(gui.close)
        self.assertIn("RequestClipboard", [m for m, _ in self.connection.calls])

    def test_clipboard_is_not_requested_by_default(self):
        # It widens what the one consent dialog grants, and a caller
        # injecting input has no use for it.
        self.module.PortalBackend(connection=self.connection).close()
        self.assertNotIn("RequestClipboard", [m for m, _ in self.connection.calls])

    def test_the_capability_follows_what_was_asked_for(self):
        self.assertIn(Capability.CLIPBOARD, self._gui(clipboard=True).capabilities)
        self.assertNotIn(Capability.CLIPBOARD, self._gui(clipboard=False).capabilities)

    def test_clipboard_calls_are_refused_without_the_capability(self):
        gui = self._gui(clipboard=False)
        with self.assertRaises(CapabilityUnsupported):
            gui.get_clipboard()
        with self.assertRaises(CapabilityUnsupported):
            gui.set_clipboard("x")

    # -- read ----------------------------------------------------------

    def test_get_clipboard_reads_the_fd_the_portal_hands_back(self):
        self.connection.clipboard_content = b"hello"
        self.assertEqual(self._gui().get_clipboard(), "hello")

    def test_get_clipboard_asks_for_the_text_mime_type(self):
        gui = self._gui()
        gui.get_clipboard()
        (_method, args) = next(
            c for c in self.connection.calls if c[0] == "SelectionRead"
        )
        self.assertEqual(args, ("/session/1", "text/plain;charset=utf-8"))

    def test_get_clipboard_closes_the_fd_it_was_given(self):
        # The fd is dup'd for us by g_unix_fd_list_get; leaking one per
        # read would exhaust the process over a long run.
        gui = self._gui()
        gui.get_clipboard()
        fd = self.connection.handed_out[-1]
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_a_type_the_owner_does_not_offer_falls_through_to_the_next(self):
        # Measured on GNOME, 2026-09-04: xclip advertises only
        # UTF8_STRING, so asking solely for text/plain;charset=utf-8 read
        # an empty clipboard from a selection that plainly had content.
        self.connection.offered_mimes = {"UTF8_STRING"}
        self.connection.clipboard_content = b"from xclip"
        self.assertEqual(self._gui().get_clipboard(), "from xclip")
        asked = [a[1] for m, a in self.connection.calls if m == "SelectionRead"]
        self.assertEqual(asked[0], "text/plain;charset=utf-8")
        self.assertIn("UTF8_STRING", asked)

    def test_a_non_blocking_descriptor_is_still_read_in_full(self):
        # The bug a live run caught: the portal's pipe can arrive
        # non-blocking, and BufferedReader.read() answers None rather than
        # bytes when nothing is ready yet -- so the old code raised
        # "'NoneType' object has no attribute 'decode'" at the caller.
        self.connection.non_blocking = True
        self.connection.clipboard_content = b"slow owner"
        self.assertEqual(self._gui().get_clipboard(), "slow owner")

    def test_a_writer_that_never_finishes_gives_up_rather_than_hanging(self):
        # The pipe stays open with nothing in it: a real owner that is
        # wedged. Blocking forever inside a test run is the worst of the
        # available failures.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with self.assertRaises(PyGUITestError) as caught:
            self.module._drain(read_fd, timeout=0.05)
        self.assertIn("did not finish writing", str(caught.exception))
        # Closed even on the way out, or a long run leaks one per read.
        with self.assertRaises(OSError):
            os.fstat(read_fd)

    def test_a_reply_with_no_fd_list_raises_a_typed_error(self):
        # It used to read `.get` off None, so the caller got an
        # AttributeError from inside a D-Bus helper -- which says nothing
        # about clipboards and cannot be caught meaningfully.
        self.connection.offered_mimes = set()
        with self.assertRaises(PyGUITestError) as caught:
            self._gui().get_clipboard()
        message = str(caught.exception)
        self.assertIn("could not be read as text", message)
        self.assertIn("text/plain;charset=utf-8", message)

    def test_undecodable_bytes_do_not_raise(self):
        # Another application owns the selection and can put anything on
        # it; a UnicodeDecodeError here would be pyguitest's fault-looking
        # failure for someone else's content.
        self.connection.clipboard_content = b"\xff\xfe"
        self.assertIsInstance(self._gui().get_clipboard(), str)

    # -- PRIMARY -------------------------------------------------------

    def test_primary_is_refused_rather_than_served_from_the_clipboard(self):
        # The interface has no PRIMARY at all. Serving it from the
        # clipboard would answer a different question than was asked, and
        # the two selections are independent by design.
        gui = self._gui()
        with self.assertRaises(CapabilityUnsupported):
            gui.get_clipboard(primary=True)
        with self.assertRaises(CapabilityUnsupported):
            gui.set_clipboard("x", primary=True)

    # -- write ---------------------------------------------------------

    def test_set_clipboard_declares_ownership_of_the_text_type(self):
        gui = self._gui()
        gui.set_clipboard("hello")
        (_method, args) = next(
            c for c in self.connection.calls if c[0] == "SetSelection"
        )
        self.assertEqual(args[0], "/session/1")
        self.assertEqual(args[1]["mime_types"].value, ["text/plain;charset=utf-8"])

    def test_a_transfer_request_is_answered_with_the_content(self):
        # SetSelection carries no content: the portal comes back once per
        # paste, and missing that leaves the clipboard reading as empty.
        gui = self._gui()
        gui.set_clipboard("hello")
        gui._on_transfer(
            None,
            None,
            None,
            None,
            None,
            FakeReply(("/session/1", "text/plain;charset=utf-8", 5)),
        )
        served = self.connection.written_to[-1]
        self.assertEqual(os.read(served, 64), b"hello")
        (_method, args) = next(
            c for c in self.connection.calls if c[0] == "SelectionWriteDone"
        )
        self.assertEqual(args, ("/session/1", 5, True))

    def test_a_transfer_for_another_session_is_ignored(self):
        gui = self._gui()
        gui.set_clipboard("hello")
        before = len(self.connection.calls)
        gui._on_transfer(
            None, None, None, None, None, FakeReply(("/session/other", "text", 5))
        )
        self.assertEqual(len(self.connection.calls), before)

    def test_a_failed_transfer_reports_failure_rather_than_raising(self):
        # This runs on the service thread, where an exception would kill
        # the loop and silently stop the clipboard for the whole session.
        gui = self._gui()
        gui.set_clipboard("hello")
        gui._call_for_fd = mock.Mock(side_effect=RuntimeError("no fd"))
        gui._on_transfer(
            None, None, None, None, None, FakeReply(("/session/1", "text", 5))
        )
        (_method, args) = next(
            c for c in self.connection.calls if c[0] == "SelectionWriteDone"
        )
        self.assertIs(args[2], False)

    def test_the_service_starts_once_however_often_the_clipboard_is_set(self):
        gui = self._gui()
        gui.set_clipboard("one")
        loop = gui._loop
        gui.set_clipboard("two")
        self.assertIs(gui._loop, loop)

    def test_setting_again_replaces_what_is_served(self):
        gui = self._gui()
        gui.set_clipboard("one")
        gui.set_clipboard("two")
        gui._on_transfer(
            None, None, None, None, None, FakeReply(("/session/1", "text", 1))
        )
        self.assertEqual(os.read(self.connection.written_to[-1], 64), b"two")

    def test_close_stops_the_service_before_ending_the_session(self):
        # A transfer racing the Close would address a dead session.
        gui = self.module.PortalBackend(
            connection=self.connection, session_handle="/session/1", clipboard=True
        )
        gui.set_clipboard("hello")
        loop = gui._loop
        gui.close()
        self.assertTrue(loop.quit_called)
        self.assertTrue(self.connection.unsubscribed)
        self.assertFalse(gui._serving)
        self.assertIsNone(gui._transfer_subscription)

    def test_close_is_safe_when_the_clipboard_was_never_used(self):
        self._gui().close()  # addCleanup closes it a second time too


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


def _closed_sessions(connection):
    """Object paths Session.Close() was sent to, in order."""
    return [path for method, path in connection.call_paths if method == "Close"]


class TestSessionCleanup(unittest.TestCase):
    """A session created by CreateSession must not outlive a failure.

    It lives in xdg-desktop-portal, and past CreateSession nothing else can
    end it: __init__ raises instead of returning the object whose close()
    would, and the session survives on the shared session-bus connection.
    Left open, it is a standing input grant belonging to nobody.
    """

    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, FakeMainLoop, "pending_timeout", None)
        from pyguitest.backends import portal

        self.module = portal

    def test_a_declined_start_closes_the_session(self):
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (1, {}),  # 1 == user cancelled, per the portal spec
            }
        )
        with self.assertRaises(PermissionRequired):
            self.module.PortalBackend(connection=connection)
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_a_declined_select_devices_closes_the_session(self):
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (1, {}),
            }
        )
        with self.assertRaises(PermissionRequired):
            self.module.PortalBackend(connection=connection)
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_a_request_that_is_never_answered_closes_the_session(self):
        from pyguitest.errors import PortalTimeout

        class SilentAfterCreateConnection(FakeConnection):
            """Answers up to Start, which it accepts and never responds to."""

            def call_sync(self, *args, **kwargs):
                method = args[3]
                if method != "Start":
                    return super().call_sync(*args, **kwargs)
                self.calls.append((method, args[4].value if args[4] else None))
                self.call_paths.append((method, args[1]))
                return FakeReply(("/request/never-answered",))

        connection = SilentAfterCreateConnection()
        with self.assertRaises(PortalTimeout):
            self.module.PortalBackend(connection=connection)
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_an_interrupted_consent_dialog_closes_the_session(self):
        # Why the cleanup catches BaseException: Start blocks on a human,
        # so Ctrl-C during that wait is a routine way out of __init__ --
        # and it strands an approved session exactly as a decline does.
        class InterruptedConnection(FakeConnection):
            def call_sync(self, *args, **kwargs):
                if args[3] == "Start":
                    raise KeyboardInterrupt
                return super().call_sync(*args, **kwargs)

        connection = InterruptedConnection()
        with self.assertRaises(KeyboardInterrupt):
            self.module.PortalBackend(connection=connection)
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_a_successful_negotiation_closes_nothing(self):
        # The other half of it: a session that negotiated fine belongs to
        # the caller until they close it.
        connection = FakeConnection()
        self.module.PortalBackend(connection=connection)
        self.assertEqual(_closed_sessions(connection), [])


class TestClose(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import portal

        self.module = portal

    def test_close_ends_the_negotiated_session(self):
        # Without this the session outlives the backend as a standing input
        # grant: GLib's shared session-bus connection does not drop with it.
        connection = FakeConnection()
        gui = self.module.PortalBackend(connection=connection)
        gui.close()
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_close_is_idempotent(self):
        connection = FakeConnection()
        gui = self.module.PortalBackend(connection=connection)
        gui.close()
        gui.close()
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_the_context_manager_closes_on_exit(self):
        connection = FakeConnection()
        with self.module.PortalBackend(connection=connection):
            self.assertEqual(_closed_sessions(connection), [])
        self.assertEqual(_closed_sessions(connection), ["/session/1"])

    def test_close_leaves_an_injected_session_alone(self):
        # An injected handle belongs to whoever injected it -- closing it
        # would end a session this backend was only borrowing.
        connection = FakeConnection()
        gui = self.module.PortalBackend(
            connection=connection, session_handle="/session/borrowed"
        )
        gui.close()
        self.assertEqual(_closed_sessions(connection), [])

    def test_injecting_after_close_raises(self):
        # Rather than sending Notify* calls at a session the portal has
        # already dropped, or building a variant around a None handle.
        from pyguitest.errors import PyGUITestError

        connection = FakeConnection()
        gui = self.module.PortalBackend(connection=connection)
        gui.close()
        with self.assertRaises(PyGUITestError) as ctx:
            gui.press_key("a")
        self.assertIn("closed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
