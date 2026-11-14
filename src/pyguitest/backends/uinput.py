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
where nobody can click Allow. See `docs/developers/adr-002-transports.md`.

Creating the device needs write access to /dev/uinput, normally via the
`input` group.
"""

import contextlib
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

_KEYSYM_NAMES = {
    "return": "ENTER",
    "escape": "ESC",
    "prior": "PAGEUP",
    "next": "PAGEDOWN",
    "page_up": "PAGEUP",
    "page_down": "PAGEDOWN",
    "print": "SYSRQ",
    "sys_req": "SYSRQ",
    "caps_lock": "CAPSLOCK",
    "num_lock": "NUMLOCK",
    "scroll_lock": "SCROLLLOCK",
    "control_l": "LEFTCTRL",
    "control_r": "RIGHTCTRL",
    "alt_l": "LEFTALT",
    "alt_r": "RIGHTALT",
    "shift_l": "LEFTSHIFT",
    "shift_r": "RIGHTSHIFT",
    "meta_l": "LEFTMETA",
    "meta_r": "RIGHTMETA",
    "super_l": "LEFTMETA",
    "super_r": "RIGHTMETA",
    "iso_level3_shift": "RIGHTALT",
    "bracketleft": "LEFTBRACE",
    "bracketright": "RIGHTBRACE",
    "period": "DOT",
    "kp_add": "KPPLUS",
    "kp_subtract": "KPMINUS",
    "kp_multiply": "KPASTERISK",
    "kp_divide": "KPSLASH",
    "kp_enter": "KPENTER",
    "kp_decimal": "KPDOT",
    "kp_separator": "KPCOMMA",
    "kp_0": "KP0",
    "kp_1": "KP1",
    "kp_2": "KP2",
    "kp_3": "KP3",
    "kp_4": "KP4",
    "kp_5": "KP5",
    "kp_6": "KP6",
    "kp_7": "KP7",
    "kp_8": "KP8",
    "kp_9": "KP9",
}
"""X11 keysym names that evdev spells differently, lowercased for lookup.

`press_key`/`tap_key` take a key *name*, and the name this package documents
is the X11 keysym -- what `GUIBackend.KEY_ALIASES` resolves its abbreviations
to, what X11Backend expects, and what the xdotool/wdotool/wtype adapters all
share. This backend speaks evdev, and deriving `KEY_<NAME>` from the keysym
is right only where the two happen to agree.

Where they do not, it failed two ways. `Return`, `Escape`, both `Control_*`
and thirteen more raised `unknown key name` -- the loud half, and how this
was found: a recorded script replayed on a uinput session died on
`gui.tap_key("Return")`. Worse were the two that resolved to a real code for
the wrong physical key: X11's `Next` and `Prior` are Page Down and Page Up,
but `KEY_NEXT`/`KEY_PREVIOUS` are media track controls (407/412, against
PAGEDOWN 109 and PAGEUP 104), and X11's `Print` is Print Screen while
`KEY_PRINT` is a printer key (210, against SYSRQ 99). Those ran cleanly and
pressed something else.

Evdev spellings still work: this is consulted before the `KEY_<NAME>`
fallback, not in place of it, so `press_key("ENTER")` is unchanged -- which
matters because this backend's own `KEY_ALIASES` emit exactly those.

Three more families were the same loud half, found by asking which keysym
names the other backends accept that this one refuses. `Page_Up`/`Page_Down`
are X11's own aliases for `Prior`/`Next` and the spelling `docs/input.md`
actually teaches, so a script written from the documentation died here while
the same call worked on X11, Windows and macOS. The punctuation keysyms
`bracketleft`/`bracketright`/`period` are spelled `LEFTBRACE`/`RIGHTBRACE`/
`DOT` by evdev. And the keypad is spelled `KP_Add`/`KP_0` by X11 against
`KPPLUS`/`KP0` here, for every arithmetic key and every digit.

