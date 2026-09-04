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
    CapabilityUnsupported,
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
    PONG = 90


class FakeEvent:
    def __init__(self, event_type, seat=None, device=None, pong=None):
        self.event_type = event_type
        self.seat = seat
        self.device = device
        self._pong = pong

    @property
    def pong(self):
        """Mirrors the real accessor, which raises off a non-PONG event."""
        if self.event_type is not FakeEventType.PONG:
            raise AssertionError("pong read off a non-PONG event")
        if self._pong is None:
            raise RuntimeError("this libei is older than 1.4")
        return self._pong


class FakePing:
    """Stands in for libei's Ping: an id, and a send() that records."""

    _next_id = 1

    def __init__(self):
        self.id = FakePing._next_id
        FakePing._next_id += 1
        self.sent = False

    def send(self):
        self.sent = True
        return self


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

    def __init__(self, event_batches=(), fd=99, can_ping=True):
        self.fd = fd
        self._batches = list(event_batches)
        self.can_ping = can_ping
        self.pings = []

    def new_ping(self):
        # Mirrors libei <1.4, where the symbol does not resolve at all.
        if not self.can_ping:
            raise AttributeError("ei_new_ping")
        ping = FakePing()
        self.pings.append(ping)
        return ping

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


# -- portal-side fakes: what libei.portal hands back, and how it fails. --
#
# eiinput.py no longer negotiates anything itself -- libei.portal does (see
# its module docstring). So these stand in for that module rather than for
# Gio: the D-Bus plumbing they used to fake is tested in python-libei's own
# tests/test_portal.py, and what is left to prove here is the orchestration
# around it. TestTheFakePortalMatchesTheRealApiShape checks these against
# the real module wherever it is installed.


class FakePortalError(Exception):
    """Stands in for libei.portal.PortalError, the base of the hierarchy."""


class FakePortalVersionError(FakePortalError):
    pass


class FakePortalDeniedError(FakePortalError):
    def __init__(self, step, message=None):
        super().__init__(message or f"{step} was not approved")
        self.step = step
        self.message = message


class FakePortalTimeoutError(FakePortalError):
    def __init__(self, step, timeout):
        super().__init__(f"{step} did not answer within {timeout:g}s")
        self.step = step
        self.timeout = timeout


class FakeDeviceType(enum.IntFlag):
    ALL_DEVICES = 0
    KEYBOARD = 1
    POINTER = 2
    TOUCHSCREEN = 4


class FakePersistMode(enum.IntEnum):
    NONE = 0
    WHILE_RUNNING = 1
    UNTIL_REVOKED = 2


class FakeSession:
    """A negotiated session: an EIS fd, a token, and something to close."""

    def __init__(self, eis_fd=12, restore_token=None):
        self._eis_fd = eis_fd
        self.restore_token = restore_token
        self.eis_fd_reads = 0
        self.closed = False

    @property
    def eis_fd(self):
        # A property because reading it is what transfers ownership of the
        # fd in the real thing -- so a test can tell that it was read once,
        # by the Sender, rather than stashed and reused.
        self.eis_fd_reads += 1
        return self._eis_fd

    def close(self):
        self.closed = True


def install_fake_portal(session=None, error=None):
    """Patch _portal() to return a stand-in libei.portal module.

    Patching the probe rather than sys.modules keeps `from libei import
    portal` out of it entirely: TestPortalProbe covers that import on its
    own, and everything else here only cares what negotiate() returns.
    """
    portal = types.ModuleType("libei.portal")
    portal.DeviceType = FakeDeviceType
    portal.PersistMode = FakePersistMode
    portal.PortalError = FakePortalError
    portal.PortalVersionError = FakePortalVersionError
    portal.PortalDeniedError = FakePortalDeniedError
    portal.PortalTimeoutError = FakePortalTimeoutError
    portal.is_available = lambda: True
    portal.calls = []

    def negotiate(**kwargs):
        portal.calls.append(kwargs)
        if error is not None:
            raise error
        return session if session is not None else FakeSession()

    portal.RemoteDesktopSession = types.SimpleNamespace(negotiate=negotiate)
    return mock.patch("pyguitest.backends.eiinput._portal", return_value=portal), portal


