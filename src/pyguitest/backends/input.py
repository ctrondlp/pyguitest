"""Input injection, adapting external tools.

No binding, no device handling: each supported tool already speaks the right
mechanism, so this builds an argv and runs it. The tool is chosen by
tools.INPUT_TOOLS preference order, which ranks keymap-safe tools first.

`runner` is injectable so argv construction can be tested without a compositor
-- getting ydotool's syntax subtly wrong is the likely bug here, not the
subprocess call.
"""

from __future__ import annotations

import math
import shlex
import subprocess
import warnings
from collections.abc import Callable

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported, PyGUITestError
from .base import GUIBackend

__all__ = ["ToolInputBackend", "KeymapWarning"]

CommandTable = dict[str, Callable[..., list[str]]]
"""One tool's command builders, keyed by operation name."""


_SUBPROCESS_TIMEOUT = 10
"""Default seconds before a hung input tool is given up on and reported.

Override per backend via ToolInputBackend(tool, timeout=...) for a call
expected to run longer than this, such as a long type_text with a large
per-key delay.
"""

# ydotool's `key` subcommand takes numeric Linux input-event codes
# ("29:1"), not names -- the one tool here whose key vocabulary is not the
# X11 keysym names GUIBackend's own MODIFIER_KEYS/KEY_ALIASES/
# resolve_char_key default to (which xdotool, wdotool and wtype all share).
# Values verified against include/uapi/linux/input-event-codes.h, a stable
# kernel ABI that has not renumbered these in decades.
_YDOTOOL_MODIFIER_KEYS = {
    "^": "29",  # KEY_LEFTCTRL
    "%": "56",  # KEY_LEFTALT
    "+": "42",  # KEY_LEFTSHIFT
    "#": "125",  # KEY_LEFTMETA
    "&": "100",  # KEY_RIGHTALT
}

_YDOTOOL_KEY_ALIASES = {
    "BAC": "14",  # KEY_BACKSPACE
    "BS": "14",
    "BKS": "14",
    "CAP": "58",  # KEY_CAPSLOCK
    "DEL": "111",  # KEY_DELETE
    "DOWN": "108",  # KEY_DOWN
    "UP": "103",  # KEY_UP
    "LEF": "105",  # KEY_LEFT
    "RIG": "106",  # KEY_RIGHT
    "END": "107",  # KEY_END
    "HOM": "102",  # KEY_HOME
    "ENT": "28",  # KEY_ENTER
    "ESC": "1",  # KEY_ESC
    "INS": "110",  # KEY_INSERT
    "NUM": "69",  # KEY_NUMLOCK
    "PGD": "109",  # KEY_PAGEDOWN
    "PGU": "104",  # KEY_PAGEUP
    "SCR": "70",  # KEY_SCROLLLOCK
    "TAB": "15",  # KEY_TAB
    "F1": "59",
    "F2": "60",
    "F3": "61",
    "F4": "62",
    "F5": "63",
    "F6": "64",
    "F7": "65",
    "F8": "66",
    "F9": "67",
    "F10": "68",
    "F11": "87",
    "F12": "88",
    "SPC": "57",  # KEY_SPACE
    "SPA": "57",
    "MNU": "127",  # KEY_COMPOSE
    "LSH": "42",  # KEY_LEFTSHIFT
    "RSH": "54",  # KEY_RIGHTSHIFT
    "LCT": "29",  # KEY_LEFTCTRL
    "RCT": "97",  # KEY_RIGHTCTRL
    "LAL": "56",  # KEY_LEFTALT
    "RAL": "100",  # KEY_RIGHTALT
    "LMA": "125",  # KEY_LEFTMETA
    "RMA": "126",  # KEY_RIGHTMETA
    "LSK": "125",  # same as LMA: evdev has no separate Super_L/R keycode
    "RSK": "126",  # same as RMA: ditto
}
"""ydotool's version of GUIBackend.KEY_ALIASES: send_keys()'s `{BAC}`-style
abbreviations, but as numeric codes. BRE(ak), CAN(cel), HEL(p) and PRT are
left out, same as UinputBackend -- no settled evdev key for them either."""

_YDOTOOL_LETTERS = {
    "a": "30",
    "b": "48",
    "c": "46",
    "d": "32",
    "e": "18",
    "f": "33",
    "g": "34",
    "h": "35",
    "i": "23",
    "j": "36",
    "k": "37",
    "l": "38",
    "m": "50",
    "n": "49",
    "o": "24",
    "p": "25",
    "q": "16",
    "r": "19",
    "s": "31",
    "t": "20",
    "u": "22",
    "v": "47",
    "w": "17",
    "x": "45",
    "y": "21",
    "z": "44",
}
_YDOTOOL_DIGITS = {
    "0": "11",
    "1": "2",
    "2": "3",
    "3": "4",
    "4": "5",
    "5": "6",
    "6": "7",
    "7": "8",
    "8": "9",
    "9": "10",
}
_YDOTOOL_PLAIN = {
    "-": "12",
    "=": "13",
    "[": "26",
    "]": "27",
    "\\": "43",
    ";": "39",
    "'": "40",
    ",": "51",
    ".": "52",
    "/": "53",
    "`": "41",
    " ": "57",
    "\n": "28",
    "\t": "15",
}
_YDOTOOL_SHIFTED = {
    "!": "2",
    "@": "3",
    "#": "4",
    "$": "5",
    "%": "6",
    "^": "7",
    "&": "8",
    "*": "9",
    "(": "10",
    ")": "11",
    "_": "12",
    "+": "13",
    "{": "26",
    "}": "27",
    "|": "43",
    ":": "39",
    '"': "40",
    "<": "51",
    ">": "52",
    "?": "53",
    "~": "41",
}
"""ydotool's version of the _SENDKEYS_PLAIN/_SENDKEYS_SHIFTED split in
backends/base.py, and structurally identical to UinputBackend's own
_PLAIN/_SHIFTED -- same categorisation, numeric codes instead of names."""


def _ydotool_resolve_char_key(char: str) -> tuple[str, bool]:
    """resolve_char_key for ydotool: numeric code instead of a keysym name."""
    if len(char) == 1 and char.isascii() and char.isalpha():
        return _YDOTOOL_LETTERS[char.lower()], char.isupper()
    if char in _YDOTOOL_DIGITS:
        return _YDOTOOL_DIGITS[char], False
    if char in _YDOTOOL_PLAIN:
        return _YDOTOOL_PLAIN[char], False
    if char in _YDOTOOL_SHIFTED:
        return _YDOTOOL_SHIFTED[char], True
    raise ValueError(f"{char!r} has no static key mapping on ydotool")


def _delay_ms(delay: float) -> int:
    """Convert a delay in seconds to whole milliseconds, rounding up.

    `int()` truncation floors any sub-millisecond delay to 0, which drops a
    tool's --delay flag entirely and types at full speed with no warning.
    """
    return math.ceil(delay * 1000) if delay else 0


class KeymapWarning(UserWarning):
    """Typed text may not match the requested characters.

    Warned when text is typed through a tool that injects scancodes below the
    compositor: the session's active xkb layout is applied, so "Hello" on an
    AZERTY session arrives as something else.
    """


# Buttons use X11 numbering (1 left, 2 middle, 3 right), the convention this
# API inherits from X11::GUITest's M_BTN1..M_BTN5. ydotool wants evdev codes
# -- 0x00 LEFT, 0x01 RIGHT, 0x02 MIDDLE, note the swap against X11 numbering
# -- with 0x40 meaning press and 0x80 meaning release.
_YDOTOOL_BUTTONS = {1: 0x00, 2: 0x02, 3: 0x01}


def _scroll_argv(name: str, dx: int, dy: int) -> list[str]:
    """Build an xdotool-style scroll: buttons 4/5 vertical, 6/7 horizontal.

    Each axis is its own `click --repeat N button` clause, chained in one
    invocation -- xdotool reads multiple actions from a single argv -- so
    magnitude is honoured on both axes instead of collapsing to one click,
    and a horizontal-only request never touches the vertical buttons.
    """
    argv = [name]
    if dy:
        argv += ["click", "--repeat", str(abs(dy)), "4" if dy > 0 else "5"]
    if dx:
        argv += ["click", "--repeat", str(abs(dx)), "7" if dx > 0 else "6"]
    return argv


def _xdotool_like(name: str) -> CommandTable:
    """Wdotool deliberately mirrors xdotool's command surface."""
    return {
        "move": lambda x, y: [name, "mousemove", str(x), str(y)],
        "button_down": lambda b: [name, "mousedown", str(b)],
        "button_up": lambda b: [name, "mouseup", str(b)],
        "scroll": lambda dx, dy: _scroll_argv(name, dx, dy),
        "key_down": lambda k: [name, "keydown", k],
        "key_up": lambda k: [name, "keyup", k],
        "type": lambda t, ms=0: (
            [name, "type"] + (["--delay", str(ms)] if ms else []) + ["--", t]
        ),
    }


_COMMANDS: dict[str, CommandTable] = {
    "wdotool": _xdotool_like("wdotool"),
    "xdotool": _xdotool_like("xdotool"),
    "wtype": {
        "key_down": lambda k: ["wtype", "-P", k],
        "key_up": lambda k: ["wtype", "-p", k],
        "type": lambda t, ms=0: ["wtype"] + (["-d", str(ms)] if ms else []) + ["--", t],
    },
    "ydotool": {
        "move": lambda x, y: [
            "ydotool",
            "mousemove",
            "--absolute",
            "-x",
            str(x),
            "-y",
            str(y),
        ],
        "button_down": lambda b: [
            "ydotool",
            "click",
            f"0x{0x40 | _YDOTOOL_BUTTONS[b]:02x}",
        ],
        "button_up": lambda b: [
            "ydotool",
            "click",
            f"0x{0x80 | _YDOTOOL_BUTTONS[b]:02x}",
        ],
        "scroll": lambda dx, dy: [
            "ydotool",
            "mousemove",
            "--wheel",
            "-x",
            str(dx),
            "-y",
            str(dy),
        ],
        "key_down": lambda k: ["ydotool", "key", f"{k}:1"],
        "key_up": lambda k: ["ydotool", "key", f"{k}:0"],
        "type": lambda t, ms=0: (
            ["ydotool", "type"] + (["--key-delay", str(ms)] if ms else []) + ["--", t]
        ),
    },
}


