"""The Windows backend, and the key vocabulary it is built on.

This is the in-process half of Windows support: window enumeration and
control, monitors and their scale, input injection, screen capture and the
clipboard, all through `ctypes` against DLLs every Windows process already has
loaded. The element tree is a separate backend (`uia.py`), because it is the
only part that needs `comtypes` -- so `pip install pyguitest` on Windows gets
everything here, and the `windows` extra adds elements.

Two names to keep apart before reading on. `backends/windows.py` -- plural --
is the **compositor IPC** module for sway, Hyprland, niri and KWin, where
"windows" means GUI windows; it is registered as the "windows" backend and is
not this. `pyguitest.backends.win32` is this file, named after `sys.platform`
as `SessionType.WIN32` is. The other `win32` in a Windows reader's mind is
`pywin32`'s top-level package, which is unrelated and never imported here.

What this backend deliberately does **not** serve, and why:

- **Elements.** They are UI Automation's, and `uia.py` owns them.
- **`WINDOW_CAPTURE`.** Capturing one window un-occluded needs either
  `PrintWindow` (a later phase) or Windows.Graphics.Capture, and declaring the
  capability before one of them lands would make `supports()` claim that
  `screenshot(window=...)` excludes an occluding window when it does not.
  `CompositeBackend`'s geometry-plus-crop path answers that call meanwhile --
  honestly, because the crop really does include what is on top.
- **`INPUT_SYNC`.** `SendInput`'s return value is a count of events queued,
  not an acknowledgement that anything consumed them, and it cannot even
  report the failure that matters: a UIPI-blocked call returns the full count
  and delivers nothing. `sync()` refuses with that as its reason rather than
  pretending a sleep is a round trip.

`WINDOW_EVENTS` *is* served, through `SetWinEventHook` rather than a message
pump this backend keeps running: `window_events()` installs the hook and a
dedicated pump thread for the life of one call, the same per-call lifecycle
`GnomeShellBackend.window_events` uses for its D-Bus subscription. See that
method's docstring for the event set, the `known`-handles bookkeeping that
tells "new" from "title" and filters a WinEvent hook's much noisier stream
down to the toplevels `windows()` would also report, and what is deliberately
not covered (`EVENT_OBJECT_SHOW`/`_HIDE`, `EVENT_OBJECT_LOCATIONCHANGE`,
`EVENT_SYSTEM_MINIMIZEEND` -- the design document's own "extras").

This module has not yet driven a real Windows desktop. The suite it belongs
to passes on Windows 11 build 26200, so every prototype here at least loads
and every call is shaped as declared -- but that run was over SSH, which is
not on an interactive window station, so nothing below has moved a pointer,
enumerated a real window or captured a real screen. Every prototype lives in
`_winapi.py`, transcribed from Microsoft's documentation, and the tests drive
fakes; what a real desktop still has to confirm is listed in
docs/validation.md's "Not run live" section.

Windows has three keyboard vocabularies where X11 has two:

- **Virtual keys** (`VK_*`): logical keys, resolved through the active
  layout. `KEYBDINPUT.wVk` with no flag injects these.
- **Scan codes**: physical positions, layout-blind, optionally prefixed with
  `0xE0` for the extended keys. Injected with `KEYEVENTF_SCANCODE`, marked
  with `KEYEVENTF_EXTENDEDKEY`. This is what ydotool does on Linux, and it
  carries the same warning about a non-US layout.
- **Characters**: `KEYEVENTF_UNICODE` puts a UTF-16 code unit in the event
  with `wVk` zero, and the system synthesises a keystroke from it. That is
  the layout-independent route, and what `type_text` should use by default.

The names in every table here are **this package's**, not the platform's:
what `press_key` and `send_keys` accept is an X11 keysym name, because that
is what `GUIBackend.KEY_ALIASES` resolves to and what the recorder carries
through the whole pipeline as `RawEvent.keysym`. So these tables translate
*from* that vocabulary *to* Windows' -- the same direction as
`uinput.py`'s `_KEYSYM_NAMES` translates from it to evdev names, and keyed
the same way, by the lower-cased keysym spelling.

Three names of that translation are public -- `virtual_key_code`,
`key_names_for_virtual_key` and `key_name_for_virtual_key` -- because the other
half of a recorder needs them rather than this backend does: a Windows input
hook reports a *virtual-key code*, and turning that back into a name a script
can say is this same vocabulary boundary read the other way. All three are pure
table lookups with no backend behind them, so they answer before anything is
connected, and the plural one is where the codes whose translation is not
one-to-one are accounted for.

One divergence from Windows' own SendKeys dialect is deliberate, and it is
recorded here rather than discovered later: **`~` stays a printable tilde**,
not Enter. It is the most familiar spelling in that lineage --
`SendKeys("%fn~")` is Alt-F, n, Enter -- and it is also a character
`type_text("~")` legitimately types, which `_SENDKEYS_SHIFTED` maps to
`grave` because that is the key it sits on. Making it an alias for Enter
would silently break typing a tilde. The long names in
`_SENDKEYS_LONG_NAMES` are the other half of that compromise: `{ENTER}` and
`{BACKSPACE}` are accepted as second names for `{ENT}` and `{BAC}`, while
the short forms stay canonical, because the recorder emits those.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import tempfile
import threading
import time

from .. import png as _png
from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PermissionRequired,
    PyGUITestError,
    WindowNotFound,
)
from . import _winapi
from .base import GUIBackend, Screen, Window, check_region
from .windows import WindowEvent

__all__ = [
    "Win32Backend",
    "VK",
    "available",
    "virtual_key_code",
    "key_name_for_virtual_key",
    "key_names_for_virtual_key",
]

VK = {
    "VK_CANCEL": 0x03,
    "VK_BACK": 0x08,
    "VK_TAB": 0x09,
    "VK_RETURN": 0x0D,
    "VK_PAUSE": 0x13,
    "VK_CAPITAL": 0x14,
    "VK_ESCAPE": 0x1B,
    "VK_SPACE": 0x20,
    "VK_PRIOR": 0x21,
    "VK_NEXT": 0x22,
    "VK_END": 0x23,
    "VK_HOME": 0x24,
    "VK_LEFT": 0x25,
    "VK_UP": 0x26,
    "VK_RIGHT": 0x27,
    "VK_DOWN": 0x28,
    "VK_SNAPSHOT": 0x2C,
    "VK_INSERT": 0x2D,
    "VK_DELETE": 0x2E,
    "VK_HELP": 0x2F,
    "VK_LWIN": 0x5B,
    "VK_RWIN": 0x5C,
    "VK_APPS": 0x5D,
    "VK_MULTIPLY": 0x6A,
    "VK_ADD": 0x6B,
    "VK_SEPARATOR": 0x6C,
    "VK_SUBTRACT": 0x6D,
    "VK_DECIMAL": 0x6E,
    "VK_DIVIDE": 0x6F,
    "VK_NUMLOCK": 0x90,
    "VK_SCROLL": 0x91,
    "VK_LSHIFT": 0xA0,
    "VK_RSHIFT": 0xA1,
    "VK_LCONTROL": 0xA2,
    "VK_RCONTROL": 0xA3,
    "VK_LMENU": 0xA4,
    "VK_RMENU": 0xA5,
}
"""Virtual-key name -> code, for the keys this package can name.

A subset of Windows' `VK_*` constants deliberately, rather than a transcribed
list: it holds the keys `_KEYSYM_VK` and `_MODIFIER_KEYS` can produce, so a
name missing here is a name this package cannot press. The numpad and
function keys are added below, where two runs say it more clearly than
forty-two more lines of literal would.
"""

VK.update({f"VK_NUMPAD{n}": 0x60 + n for n in range(10)})
VK.update({f"VK_F{n}": 0x6F + n for n in range(1, 25)})
# VK_NUMPAD0..9 run from 0x60 and VK_F1..F24 from 0x70. Windows defines all
# twenty-four function keys, even though KEY_ALIASES names twelve of them.

VK.update({f"VK_{chr(code)}": code for code in range(ord("A"), ord("Z") + 1)})
VK.update({f"VK_{chr(code)}": code for code in range(ord("0"), ord("9") + 1)})
# Letters and digits are the one run whose virtual-key code *is* its ASCII
# value, which is what lets a single printable character be resolved without a
# second table -- see Win32Backend._virtual_key.

VK.update(
    {
        # The OEM keys: the punctuation positions, each named after the
        # character a US layout prints on it.
        "VK_OEM_1": 0xBA,
        "VK_OEM_PLUS": 0xBB,
        "VK_OEM_COMMA": 0xBC,
        "VK_OEM_MINUS": 0xBD,
        "VK_OEM_PERIOD": 0xBE,
        "VK_OEM_2": 0xBF,
        "VK_OEM_3": 0xC0,
        "VK_OEM_4": 0xDB,
        "VK_OEM_5": 0xDC,
        "VK_OEM_6": 0xDD,
        "VK_OEM_7": 0xDE,
    }
)


_KEYSYM_VK = {
    "backspace": "VK_BACK",
    "tab": "VK_TAB",
    "return": "VK_RETURN",
    "escape": "VK_ESCAPE",
    "space": "VK_SPACE",
    "cancel": "VK_CANCEL",
    "break": "VK_CANCEL",
    "pause": "VK_PAUSE",
    "help": "VK_HELP",
    "caps_lock": "VK_CAPITAL",
    "num_lock": "VK_NUMLOCK",
    "scroll_lock": "VK_SCROLL",
    "print": "VK_SNAPSHOT",
    "insert": "VK_INSERT",
    "delete": "VK_DELETE",
    "home": "VK_HOME",
    "end": "VK_END",
    "prior": "VK_PRIOR",
    "next": "VK_NEXT",
    "left": "VK_LEFT",
    "up": "VK_UP",
    "right": "VK_RIGHT",
    "down": "VK_DOWN",
    "menu": "VK_APPS",
    "shift_l": "VK_LSHIFT",
    "shift_r": "VK_RSHIFT",
    "control_l": "VK_LCONTROL",
    "control_r": "VK_RCONTROL",
    "alt_l": "VK_LMENU",
    "alt_r": "VK_RMENU",
    "meta_l": "VK_LWIN",
    "meta_r": "VK_RWIN",
    "super_l": "VK_LWIN",
    "super_r": "VK_RWIN",
    "iso_level3_shift": "VK_RMENU",
    "kp_enter": "VK_RETURN",
    "kp_add": "VK_ADD",
    "kp_subtract": "VK_SUBTRACT",
    "kp_multiply": "VK_MULTIPLY",
    "kp_divide": "VK_DIVIDE",
    "kp_decimal": "VK_DECIMAL",
    "kp_separator": "VK_SEPARATOR",
}
"""X11 keysym spelling (lower-cased) -> virtual-key name.

Three entries are worth reading twice. `return` and `kp_enter` are both
`VK_RETURN`: Windows gives the main Enter and the keypad's the same virtual
key, and only the scan code distinguishes them, which is why `_EXTENDED_VK`
cannot express the difference either. `print` is Print Screen -- X11's
`Print` keysym is the screen-copy key, and `VK_SNAPSHOT` is what Windows
calls it. `iso_level3_shift` is the keysym most layouts use for AltGr, and on
Windows that is not a modifier of its own but the right Alt key, so a backend
sending it has to send Ctrl alongside to mean what AltGr means -- the same
asymmetry that makes `&` below unlike the other four send_keys modifiers.

`break` and `cancel` both land on `VK_CANCEL`, which is Ctrl+Break: Windows
has one virtual key for the two X11 keysyms.