class LibeiTestCase(unittest.TestCase):
    """Installs both fakes and injects a ready-made device, skipping negotiation."""

    def setUp(self):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        portal_patcher, self.portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        self.device = FakeDevice()
        self.gui = LibeiBackend(device=self.device)


class TestAvailability(unittest.TestCase):
    def test_available_when_both_libraries_import(self):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        portal_patcher, _portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends import eiinput

        self.assertTrue(eiinput.available())

    def test_missing_libei_refuses_with_an_install_hint(self):
        portal_patcher, _portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        with mock.patch.dict(sys.modules, {"libei": None}):
            from pyguitest.backends.eiinput import LibeiBackend

            with self.assertRaises(BackendUnavailable) as ctx:
                LibeiBackend()
            self.assertIn("libei", str(ctx.exception))

    def test_missing_pygobject_refuses_with_an_install_hint(self):
        # _portal() reports None for a missing PyGObject just as it does for
        # a python-libei too old to have the module -- from here the two are
        # indistinguishable, so the message has to name both.
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        with mock.patch("pyguitest.backends.eiinput._portal", return_value=None):
            from pyguitest.backends.eiinput import LibeiBackend

            with self.assertRaises(BackendUnavailable) as ctx:
                LibeiBackend()
            self.assertIn("PyGObject", str(ctx.exception))
            self.assertIn("python-libei", str(ctx.exception))


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
        portal_patcher, _portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
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


class TestSync(unittest.TestCase):
    """sync() is a real round trip, not a sleep with better branding."""

    def _gui(self, sender, device=None):
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        portal_patcher, _portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        gui = LibeiBackend(sender=sender, device=device or FakeDevice())
        gui._sender = sender
        return gui

    @staticmethod
    def _pong_for(sender):
        """A batch answering whichever ping has been sent by then."""

        class Answering(list):
            def __iter__(self):
                ping = sender.pings[-1]
                return iter([FakeEvent(FakeEventType.PONG, pong=ping)])

        return Answering()

    def test_a_pong_for_our_ping_confirms(self):
        sender = FakeSender()
        gui = self._gui(sender)
        sender._batches = [self._pong_for(sender)]
        with _always_ready():
            self.assertTrue(gui.sync())
        # pings[0] is _can_ping's throwaway probe; the sent one is ours.
        self.assertTrue(sender.pings[-1].sent)

    def test_a_pong_for_someone_elses_ping_is_not_ours(self):
        # Two clients share an EIS connection's event stream in principle;
        # matching on "a PONG arrived" rather than on the id would return
        # early and report input delivered that is still queued.
        sender = FakeSender(
            event_batches=[[FakeEvent(FakeEventType.PONG, pong=FakePing())]]
        )
        gui = self._gui(sender)
        with _always_ready():
            self.assertFalse(gui.sync(timeout=0.05))

    def test_timeout_returns_false_rather_than_raising(self):
        # Matches wait_for_window and the rest of the wait family.
        sender = FakeSender(event_batches=[])
        gui = self._gui(sender)
        with _always_ready():
            self.assertFalse(gui.sync(timeout=0.05))

    def test_unrelated_events_in_the_batch_are_drained_not_skipped(self):
        sender = FakeSender()
        gui = self._gui(sender)

        class Mixed(list):
            def __iter__(self):
                return iter(
                    [
                        FakeEvent(FakeEventType.DEVICE_RESUMED),
                        FakeEvent(FakeEventType.PONG, pong=sender.pings[-1]),
                    ]
                )

        sender._batches = [Mixed()]
        with _always_ready():
            self.assertTrue(gui.sync())

    def test_a_libei_too_old_to_read_pong_does_not_confirm(self):
        # `Event.pong` needs libei 1.4; a PONG whose accessor raises must
        # not be counted as ours.
        sender = FakeSender(event_batches=[[FakeEvent(FakeEventType.PONG, pong=None)]])
        gui = self._gui(sender)
        with _always_ready():
            self.assertFalse(gui.sync(timeout=0.05))

    def test_input_sync_is_declared_when_the_connection_can_ping(self):
        gui = self._gui(FakeSender())
        self.assertIn(Capability.INPUT_SYNC, gui.capabilities)

    def test_input_sync_is_withheld_where_ping_is_unavailable(self):
        # libei <1.4: promising a confirmation this can never deliver is
        # worse than not offering it.
        gui = self._gui(FakeSender(can_ping=False))
        self.assertNotIn(Capability.INPUT_SYNC, gui.capabilities)

    def test_input_sync_is_withheld_without_a_sender(self):
        # A directly-injected device has no connection to round-trip over.
        ei_patcher = install_fake_ei()
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        gui = LibeiBackend(device=FakeDevice())
        self.assertNotIn(Capability.INPUT_SYNC, gui.capabilities)
        with self.assertRaises(CapabilityUnsupported):
            gui.sync()


