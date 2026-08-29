"""LibeiBackend tests against stand-in Gio and ei modules.

Not exercised against real libei or a real portal anywhere in this file --
see test_eiinput_libei.py for the real-libei round-trip (via libei.eis's fake
compositor) and the module docstring in eiinput.py for what has and hasn't
been verified live. This file proves LibeiBackend's own orchestration: the
RemoteDesktop negotiation sequence and its raceless Request/Response
handling (Gio fakes, mirroring test_portal.py's install_fake_gi()), and the
post-connection device/event handling (ei fakes) -- including the
two-devices-per-seat race that made move_mouse silently do nothing.
"""

from __future__ import annotations

import enum
import sys
import types
import unittest
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    PermissionRequired,
    PyGUITestError,
)

# -- ei-side fakes: SEAT_ADDED/DEVICE_RESUMED negotiation, and the device
# itself once negotiated. -----------------------------------------------


class FakeDeviceCapability(enum.IntFlag):
    POINTER = 1 << 0
    POINTER_ABSOLUTE = 1 << 1
    KEYBOARD = 1 << 2
    TOUCH = 1 << 3
    SCROLL = 1 << 4
    BUTTON = 1 << 5
    TEXT = 1 << 6


def _capability_tuple(caps):
    """Normalise a capability set the way real python-libei reports one.

    libei's `Seat.capabilities` and `Device.capabilities` are both typed
    `tuple[DeviceCapability, ...]` -- a tuple of individual flags, not a
    single combined Flag value. Tests find it far more readable to write
    `POINTER_ABSOLUTE | BUTTON`, so this converts.

    It is not cosmetic. Storing the combined Flag instead made the fake
    iterable only on Python 3.11+, where iterating a Flag *member* was
    introduced; on 3.10 the same code raises "object is not iterable", and
    the production loop over `seat.capabilities` blew up on CI's oldest
    matrix job while passing everywhere else. The fake diverged from the
    API it stands in for, and a newer interpreter concealed it.

    Iterating the enum *class* works on every supported version; only
    iterating a member does not.
    """
    if isinstance(caps, FakeDeviceCapability):
        return tuple(c for c in FakeDeviceCapability if caps & c)
    return tuple(caps)


class FakeEventType(enum.IntEnum):
    SEAT_ADDED = 1
    DEVICE_ADDED = 2
    DEVICE_RESUMED = 3


class FakeEvent:
    def __init__(self, event_type, seat=None, device=None):
        self.event_type = event_type
        self.seat = seat
        self.device = device


class FakeDevice:
    """Records every call, chaining like the real Device does."""

    def __init__(
        self,
        capabilities=(
            FakeDeviceCapability.POINTER_ABSOLUTE
            | FakeDeviceCapability.BUTTON
            | FakeDeviceCapability.SCROLL
        ),
    ):
        self.calls = []
        self.capabilities = _capability_tuple(capabilities)

    def start_emulating(self):
        self.calls.append(("start_emulating",))
        return self

    def stop_emulating(self):
        self.calls.append(("stop_emulating",))
        return self

    def frame(self):
        self.calls.append(("frame",))
        return self

    def pointer_motion_absolute(self, x, y):
        self.calls.append(("pointer_motion_absolute", x, y))
        return self

    def button(self, code, is_press):
        self.calls.append(("button", code, is_press))
        return self

    def scroll_delta(self, dx, dy):
        self.calls.append(("scroll_delta", dx, dy))
        return self

    def keyboard_key(self, keycode, is_press):
        self.calls.append(("keyboard_key", keycode, is_press))
        return self


class FakeKeymap:
    """Stands in for xkb.Keymap with a tiny, explicit table."""

    TABLE = {
        "a": (30, ()),  # KEY_A
        "A": (30, (42,)),  # KEY_A + KEY_LEFTSHIFT
        "!": (2, (42,)),  # KEY_1 + KEY_LEFTSHIFT
    }
    NAMES = {"Return": (28, ()), "Shift_L": (42, ())}

    def __init__(self):
        self.closed = False

    def for_char(self, char):
        return self.TABLE.get(char)

    def for_name(self, name):
        return self.NAMES.get(name)

    def close(self):
        self.closed = True