The punctuation entries are Windows' OEM virtual keys, and they are the one
group here that is a *position* rather than a character: the key a US layout
prints `-` on carries `VK_OEM_MINUS`, so a layout that moves its punctuation
moves that virtual key with it. That is the same layout dependence `uinput`'s
evdev names have, and it gets the same answer -- `type_text`'s Unicode route is
the layout-independent one.
"""

_KEYSYM_VK.update({f"f{n}": f"VK_F{n}" for n in range(1, 25)})
_KEYSYM_VK.update({f"kp_{n}": f"VK_NUMPAD{n}" for n in range(10)})
# Function keys are spelled F1..F24 and a keysym is looked up lower-cased;
# the numpad digits are the other run without letter names of their own.

_KEYSYM_VK.update(
    {
        # `_SENDKEYS_PLAIN`'s names for the punctuation keys, which are the
        # keys `send_keys("-")` and `send_keys(";")` resolve to.
        "minus": "VK_OEM_MINUS",
        "equal": "VK_OEM_PLUS",
        "bracketleft": "VK_OEM_4",
        "bracketright": "VK_OEM_6",
        "backslash": "VK_OEM_5",
        "semicolon": "VK_OEM_1",
        "apostrophe": "VK_OEM_7",
        "comma": "VK_OEM_COMMA",
        "period": "VK_OEM_PERIOD",
        "slash": "VK_OEM_2",
        "grave": "VK_OEM_3",
    }
)
_KEYSYM_VK.update(
    {char: f"VK_{char.upper()}" for char in "abcdefghijklmnopqrstuvwxyz0123456789"}
)
# The keysym names for letters and digits are the characters themselves, and
# their virtual keys are the ASCII values -- so this run is one line rather
# than thirty-six, and it is what makes `press_key("a")` work.


_MODIFIER_KEYS = {
    "^": "VK_LCONTROL",
    "%": "VK_LMENU",
    "+": "VK_LSHIFT",
    "#": "VK_LWIN",
    "&": "VK_RMENU",
}
"""send_keys()'s modifier characters, as virtual keys.

`#` is Meta, which is this package's own addition to the X11 lineage --
`compat.py` records that the original omitted it -- and on Windows Meta is
the Windows key. `&` (AltGr) is the one that does not fit: on Windows AltGr
is a Ctrl-and-Alt combination the layout generates, not a key of its own, so
a backend must either expand an `&(...)` group to both modifiers or refuse it
with a typed error. Sending this key alone would silently do something else,
which is the outcome worth avoiding.
"""

_EXTENDED_VK = frozenset(
    {
        "VK_INSERT",
        "VK_DELETE",
        "VK_HOME",
        "VK_END",
        "VK_PRIOR",
        "VK_NEXT",
        "VK_LEFT",
        "VK_UP",
        "VK_RIGHT",
        "VK_DOWN",
        "VK_RCONTROL",
        "VK_RMENU",
        "VK_NUMLOCK",
        "VK_DIVIDE",
        "VK_APPS",
        "VK_LWIN",
        "VK_RWIN",
    }
)
"""Virtual keys whose scan codes carry the 0xE0 prefix.

The arrows and the navigation cluster are the duplicates on the numeric
keypad, so the prefix is what says which of the two physical keys was meant;
right Ctrl and right Alt are the same story, and the Windows and Menu keys
have been part of the extended set since they were introduced.

Enter is absent, and that is the point rather than an omission: the main
Enter and the keypad's share `VK_RETURN`, and only the scan code tells them
apart -- 0x1C against 0xE0 0x1C. A table keyed by virtual key cannot express
that, which is one reason a backend needing the distinction injects scan
codes instead. Print Screen and Pause are absent for a different reason:
theirs are the two irregular sequences in the whole table (0xE0 2A 0xE0 37
and 0xE1 1D 45), and neither is a single prefix on a scan code.
"""

_SENDKEYS_LONG_NAMES = {
    "BACKSPACE": "BAC",
    "CAPSLOCK": "CAP",
    "DELETE": "DEL",
    "ENTER": "ENT",
    "ESCAPE": "ESC",
    "INSERT": "INS",
    "NUMLOCK": "NUM",
    "PGDN": "PGD",
    "PGUP": "PGU",
    "SCROLLLOCK": "SCR",
}
"""The Windows spellings of aliases this package writes short.

Each long name maps to the short one it is a second name for, so a backend
merges them into `GUIBackend.KEY_ALIASES` without restating a single key:
`{ENTER}` and `{ENT}` then resolve to the same virtual key, and the short
form stays canonical because that is what `RawEvent.keysym` carries and what
the recorder's own examples are written in.

A key here is what a `{...}` group may contain, so nothing here is a single
character, and `~` in particular is not here at all -- the module docstring
says why it stays a tilde.
"""


_CONTROL_VK = {
    "\n": "VK_RETURN",
    "\r": "VK_RETURN",
    "\t": "VK_TAB",
    "\b": "VK_BACK",
    "\x1b": "VK_ESCAPE",
}
"""Control characters `type_text` sends as virtual keys, not as Unicode.

The Unicode route synthesises a keystroke from a character, which is the wrong
shape for the characters that are *commands*: an application expecting a
Return key press gets a text character instead, so a dialog's default button
never fires and a text box never submits. The X11 backend draws the same line
in its own `_CONTROL_KEYSYMS`.
"""

_CURSOR_SHAPES = {
    # X11 cursor-font shape number -> the system cursor with the same meaning.
    # Only the eight that have an unambiguous counterpart: Windows' two
    # diagonal resize cursors do not distinguish the four corners X11's do,
    # so those numbers are refused rather than mapped onto the wrong one.
    68: _winapi.IDC_ARROW,  # XC_left_ptr
    152: _winapi.IDC_IBEAM,  # XC_xterm
    150: _winapi.IDC_WAIT,  # XC_watch
    34: _winapi.IDC_CROSS,  # XC_crosshair
    60: _winapi.IDC_HAND,  # XC_hand2
    52: _winapi.IDC_SIZEALL,  # XC_fleur
    108: _winapi.IDC_SIZEWE,  # XC_sb_h_double_arrow
    116: _winapi.IDC_SIZENS,  # XC_sb_v_double_arrow
}
"""Cursor shapes `is_window_cursor` can answer about, keyed as X11 numbers.

The numbers are the cursor font's, which is what `WINDOW_CURSOR_QUERY`'s
callers pass and what the legacy `IsWindowCursor` took. Windows has no such
numbering -- a cursor's identity there is a handle -- so the mapping is
hand-written and deliberately partial.
"""

_DIAGONAL_SHAPES = {
    134: "top left",
    14: "bottom right",
    136: "top right",
    12: "bottom left",
}
"""The four X11 corner-resize shapes, and which corner each names.

Kept apart from `_CURSOR_SHAPES` rather than added to it: these are the numbers
whose refusal has a *reason* worth giving, and a caller who hits one is asking
a question Windows cannot answer rather than naming something misspelled. See
`is_window_cursor`, which is the only reader.
"""

_BUTTONS = {
    1: (_winapi.MOUSEEVENTF_LEFTDOWN, _winapi.MOUSEEVENTF_LEFTUP, 0),
    2: (_winapi.MOUSEEVENTF_MIDDLEDOWN, _winapi.MOUSEEVENTF_MIDDLEUP, 0),
    3: (_winapi.MOUSEEVENTF_RIGHTDOWN, _winapi.MOUSEEVENTF_RIGHTUP, 0),
    8: (_winapi.MOUSEEVENTF_XDOWN, _winapi.MOUSEEVENTF_XUP, _winapi.XBUTTON1),
    9: (_winapi.MOUSEEVENTF_XDOWN, _winapi.MOUSEEVENTF_XUP, _winapi.XBUTTON2),
}
"""Button number -> (press flag, release flag, `mouseData`).

1/2/3 are left/middle/right, which is the interface's own numbering and X11's
as well. 8 and 9 are the two side buttons -- X11's numbering again, and the
only place Windows needs `mouseData` to say which button a flag pair means.
"""

_MAX_WHEEL_STEPS = 32767 // _winapi.WHEEL_DELTA
"""The most detents one wheel event can carry: 273.

`MOUSEINPUT.mouseData` is 32 bits wide, but the wheel delta it holds is read
back through 16 signed ones by everything that consumes it, so this is the
ceiling that actually applies rather than the field's own. `_wheel_events`
splits anything larger.
"""

_BUTTON_VK = {
    1: _winapi.VK_LBUTTON,
    2: _winapi.VK_MBUTTON,
    3: _winapi.VK_RBUTTON,
    8: _winapi.VK_XBUTTON1,
    9: _winapi.VK_XBUTTON2,
}
"""Button number -> the virtual key `GetAsyncKeyState` reports it under.

The query side of `_BUTTONS`, and keyed the same way so the two cannot drift.
These codes live in `_winapi` rather than in `VK` above because they are not
keys a caller can press: the module already declares them for exactly this
question, and a second copy in `VK` would be a second spelling of one number.
"""

_SIDELESS_VK = {
    VK["VK_LSHIFT"]: _winapi.VK_SHIFT,
    VK["VK_RSHIFT"]: _winapi.VK_SHIFT,
    VK["VK_LCONTROL"]: _winapi.VK_CONTROL,
    VK["VK_RCONTROL"]: _winapi.VK_CONTROL,
    VK["VK_LMENU"]: _winapi.VK_MENU,
    VK["VK_RMENU"]: _winapi.VK_MENU,
}
"""The six sided keys, redirected to the side-agnostic code for a state query.

Keyed by the *value* `VK` resolves a name to, because that is what the query
has in hand and what `GetAsyncKeyState` takes -- a table keyed by name would
never match anything a caller asked for, which is how a "is Shift held"
question quietly answers False while Shift is down.

`GetAsyncKeyState` answers about `VK_SHIFT` for either Shift and about
`VK_LSHIFT` for the left one specifically, and a caller asking whether Shift is
held means either. `press_key` still presses the side it was asked for -- only
the *query* is done this way, which is the one place the difference is worth
papering over.
"""


def _names_by_virtual_key():
    """Virtual-key code -> the key names that press it, in table order.

    Built from `_KEYSYM_VK` rather than written out a second time, so the two
    directions of the translation cannot drift apart. Most codes end up with one
    name; five end up with two, and `key_names_for_virtual_key`'s own docstring
    is where those are explained.
    """
    found: dict[int, list[str]] = {}
    for keysym, name in _KEYSYM_VK.items():
        found.setdefault(VK[name], []).append(keysym)
    return {code: tuple(names) for code, names in found.items()}


_KEY_NAMES_BY_VK = _names_by_virtual_key()
"""Virtual-key code -> every key name that presses it, canonical first.