class TestClose(LibeiTestCase):
    def test_close_drops_device_sender_and_connection(self):
        self.gui.close()
        self.assertIsNone(self.gui._device)
        self.assertIsNone(self.gui._sender)
        self.assertIsNone(self.gui._connection)


class TestNegotiation(unittest.TestCase):
    """What is left here after libei.portal took the D-Bus plumbing.

    The Request/Response sequence itself -- CreateSession, SelectDevices,
    Start, ConnectToEIS, the raceless subscribe-first pattern, the
    session_handle_token workaround -- is python-libei's, and is tested in
    its own tests/test_portal.py. What this backend still owns, and what
    these cover: what it asks that library for, how it translates that
    library's failures into pyguitest's, and everything after the fd
    arrives.
    """

    def _backend(self, session=None, error=None, sender=None):
        self.sender_calls = []
        sender_class = (
            types.SimpleNamespace(
                create_for_fd=lambda fd, name=None: (
                    self.sender_calls.append((fd, name)) or sender
                )
            )
            if sender is not None
            else None
        )
        ei_patcher = install_fake_ei(sender=sender_class)
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        portal_patcher, portal = install_fake_portal(session=session, error=error)
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        return LibeiBackend, portal

    def _one_absolute_device(self, device=None):
        """A sender scripted to resume a single absolute pointer."""
        seat = FakeSeat(offered=FakeDeviceCapability.POINTER_ABSOLUTE)
        return FakeSender(
            event_batches=[
                [FakeEvent(FakeEventType.SEAT_ADDED, seat=seat)],
                [
                    FakeEvent(
                        FakeEventType.DEVICE_RESUMED,
                        device=device if device is not None else FakeDevice(),
                    )
                ],
            ]
        )

    def test_asks_for_a_keyboard_and_a_pointer_and_nothing_else(self):
        # Not ALL_DEVICES: a touchscreen is not something this backend can
        # drive, and asking for one would widen the grant the user is shown
        # for no gain. (libei.portal reads ALL_DEVICES, which is literally
        # 0, as every type -- so this has to be explicit, not left blank.)
        session = FakeSession()
        LibeiBackend, portal = self._backend(
            session=session, sender=self._one_absolute_device()
        )
        with _always_ready():
            gui = LibeiBackend()

        (call,) = portal.calls
        self.assertEqual(
            call["devices"], FakeDeviceType.KEYBOARD | FakeDeviceType.POINTER
        )
        self.assertIsNone(call["connection"])
        self.assertIs(gui._session, session)

    def test_the_negotiated_fd_is_read_once_and_handed_to_the_sender(self):
        # Reading eis_fd is what transfers ownership of the descriptor in
        # the real session object, so reading it twice would leave two
        # owners for one fd -- and the Sender closes what it is given.
        session = FakeSession(eis_fd=77)
        LibeiBackend, _portal = self._backend(
            session=session, sender=self._one_absolute_device()
        )
        with _always_ready():
            LibeiBackend()
        self.assertEqual(self.sender_calls, [(77, "pyguitest")])
        self.assertEqual(session.eis_fd_reads, 1)

    def test_an_injected_connection_is_passed_through(self):
        connection = object()
        LibeiBackend, portal = self._backend(sender=self._one_absolute_device())
        with _always_ready():
            LibeiBackend(connection=connection)
        self.assertIs(portal.calls[0]["connection"], connection)

    def test_each_round_trip_is_bounded_by_the_portal_timeout(self):
        from pyguitest.backends import eiinput

        LibeiBackend, portal = self._backend(sender=self._one_absolute_device())
        with _always_ready():
            LibeiBackend()
        self.assertEqual(portal.calls[0]["timeout"], eiinput._PORTAL_TIMEOUT)

    def test_absolute_device_wins_over_a_relative_one_that_resumed_first(self):
        # Regression, and the real bug behind a long run of "identical code,
        # different result" failures: mutter resumes *two* pointer devices
        # on one seat, relative and absolute, in no guaranteed order.
        # Returning the first one to resume is a coin flip -- when the
        # relative one won, move_mouse silently did nothing.
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
        LibeiBackend, _portal = self._backend(sender=sender)
        with _always_ready():
            gui = LibeiBackend()
        self.assertIs(gui._device, absolute)
        self.assertIn(Capability.POINTER_MOVE, gui.capabilities)

    def test_a_relative_only_device_is_still_returned_after_settling(self):
        # Degrades to a partial backend rather than no backend: button and
        # scroll still work, and capabilities reports no POINTER_MOVE.
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
        LibeiBackend, _portal = self._backend(sender=sender)
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._SIBLING_SETTLE", 0.05),
        ):
            gui = LibeiBackend()
        self.assertIs(gui._device, relative)
        self.assertNotIn(Capability.POINTER_MOVE, gui.capabilities)
        self.assertIn(Capability.POINTER_BUTTON, gui.capabilities)

    def test_no_persist_options_are_sent_by_default(self):
        LibeiBackend, portal = self._backend(sender=self._one_absolute_device())
        with _always_ready():
            gui = LibeiBackend()
        (call,) = portal.calls
        self.assertEqual(call["persist_mode"], FakePersistMode.NONE)
        self.assertIsNone(call["restore_token"])
        self.assertIsNone(gui.restore_token)

    def test_persist_mode_and_restore_token_round_trip(self):
        session = FakeSession(restore_token="tok-next")
        LibeiBackend, portal = self._backend(
            session=session, sender=self._one_absolute_device()
        )
        with _always_ready():
            gui = LibeiBackend(
                restore_token="tok-old",
                persist_mode=2,  # PERSIST_UNTIL_REVOKED
            )
        (call,) = portal.calls
        self.assertEqual(call["persist_mode"], FakePersistMode.UNTIL_REVOKED)
        self.assertEqual(call["restore_token"], "tok-old")
        # Whatever the portal answered with replaces the token we presented
        # -- it may hand back a different one, so the caller must store the
        # reply rather than keep reusing the original.
        self.assertEqual(gui.restore_token, "tok-next")

    def test_a_plain_int_persist_mode_still_reaches_the_library_as_its_enum(self):
        # PERSIST_* stay plain ints here because PersistMode lives behind an
        # optional import; the conversion is this module's job, and passing
        # the int straight through would be a silent API mismatch.
        LibeiBackend, portal = self._backend(sender=self._one_absolute_device())
        with _always_ready():
            LibeiBackend(persist_mode=1)
        self.assertIsInstance(portal.calls[0]["persist_mode"], FakePersistMode)
        self.assertEqual(portal.calls[0]["persist_mode"], FakePersistMode.WHILE_RUNNING)

    def test_a_declined_dialog_becomes_permission_required(self):
        LibeiBackend, _portal = self._backend(
            error=FakePortalDeniedError(
                "Start", "the user declined the remote-control consent dialog"
            )
        )
        with self.assertRaises(PermissionRequired) as ctx:
            LibeiBackend()
        self.assertIn("declined", str(ctx.exception))
        self.assertEqual(ctx.exception.capability, Capability.POINTER_MOVE)

    def test_an_old_remote_desktop_becomes_backend_unavailable(self):
        # A portal too old for ConnectToEIS is "this cannot run here", not
        # "the user said no" -- the distinction a caller skips or fails on.
        LibeiBackend, _portal = self._backend(
            error=FakePortalVersionError(
                "RemoteDesktop version 1 is too old for ConnectToEIS (need 2+)"
            )
        )
        with self.assertRaises(BackendUnavailable) as ctx:
            LibeiBackend()
        self.assertIn("too old", str(ctx.exception))

    def test_any_other_portal_failure_becomes_backend_unavailable(self):
        LibeiBackend, _portal = self._backend(
            error=FakePortalError("cannot reach the session bus: no such bus")
        )
        with self.assertRaises(BackendUnavailable) as ctx:
            LibeiBackend()
        self.assertIn("session bus", str(ctx.exception))

    def test_a_portal_that_never_answers_times_out(self):
        # The failure an unbounded wait cannot recover from: the call is
        # accepted, no Response ever fires, and there is no fd to poll and
        # nothing to interrupt the wait. libei.portal raises its own timeout
        # for it; this backend must present it as pyguitest's, distinct from
        # a decline.
        from pyguitest.errors import PortalTimeout

        LibeiBackend, _portal = self._backend(
            error=FakePortalTimeoutError("CreateSession", 60)
        )
        with self.assertRaises(PortalTimeout) as ctx:
            LibeiBackend()
        self.assertEqual(ctx.exception.method, "CreateSession")
        self.assertEqual(ctx.exception.timeout, 60)

    def test_device_wait_timeout_raises_backend_unavailable(self):
        sender = FakeSender(event_batches=[])
        LibeiBackend, _portal = self._backend(sender=sender)
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._DEVICE_TIMEOUT", 0.05),
        ):
            with self.assertRaises(BackendUnavailable):
                LibeiBackend()

    def test_close_ends_the_portal_session(self):
        # Nothing closed it before this module delegated: a portal session
        # lives in xdg-desktop-portal until Session.Close(), and the D-Bus
        # connection that would otherwise end it is GLib's shared singleton.
        session = FakeSession()
        LibeiBackend, _portal = self._backend(
            session=session, sender=self._one_absolute_device()
        )
        with _always_ready():
            gui = LibeiBackend()
        self.assertFalse(session.closed)
        gui.close()
        self.assertTrue(session.closed)
        self.assertIsNone(gui._session)

    def test_a_failed_construction_closes_the_session_it_negotiated(self):
        # The reachable leak: negotiation succeeds, the consent has been
        # granted, and then no device resumes within _DEVICE_TIMEOUT. The
        # object never reaches a caller, so nothing will ever call close()
        # on it -- but the portal session is already live in
        # xdg-desktop-portal and stays there, grant and all, until the
        # process exits.
        session = FakeSession()
        LibeiBackend, _portal = self._backend(
            session=session, sender=FakeSender(event_batches=[])
        )
        with (
            _always_ready(),
            mock.patch("pyguitest.backends.eiinput._DEVICE_TIMEOUT", 0.05),
        ):
            with self.assertRaises(BackendUnavailable):
                LibeiBackend()
        self.assertTrue(session.closed)

    def test_a_sender_that_cannot_take_the_fd_closes_the_session_too(self):
        session = FakeSession()
        ei_patcher = install_fake_ei(
            sender=types.SimpleNamespace(
                create_for_fd=mock.Mock(side_effect=OSError("bad fd"))
            )
        )
        ei_patcher.start()
        self.addCleanup(ei_patcher.stop)
        portal_patcher, _portal = install_fake_portal(session=session)
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        with self.assertRaises(OSError):
            LibeiBackend()
        self.assertTrue(session.closed)

    def test_an_interrupt_during_the_device_wait_closes_the_session(self):
        # BaseException, not Exception: Ctrl-C during the 15s device wait is
        # exactly when a caller is least likely to clean up by hand.
        session = FakeSession()
        sender = FakeSender(event_batches=[])
        sender.dispatch = mock.Mock(side_effect=KeyboardInterrupt)
        LibeiBackend, _portal = self._backend(session=session, sender=sender)
        with _always_ready():
            with self.assertRaises(KeyboardInterrupt):
                LibeiBackend()
        self.assertTrue(session.closed)

    def test_close_is_safe_with_no_session_to_close(self):
        LibeiBackend, _portal = self._backend()
        gui = LibeiBackend(device=FakeDevice())
        gui.close()  # an injected device negotiated nothing
        self.assertIsNone(gui._session)