class FakeSeat:
    def __init__(self, offered):
        self.capabilities = _capability_tuple(offered)
        self.bound = None

    def bind(self, caps):
        if not caps:
            raise ValueError("bind() needs at least one capability")
        self.bound = caps


class FakeSender:
    """Yields one scripted batch of events per `events` access."""

    def __init__(self, event_batches=(), fd=99):
        self.fd = fd
        self._batches = list(event_batches)

    def dispatch(self):
        pass

    @property
    def events(self):
        if self._batches:
            return self._batches.pop(0)
        return []

    @classmethod
    def create_for_fd(cls, fd, name=None):
        return cls(fd=fd)


def install_fake_ei(sender=None):
    ei = types.ModuleType("libei.ei")
    ei.DeviceCapability = FakeDeviceCapability
    ei.EventType = FakeEventType
    ei.Sender = sender if sender is not None else FakeSender
    ei.is_available = lambda: True

    libei_pkg = types.ModuleType("libei")
    libei_pkg.ei = ei

    return mock.patch.dict(sys.modules, {"libei": libei_pkg, "libei.ei": ei})


def _always_ready():
    """Patch select.select to report the fd immediately readable.

    Fake fds here are plain ints, not real sockets -- a real select() call
    would raise. Negotiation tests patch this so timing is governed by the
    (also patched, small) timeout constants.
    """
    return mock.patch(
        "pyguitest.backends.eiinput.select.select", return_value=([1], [], [])
    )


# -- Gio-side fakes: the RemoteDesktop portal negotiation. --------------


class FakeVariant:
    def __init__(self, signature, value):
        self.signature = signature
        self.value = value


class FakeReply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeUnixFDList:
    def __init__(self, fd):
        self._fd = fd

    def get(self, _index):
        return self._fd


class FakeMainLoop:
    """quit() is always called synchronously, before run() is reached."""

    def __init__(self):
        self._quit = False

    def run(self):
        if not self._quit:
            raise AssertionError("run() called before a synchronous quit()")

    def quit(self):
        self._quit = True


class FakeConnection:
    """Stands in for a Gio.DBusConnection bound to the session bus.

    Unlike test_portal.py's FakeConnection, signal_subscribe happens
    *before* call_sync here (the raceless pattern eiinput.py uses) -- so
    call_sync itself must compute the request path from the handle_token in
    its own parameters and fire whichever subscription is already
    registered for it, rather than the other way around.
    """

    def __init__(
        self, responses=None, version=2, fd_responses=None, unique_name=":1.99"
    ):
        self.responses = responses or {
            "CreateSession": (0, {"session_handle": "/session/1"}),
            "SelectDevices": (0, {}),
            "Start": (0, {"devices": 2}),
        }
        self.version = version
        self.fd_responses = fd_responses or {"ConnectToEIS": 12}
        self.calls = []
        self._unique_name = unique_name
        self._subscriptions = {}

    def get_unique_name(self):
        return self._unique_name

    def _escaped_sender(self):
        return self._unique_name[1:].replace(".", "_")

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
        self.calls.append((method, parameters.value))
        if method == "Get":
            return FakeReply((self.version,))
        *_leading, options = parameters.value
        token = options["handle_token"].value
        path = (
            f"/org/freedesktop/portal/desktop/request/{self._escaped_sender()}/{token}"
        )
        code, results = self.responses.get(method, (0, {}))
        callback = self._subscriptions.get(path)
        if callback is not None:
            callback(None, None, path, None, "Response", FakeReply((code, results)))
        return FakeReply(())

    def signal_subscribe(
        self, bus_name, iface, signal, path, arg0, flags, callback, user_data
    ):
        self._subscriptions[path] = callback
        return 1

    def signal_unsubscribe(self, subscription_id):
        pass

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
        self.calls.append((method, parameters.value))
        return FakeReply((0,)), FakeUnixFDList(self.fd_responses[method])


