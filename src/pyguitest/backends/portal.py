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

import contextlib
import os
import threading
import time
import uuid

from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PermissionRequired,
    PyGUITestError,
)
from . import portalrequest as _portalrequest
from .base import GUIBackend

__all__ = ["PortalBackend", "available"]

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
_CLIPBOARD_INTERFACE = "org.freedesktop.portal.Clipboard"

_CLIPBOARD_MIME = "text/plain;charset=utf-8"
"""The one MIME type this backend reads and offers.

`Session.get_clipboard`/`set_clipboard` are text-only by signature, so
advertising more would promise something the API above cannot express."""

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

_CONTROL_KEYSYMS = {
    "\n": "Return",
    "\r": "Return",
    "\t": "Tab",
    "\b": "BackSpace",
    "\x1b": "Escape",
}
"""Control characters, mapped to the key they mean. The same table
X11Backend keeps, and for the same reason.

A control character has no keysym of its own in the Latin-1 range the
fallback below covers, so without this `type_text("hello\n")` sent
`0x01000000 | 10` -- the Unicode keysym form of U+000A, which names no
key on any keymap -- instead of Return, and the newline every caller
means as "press Enter" did nothing. uinput maps `"\n"` to ENTER and
X11Backend maps it to Return already; this is the third backend agreeing
with them rather than a new convention."""


