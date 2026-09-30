"""Input injection on macOS: `CGEventPost`, and nothing else.

Split from `macos` because the two halves need different grants and different
PyObjC distributions. This one posts events and never calls an AX API, so it
preflights **PostEvent** (`kTCCServicePostEvent`) and not Accessibility --
Apple's own guidance is explicit that the Accessibility privilege is not what
`CGEventPost` requires, even though System Settings files the two under one
pane. Conflating them would make this backend refuse itself on a machine that
had granted exactly what it needs.

Coordinates here are global display space with the origin at the *top left* of
the primary display. That is not a choice this module gets to make: it is the
only space `CGEventPost` accepts, and it is the space `CGEventGetLocation`
answers in, which is why `pointer_position()` needs no grant at all on a Mac
and no translation either.

`CGEventPost` reports nothing. There is no return value meaning "the target
received it", so `INPUT_SYNC` is absent from `capabilities` rather than
implemented as a no-op -- a `sync()` that always returned True would be a lie
a test could be built on, which is the same reason ADR 004 calls it a
permanent refusal.

The *requesting* form of the grant is called from `MacquartzBackend.__init__`,
which is only ever reached by a caller who named this backend: `connect(
backend="macquartz")` **is** the request, and the registry's `opt_in` flag is
what keeps a plain `connect()` from asking for anything at all.

Requesting and *prompting* came apart under measurement, which is worth being
precise about because the ADR that chose this shape was written from the
documentation: on macOS 26.7, called from a process started in a terminal,
`CGRequestPostEventAccess()` put no dialog on screen and answered False -- see
docs/validation.md, where that is recorded as measured on one machine. The
grant itself is made by hand in System Settings > Privacy & Security >
Accessibility, and TCC then records the answer against the app that launched
this process (Terminal, or an IDE) or against the signed interpreter it runs,
rather than against a user or the virtualenv path this process starts from.
Over SSH there is no such app at all: tccd attributes the process to the
session's own `sshd-keygen-wrapper`, an Apple platform binary whose prompt
policy is "no prompt", so the SSH route grants nothing by asking and a row
filed by hand under that name does not match either.

With the grant in place the rest of this module is measured rather than
transcribed. The Unicode path carries an accented letter, a euro sign and a Han
character; `~` and a bare newline both break a line; named keys and held
modifiers arrive where they were aimed; `move_mouse` lands exactly where
`CGEventGetLocation` reads; and `scroll`'s sign is a reading rather than an
assumption. docs/validation.md has each of them, and the numbers.
"""

from __future__ import annotations

import importlib
import time

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported
from . import _macapi
from .base import GUIBackend

__all__ = ["MacquartzBackend", "available", "client", "request_post_event"]

_NEEDED = (
    "CGEventCreateMouseEvent",
    "CGEventCreateScrollWheelEvent",
    "CGEventCreateKeyboardEvent",
    "CGEventCreate",
    "CGEventGetLocation",
    "CGEventPost",
    "CGEventSetFlags",
    "CGEventSetIntegerValueField",
    "CGEventSourceCreate",
    "CGEventKeyboardSetUnicodeString",
    "CGRequestPostEventAccess",
)
"""Quartz entry points this backend calls, by the names PyObjC gives them.

Only the functions, and every one of them: a missing symbol is a PyObjC too
old for this backend, which is a condition to report rather than an
AttributeError from inside `move_mouse`. The `kCG*` constants are deliberately
not listed -- they are read straight off `Quartz` at the call site, where the
documented name is its own documentation, and a test's fake module can answer
any `kCG*` name without a table of its own.
"""


def client():
    """PyObjC's `Quartz` module, or raise naming what is missing.

    The seam the tests replace, in the shape `tests/test_x11.py` established
    for `Xlib`: a fake module installed in `sys.modules` is what
    `importlib.import_module` finds, so nothing here needs a patch target of
    its own.
    """
    try:
        quartz = importlib.import_module("Quartz")
    except ImportError as exc:
        raise BackendUnavailable(
            "input injection on macOS needs PyObjC's Quartz, which is not "
            "importable here; install the 'macos' extra "
            f"(pip install 'pyguitest[macos]'): {exc}"
        ) from exc
    absent = [name for name in _NEEDED if not hasattr(quartz, name)]
    if absent:
        raise BackendUnavailable(
            f"Quartz does not provide {', '.join(absent)}, which CGEvent "
            "injection needs; the installed PyObjC is older than this backend "
            "is written against"
        )
    return quartz