The first name is the canonical spelling: the one `_KEYSYM_VK` lists first,
which is the one this package writes and the recorder's scripts are written in.
"""


def virtual_key_code(key):
    """The virtual-key code a key name stands for, or raise ValueError.

    Four shapes are accepted, in this order: a control character from
    `_CONTROL_VK` -- the characters that are *commands*, which `type_text` sends
    as key presses rather than as Unicode -- an X11 keysym spelling as the rest
    of this package writes it (`Return`, `F5`, `space`), a `VK_*` name as
    Windows spells it, and a single unshifted printable ASCII character, which
    works because the keysym name for a letter or a digit is the character
    itself.

    A single character that is not a key name of its own is an unknown name
    here. `press_key` resolves those through the same static SendKeys table as
    `send_keys` and refuses the shifted ones, which is why `press_key("-")`
    reaches the minus key while `press_key("!")` is a typed error: this function
    answers about the vocabulary, not about the characters a caller might hope to
    type. Use `send_keys` or `type_text` for characters.

    Public because it is the vocabulary boundary rather than a backend detail: a
    caller that has to speak this platform's names -- a Windows input hook
    turning a captured virtual-key code back into one, say -- needs both
    directions, and reaching into `_KEYSYM_VK` for one of them would make a
    private table part of somebody else's API. `key_name_for_virtual_key` is the
    other direction.
    """
    if key in _CONTROL_VK:
        return VK[_CONTROL_VK[key]]
    name = key.lower()
    if name in _KEYSYM_VK:
        return VK[_KEYSYM_VK[name]]
    upper = key.upper()
    if upper in VK:
        return VK[upper]
    raise ValueError(
        f"unknown key name {key!r}: names are X11 keysym spellings (`Return`, "
        "`F5`, `space`, `minus`) or Windows virtual-key names (`VK_RETURN`), and "
        "a letter or a digit is its own keysym name. press_key additionally "
        "resolves the other printable characters through send_keys' table and "
        "refuses the shifted ones; type_text is the route for typing text"
    )


def key_names_for_virtual_key(code):
    """Every key name that presses the Windows virtual key `code`.

    The other direction of `virtual_key_code`, and a tuple rather than one name
    because the translation is genuinely not one-to-one: Windows gives the main
    Enter and the keypad's the same virtual key, so `VK_RETURN` answers
    `("return", "kp_enter")` and a caller that can see the scan code -- 0x1C
    against 0xE0 0x1C -- can pick between them. The other four pairs are
    `("meta_l", "super_l")`, `("meta_r", "super_r")`, `("cancel", "break")`
    and `("alt_r", "iso_level3_shift")`: one Windows key, two X11 names, each.

    Empty for a code this package cannot name, which is an ordinary answer
    rather than an error -- there are `VK_*` codes with no keysym counterpart,
    and a caller needs to decide what to do about one rather than be handed a
    name that would not replay.

    Public for the same reason `virtual_key_code` is; a caller with no scan code
    to disambiguate with wants `key_name_for_virtual_key` instead.
    """
    return _KEY_NAMES_BY_VK.get(code, ())


def key_name_for_virtual_key(code):
    """The canonical key name for a virtual key, or None where there is none.

    `key_names_for_virtual_key(code)[0]`, for the ordinary case: one Windows key
    whose name a script can say, spelled the way this package writes it
    (`return` rather than `kp_enter`, `meta_l` rather than `super_l`). None where
    that tuple is empty, so a caller can tell "this package has no name for that
    key" from a name it does not recognise.
    """
    names = key_names_for_virtual_key(code)
    return names[0] if names else None


def _absolute_input(x, y, box):
    """Physical desktop pixels -> `SendInput`'s normalized 0-65535 range.

    `box` is the virtual desktop as `(left, top, width, height)`, which is what
    the function is given rather than looked up so that it can be tested
    without a Windows machine -- there is no arithmetic here that depends on
    the API, and every digit of it is a published convention worth pinning.

    Two details are decisions. Rounding is to the nearest step by integer
    arithmetic: truncation would bias every coordinate inward from the
    desktop's lower-right corner, and float division would make the answer
    depend on the platform's floating point for a value that is an integer by
    definition. And the result is clamped, so a coordinate outside the virtual
    desktop is sent as its nearest edge rather than wrapped around to wherever
    the range happens to land.

    The quantization is real and belongs in the contract rather than in a
    comment: a 3840-pixel-wide desktop is squeezed into 65535 steps, and as the
    desktop grows the step grows with it, so the pixel asked for and the pixel
    the pointer lands on can differ. A coordinate exactly on the desktop's
    last pixel is 65535, which is the documented lower-right corner.
    """
    left, top, width, height = box
    return (_normalize(x - left, width), _normalize(y - top, height))


def _normalize(offset, span):
    """One axis: a pixel offset within `span` pixels, as 0-65535.

    A one-pixel axis has no second edge to interpolate toward, so everything
    on it reads as 0; that is a degenerate desktop rather than a case worth
    raising over, since the coordinate is still inside it.
    """
    if span <= 1:
        return 0
    step = (offset * 65535 + (span - 1) // 2) // (span - 1)
    return min(max(step, 0), 65535)


# -- window events ---------------------------------------------------------
#
# `SetWinEventHook`'s out-of-context delivery only happens while the
# installing thread calls `GetMessageW`/`PeekMessageW` (see `_winapi`'s
# `WINEVENT_OUTOFCONTEXT` docstring), so the hook and the message loop that
# keeps it alive are installed together, on a thread that exists only for the
# life of one `window_events()` call -- the same per-call lifecycle
# `GnomeShellBackend.window_events` gives its D-Bus subscription.

_WINDOW_EVENT_RANGES = (
    (_winapi.EVENT_OBJECT_CREATE, _winapi.EVENT_OBJECT_DESTROY),
    (_winapi.EVENT_OBJECT_NAMECHANGE, _winapi.EVENT_OBJECT_NAMECHANGE),
    (_winapi.EVENT_SYSTEM_FOREGROUND, _winapi.EVENT_SYSTEM_FOREGROUND),
)
"""The (min, max) ranges `_run_event_pump` hooks, one `SetWinEventHook` call
each. `EVENT_OBJECT_CREATE`..`_DESTROY` is one contiguous range; the other two
events are not adjacent to it or to each other, so each gets its own hook
sharing the same callback. This is the design document's core event set for
`WINDOW_EVENTS` -- not the "extras" (`EVENT_OBJECT_SHOW`/`_HIDE`,
`EVENT_OBJECT_LOCATIONCHANGE`, `EVENT_SYSTEM_MINIMIZEEND`) it also lists,
which would extend this tuple if a use case asked for them."""


def _relay_window_event(sink):
    """Build the `WINEVENTPROC` that pushes raw `(event, hwnd)` pairs onto `sink`.

    Deliberately free of everything but the filter every event needs: whether
    the event is about a window at all (`OBJID_WINDOW`) rather than one of its
    accessible children (a button inside a dialog is its own `HWND` and fires
    its own events), and about the window itself rather than a piece of it
    (`CHILDID_SELF`). Everything that depends on *which* window this package
    would list -- `_is_listable`, the `known`-handles bookkeeping -- runs
    later, in `window_events()`'s single consumer, deliberately not on the
    hook thread: `sink` is a `queue.Queue`, so the two sides need no lock, and
    the classification logic stays single-threaded and easy to reason about.

    A closure over `sink` rather than a method: nothing here reads backend
    state, and a plain function is what keeps the callback safe to build and
    call with no `Win32Backend` in hand, which is what the tests do.
    """

    def _on_event(_hook, event, hwnd, id_object, id_child, _thread, _time):
        """The `WINEVENTPROC` Windows calls: filter, then relay to `sink`."""
        if (
            hwnd
            and id_object == _winapi.OBJID_WINDOW
            and id_child == _winapi.CHILDID_SELF
        ):
            sink.put((event, hwnd))

    return _winapi.WINEVENTPROC(_on_event)


def _run_event_pump(callback, ready, result):
    """Install the WinEvent hooks and pump this thread's message queue.

    Runs on its own OS thread for as long as one `window_events()` call is
    iterated, because an out-of-context hook is only ever serviced by the
    thread that installed it, and only while that thread calls `GetMessageW`.
    `ready` is set exactly once, the moment installation has succeeded or
    failed -- `window_events()` blocks on it before doing anything else, so it
    never reads `result` too early. `result["thread_id"]` is what lets the
    generator's `finally` end this loop from the outside: posting `WM_QUIT` to
    that specific thread is the documented way to make `GetMessageW` return 0
    for a thread with no window of its own to send it a real message.

    Every hook this call installed is unhooked in the same `finally`,
    regardless of how the loop ends -- including a partial failure, where an
    earlier range's hook must not outlive a later range's failing to install.
    """
    lib = _winapi.user32()
    hooks = []
    try:
        try:
            for low, high in _WINDOW_EVENT_RANGES:
                hook = lib.SetWinEventHook(
                    low,
                    high,
                    None,
                    callback,
                    0,
                    0,
                    _winapi.WINEVENT_OUTOFCONTEXT | _winapi.WINEVENT_SKIPOWNPROCESS,
                )
                if not hook:
                    result["error"] = PyGUITestError(
                        f"SetWinEventHook failed for event range {low:#06x}-{high:#06x}"
                    )
                    return
                hooks.append(hook)
            result["thread_id"] = _winapi.kernel32().GetCurrentThreadId()
            message = _winapi.MSG()
            pointer = ctypes.byref(message)
            # Forces this thread's message queue into existence before
            # `ready` is signalled, so the `PostThreadMessageW(WM_QUIT, ...)`
            # a caller sends the moment they decide to stop can never lose the
            # race against this thread's first real `GetMessageW` -- see
            # `PM_NOREMOVE`'s docstring in `_winapi`.
            lib.PeekMessageW(pointer, None, 0, 0, _winapi.PM_NOREMOVE)
        finally:
            ready.set()
        if result["error"] is not None:
            return
        while True:
            status = lib.GetMessageW(pointer, None, 0, 0)
            if status <= 0:
                # 0 is WM_QUIT, the only message this thread is ever sent;
                # -1 is GetMessageW's own documented error return. Either
                # way, there is nothing left to pump.
                break
            lib.TranslateMessage(pointer)
            lib.DispatchMessageW(pointer)
    finally:
        for hook in hooks:
            lib.UnhookWinEvent(hook)


def available():
    """Whether this backend can be constructed here.

    The factory asks before building anything: False on every host without
    user32, which is every host that is not Windows.
    """
    return _winapi.available()


class Win32Backend(GUIBackend):
    """Window control, input, capture and the clipboard on Windows."""

    name = "win32"

    KEY_ALIASES = {
        **GUIBackend.KEY_ALIASES,
        **{
            long_name: GUIBackend.KEY_ALIASES[short]
            for long_name, short in _SENDKEYS_LONG_NAMES.items()
        },
    }
    """`GUIBackend`'s abbreviations, plus the long Windows spellings.

    `{ENTER}` and `{BACKSPACE}` are second names for `{ENT}` and `{BAC}`
    here, and the short forms stay canonical because that is what a
    `RawEvent.keysym` carries -- see `_SENDKEYS_LONG_NAMES`.

    The long names resolve *through* the short ones rather than to them: a
    value in this table is the key name `press_key` accepts, so `{ENTER}` has
    to end up as `Return` and not as `ENT`. Merging `_SENDKEYS_LONG_NAMES` in
    directly would make every long spelling an unknown key name at the moment
    of pressing it, which is the failure this comprehension exists to avoid.
    """

    MODIFIER_KEYS = _MODIFIER_KEYS
    """send_keys()' modifiers as virtual keys -- `#` is the Windows key, and
    `&` is the right Alt, which is half of what AltGr means here."""

    def __init__(self, environment=None):
        """Confirm the API is present, and set this thread's DPI context.

        The DPI side is not optional and is not global: `SendInput` takes
        physical pixels while an unaware thread is shown a virtualised desktop
        and gets scaled numbers back from every window query, so a thread that
        does not set its own context contradicts itself. Per-thread rather
        than process-wide, because a process-wide change cannot be undone and
        would re-lay-out the host application's own windows -- see
        docs/developers/adr-003-windows.md.
        """
        if not available():
            raise BackendUnavailable(
                "the Windows API is not available here (user32.dll did not "
                "load); this backend drives a native Windows session"
            )
        self.environment = environment
        self._set_dpi_awareness()

    @property
    def capabilities(self):
        """Everything but elements and un-occluded capture.

        The tier-6 block is the surprising part and it is deliberate: four
        capabilities Wayland refuses outright are ordinary calls here, so a
        Windows `report()` shows `[yes]` under a heading whose own description
        says "deliberately prevented". The tier is a Wayland ceiling, which is
        what `Capabilities` documents it as.
        """
        return CapabilitySet(
            {
                Capability.SCREEN_INFO,
                Capability.SCREEN_CAPTURE,
                Capability.POINTER_MOVE,
                Capability.POINTER_BUTTON,
                Capability.POINTER_SCROLL,
                Capability.KEY_EVENT,
                Capability.TEXT_ENTRY,
                Capability.POINTER_QUERY,
                Capability.INPUT_STATE_QUERY,
                Capability.WINDOW_LIST,
                Capability.WINDOW_EVENTS,
                Capability.WINDOW_STATE,
                Capability.WINDOW_ACTIVATE,
                Capability.WINDOW_GEOMETRY,
                Capability.WINDOW_PLACEMENT,
                Capability.WINDOW_RESIZE,
                Capability.WINDOW_MINIMIZE,
                Capability.WINDOW_PID,
                Capability.WINDOW_AT_POINT,
                Capability.WINDOW_TITLE_SET,
                Capability.WINDOW_LOWER,
                Capability.WINDOW_CURSOR_QUERY,
                Capability.CLIPBOARD,
            }
        )

    # -- helpers -----------------------------------------------------------

    def _lib(self):
        """user32, or raise naming what is missing.

        Not cached on the instance: `_winapi` caches the load itself, and a
        stale attribute here would outlive the library it points at.
        `__init__` has already refused to build a backend without the API, so
        this is what keeps a hand-built instance honest rather than a
        condition expected to fire.
        """
        lib = _winapi.user32()
        if lib is None:
            raise BackendUnavailable(
                "user32.dll is not available; this backend drives a native "
                "Windows session"
            )
        return lib

    def _set_dpi_awareness(self):
        """Make this thread's coordinates physical, per-monitor-v2 aware.

        `_winapi.set_thread_dpi_awareness` holds the call and the reasoning,
        because it is not only this backend's: `uia` sets the same context at
        the same point in its own construction, and a session that composes
        the two has to agree with itself about what a coordinate means.
        """
        _winapi.set_thread_dpi_awareness()

    def _own(self, window):
        """`window`, if this backend issued it, or raise.

        A `Window` from another backend carries a handle this one cannot act on
        -- an X11 window id is a perfectly plausible integer to hand to
        `SetWindowPos` -- so every window-taking method starts here rather than
        discovering it several calls down as a failure about a handle nobody
        recognises.
        """
        if not isinstance(window, Window) or window.backend is not self:
            raise WindowNotFound(
                f"{window!r} was not issued by the {self.name} backend"
            )
        return window

    def _hwnd(self, window):
        """`window`'s handle, or raise if it is not a live one of ours.

        `_own` plus `IsWindow`, and the second half matters as much as the
        first: Windows recycles handle values, so a `Window` that is simply old
        can name a *different* window by the time it is used, and `IsWindow` is
        the only way to ask.
        """
        handle = self._own(window).handle
        if not self._lib().IsWindow(handle):
            raise WindowNotFound(f"{window!r} has closed")
        return handle

    def _title(self, handle):
        """A window's title, or "" where it has none."""
        lib = self._lib()
        length = lib.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        lib.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _class_name(self, handle):
        """A window's class name, which is what `Window.app_id` carries here.

        256 characters is the platform's own limit on a class name, so the
        buffer cannot truncate one.
        """
        buffer = ctypes.create_unicode_buffer(256)
        if self._lib().GetClassNameW(handle, buffer, 256) <= 0:
            return ""
        return buffer.value

    def _window(self, handle):
        """The `Window` describing one handle.

        `pid` is `GetWindowThreadProcessId`'s answer, which is the process that
        *owns* the window -- and for a Store application that is the host,
        `ApplicationFrameHost.exe`, rather than the application itself. See
        `windows()` and `WINDOW_PID`'s own note.
        """
        pid = _winapi.DWORD()
        self._lib().GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return Window(
            handle=handle,
            backend=self,
            title=self._title(handle),
            app_id=self._class_name(handle),
            pid=pid.value or None,
        )

    def _rect(self, handle):
        """A window's rectangle, or raise if Windows will not give it."""
        rect = _winapi.RECT()
        if not self._lib().GetWindowRect(handle, ctypes.byref(rect)):
            raise PyGUITestError(f"GetWindowRect failed for window handle {handle!r}")
        return rect

    def _contains(self, handle, x, y):
        """Whether a screen coordinate falls inside a window's rectangle.

        Right and bottom are exclusive, which is how `RECT` means them, so a
        pointer exactly on the boundary pixel belongs to the window to the
        right or below rather than to this one.
        """
        rect = self._rect(handle)
        return rect.left <= x < rect.right and rect.top <= y < rect.bottom

    # -- screens (T2) ------------------------------------------------------

    def screens(self):
        """Every monitor, primary first.

        Index 0 is the primary, which `MONITORINFOF_PRIMARY` names and Wayland
        has no way to express: `move_mouse(x, y, screen=0)` and the composite's
        whole-desktop capture both default there, and "the primary" is what a
        caller means by it. The rest follow in virtual-desktop reading order --
        left to right, then top to bottom -- so a two-monitor desktop does not
        renumber itself between calls.

        Coordinates are the monitor's `rcMonitor`: the whole screen, in the one
        physical coordinate space every monitor shares. `rcWork` (the same
        rectangle minus the taskbar) is what `WINDOW_PLACEMENT` is adjusted
        against and is deliberately not reported here.
        """
        lib = self._lib()
        found = []

        def collect(hmonitor, _hdc, _rect, _param):
            """`EnumDisplayMonitors`' callback: append one monitor's info."""
            info = _winapi.MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_winapi.MONITORINFOEXW)
            if lib.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                found.append((info, hmonitor))
            return 1

        callback = _winapi.MONITORENUMPROC(collect)
        # An enumeration that failed and one that found nothing are the same
        # empty list, and they mean opposite things: a session always has a
        # monitor, so "no screens" would send a caller looking at their display
        # configuration for a fault that is in the call.
        if not lib.EnumDisplayMonitors(None, None, callback, 0):
            raise PyGUITestError(
                "EnumDisplayMonitors failed, so this session's monitors could "
                "not be listed"
            )
        ordered = sorted(
            found,
            key=lambda pair: (
                not pair[0].dwFlags & _winapi.MONITORINFOF_PRIMARY,
                pair[0].rcMonitor.left,
                pair[0].rcMonitor.top,
            ),
        )
        return [
            Screen(
                index=index,
                width=info.rcMonitor.right - info.rcMonitor.left,
                height=info.rcMonitor.bottom - info.rcMonitor.top,
                scale=self._scale(hmonitor),
                name=_winapi.wide_string(info.szDevice),
            )
            for index, (info, hmonitor) in enumerate(ordered)
        ]

    def _scale(self, hmonitor):
        """A monitor's DPI over 96 -- the number `Screen.scale` documents.

        `GetDpiForMonitor` from `shcore`, which arrived in Windows 8.1: a
        machine older than that, or one where the call fails, reports 1.0
        rather than a guess, and 1.0 is also the correct answer for every
        monitor left at 100%. `MDT_EFFECTIVE_DPI` is asked for rather than the
        raw DPI, because the effective one is what the user chose to see.

        This is the *reported* scale, not a conversion: with the thread's DPI
        context set in `__init__`, `Screen.width`, `geometry()` and a capture
        are all in the same physical pixels, so nothing here multiplies or
        divides anything.
        """
        lib = _winapi.shcore()
        if lib is None:
            return 1.0
        dpi_x = _winapi.DWORD()
        dpi_y = _winapi.DWORD()
        result = lib.GetDpiForMonitor(
            hmonitor,
            _winapi.MDT_EFFECTIVE_DPI,
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        )
        if result != 0 or not dpi_x.value:
            return 1.0
        return dpi_x.value / 96.0

    def _virtual_screen(self):
        """The virtual desktop as `(left, top, width, height)`, in pixels.

        Every monitor's union, whose origin is negative on a machine with a
        monitor to the left of or above the primary. Both of its consumers need
        exactly this rectangle: `SendInput` maps its 0-65535 range onto it, and
        a whole-desktop `capture()` blits from its top-left corner.
        """
        lib = self._lib()
        return (
            lib.GetSystemMetrics(_winapi.SM_XVIRTUALSCREEN),
            lib.GetSystemMetrics(_winapi.SM_YVIRTUALSCREEN),
            lib.GetSystemMetrics(_winapi.SM_CXVIRTUALSCREEN),
            lib.GetSystemMetrics(_winapi.SM_CYVIRTUALSCREEN),
        )

    # -- input (T4) --------------------------------------------------------
    #
    # One `SendInput` call per operation, built from INPUT structures. The
    # builders are separate from the sends so a test can assert the message
    # layout -- flags, union member, units -- with no DLL in sight.

    def _send(self, events, capability):
        """Deliver `events` in one `SendInput` call, or raise naming the state.

        One call rather than one per event, and that is not an optimisation:
        the documentation says the events of a single call are inserted
        serially and are *not* interspersed with anyone else's input, so a
        chord or a double-click sent this way cannot be broken up by a user
        moving the pointer halfway through. Strictly better than the X11 and
        libei situation, and the reason `Session.double_click` and `drag` want
        it.

        The failure this cannot diagnose is the one that matters: a call
        blocked by UIPI returns the *full* count and delivers nothing, and
        neither `GetLastError` nor the return value says so. Zero is the other
        documented answer -- "the input was already blocked by another thread"
        -- and both are reported as one typed refusal, because from here they
        are indistinguishable and both mean "not delivered".
        """
        lib = self._lib()
        array = (_winapi.INPUT * len(events))(*events)
        sent = lib.SendInput(len(events), array, ctypes.sizeof(_winapi.INPUT))
        if sent == len(events):
            return
        raise PermissionRequired(
            capability,
            self.name,
            f"SendInput queued {sent} of {len(events)} events, and Windows does "
            "not report why: an integrity-level (UIPI) block looks exactly like "
            "this -- success, and nothing delivered -- and so does input another "
            "thread is holding. Run the test at the same privilege level as the "
            "application under test, and as the same user",
        )

    def _mouse_event(self, flags, dx=0, dy=0, data=0):
        """One `MOUSEINPUT`, as the `INPUT` `SendInput` takes."""
        event = _winapi.INPUT()
        event.type = _winapi.INPUT_MOUSE
        event.mi.dx = dx
        event.mi.dy = dy
        event.mi.mouseData = data
        event.mi.dwFlags = flags
        return event

    def _key_event(self, vk, up=False):
        """One keyboard `INPUT` for virtual key `vk`.

        No `KEYEVENTF_SCANCODE` and no `KEYEVENTF_EXTENDEDKEY`: with a virtual
        key Windows resolves the scan code itself, extended set included, which
        is why `press_key` sends virtual keys rather than scan codes. The module
        docstring has the three vocabularies and what each of them costs.
        """
        event = _winapi.INPUT()
        event.type = _winapi.INPUT_KEYBOARD
        event.ki.wVk = vk
        event.ki.dwFlags = _winapi.KEYEVENTF_KEYUP if up else 0
        return event

    def _unicode_events(self, text):
        """`text` as `KEYEVENTF_UNICODE` press-and-release pairs.

        UTF-16 *code units*, so a character outside the BMP arrives as the
        surrogate pair Windows expects -- and both halves go in one call,
        because a surrogate split across two calls is two broken characters.
        The unit rides in `wScan` with `wVk` left at zero, which is the
        documented shape: the system synthesises the keystroke from the
        character, so the active layout is never consulted.
        """
        units = text.encode("utf-16-le")
        events: list[_winapi.INPUT] = []
        for index in range(0, len(units), 2):
            unit = units[index] | (units[index + 1] << 8)
            down = self._key_event(0)
            down.ki.wScan = unit
            down.ki.dwFlags = _winapi.KEYEVENTF_UNICODE
            up = self._key_event(0, up=True)
            up.ki.wScan = unit
            up.ki.dwFlags = _winapi.KEYEVENTF_UNICODE | _winapi.KEYEVENTF_KEYUP
            events.extend((down, up))
        return events

    def _wheel_data(self, steps):
        """`steps` detents as `mouseData` counts them.

        One detent is `WHEEL_DELTA`, and a negative value is the other
        direction. The field is unsigned, so the two's-complement wrap is
        written out here rather than left to ctypes' own conversion: it is part
        of the message's layout and worth being able to see.

        `steps` must already be within `_MAX_WHEEL_STEPS`; `_wheel_events` is
        what guarantees that, and its docstring says what happens otherwise.
        """
        return (steps * _winapi.WHEEL_DELTA) & 0xFFFFFFFF

    def _wheel_events(self, flag, steps):
        """`steps` detents on one axis, split across as many events as it takes.

        `mouseData` is a 32-bit field, but the wheel delta inside it is not:
        every consumer reads it back through 16 bits -- `WM_MOUSEWHEEL` carries
        it in the signed high word of `wParam`, which is what
        `GET_WHEEL_DELTA_WPARAM` casts to a `short`, and raw input carries it in
        `RAWMOUSE.usButtonData`. So a single event cannot say more than
        `_MAX_WHEEL_STEPS` detents, and one that tries does not merely saturate:
        `scroll(dy=300)` is 36000, which reads back as -29536 and scrolls the
        *other way*. Splitting is the only way to deliver a large scroll, and
        the pieces still go in one `SendInput` call, so nobody else's input can
        land in the middle of one.
        """
        events = []
        remaining = steps
        while remaining:
            chunk = max(-_MAX_WHEEL_STEPS, min(_MAX_WHEEL_STEPS, remaining))
            events.append(self._mouse_event(flag, data=self._wheel_data(chunk)))
            remaining -= chunk
        return events

    def _button(self, button):
        """`_BUTTONS`' entry for `button`, or raise naming what exists."""
        try:
            return _BUTTONS[button]
        except KeyError:
            raise ValueError(
                f"unknown mouse button {button!r} on {self.name}; known buttons: "
                f"{', '.join(str(number) for number in sorted(_BUTTONS))}"
            ) from None

    def _virtual_key(self, key):
        """The virtual key for a key name, or raise naming the miss.

        The vocabulary itself is `virtual_key_code`, which is public and answers
        about names alone. What this adds is the character route: a single ASCII
        character that is not a keysym name of its own is resolved through the
        same static US-layout table `send_keys` uses, so `press_key(" ")`
        reaches the space bar and `press_key("-")` the minus key -- without it,
        `press_key("a")` would be an unknown name on the one platform where
        every other backend accepts a plain letter.

        A *shifted* character is refused rather than resolved to the key it sits
        on: pressing `1` because the caller wrote `!` types the wrong character,
        and the package's answer to characters is send_keys and type_text. That
        matches the X11 backend, whose keysym lookup has no key for `!` either.
        Both refusals name this backend, because a caller who asked it for a key
        is owed the name of the backend that answered.
        """
        try:
            return virtual_key_code(key)
        except ValueError as exc:
            if not isinstance(key, str) or len(key) != 1 or not key.isascii():
                raise ValueError(
                    f"unknown key name {key!r} on {self.name}; names are X11 "
                    "keysyms (`Return`, `F5`, `space`), Windows virtual keys "
                    "(`VK_RETURN`), or one unshifted printable ASCII character"
                ) from exc
        char, needs_shift = self.resolve_char_key(key)
        if needs_shift:
            raise ValueError(
                f"{key!r} is a shifted key on {self.name}, so pressing it needs "
                "Shift held; use send_keys(...) or type_text(...) for "
                "characters, or name the key it sits on"
            )
        return virtual_key_code(char)

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position in physical pixels.

        `screen` selects nothing here and is not consulted: the virtual desktop
        is one coordinate space, so a coordinate on a second monitor is simply a
        larger `x` -- the same reason `X11Backend` ignores it. What matters is
        that the event is `ABSOLUTE` *and* `VIRTUALDESK`: without the second
        flag the normalized range covers the primary monitor only, so every
        coordinate on any other monitor lands on the primary instead.

        Two consequences of that normalization a caller should know. The pixel
        asked for and the pixel landed on can differ, because the whole desktop
        is squeezed into 65535 steps -- `_absolute_input` is where the rounding
        rule lives. And the position is the *screen* coordinate of a pointer,
        never a window-relative one; `Session` builds the second kind from
        `geometry()`, which is why the two agree.
        """
        self.require(Capability.POINTER_MOVE)
        dx, dy = _absolute_input(x, y, self._virtual_screen())
        self._send(
            [
                self._mouse_event(
                    _winapi.MOUSEEVENTF_MOVE
                    | _winapi.MOUSEEVENTF_ABSOLUTE
                    | _winapi.MOUSEEVENTF_VIRTUALDESK,
                    dx,
                    dy,
                )
            ],
            Capability.POINTER_MOVE,
        )

    def press_button(self, button):
        """Press a mouse button. 1 is left, 2 middle, 3 right, 8 and 9 the sides.

        `mouseData` comes from `_BUTTONS` and not from the caller, because the
        side buttons share one flag pair and the field is the only thing that
        says which of the two was meant.
        """
        self.require(Capability.POINTER_BUTTON)
        down, _up, data = self._button(button)
        self._send([self._mouse_event(down, data=data)], Capability.POINTER_BUTTON)

    def release_button(self, button):
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        _down, up, data = self._button(button)
        self._send([self._mouse_event(up, data=data)], Capability.POINTER_BUTTON)

    def scroll(self, dx=0, dy=0):
        """Scroll by whole wheel detents: `dy` positive is up, `dx` right.

        Windows counts wheel units in 120ths of a detent and takes a positive
        value as "away from the user", which is up -- so the base class's sign
        convention needs no negation here, unlike the portal and libei
        backends. Each axis is its own event with its own flag, and both go in
        one call, so a diagonal scroll cannot be split in half.

        An axis asking for more than `_MAX_WHEEL_STEPS` detents becomes several
        events rather than one -- see `_wheel_events` for why a single event
        cannot carry them, and what a scroll that ignored the limit would do.
        """
        self.require(Capability.POINTER_SCROLL)
        events = []
        if dy:
            events.extend(self._wheel_events(_winapi.MOUSEEVENTF_WHEEL, dy))
        if dx:
            events.extend(self._wheel_events(_winapi.MOUSEEVENTF_HWHEEL, dx))
        if events:
            self._send(events, Capability.POINTER_SCROLL)

    def press_key(self, key):
        """Press a key by name, without releasing it."""
        self.require(Capability.KEY_EVENT)
        self._send([self._key_event(self._virtual_key(key))], Capability.KEY_EVENT)

    def release_key(self, key):
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        self._send(
            [self._key_event(self._virtual_key(key), up=True)], Capability.KEY_EVENT
        )

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        r"""Type `text`, pausing `delay` seconds between characters.

        The Unicode route, and the reason `allow_keymap_unsafe` changes nothing
        here: every character goes as a `KEYEVENTF_UNICODE` event whose code
        unit the system turns into a keystroke, so no scancode is injected and
        there is no keymap to be unsafe about. The flag stays in the signature
        and is accepted and ignored, as the base class documents, because a
        caller should not have to know which backend is active to pass it.

        Control characters are the exception `_CONTROL_VK` exists for: `\\n`,
        `\\r`, `\\t`, `\\b` and `\\x1b` are *commands* rather than text, and a
        Unicode event for one hands the application a character where a key
        press was meant -- a dialog's default button never fires and a text box
        never submits. Those go as virtual keys, press and release in one call
        each. The X11 backend draws the same line in its own `_CONTROL_KEYSYMS`.
        """
        self.require(Capability.TEXT_ENTRY)
        for index, char in enumerate(text):
            if index and delay:
                time.sleep(delay)
            if char in _CONTROL_VK:
                vk = VK[_CONTROL_VK[char]]
                self._send(
                    [self._key_event(vk), self._key_event(vk, up=True)],
                    Capability.TEXT_ENTRY,
                )
            else:
                self._send(self._unicode_events(char), Capability.TEXT_ENTRY)

    def sync(self, timeout=1.0):
        """Refuse, and say why rather than only that it is unsupported.

        `SendInput` reports how many events it *queued*, and nothing about what
        consumed them -- that is the UIPI hole `_send` describes -- so there is
        no round trip to block on and any implementation of this would be a
        sleep wearing the name of a guarantee. The reason is spelled out here
        rather than left to the generic "unsupported" message because it is the
        one piece of Windows input behaviour a caller may reasonably doubt.
        """
        self.require(
            Capability.INPUT_SYNC,
            "SendInput knows how many events it queued and nothing about what "
            "consumed them -- a UIPI-blocked call returns the full count and "
            "delivers nothing -- so there is no round trip to confirm; see "
            "docs/developers/adr-003-windows.md",
        )

    # -- windows (T3) ------------------------------------------------------

    def windows(self):
        """Every toplevel a caller means, in z-order, **bottommost first**.

        `EnumWindows` yields them in stacking order from the *top* down, and
        the list is reversed on the way out, because bottom-to-top is this
        package's cross-platform invariant rather than this backend's
        preference: `Session.find_window` and `Session.wait_for_window` both
        take `found[-1]` as "the topmost match", `X11Backend.windows` gets
        that order from `_NET_CLIENT_LIST_STACKING`, and the compositor-IPC
        backends' `window_at` documents it as "the topmost is last".
        Returning `EnumWindows`' own order here would leave every one of those
        callers picking the window at the *bottom* of the stack -- which is
        exactly the bug fixed for X11 in the window-title-collision work, and
        is invisible until two windows share a title.

        It yields considerably more than a caller means, too: `_is_listable`
        is the filter, and it drops invisible windows, `WS_EX_TOOLWINDOW`
        palettes and DWM-cloaked windows. Owned windows are kept -- see
        `_is_listable` for why that deliberately differs from what Alt-Tab
        shows. Minimized windows are kept deliberately, too -- `WINDOW_STATE`
        exists to report them.

        `app_id` is the window *class* name (`Notepad`, `Chrome_WidgetWin_1`),
        which is Windows' nearest thing to the application identity Wayland
        calls an app id: one string per window, stable for its life, and what
        `find_windows(app_id=...)` matches on. It is a window class rather than
        an application, and the two coincide only sometimes.

        `pid` is `GetWindowThreadProcessId`'s answer and carries the UWP caveat
        `WINDOW_PID` documents: a Store application's toplevel belongs to
        `ApplicationFrameHost.exe`, so this is the host's pid rather than the
        application's. Anything stronger needs the element tree, where UI
        Automation reports the real process.
        """
        handles = []

        def collect(hwnd, _param):
            """`EnumWindows`' callback: keep `hwnd` if it belongs in the list."""
            if self._is_listable(hwnd):
                handles.append(hwnd)
            return 1

        callback = _winapi.WNDENUMPROC(collect)
        self._lib().EnumWindows(callback, 0)
        # Reversed, not sorted: EnumWindows' order *is* the stacking order,
        # top down, and this package's is bottom up. See the docstring.
        return [self._window(handle) for handle in reversed(handles)]

    def _is_listable(self, hwnd):
        """Whether `hwnd` is a toplevel this backend should report.

        The same shape of filter the Wayland backends apply to
        `_NET_WM_WINDOW_TYPE`, and every part of it is from the Windows
        analysis: a tool window is a palette or a floating toolbar and never
        belongs in a task list, and a cloaked window is a suspended Store
        application's -- enumerable and style-visible while being nowhere on
        screen, which is what produces a `wait_for_window` that matches a
        ghost.

        Owned windows are deliberately **kept**, even though Alt-Tab hides
        them. Alt-Tab hides an owned window because the window travels with
        its owner -- close the owner and it goes too -- but that is a claim
        about what a user switching tasks wants to see, not about what is on
        screen. A dialog created with an owner (`CreateWindowExW`'s
        `hWndParent`, which is what nearly every Find/Replace, About and
        confirmation dialog is) is visible, clickable, and exactly what a test
        means by "the window I am waiting for". Filtering it here made those
        dialogs structurally unreachable: `find_windows` never reported them,
        so `wait_for_window("Slow Dialog")` returned None forever and not even
        a `.*` search contained them, while `FindWindowW` answered with the
        handle immediately. Found live, driving a deliberately slow Windows
        probe window whose dialog opens a beat after the click that asked for
        it.

        `WS_EX_APPWINDOW` still overrides `WS_EX_TOOLWINDOW`: it means "put
        this on the taskbar", which is a window asking to be listed after all,
        and dropping it here would make a window the user can see and click
        untouchable by any test.
        """
        lib = self._lib()
        if not lib.IsWindowVisible(hwnd):
            return False
        style = _winapi.get_window_ex_style(hwnd)
        if not style & _winapi.WS_EX_APPWINDOW and style & _winapi.WS_EX_TOOLWINDOW:
            return False
        return not self._is_cloaked(hwnd)

    def _is_cloaked(self, hwnd):
        """Whether DWM is hiding `hwnd` -- a suspended Store application.

        A missing `dwmapi`, or a failing call, reads as "not cloaked": the
        attribute arrived with the cloaking behaviour itself, so a machine
        without the library is one where the condition does not exist.
        """
        lib = _winapi.dwmapi()
        if lib is None:
            return False
        cloaked = _winapi.DWORD()
        result = lib.DwmGetWindowAttribute(
            hwnd, _winapi.DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return result == 0 and bool(cloaked.value)

    def active_window(self):
        """The window holding the foreground, or None.

        Filtered by `_is_listable` like a listed window, so a `Window` from here
        is always one `windows()` would also report. Two answers become None
        that way: nothing having the foreground at all (`GetForegroundWindow`
        returns 0), and the foreground being something this backend does not
        report -- a tool window, or a Store application's window mid-suspend. An
        honest "no window I can name" beats a handle nothing else here will act
        on.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._lib().GetForegroundWindow()
        if not handle or not self._is_listable(handle):
            return None
        return self._window(handle)

    def is_window_viewable(self, window):
        """Whether `window` is mapped and showing.

        `IsWindowVisible`, which means "this window and every ancestor carries
        `WS_VISIBLE`". A minimized window still answers True: it is on the
        desktop and its rectangle is still its rectangle, which is exactly what
        `geometry()` reports. Minimizedness is `WINDOW_STATE`'s separate
        question, and this is not the call that answers it.

        A window that has closed answers False rather than raising, because "not
        showing" is a true answer about the desktop; a `Window` this backend
        never issued still raises, because that is a mistake in the caller.
        """
        self.require(Capability.WINDOW_STATE)
        lib = self._lib()
        handle = self._own(window).handle
        if not lib.IsWindow(handle):
            return False
        return bool(lib.IsWindowVisible(handle))

    def window_at(self, x, y, screen=0):
        """The topmost window covering a point, or None.

        `WindowFromPoint` answers about the deepest window at the coordinate,
        child controls included, so `GetAncestor(GA_ROOT)` walks up to the
        toplevel -- which is what a caller asking "what window is there" means,
        and the same reduction `windows()` performs. A point over no window, or
        over something `_is_listable` would not report, is None.

        `screen` selects nothing, for `move_mouse`'s reason: every monitor
        shares one coordinate space, so `(x, y)` is already unambiguous.
        """
        self.require(Capability.WINDOW_AT_POINT)
        lib = self._lib()
        handle = lib.WindowFromPoint(_winapi.POINT(x, y))
        if not handle:
            return None
        root = lib.GetAncestor(handle, _winapi.GA_ROOT)
        if not root or not self._is_listable(root):
            return None
        return self._window(root)

    def geometry(self, window):
        """`(x, y, width, height)` of `window`'s frame, in physical pixels.

        `GetWindowRect`, which includes the invisible resize border DWM keeps
        around a window -- a few pixels wider than what is visibly drawn, and
        the same rectangle `SetWindowPos` moves, so `move_window` and this agree
        by construction. A maximized window reports the rectangle it currently
        covers rather than the one it would restore to, and a minimized one
        reports the off-screen rectangle Windows parks it at, so coordinates
        read from a minimized window are not meaningful positions.
        """
        self.require(Capability.WINDOW_GEOMETRY)
        rect = self._rect(self._hwnd(window))
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def move_window(self, window, x, y):
        """Move `window`'s top-left corner to (x, y), without resizing it.

        `SWP_NOSIZE`, `SWP_NOZORDER` and `SWP_NOACTIVATE`: a move must not
        resize, restack or steal focus -- the rule the Wayland backends follow
        too, and the opposite of what `activate_window` is for.

        Two honest limits. A maximized window is the documented exception: the
        platform moves the rectangle it would *restore* to and leaves the
        visible frame where it is, so a caller who needs a maximized window
        somewhere else restores it first. And a target under the taskbar is
        honoured by the API and then adjusted by the system, so an assertion
        after a move is an assertion about `GetWindowRect` rather than about the
        numbers passed in.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        self._set_window_pos(window, x, y, _winapi.SWP_NOSIZE)

    def resize_window(self, window, width, height):
        """Resize `window` to `width` by `height`, without moving it.

        `SWP_NOMOVE`, `SWP_NOZORDER` and `SWP_NOACTIVATE`, for the reasons
        `move_window` gives. The same two platform adjustments apply -- a
        maximized window resizes in its restored form, and a window with a
        minimum or maximum size set by its application comes back as something
        else, which is why the caller should read `geometry()` back rather than
        assume.
        """
        self.require(Capability.WINDOW_RESIZE)
        self._set_window_pos(window, 0, 0, _winapi.SWP_NOMOVE, width, height)

    def _set_window_pos(self, window, x, y, flags, width=0, height=0):
        """`SetWindowPos` with NOZORDER and NOACTIVATE always set.

        Both are on every call this backend makes, which is why they live here
        rather than at each call site: moving or resizing a window must not
        restack it or pull focus into it by accident, and the one operation that
        *should* change the z-order (`lower_window`) sets NOACTIVATE too. The
        only call that changes focus is `activate_window`, and it uses
        `SetForegroundWindow` rather than this.
        """
        result = self._lib().SetWindowPos(
            self._hwnd(window),
            None,
            x,
            y,
            width,
            height,
            flags | _winapi.SWP_NOZORDER | _winapi.SWP_NOACTIVATE,
        )
        if not result:
            raise PyGUITestError(f"SetWindowPos failed for {window!r}")

    def activate_window(self, window):
        """Raise and focus `window`, verifying that it actually took.

        `SetForegroundWindow` succeeds only when the calling process is entitled
        to the foreground -- it is already the foreground process, or it received
        the last input event, or one of a short list of documented exceptions
        applies -- and it is documented to return nonzero while the window does
        *not* become foreground. A backend that trusted that return value would
        report success, leave the raised window behind, and send every following
        keystroke to whatever was in front -- the kind of failure that surfaces
        as a mysterious assertion three steps later. So the answer is read back
        from `GetForegroundWindow`, and a mismatch raises.

        What a caller can do about it is documented rather than argued with
        here: the usual causes are another process holding the foreground (a
        person typing), a window that may not be activated (`WS_EX_NOACTIVATE`,
        or a `WS_EX_TOOLWINDOW` this backend would not list anyway), and the
        foreground lock timeout.

        A minimized window is the one cause worth telling apart, because
        `SetForegroundWindow` does not restore one and the failure then has
        nothing to do with who holds the foreground. Activating does not
        restore it either -- `X11Backend.activate_window` does not, and one
        backend quietly doing more than the interface says is worse than a
        caller writing the `minimize_window(window, False)` they meant -- so
        `IsIconic` is asked only to name the cause in the error.

        **One retry, after a no-op mouse move.** Measured on Windows 11: with
        another process in front (a window the user clicked, an application
        that launched itself) and no input of this process's own in the last
        few seconds, `SetForegroundWindow` is refused -- so activating a
        background window, the ordinary thing a two-window test does, raised
        every time. A `SendInput` mouse move of zero distance is what fixes it:
        the system counts it as this process's input, the entitlement the call
        is documented to need, and the retry then takes. It moves nothing and
        types nothing, which is why it is preferred to a synthetic Alt tap --
        that also works, and opens the foreground window's menu bar.
        `AttachThreadInput` to the foreground thread was tried and did not
        help. The result is read back exactly as before, so a window that
        still does not take the foreground raises with the same message, and a
        minimized one is not retried, since input would not change its answer.
        """
        self.require(Capability.WINDOW_ACTIVATE)
        lib = self._lib()
        handle = self._hwnd(window)
        lib.SetForegroundWindow(handle)
        if lib.GetForegroundWindow() != handle and not lib.IsIconic(handle):
            self._claim_last_input(lib)
            lib.SetForegroundWindow(handle)
        if lib.GetForegroundWindow() != handle:
            if lib.IsIconic(handle):
                raise PyGUITestError(
                    f"{window!r} did not become the foreground window because it "
                    "is minimized, and Windows does not restore a window to give "
                    "it the foreground; restore it first with "
                    "minimize_window(window, False)"
                )
            raise PyGUITestError(
                f"{window!r} did not become the foreground window; Windows grants "
                "the foreground only to a process that already has it or has the "
                "last input event, so something else on the desktop is holding it"
            )

    def _claim_last_input(self, lib):
        """Send a mouse move of zero distance, so this process has just had input.

        Best effort, and deliberately not through `_send`: a refusal here (a
        UIPI block, another thread holding input) only means the retry
        `activate_window` makes next will fail the way the first did, and that
        call is what reads the answer back and raises. A relative move by
        nothing does not disturb the pointer, and is not a click or a key, so
        it cannot land in the window that has the foreground.
        """
        event = self._mouse_event(_winapi.MOUSEEVENTF_MOVE)
        array = (_winapi.INPUT * 1)(event)
        lib.SendInput(1, array, ctypes.sizeof(_winapi.INPUT))

    def minimize_window(self, window, minimized=True):
        """Minimize `window`, or restore it when `minimized` is False.

        `ShowWindow`'s return value is ignored, because it answers "was the
        window previously hidden" rather than "did this work" -- so it cannot
        answer the question asked. `IsIconic` is asked instead and a mismatch
        raises, for the reason `activate_window` reads its result back: the
        platform quietly declining an operation must not be reported as success.
        """
        self.require(Capability.WINDOW_MINIMIZE)
        lib = self._lib()
        handle = self._hwnd(window)
        lib.ShowWindow(handle, _winapi.SW_MINIMIZE if minimized else _winapi.SW_RESTORE)
        if bool(lib.IsIconic(handle)) != minimized:
            wanted = "minimize" if minimized else "restore"
            raise PyGUITestError(f"{window!r} did not {wanted}: IsIconic disagrees")

    def set_window_title(self, window, title):
        """Replace `window`'s title, verifying that it took.

        `SetWindowTextW` first, then `SendMessageTimeoutW`'s `WM_SETTEXT` as the
        fallback for a window whose class ignores the first -- the two differ in
        which windows respond, and for some classes only the message works. Both
        reach another process, the message because the system marshals it.

        `SMTO_ABORTIFHUNG` is why the fallback goes through the timeout call
        rather than `SendMessage`: the platform expects applications to stop
        pumping messages, and this flag is what turns a hung target from a hung
        caller into an error. A title neither route changed raises rather than
        returning quietly.
        """
        self.require(Capability.WINDOW_TITLE_SET)
        lib = self._lib()
        handle = self._hwnd(window)
        lib.SetWindowTextW(handle, title)
        if self._title(handle) == title:
            return
        # WM_SETTEXT carries a pointer in lParam, so the text goes into a buffer
        # that stays alive across the call: SendMessageTimeoutW's argument is
        # declared integer-typed, which is how almost every other message uses
        # it, and a temporarily-created string would be freed before the target
        # could read it.
        buffer = ctypes.create_unicode_buffer(title)
        result = lib.SendMessageTimeoutW(
            handle,
            _winapi.WM_SETTEXT,
            0,
            ctypes.addressof(buffer),
            _winapi.SMTO_ABORTIFHUNG,
            1000,
            None,
        )
        if not result or self._title(handle) != title:
            raise PyGUITestError(
                f"{window!r} did not accept the title {title!r}: neither "
                "SetWindowTextW nor WM_SETTEXT changed it"
            )

    def lower_window(self, window):
        """Put `window` at the bottom of the z-order, focusing nothing.

        `SetWindowPos` with `HWND_BOTTOM`, which is the one operation here whose
        whole point is that the window must not come forward: it exists so a
        suite can clear its own window out of the way without disturbing whoever
        is using the desktop, which is what `WINDOW_LOWER` is for.
        """
        self.require(Capability.WINDOW_LOWER)
        result = self._lib().SetWindowPos(
            self._hwnd(window),
            _winapi.HWND_BOTTOM,
            0,
            0,
            0,
            0,
            _winapi.SWP_NOMOVE | _winapi.SWP_NOSIZE | _winapi.SWP_NOACTIVATE,
        )
        if not result:
            raise PyGUITestError(f"SetWindowPos failed to lower {window!r}")

    # -- window events (T3) ------------------------------------------------

    def window_events(self, timeout=None):
        """Yield WindowEvents from a `SetWinEventHook` subscription.

        `change` is one of "new", "close", "focus", "title" -- the same
        vocabulary `GnomeShellBackend`/the compositor-IPC backends yield, so
        `Session.wait_for_window`/`wait_window_close` need nothing
        Windows-specific to consume this. `timeout`, in seconds, bounds the
        whole call; the generator simply ends once it expires, and None waits
        indefinitely.

        A raw WinEvent hook is far noisier than what this yields: it fires for
        every window-class object on the desktop, controls included, not only
        the toplevels `windows()` reports. `known` -- seeded from `windows()`
        before the first event is read, then kept current here -- is what
        narrows that down and tells "new" from "title": a handle is added the
        first time it clears `_is_listable`, an `EVENT_OBJECT_DESTROY` for a
        handle *not* in `known` is silently dropped (a control's own destroy,
        never reported as anything to begin with), and one for a handle that
        *is* known reports "close" even when this call never saw it created --
        the ordinary case for `wait_window_close`, whose caller already had
        the window from an earlier `windows()` or `wait_for_window()` call.

        The hook and its pump thread exist only for this call: they are
        installed just before `known` is seeded and torn down in `finally`,
        the same per-call lifecycle `GnomeShellBackend.window_events` gives
        its D-Bus subscription. `SetWinEventHook` failing outright (a
        misdeclared flag, a desktop policy) raises `PyGUITestError` rather
        than yielding nothing forever, which is `_run_event_pump`'s "result"
        dict crossing back over the thread boundary once, before this
        generator does anything else observable.
        """
        self.require(Capability.WINDOW_EVENTS)
        sink: queue.Queue = queue.Queue()
        ready = threading.Event()
        result = {"error": None, "thread_id": None}
        pump = threading.Thread(
            target=_run_event_pump,
            args=(_relay_window_event(sink), ready, result),
            name="pyguitest-win32-events",
            daemon=True,
        )
        pump.start()
        ready.wait()
        if result["error"] is not None:
            pump.join(timeout=2.0)
            raise result["error"]
        known = {window.handle for window in self.windows()}
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                try:
                    event, hwnd = sink.get(timeout=remaining)
                except queue.Empty:
                    return
                change = self._classify_window_event(event, hwnd, known)
                if change is None:
                    continue
                if change == "close":
                    window = Window(handle=hwnd, backend=self)
                else:
                    window = self._window(hwnd)
                yield WindowEvent(change=change, window=window)
        finally:
            thread_id = result["thread_id"]
            if thread_id:
                self._lib().PostThreadMessageW(thread_id, _winapi.WM_QUIT, 0, 0)
            pump.join(timeout=2.0)

    def _classify_window_event(self, event, hwnd, known):
        """The `WindowEvent` verb for one raw WinEvent, updating `known` in place.

        `known` is the set of handles this `window_events()` call has already
        told its caller are open.

        `EVENT_OBJECT_CREATE` is "new" whenever the handle is listable, and
        that is so even for a handle already in `known` -- because there is
        exactly one way that happens and it is not a title change. The hook
        is installed *before* `known` is seeded from `windows()`, deliberately,
        so that no window opening during the seed is missed; the cost is that
        such a window is in the snapshot *and* has a create event queued
        behind it. Reading that second one as "title" is what a rule keyed
        only on `known` does, and it is wrong in the one direction that
        matters: `wait_for_window` accepts both verbs, but a caller filtering
        the stream for `change == "new"` would never see the window open. A
        create event means the window was created; a recycled handle is the
        only other reading, and "new" is right for that too.

        `EVENT_OBJECT_NAMECHANGE` keeps the split, and keeps it for its own
        reason: a rename can be the first sign of a window this call missed
        the creation of just as easily as it can be an ordinary title change,
        so an unknown handle that passes `_is_listable` is "new" and a known
        one is "title".

        `EVENT_OBJECT_DESTROY` reports "close" only for a handle already in
        `known`, which is what keeps every control's own destruction silent
        without needing a second, less trustworthy filter at the moment a
        window is tearing down. `EVENT_SYSTEM_FOREGROUND` reports "focus" for
        a known handle and falls back to the same "new" treatment for one that
        is not, on the theory that a window cannot become the foreground
        window without existing.

        Returns None for an event this stream reports nothing about, which is
        the ordinary answer for most of what a WinEvent hook delivers.
        """
        if event == _winapi.EVENT_OBJECT_CREATE:
            if not self._is_listable(hwnd):
                return None
            known.add(hwnd)
            return "new"
        if event == _winapi.EVENT_OBJECT_NAMECHANGE:
            if hwnd in known:
                return "title"
            if not self._is_listable(hwnd):
                return None
            known.add(hwnd)
            return "new"
        if event == _winapi.EVENT_OBJECT_DESTROY:
            if hwnd not in known:
                return None
            known.discard(hwnd)
            return "close"
        if event == _winapi.EVENT_SYSTEM_FOREGROUND:
            if hwnd in known:
                return "focus"
            if not self._is_listable(hwnd):
                return None
            known.add(hwnd)
            return "new"
        return None

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` (regex) appears.

        Checks existing windows first, so one already open when this is
        called is not missed -- the same race a bare `window_events()`
        subscription would have, and the same check
        `GnomeShellBackend.wait_for_window`/the compositor-IPC backends make
        before falling back to their own event stream.

        The *last* match among those, not the first, because `windows()` is
        ordered bottommost first and `Session.find_window` defines a title
        that several windows share as naming the topmost of them. Taking the
        first would make this method and `find_window` disagree about which
        window one title means, on the one platform where both are reachable.
        """
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        existing = [w for w in self.windows() if pattern.search(w.title)]
        if existing:
            return existing[-1]
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    # -- tier 6: the four a Wayland compositor refuses ---------------------
    #
    # Ordinary calls here, which is why a Windows `report()` shows them as
    # available under a heading whose description reads "deliberately
    # prevented": that heading is a Wayland ceiling, not a platform fact.

    def _off_desktop_note(self):
        """The window-station caveat, where it is the likely cause, else "".

        Every call that reaches the *input desktop* -- the pointer, the blit --
        fails with nothing to say when this process is not attached to an
        interactive window station, which is the ordinary state of a service, a
        scheduled task and an ssh session. `detect()` already knows; this is
        what gets that answer into the error the caller actually sees, measured
        on a real Windows 11 box over ssh where `GetCursorPos failed` and
        `BitBlt failed` were the whole message for a machine that was working
        exactly as designed.

        Empty where the backend was built without an environment, or on a
        station that *is* interactive: appending a guess to a failure it does
        not explain would be worse than the terse message.
        """
        environment = getattr(self, "environment", None)
        if environment is None or getattr(environment, "is_interactive_desktop", True):
            return ""
        return (
            " -- and this process is not attached to an interactive window "
            "station, which is almost certainly why: a service, a scheduled "
            "task and an ssh session all land outside it, and nothing on the "
            "desktop is reachable from there. Run from the logged-in session."
        )

    def pointer_position(self):
        """The global pointer position, in physical pixels."""
        self.require(Capability.POINTER_QUERY)
        point = _winapi.POINT()
        if not self._lib().GetCursorPos(ctypes.byref(point)):
            raise PyGUITestError(f"GetCursorPos failed{self._off_desktop_note()}")
        return (point.x, point.y)

    def is_button_pressed(self, button):
        """Whether a mouse button is currently held down."""
        self.require(Capability.INPUT_STATE_QUERY)
        return self._is_down(_BUTTON_VK[self._known_button(button)])

    def is_key_pressed(self, key):
        """Whether a key is currently held down. Replaces IsKeyPressed.

        The same resolution `press_key` uses, so a caller can ask about the key
        it just pressed, with one deliberate difference: the six sided keys
        (`Shift_L`/`Shift_R` and the two pairs below them) are asked about
        side-agnostically, because that is the only way Windows reports a
        modifier's state and it is what a caller writing "is Shift held" means.
        """
        self.require(Capability.INPUT_STATE_QUERY)
        vk = self._virtual_key(key)
        return self._is_down(_SIDELESS_VK.get(vk, vk))

    def _is_down(self, vk):
        """Whether virtual key `vk` is down right now.

        `GetAsyncKeyState`'s high bit only: the low bit is "it was pressed
        since the last call", which is a different question and one a test that
        polled twice would get wrong answers from.
        """
        return bool(self._lib().GetAsyncKeyState(vk) & 0x8000)

    def _known_button(self, button):
        """`button` if `_BUTTON_VK` has it, or raise naming what exists."""
        if button not in _BUTTON_VK:
            raise ValueError(
                f"unknown mouse button {button!r} on {self.name}; known buttons: "
                f"{', '.join(str(number) for number in sorted(_BUTTON_VK))}"
            )
        return button

    def is_window_cursor(self, window, shape):
        """Whether the pointer is over `window` showing cursor `shape`.

        Windows has no per-window cursor to read: a cursor's identity is a
        handle, and the handle the system reports is whichever one is being
        displayed. So the comparison is between the shown cursor and the
        standard handle `LoadCursorW` hands out for the same meaning, and the
        answer is about `window` only while the pointer is inside it -- a
        pointer anywhere else answers False rather than answering about some
        other window.

        A themed desktop is the other honest limit: its cursors are its own
        handles and match no standard one, so a suite that asserts on a cursor
        shape will see False however the desktop is configured. That is stated
        on `WINDOW_CURSOR_QUERY` as well, and it is the reason this capability
        is documented as useful for "has the pointer changed shape yet" rather
        than as a guarantee about a theme.

        `shape` is an X11 cursor-font number, as the legacy `IsWindowCursor`
        took and as `_CURSOR_SHAPES` records. The four corner-resize numbers
        raise rather than being mapped, and the reason is worth stating because
        it is not a gap that can be closed: Windows has *two* diagonal resize
        cursors where X11 has four corners, so `IDC_SIZENWSE` is shown for the
        top-left corner and the bottom-right one alike. Mapping
        `XC_top_left_corner` onto it would answer True with the pointer on the
        opposite corner -- a wrong True, where the refusal is merely a question
        this platform cannot be asked. X11's vocabulary has no "either
        diagonal" number to widen the question to, which is why the refusal
        names the limit instead of quietly approximating it.
        """
        self.require(Capability.WINDOW_CURSOR_QUERY)
        if shape not in _CURSOR_SHAPES:
            if shape in _DIAGONAL_SHAPES:
                raise ValueError(
                    f"cursor shape {shape!r} is X11's {_DIAGONAL_SHAPES[shape]} "
                    f"corner-resize cursor, which {self.name} cannot answer "
                    "about: Windows shows one cursor for a whole diagonal, so "
                    "this corner and the one opposite it are the same handle "
                    "here and cannot be told apart. Nothing can be passed "
                    "instead -- test the resize behaviour rather than the "
                    "cursor, or skip this assertion on Windows"
                )
            raise ValueError(
                f"cursor shape {shape!r} has no Windows counterpart on {self.name}; "
                f"known shapes: {', '.join(str(n) for n in sorted(_CURSOR_SHAPES))}"
            )
        lib = self._lib()
        handle = self._hwnd(window)
        info = _winapi.CURSORINFO()
        info.cbSize = ctypes.sizeof(_winapi.CURSORINFO)
        if not lib.GetCursorInfo(ctypes.byref(info)):
            raise PyGUITestError("GetCursorInfo failed")
        if not info.flags & _winapi.CURSOR_SHOWING:
            return False
        if not self._contains(handle, info.ptScreenPos.x, info.ptScreenPos.y):
            return False
        standard = lib.LoadCursorW(None, _winapi.MAKEINTRESOURCE(_CURSOR_SHAPES[shape]))
        return bool(standard) and info.hCursor == standard

    # -- clipboard (T3) ----------------------------------------------------

    def get_clipboard(self, primary=False):
        """The clipboard's text, or a refusal where PRIMARY is asked for.

        There is one clipboard on Windows: `primary=True` names the X11 and
        Wayland selection that middle-click paste reads, and there is no second
        selection here to name. It raises rather than quietly answering about
        the clipboard anyway, because a suite asserting on PRIMARY and getting
        the clipboard's own contents would pass for the wrong reason.

        A missing `CF_UNICODETEXT` is its own typed failure. An owner that
        publishes only `CF_TEXT` or `CF_HTML` is uncommon but real, and an empty
        string would be indistinguishable from an empty clipboard.

        One thing worth knowing before putting anything sensitive here: Windows
        clipboard history and cloud clipboard can retain and upload what a test
        writes. That is the user's machine setting, nothing here can detect it,
        and the same warning sits next to `set_clipboard`.
        """
        self.require(Capability.CLIPBOARD)
        self._refuse_primary(primary)
        lib = self._lib()
        self._open_clipboard()
        try:
            if not lib.IsClipboardFormatAvailable(_winapi.CF_UNICODETEXT):
                raise PyGUITestError(
                    "the clipboard holds no CF_UNICODETEXT; an application that "
                    "publishes only CF_TEXT or CF_HTML is the usual cause"
                )
            handle = lib.GetClipboardData(_winapi.CF_UNICODETEXT)
            if not handle:
                raise PyGUITestError("GetClipboardData returned nothing")
            return self._clipboard_text(handle)
        finally:
            lib.CloseClipboard()

    def set_clipboard(self, text, primary=False):
        """Replace the clipboard's text, or refuse where PRIMARY is asked for.

        The block is allocated with `GlobalAlloc(GMEM_MOVEABLE)` and handed to
        the system by `SetClipboardData`, which *transfers ownership*: freeing
        it afterwards is a crash waiting to happen, and failing to free it when
        `SetClipboardData` refuses is a leak. Both halves are handled here, and
        the second is the one an implementation written from a tutorial gets
        wrong.

        See `get_clipboard` on clipboard history and cloud sync: what a test
        writes here may leave the machine.
        """
        self.require(Capability.CLIPBOARD)
        self._refuse_primary(primary)
        lib = self._lib()
        memory = _winapi.kernel32()
        self._open_clipboard()
        try:
            if not lib.EmptyClipboard():
                raise PyGUITestError("EmptyClipboard failed")
            handle = self._alloc_text(text)
            if not lib.SetClipboardData(_winapi.CF_UNICODETEXT, handle):
                # Ownership never transferred, so this block is ours to free.
                memory.GlobalFree(handle)
                raise PyGUITestError("SetClipboardData failed")
        finally:
            lib.CloseClipboard()

    def _refuse_primary(self, primary):
        """Raise where a caller asks for PRIMARY, which does not exist here."""
        if primary:
            raise CapabilityUnsupported(
                Capability.CLIPBOARD,
                self.name,
                "Windows has one clipboard and no PRIMARY selection beside it; "
                "PRIMARY is the X11/Wayland selection that middle-click paste "
                "reads",
            )

    def _open_clipboard(self):
        """Take the clipboard, retrying briefly, or raise naming the holder.

        `OpenClipboard` fails while another process holds the clipboard open,
        and that is ordinary rather than exceptional: clipboard managers,
        Office and remote-desktop helpers all hold it for milliseconds at a
        time. The retry is short, and the failure is typed with a message that
        says what is wrong -- the underlying error is an access-denied from
        three frames down that mentions none of it.
        """
        lib = self._lib()
        for attempt in range(5):
            if lib.OpenClipboard(None):
                return
            time.sleep(0.02 * (attempt + 1))
        raise PermissionRequired(
            Capability.CLIPBOARD,
            self.name,
            "another application is holding the clipboard open; it lets go when "
            "it is finished, so the call can simply be retried",
        )

    def _alloc_text(self, text):
        """`text` in a movable global block, as `SetClipboardData` wants it.

        UTF-16 with its terminating NUL, which is what `CF_UNICODETEXT`
        promises. The block is unlocked again as soon as the copy is done:
        leaving it locked is documented as harmful to any caller that later
        tries to read it, this backend's own `get_clipboard` included.
        """
        memory = _winapi.kernel32()
        raw = (text + "\0").encode("utf-16-le")
        handle = memory.GlobalAlloc(_winapi.GMEM_MOVEABLE, len(raw))
        if not handle:
            raise PyGUITestError("GlobalAlloc failed for the clipboard text")
        pointer = memory.GlobalLock(handle)
        if not pointer:
            memory.GlobalFree(handle)
            raise PyGUITestError("GlobalLock failed for the clipboard text")
        try:
            ctypes.memmove(pointer, raw, len(raw))
        finally:
            memory.GlobalUnlock(handle)
        return handle

    def _clipboard_text(self, handle):
        """The text inside a clipboard block, as `str`.

        Decoded from the bytes `GlobalSize` reports rather than through
        `wstring_at`, so the length is the block's own -- a `wstring_at` without
        one reads past the end of a malformed owner's block. The terminating NUL
        is stripped, because the format guarantees one and a caller comparing
        `get_clipboard()` with what they set would otherwise never match.
        """
        memory = _winapi.kernel32()
        pointer = memory.GlobalLock(handle)
        if not pointer:
            raise PyGUITestError("GlobalLock failed for the clipboard text")
        try:
            raw = ctypes.string_at(pointer, memory.GlobalSize(handle))
        finally:
            memory.GlobalUnlock(handle)
        return raw.decode("utf-16-le", errors="replace").rstrip("\0")

    # -- capture (T2/T5) ---------------------------------------------------

    def capture(self, window=None, path=None, region=None):
        """Write a screenshot and return its path.

        `window=` is refused rather than half-answered. Capturing one window
        un-occluded needs `PrintWindow` (a later phase) or Windows.Graphics
        Capture, so WINDOW_CAPTURE is not declared -- which means this raises
        `CapabilityUnsupported` rather than returning a crop that includes
        whatever happens to be on top. Composite composition crops via
        `geometry()` in the meantime, and that path is honest for exactly the
        same reason.

        The pixels come from GDI: `GetDC(NULL)` for the screen, a compatible
        bitmap, `BitBlt` with `SRCCOPY | CAPTUREBLT`, and `GetDIBits` into a
        buffer. `CAPTUREBLT` is the flag that includes layered windows, without
        which every window drawn with `WS_EX_LAYERED` -- most modern application
        windows -- is missing from the result.

        Two limits are documented rather than detected, because neither can be
        honestly detected from here. Content drawn outside GDI's knowledge (some
        DirectX surfaces, overlay planes, protected video) comes back black, and
        there is no grant that fixes it. And the blit covers the *virtual
        desktop*, whose origin is not (0,0) when a monitor sits left of or above
        the primary -- which is why `region`, when given, is in virtual-desktop
        pixels like everything else here.
        """
        self.require(
            Capability.WINDOW_CAPTURE
            if window is not None
            else Capability.SCREEN_CAPTURE
        )
        x, y, width, height = check_region(region, window) or self._virtual_screen()
        if path is None:
            # The same convention the tool-driven capture backends use: a
            # temporary file whose suffix the encoder's format matches.
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        return _png.write_rgb(path, width, height, self._blit(x, y, width, height))

    def _blit(self, x, y, width, height):
        """The pixels of one screen rectangle, as top-down RGB rows.

        GDI hands them back bottom-up, so the rows are reversed on the way out
        of `_dib_rows` rather than in the encoder: `pyguitest.png` writes
        scanlines in the order it is given them, and an upside-down screenshot
        is a bug in whatever read it.

        The order of the two GDI steps is a documented requirement rather than a
        preference. `BitBlt` needs the bitmap selected into the memory DC --
        that is what it draws into -- and `GetDIBits` documents the opposite,
        that the bitmap "must not be selected into a device context" when it is
        called. So the selection is undone between the two, and the pixels are
        read back from a bitmap that is no longer selected anywhere.
        """
        lib = self._lib()
        gdi = _winapi.gdi32()
        if gdi is None:
            raise BackendUnavailable(
                "gdi32.dll is not available, so there is no way to read the "
                "screen's pixels"
            )
        screen = lib.GetDC(None)
        if not screen:
            raise PyGUITestError("GetDC failed")
        memory_dc = None
        bitmap = None
        try:
            memory_dc = gdi.CreateCompatibleDC(screen)
            bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
            if not memory_dc or not bitmap:
                raise PyGUITestError("could not create a bitmap for the capture")
            # A failed selection leaves the DC's original 1x1 monochrome
            # surface in place, and `BitBlt` then succeeds against *that* --
            # so the bitmap `GetDIBits` reads was never drawn into, and the
            # screenshot comes back black with nothing having reported a
            # failure.
            previous = gdi.SelectObject(memory_dc, bitmap)
            if not previous:
                raise PyGUITestError("could not select the capture bitmap into its DC")
            try:
                if not gdi.BitBlt(
                    memory_dc,
                    0,
                    0,
                    width,
                    height,
                    screen,
                    x,
                    y,
                    _winapi.SRCCOPY | _winapi.CAPTUREBLT,
                ):
                    raise PyGUITestError(
                        f"BitBlt failed for {width}x{height} at ({x}, {y})"
                        f"{self._off_desktop_note()}"
                    )
            finally:
                gdi.SelectObject(memory_dc, previous)
            return self._dib_rows(gdi, memory_dc, bitmap, width, height)
        finally:
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory_dc:
                gdi.DeleteDC(memory_dc)
            lib.ReleaseDC(None, screen)

    def _dib_rows(self, gdi, memory_dc, bitmap, width, height):
        """A blitted bitmap as RGB rows, top row first.

        `GetDIBits` fills the buffer bottom-up, because `biHeight` is positive
        -- the platform's documented default. Read back in reverse, that
        becomes the top-down order `_blit` promises and `pyguitest.png` writes.

        32-bit `BI_RGB`, so every pixel is four bytes with blue first and the
        alpha byte undefined -- three slices pick out the three colours, which
        is a C-level operation per row rather than a Python loop per pixel. The
        32-bit format is what `GetDIBits` documents as always available, and it
        removes the row padding a 24-bit DIB would need.

        `bitmap` must already have been deselected from `memory_dc`, which is
        `GetDIBits`' own documented precondition; `_blit` is where that happens.
        """
        header = _winapi.BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_winapi.BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _winapi.BI_RGB
        stride = width * 4
        buffer = ctypes.create_string_buffer(stride * height)
        copied = gdi.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(header),
            _winapi.DIB_RGB_COLORS,
        )
        if copied != height:
            raise PyGUITestError(f"GetDIBits returned {copied} of {height} scanlines")
        rows = []
        for row in range(height - 1, -1, -1):
            source = buffer.raw[row * stride : (row + 1) * stride]
            pixels = bytearray(width * 3)
            pixels[0::3] = source[2::4]
            pixels[1::3] = source[1::4]
            pixels[2::3] = source[0::4]
            rows.append(bytes(pixels))
        return rows