def install_fake_gi(connection=None):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **kw: None
    repository = types.ModuleType("gi.repository")

    Gio = types.ModuleType("gi.repository.Gio")
    Gio.BusType = types.SimpleNamespace(SESSION=1)
    Gio.DBusCallFlags = types.SimpleNamespace(NONE=0)
    Gio.DBusSignalFlags = types.SimpleNamespace(NONE=0)
    Gio.bus_get_sync = lambda *a, **kw: connection or FakeConnection()

    GLib = types.ModuleType("gi.repository.GLib")
    GLib.Variant = FakeVariant
    GLib.MainLoop = FakeMainLoop
    GLib.VariantType = types.SimpleNamespace(new=lambda sig: sig)

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


class LibeiTestCase(unittest.TestCase):
    """Installs both fakes and injects a ready-made device, skipping negotiation."""

    def setUp(self):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        gi_patcher = install_fake_gi()
        gi_patcher.start()
        self.addCleanup(gi_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        self.device = FakeDevice()
        self.gui = LibeiBackend(device=self.device)


class TestAvailability(unittest.TestCase):
    def test_available_when_both_libraries_import(self):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        gi_patcher = install_fake_gi()
        gi_patcher.start()
        self.addCleanup(gi_patcher.stop)
        from pyguitest.backends import eiinput

        self.assertTrue(eiinput.available())

    def test_missing_libei_refuses_with_an_install_hint(self):
        gi_patcher = install_fake_gi()
        gi_patcher.start()
        self.addCleanup(gi_patcher.stop)
        with mock.patch.dict(sys.modules, {"libei": None}):
            from pyguitest.backends.eiinput import LibeiBackend

            with self.assertRaises(BackendUnavailable) as ctx:
                LibeiBackend()
            self.assertIn("libei", str(ctx.exception))

    def test_missing_pygobject_refuses_with_an_install_hint(self):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        with mock.patch.dict(sys.modules, {"gi": None}):
            from pyguitest.backends.eiinput import LibeiBackend

            with self.assertRaises(BackendUnavailable) as ctx:
                LibeiBackend()
            self.assertIn("PyGObject", str(ctx.exception))


class TestCapabilities(LibeiTestCase):
    def test_derives_from_the_real_device_not_a_hardcoded_set(self):
        # Regression: a hardcoded capability set is exactly how the
        # ScreenCast-linkage bug (see eiinput.py's module docstring) went
        # unnoticed -- this device only has BUTTON/SCROLL, no
        # POINTER_ABSOLUTE, so POINTER_MOVE must not be claimed.
        device = FakeDevice(
            capabilities=FakeDeviceCapability.BUTTON | FakeDeviceCapability.SCROLL
        )
        gui = type(self.gui)(device=device)
        self.assertNotIn(Capability.POINTER_MOVE, gui.capabilities)
        self.assertIn(Capability.POINTER_BUTTON, gui.capabilities)
        self.assertIn(Capability.POINTER_SCROLL, gui.capabilities)

    def test_pointer_capabilities_when_the_device_has_them_all(self):
        caps = self.gui.capabilities
        for cap in (
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
        ):
            with self.subTest(cap=cap):
                self.assertIn(cap, caps)

    def test_no_keyboard_capability(self):
        self.assertNotIn(Capability.KEY_EVENT, self.gui.capabilities)
        self.assertNotIn(Capability.TEXT_ENTRY, self.gui.capabilities)


class TestPointer(LibeiTestCase):
    def test_move_mouse_sends_absolute_motion(self):
        self.gui.move_mouse(960, 540)
        self.assertEqual(
            self.device.calls,
            [
                ("start_emulating",),
                ("pointer_motion_absolute", 960, 540),
                ("frame",),
            ],
        )

    def test_one_emulation_sequence_is_held_across_calls(self):
        # Regression: a start/stop around every event still delivers it --
        # apps highlight on hover -- but the visible cursor snaps back the
        # moment the sequence ends, so nothing appears to move. See
        # _emulating() in eiinput.py.
        self.gui.move_mouse(10, 20)
        self.gui.press_button(1)
        self.gui.scroll(dy=1)
        self.assertEqual([c[0] for c in self.device.calls].count("start_emulating"), 1)
        self.assertNotIn("stop_emulating", [c[0] for c in self.device.calls])

    def test_close_ends_the_emulation_sequence(self):
        self.gui.move_mouse(10, 20)
        self.gui.close()
        self.assertEqual(self.device.calls[-1], ("stop_emulating",))

    def test_close_without_any_emulation_does_not_stop(self):
        self.gui.close()
        self.assertEqual(self.device.calls, [])

    def test_move_mouse_without_absolute_capability_raises(self):
        from pyguitest.errors import CapabilityUnsupported

        device = FakeDevice(capabilities=FakeDeviceCapability.BUTTON)
        gui = type(self.gui)(device=device)
        with self.assertRaises(CapabilityUnsupported):
            gui.move_mouse(0, 0)

    def test_press_button_uses_evdev_codes(self):
        self.gui.press_button(1)
        self.assertIn(("button", 0x110, True), self.device.calls)  # BTN_LEFT

    def test_all_three_buttons_map_correctly(self):
        self.gui.press_button(2)
        self.assertIn(("button", 0x112, True), self.device.calls)  # BTN_MIDDLE
        self.gui.release_button(3)
        self.assertIn(("button", 0x111, False), self.device.calls)  # BTN_RIGHT

    def test_unsupported_button_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_button(9)

    def test_scroll_sends_a_delta(self):
        self.gui.scroll(dx=1, dy=3)
        self.assertIn(("scroll_delta", 1, 3), self.device.calls)

    def test_scroll_with_nothing_to_do_sends_nothing(self):
        self.gui.scroll()
        self.assertEqual(self.device.calls, [])


class TestKeyboard(unittest.TestCase):
    """Typing goes through the compositor's keymap, or is not offered."""

    _DEFAULT = object()

    def _gui(self, keymap=None, keyboard=_DEFAULT):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        gi_patcher = install_fake_gi()
        gi_patcher.start()
        self.addCleanup(gi_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        self.keyboard = FakeDevice() if keyboard is self._DEFAULT else keyboard
        return LibeiBackend(device=FakeDevice(), keyboard=self.keyboard, keymap=keymap)

    def test_no_keyboard_capability_without_a_keymap(self):
        # A keyboard device with no keymap could still press keycodes, but
        # which characters they produce would be a guess -- uinput's exact
        # failure mode. Offering nothing is the correct answer.
        gui = self._gui(keymap=None)
        self.assertNotIn(Capability.KEY_EVENT, gui.capabilities)
        self.assertNotIn(Capability.TEXT_ENTRY, gui.capabilities)

    def test_no_keyboard_capability_without_a_keyboard_device(self):
        gui = self._gui(keymap=FakeKeymap(), keyboard=None)
        self.assertNotIn(Capability.TEXT_ENTRY, gui.capabilities)

    def test_keyboard_capabilities_with_both(self):
        gui = self._gui(keymap=FakeKeymap())
        self.assertIn(Capability.KEY_EVENT, gui.capabilities)
        self.assertIn(Capability.TEXT_ENTRY, gui.capabilities)

    def test_typing_a_plain_character_presses_and_releases_it(self):
        gui = self._gui(keymap=FakeKeymap())
        gui.type_text("a")
        codes = [c for c in self.keyboard.calls if c[0] == "keyboard_key"]
        self.assertEqual(
            codes, [("keyboard_key", 30, True), ("keyboard_key", 30, False)]
        )

    def test_typing_a_shifted_character_holds_shift_around_it(self):
        gui = self._gui(keymap=FakeKeymap())
        gui.type_text("A")
        codes = [c for c in self.keyboard.calls if c[0] == "keyboard_key"]
        self.assertEqual(
            codes,
            [
                ("keyboard_key", 42, True),  # shift down
                ("keyboard_key", 30, True),  # key down
                ("keyboard_key", 30, False),  # key up
                ("keyboard_key", 42, False),  # shift up
            ],
        )

    def test_an_unproducible_character_raises_rather_than_substituting(self):
        # Pressing something approximate would put the wrong text on screen
        # with no way for the caller to notice.
        gui = self._gui(keymap=FakeKeymap())
        with self.assertRaises(PyGUITestError) as ctx:
            gui.type_text("é")
        self.assertIn("cannot produce", str(ctx.exception))

    def test_press_key_accepts_a_keysym_name(self):
        gui = self._gui(keymap=FakeKeymap())
        gui.press_key("Return")
        self.assertIn(("keyboard_key", 28, True), self.keyboard.calls)

    def test_one_emulation_sequence_is_held_on_the_keyboard_too(self):
        gui = self._gui(keymap=FakeKeymap())
        gui.type_text("aa")
        starts = [c for c in self.keyboard.calls if c[0] == "start_emulating"]
        self.assertEqual(len(starts), 1)

    def test_close_releases_the_keymap(self):
        keymap = FakeKeymap()
        gui = self._gui(keymap=keymap)
        gui.close()
        self.assertTrue(keymap.closed)


class TestClose(LibeiTestCase):
    def test_close_drops_device_sender_and_connection(self):
        self.gui.close()
        self.assertIsNone(self.gui._device)
        self.assertIsNone(self.gui._sender)
        self.assertIsNone(self.gui._connection)


class TestNegotiation(unittest.TestCase):
    def _backend(self, connection, sender=None):
        sender_class = (
            types.SimpleNamespace(create_for_fd=lambda fd, name=None: sender)
            if sender is not None
            else None
        )
        ei_patcher = install_fake_ei(sender=sender_class)
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        gi_patcher = install_fake_gi(connection=connection)
        gi_patcher.start()
        self.addCleanup(gi_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        return LibeiBackend

    def test_successful_negotiation_calls_every_step_in_order(self):
        connection = FakeConnection()
        seat = FakeSeat(
            offered=(
                FakeDeviceCapability.POINTER
                | FakeDeviceCapability.POINTER_ABSOLUTE
                | FakeDeviceCapability.BUTTON
                | FakeDeviceCapability.SCROLL
            )
        )
        device = FakeDevice()
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=device)],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with _always_ready():
            gui = LibeiBackend()

        methods = [c[0] for c in connection.calls]
        self.assertEqual(
            methods,
            ["Get", "CreateSession", "SelectDevices", "Start", "ConnectToEIS"],
        )
        self.assertIs(gui._device, device)

    def test_no_screencast_source_is_ever_requested(self):
        # Verified live that an absolute-pointer device with a real region
        # needs no ScreenCast source; asking for one would make the user
        # grant screen recording for nothing. See eiinput.py's docstring.
        connection = FakeConnection()
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER_ABSOLUTE)
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=FakeDevice())],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with _always_ready():
            LibeiBackend()
        methods = [c[0] for c in connection.calls]
        self.assertNotIn("SelectSources", methods)
        self.assertNotIn("OpenPipeWireRemote", methods)

    def test_old_remote_desktop_version_is_refused(self):
        connection = FakeConnection(version=1)
        LibeiBackend = self._backend(connection)
        with self.assertRaises(BackendUnavailable):
            LibeiBackend()
        # Refused before ever calling CreateSession.
        self.assertEqual(
            connection.calls,
            [("Get", ("org.freedesktop.portal.RemoteDesktop", "version"))],
        )

    def test_declined_start_raises_permission_required(self):
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (1, {}),  # 1 == user cancelled, per the portal spec
            }
        )
        LibeiBackend = self._backend(connection)
        with self.assertRaises(PermissionRequired):
            LibeiBackend()

    def test_absolute_device_wins_over_a_relative_one_that_resumed_first(self):
        # Regression, and the real bug behind a long run of "identical code,
        # different result" failures: mutter resumes *two* pointer devices
        # on one seat, relative and absolute, in no guaranteed order.
        # Returning the first one to resume is a coin flip -- when the
        # relative one won, move_mouse silently did nothing.
        connection = FakeConnection()
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER)
        relative = FakeDevice(
            capabilities=(FakeDeviceCapability.POINTER | FakeDeviceCapability.BUTTON)
        )
        absolute = FakeDevice(
            capabilities=(
                FakeDeviceCapability.POINTER_ABSOLUTE | FakeDeviceCapability.BUTTON
            )
        )
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=relative)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=absolute)],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with _always_ready():
            gui = LibeiBackend()
        self.assertIs(gui._device, absolute)
        self.assertIn(Capability.POINTER_MOVE, gui.capabilities)

    def test_a_relative_only_device_is_still_returned_after_settling(self):
        # Degrades to a partial backend rather than no backend: button and
        # scroll still work, and capabilities reports no POINTER_MOVE.
        connection = FakeConnection()
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER)
        relative = FakeDevice(
            capabilities=(FakeDeviceCapability.POINTER | FakeDeviceCapability.BUTTON)
        )
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=relative)],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._SIBLING_SETTLE", 0.05),
        ):
            gui = LibeiBackend()
        self.assertIs(gui._device, relative)
        self.assertNotIn(Capability.POINTER_MOVE, gui.capabilities)
        self.assertIn(Capability.POINTER_BUTTON, gui.capabilities)

    def test_no_persist_options_are_sent_by_default(self):
        connection = FakeConnection()
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER_ABSOLUTE)
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=FakeDevice())],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with _always_ready():
            gui = LibeiBackend()
        select = next(c for c in connection.calls if c[0] == "SelectDevices")
        self.assertNotIn("persist_mode", select[1][-1])
        self.assertNotIn("restore_token", select[1][-1])
        self.assertIsNone(gui.restore_token)

    def test_persist_mode_and_restore_token_round_trip(self):
        connection = FakeConnection(
            responses={
                "CreateSession": (0, {"session_handle": "/session/1"}),
                "SelectDevices": (0, {}),
                "Start": (0, {"devices": 2, "restore_token": "tok-next"}),
            }
        )
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER_ABSOLUTE)
        sender = FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [FakeEvent(FakeEventType.DEVICE_RESUMED, device=FakeDevice())],
            ]
        )
        LibeiBackend = self._backend(connection, sender=sender)
        with _always_ready():
            gui = LibeiBackend(
                restore_token="tok-old",
                persist_mode=2,  # PERSIST_UNTIL_REVOKED
            )
        select = next(c for c in connection.calls if c[0] == "SelectDevices")
        self.assertEqual(select[1][-1]["persist_mode"].value, 2)
        self.assertEqual(select[1][-1]["restore_token"].value, "tok-old")
        # Single-use: the fresh token replaces the one we presented.
        self.assertEqual(gui.restore_token, "tok-next")

    def test_device_wait_timeout_raises_backend_unavailable(self):
        connection = FakeConnection()
        sender = FakeSender(event_batches=[])
        LibeiBackend = self._backend(connection, sender=sender)
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._DEVICE_TIMEOUT", 0.05),
        ):
            with self.assertRaises(BackendUnavailable):
                LibeiBackend()