def available() -> bool:
    """Whether CGEvent injection can be attempted from this process.

    Importability only. The PostEvent *grant* is a separate question, asked by
    `_macapi.post_event_allowed`, and a factory that folded the two into one
    answer would leave a caller unable to tell "install the extra" from
    "grant the permission" -- the distinction ADR 004's preflight/prompt split
    exists to keep.
    """
    try:
        client()
    except BackendUnavailable:
        return False
    return True


def request_post_event() -> bool:
    """Ask for the PostEvent grant, and return the status **as it stands**.

    `CGRequestPostEventAccess` is documented as the prompting form and is the
    closest thing to a request this package can make, but it does not put a
    dialog on screen here. Measured on macOS 26.7 from a process started in a
    terminal: it answered False and showed nothing. So nothing may read the
    return value as "the user's answer", and -- the part that changed -- no
    reader may be told that naming this backend offers them something to
    click. The grant comes from System Settings.

    Still called, and only from a named backend's constructor, because it is
    where the request belongs: a caller who has already granted it gets True
    back, which is what `capabilities` reads to keep a live grant honest.
    """
    quartz = client()
    return bool(quartz.CGRequestPostEventAccess())


# -- keycodes --------------------------------------------------------------
#
# `kVK_*` from Carbon's `Events.h`, which is the only place Apple publishes
# them: `CGEventCreateKeyboardEvent` takes the number, PyObjC exposes no
# constant for it, and `Carbon` itself is a 32-bit-only framework that no
# 64-bit process can link. So this table is a transcription, and what it is
# transcribed *from* is worth naming for whoever checks it.
#
# The names are what this backend's press_key/release_key take, and they are
# deliberately the printed legend rather than a keysym: `minus`, `leftbracket`,
# `quote`. The layout is US, which is the whole reason `type_text` does not
# use this table at all -- see its docstring.

_KEYCODES = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "equal": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "minus": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "rightbracket": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "leftbracket": 0x21,
    "i": 0x22,
    "p": 0x23,
    "return": 0x24,
    "l": 0x25,
    "j": 0x26,
    "quote": 0x27,
    "k": 0x28,
    "semicolon": 0x29,
    "backslash": 0x2A,
    "comma": 0x2B,
    "slash": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    "period": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "grave": 0x32,
    "delete": 0x33,
    "escape": 0x35,
    "rightcommand": 0x36,
    "command": 0x37,
    "shift": 0x38,
    "capslock": 0x39,
    "option": 0x3A,
    "control": 0x3B,
    "rightshift": 0x3C,
    "rightoption": 0x3D,
    "rightcontrol": 0x3E,
    "function": 0x3F,
    "f17": 0x40,
    "f18": 0x4F,
    "f19": 0x50,
    "f20": 0x5A,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f3": 0x63,
    "f8": 0x64,
    "f9": 0x65,
    "f11": 0x67,
    "f13": 0x69,
    "f16": 0x6A,
    "f14": 0x6B,
    "f10": 0x6D,
    "f12": 0x6F,
    "f15": 0x71,
    "help": 0x72,
    "home": 0x73,
    "pageup": 0x74,
    "forwarddelete": 0x75,
    "f4": 0x76,
    "end": 0x77,
    "f2": 0x78,
    "pagedown": 0x79,
    "f1": 0x7A,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
}
"""US-layout virtual keycodes, keyed by the name press_key takes.

Case-insensitive at lookup, so `send_keys("{ENT}")` and `press_key("Return")`
both land on `return`; the names are lower-case here because that is how the
printed legends read.
"""