def _keysym_for_name(name: str) -> int:
    """An X11 keysym value for a key name or single character.

    Named keys come from the table above, then the control characters
    beside it. A single character otherwise falls back to its own
    codepoint for Latin-1 (X11 keysym values equal the codepoint by
    definition in that range, the same rule X11Backend's _char_keysym
    uses), or the Unicode keysym form for anything past it.
    """
    if name in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[name]
    if name in _CONTROL_KEYSYMS:
        return _NAMED_KEYSYMS[_CONTROL_KEYSYMS[name]]
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
        clipboard=False,
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
        self._clipboard = clipboard
        self._content = b""
        """What set_clipboard last put up, served on every paste."""
        self._serving = False
        self._loop = None
        self._thread = None
        self._transfer_subscription = None
        """Whether this session asked the portal for clipboard access.

        Off by default: it widens what the one consent dialog grants, and
        a caller injecting input has no use for it. On GNOME it is the
        only clipboard path this package has at all -- Mutter implements
        no wlr-data-control, so wl-clipboard cannot serve it (see
        backends/clipboard.py)."""
        self.restore_token = None
        """The token to reuse next time, or None if the portal issued none.

        Populated from Start()'s reply. Only ever non-None when a
        persist_mode was requested and the portal honoured it."""
        # An injected handle belongs to whoever injected it: `close()` ends
        # only a session this backend negotiated itself, so a caller (or a
        # test) sharing one session across backends does not have it closed
        # out from under them by the first of them to be torn down.
        self._owns_session = session_handle is None
        self._session_handle = session_handle or self._negotiate_session()

    # -- portal request/response plumbing ----------------------------------

    def _call(self, method, signature, args, interface=_INTERFACE):
        """Call one portal method, returning its raw GVariant reply.

        `interface` defaults to RemoteDesktop, which is all this backend
        spoke originally. The clipboard half addresses
        org.freedesktop.portal.Clipboard instead -- a different interface
        on the same object path, bound to the same session.
        """
        if self._session_handle is None:
            raise PyGUITestError(
                "the portal session is closed; construct another "
                "PortalBackend to inject input again"
            )
        return _portalrequest.call(
            (self._Gio, self._GLib),
            self._connection,
            interface,
            method,
            signature,
            args,
        )

    def _call_for_fd(self, method, signature, args):
        """Call a Clipboard method that answers with a fd, and return it.

        The `h` in a portal signature is an *index* into a UnixFDList
        delivered on a side channel, not a descriptor -- so this needs
        `call_with_unix_fd_list_sync` (which answers `(reply, fd_list)`)
        and an explicit reply type, exactly as ConnectToEIS does. Reading
        the int and treating it as a fd would address whatever this
        process happens to have open at that number.

        The fd that comes back is owned -- `g_unix_fd_list_get()` dups it
        -- so the caller closes it.
        """
        if self._session_handle is None:
            raise PyGUITestError(
                "the portal session is closed; construct another "
                "PortalBackend to use the clipboard again"
            )
        reply, fd_list = self._connection.call_with_unix_fd_list_sync(
            _BUS_NAME,
            _OBJECT_PATH,
            _CLIPBOARD_INTERFACE,
            method,
            self._GLib.Variant(signature, args),
            self._GLib.VariantType.new("(h)"),
            self._Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        (index,) = reply.unpack()
        return fd_list.get(index)

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
        try:
            return self._select_and_start(session_handle)
        except BaseException:
            # CreateSession has already created a session inside the portal,
            # and from here nothing else can close it: __init__ raises rather
            # than returning the object whose close() would, and the session
            # outlives the failure on the shared session-bus connection.
            # BaseException, not Exception: Start blocks on a human answering
            # the dialog, so Ctrl-C during that wait is a routine way out --
            # and it strands an approved session exactly as a decline does.
            _portalrequest.close_session(
                (self._Gio, self._GLib), self._connection, session_handle
            )
            raise

    def _select_and_start(self, session_handle):
        """Ask for devices and start the session, returning its handle.

        Split out of `_negotiate_session` only so the caller can wrap the
        whole of it in one `try`: every step here can fail, and each of
        those failures leaves the same session behind to be closed.
        """
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

        # Before Start, and the ordering is the whole constraint: the
        # portal binds clipboard access to the session at Start time, so
        # requesting it afterwards is ignored -- the session comes up
        # without access and every Selection* call then fails against a
        # session that was never granted any. Not a Request/Response
        # method either (no out-arguments, answers immediately), so this
        # is a plain call. It goes through _portalrequest directly rather
        # than self._call, whose closed-session guard reads
        # self._session_handle -- not assigned until this method returns.
        if self._clipboard:
            _portalrequest.call(
                (self._Gio, self._GLib),
                self._connection,
                _CLIPBOARD_INTERFACE,
                "RequestClipboard",
                "(oa{sv})",
                (session_handle, {}),
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
        """Keyboard and pointer button/scroll; no POINTER_MOVE (see above).

        CLIPBOARD only when `clipboard=True` was asked for at
        construction: the portal grants it at Start, so a session that
        did not request it cannot acquire it later, and declaring the
        capability regardless would promise what this session was never
        granted.
        """
        provided = {
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
        }
        if self._clipboard:
            provided.add(Capability.CLIPBOARD)
        return CapabilitySet(provided)

    def close(self):
        """End the portal session this backend negotiated.

        The session lives in xdg-desktop-portal, not here, and outlives
        this object: without `Session.Close()` it stays a standing input
        grant until the D-Bus connection that created it drops -- and that
        connection is GLib's shared session-bus singleton, which does not
        drop when a backend is discarded. A process that builds backends
        repeatedly (a test suite, a long-running driver) would otherwise
        leave one live session behind per backend.

        Idempotent, and a no-op for a session that was injected rather than
        negotiated here: that one belongs to whoever passed it in. After
        this, injection raises rather than being sent to a dead session.
        """
        # Before the session: the service thread answers pastes *on* it,
        # and a transfer racing the Close would address a dead session.
        # Unconditional, unlike the session teardown below -- the thread
        # is this object's own either way, injected session or not.
        self._stop_serving()
        if self._session_handle is None or not self._owns_session:
            return
        handle, self._session_handle = self._session_handle, None
        _portalrequest.close_session((self._Gio, self._GLib), self._connection, handle)

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

    # -- clipboard ---------------------------------------------------------

    def _no_primary(self, primary):
        """Refuse PRIMARY, which this interface does not have.

        The portal's Clipboard interface addresses one selection -- the
        clipboard proper. There is no PRIMARY in it at all, so a
        `primary=True` served quietly from the clipboard would answer a
        different question than the one asked, and the two selections are
        independent by design. Every other clipboard backend here really
        does offer both (see backends/clipboard.py), so the honest answer
        is a typed refusal rather than a silent substitution.
        """
        if primary:
            raise CapabilityUnsupported(
                Capability.CLIPBOARD,
                self.name,
                "the Clipboard portal has no PRIMARY selection; it "
                "addresses the clipboard proper only",
            )

    def get_clipboard(self, primary=False):
        """The clipboard's current text, read through the portal.

        `SelectionRead` hands back a pipe the portal writes the current
        owner's content into; this reads it to EOF and decodes it. One
        MIME type is asked for (see _CLIPBOARD_MIME) rather than
        negotiating, because the API above is text-only.
        """
        self.require(Capability.CLIPBOARD)
        self._no_primary(primary)
        fd = self._call_for_fd(
            "SelectionRead", "(os)", (self._session_handle, _CLIPBOARD_MIME)
        )
        with os.fdopen(fd, "rb", closefd=True) as handle:
            return handle.read().decode("utf-8", errors="replace")

    def set_clipboard(self, text, primary=False):
        """Put `text` on the clipboard, and keep serving it.

        This is the half with a lifetime. `SetSelection` only declares
        *ownership*: no content travels with it, and the portal comes back
        later -- once per paste -- with a `SelectionTransfer` signal naming
        a MIME type and a serial. Answering means `SelectionWrite` for a
        pipe, the bytes, then `SelectionWriteDone`. Miss those and the
        clipboard reads as empty to everything on the desktop.

        So something has to be listening for as long as this process owns
        the selection, which is exactly the problem `wl-copy` and `xclip`
        solve by forking a daemon on write (see backends/clipboard.py's
        module docstring, which found that the hard way). This backend
        cannot fork -- the session belongs to this process -- so it runs a
        GLib main loop on a daemon thread instead, started on the first
        `set_clipboard` and living until `close()`.

        The practical consequence is worth stating plainly: the clipboard
        holds only while the Session does. A script that sets the
        clipboard and exits leaves nothing behind, where the CLI backends
        leave a daemon that outlives them.
        """
        self.require(Capability.CLIPBOARD)
        self._no_primary(primary)
        self._content = text.encode("utf-8")
        self._start_serving()
        self._call(
            "SetSelection",
            "(oa{sv})",
            (
                self._session_handle,
                {"mime_types": self._GLib.Variant("as", [_CLIPBOARD_MIME])},
            ),
            interface=_CLIPBOARD_INTERFACE,
        )

    def _start_serving(self):
        """Subscribe to SelectionTransfer and pump a loop for it, once.

        The thread is a daemon and the loop is its own MainContext rather
        than the default one: a caller may be running a main loop of their
        own on the main thread (a GTK application driving this in-process
        is not far-fetched), and stealing the default context would
        deliver their signals here instead.
        """
        if self._serving:
            return
        self._serving = True
        context = self._GLib.MainContext.new()
        context.push_thread_default()
        try:
            self._transfer_subscription = self._connection.signal_subscribe(
                _BUS_NAME,
                _CLIPBOARD_INTERFACE,
                "SelectionTransfer",
                _OBJECT_PATH,
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_transfer,
                None,
            )
        finally:
            context.pop_thread_default()
        self._loop = self._GLib.MainLoop.new(context, False)
        self._thread = threading.Thread(
            target=self._serve,
            args=(self._loop,),
            name="pyguitest-clipboard",
            daemon=True,
        )
        self._thread.start()

    def _serve(self, loop):
        """Run the clipboard loop on its own context, on its own thread.

        Takes the loop as an argument rather than reading `self._loop`:
        `_stop_serving` clears that attribute, and a thread still winding
        down would otherwise read None off it and raise on the way out.
        """
        context = loop.get_context()
        context.push_thread_default()
        try:
            loop.run()
        finally:
            context.pop_thread_default()

    def _on_transfer(self, _conn, _sender, _path, _iface, _signal, params, *_args):
        """Answer one paste request with the content we last were given.

        Deliberately total: this runs on the service thread, where an
        exception would kill the loop and silently stop the clipboard
        working for the rest of the session, with nothing to report it to.
        A failed transfer is reported to the portal instead --
        `SelectionWriteDone(success=False)` is what the interface documents
        for a request that cannot be handled.
        """
        try:
            session_handle, _mime_type, serial = params.unpack()
        except Exception:  # noqa: BLE001 -- see above
            return
        if session_handle != self._session_handle:
            return  # another session's paste, on a shared connection
        success = False
        try:
            fd = self._call_for_fd(
                "SelectionWrite", "(ou)", (self._session_handle, serial)
            )
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(self._content)
            success = True
        except Exception:  # noqa: BLE001 -- see above
            success = False
        with contextlib.suppress(Exception):  # see above
            self._call(
                "SelectionWriteDone",
                "(oub)",
                (self._session_handle, serial, success),
                interface=_CLIPBOARD_INTERFACE,
            )

    def _stop_serving(self):
        """Tear the clipboard service down. Idempotent, and never raises."""
        if self._transfer_subscription is not None:
            with contextlib.suppress(Exception):
                self._connection.signal_unsubscribe(self._transfer_subscription)
            self._transfer_subscription = None
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.quit()
            self._loop = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._serving = False

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
