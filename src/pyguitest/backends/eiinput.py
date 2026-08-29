"""Absolute pointer motion via libei, negotiated through the RemoteDesktop portal.

`portal.py`'s own D-Bus `NotifyPointerMotionAbsolute` needs a PipeWire stream
id, which only exists after a second, separate ScreenCast consent dialog --
which is why that backend deliberately has no `move_mouse`. libei's protocol
has no such requirement: mutter offers an absolute-pointer device carrying
its own region, from a plain RemoteDesktop session with no screen capture
involved. Verified live (2026-08-26, GNOME 50 host, python-libei
0.1.0.dev0) by enumerating every device the seat resumes, with and without
a ScreenCast source selected -- identical either way, region included.

The one real trap here, and the reason this module documents it at length:
**one seat resumes two pointer devices**, not one. Mutter's are named
`<client> virtual pointer` (`POINTER`, relative, no regions) and
`<client> shared virtual absolute pointer` (`POINTER_ABSOLUTE`, with a
region), and they arrive as separate `DEVICE_RESUMED` events -- the
relative one first, in every run observed. Taking the first device to
resume therefore yields a device on which `pointer_motion_absolute()` logs
libei's own internal `device is not an absolute pointer` warning and
silently does nothing: no Python exception, no movement, and a
`capabilities` set that looks plausible. See `_wait_for_device`, which
waits for the device it actually needs instead.

That failure cost a long debugging session, so: it was originally
misdiagnosed as a missing ScreenCast linkage, on the strength of two lucky
runs where the absolute device happened to resume first. An entire
combined RemoteDesktop+ScreenCast negotiation was built on that
misreading before a side-by-side `busctl --user monitor` comparison caught
the *reference* script failing identically on an unmodified rerun, which
is what finally pointed at the device race. Do not re-add a ScreenCast
source here to "fix" absolute motion -- it is not required, and it makes
the user grant screen-recording permission for nothing.

A hard-won environmental note, recorded because it cost a very long
debugging session here: on this machine the visible cursor did not move for
*any* injection method -- libei, uinput, or ydotool -- while input was
otherwise delivered perfectly (clicking GNOME's Activities button opened
the overview). The cause was not libei, mutter, seat assignment, udev tags
or device classification, all of which were investigated and all of which
were fine. It was **VirtualBox Mouse Integration**, which slaves the guest
pointer to the host's mouse position via an absolute "VirtualBox USB
Tablet" device and so continuously overrides anything injected inside the
guest. Turning it off (Input -> Mouse Integration, or Host+I) fixed it
immediately, for every backend.

The general lesson, worth keeping: a cursor that does not move is not
evidence that injection failed. Check what the application under the
pointer actually did -- `libinput debug-events --device /dev/input/eventN`
shows whether events reach libinput at all, and `loginctl seat-status
seat0` shows whether the device is attached to the session's seat.

Bypasses `libei.oeffis`, which wraps this same negotiation: doing it
directly over Gio drops a native dependency (python-libei's own README
calls liboeffis the least reliable part of that stack in live testing) and
keeps the session handle available for future work such as
`persist_mode`/`restore_token`, which oeffis does not expose. The cost is
~100 lines of Request/Response plumbing, including `RemoteDesktop.ConnectToEIS`
-- a `v2+`-only method returning a fd via
`Gio.DBusConnection.call_with_unix_fd_list_sync` (a `GUnixFDList` index, not
a raw fd number) rather than the plain `call_sync` `portal.py` uses for
everything else.

Deliberately not sharing negotiation code with `portal.py`, despite the
similar shape: `portal.py`'s negotiation is already live-verified and
carries a hard-won crash workaround (`session_handle_token` -- see its own
docstring); refactoring it to share code with this newer, still-settling
negotiation risks that already-working path for a modest amount of
duplication.

Bug found and fixed while building this: subscribing to a portal Request's
`Response` signal only *after* the method call that returns its handle is a
real race, not a hypothetical one -- a fast, non-interactive response (no
consent dialog involved, e.g. `SelectDevices`/`SelectSources`) can arrive
and be delivered before the subscription is registered, hanging forever on
a signal that already came and went. Reproduced live (intermittent hangs at
both `SelectDevices` and `SelectSources`). Fixed here by choosing the
`handle_token` ourselves, computing the resulting request object path up
front, and subscribing to that exact path *before* making the call at all
-- the pattern xdg-desktop-portal's own documentation describes. `portal.py`
does not do this yet and may share the same latent race; out of scope to
fix here since it has not been observed to hang in practice there.

Keyboard input is keymap-*safe* here, which is the one thing neither
`uinput.py` nor ydotool can offer. `Device.keyboard_key()` takes a raw
Linux keycode, and the compositor interprets it using the very keymap it
handed this client through `device.keymap` -- so "which key produces this
character" is a lookup, not a guess. `xkb.py` compiles that keymap and
answers it. On a French AZERTY layout `type_text("a")` presses the
physical Q key, where a hardcoded US table would have typed "q".

Two consequences worth knowing. TEXT_ENTRY and KEY_EVENT are offered only
when a keymap was actually obtained -- a keyboard device without one would
mean guessing, so it is reported as no keyboard at all rather than an
unsafe one. And a character the active layout genuinely cannot produce
(an accented letter on a US layout) raises instead of pressing something
approximate, because the alternative is the wrong text on screen with no
way for the caller to notice.

The keyboard is its own device, arriving as a separate DEVICE_RESUMED
event from the pointer -- see `_wait_for_devices`, and the two-device trap
documented above.
"""

