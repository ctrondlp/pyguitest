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

import os
import tempfile
import time

from .. import png as _png
from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported, WindowNotFound
from .base import GUIBackend, Screen, Window, check_region

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
    """Import python-xlib, or return None."""
    try:
        from Xlib import XK, X, display
        from Xlib.ext import xtest
        from Xlib.protocol import event
    except Exception:
        return None
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

    def _resolve_char(self, char):
        """Return (keycode, shift_level) for `char`, or None if unmapped."""
        keysym = self._char_keysym(char)
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            return None
        levels = self._display.get_keyboard_mapping(keycode, 1)[0]
        try:
            level = levels.index(keysym)
        except ValueError:
            level = 0
        return keycode, level

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Keymap-correct by construction: each character is resolved to a
        keysym and then to whatever keycode and shift level the *server's
        current map* assigns it, with Shift_L held for any non-zero level.
        This is what uinput cannot do, and why the audit ranks scancode
        injection last. `allow_keymap_unsafe` is accepted and ignored: X11 is
        never keymap-unsafe, so there is nothing for it to refuse.

        Raises CapabilityUnsupported for a character with no keycode at all
        in the current map, rather than pressing keycode 0 and typing
        nothing. There is no fallback here for that case (xdotool handles it
        by temporarily remapping a spare keycode); this backend does not
        mutate the server's keyboard mapping.
        """
        self.require(Capability.TEXT_ENTRY)
        shift = self._keycode("Shift_L")
        for char in text:
            resolved = self._resolve_char(char)
            if resolved is None:
                raise CapabilityUnsupported(
                    Capability.TEXT_ENTRY,
                    self.name,
                    f"{char!r} has no keycode in the server's current keymap",
                )
            keycode, level = resolved
            needs_shift = level != 0
            if needs_shift:
                self._display.xtest_fake_input(self._X.KeyPress, shift)
            self._display.xtest_fake_input(self._X.KeyPress, keycode)
            self._display.xtest_fake_input(self._X.KeyRelease, keycode)
            if needs_shift:
                self._display.xtest_fake_input(self._X.KeyRelease, shift)
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
        """A window's title, or empty string if it has none."""
        try:
            return window.get_wm_name() or ""
        except Exception:
            return ""

    def windows(self):
        """Every toplevel with a title, across all screens.

        Prefers _NET_CLIENT_LIST_STACKING; falls back to a full tree walk only for a
        window manager that does not maintain it, which is also the only
        path where one application can appear more than once.
        """
        self.require(Capability.WINDOW_LIST)
        found = []
        for screen_number in range(self._display.screen_count()):
            root = self._display.screen(screen_number).root
            clients = self._client_list(root)
            candidates = clients if clients is not None else self._walk(root)
            for child in candidates:
                title = self._title(child)
                if title:
                    found.append(
                        Window(
                            handle=child,
                            backend=self,
                            title=title,
                            pid=self._pid(child),
                        )
                    )
        return found

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

    def geometry(self, window):
        """A window's (x, y, width, height), in screen (root) coordinates.

        get_geometry() alone reports position relative to the window's
        *parent*, which under any reparenting window manager is the
        decoration frame the WM inserted -- not the screen. XTranslateCoordinates
        is what the legacy GetWindowPos used to fix this, so the origin is
        translated to the root window here rather than trusting get_geometry
        directly.

        Observed live on GNOME/Mutter (XWayland): this can still return a
        position wildly off from where the window is actually rendered, even
        though the root window's own geometry and the client's offset within
        its frame both check out as sane on their own. The likely, but not
        independently confirmed, explanation is that Mutter's XWayland
        integration doesn't keep the decoration frame's X11-visible position
        synced to its real Wayland-compositor placement -- move_window still
        visibly moves the window correctly in that case; only the read-back
        through geometry() disagrees with reality. Not reproduced on other
        window managers, and not something a different X11 request from this
        backend would fix if the underlying frame position X11 reports really
        is stale.
        """
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        try:
            geom = handle.get_geometry()
            root = geom.root
            origin = handle.translate_coords(root, 0, 0)
        except Exception as exc:
            raise WindowNotFound(f"window is gone: {exc}") from exc
        return (origin.x, origin.y, geom.width, geom.height)

    _MOVERESIZE_GRAVITY_STATIC = 10
    """Position the window's own top-left corner exactly at (x, y),
    decorations included, regardless of their size -- see _moveresize."""

    _MOVERESIZE_SOURCE_APPLICATION = 1 << 12

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
        """
        handle = self._handle(window)
        flags = self._MOVERESIZE_GRAVITY_STATIC | self._MOVERESIZE_SOURCE_APPLICATION
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

        Confirmed visually on GNOME/Mutter (XWayland): the window does move
        to the requested position. A geometry() call made right afterward may
        still disagree, though -- see its docstring for why that appears to
        be a separate, unconfirmed issue on the read side.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        self._moveresize(window, x=x, y=y)

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`."""
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

    def active_window(self):
        """The window holding input focus, or None per the base contract.

        get_input_focus().focus is ordinarily a window object, but can also
        be the PointerRoot or None X constants -- both plain integers, not
        window objects -- when nothing has taken focus explicitly. Wrapping
        either as a Window used to produce a handle to nothing, silently:
        _title()'s bare except swallowed the resulting AttributeError and
        returned "", so the caller got Window('') with no indication
        anything was wrong.
        """
        self.require(Capability.WINDOW_STATE)
        focus = self._display.get_input_focus().focus
        if isinstance(focus, int):
            return None
        return Window(handle=focus, backend=self, title=self._title(focus))

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
            raise WindowNotFound(f"window is gone: {exc}") from exc
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

        order = "big" if big_endian else "little"
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

    def _drawable_size(self, drawable, window=None):
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
                raise WindowNotFound(f"window is gone: {exc}") from exc
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                f"cannot read the root window's geometry: {exc}",
            ) from exc
        return (geometry.width, geometry.height)

    def _check_within(self, x, y, width, height, bounds):
        """Reject a rectangle the drawable does not wholly contain.

        X requires the requested rectangle to lie entirely inside the
        drawable; anything else is BadMatch, an error that names none of
        the numbers involved and so says nothing about which side was
        wrong. Checking here turns it into a message with both rectangles
        in it.
        """
        limit_width, limit_height = bounds
        if x < 0 or y < 0 or x + width > limit_width or y + height > limit_height:
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                f"the requested rectangle {width}x{height}+{x}+{y} is not "
                f"wholly inside the {limit_width}x{limit_height} drawable; "
                "X refuses a GetImage that reaches outside it",
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

        if window is not None:
            x, y = 0, 0
            width, height = bounds
        elif region is not None:
            x, y, width, height = region
        else:
            x, y = 0, 0
            width, height = bounds
        self._check_within(x, y, width, height, bounds)

        data = self._read_image(drawable, x, y, width, height)
        rows = self._to_rgb_rows(data, width, height)
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        return _png.write_rgb(path, width, height, rows)

    def window_at(self, x, y, screen=0):
        """The topmost window covering a screen coordinate, or None."""
        self.require(Capability.WINDOW_AT_POINT)
        match = None
        for window in self.windows():
            wx, wy, width, height = self.geometry(window)
            if wx <= x < wx + width and wy <= y < wy + height:
                match = window
        return match
