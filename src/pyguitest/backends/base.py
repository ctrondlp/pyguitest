"""The backend interface.

Shaped by capability rather than by the 50 legacy function names -- the audit
found that only 13 of those port unchanged, 6 should not exist, and the rest
change shape, so a faithful 1-to-1 interface would spend most of its surface on
the parts most worth redesigning.

A backend implements what it can and declares it in `capabilities`. Every
unimplemented operation raises CapabilityUnsupported by default, so a partial
backend is a valid backend -- which is the normal case, not the exception.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NamedTuple

from ..capabilities import Capability
from ..errors import CapabilityUnsupported

__all__ = ["GUIBackend", "Window", "Screen", "ImageMatch", "check_region"]


def check_region(region, window=None):
    """Validate a capture region and return it as four ints.

    One place rather than four: every capture backend takes the same
    normalized `(x, y, width, height)` in screen coordinates, and every one
    of them would otherwise repeat the same three checks slightly
    differently.

    A zero or negative width or height is rejected rather than passed
    through. The tools disagree on what they do with one -- grim errors,
    ImageMagick's `-crop 0x0` quietly yields the *whole* image -- and a
    caller who asked for an empty rectangle and got back a full-screen
    shot has the worst possible outcome: a plausible-looking image of the
    wrong thing.
    """
    if region is None:
        return None
    if window is not None:
        raise ValueError(
            "capture() takes window= or region=, not both: region is "
            "screen-absolute, so pairing it with a window would silently "
            "mean one thing or the other"
        )
    try:
        x, y, width, height = (int(value) for value in region)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"region must be four numbers (x, y, width, height), got {region!r}"
        ) from exc
    if width <= 0 or height <= 0:
        raise ValueError(
            f"region must have a positive width and height, got {width}x{height}"
        )
    return (x, y, width, height)


class Screen:
    """One output."""

    __slots__ = ("index", "width", "height", "scale", "name")

    def __init__(
        self,
        index: int,
        width: int,
        height: int,
        scale: float = 1.0,
        name: str = "",
    ) -> None:
        """Describe one output."""
        self.index = index
        self.width = width
        self.height = height
        self.scale = scale
        self.name = name

    @property
    def size(self) -> tuple[int, int]:
        """The output's (width, height) in pixels."""
        return (self.width, self.height)

    def __repr__(self):
        return (
            f"Screen({self.index}, {self.width}x{self.height}"
            f"{f'@{self.scale}x' if self.scale != 1.0 else ''}"
            f"{f' {self.name!r}' if self.name else ''})"
        )


class ImageMatch(NamedTuple):
    """Where a template image was found.

    In whichever coordinate space it was searched in -- screen-absolute or
    otherwise -- the same way geometry()'s (x, y, width, height) leaves that
    to the caller.
    """

    x: int
    y: int
    width: int
    height: int
    score: float


class Window:
    """A toplevel window.

    Deliberately not an integer. X11 window ids are reusable, so a stale id can
    silently address a different window; Wayland toplevel handles are objects
    with a lifetime, and this mirrors that. `handle` is backend-private.
    """

    __slots__ = ("handle", "backend", "title", "app_id", "pid")

    def __init__(
        self,
        handle: object,
        backend: GUIBackend,
        title: str = "",
        app_id: str = "",
        pid: int | None = None,
    ) -> None:
        """Describe one toplevel window."""
        self.handle = handle
        self.backend = backend
        self.title = title
        self.app_id = app_id
        self.pid = pid

    def __repr__(self):
        bits = [repr(self.title)]
        if self.app_id:
            bits.append(f"app_id={self.app_id!r}")
        if self.pid:
            bits.append(f"pid={self.pid}")
        return f"Window({', '.join(bits)})"


_SENDKEYS_PLAIN = {
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "`": "grave",
    " ": "space",
    "\n": "Return",
    "\t": "Tab",
}
"""char -> X11 keysym name, unshifted. Values for GUIBackend.resolve_char_key."""

_SENDKEYS_SHIFTED = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "minus",
    "+": "equal",
    "{": "bracketleft",
    "}": "bracketright",
    "|": "backslash",
    ":": "semicolon",
    '"': "apostrophe",
    "<": "comma",
    ">": "period",
    "?": "slash",
    "~": "grave",
}
"""char -> X11 keysym name of its unshifted base key. The other half of
resolve_char_key's default table."""


