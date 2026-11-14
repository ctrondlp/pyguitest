"""The X11 backend, over python-xlib.

Not a legacy path. It is the surest route to the FreeBSD and Solaris support
X11::GUITest has today -- python-xlib speaks the wire protocol in pure Python,
so it assumes no kernel -- and it is the only backend that can serve the
tier-6 capabilities at all.

It is not the *sole* route off Linux, though, and the README no longer claims
it is: /dev/uinput and libei are kernel interfaces, but compositor IPC is a
unix socket and JSON, and sway, grim and wtype are all in FreeBSD ports. A
wlroots session there would get window IPC, capture and keymap-safe typing
without this backend. Nothing in the package gates on the platform -- every
mechanism is probed -- and nothing off Linux is tested either, so treat all
of it as reasoned rather than demonstrated.

The tier-6 point is the reason the tier scale is documented as a *Wayland*
ceiling rather than an absolute one. Reading the global pointer position or the
keyboard state is impossible for an ordinary Wayland client on any compositor,
but under X11 it is a single round trip. Same API, larger capability set --
which is exactly what the negotiation surface exists to express.

This backend serves all five tier-6 capabilities, including
WINDOW_CURSOR_QUERY: python-xlib does bind XTestCompareCursorWithWindow, as
`window.xtest_compare_cursor`. Every one of the 50 X11::GUITest exports
therefore has a path here.

It is also the only backend that captures a *window* rather than a
rectangle of the screen. GetImage takes any drawable, so pointing it at the
window itself asks the server for that window's own pixels instead of
whatever happens to be stacked on top of them at those coordinates -- which
is what cropping a full-screen shot down to a window's geometry actually
gets you everywhere else.
"""

import contextlib
import os
import tempfile
import time
from typing import Literal

from .. import png as _png
from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported, WindowNotFound
from .base import GUIBackend, Screen, Window, check_region, fit_region

__all__ = ["X11Backend", "available"]

# Control characters with no printable keysym name that string_to_keysym can
# find on its own; typed here as their proper X11 keysym name.
_CONTROL_KEYSYMS = {
    "\n": "Return",
    "\r": "Return",
    "\t": "Tab",
    "\b": "BackSpace",
    "\x1b": "Escape",
}


def _xlib():
    """Import python-xlib, or return None.

    Also loads the 'xkb' keysym group into `Xlib.XK`, which the module does
    not do on its own: it auto-loads only 'miscellany' and 'latin1' at
    import time, and ISO_Level3_Shift -- the AltGr key most layouts use for
    keyboard group 2 -- lives in 'xkb'. Confirmed live, on python-xlib
    0.33: without this, `XK.string_to_keysym("ISO_Level3_Shift")` returns
    NoSymbol, which silently broke two things at once -- `MODIFIER_KEYS
    ["&"]` for `send_keys()`'s explicit AltGr syntax, and this backend's own
    `_group_switch_keycode()` -- neither of which had ever actually reached
    a real server. A load failure is swallowed rather than raised: both of
    those callers already treat an unresolvable keysym name as "this
    server or library has no such key", not as a hard error, and calling
    it again costs nothing worth guarding against -- it only re-copies
    names into a module dict.
    """
    try:
        from Xlib import XK, X, display
        from Xlib.ext import xtest
        from Xlib.protocol import event
    except Exception:
        return None
    with contextlib.suppress(Exception):
        XK.load_keysym_group("xkb")
    return X, XK, display, xtest, event


def available():
    """Whether the library this backend needs is importable."""
    return _xlib() is not None