from __future__ import annotations

import mmap
import select
import time
import uuid

from .. import xkb as _xkb
from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, PermissionRequired, PyGUITestError
from .base import GUIBackend

__all__ = ["LibeiBackend", "available"]

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
_REQUEST_INTERFACE = "org.freedesktop.portal.Request"

_MIN_REMOTE_DESKTOP_VERSION = 2  # ConnectToEIS needs v2+

# AvailableDeviceTypes / SelectDevices bitmask, per the RemoteDesktop XML --
# the same numbering portal.py's own _DEVICE_POINTER already uses.
_DEVICE_TYPE_KEYBOARD = 1
_DEVICE_TYPE_POINTER = 2

# SelectDevices `persist_mode`, per the RemoteDesktop XML. Same values and
# same meaning as portal.py's; see PortalBackend.__init__ for the rationale
# on never storing the token here.
PERSIST_NONE = 0
PERSIST_WHILE_RUNNING = 1
PERSIST_UNTIL_REVOKED = 2

_PORTAL_TIMEOUT = 60
"""Seconds to wait for the consent dialog -- generous, since a human has to
see and answer it, same reasoning as portal.py's Start() having no bound at
all; this at least caps it."""

_DEVICE_TIMEOUT = 15
"""Seconds to wait for a device to reach DEVICE_RESUMED once the fd is live.
No dialog involved past this point, so much shorter than the wait above."""

_SIBLING_SETTLE = 1.0
"""Extra seconds to keep draining events after a device resumes that lacks
POINTER_ABSOLUTE, in case its absolute-pointer sibling is still coming --
see _wait_for_device. Only ever paid when the first device to arrive is not
the one wanted; an absolute device returns immediately."""

_BUTTONS = {1: 0x110, 2: 0x112, 3: 0x111}  # BTN_LEFT, BTN_MIDDLE, BTN_RIGHT