class TestPortalProbe(unittest.TestCase):
    """_portal() must fail closed, exactly as _libei() does.

    Both reasons it can fail look the same from the caller's side -- a
    python-libei too old to have the module, and a PyGObject that is not
    installed -- and neither is an error worth propagating: the backend
    registry probes availability on desktops where this backend is simply
    not usable.
    """

    def _install(self, portal_module):
        libei_pkg = types.ModuleType("libei")
        mapping = {"libei": libei_pkg}
        if portal_module is not None:
            libei_pkg.portal = portal_module
            mapping["libei.portal"] = portal_module
        patcher = mock.patch.dict(sys.modules, mapping)
        patcher.start()
        self.addCleanup(patcher.stop)
        if portal_module is None:
            # patch.dict restores the whole mapping on exit, so removing a
            # real installation's submodule here is safe.
            sys.modules.pop("libei.portal", None)

    def _portal(self):
        from pyguitest.backends import eiinput

        return eiinput._portal()

    def test_a_python_libei_without_the_module_reports_unavailable(self):
        self._install(None)
        self.assertIsNone(self._portal())

    def test_an_unavailable_portal_module_reports_unavailable(self):
        module = types.ModuleType("libei.portal")
        module.is_available = lambda: False  # PyGObject missing
        self._install(module)
        self.assertIsNone(self._portal())

    def test_an_is_available_that_raises_reports_unavailable(self):
        module = types.ModuleType("libei.portal")

        def boom():
            raise RuntimeError("gi blew up on import")

        module.is_available = boom
        self._install(module)
        self.assertIsNone(self._portal())

    def test_a_usable_module_is_returned(self):
        module = types.ModuleType("libei.portal")
        module.is_available = lambda: True
        self._install(module)
        self.assertIs(self._portal(), module)


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


