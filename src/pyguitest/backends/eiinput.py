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

The portal negotiation itself lives in python-libei, not here:
`libei.portal.RemoteDesktopSession.negotiate()` runs the whole
`CreateSession` -> `SelectDevices` -> `Start` -> `ConnectToEIS` sequence
over D-Bus and hands back an EIS fd, and this module only translates its
failures into pyguitest's own error vocabulary. That code started life
here -- ~100 lines of Request/Response plumbing, including
`RemoteDesktop.ConnectToEIS`, a `v2+`-only method returning a fd via
`Gio.DBusConnection.call_with_unix_fd_list_sync` (a `GUnixFDList` index,
not a raw fd number) rather than the plain `call_sync` `portal.py` uses
for everything else -- and moved upstream in python-libei 0.3.0 so every
consumer of that library gets it rather than just this one. Both hard-won
fixes went with it: the subscribe-before-call race described below, and
the `session_handle_token` crash workaround `portal.py`'s docstring
records.

`libei.oeffis` still cannot do this job, which is why python-libei grew a
D-Bus path beside its liboeffis wrapper rather than this module using the
wrapper: `oeffis_create_session()` takes only a device-type bitmask, so it
exposes neither `persist_mode`/`restore_token` nor the session handle.
Upstream libei's own documentation points the same way -- "liboeffis is
intentionally kept simple, any more complex needs should be handled by an
application talking to DBus directly".

pyguitest keeps `portalrequest.py` regardless. `portal.py` and
`screenshot.py` talk to RemoteDesktop and Screenshot with no libei
involvement at all, so routing them through a libei dependency for the
sake of sharing one request helper would be backwards. The duplication is
one request helper on each side of a library boundary, deliberately.

Bug found and fixed while building this, and now carried in both
`libei.portal` and `portalrequest.py`: subscribing to a portal Request's
`Response` signal only *after* the method call that returns its handle is a
real race, not a hypothetical one -- a fast, non-interactive response (no
consent dialog involved, e.g. `SelectDevices`/`SelectSources`) can arrive
and be delivered before the subscription is registered, hanging forever on
a signal that already came and went. Reproduced live (intermittent hangs at
both `SelectDevices` and `SelectSources`). The fix is the pattern
xdg-desktop-portal's own documentation describes: choose the `handle_token`
yourself, compute the resulting request object path up front, and subscribe
to that exact path *before* making the call at all.

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

from .. import xkb as _xkb
from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    PermissionRequired,
    PortalTimeout,
    PyGUITestError,
)
from .base import GUIBackend

__all__ = ["LibeiBackend", "available"]

# SelectDevices `persist_mode`, per the RemoteDesktop XML. Same values and
# same meaning as portal.py's and as libei.portal.PersistMode, which these
# are converted to before the call; kept as plain ints here because that
# enum lives behind an optional import. See PortalBackend.__init__ for the
# rationale on never storing the token here.
PERSIST_NONE = 0
PERSIST_WHILE_RUNNING = 1
PERSIST_UNTIL_REVOKED = 2

_PORTAL_TIMEOUT = 60
"""Seconds to wait for the consent dialog -- generous, since a human has to
see and answer it, but bounded: a portal that accepts the call and then dies
sends no Response and no error, and an unbounded `loop.run()` waits on that
forever with no fd to poll and nothing to interrupt it. Passed to
libei.portal, which caps each round trip of the negotiation with it, and
matches portalrequest.py's DEFAULT_TIMEOUT, which caps the same wait for the
other portal backends."""

_DEVICE_TIMEOUT = 15
"""Seconds to wait for a device to reach DEVICE_RESUMED once the fd is live.
No dialog involved past this point, so much shorter than the wait above."""

_SIBLING_SETTLE = 1.0
"""Extra seconds to keep draining events after a device resumes that lacks
POINTER_ABSOLUTE, in case its absolute-pointer sibling is still coming --
see _wait_for_device. Only ever paid when the first device to arrive is not
the one wanted; an absolute device returns immediately."""

_BUTTONS = {1: 0x110, 2: 0x112, 3: 0x111}  # BTN_LEFT, BTN_MIDDLE, BTN_RIGHT


def _portal():
    """Import libei.portal, or return None if it cannot negotiate here.

    Returns None both when python-libei is too old to have the module and
    when PyGObject is missing, which is what `is_available()` reports -- the
    module imports fine without it, since the Gio import is deferred exactly
    as `_libei()` describes for the native library.
    """
    try:
        from libei import portal
    except Exception:
        return None
    try:
        if not portal.is_available():
            return None
    except Exception:
        return None
    return portal


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
    return _portal() is not None and _libei() is not None


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
        `connection` is handed straight to
        `libei.portal.RemoteDesktopSession.negotiate()`, which opens a
        session-bus connection of its own when it is None.

        `persist_mode`/`restore_token` avoid re-prompting on every launch,
        exactly as in `PortalBackend`: ask for persistence, read
        `self.restore_token` afterwards, and hand it back next time. Save
        whatever comes back on every run rather than only the first: the
        portal may answer with a different token, and a caller keeping only
        the original would eventually present a stale one. (Measured
        2026-09-01 on GNOME: the same token comes back each restore. That
        is one portal's behaviour, not a guarantee.) Nothing is written to
        disk here -- the token is a standing grant of input injection, so
        storing it is the caller's decision. This is also the reason
        negotiation goes through `libei.portal` rather than `libei.oeffis`,
        whose `oeffis_create_session()` takes only a device-type bitmask and
        exposes neither options nor the session handle.

        Passing a `restore_token` while leaving `persist_mode` at
        PERSIST_NONE raises ValueError rather than quietly spending the
        token: the portal answers such a request with no token at all, so a
        caller following the save-every-run rule above would write None over
        the token it just used up.
        """
        # PyGObject is deliberately NOT required here. It is needed only to
        # negotiate the portal session, and the branch below already says
        # so: an injected sender or device means there is nothing to
        # negotiate. Demanding it up front contradicted that and refused a
        # construction that needs nothing from it -- which is exactly how
        # the integration test fails on a machine that has python-libei but
        # not PyGObject.
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
        self._session = None
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
            try:
                if sender is None:
                    # The portal (and so PyGObject, and so the session bus)
                    # is only needed to negotiate; an injected sender or
                    # device means there is nothing to negotiate, so nothing
                    # here is imported or opened on that path.
                    portal = _portal()
                    if portal is None:
                        raise BackendUnavailable(
                            "libei.portal is unusable: negotiating a portal "
                            "session needs python-libei 0.4.0+ and PyGObject, "
                            "and one of them is missing; `pip install "
                            "'pyguitest[eiinput]'` supplies both (see README)"
                        )
                    self._session = self._negotiate(portal)
                    # Reading eis_fd transfers the fd to the Sender, which
                    # owns and closes it from here on; the session's own
                    # close() then has only the D-Bus half left to do. See
                    # libei.portal.RemoteDesktopSession.
                    #
                    # If create_for_fd itself raises, that one descriptor
                    # leaks: it has already left the session (so its close()
                    # will not close it), and whether libei closed it before
                    # failing is not knowable from here -- `ei_setup_backend_fd`
                    # takes ownership on success and documents nothing about
                    # its error path. Closing it on a guess risks closing a
                    # since-reused fd number belonging to something else,
                    # which is far worse than leaking one fd while a
                    # construction is already failing.
                    self._sender = ei.Sender.create_for_fd(
                        self._session.eis_fd, name="pyguitest"
                    )
                self._device, self._keyboard = self._wait_for_devices()
                self._keymap = self._compile_keymap(self._keyboard)
            except BaseException:
                # Nothing else can close a session negotiated by a
                # constructor that then raised: close() is never reached on
                # an object that was never returned, and the session lives
                # in xdg-desktop-portal until Session.Close() regardless of
                # what happens in this process. The reachable case is not
                # exotic -- _wait_for_devices() raises BackendUnavailable
                # whenever no device resumes within _DEVICE_TIMEOUT -- and
                # leaving it would strand an approved session, and the
                # standing input-injection grant it carries, until exit.
                # BaseException so that a Ctrl-C during that 15s wait
                # cleans up too.
                if self._session is not None:
                    self._session.close()
                    self._session = None
                raise

    # -- portal negotiation ------------------------------------------------

    def _negotiate(self, portal):
        """Negotiate a RemoteDesktop session, in this package's error vocabulary.

        The sequence itself (CreateSession -> SelectDevices -> Start ->
        ConnectToEIS, raceless Request/Response handling, the
        `session_handle_token` crash workaround) belongs to
        `libei.portal`; see the module docstring for why it lives there.
        What is left here is the translation, because a caller of pyguitest
        should not have to catch a second library's exception hierarchy to
        find out that a consent dialog was declined.

        No ScreenCast source is requested -- `libei.portal` never asks for
        one, which is what this backend needs: an absolute-pointer device
        carries its own region, verified live, so asking would make the
        user grant screen recording for nothing. See the module docstring.
        """
        try:
            session = portal.RemoteDesktopSession.negotiate(
                connection=self._connection,
                devices=portal.DeviceType.KEYBOARD | portal.DeviceType.POINTER,
                persist_mode=portal.PersistMode(self._persist_mode),
                restore_token=self._restore_token,
                timeout=_PORTAL_TIMEOUT,
            )
        except portal.PortalTimeoutError as exc:
            raise PortalTimeout(exc.step, exc.timeout) from exc
        except portal.PortalDeniedError as exc:
            raise PermissionRequired(
                Capability.POINTER_MOVE, self.name, str(exc)
            ) from exc
        except portal.PortalError as exc:
            # PortalVersionError (RemoteDesktop too old for ConnectToEIS),
            # an unreachable session bus, and any other portal-side failure:
            # all "this backend cannot run here", none of them the user
            # having said no.
            raise BackendUnavailable(str(exc)) from exc
        self.restore_token = session.restore_token
        return session

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
        """End emulation on both devices, and release everything held.

        The portal session goes last, and only if this backend negotiated
        one: closing it is what tears the EIS connection down, so the
        devices have to stop emulating first. Nothing closed it before this
        module delegated to `libei.portal` -- a portal session outlives the
        object that created it, living in xdg-desktop-portal until
        `Session.Close()` or the D-Bus connection drops, and that
        connection is GLib's shared session-bus singleton, which does not.
        """
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
        if self._session is not None:
            self._session.close()
            self._session = None
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