def _gio():
    """Import Gio and GLib, or return None. Same dependency portal.py needs."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    return Gio, GLib


def _libei():
    """Import libei's ei module, or return None.

    Importing is always safe even without the native library (see
    python-libei's own __init__.py docstring); availability is checked
    separately via is_available(), which fails closed on any problem.
    """
    try:
        from libei import ei
    except Exception:
        return None
    try:
        if not ei.is_available():
            return None
    except Exception:
        return None
    return ei


def available():
    """Whether PyGObject and python-libei (with its native library) are usable."""
    return _gio() is not None and _libei() is not None


class LibeiBackend(GUIBackend):
    """Absolute pointer motion, buttons and scroll, via libei."""

    name = "eiinput"

    def __init__(
        self,
        connection=None,
        sender=None,
        device=None,
        keyboard=None,
        keymap=None,
        restore_token=None,
        persist_mode=PERSIST_NONE,
    ):
        """Negotiate a RemoteDesktop portal session, or accept injected pieces.

        Passing `device` directly skips both the portal round-trip and the
        seat/device negotiation loop entirely -- the seam most tests use.
        `connection` can be injected to exercise the real negotiation against
        a fake Gio connection (see tests/test_eiinput.py) without a real
        portal.

        `persist_mode`/`restore_token` avoid re-prompting on every launch,
        exactly as in `PortalBackend`: ask for persistence, read
        `self.restore_token` afterwards, and hand it back next time. Each
        restore mints a new token. Nothing is written to disk here -- the
        token is a standing grant of input injection, so storing it is the
        caller's decision. This is also the reason this backend negotiates
        the portal itself instead of using `libei.oeffis`, whose
        `oeffis_create_session()` takes only a device-type bitmask and
        exposes neither options nor the session handle.
        """
        gio_modules = _gio()
        if gio_modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs (see README)"
            )
        self._Gio, self._GLib = gio_modules
        ei = _libei()
        if ei is None:
            raise BackendUnavailable(
                "python-libei is not installed, or the native libei "
                "library is missing; `pip install pyguitest[eiinput]` "
                "supplies the bindings, the distribution supplies libei "
                "(see README)"
            )
        self._ei = ei

        self._connection = connection
        self._persist_mode = persist_mode
        self._restore_token = restore_token
        self.restore_token = None
        """The token to reuse next time, or None if the portal issued none."""
        self._emulation_started = False
        self._keyboard_emulation_started = False
        self._sender = sender
        self._device = device
        self._keyboard = keyboard
        self._keymap = keymap
        if device is None:
            if sender is None:
                # The bus is only needed to negotiate; an injected sender or
                # device means there is nothing to negotiate, so do not open
                # a session bus that a test (or a headless run) may not have.
                if self._connection is None:
                    try:
                        self._connection = self._Gio.bus_get_sync(
                            self._Gio.BusType.SESSION, None
                        )
                    except Exception as exc:
                        raise BackendUnavailable(
                            f"cannot reach the session bus: {exc}"
                        ) from exc
                eis_fd = self._negotiate_eis_fd()
                self._sender = ei.Sender.create_for_fd(eis_fd, name="pyguitest")
            self._device, self._keyboard = self._wait_for_devices()
            self._keymap = self._compile_keymap(self._keyboard)

    # -- portal request/response plumbing ----------------------------------

    def _request(self, interface, method, signature, leading_args, options):
        """Call a Request-returning method, racelessly.

        Subscribing to the Response signal only after the call returns its
        handle is a real race (see the module docstring); this computes the
        handle_token and the resulting request path itself, subscribes to
        that exact path, and only then makes the call.
        """
        unique_name = self._connection.get_unique_name()
        escaped_sender = unique_name[1:].replace(".", "_")
        token = uuid.uuid4().hex
        options = dict(options)
        options["handle_token"] = self._GLib.Variant("s", token)
        expected_path = (
            f"/org/freedesktop/portal/desktop/request/{escaped_sender}/{token}"
        )

        loop = self._GLib.MainLoop()
        result = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params, *_a):
            result["code"], result["results"] = params.unpack()
            loop.quit()

        subscription = self._connection.signal_subscribe(
            _BUS_NAME,
            _REQUEST_INTERFACE,
            "Response",
            expected_path,
            None,
            self._Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        try:
            parameters = self._GLib.Variant(signature, (*leading_args, options))
            self._connection.call_sync(
                _BUS_NAME,
                _OBJECT_PATH,
                interface,
                method,
                parameters,
                None,
                self._Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            loop.run()
        finally:
            self._connection.signal_unsubscribe(subscription)
        return result["code"], result["results"]

    def _call_for_fd(self, interface, method, session_handle):
        """Call a method that returns a fd via a GUnixFDList index."""
        reply, fd_list = self._connection.call_with_unix_fd_list_sync(
            _BUS_NAME,
            _OBJECT_PATH,
            interface,
            method,
            self._GLib.Variant("(oa{sv})", (session_handle, {})),
            self._GLib.VariantType.new("(h)"),
            self._Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        (handle_index,) = reply.unpack()
        return fd_list.get(handle_index)

    def _remote_desktop_version(self):
        reply = self._connection.call_sync(
            _BUS_NAME,
            _OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            self._GLib.Variant("(ss)", (_REMOTE_DESKTOP, "version")),
            None,
            self._Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        (version,) = reply.unpack()
        return int(version)

    def _negotiate_eis_fd(self):
        """CreateSession, SelectDevices, Start, ConnectToEIS.

        No ScreenCast source is selected: verified live that it is not
        needed for an absolute-pointer device with a real region, and
        asking for one would make the user grant screen recording for
        nothing. See the module docstring.
        """
        version = self._remote_desktop_version()
        if version < _MIN_REMOTE_DESKTOP_VERSION:
            raise BackendUnavailable(
                f"RemoteDesktop version {version} is too old for ConnectToEIS "
                f"(need {_MIN_REMOTE_DESKTOP_VERSION}+)"
            )

        # session_handle_token is a *different* token from the handle_token
        # _request() injects itself: omitting it crashes xdg-desktop-portal
        # 1.22.1 outright (SIGABRT, taking the portal down system-wide) --
        # see portal.py's own docstring for the reproduction. Not optional.
        code, results = self._request(
            _REMOTE_DESKTOP,
            "CreateSession",
            "(a{sv})",
            (),
            {"session_handle_token": self._GLib.Variant("s", uuid.uuid4().hex)},
        )
        if code != 0:
            raise PermissionRequired(
                Capability.POINTER_MOVE, self.name, "CreateSession was not approved"
            )
        session_handle = results["session_handle"]

        options = {
            "types": self._GLib.Variant(
                "u", _DEVICE_TYPE_KEYBOARD | _DEVICE_TYPE_POINTER
            )
        }
        if self._persist_mode != PERSIST_NONE:
            options["persist_mode"] = self._GLib.Variant("u", self._persist_mode)
        if self._restore_token is not None:
            options["restore_token"] = self._GLib.Variant("s", self._restore_token)
        code, _results = self._request(
            _REMOTE_DESKTOP, "SelectDevices", "(oa{sv})", (session_handle,), options
        )
        if code != 0:
            raise PermissionRequired(
                Capability.POINTER_MOVE, self.name, "SelectDevices was not approved"
            )

        code, results = self._request(
            _REMOTE_DESKTOP, "Start", "(osa{sv})", (session_handle, ""), {}
        )
        if code != 0:
            raise PermissionRequired(
                Capability.POINTER_MOVE,
                self.name,
                "the user declined the remote-control consent dialog",
            )
        self.restore_token = results.get("restore_token")
        return self._call_for_fd(_REMOTE_DESKTOP, "ConnectToEIS", session_handle)

    def _wait_for_devices(self):
        """Bind what this backend needs and collect the resulting devices.

        Waits for DEVICE_RESUMED, not DEVICE_ADDED -- a device arrives
        paused, and libei calls sending events before it resumes "a client
        bug" (see python-libei's own README).

        One seat resumes *several* devices, and mutter does exactly that: a
        relative pointer, an absolute pointer, and a keyboard all arrive as
        separate DEVICE_RESUMED events in no guaranteed order. Taking
        whichever resumed first -- what this did originally -- is a coin
        flip, and was the real cause of a long run of "identical code,
        different result" failures where move_mouse silently did nothing:
        the relative device had won the race. So devices are sorted by what
        they can do rather than by when they turned up.

        Returns (pointer, keyboard); either may be None. A partial result is
        deliberate -- `capabilities` reports only what was actually
        obtained, so a seat that offers no keyboard yields a working
        pointer backend rather than no backend.
        """
        ei = self._ei
        wanted = (
            ei.DeviceCapability.POINTER
            | ei.DeviceCapability.POINTER_ABSOLUTE
            | ei.DeviceCapability.BUTTON
            | ei.DeviceCapability.SCROLL
            | ei.DeviceCapability.KEYBOARD
        )
        pointer = keyboard = fallback_pointer = None
        deadline = time.monotonic() + _DEVICE_TIMEOUT
        settle_deadline = None
        while time.monotonic() < deadline:
            if pointer is not None and keyboard is not None:
                break
            if settle_deadline is not None and time.monotonic() > settle_deadline:
                break
            select.select([self._sender.fd], [], [], 0.5)
            self._sender.dispatch()
            for event in self._sender.events:
                if event.event_type is ei.EventType.SEAT_ADDED:
                    offered = 0
                    for cap in event.seat.capabilities:
                        offered |= cap
                    to_bind = tuple(
                        cap for cap in ei.DeviceCapability if cap & offered & wanted
                    )
                    if to_bind:
                        event.seat.bind(to_bind)
                elif event.event_type is ei.EventType.DEVICE_RESUMED:
                    device = event.device
                    caps = device.capabilities
                    if pointer is None and ei.DeviceCapability.POINTER_ABSOLUTE in caps:
                        pointer = device
                    elif keyboard is None and ei.DeviceCapability.KEYBOARD in caps:
                        keyboard = device
                    elif fallback_pointer is None and (
                        ei.DeviceCapability.BUTTON in caps
                        or ei.DeviceCapability.SCROLL in caps
                    ):
                        # The relative-pointer sibling. Kept only in case no
                        # absolute one ever arrives: it still clicks and
                        # scrolls, and a partial backend beats none.
                        fallback_pointer = device
                    if settle_deadline is None:
                        settle_deadline = time.monotonic() + _SIBLING_SETTLE
        if pointer is None:
            pointer = fallback_pointer
        if pointer is None and keyboard is None:
            raise BackendUnavailable(
                "no libei device reached DEVICE_RESUMED within the timeout"
            )
        return pointer, keyboard

    def _compile_keymap(self, keyboard):
        """Compile the keymap the compositor handed us, or return None.

        This is what makes typing here keymap-*safe*: the compositor will
        interpret whatever keycode is sent using precisely this keymap, so
        reading it turns "which key produces this character" from a guess
        into a lookup. Without it, TEXT_ENTRY is not offered at all rather
        than falling back to a US-layout table -- reproducing uinput's
        wrong-characters-on-AZERTY behaviour is the exact thing this
        backend exists to avoid.
        """
        if keyboard is None or not _xkb.available():
            return None
        try:
            keymap = keyboard.keymap
            if keymap is None or keymap.keymap_type != self._ei.KeymapType.XKB:
                return None
            # keymap.fd hands out a fresh dup() that *this* caller owns --
            # python-libei keeps its own and closes that itself -- so the
            # handle has to be closed here rather than left to the GC.
            # mmap maps from offset 0 regardless of the fd's position, so
            # the rewind python-libei does is irrelevant either way.
            with (
                keymap.fd as handle,
                mmap.mmap(
                    handle.fileno(), keymap.size, access=mmap.ACCESS_READ
                ) as buffer,
            ):
                text = buffer.read().decode("utf-8").rstrip("\x00")
            return _xkb.Keymap(text)
        except Exception:
            # A keymap this cannot read is a reason to offer no keyboard,
            # never a reason to fail construction: the pointer half is
            # independently useful and already negotiated by this point.
            return None

    @property
    def capabilities(self):
        """What the negotiated device actually reports, not what was hoped for.

        A hardcoded {POINTER_MOVE, POINTER_BUTTON, POINTER_SCROLL} previously
        sat here regardless of the real device -- which is exactly how the
        two-device race this module's docstring describes stayed invisible
        to capability checks for so long: the device silently lacked
        POINTER_ABSOLUTE while this property kept claiming POINTER_MOVE, so
        move_mouse() sailed past require() into a libei-internal warning
        and did nothing. Deriving it from the real device means a caller's
        supports() check now reflects what will actually happen.
        """
        provided = set()
        if self._device is not None:
            caps = self._device.capabilities
            if self._ei.DeviceCapability.POINTER_ABSOLUTE in caps:
                provided.add(Capability.POINTER_MOVE)
            if self._ei.DeviceCapability.BUTTON in caps:
                provided.add(Capability.POINTER_BUTTON)
            if self._ei.DeviceCapability.SCROLL in caps:
                provided.add(Capability.POINTER_SCROLL)
        # Keyboard is offered only with a keymap in hand. A keyboard device
        # without one could still press keycodes, but which characters they
        # produce would be a guess -- exactly uinput's failure mode, and
        # the thing this backend exists to avoid. Better to report no
        # keyboard than an unsafe one.
        if self._keyboard is not None and self._keymap is not None:
            provided.add(Capability.KEY_EVENT)
            provided.add(Capability.TEXT_ENTRY)
        return CapabilitySet(provided)

    def close(self):
        """End emulation on both devices, and release everything held."""
        if self._emulation_started and self._device is not None:
            self._device.stop_emulating()
            self._emulation_started = False
        if self._keyboard_emulation_started and self._keyboard is not None:
            self._keyboard.stop_emulating()
            self._keyboard_emulation_started = False
        if self._keymap is not None:
            self._keymap.close()
            self._keymap = None
        self._device = None
        self._keyboard = None
        self._sender = None
        self._connection = None

    def _button_code(self, button):
        try:
            return _BUTTONS[button]
        except KeyError:
            raise ValueError(f"unsupported button {button!r}") from None

    def _emulating(self):
        """The device, inside a started emulation sequence.

        One sequence is opened on first use and held for the life of this
        backend, rather than a start/stop around every call. Wrapping each
        event individually does deliver it -- an app under the pointer sees
        the motion and highlights on hover -- but the *visible cursor* snaps
        back as soon as the sequence ends, so a moved pointer leaves no
        trace on screen and every position looks like it was ignored. Real
        remote-desktop clients hold one sequence open and stream into it,
        which is also cheaper per event: the same "hold the device open for
        the session" reasoning uinput.py gives for not re-creating its
        device per call.
        """
        if not self._emulation_started:
            self._device.start_emulating()
            self._emulation_started = True
        return self._device

    def _emulating_keyboard(self):
        """The keyboard device, inside a started emulation sequence."""
        if not self._keyboard_emulation_started:
            self._keyboard.start_emulating()
            self._keyboard_emulation_started = True
        return self._keyboard

    def _pump(self):
        """Service the libei connection after queueing events.

        Nothing here consumes incoming events -- this is a Sender -- but the
        socket still has to be read: without it the compositor's own
        traffic (DEVICE_PAUSED, disconnects, SYNC) accumulates unread for
        the life of the session, and a long automation run is exactly the
        case where that matters. `Context.events` releases each event as it
        is drained, which is also what answers a SYNC.
        """
        if self._sender is None:  # a directly-injected device, in tests
            return
        self._sender.dispatch()
        for _event in self._sender.events:
            pass

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position, within the device's region."""
        self.require(Capability.POINTER_MOVE)
        self._emulating().pointer_motion_absolute(x, y).frame()
        self._pump()

    def press_button(self, button):
        """Press a mouse button. 1 is left, 2 middle, 3 right."""
        self.require(Capability.POINTER_BUTTON)
        self._emulating().button(self._button_code(button), True).frame()
        self._pump()

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._emulating().button(self._button_code(button), False).frame()
        self._pump()

    def scroll(self, dx=0, dy=0):
        """Scroll by logical-pixel deltas, on whichever axes are non-zero."""
        self.require(Capability.POINTER_SCROLL)
        if not dx and not dy:
            return
        self._emulating().scroll_delta(dx, dy).frame()
        self._pump()

    # -- keyboard ----------------------------------------------------------

    def _send_key(self, keycode, modifiers, press):
        """Press or release one key, holding `modifiers` around it."""
        keyboard = self._emulating_keyboard()
        if press:
            for modifier in modifiers:
                keyboard.keyboard_key(modifier, True)
            keyboard.keyboard_key(keycode, True).frame()
        else:
            keyboard.keyboard_key(keycode, False)
            for modifier in reversed(modifiers):
                keyboard.keyboard_key(modifier, False)
            keyboard.frame()
        self._pump()

    def _resolve_name(self, key):
        """(keycode, modifiers) for a key name, via the compositor's keymap.

        Names are X11 keysym names -- the vocabulary GUIBackend's own
        MODIFIER_KEYS/KEY_ALIASES already use, so send_keys() works here
        with no per-backend table. A single character resolves as itself.
        """
        found = self._keymap.for_name(key)
        if found is None and len(key) == 1:
            found = self._keymap.for_char(key)
        if found is None:
            raise PyGUITestError(f"the active keyboard layout cannot produce {key!r}")
        return found

    def press_key(self, key):
        """Press a key by name, without releasing it."""
        self.require(Capability.KEY_EVENT)
        keycode, modifiers = self._resolve_name(key)
        self._send_key(keycode, modifiers, True)

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        keycode, modifiers = self._resolve_name(key)
        self._send_key(keycode, modifiers, False)

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Keymap-safe by construction: every character is resolved through
        the keymap the compositor itself supplied, so 'a' presses whichever
        physical key produces 'a' on the active layout -- KEY_A on QWERTY,
        KEY_Q on AZERTY. `allow_keymap_unsafe` is accepted for signature
        compatibility and has nothing to refuse here.

        Raises rather than substituting when the layout cannot produce a
        character at all (an accented letter on a US layout, say): pressing
        something approximate would put the wrong text on screen and the
        caller would have no way to tell.
        """
        self.require(Capability.TEXT_ENTRY)
        for char in text:
            found = self._keymap.for_char(char)
            if found is None:
                raise PyGUITestError(
                    f"the active keyboard layout cannot produce {char!r}; "
                    "no key on it generates that character"
                )
            keycode, modifiers = found
            self._send_key(keycode, modifiers, True)
            self._send_key(keycode, modifiers, False)
            if delay:
                time.sleep(delay)