class GUIBackend(ABC):
    """One way of driving a desktop.

    Subclasses override `capabilities` and the methods they can honour. The
    base implementations raise, so nothing silently no-ops -- the failure mode
    the audit singled out in the Perl module, where every error was a zero.
    """

    name = "abstract"

    MODIFIER_KEYS = {
        "^": "Control_L",
        "%": "Alt_L",
        "+": "Shift_L",
        "#": "Meta_L",
        "&": "ISO_Level3_Shift",
    }
    """send_keys()'s modifier characters, mapped to the name press_key/
    release_key expect for that key on this backend. X11 keysym names by
    default -- correct for X11Backend as-is, and for the xdotool/wdotool/wtype
    tool adapters, which speak the same vocabulary. A backend with a different
    key-naming convention (uinput's evdev names) overrides this."""

    KEY_ALIASES = {
        "BAC": "BackSpace",
        "BS": "BackSpace",
        "BKS": "BackSpace",
        "BRE": "Break",
        "CAN": "Cancel",
        "CAP": "Caps_Lock",
        "DEL": "Delete",
        "DOWN": "Down",
        "END": "End",
        "ENT": "Return",
        "ESC": "Escape",
        "HEL": "Help",
        "HOM": "Home",
        "INS": "Insert",
        "LEF": "Left",
        "NUM": "Num_Lock",
        "PGD": "Next",
        "PGU": "Prior",
        "PRT": "Print",
        "RIG": "Right",
        "SCR": "Scroll_Lock",
        "TAB": "Tab",
        "UP": "Up",
        "F1": "F1",
        "F2": "F2",
        "F3": "F3",
        "F4": "F4",
        "F5": "F5",
        "F6": "F6",
        "F7": "F7",
        "F8": "F8",
        "F9": "F9",
        "F10": "F10",
        "F11": "F11",
        "F12": "F12",
        "SPC": "space",
        "SPA": "space",
        "LSK": "Super_L",
        "RSK": "Super_R",
        "MNU": "Menu",
        "LSH": "Shift_L",
        "RSH": "Shift_R",
        "LCT": "Control_L",
        "RCT": "Control_R",
        "LAL": "Alt_L",
        "RAL": "Alt_R",
        "LMA": "Meta_L",
        "RMA": "Meta_R",
    }
    """send_keys()'s `{BAC}`-style abbreviations, looked up case-insensitively
    and mapped the same way as MODIFIER_KEYS. Ported from X11::GUITest's own
    table. A name absent here is passed to press_key unabbreviated, e.g.
    `{BackSpace}` in place of `{bac}`."""

    def resolve_char_key(self, char: str) -> tuple[str, bool]:
        """The (key name, needs_shift) that presses one character's own key.

        The name goes to press_key/release_key as-is. Used by send_keys() for
        the characters in a modifier group or a `{}`
        brace set, where the *physical* key matters and an already-held
        modifier must combine with it -- unlike type_text, which decides its
        own shift state per character and would fight a modifier the caller
        is already holding.

        A static US-layout ASCII table, matching X11::GUITest's own SendKeys,
        which was ASCII-only. Raises ValueError outside that range; use
        type_text for arbitrary or non-ASCII text instead.
        """
        if len(char) == 1 and char.isascii() and char.isalpha():
            return char.lower(), char.isupper()
        if len(char) == 1 and char.isascii() and char.isdigit():
            return char, False
        if char in _SENDKEYS_PLAIN:
            return _SENDKEYS_PLAIN[char], False
        if char in _SENDKEYS_SHIFTED:
            return _SENDKEYS_SHIFTED[char], True
        raise ValueError(
            f"{char!r} has no static key mapping on {self.name}; "
            "use type_text for arbitrary text"
        )

    @property
    @abstractmethod
    def capabilities(self):
        """A CapabilitySet describing what this backend can actually do."""

    def supports(self, capability: Capability) -> bool:
        """Whether this backend provides `capability`."""
        return capability in self.capabilities

    def require(self, capability: Capability, reason: str | None = None) -> None:
        """Raise unless `capability` is supported. Call at the top of methods."""
        if capability not in self.capabilities:
            raise CapabilityUnsupported(capability, self.name, reason)

    def close(self) -> None:
        """Release compositor connections, portal sessions, virtual devices."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return f"<{type(self).__name__} {self.name!r} caps={len(self.capabilities)}>"

    # -- screens (T2) ------------------------------------------------------

    def screens(self):
        """Every output, in advertised order."""
        self.require(Capability.SCREEN_INFO)
        raise NotImplementedError

    # -- input (T4) --------------------------------------------------------

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position."""
        self.require(Capability.POINTER_MOVE)
        raise NotImplementedError

    def press_button(self, button):
        """Press a mouse button. 1 is left, 2 middle, 3 right."""
        self.require(Capability.POINTER_BUTTON)
        raise NotImplementedError

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        raise NotImplementedError

    def scroll(self, dx=0, dy=0):
        """Scroll by axis steps. X11 buttons 4 and 5 map here, not to buttons."""
        self.require(Capability.POINTER_SCROLL)
        raise NotImplementedError

    def press_key(self, key):
        """Press a key by name, without releasing it."""
        self.require(Capability.KEY_EVENT)
        raise NotImplementedError

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        raise NotImplementedError

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Separate from press_key because correctness depends on the keymap: a
        backend injecting raw scancodes types the wrong characters on a
        non-US layout, and no protocol reports which layout is active.

        `allow_keymap_unsafe` is part of the common signature even though
        only the scancode-injecting backends (uinput, ydotool) act on it --
        a keymap-safe backend accepts and ignores it rather than raising
        TypeError, so a caller does not need to know which backend is active
        to pass the setting a keymap-unsafe suite needs.
        """
        self.require(Capability.TEXT_ENTRY)
        raise NotImplementedError

    # -- windows (T3) ------------------------------------------------------

    def windows(self):
        """Every toplevel currently known."""
        self.require(Capability.WINDOW_LIST)
        raise NotImplementedError

    def active_window(self):
        """The currently focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        raise NotImplementedError

    def window_events(self, timeout=None):
        """Yield WindowEvent objects as the compositor reports them.

        `timeout` bounds the total wait in seconds; None yields indefinitely.
        The one real event feed backends dispatch through -- see
        Session.wait_for_window and Session.wait_window_close, which are
        what most callers want instead of consuming this directly.
        """
        self.require(Capability.WINDOW_EVENTS)
        raise NotImplementedError

    def wait_for_window(self, title, timeout=None):
        """Block until a window matching `title` (regex) appears, or None.

        Session.wait_for_window is the capability-agnostic entry point --
        it calls this only where Capability.WINDOW_EVENTS is available
        (real notification), and polls windows() itself everywhere else.
        """
        self.require(Capability.WINDOW_EVENTS)
        raise NotImplementedError

    def is_window_viewable(self, window):
        """Whether `window` is currently mapped and showing.

        Replaces X11::GUITest's WaitWindowViewable -- a one-shot state read
        here rather than a wait, since it shares WINDOW_STATE's tier and
        polling it in a loop is exactly what a caller already gets for free
        from wait_for_window/wait_window_close.
        """
        self.require(Capability.WINDOW_STATE)
        raise NotImplementedError

    def window_at(self, x, y, screen=0):
        """The topmost window covering a screen coordinate, or None."""
        self.require(Capability.WINDOW_AT_POINT)
        raise NotImplementedError

    def geometry(self, window):
        """(x, y, width, height). The most commonly missing capability."""
        self.require(Capability.WINDOW_GEOMETRY)
        raise NotImplementedError

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y)."""
        self.require(Capability.WINDOW_PLACEMENT)
        raise NotImplementedError

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`."""
        self.require(Capability.WINDOW_RESIZE)
        raise NotImplementedError

    def activate_window(self, window):
        """Raise and focus. There is no raise-without-focus operation."""
        self.require(Capability.WINDOW_ACTIVATE)
        raise NotImplementedError

    def minimize_window(self, window, minimized=True):
        """Minimize a window, or restore it when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        raise NotImplementedError

    def capture(self, window=None, path=None, region=None):
        """Screenshot the whole desktop, one window, or one rectangle.

        Writes a PNG and returns its path; `path` names the file, and one
        is allocated in the temporary directory when it is omitted. No
        image library is involved -- callers wanting pixels hand the path
        to Pillow.

        `region` is a normalized `(x, y, width, height)` in *screen*
        coordinates, the same tuple `geometry()` returns, so a rectangle
        read from one backend can be handed straight to another. Backends
        translate it into whatever their own mechanism wants; no caller
        ever writes grim's "x,y WxH" or ImageMagick's "WxH+X+Y" by hand.

        `window` and `region` are mutually exclusive: `region` is already
        screen-absolute, so pairing the two would mean either interpreting
        it relative to the window (a second, silently different coordinate
        space) or ignoring one of the two arguments. Both are worse than
        refusing.

        The two arguments need different capabilities, and only the one
        actually being used is required. A backend can serve
        WINDOW_CAPTURE without SCREEN_CAPTURE -- X11 under XWayland is
        exactly that, since an X11 client's own drawable is readable there
        while the root is not -- and demanding both would refuse a
        per-window capture for lacking a capability it never needed.
        """
        self.require(
            Capability.WINDOW_CAPTURE
            if window is not None
            else Capability.SCREEN_CAPTURE
        )
        raise NotImplementedError

    # -- images (T1) -------------------------------------------------------

    def locate(self, haystack, template, region=None, metric="RMSE", threshold=None):
        """Find `template`'s position within `haystack`.

        Returns None if no match clears `threshold`. Both are paths to
        already-captured image files; this needs no live display connection,
        only pixels already on disk. `region` restricts the search to
        (x, y, width, height) within `haystack`, in that image's own pixel
        coordinates. `metric` and `threshold` are forwarded to the concrete
        backend's own similarity measure.
        """
        self.require(Capability.IMAGE_LOCATE)
        raise NotImplementedError

    # -- elements (T5) -----------------------------------------------------

    def root_element(self):
        """The accessible-tree root. The replacement for the X11 window tree."""
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError

    def find_elements(self, role=None, name=None, within=None):
        """Search the accessible tree.

        The preferred automation entry point: unaffected by the missing
        geometry and input capabilities, and identical under X11 and Wayland.
        """
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError

    def find_element(self, role=None, name=None, within=None):
        """The first accessible element matching a role and/or name, or None."""
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError
