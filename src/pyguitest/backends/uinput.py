"""In-process input injection via python-evdev.

The one input layer where a mature, maintained Python library exists. Unlike
the CLI adapters this holds a virtual device open for the life of the session,
so there is no process spawn per event -- which matters for a suite doing
thousands of operations.

It carries uinput's inherent limitation and cannot escape it: events are
injected *below* the compositor, which then applies the session's active xkb
layout. Typing is therefore keymap-unsafe here exactly as it is with ydotool,
and TEXT_ENTRY warns. `eiinput.py` is the path that escapes this: libei hands
the client the compositor's own XKB keymap, and `xkb.py` compiles it, so
typing there is a lookup rather than a guess. It is opt-in (it can raise a
consent dialog), so this backend remains the in-process option for sessions
where nobody can click Allow. See `docs/adr-002-transports.md`.

Creating the device needs write access to /dev/uinput, normally via the
`input` group.
"""

import os
import time
import warnings

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, PermissionRequired, PyGUITestError
from .base import GUIBackend
from .input import KeymapWarning

__all__ = ["UinputBackend", "available"]


def _evdev():
    """Import python-evdev, or return None when it is absent."""
    try:
        from evdev import AbsInfo, UInput, ecodes
    except Exception:
        return None
    return UInput, AbsInfo, ecodes


def available():
    """Whether the library this backend needs is importable."""
    return _evdev() is not None


# ASCII to evdev key name, with whether shift is required. This table is what
# makes typing layout-dependent: it names US-layout positions, and the
# compositor maps those positions through whatever layout is actually active.
_SHIFTED = {
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
    "_": "MINUS",
    "+": "EQUAL",
    "{": "LEFTBRACE",
    "}": "RIGHTBRACE",
    "|": "BACKSLASH",
    ":": "SEMICOLON",
    '"': "APOSTROPHE",
    "<": "COMMA",
    ">": "DOT",
    "?": "SLASH",
    "~": "GRAVE",
}
_PLAIN = {
    "-": "MINUS",
    "=": "EQUAL",
    "[": "LEFTBRACE",
    "]": "RIGHTBRACE",
    "\\": "BACKSLASH",
    ";": "SEMICOLON",
    "'": "APOSTROPHE",
    ",": "COMMA",
    ".": "DOT",
    "/": "SLASH",
    "`": "GRAVE",
    " ": "SPACE",
    "\n": "ENTER",
    "\t": "TAB",
}


class UinputBackend(GUIBackend):
    """A virtual keyboard and absolute pointer, held open for the session."""

    name = "uinput"
    keymap_safe = False

    MODIFIER_KEYS = {
        "^": "LEFTCTRL",
        "%": "LEFTALT",
        "+": "LEFTSHIFT",
        "#": "LEFTMETA",
        "&": "RIGHTALT",
    }
    """send_keys()'s modifiers, in evdev names. `&` (AltGr) is conventionally
    the right Alt key rather than a distinct keysym, unlike on X11."""

    KEY_ALIASES = {
        "BAC": "BACKSPACE", "BS": "BACKSPACE", "BKS": "BACKSPACE",
        "CAP": "CAPSLOCK", "DEL": "DELETE", "DOWN": "DOWN",
        "END": "END", "ENT": "ENTER", "ESC": "ESC",
        "HOM": "HOME", "INS": "INSERT", "LEF": "LEFT",
        "NUM": "NUMLOCK", "PGD": "PAGEDOWN", "PGU": "PAGEUP",
        "RIG": "RIGHT", "SCR": "SCROLLLOCK", "TAB": "TAB",
        "UP": "UP",
        "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4",
        "F5": "F5", "F6": "F6", "F7": "F7", "F8": "F8",
        "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
        "SPC": "SPACE", "SPA": "SPACE", "MNU": "COMPOSE",
        "LSH": "LEFTSHIFT", "RSH": "RIGHTSHIFT",
        "LCT": "LEFTCTRL", "RCT": "RIGHTCTRL",
        "LAL": "LEFTALT", "RAL": "RIGHTALT",
        "LMA": "LEFTMETA", "RMA": "RIGHTMETA",
        "LSK": "LEFTMETA", "RSK": "RIGHTMETA",
    }  # fmt: skip
    """send_keys()'s `{BAC}`-style abbreviations, in evdev names. Narrower
    than the X11-keysym default: BRE(ak), CAN(cel), HEL(p) and PRT have no
    settled evdev equivalent on a standard keyboard, so they are left out
    rather than guessed at -- send_keys raises for them here instead of
    silently pressing the wrong key."""

    def __init__(self, screen_size=(1920, 1080), device=None):
        """Create the virtual device, or accept an injected one for tests."""
        modules = _evdev()
        if modules is None:
            raise BackendUnavailable(
                "python-evdev is not installed; pip install 'pyguitest[uinput]'"
            )
        self._UInput, self._AbsInfo, self._ecodes = modules
        self.screen_size = screen_size
        self._device = device or self._create_device()

    def _create_device(self):
        """Register a virtual keyboard and absolute pointer with the kernel."""
        # evdev's UInput.__init__ catches the OSError an inaccessible
        # /dev/uinput raises and re-wraps it as its own UInputError, so the
        # `except PermissionError` below never actually fires for that case
        # -- checked explicitly first, so the input-group hint still reaches
        # the caller instead of the generic BackendUnavailable it would
        # otherwise fall through to.
        path = "/dev/uinput"
        if os.path.exists(path) and not os.access(path, os.W_OK):
            raise PermissionRequired(
                Capability.POINTER_MOVE,
                self.name,
                f"cannot open {path}: permission denied; "
                "add your user to the 'input' group",
            )
        e, AbsInfo = self._ecodes, self._AbsInfo
        width, height = self.screen_size

        def axis(maximum):
            """Describe one absolute axis spanning 0 to `maximum`."""
            return AbsInfo(value=0, min=0, max=maximum, fuzz=0, flat=0, resolution=0)

        capabilities = {
            e.EV_KEY: (
                [e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT]
                # KEY_ESC..KEY_COMPOSE is the keyboard range proper. Sweeping
                # up to KEY_MAX instead pulls in BTN_TOUCH, BTN_TOOL_PEN,
                # BTN_TOOL_FINGER and the joystick range, which combined with
                # the ABS_X/ABS_Y axes below lets libinput classify this
                # device as a touchscreen or tablet instead of a pointer.
                + [c for c in range(e.KEY_ESC, e.KEY_COMPOSE + 1) if c in e.keys]
            ),
            e.EV_ABS: [(e.ABS_X, axis(width)), (e.ABS_Y, axis(height))],
            e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
        }
        try:
            device = self._UInput(capabilities, name="pyguitest")
        except PermissionError as exc:
            raise PermissionRequired(
                Capability.POINTER_MOVE,
                self.name,
                f"cannot open /dev/uinput ({exc}); add your user to the 'input' group",
            ) from exc
        except Exception as exc:
            raise BackendUnavailable(f"cannot create a uinput device: {exc}") from exc
        # A freshly created uinput device is not immediately usable: udev has
        # not finished processing it yet, and events written in that window
        # are routinely dropped. This is the first thing a caller does, so
        # it is the action most likely to silently vanish without the wait.
        time.sleep(0.2)
        return device

    def close(self):
        """Remove the virtual device."""
        self._device.close()

    @property
    def capabilities(self):
        """The input capabilities a virtual device can provide."""
        return CapabilitySet(
            {
                Capability.POINTER_MOVE,
                Capability.POINTER_BUTTON,
                Capability.POINTER_SCROLL,
                Capability.KEY_EVENT,
                Capability.TEXT_ENTRY,
            }
        )

    def _emit(self, etype, code, value):
        """Write one event and synchronise."""
        self._device.write(etype, code, value)
        self._device.syn()

    # -- pointer -----------------------------------------------------------

    _BUTTONS = {1: "BTN_LEFT", 2: "BTN_MIDDLE", 3: "BTN_RIGHT"}

    def _button(self, button):
        """Translate an X11-style button number to its evdev code."""
        try:
            return getattr(self._ecodes, self._BUTTONS[button])
        except KeyError:
            raise ValueError(f"unsupported button {button!r}") from None

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position."""
        self.require(Capability.POINTER_MOVE)
        e = self._ecodes
        self._device.write(e.EV_ABS, e.ABS_X, int(x))
        self._device.write(e.EV_ABS, e.ABS_Y, int(y))
        self._device.syn()

    def press_button(self, button):
        """Press a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._emit(self._ecodes.EV_KEY, self._button(button), 1)

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._emit(self._ecodes.EV_KEY, self._button(button), 0)

    def scroll(self, dx=0, dy=0):
        """Scroll by wheel steps, on whichever axes are non-zero."""
        self.require(Capability.POINTER_SCROLL)
        e = self._ecodes
        if dy:
            self._emit(e.EV_REL, e.REL_WHEEL, int(dy))
        if dx:
            self._emit(e.EV_REL, e.REL_HWHEEL, int(dx))

    # -- keyboard ----------------------------------------------------------

    def _keycode(self, key):
        """Translate a key name to its evdev code."""
        name = key if key.startswith("KEY_") else f"KEY_{key.upper()}"
        code = getattr(self._ecodes, name, None)
        if code is None:
            raise ValueError(f"unknown key name {key!r}")
        return code

    def press_key(self, key):
        """Press a key by name."""
        self.require(Capability.KEY_EVENT)
        self._emit(self._ecodes.EV_KEY, self._keycode(key), 1)

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        self._emit(self._ecodes.EV_KEY, self._keycode(key), 0)

    def _char_to_key(self, char):
        """Map one character to (key name, shift required)."""
        # ASCII only: the table names US-layout key *positions*, and there is
        # no position for an accented letter. Non-ASCII text needs a
        # keymap-aware transport (libei) rather than raw scancodes.
        if char.isascii() and char.isalpha():
            return char.upper(), char.isupper()
        if char.isascii() and char.isdigit():
            return char, False
        if char in _SHIFTED:
            return _SHIFTED[char], True
        if char in _PLAIN:
            return _PLAIN[char], False
        raise PyGUITestError(
            f"cannot type character {char!r} through uinput: only ASCII is "
            "reachable by scancode; use a keymap-aware backend for this text"
        )

    def resolve_char_key(self, char):
        """The (key name, needs_shift) send_keys() uses to press one char.

        Same table type_text() already relies on -- ASCII-only, and layout-
        dependent for the same reason: see the module docstring.
        """
        return self._char_to_key(char)

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        Layout-dependent by construction -- see the module docstring.
        """
        self.require(Capability.TEXT_ENTRY)
        message = (
            "uinput injects scancodes below the compositor; typed text depends "
            "on the session's keyboard layout"
        )
        if not allow_keymap_unsafe:
            raise PyGUITestError(message)
        warnings.warn(message, KeymapWarning, stacklevel=2)

        e = self._ecodes
        for char in text:
            name, shift = self._char_to_key(char)
            code = self._keycode(name)
            if shift:
                self._device.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
            self._device.write(e.EV_KEY, code, 1)
            self._device.write(e.EV_KEY, code, 0)
            if shift:
                self._device.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            self._device.syn()
            if delay:
                time.sleep(delay)