if __name__ == "__main__":
    unittest.main()


class TestTheFakeMatchesTheRealApiShape(unittest.TestCase):
    """The fake must report capabilities the way python-libei does.

    CI's first run caught this on Python 3.10 and nowhere else. libei types
    both `Seat.capabilities` and `Device.capabilities` as
    `tuple[DeviceCapability, ...]`, but the fake stored a single combined
    Flag. Iterating a Flag *member* was introduced in 3.11, so on 3.11+ the
    production loop over `seat.capabilities` happened to work and the
    divergence was invisible; on 3.10 it raised "object is not iterable".

    That is the failure mode a stand-in has that the real thing does not:
    it can be wrong in a way only some interpreters reveal. These pin the
    shape rather than the behaviour it happened to have.
    """

    def test_a_seat_reports_a_tuple_of_individual_capabilities(self):
        seat = FakeSeat(
            FakeDeviceCapability.POINTER_ABSOLUTE | FakeDeviceCapability.KEYBOARD
        )
        self.assertIsInstance(seat.capabilities, tuple)
        self.assertEqual(
            set(seat.capabilities),
            {FakeDeviceCapability.POINTER_ABSOLUTE, FakeDeviceCapability.KEYBOARD},
        )

    def test_a_device_reports_a_tuple_too(self):
        device = FakeDevice(
            capabilities=FakeDeviceCapability.BUTTON | FakeDeviceCapability.SCROLL
        )
        self.assertIsInstance(device.capabilities, tuple)
        self.assertEqual(
            set(device.capabilities),
            {FakeDeviceCapability.BUTTON, FakeDeviceCapability.SCROLL},
        )

    def test_every_element_is_a_single_flag_not_a_combination(self):
        # A tuple containing one combined value would still be iterable and
        # would still pass a careless test, while meaning something the
        # real library never returns.
        seat = FakeSeat(
            FakeDeviceCapability.POINTER
            | FakeDeviceCapability.BUTTON
            | FakeDeviceCapability.SCROLL
        )
        self.assertEqual(len(seat.capabilities), 3)
        for cap in seat.capabilities:
            with self.subTest(cap=cap):
                self.assertEqual(bin(int(cap)).count("1"), 1)

    def test_a_tuple_is_accepted_unchanged(self):
        given = (FakeDeviceCapability.KEYBOARD,)
        self.assertEqual(FakeSeat(given).capabilities, given)


