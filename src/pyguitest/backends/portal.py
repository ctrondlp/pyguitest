"""Input injection via the RemoteDesktop XDG desktop portal.

The audit's other named gap, alongside libei: `org.freedesktop.portal.RemoteDesktop`
is the cross-desktop, unprivileged, consent-based input path -- no /dev/uinput,
no CLI tool, works the same on GNOME and KDE. `NotifyKeyboardKeysym` in
particular sends an X11 keysym value directly rather than a keycode, so the
*compositor* resolves it against whatever layout is actually active -- the
same keymap-safety property libei promises, over an interface every major
desktop already ships a portal backend for.

Scope actually implemented: keyboard (press_key/release_key/type_text),
pointer buttons, and scroll. Deliberately NOT move_mouse: absolute pointer
positioning over this portal needs a PipeWire stream id
(NotifyPointerMotionAbsolute's `stream` argument), which only exists after
negotiating a *second*, separate consent dialog with the ScreenCast portal --
materially bigger scope than this pass. Relative motion
(NotifyPointerMotion) needs no stream, but this package's move_mouse
contract is absolute, and there is no query to establish a starting point to
compute a relative delta from either.

Every method signature below is transcribed from the actual RemoteDesktop
portal XML (org.freedesktop.portal.RemoteDesktop.xml in the
xdg-desktop-portal source), not assumed. The CreateSession/SelectDevices/
Start negotiation has now been run against a real xdg-desktop-portal
(1.22.1, Fedora 44/GNOME) and completes; the keyboard/pointer/scroll
methods past that point still haven't been -- Start() needs a human
physically present to click Allow, which this got, but exercising what
comes after is still an open gap.

`CreateSession` passes a `session_handle_token` for a real, load-bearing
reason, not tidiness: omitting it crashes xdg-desktop-portal 1.22.1
outright (SIGABRT, `assertion failed: (session->token != NULL)` in
xdp-session.c) rather than returning an error, taking the portal down for
every app using it system-wide until systemd restarts it. Confirmed live
by reproducing the crash with a bare `gdbus call` (nothing pyguitest-side
involved) and then fixing it the same way here. Do not remove this option.
"""

from __future__ import annotations

import time
import uuid

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, PermissionRequired, PyGUITestError
from . import portalrequest as _portalrequest
from .base import GUIBackend

__all__ = ["PortalBackend", "available"]

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_INTERFACE = "org.freedesktop.portal.RemoteDesktop"

# AvailableDeviceTypes / SelectDevices bitmask, per the portal's own spec.
_DEVICE_KEYBOARD = 1
_DEVICE_POINTER = 2

_PRESSED = 1
_RELEASED = 0

# SelectDevices `persist_mode`, per the RemoteDesktop XML.
PERSIST_NONE = 0
"""Do not persist permissions; every session prompts. The default."""
PERSIST_WHILE_RUNNING = 1
"""Permissions persist as long as the application is running."""
PERSIST_UNTIL_REVOKED = 2
"""Permissions persist until the user explicitly revokes them."""

# Evdev button codes -- NotifyPointerButton is documented as taking these,
# the same numbering ToolInputBackend's ydotool adapter and UinputBackend
# both already use for the same reason.
_BUTTONS = {1: 0x110, 2: 0x112, 3: 0x111}  # BTN_LEFT, BTN_MIDDLE, BTN_RIGHT

# X11 keysym values for the names GUIBackend.MODIFIER_KEYS/KEY_ALIASES use by
# default -- verified against X11/keysymdef.h, not assumed. NotifyKeyboardKeysym
# takes a keysym directly, so this backend needs no override of those tables
# or of resolve_char_key: the base class's X11-style names and single-character
# ASCII/Latin-1/Unicode fallback already name exactly what this needs.
_NAMED_KEYSYMS = {
    "BackSpace": 0xFF08,
    "Tab": 0xFF09,
    "Return": 0xFF0D,
    "Pause": 0xFF13,
    "Scroll_Lock": 0xFF14,
    "Escape": 0xFF1B,
    "Delete": 0xFFFF,
    "Home": 0xFF50,
    "Left": 0xFF51,
    "Up": 0xFF52,
    "Right": 0xFF53,
    "Down": 0xFF54,
    "Prior": 0xFF55,
    "Next": 0xFF56,
    "End": 0xFF57,
    "Print": 0xFF61,
    "Insert": 0xFF63,
    "Menu": 0xFF67,
    "Cancel": 0xFF69,
    "Help": 0xFF6A,
    "Break": 0xFF6B,
    "Num_Lock": 0xFF7F,
    "F1": 0xFFBE,
    "F2": 0xFFBF,
    "F3": 0xFFC0,
    "F4": 0xFFC1,
    "F5": 0xFFC2,
    "F6": 0xFFC3,
    "F7": 0xFFC4,
    "F8": 0xFFC5,
    "F9": 0xFFC6,
    "F10": 0xFFC7,
    "F11": 0xFFC8,
    "F12": 0xFFC9,
    "Shift_L": 0xFFE1,
    "Shift_R": 0xFFE2,
    "Control_L": 0xFFE3,
    "Control_R": 0xFFE4,
    "Caps_Lock": 0xFFE5,
    "Meta_L": 0xFFE7,
    "Meta_R": 0xFFE8,
    "Alt_L": 0xFFE9,
    "Alt_R": 0xFFEA,
    "Super_L": 0xFFEB,
    "Super_R": 0xFFEC,
    "ISO_Level3_Shift": 0xFE03,
    "space": 0x0020,
}