_X11_KEYSYMS = {
    "BackSpace": "delete",
    "Delete": "forwarddelete",
    "Super_L": "command",
    "Super_R": "rightcommand",
    "Shift_L": "shift",
    "Shift_R": "rightshift",
    "Alt_L": "option",
    "Alt_R": "rightoption",
    "Control_L": "control",
    "Control_R": "rightcontrol",
    "Caps_Lock": "capslock",
    "Page_Up": "pageup",
    "Page_Down": "pagedown",
    "bracketleft": "leftbracket",
    "bracketright": "rightbracket",
    "apostrophe": "quote",
}
"""The X11 keysym spelling of a key this table names after its legend.

Keys are X11 `keysymdef.h` names, values are the `_KEYCODES` names above, and
the entries are exactly the ones the two vocabularies cannot otherwise agree
on. A recording names a key with the X11 keysym of the key printed in that
position -- pyguitest-recorder's macOS backend derives its keycode table from
`_KEYCODES` and renames each entry to its keysym -- so a recorded `Alt_L`,
`BackSpace` or `apostrophe` has to resolve here or a replay dies on a key the
recording had every right to name.

Most entries differ only in *spelling*: `Page_Up` against `pageup`,
`bracketleft` against `leftbracket`, `apostrophe` against `quote`, and the
eight modifiers against the `command`/`option`/`control`/`shift` labels a Mac
prints on them. `Delete` is the one that is not a spelling difference but a
collision, and it is why the lookup below is case-sensitive -- see `_key_name`.
"""

_X11_KEYSYMS_FOLDED = {
    name.lower(): canonical
    for name, canonical in _X11_KEYSYMS.items()
    # `Delete` is absent deliberately: lowercased it is `delete`, which is this
    # table's own name for a *different* key (backspace, 51). The case-sensitive
    # lookup is the only place it may resolve.
    if name != "Delete"
}
"""`_X11_KEYSYMS` for callers who wrote a spelling in another case.

So `{BackSpace}`, `{backspace}` and `{BACKSPACE}` are one key, the same promise
`_KEYCODES` makes for the legend names. `Delete` cannot join them: folded it
would take the backspace key away from `delete`.
"""


def _key_name(key):
    """The name `_KEYCODES` and `_MODIFIER_FLAGS` know `key` by.

    One step, called by everything that resolves a key, because the two tables
    it feeds have to agree. `press_key("Super_L")` that looked its keycode up
    through an alias while the *held* bookkeeping compared the caller's own
    spelling would post the Command key and then fail to stamp Command onto the
    `s` of the `Super_L`-s chord the recording says it is -- the chord's own
    second event, which is the half that makes it a chord.

    The X11 spelling wins, case-sensitively, because exactly one pair of names
    needs it: X11's `Delete` is the forward-delete key (117) and this backend's
    `delete` is the key a Mac keyboard prints "delete" on (51, backspace).
    Lowercased they are one string, so `Delete` resolves before the fold. A
    caller who writes `DELETE` gets `delete`, the legend -- X11's forward delete
    is spelled `forwarddelete` or `{DEL}`.
    """
    folded = key.lower() if isinstance(key, str) else key
    return _X11_KEYSYMS.get(key, _X11_KEYSYMS_FOLDED.get(folded, folded))


_MODIFIER_FLAGS = {
    "command": "kCGEventFlagMaskCommand",
    "rightcommand": "kCGEventFlagMaskCommand",
    "shift": "kCGEventFlagMaskShift",
    "rightshift": "kCGEventFlagMaskShift",
    "option": "kCGEventFlagMaskAlternate",
    "rightoption": "kCGEventFlagMaskAlternate",
    "control": "kCGEventFlagMaskControl",
    "rightcontrol": "kCGEventFlagMaskControl",
    "capslock": "kCGEventFlagMaskAlphaShift",
}
"""Which Quartz flag constant each modifier keycode stands for.

Both hands map onto one flag, because CGEvent has one bit per modifier and not
one per physical key. The dict exists so that a held modifier is reflected in
the *flags* of every event posted while it is down, which is how the receiving
application learns about it -- the keycode alone says only which key moved.
"""