def _real_portal():
    """The installed libei.portal, or None. Never fails the import."""
    try:
        from libei import portal
    except Exception:
        return None
    return portal


@unittest.skipUnless(
    _real_portal() is not None, "python-libei with libei.portal is not installed"
)
class TestTheFakePortalMatchesTheRealApiShape(unittest.TestCase):
    """The stand-in portal must have the shape the real module has.

    Same reasoning as the class above, one library further out: everything
    this backend now does with a portal goes through a fake here, so a fake
    that drifts from libei.portal would keep passing while the real call
    fails. CI has neither library and skips this; a developer machine with
    python-libei installed is where the drift gets caught.

    Deliberately shape only -- names, kwargs, exception hierarchy. What
    those calls actually do to a portal is python-libei's own test suite's
    job, and re-asserting it here would be testing the dependency.
    """

    def setUp(self):
        self.real = _real_portal()

    def test_negotiate_accepts_every_keyword_this_backend_passes(self):
        import inspect

        signature = inspect.signature(self.real.RemoteDesktopSession.negotiate)
        for keyword in (
            "connection",
            "devices",
            "persist_mode",
            "restore_token",
            "timeout",
        ):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, signature.parameters)

    def test_the_error_hierarchy_matches(self):
        # _negotiate() catches PortalError last as the catch-all, so the
        # specific ones being subclasses is what makes the order meaningful.
        for fake, real in (
            (FakePortalVersionError, self.real.PortalVersionError),
            (FakePortalDeniedError, self.real.PortalDeniedError),
            (FakePortalTimeoutError, self.real.PortalTimeoutError),
        ):
            with self.subTest(error=real.__name__):
                self.assertTrue(issubclass(real, self.real.PortalError))
                self.assertTrue(issubclass(fake, FakePortalError))

    def test_a_timeout_error_carries_the_step_and_the_timeout(self):
        # Both are read straight through into pyguitest's PortalTimeout.
        real = self.real.PortalTimeoutError("CreateSession", 60)
        fake = FakePortalTimeoutError("CreateSession", 60)
        self.assertEqual((real.step, real.timeout), (fake.step, fake.timeout))

    def test_the_device_and_persist_enums_agree_member_for_member(self):
        for name in ("KEYBOARD", "POINTER"):
            with self.subTest(member=name):
                self.assertEqual(
                    int(getattr(self.real.DeviceType, name)),
                    int(getattr(FakeDeviceType, name)),
                )
        for name in ("NONE", "WHILE_RUNNING", "UNTIL_REVOKED"):
            with self.subTest(member=name):
                self.assertEqual(
                    int(getattr(self.real.PersistMode, name)),
                    int(getattr(FakePersistMode, name)),
                )

    def test_a_session_exposes_what_this_backend_reads(self):
        for attribute in ("eis_fd", "close"):
            with self.subTest(attribute=attribute):
                self.assertTrue(hasattr(self.real.RemoteDesktopSession, attribute))
                self.assertTrue(hasattr(FakeSession, attribute))

    def test_a_session_carries_a_restore_token(self):
        # Checked through __init__ rather than with hasattr: it is an
        # instance attribute on both, and instantiating the real session
        # here would hand __del__ a fabricated fd number to close.
        import inspect

        for cls in (self.real.RemoteDesktopSession, FakeSession):
            with self.subTest(cls=cls.__name__):
                signature = inspect.signature(cls.__init__)
                self.assertIn("restore_token", signature.parameters)


class TestPyGObjectIsOnlyNeededToNegotiate(unittest.TestCase):
    """Constructing with an injected sender must not demand PyGObject.

    PyGObject is needed to negotiate a portal session and for nothing else
    -- now one level down, inside libei.portal -- and the code says as much
    where it declines to reach for it: "an injected sender or device means
    there is nothing to negotiate". The import check sat above that branch
    and fired first, so a machine with python-libei and no PyGObject could
    not construct the backend even when handing it a ready-made sender.

    That is not hypothetical -- it is the shape of a developer venv, and
    it made tests/test_eiinput_libei.py error there rather than run. CI
    cannot catch it either way, because CI has neither library.
    """

    def setUp(self):
        """Install the fake ei module, but deliberately not a fake portal."""
        patcher = install_fake_ei()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends.eiinput import LibeiBackend

        self.LibeiBackend = LibeiBackend

    def _backend(self, **kwargs):
        # _portal() returning None is exactly what a machine without
        # PyGObject looks like from inside the backend.
        with mock.patch("pyguitest.backends.eiinput._portal", return_value=None):
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


if __name__ == "__main__":
    unittest.main()