class X11Backend(GUIBackend):
    """Window control and input over the X protocol."""

    name = "x11"

    def __init__(self, environment=None, display_name=None):
        """Open the X display and confirm the XTEST extension is present."""
        modules = _xlib()
        if modules is None:
            raise BackendUnavailable(
                "python-xlib is not installed; pip install 'pyguitest[x11]'"
            )
        self._X, self._XK, self._display_mod, self._xtest, self._xevent = modules
        self.environment = environment
        self._cursor_font = None
        self._cursors = {}
        try:
            self._display = self._display_mod.Display(display_name)
        except Exception as exc:
            raise BackendUnavailable(f"cannot open X display: {exc}") from exc
        if not self._display.query_extension("XTEST"):
            self._display.close()
            raise BackendUnavailable("the X server has no XTEST extension")

    def close(self):
        """Close the X display connection."""
        self._display.close()

    @property
    def _is_xwayland(self):
        """Whether this X connection is XWayland inside a Wayland session.

        None (no environment given) is treated as a real X server, which
        is what a directly-constructed backend has historically assumed.
        """
        from ..session import SessionType

        return (
            self.environment is not None
            and self.environment.session_type is SessionType.XWAYLAND
        )

    @property
    def capabilities(self):
        """Everything except elements, including all of tier 6.

        Minus screen capture under XWayland. That is the same trap
        `tools.py` already encodes as `x11_only`: an X connection exists
        and works, but it cannot see the Wayland session around it.
        Native Wayland surfaces are never composited into the X root
        window, so GetImage on the root cannot return the desktop --
        observed live on GNOME 50 as BadMatch, though the more dangerous
        outcome would have been success, since an empty X root is a
        perfectly valid image of entirely the wrong thing.

        WINDOW_CAPTURE stays, and this is measured rather than reasoned:
        scripts/diagnose-x11-capture.py on that same GNOME 50 session
        reads an X11 client's own drawable successfully (depth 24, 4 bytes
        for one pixel) in the very run where every root request fails. An
        XWayland-backed client has real content of its own; only the root
        is unreadable. This backend also only ever receives windows it
        issued itself -- see CompositeBackend._issuer.
        """
        capabilities = {
            Capability.SCREEN_INFO,
            Capability.SCREEN_CAPTURE,
            Capability.WINDOW_CAPTURE,
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_RESIZE,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_MINIMIZE,
            Capability.WINDOW_MAXIMIZE,
            Capability.WINDOW_CLOSE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
            # Impossible on Wayland, ordinary here.
            Capability.POINTER_QUERY,
            Capability.INPUT_STATE_QUERY,
            Capability.WINDOW_TITLE_SET,
            Capability.WINDOW_LOWER,
            Capability.WINDOW_CURSOR_QUERY,
        }
        if self._is_xwayland:
            capabilities.discard(Capability.SCREEN_CAPTURE)
        return CapabilitySet(capabilities)

    # -- screens -----------------------------------------------------------

    def screens(self):
        """Every X screen, with its current pixel size.

        The size comes from the root window rather than from
        `width_in_pixels`, for the reason spelled out in _drawable_size:
        the latter is part of the connection setup, sent once and never
        revised, so it reports whatever the screen was when the process
        started. A RandR resize -- plugging in a monitor, changing
        resolution, or a VM window being dragged -- leaves it wrong for
        the life of the connection.

        It cost a live BadMatch in capture() before it was understood, and
        the same staleness here is quieter and worse: a caller centring a
        click from these numbers gets no error at all, just a click in the
        wrong place.

        Falls back to the setup values if the root cannot be read, since a
        stale size still beats no answer for a purely informational call.
        """
        self.require(Capability.SCREEN_INFO)
        screens = []
        for index in range(self._display.screen_count()):
            screen = self._display.screen(index)
            width, height = screen.width_in_pixels, screen.height_in_pixels
            try:
                geometry = screen.root.get_geometry()
            except Exception:
                pass
            else:
                width, height = geometry.width, geometry.height
            screens.append(
                Screen(index=index, width=width, height=height, name=f"screen{index}")
            )
        return screens

    # -- input -------------------------------------------------------------

    def _sync(self):
        """Flush pending requests so injected events are delivered."""
        self._display.sync()

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position."""
        self.require(Capability.POINTER_MOVE)
        self._display.xtest_fake_input(self._X.MotionNotify, x=x, y=y)
        self._sync()

    def press_button(self, button):
        """Press a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._display.xtest_fake_input(self._X.ButtonPress, button)
        self._sync()

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._display.xtest_fake_input(self._X.ButtonRelease, button)
        self._sync()

    def scroll(self, dx=0, dy=0):
        """Scroll is buttons 4-7 under X11, unlike Wayland's axis events.

        4/5 are vertical (up/down), 6/7 horizontal (left/right). Each axis
        clicks its own button `abs(steps)` times, so a pure-horizontal
        request (dy=0) never touches the vertical buttons and a magnitude
        greater than one step is not silently collapsed to a single click.
        """
        self.require(Capability.POINTER_SCROLL)
        if dy:
            button = 4 if dy > 0 else 5
            for _ in range(abs(dy)):
                self.press_button(button)
                self.release_button(button)
        if dx:
            button = 7 if dx > 0 else 6
            for _ in range(abs(dx)):
                self.press_button(button)
                self.release_button(button)

    def _keycode(self, key):
        """Translate a key name to the server's current keycode for it."""
        keysym = self._XK.string_to_keysym(key)
        if keysym == 0:
            raise ValueError(f"unknown key name {key!r}")
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise CapabilityUnsupported(
                Capability.KEY_EVENT,
                self.name,
                f"{key!r} has no keycode in the server's current keymap",
            )
        return keycode

    def press_key(self, key):
        """Press a key by name."""
        self.require(Capability.KEY_EVENT)
        self._display.xtest_fake_input(self._X.KeyPress, self._keycode(key))
        self._sync()

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        self._display.xtest_fake_input(self._X.KeyRelease, self._keycode(key))
        self._sync()

    def _char_keysym(self, char):
        r"""Resolve one character to the keysym that types it.

        A name lookup first, so control characters resolve to their proper
        keysym name (`\n` is the Return keysym, not codepoint 10, which
        `string_to_keysym` cannot see since it is not a printable name). Then
        the printable-character form: `string_to_keysym` for anything with a
        named keysym, else the Latin-1 codepoint directly for U+0020-U+00FF
        (those keysym values equal the codepoint by definition), else the
        Unicode keysym form `0x01000000 | codepoint` for anything above that.
        """
        name = _CONTROL_KEYSYMS.get(char)
        if name is not None:
            return self._XK.string_to_keysym(name)
        keysym = self._XK.string_to_keysym(char)
        if keysym:
            return keysym
        codepoint = ord(char)
        return codepoint if codepoint <= 0xFF else 0x01000000 | codepoint

    def _keysym_keycode(self, name):
        """keysym_to_keycode for a key name, or None if the server has no such key."""
        keysym = self._XK.string_to_keysym(name)
        if not keysym:
            return None
        return self._display.keysym_to_keycode(keysym) or None

    def _group_switch_keycode(self):
        """Whatever key switches to keyboard group 2 on this server, or None.

        `get_keyboard_mapping`'s third and fourth slots per keycode are
        group 2 -- reached by holding ISO_Level3_Shift under XKB (the AltGr
        key on most layouts, and what `MODIFIER_KEYS["&"]` already presses
        for `send_keys()`), or the older core-protocol Mode_switch on a
        server with no XKB key of that name.
        """
        return self._keysym_keycode("ISO_Level3_Shift") or self._keysym_keycode(
            "Mode_switch"
        )

    def _resolve_char(self, char):
        """Return (keycode, modifier keycodes) for `char`.

        The modifiers come from which of the four legacy-protocol shift
        levels `get_keyboard_mapping` assigned the keysym to: level 0 needs
        none, level 1 needs Shift, and levels 2/3 -- keyboard group 2 --
        need whatever key switches groups (see _group_switch_keycode), with
        Shift added too at level 3. Holding Shift for every non-zero level,
        as this used to, typed the wrong character for anything in group 2
        -- an AltGr-only symbol on any layout that has one.

        Raises CapabilityUnsupported for a character with no keycode at all
        in the current map, or one in group 2 on a server with no
        group-switch key to reach it, rather than pressing keycode 0 or
        typing the wrong, unshifted character.
        """
        keysym = self._char_keysym(char)
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise CapabilityUnsupported(
                Capability.TEXT_ENTRY,
                self.name,
                f"{char!r} has no keycode in the server's current keymap",
            )
        levels = self._display.get_keyboard_mapping(keycode, 1)[0]
        try:
            level = levels.index(keysym)
        except ValueError:
            level = 0
        modifiers = []
        if level in (1, 3):
            modifiers.append(self._keycode("Shift_L"))
        if level in (2, 3):
            group_switch = self._group_switch_keycode()
            if group_switch is None:
                raise CapabilityUnsupported(
                    Capability.TEXT_ENTRY,
                    self.name,
                    f"{char!r} is in keyboard group 2, and this server has "
                    "no ISO_Level3_Shift or Mode_switch key to reach it",
                )
            modifiers.append(group_switch)
        return keycode, tuple(modifiers)

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Keymap-correct by construction: each character is resolved to a
        keysym and then to whatever keycode and modifier keys the *server's
        current map* assigns it -- Shift for an ordinary shifted key, and
        the server's own group-switch key (AltGr on most layouts) for one
        in keyboard group 2, never the two confused with each other. This
        is what uinput cannot do, and why the audit ranks scancode
        injection last. `allow_keymap_unsafe` is accepted and ignored: X11
        is never keymap-unsafe, so there is nothing for it to refuse.

        Raises CapabilityUnsupported for a character with no keycode at all
        in the current map, or one whose keysym lives in a keyboard group
        this server has no switch key for -- see _resolve_char -- rather
        than pressing keycode 0 or typing the wrong character silently.
        There is no fallback here for the first case (xdotool handles it by
        temporarily remapping a spare keycode); this backend does not
        mutate the server's keyboard mapping.
        """
        self.require(Capability.TEXT_ENTRY)
        for char in text:
            keycode, modifiers = self._resolve_char(char)
            for mod in modifiers:
                self._display.xtest_fake_input(self._X.KeyPress, mod)
            self._display.xtest_fake_input(self._X.KeyPress, keycode)
            self._display.xtest_fake_input(self._X.KeyRelease, keycode)
            for mod in reversed(modifiers):
                self._display.xtest_fake_input(self._X.KeyRelease, mod)
            if delay:
                self._sync()
                time.sleep(delay)
        self._sync()

    # -- input state: impossible on Wayland, ordinary here -----------------

    def pointer_position(self):
        """The global pointer position. Replaces GetMousePos."""
        self.require(Capability.POINTER_QUERY)
        pointer = self._display.screen().root.query_pointer()
        return (pointer.root_x, pointer.root_y)

    def is_button_pressed(self, button):
        """Whether a mouse button is currently held down."""
        self.require(Capability.INPUT_STATE_QUERY)
        mask = self._display.screen().root.query_pointer().mask
        return bool(mask & (1 << (7 + button)))

    def is_key_pressed(self, key):
        """Replaces IsKeyPressed, via the server's 256-bit keymap vector."""
        self.require(Capability.INPUT_STATE_QUERY)
        keycode = self._keycode(key)
        keymap = self._display.query_keymap()
        return bool(keymap[keycode // 8] & (1 << (keycode % 8)))

    # -- windows -----------------------------------------------------------

    def _walk(self, window):
        """Yield a window and every descendant, iteratively.

        Fallback path for a window manager with no _NET_CLIENT_LIST_STACKING. A
        window destroyed mid-walk raises BadWindow from query_tree(); that
        branch is skipped rather than aborting the whole scan, and the
        traversal is an explicit stack rather than recursion so a deep tree
        cannot exhaust the recursion limit.
        """
        stack = [window]
        while stack:
            current = stack.pop()
            yield current
            try:
                children = current.query_tree().children
            except Exception:
                continue
            # Reversed so pop() (LIFO) still visits children left-to-right,
            # matching the original recursive traversal's order.
            stack.extend(reversed(children))

    def _client_list(self, root):
        """Every toplevel from _NET_CLIENT_LIST_STACKING, or None if unsupported.

        One round trip, and exactly the toplevel set every EWMH window
        manager already maintains -- unlike the tree walk, this cannot
        double-count one application as its decoration frame, its client,
        and a decorated child, which is what walking the raw tree does.

        Deliberately _NET_CLIENT_LIST_STACKING, not the plainer
        _NET_CLIENT_LIST: the EWMH spec only guarantees bottom-to-top
        stacking order for the _STACKING property -- the plain one is
        typically mapping order instead. window_at() depends on that
        ordering (the last match in windows() wins, as the topmost); silently
        losing it would make hit-testing pick an arbitrary overlapping
        window instead of the one actually on top.
        """
        try:
            atom = self._display.intern_atom("_NET_CLIENT_LIST_STACKING")
            prop = root.get_full_property(atom, self._X.AnyPropertyType)
            if prop is None or not prop.value:
                return None
            return [
                self._display.create_resource_object("window", wid)
                for wid in prop.value
            ]
        except Exception:
            return None

    def _title(self, window):
        """A window's title: "" if it has none, None if the window is gone.

        The two are kept apart because `windows()` treats them differently:
        an untitled window is still a window, while one whose name cannot be
        read any more has closed between the listing and this read.
        """
        try:
            return window.get_wm_name() or ""
        except Exception:
            return None

    def windows(self):
        """Every managed toplevel, titled or not, across all screens.

        Prefers _NET_CLIENT_LIST_STACKING; falls back to a full tree walk only for a
        window manager that does not maintain it, which is also the only
        path where one application can appear more than once. The walk is
        filtered to the windows a window manager would have managed -- see
        `_managed` -- so the two paths answer alike.

        An untitled window is reported with `title=""`, the way the win32
        and Wayland backends already report one, rather than dropped. It
        used to be dropped, and that made a whole class of dialog
        unreachable by anything that goes through this list: GTK4's
        `Gtk.AlertDialog` (a `dialog` with no accessible name and no WM_NAME,
        measured live on GhostBSD's marco), and a GTK3 `GtkAssistant` the
        application never titled. The accessibility backend places an
        element on screen by the compositor's listing of its window, and
        with the window missing from the list the AlertDialog's buttons
        raised CapabilityUnsupported while the assistant's were placed by
        the application's *main* window -- the only listing left with its
        pid -- and so reported wrong.
        """
        self.require(Capability.WINDOW_LIST)
        found = []
        for screen_number in range(self._display.screen_count()):
            root = self._display.screen(screen_number).root
            clients = self._client_list(root)
            if clients is not None:
                candidates = clients
            else:
                # The root is walked for its descendants but is never a
                # toplevel itself: mapped, not override-redirect, and
                # nameless, it would otherwise list as a screen-sized
                # untitled window under every point.
                candidates = [
                    child
                    for child in self._walk(root)
                    if child is not root and self._managed(child)
                ]
            for child in candidates:
                title = self._title(child)
                if title is None:
                    continue
                found.append(
                    Window(
                        handle=child,
                        backend=self,
                        title=title,
                        app_id=self._app_id(child),
                        pid=self._pid(child),
                    )
                )
        return found

    def _managed(self, window):
        """Whether a window manager would have managed this window.

        The tree walk exists to stand in for `_NET_CLIENT_LIST_STACKING` on a
        desktop whose window manager does not maintain it, so it has to
        answer the same set that property holds: the windows a window manager
        has taken on. It takes on a window only after a `MapRequest` -- so a
        window that is not mapped is not one -- and never an override-redirect
        one, which is exactly what that flag opts a window out of.

        Measured live on a display with no window manager, walking the raw
        tree without this: GTK's own combo-box drop-down appeared as a titled
        toplevel sharing its application's pid, sitting over the widget the
        pointer was asked about, so `window_at` named the popup where an EWMH
        client list names the application's window. The accessibility bridge's
        hidden 10x10 toplevel -- unmapped, and titled after the program like
        the popups are -- was listed the same way, giving one application
        several windows that can never be under the pointer.

        Nor an InputOnly window: it has no pixels to show, a window manager
        declines to manage one (Mutter says so by name), and GTK3 gives every
        toplevel a mapped 1x1 InputOnly child to hold keyboard focus -- see
        `_toplevel_of` -- which the walk would otherwise list as an untitled
        window of its own, once per GTK3 toplevel, with no pid.

        Deliberately not applied to the `_NET_CLIENT_LIST_STACKING` path: that
        list is the window manager's own answer, and a minimized window on it
        is mapped-but-unviewable by design (the manager unmaps the client), so
        filtering there would hide windows a caller can still restore. A read
        that fails keeps the window, as the rest of this backend does.
        """
        try:
            attributes = window.get_attributes()
        except Exception:
            return True
        if getattr(attributes, "override_redirect", False):
            return False
        if getattr(attributes, "win_class", None) == self._X.InputOnly:
            return False
        return attributes.map_state != self._X.IsUnmapped

    def _app_id(self, window):
        """A window's application identity from WM_CLASS, or empty.

        WM_CLASS is a pair -- the instance name, then the class -- and the
        class is what this reports, because that is what sway and Hyprland
        already call the `app_id` of an X11 client. The same application is
        then named the same however it was listed, which is the whole point of
        having the field: titles drift while this does not, so it is the
        identity a script should match on when a title cannot be trusted.
        """
        try:
            pair = window.get_wm_class()
        except Exception:
            return ""
        return (pair[-1] or "") if pair else ""

    def _pid(self, window):
        """The owning process id from _NET_WM_PID, or None."""
        try:
            atom = self._display.intern_atom("_NET_WM_PID")
            prop = window.get_full_property(atom, self._X.AnyPropertyType)
            return prop.value[0] if prop else None
        except Exception:
            return None

    def _handle(self, window):
        """The X window object for a Window or raw handle."""
        return window.handle if isinstance(window, Window) else window

    @staticmethod
    def _unreadable(handle, exc):
        """The `WindowNotFound` for a handle this backend could not read.

        Two different failures used to share one sentence. A window that has
        been destroyed makes Xlib raise, and "window is gone" is exactly
        right for it. A handle from *another backend's* namespace -- a KWin
        UUID string, a GNOME Shell stable_sequence integer -- is not a
        window object at all, so the failure is an `AttributeError` for the
        Xlib method that does not exist on it, and calling that "window is
        gone" described a window that was on screen the whole time while
        leaking python-xlib's own type error into the message.

        Measured live on KDE Wayland with a composite of kwinevents + x11:
        `screenshot(window=...)` on a native Wayland window reported
        "window is gone: 'str' object has no attribute 'get_geometry'".
        The window was open; x11 simply cannot see a Wayland client, which
        is what this now says.
        """
        if isinstance(exc, AttributeError):
            return WindowNotFound(
                f"not an X11 window handle ({type(handle).__name__}): a "
                f"Window another backend issued cannot be read here, and on "
                f"a Wayland session X11 does not see native Wayland windows "
                f"at all"
            )
        return WindowNotFound(f"window is gone: {exc}")

    def _frame_extents(self, handle):
        """The shadow a client draws around its own visible window, or None.

        `(left, right, top, bottom)` from the window's `_GTK_FRAME_EXTENTS`
        -- the property a client-side-decorated GTK3 or GTK4 toplevel sets
        under a compositing manager to say how much of its X window is
        translucent shadow rather than window. The window manager treats
        everything inside those margins as the window: that is the
        rectangle it snaps, tiles, constrains to the work area and reports
        as the frame, and what the user sees as the window's edge. None
        for a window that sets no such property, or one whose four values
        are all zero or not all non-negative (nothing to subtract, or
        nothing to trust).

        Measured on GhostBSD's marco with the GTK4 torture fixture: the X
        window `(0, 28, 1024, 716)` on a 1024x768 screen carries
        `[30, 30, 24, 41]`, and the window the user sees -- its header bar's
        top edge at y=52, its content 964 wide -- is exactly the X window
        with those taken off, and exactly the 964x651 the toolkit's own
        accessible frame reports for itself.
        """
        try:
            atom = self._display.intern_atom("_GTK_FRAME_EXTENTS")
            prop = handle.get_full_property(atom, self._X.AnyPropertyType)
        except Exception:
            return None
        if prop is None or len(prop.value) < 4:
            return None
        try:
            extents = tuple(int(v) for v in prop.value[:4])
        except (TypeError, ValueError):
            return None
        if min(extents) < 0 or not any(extents):
            return None
        return extents

    @staticmethod
    def _inside_extents(x, y, width, height, extents):
        """The rectangle with its `_frame_extents` margin taken off.

        Unchanged when there is no margin, and unchanged when the margin
        would leave nothing: a window shrunk below its own declared
        shadow (a client mid-unmap, a stale property) is reported whole
        rather than as a negative or empty rectangle. `geometry()` and a
        `window=` capture both go through here so the window one reports
        is the window the other reads.
        """
        if extents is None:
            return (x, y, width, height)
        left, right, top, bottom = extents
        if width <= left + right or height <= top + bottom:
            return (x, y, width, height)
        return (x + left, y + top, width - left - right, height - top - bottom)

    def geometry(self, window):
        """A window's (x, y, width, height), in screen (root) coordinates.

        The rectangle is the window the user sees. An X11 client that draws
        its own decorations under a compositing manager -- a GTK3 or GTK4
        toplevel with a header bar -- makes its X window bigger than that
        by a translucent shadow margin on every side, and declares the
        margin in `_GTK_FRAME_EXTENTS` so the window manager can treat the
        inside as the window; so does this (`_frame_extents`), which is
        also what every Wayland backend reports, since a compositor answers
        with the client's declared window geometry, shadow excluded, and
        what `move_window`/`resize_window` and a `window=` capture then
        agree with. Before 2026-11-04 the raw X window was reported, and
        the element placement built on it (`AtspiBackend`, for a toolkit
        that publishes no screen position of its own) read the 65px of
        shadow in the height as a server-side titlebar and put every
        control of the GTK4 torture fixture 10px too low on marco --
        measured by a screenshot against the placed rectangle.

        get_geometry() alone reports position relative to the window's
        *parent*, which under any reparenting window manager is the
        decoration frame the WM inserted -- not the screen. XTranslateCoordinates
        is what the legacy GetWindowPos used to fix this, so the origin is
        translated to the root window here rather than trusting get_geometry
        directly.

        The direction of that translation is the whole of it, and it used to
        be backwards. `w.translate_coords(src, x, y)` in python-xlib sends
        `src_wid=src, dst_wid=w`, so calling it on the *handle* with the root
        as source asks where the root's origin is in the window's coordinates
        -- which is (-x, -y), the negation of the wanted answer. It has to be
        called on the root with the handle as source.

        That was invisible for as long as every window checked sat at (0, 0),
        where a negation cannot be seen. It is also, in all likelihood, the
        real cause of what was recorded here as a Mutter/XWayland quirk:
        "geometry() can return a position wildly off from where the window is
        actually rendered, while move_window still moves it correctly".
        Negated coordinates are wildly off, only the read-back is affected,
        and it reproduces with no compositor in sight -- caught on a bare Xvfb
        with two windows, one at the origin and one at (600, 400), where the
        second read back as (-600, -400).
        """
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        try:
            geom = handle.get_geometry()
            origin = geom.root.translate_coords(handle, 0, 0)
        except Exception as exc:
            raise self._unreadable(handle, exc) from exc
        return self._inside_extents(
            origin.x,
            origin.y,
            geom.width,
            geom.height,
            self._frame_extents(handle),
        )

    _MOVERESIZE_GRAVITY_STATIC = 10
    """Position the window's own top-left corner exactly at (x, y),
    decorations included, regardless of their size -- see _moveresize."""

    _MOVERESIZE_SOURCE_PAGER = 2 << 12
    """Source indication 2 -- "pager or taskbar", i.e. a direct user action.

    Bits 12-15 of `data.l[0]` carry it. Source 1 means "the application
    itself", and EWMH §9.11 explicitly permits a window manager to treat
    that differently from a request made on a user's behalf: an unsolicited
    application reposition is the kind of thing a manager may demote,
    reorder or ignore outright. This package is the user's hand here, which
    is the same reasoning `close_window` and `maximize_window` already
    spell out for sending 2 -- `_moveresize` was the one request in this
    file still claiming to be the application.
    """

    def _moveresize(self, window, x=None, y=None, width=None, height=None):
        """Send _NET_MOVERESIZE_WINDOW: move and/or resize in screen coordinates.

        A raw ConfigureWindow request -- what move_window/resize_window used
        before this -- sets position relative to whatever the window manager
        reparented the client under, not the screen; that mismatch is exactly
        why geometry() has to translate through the root to report a sane
        value, and why plain configure() here silently moved windows to the
        wrong place. StaticGravity (see the EWMH spec's rationale for this
        message) sidesteps needing to know the decoration size at all: the
        window manager is required to honour this message like a
        ConfigureRequest, but positioned by the client window's own corner.
        Only the values that are not None are included in the request, so a
        pure move leaves size alone and a pure resize leaves position alone.

        The window manager is what acts on this, so there has to be one: with
        nothing listening, measured on a bare Xvfb with no manager running,
        both calls do nothing at all -- no error and no change in geometry --
        while WINDOW_PLACEMENT and WINDOW_RESIZE stay declared. And a request
        that arrives before the manager has finished placing a newly mapped
        window is lost to that placement, which is why move_window's docstring
        says to wait for `is_window_viewable` first.

        The corner StaticGravity positions is the X window's own, and for a
        client that draws its shadow inside its X window that corner sits
        a `_GTK_FRAME_EXTENTS` margin outside the window the user sees --
        the one `geometry()` reports. The request is shifted and widened by
        that margin, so `move_window(x, y)` puts the visible corner at
        `(x, y)`, `resize_window(w, h)` makes the visible window `w` by
        `h`, and each reads back through `geometry()` as asked.
        """
        handle = self._handle(window)
        extents = self._frame_extents(handle)
        if extents is not None:
            left, right, top, bottom = extents
            if x is not None:
                x -= left
            if y is not None:
                y -= top
            if width is not None:
                width += left + right
            if height is not None:
                height += top + bottom
        flags = self._MOVERESIZE_GRAVITY_STATIC | self._MOVERESIZE_SOURCE_PAGER
        values = [0, 0, 0, 0]
        for i, value in enumerate((x, y, width, height)):
            if value is not None:
                flags |= 1 << (8 + i)
                values[i] = value
        atom = self._display.intern_atom("_NET_MOVERESIZE_WINDOW")
        message = self._xevent.ClientMessage(
            window=handle, client_type=atom, data=(32, [flags] + values)
        )
        root = handle.get_geometry().root
        root.send_event(
            message,
            event_mask=self._X.SubstructureRedirectMask
            | self._X.SubstructureNotifyMask,
        )
        self._sync()

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y), in screen coordinates.

        The window manager does the moving, so this needs one running -- see
        `_moveresize` for what that costs, and for why a move issued before a
        newly mapped window has been placed goes nowhere.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        self._moveresize(window, x=x, y=y)

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`.

        The same `_NET_MOVERESIZE_WINDOW` route as move_window, including what
        that costs: nothing happens without a window manager.
        """
        self.require(Capability.WINDOW_RESIZE)
        self._moveresize(window, width=width, height=height)

    def activate_window(self, window):
        """Raise a window and give it input focus."""
        self.require(Capability.WINDOW_ACTIVATE)
        handle = self._handle(window)
        handle.configure(stack_mode=self._X.Above)
        handle.set_input_focus(self._X.RevertToParent, self._X.CurrentTime)
        self._sync()

    def lower_window(self, window):
        """Replaces LowerWindow -- no foreign-toplevel protocol offers this."""
        self.require(Capability.WINDOW_LOWER)
        self._handle(window).configure(stack_mode=self._X.Below)
        self._sync()

    def set_window_title(self, window, title):
        """Replaces SetWindowName. Impersonation is possible under X11."""
        self.require(Capability.WINDOW_TITLE_SET)
        self._handle(window).set_wm_name(title)
        self._sync()

    def minimize_window(self, window, minimized=True):
        """Unmap a window, or map it again when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        handle = self._handle(window)
        if minimized:
            handle.unmap()
        else:
            handle.map()
        self._sync()

    def maximize_window(self, window, maximized=True):
        """Maximize or restore a window, via `_NET_WM_STATE`.

        One ClientMessage carrying both `_NET_WM_STATE_MAXIMIZED_VERT` and
        `_NET_WM_STATE_MAXIMIZED_HORZ` -- EWMH lets a message name two
        properties exactly so that a maximize is one request rather than
        two half-maximized intermediate states. Routed through the manager
        like `_moveresize` and `close_window`, since the maximized frame is
        the manager's to grant.

        `data.l[0]` is the action (1 add, 0 remove), `l[3]` the same
        source-indication 2 ("direct user action") `close_window` sends.

        The manager does the maximizing, so this inherits `_moveresize`'s
        no-manager trap: on a bare Xvfb with no window manager the request
        goes nowhere, nothing errors, and `is_window_maximized` keeps
        answering False.
        """
        self.require(Capability.WINDOW_MAXIMIZE)
        handle = self._handle(window)
        action = 1 if maximized else 0
        vert = self._display.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT")
        horz = self._display.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        message = self._xevent.ClientMessage(
            window=handle,
            client_type=self._display.intern_atom("_NET_WM_STATE"),
            data=(32, [action, vert, horz, 2, 0]),
        )
        root = handle.get_geometry().root
        root.send_event(
            message,
            event_mask=self._X.SubstructureRedirectMask
            | self._X.SubstructureNotifyMask,
        )
        self._sync()

    def is_window_maximized(self, window):
        """Whether both maximized atoms are in the window's `_NET_WM_STATE`.

        Both, not either: a half-maximized (tiled-to-one-edge) window
        carries exactly one of the pair, and calling that "maximized" would
        report a state `maximize_window` never set. A window with no
        `_NET_WM_STATE` property at all is simply not maximized.
        """
        self.require(Capability.WINDOW_MAXIMIZE)
        handle = self._handle(window)
        atom = self._display.intern_atom("_NET_WM_STATE")
        try:
            prop = handle.get_full_property(atom, self._X.AnyPropertyType)
        except Exception as exc:  # the same idiom is_window_viewable uses
            raise self._unreadable(handle, exc) from exc
        if prop is None:
            return False
        states = set(prop.value)
        vert = self._display.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT")
        horz = self._display.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        return vert in states and horz in states

    def close_window(self, window):
        """Ask the window manager to close a window, via `_NET_CLOSE_WINDOW`.

        The EWMH route rather than `XDestroyWindow`: the manager forwards it
        to the client as an ordinary `WM_DELETE_WINDOW`, so the application
        keeps its say -- an editor with unsaved work answers with its "Save
        changes?" dialog instead of dying, which is the whole reason this
        operation exists apart from killing the process. Sent to the root
        with the same event mask `_moveresize` uses, since both are requests
        *to the manager* about a window rather than to the window itself.

        `data.l[1]` is 2, "direct user action" -- EWMH's spec for pagers and
        for tools acting on the user's behalf, and the value that entitles
        the request to be honoured now rather than demoted the way managers
        may demote an application's own unsolicited request (source 1).
        """
        self.require(Capability.WINDOW_CLOSE)
        handle = self._handle(window)
        atom = self._display.intern_atom("_NET_CLOSE_WINDOW")
        message = self._xevent.ClientMessage(
            window=handle,
            client_type=atom,
            data=(32, [self._X.CurrentTime, 2, 0, 0, 0]),
        )
        root = handle.get_geometry().root
        root.send_event(
            message,
            event_mask=self._X.SubstructureRedirectMask
            | self._X.SubstructureNotifyMask,
        )
        self._sync()

    def _font_cursor(self, shape):
        """Build a cursor from the standard "cursor" font, cached by shape.

        Mirrors XCreateFontCursor: the glyph at `shape` is the image and the
        one after it is the mask, which is how the XC_* constants are laid
        out. Those constants are the same values X11::GUITest exports.

        Cached rather than built fresh per call: is_window_cursor() is a
        query a caller may run repeatedly, and there are only a couple of
        hundred distinct standard cursor shapes, so caching by shape bounds
        what would otherwise be one new server-side cursor object per call
        with none of them ever released.
        """
        if self._cursor_font is None:
            self._cursor_font = self._display.open_font("cursor")
        if shape not in self._cursors:
            self._cursors[shape] = self._cursor_font.create_glyph_cursor(
                self._cursor_font, shape, shape + 1, (0, 0, 0), (65535, 65535, 65535)
            )
        return self._cursors[shape]

    def is_window_cursor(self, window, shape):
        """Whether `window` is currently showing cursor `shape`.

        Replaces IsWindowCursor. Impossible on Wayland, where cursor shape is
        negotiated privately between client and compositor -- this is the one
        capability the audit found had no workaround there.
        """
        self.require(Capability.WINDOW_CURSOR_QUERY)
        return bool(self._handle(window).xtest_compare_cursor(self._font_cursor(shape)))

    def _toplevel_of(self, window):
        """The toplevel `window` is or sits inside, as `windows()` would list it.

        X input focus is a window, not necessarily a toplevel. GTK3 gives
        every toplevel a hidden 1x1 InputOnly child and sets keyboard focus
        on *that*, so `get_input_focus()` answers a window nothing lists --
        no title, no pid, no entry in the window manager's client list --
        and a caller comparing it with the dialog that plainly holds focus
        is told they differ. Measured live on GhostBSD's marco with Qt's
        native (GTK3) file chooser: focus sat on 0x4200008, an unnamed child
        of the listed 0x4200007 'Census File', while _NET_ACTIVE_WINDOW
        named the toplevel itself; `wait_window_focus(dialog)` therefore
        answered False for two seconds about a dialog the Escape that
        followed closed. Qt-drawn dialogs focus their toplevel directly and
        compared equal all along, which is why the Qt fixture's main window
        never showed this and its GTK-backed file, colour and font dialogs
        all did.

        Climbs the tree to the nearest ancestor the window manager lists,
        or -- on a display with no `_NET_CLIENT_LIST_STACKING` to consult --
        to the child of the root, which is the toplevel wherever nothing
        reparents. A window with no such ancestor (the root itself, or a
        focused override-redirect popup no manager lists) is handed back as
        it is, and so is one whose ancestry cannot be read -- an ancestor
        destroyed mid-climb -- since the window focus is on is the one thing
        still known to be true.
        """
        try:
            tree = window.query_tree()
        except Exception:
            return window
        root = tree.root
        if root is None or window == root:
            return window
        listed = self._client_list(root)
        current = window
        # Bounded: a tree is finite, but a window destroyed mid-climb can
        # make query_tree answer about a recycled id forever.
        for _ in range(64):
            if listed is not None:
                if current in listed:
                    return current
            elif tree.parent == root:
                return current
            parent = tree.parent
            if parent is None or parent == root and listed is not None:
                # Reached the top without finding it on the manager's list:
                # not a managed window, so there is nothing truer to report
                # than the window focus is actually on.
                return window
            try:
                tree = parent.query_tree()
            except Exception:
                return window
            current = parent
        return window

    def active_window(self):
        """The window holding input focus, or None per the base contract.

        get_input_focus().focus is ordinarily a window object, but can also
        be the PointerRoot or None X constants -- both plain integers, not
        window objects -- when nothing has taken focus explicitly. Wrapping
        either as a Window used to produce a handle to nothing, silently:
        _title()'s bare except swallowed the resulting AttributeError and
        returned "", so the caller got Window('') with no indication
        anything was wrong.

        The focused window is resolved to its toplevel first -- see
        `_toplevel_of` -- so that what comes back is a window `windows()`
        lists and `Window.__eq__` can match, rather than the focus-holding
        child a toolkit may have put underneath it.
        """
        self.require(Capability.WINDOW_STATE)
        focus = self._display.get_input_focus().focus
        if isinstance(focus, int):
            return None
        focus = self._toplevel_of(focus)
        # Carries the same identity fields as a listed window. Without them a
        # caller had to decide whether a window came from windows() or from
        # here before it could rely on `app_id` or `pid` being populated, and
        # nothing said so.
        return Window(
            handle=focus,
            backend=self,
            title=self._title(focus) or "",
            app_id=self._app_id(focus),
            pid=self._pid(focus),
        )

    def is_window_viewable(self, window):
        """Whether `window` is mapped and actually showing on screen.

        Replaces X11::GUITest's WaitWindowViewable poll target directly:
        XGetWindowAttributes' own map_state. IsUnviewable means mapped but
        with an unmapped ancestor -- only IsViewable counts as viewable.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        try:
            attrs = handle.get_attributes()
        except Exception as exc:
            raise self._unreadable(handle, exc) from exc
        return attrs.map_state == self._X.IsViewable

    # -- capture -----------------------------------------------------------
    #
    # GetImage returns a raw buffer whose layout is the *server's*, not a
    # format anyone can assume. Two things vary and both are read from the
    # connection rather than guessed: the visual's channel masks say which
    # bits are red, green and blue, and image_byte_order says whether a
    # pixel's bytes arrive most- or least-significant first. Hard-coding the
    # usual x86 answer (32bpp, BGRX, little-endian) would work on most
    # machines and silently swap the colour channels on the rest.

    _MAX_REQUEST_ROWS = 64
    """Scanlines per GetImage request -- see _read_image."""

    def _visual_masks(self):
        """The root visual's (red, green, blue) bit masks.

        Walks the screen's advertised depths because python-xlib's Screen
        carries only the root visual's *id*, not the visual itself.
        """
        screen = self._display.screen()
        for depth in screen.allowed_depths:
            for visual in depth.visuals:
                if visual.visual_id == screen.root_visual:
                    return (visual.red_mask, visual.green_mask, visual.blue_mask)
        raise CapabilityUnsupported(
            Capability.SCREEN_CAPTURE,
            self.name,
            "the root visual is not among the screen's advertised depths, "
            "so its channel layout cannot be determined",
        )

    @staticmethod
    def _shift(mask):
        """How far right to shift a channel so its top bit lands at bit 7.

        Returns the shift and the bit width, so a channel narrower than 8
        bits (a 16-bit 5-6-5 visual) can be scaled up rather than left dark.
        """
        shift = 0
        while mask and not mask & 1:
            mask >>= 1
            shift += 1
        width = mask.bit_length()
        return shift, width

    def _read_image(self, drawable, x, y, width, height):
        """GetImage over a rectangle, in horizontal bands.

        One request for a whole screen would routinely exceed the X server's
        maximum request length -- the limit is on the *reply* too, and a 4K
        frame is ~33MB against a typical few-megabyte ceiling. python-xlib
        raises rather than splitting, so the read is banded here. The band
        height is a fixed small number rather than one computed from
        max_request_length: the latter is what the limit is nominally about,
        but servers and extensions disagree on how it applies to replies,
        and a conservative constant costs only a few extra round trips.
        """
        bands = []
        for top in range(0, height, self._MAX_REQUEST_ROWS):
            rows = min(self._MAX_REQUEST_ROWS, height - top)
            try:
                reply = drawable.get_image(
                    x, y + top, width, rows, self._X.ZPixmap, 0xFFFFFFFF
                )
            except Exception as exc:
                raise WindowNotFound(f"cannot read pixels: {exc}") from exc
            data = reply.data
            bands.append(data if isinstance(data, bytes) else bytes(data))
        return b"".join(bands)

    # The four memory layouts that need no per-pixel arithmetic, keyed by
    # (bytes per pixel, MSBFirst). All four are the same visual -- 8 bits
    # each of red, green and blue in the usual masks -- seen through the
    # two byte orders. A pixel P with red at 0xFF0000 arrives LSBFirst as
    # B, G, R, X and MSBFirst as X, R, G, B, so which bytes to drop and
    # whether to swap is all that differs.
    _DIRECT_MASKS = (0xFF0000, 0x00FF00, 0x0000FF)
    _DIRECT_LAYOUTS = {
        (4, False): "BGRX",
        (4, True): "XRGB",
        (3, False): "BGR",
        (3, True): "RGB",
    }

    @staticmethod
    def _to_rgb_direct(row, layout):
        """Convert one scanline in a known layout, with no Python loop.

        Worth the special case by a wide margin: the general decoder shifts
        and masks every channel of every pixel in Python, which on a
        1920x1080 frame is six million operations and takes roughly twenty
        seconds. These slice assignments run in C and do the same frame in
        well under a tenth of a second. The general path stays for the
        visuals this does not cover.
        """
        if layout == "XRGB":
            del row[0::4]
            return bytes(row)
        if layout == "BGRX":
            del row[3::4]
        elif layout == "RGB":
            return bytes(row)
        blue = row[0::3]
        row[0::3] = row[2::3]
        row[2::3] = blue
        return bytes(row)

    def _to_rgb_rows(self, data, width, height):
        """Decode a ZPixmap buffer into per-scanline RGB bytes."""
        masks = self._visual_masks()
        big_endian = self._display.display.info.image_byte_order == 1
        stride = len(data) // height
        # X pads each scanline out to a whole number of words, so the row is
        # not always width * bytes-per-pixel. The padding is at most a few
        # bytes and the pixel size is a whole number, so integer division by
        # the width recovers the latter for any image wider than the padding
        # -- which is every real screenshot.
        bytes_per_pixel = max(1, stride // width)
        if bytes_per_pixel not in (2, 3, 4):
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                f"{bytes_per_pixel * 8}-bit pixels are not supported; only "
                "16, 24 and 32-bit TrueColor visuals are decoded",
            )
        row_bytes = width * bytes_per_pixel
        layout = None
        if masks == self._DIRECT_MASKS:
            layout = self._DIRECT_LAYOUTS.get((bytes_per_pixel, big_endian))

        order: Literal["big", "little"] = "big" if big_endian else "little"
        channels = [self._shift(mask) for mask in masks]

        rows = []
        for row in range(height):
            start = row * stride
            raw = bytearray(data[start : start + row_bytes])
            if layout is not None:
                rows.append(self._to_rgb_direct(raw, layout))
                continue
            out = bytearray(width * 3)
            for column in range(width):
                offset = column * bytes_per_pixel
                pixel = int.from_bytes(raw[offset : offset + bytes_per_pixel], order)
                for channel, (shift, bits) in enumerate(channels):
                    value = (pixel >> shift) & ((1 << bits) - 1)
                    # Scale a narrow channel to a full byte by repeating its
                    # high bits, so 5-bit 31 becomes 255 rather than 248.
                    if bits < 8:
                        value = (value << (8 - bits)) | (value >> (2 * bits - 8))
                    elif bits > 8:
                        value >>= bits - 8
                    out[column * 3 + channel] = value & 0xFF
            rows.append(bytes(out))
        return rows

    def _drawable_size(self, drawable, window=None) -> tuple[int, int]:
        """The drawable's (width, height), asked of the server.

        Deliberately not `screen.width_in_pixels`. That comes from the
        connection *setup*, which the server sends once when the
        connection opens and never revises -- so after a RandR resize it
        reports the size the screen used to be. In a virtual machine
        whose guest resolution follows the host window, or on any desktop
        where a monitor is plugged in or a resolution changed after the
        process started, it is simply stale.

        Asking for a rectangle larger than the drawable is not a soft
        failure: GetImage answers BadMatch, which surfaced as
        "cannot read pixels: BadMatch" the first time this ran against a
        real X server. get_geometry() is a round trip to the server and
        always current.
        """
        try:
            geometry = drawable.get_geometry()
        except Exception as exc:
            if window is not None:
                raise self._unreadable(drawable, exc) from exc
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                f"cannot read the root window's geometry: {exc}",
            ) from exc
        return (geometry.width, geometry.height)

    def _fit_within(self, region, bounds):
        """Fit a region into the drawable, refusing one genuinely outside.

        X requires the requested rectangle to lie entirely inside the
        drawable; anything else is BadMatch, an error that names none of
        the numbers involved and so says nothing about which side was
        wrong. The shared `fit_region` turns that into either a fitted
        rectangle (one pixel of covering overhang, the legitimate HiDPI
        edge case) or a ValueError with both rectangles in it. ValueError,
        not CapabilityUnsupported, on purpose: a bad region is the
        caller's geometry, not this installation's mechanism, and the
        composite's capture fallback caches mechanism failures for the
        whole session -- a typed capability error here would blacklist
        this backend over one stale rectangle.
        """
        limit_width, limit_height = bounds
        return fit_region(
            region,
            (limit_width, limit_height),
            "drawable (X refuses a GetImage that reaches outside it)",
        )

    def capture(self, window=None, path=None, region=None):
        """Screenshot the root window, one window, or one rectangle.

        With `window`, this reads that window's own drawable rather than
        the screen coordinates it occupies, so anything stacked on top of
        it is not in the image. The X server is only obliged to keep those
        pixels while the window is viewable, though: a minimized or
        unmapped window has no backing content unless the server has
        backing store enabled, and GetImage on one raises rather than
        returning a blank frame.

        The image is the window `geometry()` reports: a client that draws
        its shadow inside its own X window (`_frame_extents`) has that
        margin left out, so the PNG is the window the user sees and its
        size matches the geometry -- not a larger picture with a band of
        shadow pixels, drawn translucent over nothing, around it.

        Writes a PNG through this package's own encoder -- no image
        library and no screenshot tool involved, so this is the one capture
        path with nothing to install.
        """
        # Only the capability actually being used. Requiring
        # SCREEN_CAPTURE up front refused a per-window capture on the one
        # session that most needs it: under XWayland this backend drops
        # SCREEN_CAPTURE (the root is unreadable) while keeping
        # WINDOW_CAPTURE (an X11 client's own drawable is readable, and
        # measured to be), so window= was rejected for lacking a
        # capability it does not use.
        region = check_region(region, window)
        if window is not None:
            self.require(Capability.WINDOW_CAPTURE)
            drawable = self._handle(window)
        else:
            self.require(Capability.SCREEN_CAPTURE)
            drawable = self._display.screen().root
        bounds = self._drawable_size(drawable, window)

        if region is not None:
            x, y, width, height = self._fit_within(region, bounds)
        elif window is not None:
            # The window inside its shadow margin -- a sub-rectangle of the
            # drawable, so it cannot be out of bounds either.
            x, y, width, height = self._inside_extents(
                0, 0, *bounds, self._frame_extents(drawable)
            )
        else:
            # A whole-root capture is exactly the drawable's own rectangle,
            # which cannot be out of bounds.
            x, y = 0, 0
            width, height = bounds

        data = self._read_image(drawable, x, y, width, height)
        rows = self._to_rgb_rows(data, width, height)
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        return _png.write_rgb(path, width, height, rows)

    def window_at(self, x, y, screen=0):
        """The topmost window covering a screen coordinate, or None.

        Covering by `geometry()`'s rectangle -- the window the user sees --
        so a point in a client-drawn shadow margin is not inside that
        window, just as a click there would reach whatever is beneath.
        """
        self.require(Capability.WINDOW_AT_POINT)
        match = None
        for window in self.windows():
            try:
                wx, wy, width, height = self.geometry(window)
            except WindowNotFound:
                # A window closing between the listing above and this
                # per-window read is ordinary -- one gone window unrelated
                # to the point asked about must not fail the whole hit
                # test for every other window still open.
                continue
            if wx <= x < wx + width and wy <= y < wy + height:
                match = window
        return match