_CHAR_KEYS = {
    " ": ("space", False),
    "`": ("grave", False),
    "~": ("grave", True),
    "-": ("minus", False),
    "_": ("minus", True),
    "=": ("equal", False),
    "+": ("equal", True),
    "[": ("leftbracket", False),
    "{": ("leftbracket", True),
    "]": ("rightbracket", False),
    "}": ("rightbracket", True),
    "\\": ("backslash", False),
    "|": ("backslash", True),
    ";": ("semicolon", False),
    ":": ("semicolon", True),
    "'": ("quote", False),
    '"': ("quote", True),
    ",": ("comma", False),
    "<": ("comma", True),
    ".": ("period", False),
    ">": ("period", True),
    "/": ("slash", False),
    "?": ("slash", True),
    "!": ("1", True),
    "@": ("2", True),
    "#": ("3", True),
    "$": ("4", True),
    "%": ("5", True),
    "^": ("6", True),
    "&": ("7", True),
    "*": ("8", True),
    "(": ("9", True),
    ")": ("0", True),
}
"""char -> (key name, needs_shift) for everything but letters and digits.

The macOS counterpart of `base._SENDKEYS_PLAIN`/`_SENDKEYS_SHIFTED`, and a
separate table rather than an override of them: those two hold X11 *keysym*
names, and `slash` is the same word in both while `bracketleft` and
`apostrophe` are not. Letters and digits stay out of it -- each is its own
keycode except that a capital needs shift -- and `resolve_char_key` handles
both tables.
"""


def pointer_position(quartz):
    """The pointer's position in global display space, ungated.

    Module-level and free of any capability check on purpose: a button press
    has to place itself, and asking through the backend would refuse on the
    one machine whose grant a click does not need. `POINTER_QUERY` is
    `macos`'s capability, not this backend's.
    """
    point = quartz.CGEventGetLocation(quartz.CGEventCreate(None))
    return int(point.x), int(point.y)


_BUTTONS = {1: "Left", 2: "Other", 3: "Right"}
"""X11 button number -> the CGEvent event-type stem for it.

X11 numbers buttons and so does this interface: 1 is primary, 2 middle, 3
secondary -- the opposite of *Windows'* ordering, where 3 is the primary.
CGEvent calls the middle one `Other` in its event types and `Center` in its
button constant, which is why these are two tables rather than one.
"""

_CG_BUTTONS = {
    1: "kCGMouseButtonLeft",
    2: "kCGMouseButtonCenter",
    3: "kCGMouseButtonRight",
}


_DOUBLE_CLICK_SECONDS = 0.5
"""How close two presses have to be to count as one multi-click, where the
user's preference is unset or unreadable. macOS's own default, and what
`NSEvent.doubleClickInterval()` answers on a machine where the setting has not
been moved -- measured, 0.5 on the live machine."""

_DOUBLE_CLICK_SLOP = 4
"""How far apart, in points, two presses may land and still be one multi-click.
A person's second click is never on exactly the first one's pixel."""


def _double_click_seconds(quartz) -> float:
    """The user's double-click interval, or the macOS default where unset.

    Read from the global preference AppKit's `NSEvent.doubleClickInterval`
    reports, through the Quartz module this backend already holds rather than
    by importing AppKit: that import loaded PyObjC's core outside the module
    the tests fake, and on a Mac the live tests later in the same run then
    failed with "Reload of objc._objc detected". Unset -- the ordinary case --
    reads None, which is the default.
    """
    try:
        value = quartz.CFPreferencesCopyAppValue(
            "com.apple.mouse.doubleClickThreshold", quartz.kCFPreferencesAnyApplication
        )
        return float(value) if value else _DOUBLE_CLICK_SECONDS
    except Exception:  # noqa: BLE001 - no answer is the default
        return _DOUBLE_CLICK_SECONDS


def _button_events(button: int) -> tuple[str, str, str]:
    """(down name, up name, CG button name) for an X11 button number.

    Raises outside 1-3 rather than passing the number through: CGEvent has no
    number for a button, only the three named constants, and a
    `press_button(4)` that no application ever sees is a worse answer than a
    refusal -- the call `ToolInputBackend._check_button` makes for ydotool.
    """
    side = _BUTTONS.get(button)
    if side is None:
        raise CapabilityUnsupported(
            Capability.POINTER_BUTTON,
            "macquartz",
            f"button {button!r} has no macOS mapping; 1, 2 and 3 are the "
            "primary, middle and secondary buttons",
        )
    return f"kCGEvent{side}MouseDown", f"kCGEvent{side}MouseUp", _CG_BUTTONS[button]