The shifted-character keysyms -- `asciitilde`, `exclam`, `at`, `numbersign`
-- stay out for the reason the abbreviations above do: there is no single
evdev key for a character that needs Shift held, so there is nothing to map
them to that would not be a guess. `type_text` is the call for those.
"""


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
        # Where this device's absolute axes stand: what the kernel compares
        # the next ABS_X/ABS_Y against before it decides whether anything
        # happened. A new device starts at the declared value, 0 on both.
        self._position = (0, 0)

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

        def axis(pixels):
            """Describe one absolute axis that addresses `pixels` positions.

            The maximum is `pixels - 1`, not `pixels`: libinput maps a value
            onto the screen as `(v - min) * size / (max - min + 1)`, so an
            axis declared 0..1920 spreads 1921 values over 1920 pixels and
            lands `move_mouse(523, 220)` on (522, 219) -- one pixel short,
            growing toward the far edge, and unable to reach the last
            column or row at all. Declared 0..1919 the map is exact.
            """
            return AbsInfo(value=0, min=0, max=pixels - 1, fuzz=0, flat=0, resolution=0)

        capabilities = {
            e.EV_KEY: (
                [e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT]
                # KEY_ESC..KEY_COMPOSE is the keyboard range proper. Sweeping
                # up to KEY_MAX instead pulls in BTN_TOUCH, BTN_TOOL_PEN,
                # BTN_TOOL_FINGER and the joystick range, which combined with
                # the ABS_X/ABS_Y axes below lets libinput classify this
                # device as a touchscreen or tablet instead of a pointer.
                # KEY_POWER (116) excluded even though it falls in that
                # range: confirmed live (Debian 13, GNOME/Wayland, fresh
                # reboot, ruling out session drift) that advertising it tags
                # the whole device `power-switch` in udev -- `udevadm info`
                # showed `CURRENT_TAGS=:power-switch:` with neither `seat`
                # nor `uaccess`, the tags logind normally grants an input
                # device for the active session. Without `uaccess`, Mutter
                # can never open the device at all: every synthetic click
                # and keystroke this backend sent was silently dropped, with
                # no error anywhere, session-wide, surviving a reboot (so
                # not transient session state -- a deterministic
                # classification this device's own declared capabilities
                # triggered). No GUI test needs to simulate a hardware power
                # button; excluding the one key id that causes this is safer
                # than asking every caller to work around a udev
                # misclassification their own device declaration caused.
                + [
                    c
                    for c in range(e.KEY_ESC, e.KEY_COMPOSE + 1)
                    if c in e.keys and c != e.KEY_POWER
                ]
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
        """Move the pointer to an absolute position.

        A move to where this device last put the pointer is stepped one
        pixel aside first, so that it is a motion at all. The kernel drops
        an absolute event whose value equals the axis's current one before
        any handler sees it (`input_handle_abs_event`, and the SYN_REPORT
        goes with it once nothing is pending), so writing the same pair
        again reaches neither libinput nor the compositor: nothing moves,
        nothing is told the pointer is there. A compositor that only
        re-evaluates which surface is under the pointer on motion -- Hyprland,
        measured 2026-11-04 -- then delivers the click that follows to the
        surface it last knew about. The way it showed: a GTK file dialog
        opened under the pointer left by the click that cancelled the
        previous one, `move_mouse()` to its Cancel button's centre, and two
        clicks there did nothing, while a third after a one-pixel nudge
        answered; the same for GTK3 and GTK4, and the same sequence on KDE,
        COSMIC, niri, GNOME and X11 answered the first click. The other
        case this covers is a pointer moved by another device in between:
        the compositor's pointer is elsewhere, this device's axes still say
        `(x, y)`, and the repeated write would leave it elsewhere.
        """
        self.require(Capability.POINTER_MOVE)
        x, y = int(x), int(y)
        if (x, y) == self._position:
            self._write_position(x + 1 if x + 1 < self.screen_size[0] else x - 1, y)
        self._write_position(x, y)

    def _write_position(self, x, y):
        """Write both axes and synchronise, remembering where they stand."""
        e = self._ecodes
        self._device.write(e.EV_ABS, e.ABS_X, x)
        self._device.write(e.EV_ABS, e.ABS_Y, y)
        self._device.syn()
        self._position = (x, y)

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
        """Translate a key name to its evdev code.

        Accepts this package's documented X11 keysym names as well as evdev's
        own -- see `_KEYSYM_NAMES` for which of the two spellings differ, and
        for the two that used to resolve to the wrong physical key.
        """
        if key.startswith("KEY_"):
            name = key
        else:
            name = f"KEY_{_KEYSYM_NAMES.get(key.lower(), key.upper())}"
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
            try:
                self._device.write(e.EV_KEY, code, 1)
                self._device.write(e.EV_KEY, code, 0)
            except BaseException:
                # The Shift is already down in the kernel, and this device
                # outlives the call -- it is held open for the session -- so
                # a write that dies between the press and the release leaves
                # Shift logically held for every later keystroke, turning the
                # rest of the run into capitals and symbols with nothing
                # raised to say so. Not a plain `finally`, for Session.drag's
                # reason: the release is likely to fail the same way the
                # character did, and in a `finally` that second error would
                # replace the one that actually explains the failure.
                if shift:
                    with contextlib.suppress(Exception):
                        self._device.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
                        self._device.syn()
                raise
            if shift:
                self._device.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            self._device.syn()
            if delay:
                time.sleep(delay)