class TestPyGObjectIsOnlyNeededToNegotiate(unittest.TestCase):
    """Constructing with an injected sender must not demand PyGObject.

    The backend needs Gio to negotiate a portal session and for nothing
    else, and the code says as much where it declines to open a session
    bus: "an injected sender or device means there is nothing to
    negotiate". The import check sat above that branch and fired first, so
    a machine with python-libei and no PyGObject could not construct the
    backend even when handing it a ready-made sender.

    That is not hypothetical -- it is the shape of a developer venv, and
    it made tests/test_eiinput_libei.py error there rather than run. CI
    cannot catch it either way, because CI has neither library.
    """

    def setUp(self):
        """Install the fake ei module, but deliberately not the fake gi."""
        patcher = install_fake_ei()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        self.LibeiBackend = LibeiBackend

    def _backend(self, **kwargs):
        # _gio() returning None is exactly what a machine without
        # PyGObject looks like from inside the backend.
        with mock.patch("pyguitest.backends.eiinput._gio", return_value=None):
            return self.LibeiBackend(**kwargs)

    def test_an_injected_device_constructs_without_pygobject(self):
        self.assertIsNotNone(self._backend(device=FakeDevice()))

    def test_an_injected_sender_alone_constructs_too(self):
        # FakeSender takes batches of events, one batch per dispatch().
        sender = FakeSender(
            [
                [
                    FakeEvent(
                        FakeEventType.SEAT_ADDED,
                        seat=FakeSeat(
                            FakeDeviceCapability.POINTER_ABSOLUTE
                            | FakeDeviceCapability.BUTTON
                        ),
                    ),
                    FakeEvent(FakeEventType.DEVICE_RESUMED, device=FakeDevice()),
                ]
            ]
        )
        # An injected sender's fd is a plain int, not a real descriptor, so
        # _wait_for_devices' select() has to be patched here as everywhere
        # else. Without it the result depends on whether that number happens
        # to be an open fd in the running process: fine on a developer
        # machine with a busy fd table, EBADF on a CI runner.
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._SIBLING_SETTLE", 0.05),
        ):
            self.assertIsNotNone(self._backend(sender=sender))

    def test_negotiating_without_pygobject_still_refuses_clearly(self):
        # The requirement is real on the path that actually uses it, and
        # the message must still name the package to install.
        with self.assertRaises(BackendUnavailable) as caught:
            self._backend()
        self.assertIn("PyGObject", str(caught.exception))