def _utf16_units(text: str) -> int:
    """How many UTF-16 code units `text` occupies.

    The length `CGEventKeyboardSetUnicodeString` wants, which is not `len()`:
    PyObjC declares that parameter as `UniChar *` and UniChar is 16 bits, so a
    character outside the BMP -- an emoji, most obviously -- is one Python
    character and *two* units. Passing `len()` there tells CGEvent to read half
    of a surrogate pair, which arrives as a wrong character rather than as a
    dropped one, and nothing about it raises. Byte order makes no difference
    to a count; the encoding is used because it is the one that counts units
    rather than characters.
    """
    return len(text.encode("utf-16-le")) // 2


class MacquartzBackend(GUIBackend):
    """Pointer, buttons, scroll, keys and text, over `CGEventPost`.

    Registered at 70, beside `win32` and the Linux tool-backed input backends,
    and `opt_in` because constructing it *asks* for the PostEvent grant -- see
    `register`'s docstring on what that flag is for, and `request_post_event`
    on why asking is not measurable as prompting on a Mac. Nothing here reads
    an AX attribute, which is what keeps it off the Accessibility grant.
    """

    name = "macquartz"

    MODIFIER_KEYS = {
        "^": "control",
        "%": "option",
        "+": "shift",
        "#": "command",
    }
    """send_keys()'s modifier characters, mapped to this backend's key names.

    Four where the inherited table has five. `&` (AltGr) is dropped rather
    than aliased to Option: Option is a real key on a Mac and level 3 is not,
    so aliasing would make `{&x}` post Option-x while reading as something
    else. A refusal a caller can see beats a silent change of meaning -- the
    rule ADR 004 §7 applies to four other spellings here.
    """

    KEY_ALIASES = {
        "BAC": "delete",
        "BS": "delete",
        "BKS": "delete",
        "DEL": "forwarddelete",
        "DOWN": "down",
        "UP": "up",
        "LEF": "left",
        "RIG": "right",
        "END": "end",
        "ENT": "return",
        "ESC": "escape",
        "HOM": "home",
        "PGD": "pagedown",
        "PGU": "pageup",
        "TAB": "tab",
        "F1": "f1",
        "F2": "f2",
        "F3": "f3",
        "F4": "f4",
        "F5": "f5",
        "F6": "f6",
        "F7": "f7",
        "F8": "f8",
        "F9": "f9",
        "F10": "f10",
        "F11": "f11",
        "F12": "f12",
        "SPC": "space",
        "SPA": "space",
        "LSH": "shift",
        "RSH": "rightshift",
        "LCT": "control",
        "RCT": "rightcontrol",
        "LAL": "option",
        "RAL": "rightoption",
        "LSK": "command",
        "RSK": "rightcommand",
        "LMA": "command",
        "RMA": "rightcommand",
    }
    """send_keys()'s `{BAC}`-style abbreviations, in this backend's vocabulary.

    The same abbreviations X11::GUITest used, mapped onto macOS key names
    rather than X11 keysyms. Seven are deliberately absent -- BRE, CAN, HEL,
    INS, MNU, NUM and PRT name keys a Mac does not have -- so `{INS}` raises
    naming the key instead of quietly pressing a different one.
    """

    def __init__(self, environment=None):
        """Open Quartz, and ask for the PostEvent grant.

        `opt_in` is what makes asking defensible: this constructor runs only
        for a caller who named `macquartz`, so `connect(backend="macquartz")`
        **is** the request. A plain `connect()` never reaches it, and so never
        asks for anything on anyone else's behalf.

        No dialog is expected -- `request_post_event` records what the call
        actually did on the one machine it has been measured on -- so this is
        not a way to grant anything, and no message should send a reader
        looking for a prompt it will not find. `capabilities` is where the
        answer shows up, and System Settings is where the grant is made.
        """
        self._q = client()
        self.environment = environment
        self._source = self._q.CGEventSourceCreate(
            self._q.kCGEventSourceStateHIDSystemState
        )
        self._held: set[str] = set()
        # Buttons this backend has pressed and not released, in press order:
        # what makes a move a drag. See `move_mouse`.
        self._buttons: list[int] = []
        # (button, when, x, y, count) of the last press, for the click count a
        # Cocoa application reads a double-click from. See `_click_count`.
        self._last_press: tuple[int, float, int, int, int] | None = None
        request_post_event()

    @property
    def capabilities(self):
        """Every input capability, unless PostEvent has not been granted.

        Read live rather than cached, so a grant made while this session is
        alive is picked up: TCC stores the answer where the next preflight
        sees it, and a set frozen at construction would keep saying no to a
        machine that had just said yes. Withdrawn rather than raised from each
        method, because `capabilities` is what a suite reads to decide whether
        to skip.
        """
        if not _macapi.post_event_allowed():
            return CapabilitySet()
        return CapabilitySet(
            {
                Capability.POINTER_MOVE,
                Capability.POINTER_BUTTON,
                Capability.POINTER_SCROLL,
                Capability.KEY_EVENT,
                Capability.TEXT_ENTRY,
            }
        )

    def _flags(self) -> int:
        """The flags to stamp on an event posted right now.

        The modifiers this backend believes are held, or-ed together. CGEvent
        carries modifier state per event rather than tracking it, so an event
        that does not say Shift is not a shifted event however the key
        physically stands -- which is why `press_key` on a modifier records it
        and every later event reads this.
        """
        flags = 0
        for name in self._held:
            flags |= getattr(self._q, _MODIFIER_FLAGS[name])
        return flags

    def _post(self, event) -> None:
        """Post `event` at the HID tap.

        The HID tap rather than the session or annotated-session taps: those
        two are where an application's *own* event taps see events, so posting
        there would put these where the desktop's normal processing has
        already passed. Requires PostEvent; never Accessibility.
        """
        self._q.CGEventPost(self._q.kCGHIDEventTap, event)

    # -- pointer -----------------------------------------------------------

    def move_mouse(self, x, y, screen=0):
        """Move the pointer to an absolute position in global display space.

        `screen` is accepted because the interface has it, and refused when it
        is non-zero. Coordinates here already span every display -- CGEvent's
        space has one origin for the whole desktop -- so there is nothing for
        a screen index to select, and ignoring one would let a caller believe
        they had addressed a second display.

        **With a button held, the move is a drag event, not a move.** macOS
        has a separate event type for motion under a pressed button
        (`kCGEventLeftMouseDragged` and its right and other siblings), and a
        `kCGEventMouseMoved` posted in its place is not delivered as a drag at
        all: measured on macOS 26.7, `Session.drag` across a Tk canvas posted
        its whole glide as moves, the canvas saw a press and a release and not
        one motion event between them, and the release landed where the press
        had -- nothing was dragged. The button the drag event names is the
        first one still held, which is the one a drag is conventionally about.
        """
        self.require(Capability.POINTER_MOVE)
        if screen:
            raise CapabilityUnsupported(
                Capability.POINTER_MOVE,
                self.name,
                "coordinates on macOS are already desktop-global, so there is "
                "no screen to address; pass screen=0 or leave it out",
            )
        if self._buttons:
            held = self._buttons[0]
            kind = f"kCGEvent{_BUTTONS[held]}MouseDragged"
            button = _CG_BUTTONS[held]
        else:
            kind, button = "kCGEventMouseMoved", "kCGMouseButtonLeft"
        self._post(
            self._q.CGEventCreateMouseEvent(
                self._source,
                getattr(self._q, kind),
                (x, y),
                getattr(self._q, button),
            )
        )

    def press_button(self, button):
        """Press a mouse button, wherever the pointer already is.

        The position is read rather than assumed (0, 0): a button event
        carries coordinates, and a press sent with the wrong ones moves the
        pointer there before clicking -- which is a click on something else,
        silently.
        """
        self.require(Capability.POINTER_BUTTON)
        down, _up, cg_button = _button_events(button)
        x, y = pointer_position(self._q)
        count = self._click_count(button, x, y)
        self._post(self._button_event(down, (x, y), cg_button, count))
        if button not in self._buttons:
            self._buttons.append(button)

    def release_button(self, button):
        """Release a mouse button, wherever the pointer already is.

        The release carries the click count of the press it ends, as a
        hardware release does.
        """
        self.require(Capability.POINTER_BUTTON)
        _down, up, cg_button = _button_events(button)
        x, y = pointer_position(self._q)
        last = self._last_press
        count = last[4] if last is not None and last[0] == button else 1
        if button in self._buttons:
            self._buttons.remove(button)
        self._post(self._button_event(up, (x, y), cg_button, count))

    def _button_event(self, kind, point, cg_button, count):
        """One button event, stamped with its click count."""
        event = self._q.CGEventCreateMouseEvent(
            self._source, getattr(self._q, kind), point, getattr(self._q, cg_button)
        )
        self._q.CGEventSetIntegerValueField(
            event, self._q.kCGMouseEventClickState, count
        )
        return event

    def _click_count(self, button, x, y):
        """1 for a lone press, 2 for the second of a double-click, and so on.

        A Cocoa application does not time presses itself: it reads
        `NSEvent.clickCount`, which comes from the event's
        `kCGMouseEventClickState` field, and CGEventCreateMouseEvent leaves that
        at 1. So two presses posted inside the double-click interval were still
        two single clicks to AppKit -- measured on macOS 26.7, where
        `Session.double_click` on a word in TextEdit selected nothing. The
        count is kept here the way the window server keeps it for hardware:
        the same button again, within the user's double-click interval and a
        few points of the last press, counts up; anything else starts over.
        """
        now = time.monotonic()
        last = self._last_press
        count = 1
        if (
            last is not None
            and last[0] == button
            and now - last[1] <= _double_click_seconds(self._q)
            and abs(x - last[2]) <= _DOUBLE_CLICK_SLOP
            and abs(y - last[3]) <= _DOUBLE_CLICK_SLOP
        ):
            count = last[4] + 1
        self._last_press = (button, now, x, y, count)
        return count

    def scroll(self, dx=0, dy=0):
        """Scroll by wheel steps, on whichever axes are non-zero.

        The sign convention is the package's rather than Apple's: positive
        `dy` is up and positive `dx` is right, matching `x11.py`'s buttons 4
        and 7 and evdev's `REL_WHEEL`. A wheel event's deltas run the same way,
        so the values are passed through instead of negated -- and that was
        the line to change if a live check ever said otherwise, so it has been
        checked. On macOS 26.7 with the PostEvent grant in place,
        `scroll(dy=5)` moves a TextEdit document toward its start and
        `scroll(dy=-5)` toward its end, read off the window's vertical
        scrollbar and its visible-character range rather than off a
        screenshot, and a hand-built `CGEventCreateScrollWheelEvent` carrying
        the same wheel value moves it the same way. No negation is needed, and
        docs/validation.md has the numbers.
        """
        self.require(Capability.POINTER_SCROLL)
        if not dx and not dy:
            return
        self._post(
            self._q.CGEventCreateScrollWheelEvent(
                self._source, self._q.kCGScrollEventUnitLine, 2, int(dy), int(dx)
            )
        )
        return

    # -- keyboard ----------------------------------------------------------

    def _keycode(self, key) -> int:
        """`key`'s virtual keycode, or raise naming what is known.

        Resolved through `_key_name`, so this backend takes both spellings of
        every key it names after its legend: the legend itself and the X11
        keysym a recording writes (`Super_L`, `BackSpace`, `apostrophe`). Case
        is otherwise ignored, because X11::GUITest's own abbreviations were: a
        caller porting a send_keys string should not have to re-case it, and
        `{bac}` and `{BAC}` are the same key.
        """
        name = _key_name(key)
        code = _KEYCODES.get(name)
        if code is None:
            raise CapabilityUnsupported(
                Capability.KEY_EVENT,
                self.name,
                f"{key!r} is not a key this backend knows; the names in "
                "backends.macquartz._KEYCODES and _X11_KEYSYMS are the whole "
                "vocabulary",
            )
        return code

    def resolve_char_key(self, char: str) -> tuple[str, bool]:
        """The (key name, needs_shift) that presses `char`'s own key.

        Overridden because the inherited table is a set of X11 keysym names --
        `bracketleft`, `apostrophe`, `grave` -- while this backend names keys
        after the legend printed on them. The lookup is otherwise the same
        one: US-layout ASCII only, and a ValueError outside it, which is what
        sends a caller to `type_text`.
        """
        if len(char) == 1 and char.isascii() and char.isalpha():
            return char.lower(), char.isupper()
        if len(char) == 1 and char.isascii() and char.isdigit():
            return char, False
        if char in _CHAR_KEYS:
            return _CHAR_KEYS[char]
        raise ValueError(
            f"{char!r} has no static key mapping on {self.name}; "
            "use type_text for arbitrary text"
        )

    def _key_event(self, key, down: bool):
        """A key event with the modifiers held right now stamped onto it."""
        event = self._q.CGEventCreateKeyboardEvent(
            self._source, self._keycode(key), down
        )
        self._q.CGEventSetFlags(event, self._flags())
        return event

    def press_key(self, key):
        """Press a key by name, interpreted on the US layout.

        A modifier is recorded as held *before* its own event is built, so that event
        carries its own flag. That is not a detail of the bookkeeping: measured live,
        posting `Super_L` makes the window server hand every listener a
        `kCGEventFlagsChanged` carrying whatever `CGEventSetFlags` said on the event
        that was posted -- and a flags-changed event says press or release through its
        flag set and nothing else. Posted with no flag, a capturing event tap (and
        pyguitest-recorder's macOS backend is one) reads a Command press as a Command
        *release*, so a replayed `Command+S` captures as "release Command, press s":
        a chord with no press left in it. Hardware arrives with the bit set.
        """
        self.require(Capability.KEY_EVENT)
        name = _key_name(key)
        if name in _MODIFIER_FLAGS:
            self._held.add(name)
        self._post(self._key_event(key, True))

    def release_key(self, key):
        """Release a key by name, resolved the same way `press_key` resolves it."""
        self.require(Capability.KEY_EVENT)
        name = _key_name(key)
        # Dropped first: the release event is the one that must *not* claim
        # the modifier is still down, and a caller who never presses it again
        # (a `press_key("command")` around a click) would otherwise leave
        # every later event shifted.
        self._held.discard(name)
        self._post(self._key_event(key, False))

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Type `text` through `CGEventKeyboardSetUnicodeString`.

        Layout-independent, which is why this does not walk `_KEYCODES` at
        all: every character is posted as a Unicode string on a synthetic
        keycode-0 event, so an accented letter or a `€` arrives as itself
        whatever the machine's keyboard layout is. That is also why
        `allow_keymap_unsafe` is accepted and ignored -- the parameter exists
        for tools that inject scancodes below the compositor, and nothing here
        is interpreted through the live layout in the first place. Warning
        about a risk this backend does not have would be worse than saying
        nothing.

        `delay` is a real sleep between characters rather than a flag on a
        tool's command line, because there is no command line. An application
        that drops events posted in a tight loop is why the argument exists,
        and sleeping is the only way to slow them down from here.

        One character per event pair rather than the whole string in one: a
        single event carrying the text is honoured by some toolkits and
        ignored by others, and per character it is at worst slow. Whether the
        target honours it at all is unmeasured -- see docs/validation.md. The
        length handed to CGEvent is a UTF-16 code-unit count rather than
        `len()`, so an astral character goes out as the pair it is -- see
        `_utf16_units`.
        """
        self.require(Capability.TEXT_ENTRY)
        for index, char in enumerate(text):
            if index and delay:
                time.sleep(delay)
            down = self._q.CGEventCreateKeyboardEvent(self._source, 0, True)
            # Code units rather than characters, which differ outside the
            # BMP -- see _utf16_units.
            self._q.CGEventKeyboardSetUnicodeString(down, _utf16_units(char), char)
            self._q.CGEventSetFlags(down, self._flags())
            self._post(down)
            self._post(self._q.CGEventCreateKeyboardEvent(self._source, 0, False))