def _keysym_for_name(name: str) -> int:
    """An X11 keysym value for a key name or single character.

    Named keys come from the table above. A single character falls back to
    its own codepoint for Latin-1 (X11 keysym values equal the codepoint by
    definition in that range, the same rule X11Backend's _char_keysym
    uses), or the Unicode keysym form for anything past it.
    """
    if name in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[name]
    if len(name) == 1:
        codepoint = ord(name)
        if 0x20 <= codepoint <= 0xFF:
            return codepoint
        return 0x01000000 | codepoint
    raise ValueError(f"unknown key name {name!r}")


def _gio():
    """Import Gio and GLib, or return None.

    Same PyGObject dependency the atspi extra already needs -- see
    docs/adr-001-dependencies.md.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    return Gio, GLib


def available():
    """Whether the library this backend needs is importable."""
    return _gio() is not None


class PortalBackend(GUIBackend):
    """Keyboard and pointer button/scroll injection via the portal."""

    name = "portal"

    def __init__(
        self,
        connection=None,
        session_handle=None,
        restore_token=None,
        persist_mode=PERSIST_NONE,
    ):
        """Connect to the session bus and negotiate a RemoteDesktop session.

        Accepts an injected `connection` and/or `session_handle` for tests --
        passing `session_handle` skips negotiation (and so the consent
        dialog) entirely, which is the only way to unit test anything past
        construction without a live portal and a human to click Allow.

        `persist_mode` and `restore_token` are how a caller avoids being
        prompted on every launch. Ask for persistence with
        `persist_mode=PERSIST_UNTIL_REVOKED`, then read `self.restore_token`
        after construction and hand it back as `restore_token=` next time;
        the portal recognises the prior grant and skips straight past the
        dialog. Each successful restore mints a *new* token, so always save
        the one from the latest session rather than reusing the old one.

        This deliberately never writes the token anywhere. A restore_token
        is a standing grant of keyboard and pointer injection to whoever
        presents it -- a credential, not a convenience flag -- so where it
        is stored is the caller's decision to make, not this library's.
        Nothing persists unless a caller opts in by asking for a
        persist_mode and saving what comes back.
        """
        modules = _gio()
        if modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs (see README)"
            )
        self._Gio, self._GLib = modules
        if connection is not None:
            self._connection = connection
        else:
            try:
                self._connection = self._Gio.bus_get_sync(
                    self._Gio.BusType.SESSION, None
                )
            except Exception as exc:
                raise BackendUnavailable(
                    f"cannot reach the session bus: {exc}"
                ) from exc
        self._persist_mode = persist_mode
        self._restore_token = restore_token
        self.restore_token = None
        """The token to reuse next time, or None if the portal issued none.

        Populated from Start()'s reply. Only ever non-None when a
        persist_mode was requested and the portal honoured it."""
        self._session_handle = session_handle or self._negotiate_session()

    # -- portal request/response plumbing ----------------------------------

    def _call(self, method, signature, args):
        """Call one RemoteDesktop method, returning its raw GVariant reply."""
        return _portalrequest.call(
            (self._Gio, self._GLib),
            self._connection,
            _INTERFACE,
            method,
            signature,
            args,
        )

    def _request(self, method, signature, args):
        """Call a method that returns a Request handle, and await its reply.

        Every RemoteDesktop method that can show UI (CreateSession,
        SelectDevices, Start) works this way, and so does every method on
        every other portal interface -- so the plumbing, including the
        subscribe-before-calling race fix its docstring explains, lives in
        portalrequest.py and is shared with the Screenshot portal backend
        rather than written twice.
        """
        return _portalrequest.request(
            (self._Gio, self._GLib),
            self._connection,
            _INTERFACE,
            method,
            signature,
            args,
        )

    def _negotiate_session(self):
        """CreateSession, SelectDevices, Start -- the one-time consent flow.

        Start is what actually raises the dialog; nothing here can complete
        without a human present to answer it.
        """
        session_token = self._GLib.Variant("s", uuid.uuid4().hex)
        code, results = self._request(
            "CreateSession",
            "(a{sv})",
            ({"session_handle_token": session_token},),
        )
        if code != 0:
            raise PermissionRequired(
                Capability.KEY_EVENT, self.name, "CreateSession was not approved"
            )
        session_handle = results["session_handle"]

        options = {"types": self._GLib.Variant("u", _DEVICE_KEYBOARD | _DEVICE_POINTER)}
        if self._persist_mode != PERSIST_NONE:
            options["persist_mode"] = self._GLib.Variant("u", self._persist_mode)
        if self._restore_token is not None:
            options["restore_token"] = self._GLib.Variant("s", self._restore_token)
        code, _results = self._request(
            "SelectDevices",
            "(oa{sv})",
            (session_handle, options),
        )
        if code != 0:
            raise PermissionRequired(
                Capability.KEY_EVENT, self.name, "SelectDevices was not approved"
            )

        code, results = self._request("Start", "(osa{sv})", (session_handle, "", {}))
        if code != 0:
            raise PermissionRequired(
                Capability.KEY_EVENT,
                self.name,
                "the user declined the remote-control consent dialog",
            )
        # Save this in place of the token that was presented: the portal may
        # answer with a different one, and a caller that keeps reusing the
        # original would eventually present a stale token and be prompted
        # again. (On GNOME the same token comes back each restore -- one
        # portal's behaviour, not a guarantee.) Absent whenever no
        # persist_mode was asked for.
        self.restore_token = results.get("restore_token")
        return session_handle

    @property
    def capabilities(self):
        """Keyboard and pointer button/scroll; no POINTER_MOVE (see above)."""
        return CapabilitySet(
            {
                Capability.POINTER_BUTTON,
                Capability.POINTER_SCROLL,
                Capability.KEY_EVENT,
                Capability.TEXT_ENTRY,
            }
        )

    # -- pointer -----------------------------------------------------------

    def _notify_button(self, button, state):
        try:
            code = _BUTTONS[button]
        except KeyError:
            raise ValueError(f"unsupported button {button!r}") from None
        self._call(
            "NotifyPointerButton",
            "(oa{sv}iu)",
            (self._session_handle, {}, code, state),
        )

    def press_button(self, button):
        """Press a mouse button. 1 is left, 2 middle, 3 right."""
        self.require(Capability.POINTER_BUTTON)
        self._notify_button(button, _PRESSED)

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._notify_button(button, _RELEASED)

    def scroll(self, dx=0, dy=0):
        """Scroll by axis steps, on whichever axes are non-zero."""
        self.require(Capability.POINTER_SCROLL)
        if dy:
            self._call(
                "NotifyPointerAxisDiscrete",
                "(oa{sv}ui)",
                (self._session_handle, {}, 0, int(dy)),
            )
        if dx:
            self._call(
                "NotifyPointerAxisDiscrete",
                "(oa{sv}ui)",
                (self._session_handle, {}, 1, int(dx)),
            )

    # -- keyboard ----------------------------------------------------------

    def _notify_keysym(self, keysym, state):
        self._call(
            "NotifyKeyboardKeysym",
            "(oa{sv}iu)",
            (self._session_handle, {}, keysym, state),
        )

    def press_key(self, key):
        """Press a key by name."""
        self.require(Capability.KEY_EVENT)
        self._notify_keysym(_keysym_for_name(key), _PRESSED)

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        self._notify_keysym(_keysym_for_name(key), _RELEASED)

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Sends each character's own keysym directly -- unlike every other
        keyboard-capable backend, no explicit shift press is needed here:
        resolving an abstract keysym into whatever physical key and modifier
        state produces it is the compositor's job for a keysym-based
        virtual device, which is the entire reason this is the one backend
        that does not need `resolve_char_key`'s (name, needs_shift) split.
        `allow_keymap_unsafe` is accepted for signature compatibility with
        every other backend's type_text, but this path is keymap-*safe* by
        construction, so there is nothing for it to refuse.
        """
        self.require(Capability.TEXT_ENTRY)
        for char in text:
            try:
                keysym = _keysym_for_name(char)
            except ValueError as exc:
                raise PyGUITestError(f"cannot type character {char!r}: {exc}") from exc
            self._notify_keysym(keysym, _PRESSED)
            self._notify_keysym(keysym, _RELEASED)
            if delay:
                time.sleep(delay)