class ToolInputBackend(GUIBackend):
    """Injection through whichever input tool is installed."""

    def __init__(self, tool, runner=None, timeout=_SUBPROCESS_TIMEOUT):
        """Drive `tool`, optionally through an injected `runner`.

        `timeout` bounds every command this backend runs, `type_text`
        included -- raise it for a long string typed with a large per-key
        delay, where the default would otherwise cut a legitimate call short.
        """
        if tool.name not in _COMMANDS:
            raise BackendUnavailable(f"no command mapping for {tool.name!r}")
        self.tool = tool
        self._commands = _COMMANDS[tool.name]
        self._timeout = timeout
        self._runner = runner or self._run

    # GUIBackend.name/MODIFIER_KEYS/KEY_ALIASES are plain, writable class
    # attributes so a subclass can just set e.g. `name = "atspi"`; overriding
    # them with a read-only @property here is what lets this backend derive
    # them from `self.tool` instead. Nothing assigns to them externally, so
    # the narrowing is safe -- mypy checks it against the general case, not
    # this codebase.
    @property
    def name(self) -> str:  # type: ignore[override]
        """Identifier for this backend, e.g. 'input:wdotool'."""
        return f"input:{self.tool.name}"

    @property
    def capabilities(self):
        # Intersect what the tool claims with what this adapter can actually
        # build a command for: wtype has no pointer commands, so it must not
        # advertise pointer capabilities however it is registered.
        """What the tool claims, narrowed to what this adapter can build."""
        buildable = set()
        if "move" in self._commands:
            buildable.add(Capability.POINTER_MOVE)
        if "button_down" in self._commands:
            buildable.add(Capability.POINTER_BUTTON)
        if "scroll" in self._commands:
            buildable.add(Capability.POINTER_SCROLL)
        if "key_down" in self._commands:
            buildable.add(Capability.KEY_EVENT)
        if "type" in self._commands:
            buildable.add(Capability.TEXT_ENTRY)
        return CapabilitySet(buildable & set(self.tool.capabilities))

    @property
    def keymap_safe(self):
        """Whether typed text is guaranteed to match the requested characters."""
        return self.tool.keymap_safe

    # -- send_keys() key-name tables ---------------------------------------
    #
    # xdotool, wdotool and wtype all take X11 keysym names, so GUIBackend's
    # own defaults already work for them unmodified. ydotool alone needs
    # numeric evdev codes instead -- these three cannot be plain class
    # attributes like every other backend's because the right table depends
    # on which tool this instance wraps, decided only at __init__ time.

    @property
    def MODIFIER_KEYS(self):  # type: ignore[override]
        """send_keys()'s modifiers, in whichever vocabulary self.tool speaks."""
        if self.tool.name == "ydotool":
            return _YDOTOOL_MODIFIER_KEYS
        return GUIBackend.MODIFIER_KEYS

    @property
    def KEY_ALIASES(self):  # type: ignore[override]
        """send_keys()'s `{BAC}`-style abbreviations, ditto."""
        if self.tool.name == "ydotool":
            return _YDOTOOL_KEY_ALIASES
        return GUIBackend.KEY_ALIASES

    def resolve_char_key(self, char):
        """The (key name, needs_shift) send_keys() uses to press one char."""
        if self.tool.name == "ydotool":
            return _ydotool_resolve_char_key(char)
        return super().resolve_char_key(char)

    def _run(self, argv):
        """Run `argv`, raising if the tool reports failure or hangs."""
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(f"{shlex.join(argv)} timed out") from exc
        if result.returncode != 0:
            # Prefer stderr, the conventional stream for error text, but some
            # tools (ydotool included, on some builds) print theirs to stdout
            # instead -- falling back silently to "no output" when stdout
            # actually held the reason was a real, live source of confusion.
            output = result.stderr.strip() or result.stdout.strip() or "no output"
            raise PyGUITestError(
                f"{shlex.join(argv)} failed ({result.returncode}): {output}"
            )
        return result

    # -- pointer -----------------------------------------------------------

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position."""
        self.require(Capability.POINTER_MOVE)
        return self._runner(self._commands["move"](x, y))

    def _check_button(self, button):
        """Raise a typed error for a button this tool has no mapping for.

        ydotool's command table is a plain dict keyed by X11 button number;
        an unmapped one would otherwise surface as a bare KeyError from deep
        inside the lambda, indistinguishable from a bug in this package.
        """
        if self.tool.name == "ydotool" and button not in _YDOTOOL_BUTTONS:
            raise CapabilityUnsupported(
                Capability.POINTER_BUTTON,
                self.name,
                f"button {button!r} has no ydotool mapping",
            )

    def press_button(self, button):
        """Press a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._check_button(button)
        return self._runner(self._commands["button_down"](button))

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        self._check_button(button)
        return self._runner(self._commands["button_up"](button))

    def scroll(self, dx=0, dy=0):
        """Scroll by axis steps."""
        self.require(Capability.POINTER_SCROLL)
        if not dx and not dy:
            return None
        return self._runner(self._commands["scroll"](dx, dy))

    # -- keyboard ----------------------------------------------------------

    def press_key(self, key):
        """Press a key by name."""
        self.require(Capability.KEY_EVENT)
        return self._runner(self._commands["key_down"](key))

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        return self._runner(self._commands["key_up"](key))

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text`, pausing `delay` seconds between characters.

        The delay becomes the tool's own inter-key flag rather than a Python
        sleep, so the whole string is still one invocation.

        Warns when the active tool cannot guarantee the characters arrive as
        written; pass allow_keymap_unsafe=False to refuse instead, which is the
        right setting for a suite that asserts on typed content.
        """
        self.require(Capability.TEXT_ENTRY)
        if not self.tool.keymap_safe:
            message = (
                f"{self.tool.name} injects scancodes below the compositor; typed "
                "text depends on the session's keyboard layout"
            )
            if not allow_keymap_unsafe:
                raise PyGUITestError(message)
            warnings.warn(message, KeymapWarning, stacklevel=2)
        return self._runner(self._commands["type"](text, _delay_ms(delay)))
