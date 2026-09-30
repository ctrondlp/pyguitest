"""macOS elements: the AX role table, and Accessibility itself.

Two layers in one module, because they are one subject, the way `uia.py` is
two layers. The first is a **table**: every Accessibility role the AX API
publishes, mapped to the role name this package reports for it. Data --
checkable on any machine, and the part that says why a Mac's role vocabulary
is not at-spi's. The second is `MacosBackend`: element search, element
actions, element geometry and hit-testing, window enumeration joined to
`CGWindowListCopyWindowInfo`, and the display list -- the half of macOS
support that needs the Accessibility grant, and therefore the half behind the
`macos` extra.

`atspi.py` and `uia.py` are the same backend on the other two desktops, and
the things to read this against: `Element` here wraps an `AXUIElement` where
those wrap a dogtail Node and an `IUIAutomationElement`, `_build_predicate`
answers the same question the same way, `extents` and `element_at` make the
same decisions about screen coordinates, and `Element.double_click` delegates
to the session for the same reason. The differences are of exactly two kinds
-- AX's vocabulary (roles, subroles and named actions, where UIA has control
types and patterns) and AX's failure mode (every call returns an `AXError`
and leaves the out-parameter unset, rather than raising) -- and where either
shows through, this module says so.

**Measured on macOS 26.7, in a virtual machine, over SSH.** That last part is
not incidental: tccd attributes this process to `sshd-keygen-wrapper`, which
holds the Accessibility row, so every read below reached the console session's
real applications from a shell that has no window server connection of its
own. What that run settled:

- `AXUIElementCreateApplication(pid)` plus `kAXWindowsAttribute` answers for
  other applications' windows, cross-session, with real titles: Terminal's one
  window and TextEdit's four, named `live_doc.txt` and its siblings.
- `CGWindowListCopyWindowInfo` reports the same windows, and **only one of
  fifteen had a non-empty `kCGWindowName`** -- a Window Server entry. Other
  applications' window names are withheld without the Screen Recording grant,
  which is why the join below is not an optimisation: on a Mac with only
  Accessibility granted, AX is the *only* source of a window title.
- The join is one-to-one where it can be: six `kCGWindowLayer == 0` windows,
  six AX windows, and every `layer 0` window had an AX counterpart. It is not
  total in either direction -- the Window Server's own process answers
  `-25204` (`kAXErrorCannotComplete`) for `kAXWindowsAttribute`, and Dock and
  Control Center answer with zero AX windows while listing windows in CG. So
  the window list is CG's *layer-0* entries joined to AX, and the others are
  dropped rather than synthesised.
- A breadth-first walk of Terminal's window cost 17 nodes, depth 4, 0.031 s --
  **1.83 ms per node** over SSH. That is the number `_MAX_NODES` is set
  against, and the reason a search prunes by depth rather than walking a
  browser's whole tree every call.
- `AXUIElementCopyElementAtPosition` answers with the deepest element at a
  point, cross-session, and its geometry can extend *far* outside the screen:
  the text area at (640, 400) reported y = -6639 and height = 7217, because an
  AX text area publishes its document rectangle rather than its viewport. So
  `extents` here is a rectangle to aim at, not a claim about what is visible.
- A read of an attribute an element does not have answers `-25205`
  (`kAXErrorAttributeUnsupported`) -- the ordinary case, and the reason every
  property below has a defensible default rather than raising.

A second live run, with the backend written and driven from that same SSH login,
settled the rest of it and found two bugs no fake could have:

- `windows()` reported exactly the six windows a person could see -- Terminal's
  window and TextEdit's four by real title, plus the Command Line Tools installer
  panel -- and `window_at` at the centre of the front-most one returned that same
  window.
- `root_element()` found seven applications, Dock and Control Center among them.
  That is honest rather than sloppy: they own on-screen windows and their AX trees
  are real, so a search can find their elements.
- A whole-desktop search cost **0.58 s** for 43 push buttons and 0.62 s for five
  text areas, in-process. The 1.83 ms per node above was measured with a round trip
  *over SSH* per call, and this is the same walk without that: `_MAX_NODES` is a
  budget nothing ordinary comes near.
- **`ApplicationServices` publishes no `kAXActionNamesAttribute`.** Reading
  `actions` the way the AX attribute documentation describes raised
  `AttributeError` on every element that had actions to report.
  `AXUIElementCopyActionNames` is the call; every other `kAX`/`kCG` name in this
  module was then checked against the real bindings, and that was the only one
  missing.
- **A window write makes the join stale.** `move_window` succeeded and the
  `resize_window` after it failed with "no AX window matches its handle": the cached
  CoreGraphics rectangle predated the move, so the frame match was looking for the
  window where it no longer was. `_forget_windows` is the fix, and `_close` allows
  the point or two of slack between an AX write landing and the window server
  reporting it.

The grant is the *Accessibility* pane, and what is in it is a row for the
process that launched this one -- `Terminal`, or an IDE, or over SSH the
platform's own `sshd-keygen-wrapper` -- never the interpreter's path. The four
preflights live in `_macapi`; the *requesting* form is
`request_accessibility` below, called only by a caller who named this backend
with `request=True`, which is the `opt_in` rule of ADR 004 §4 spelled as an
option rather than as a registry flag -- see `MacosBackend.__init__`.

What this backend deliberately does **not** serve, and why:

- **`WINDOW_TITLE_SET`, `WINDOW_LOWER`, `WINDOW_CURSOR_QUERY`.** ADR 004 §7's
  permanent refusals: `kAXTitleAttribute` is read-only for a foreign window,
  `kAXRaiseAction` has no counterpart, and the window server's cursor is not
  queryable. They are absent from `capabilities` rather than implemented as
  no-ops, so a caller finds out from `supports()`.
- **`INPUT_SYNC`.** Posting is asynchronous and nothing reports consumption,
  so the answer is `macquartz`'s and not this backend's.
- **`WINDOW_EVENTS`.** `AXObserver` exists and is a later phase, the same
  answer `win32` and `uia` give for their own event models.
- **Input injection and capture.** `macquartz` injects; `screencapture` in
  `capture.py` captures. Neither needs an AX call, and this backend claims no
  capability it would have to implement by calling into another module.
- **The clipboard.** `clipboard.py`'s tool backend already owns
  `pbpaste`/`pbcopy`, and NSPasteboard would be a second implementation of one
  capability.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import time
from typing import TYPE_CHECKING

from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    ElementNotFound,
    PyGUITestError,
    WindowNotFound,
)
from ..roles import Role, spellings
from . import _macapi
from .base import GUIBackend, Screen, Window, check_region, click_by_pointer
from .capture import capture_window_id

if TYPE_CHECKING:
    from .base import Element as _ElementInterface

__all__ = ["Element", "MacosBackend", "available", "request_accessibility"]

_ACCESSIBILITY = "ApplicationServices"
"""The PyObjC distribution the AX calls come from.

`macquartz` uses `Quartz`, and the two are separate requirements of one extra
because they gate different halves -- see `_macapi`'s docstring on why the
preflights are `ctypes` rather than either binding.
"""

_NEEDED = (
    "AXUIElementCreateApplication",
    "AXUIElementCreateSystemWide",
    "AXUIElementCopyAttributeValue",
    "AXUIElementCopyActionNames",
    "AXUIElementCopyElementAtPosition",
    "AXUIElementPerformAction",
    "AXUIElementSetAttributeValue",
    "AXUIElementGetPid",
    "AXValueCreate",
    "AXValueGetValue",
    "AXIsProcessTrusted",
    "AXIsProcessTrustedWithOptions",
)
"""The AX entry points this backend calls, by the names PyObjC publishes.

Checked rather than assumed, and checked against the real binding once: measured on
PyObjC 12.2.2, every `kAX*` constant this module names exists in
`ApplicationServices` **except** `kAXActionNamesAttribute`, which is not published
at all -- which is why `actions` is read through `AXUIElementCopyActionNames`
rather than through a missing constant, and why this tuple is what `available()`
tests rather than a docstring's assumption.
"""

_QUARTZ_NEEDED = (
    "CGWindowListCopyWindowInfo",
    "CGGetActiveDisplayList",
    "CGDisplayBounds",
    "CGDisplayScreenSize",
    "CGDisplayPixelsWide",
    "CGDisplayPixelsHigh",
    "CGDisplayIsMain",
    "CGEventCreate",
    "CGEventGetLocation",
    "CGPointMake",
    "CGSizeMake",
    "kCGWindowListOptionAll",
    "kCGWindowListOptionOnScreenOnly",
    "kCGWindowListExcludeDesktopElements",
    "kCGNullWindowID",
)
"""The CoreGraphics entry points the window join, the display list and the
pointer read need."""

_MAX_ACTIVE_DISPLAYS = 32
"""How many displays to ask `CGGetActiveDisplayList` for.

CoreGraphics has no count-only call, so the count comes back *with* the list:
the function is handed the size of an array to fill and answers how many of it
it used, which means picking a maximum nobody will reach. Thirty-two is beyond
any Mac -- the practical ceiling is a handful of panels plus a Sidecar -- and
an array this size is nothing next to the AX round trips around it. This
constant exists because the alternative, a `CGGetActiveDisplayCount()` to ask
first, is a function that does not exist; see `screens`.
"""


def _import(name: str):
    """The module called `name`, or None where it will not import.

    `importlib` rather than a module-level `import`, because
    `import pyguitest` has to work on Linux and Windows, where neither
    binding exists -- the same rule `_macapi` follows for the same reason. A
    binding that is present but broken (a dyld that refuses) is this function's
    other None, and the caller reports it rather than raising out of an
    import.
    """
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - any failure here is "not available"
        return None


def available() -> bool:
    """Whether PyObjC's ApplicationServices is importable and complete.

    The registry's question, so it must not raise and must not construct
    anything -- `select` asks every backend in priority order on every
    `connect()`. False on every host without the binding, which is every host
    that is not a Mac with the `macos` extra installed, so the factory needs no
    platform test of its own.
    """
    module = _import(_ACCESSIBILITY)
    return module is not None and all(hasattr(module, name) for name in _NEEDED)


def _connection():
    """`(ApplicationServices, Quartz)`, or raise naming what is missing.

    Called from `MacosBackend.__init__`, so a reason raised here reaches a
    caller who named this backend and is folded into "not this session" by
    automatic composition -- see `register`'s docstring in `backends/__init__`.

    Split into its own function so the tests replace one seam: a fake
    `ApplicationServices` and `Quartz` in `sys.modules` are what `_import`
    finds, which is how `tests/test_macos_backend.py` drives every AX call
    without a Mac.
    """
    accessibility = _import(_ACCESSIBILITY)
    if accessibility is None:
        raise BackendUnavailable(
            f"{_ACCESSIBILITY} is not importable, so Accessibility is "
            "unreachable; it comes from the 'macos' extra "
            "(pip install 'pyguitest[macos]')"
        )
    missing = [name for name in _NEEDED if not hasattr(accessibility, name)]
    if missing:
        raise BackendUnavailable(
            f"{_ACCESSIBILITY} has no {', '.join(missing)}; the binding is "
            "older than this backend needs"
        )
    quartz = _import("Quartz")
    if quartz is None:
        raise BackendUnavailable(
            "Quartz is not importable, so the window join and the display "
            "list are unavailable; they come from the 'macos' extra "
            "(pip install 'pyguitest[macos]')"
        )
    missing = [name for name in _QUARTZ_NEEDED if not hasattr(quartz, name)]
    if missing:
        raise BackendUnavailable(
            f"Quartz has no {', '.join(missing)}; the binding is older than "
            "this backend needs"
        )
    return accessibility, quartz


def request_accessibility() -> bool:
    """Ask for the Accessibility grant, and report what it is now.

    The *prompting* form, `AXIsProcessTrustedWithOptions` with
    `kAXTrustedCheckOptionPrompt` set -- the one API here that can put a
    dialog on screen, which is why it is a named call rather than something
    a constructor does (see `MacosBackend.__init__` and ADR 004 §4).

    Returns the status at the moment of the call, which on the SSH login a
    live run was measured from is `False` either way: tccd attributes this
    process to `sshd-keygen-wrapper`, whose prompt policy is "system set", so
    no dialog can appear and asking grants nothing. The grant is a row
    switched on by hand in System Settings > Privacy & Security >
    Accessibility, under the name of the process that launched this one.
    docs/validation.md records both the measurement and the row.
    """
    accessibility = _import(_ACCESSIBILITY)
    if accessibility is None:
        return False
    return _prompt(accessibility)


def _prompt(accessibility) -> bool:
    """`AXIsProcessTrustedWithOptions` with the prompt option, or False."""
    options = accessibility.kAXTrustedCheckOptionPrompt
    try:
        return bool(accessibility.AXIsProcessTrustedWithOptions({options: True}))
    except Exception:  # noqa: BLE001 - a dialog that will not open
        return False


_UNKNOWN_ROLE = "unknown"
"""The role for an element whose `kAXRoleAttribute` cannot be read at all.

Not for an AX role this table has never heard of -- those fall through to
`_camel_words`, because AX names a role for everything it publishes and
mapping an unknown one onto `unknown` would claim the element never answered.
This is the other case: a dead element, or a process that would not be
interviewed, where `unknown` is exactly what happened.
"""

# -- the AX vocabulary -----------------------------------------------------
#
# Keyed by the role *names* AX publishes, which is what `kAXRoleAttribute`
# returns as a string ("AXButton"), rather than by the `kAX*Role` constants --
# the constants are the same strings under a symbol, and a reader checking one
# against Apple's documentation has nothing to translate either way. Only the
# roles this backend has a use for are here; anything absent falls through to
# `_camel_words` instead of being guessed at.
#
# The values are at-spi role names, deliberately: `roles.py` says the values
# are the strings AT-SPI uses, and a script written on Linux
# (`gui.button("Save")`, `gui.find_element(role=Role.PUSH_BUTTON)`) has to
# keep working on a Mac. Where the two vocabularies do not line up, the
# comment says which side gave way and why -- the four entries uia.py handles
# the same way are the precedent.

_AX_ROLES = {
    # -- windows and containers --------------------------------------------
    "AXApplication": "application",
    "AXWindow": Role.WINDOW,
    "AXSheet": Role.DIALOG,
    "AXDialog": Role.DIALOG,
    "AXSystemDialog": Role.DIALOG,
    "AXDrawer": Role.PANEL,
    "AXGroup": Role.PANEL,
    # at-spi has `split pane` and `roles.py` names no constant for it, the
    # same situation uia.py's "_ATSPI_NAMES_WITHOUT_CONSTANTS" describes.
    "AXSplitGroup": "split pane",
    "AXScrollArea": Role.SCROLL_PANE,
    "AXToolbar": Role.TOOL_BAR,
    "AXTabGroup": Role.PAGE_TAB_LIST,
    "AXList": Role.LIST,
    "AXOutline": Role.TREE,
    "AXTable": Role.TABLE,
    # A column view is a hierarchy navigated one level at a time, so `tree`
    # rather than `table`: a caller asking for a table is looking for cells in
    # rows, and a browser view reports neither.
    "AXBrowser": Role.TREE,
    "AXRow": Role.TABLE_ROW,
    "AXColumn": Role.TABLE_COLUMN_HEADER,
    # -- things you press --------------------------------------------------
    "AXButton": Role.PUSH_BUTTON,
    # GtkMenuButton reports `push button` on Linux: it is a control that opens
    # something when pressed, which is what AXMenuButton is too.
    "AXMenuButton": Role.PUSH_BUTTON,
    "AXDisclosureTriangle": Role.TOGGLE_BUTTON,
    "AXCheckBox": Role.CHECK_BOX,
    "AXRadioButton": Role.RADIO_BUTTON,
    "AXLink": Role.LINK,
    "AXPopUpButton": Role.COMBO_BOX,
    "AXComboBox": Role.COMBO_BOX,
    "AXIncrementor": Role.SPIN_BUTTON,
    # -- things you type into ----------------------------------------------
    "AXTextField": Role.ENTRY,
    "AXSearchField": Role.ENTRY,
    "AXTextArea": Role.TEXT,
    "AXSecureTextField": Role.PASSWORD_TEXT,
    # -- menus -------------------------------------------------------------
    "AXMenuBar": "menu bar",
    # A menu bar item is what a menu hangs from, so `menu item` rather than
    # `menu bar`: the bar itself is the element above it, and at-spi names no
    # role for the entry hanging from one.
    "AXMenuBarItem": Role.MENU_ITEM,
    "AXMenu": Role.MENU,
    "AXMenuItem": Role.MENU_ITEM,
    # -- things you only read ----------------------------------------------
    "AXStaticText": Role.LABEL,
    "AXImage": Role.IMAGE,
    "AXSlider": Role.SLIDER,
    "AXScrollBar": Role.SCROLL_BAR,
    # The draggable part of a scroll bar, and deliberately not `scroll bar`
    # for the reason uia.py gives about UIA's thumb: it is a *child* of the
    # bar, so calling it one makes a scroll-bar search report two elements per
    # bar on a Mac and one on Linux. Measured: Terminal's scroll area walks as
    # AXScrollArea -> AXScrollBar and AXValueIndicator, one each.
    "AXValueIndicator": "thumb",
    # A tooltip. `help tag` is AX's private word for it, and what the fallback
    # below spelled it; at-spi and UIA both say `tool tip`, so a script written
    # against either found nothing here. Measured on macOS 26.7: hovering
    # TextEdit's Bold checkbox, whose AXHelp is "Bold text", added exactly one
    # element to the application's tree, an AXHelpTag.
    "AXHelpTag": "tool tip",
    "AXProgressIndicator": Role.PROGRESS_BAR,
    "AXSeparator": Role.SEPARATOR,
    "AXSplitter": Role.SEPARATOR,
    # AXUnknown is AX's own "this element publishes no role", which is what
    # `unknown` means here.
    "AXUnknown": _UNKNOWN_ROLE,
}
"""AX role name -> the role string this package reports for it."""

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
"""Where to break an AX role name so "AXColorWell" reads as words."""


def _camel_words(name: str) -> str:
    """`"AXColorWell"` -> `"color well"`, the honest name for an unmapped role.

    AX camel-cases its role names, so the words are in the string and only the
    spelling of the gaps has to change. This is what uia.py does with a
    control type at-spi has no counterpart for, and for the same reason: the
    platform's own name, lower-cased into the style of the others, is a better
    answer than `unknown`, which would claim the element never answered.
    """
    stem = name[2:] if name.startswith("AX") else name
    return " ".join(_CAMEL.split(stem)).lower()


def _ax_role(name: str) -> str:
    """The role this package reports for an AX role name.

    A name this table does not know is spelled out into words rather than
    reported as `unknown` -- see `_camel_words`. An empty or missing role is
    the other case: nothing answered at all.
    """
    if not name:
        return _UNKNOWN_ROLE
    return _AX_ROLES.get(name) or _camel_words(name)


_AX_ACTIONS = {
    "AXPress": "click",
    "AXShowMenu": "show menu",
    "AXPick": "pick",
    "AXConfirm": "confirm",
    "AXCancel": "cancel",
    "AXOpen": "open",
    "AXIncrement": "increment",
    "AXDecrement": "decrement",
    "AXRaise": "raise",
    "AXToggle": "toggle",
    "AXDelete": "delete",
    "AXShowAlternateUI": "show alternate ui",
    "AXShowDefaultUI": "show default ui",
    "AXScrollToVisible": "scroll to visible",
}
"""AX action name -> the name this package publishes for it.

Not AX's names. `actions` is what a caller prints, checks and passes back to
`do_action`, and a script written against `atspi` says `click` -- so `AXPress`
is published as `click`, which is the same reasoning uia.py's `_ACTIONS` table
gives for naming UIA patterns after at-spi's actions. An action AX publishes
and this table does not know is spelled out through `_camel_words` instead,
like an unknown role.
"""

_ACTION_ALIASES = {
    "press": "AXPress",
    "activate": "AXPress",
    "click": "AXPress",
    "menu": "AXShowMenu",
    "open": "AXOpen",
    "pick": "AXPick",
    "confirm": "AXConfirm",
    "cancel": "AXCancel",
    "raise": "AXRaise",
    "toggle": "AXToggle",
    "delete": "AXDelete",
}
"""What a caller calls an action -> the AX action that means the same thing.

`activate` is at-spi's word for what AX calls a press, and `click` is the one
scripts actually type -- uia.py accepts the same two spellings for the same
reason. Everything else here is published under its own name already and is
listed so that a caller who learned the *AX* spelling is not told an action
does not exist.
"""

_AX_ACTION_NAMES = {published: name for name, published in _AX_ACTIONS.items()}
"""The reverse of `_AX_ACTIONS`, for resolving a published name back."""


def _ax_action(name: str) -> str | None:
    """The AX action name for `name`, or None where nothing here names one.

    Three spellings are accepted, in this order: an at-spi alias, a name this
    backend publishes, and a raw `AX...` name a caller read off `AXActionNames`
    themselves. None means "not an action this backend performs" rather than
    "this element does not have it" -- `Element.do_action` tells those apart,
    the second answer coming from the element's own list.
    """
    if name in _ACTION_ALIASES:
        return _ACTION_ALIASES[name]
    if name in _AX_ACTION_NAMES:
        return _AX_ACTION_NAMES[name]
    if name.startswith("AX"):
        return name
    return None


def _matches_text(value: str | None, wanted) -> bool:
    """Whether `value` satisfies `wanted`.

    Exact match for a plain string, `.search()` for a compiled pattern --
    mirrors `atspi._matches_text`, because a `name` filter has to mean the
    same thing on every desktop.
    """
    if isinstance(wanted, re.Pattern):
        return wanted.search(value or "") is not None
    return value == wanted


def _text_of(value) -> str:
    """One attribute's value as a string, with anything else read as none.

    AX attributes are `CFTypeRef`s, so an application *can* publish a title or a
    description as a number -- nothing in the binding stops it -- and this
    package's own `_text` already refuses to treat one as text. `Element.name`
    and `Element.description` are documented as strings and are what a search's
    filters are compared against, so the same refusal belongs here: a non-string
    value would otherwise reach `_matches_text` and turn a `find_element(name=
    re.compile(...))` against a lying tree into a `TypeError`.
    """
    return value if isinstance(value, str) else ""


def _ax_roles(role: str | None):
    """Every AX role name this package reports as `role`, or None for no filter.

    The comparison is against `spellings(role)` rather than against `role`,
    which is what keeps `roles.py`'s alias table working here: a script that
    asks for `button` has to match a Mac that publishes `AXButton` -> `push
    button`, exactly as it matches an at-spi2 that renamed the role.

    A raw AX name is accepted as itself, so a caller who read `AXTextField`
    off a live tree can search for it. Where nothing maps, `{role}` is
    returned rather than an empty set: an empty set would silently match
    nothing, and a search for a role this backend never produces should come
    back empty because no element has it, not because the filter was dropped.
    """
    if role is None:
        return None
    wanted = spellings(role)
    named = {name for name, reported in _AX_ROLES.items() if reported in wanted}
    if role.startswith("AX"):
        named.add(role)
    return named or {role}


_MAX_DEPTH = 24
"""How far up or down the tree a walk will go.

The same ceiling `uia.py` uses, and for the same reason: a provider that
reports a cycle cannot be allowed to spin a walk, and 24 levels of containment
is deeper than any live tree measured here (Terminal's window came out four
levels deep).
"""

_MAX_NODES = 20000
"""How many elements one search will visit before it gives up and answers.

AX has no search: `AXUIElementCopyAttributeValue` reads one attribute of one
element, so finding anything means walking. Measured on the live machine, that
walk costs **1.83 ms per node** over SSH -- which makes 20 000 nodes about
thirty-seven seconds, and makes the ceiling the difference between a search that
is slow and one that never returns. `uia.py` needs no equivalent because UIA
answers a query on the provider's side; this is what it costs to do the same job
with an attribute API, and the number is a budget rather than a limit anybody
should reach: a browser's tree is the case that would.
"""


class Element:
    """One node of the Accessibility tree.

    Thin wrapper over an `AXUIElement`, exposing the subset the Capability
    interface promises so callers are not coupled to PyObjC; `node` stays
    reachable for anything this does not cover, exactly as `atspi.Element` keeps
    its dogtail Node reachable and `uia.Element` keeps its COM pointer.

    Unlike `atspi.Element`, the backend is not optional. An `AXUIElement` is an
    opaque reference with no methods of its own, so the attribute reads, the
    action calls and the geometry all go back through the object that knows how
    to make them.
    """

    __slots__ = (
        "node",
        "_backend",
        "_session",
        "_attributes",
        "_unsupported",
        "_actions",
    )

    def __init__(self, node, backend, session=None):
        """Wrap one element, the backend that found it, and the session above it.

        `session` is set by Session as it hands an element out, never by a
        backend: an AX reference knows nothing of the session above it, and
        double_click needs one, since the pointer is the session's. Elements
        made while walking the tree inherit it from the one they came from; see
        parent, children and find.
        """
        self.node = node
        self._backend = backend
        self._session = session
        self._attributes = {}
        self._unsupported = set()
        self._actions = None

    @property
    def _ax(self):
        """The ApplicationServices module, which is where the AX names live.

        The constants are read off the module rather than transcribed, the same
        way `macquartz` reads `kCG*`: the Python name is its own documentation,
        and a fake module can be complete without duplicating a table of
        strings.
        """
        return self._backend._ax

    def _read(self, attribute, default=None):
        """One identifying attribute, memoized on this element.

        Two reasons. Cost: every read here is a cross-process round trip,
        measured at 1.83 ms per node on a live Mac, and a single search asks for
        an element's role twice (`_build_predicate` asks, and `__repr__` asks
        again when something is reported). Correctness: an attribute that
        *identifies* an element cannot change while the element is the same
        element, so remembering it is exact rather than an approximation. State
        goes through `_state` instead, which never remembers.

        An attribute an element does not publish is remembered as such, so the
        second question about it costs nothing -- measured: `-25205`
        (`kAXErrorAttributeUnsupported`) is the ordinary answer for the ordinary
        case, and it is not going to change while the element is alive.

        Keyed by `str(attribute)` rather than by the attribute itself, which matters
        because the key is what the cache is worth: a binding that handed back a fresh
        object for every `kAXRoleAttribute` would miss on every read and quietly turn
        this into no cache at all. Measured while testing: a fake that did exactly that
        made the cache invisible, and the fix is to key on the name -- a `CFString`'s
        `str()` is its text.
        """
        key = str(attribute)
        if key in self._unsupported:
            return default
        if key in self._attributes:
            return self._attributes[key]
        err, value = self._backend._copy(self.node, attribute)
        if err or value is None:
            self._unsupported.add(key)
            return default
        self._attributes[key] = value
        return value

    def _state(self, attribute, default=None):
        """A *state* attribute, read fresh every time.

        Deliberately not memoized: `checked`, `selected`, `expanded` and
        `focused` are the ones a script acts on and then reads back, and a
        remembered answer would make the second read describe the world before
        its own click. That is the split `uia.Element._pattern` draws between a
        *lookup* (cached) and a state read (not), arrived at here from the other
        direction -- AX has no pattern objects to look up, so the line falls
        between attributes instead.
        """
        err, value = self._backend._copy(self.node, attribute)
        return default if err or value is None else value

    @property
    def name(self):
        """The element's accessible name, such as a button's label.

        `kAXTitleAttribute` when it answers, `kAXDescriptionAttribute` when it
        does not. That order is not arbitrary: a control drawn as an image has
        no title and publishes its label as a description, which is then the
        only name it has. Measured on a live Mac, Terminal's window answers the
        title attribute and TextEdit's four windows answer with their file
        names.

        Always a string: AX will hand back whatever type an application put
        there, and `_text_of` is where a title that is not text stops being a
        name rather than becoming a `TypeError` inside a search.
        """
        ax = self._ax
        return _text_of(
            self._read(ax.kAXTitleAttribute, "")
            or self._read(ax.kAXDescriptionAttribute, "")
        )

    @property
    def role(self):
        """The element's accessible role, such as 'push button'.

        Translated from AX's role through the table at the top of this module,
        which is why the answer is an at-spi role name rather than AX's, and
        `unknown` only where nothing could be read at all.
        """
        return _ax_role(self._read(self._ax.kAXRoleAttribute, ""))

    @property
    def parent(self):
        """The containing element, or None at the root."""
        parent = self._backend._parent(self.node)
        if parent is None:
            return None
        return Element(parent, self._backend, self._session)

    @property
    def children(self):
        """The elements directly inside this one, in AX's own order."""
        return self._backend._children(self)

    @property
    def visible(self):
        """Whether the element is currently showing.

        A Mac publishes no `showing` state, so this is the three things that
        can be known instead: the element reports a rectangle, it is not
        `kAXHiddenAttribute`, and the window it lives in is in the window
        server's on-screen list. What it cannot tell you is whether a
        *scrolling* ancestor has moved the element out of view -- AX reports
        geometry for rows a list has scrolled past, where UIA and at-spi both
        answer offscreen. Measured on a live Mac from the other side: an
        AXTextArea at (640, 400) reported its whole document rectangle,
        y = -6639 and height = 7217.

        A join that cannot decide -- a window missing from the CG list, an
        element with no window above it -- answers visible, the same leniency
        `enabled` shows and `uia.Element.visible` gives for the same reason: a
        caller about to act on the element is better served by the optimistic
        answer than by a false "it is hidden".
        """
        if bool(self._state(self._ax.kAXHiddenAttribute, False)):
            return False
        if self._backend._frame(self.node) is None:
            return False
        showing = self._backend._window_showing(self.node)
        return True if showing is None else showing

    @property
    def enabled(self):
        """Whether the element accepts input, rather than being greyed out."""
        return bool(self._state(self._ax.kAXEnabledAttribute, True))

    @property
    def description(self):
        """The element's longer accessible description, often a tooltip.

        `kAXHelpAttribute` first, which is the tooltip, then
        `kAXDescriptionAttribute` -- the same property `name` falls back to.
        The overlap is AX's rather than this module's: a Mac publishes one
        string for "what this control is for", and at-spi asks the question
        twice. A string or nothing, for `name`'s reason.
        """
        ax = self._ax
        return _text_of(
            self._read(ax.kAXHelpAttribute, "")
            or self._read(ax.kAXDescriptionAttribute, "")
        )

    @property
    def text(self):
        """The element's text content, for text boxes and labels.

        `kAXValueAttribute` when it holds a string: the field's own contents for
        an entry, the whole document for a text area, and the visible string for
        a static text. That last one is why a label answers here, where an
        at-spi label's `text` is None -- `base.Element.text`'s own docstring asks
        for "text boxes and labels", and on a Mac the label's string is in the
        one attribute that holds text.
        """
        value = self._read(self._ax.kAXValueAttribute, None)
        return value if isinstance(value, str) else None

    @property
    def value(self):
        """The numeric value of a slider, spinner, or progress bar, or None.

        The same attribute as `text`, read the other way: AX puts a number there
        for a slider and a progress indicator, and a string for anything holding
        text, so which one a caller gets is decided by what the element
        published. A bool is excluded even though Python counts it as an int --
        a check box's 0/1 is `checked`'s answer, not a number to compare against
        a slider's.
        """
        raw = self._read(self._ax.kAXValueAttribute, None)
        if isinstance(raw, (bool, str)):
            return None
        try:
            return None if raw is None else float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def checked(self):
        """Whether a check box, radio button, or toggle is set.

        Two attributes, because AX splits what at-spi calls one state: a check
        box, radio button or switch publishes `kAXValueAttribute` as 0/1, and a
        menu item publishes `kAXMenuItemMarkCharAttribute` -- the tick
        character, empty when unset. None where neither answers, which is what
        "this element has nothing to be checked" looks like; read `checkable`
        first, as the interface asks.
        """
        value = self._state(self._ax.kAXValueAttribute, None)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        mark = self._state(self._ax.kAXMenuItemMarkCharAttribute, None)
        return bool(mark) if isinstance(mark, str) else None

    @property
    def checkable(self):
        """Whether the element has a check box, radio button, or toggle.

        Two sources rather than one, which is this platform's shape: UIA answers
        "does it publish Toggle" through a pattern lookup, where AX publishes no
        such thing. The role answers for the widgets, and the mark attribute
        answers for a menu item -- whose role is `menu item` whether or not it
        can carry a tick.
        """
        if self.role in (Role.CHECK_BOX, Role.RADIO_BUTTON, Role.TOGGLE_BUTTON):
            return True
        mark = self._state(self._ax.kAXMenuItemMarkCharAttribute, None)
        return isinstance(mark, str)

    @property
    def selected(self):
        """Whether a list item, tab, or menu item is currently selected.

        `kAXSelectedAttribute`, None where the element does not publish it,
        which is the ordinary answer for a button. A table or outline holding
        several selections reports them through `kAXSelectedRowsAttribute`
        instead, and reading *that* off a row is unmeasured -- see
        docs/validation.md.
        """
        value = self._state(self._ax.kAXSelectedAttribute, None)
        return None if value is None else bool(value)

    @property
    def selectable(self):
        """Whether the element can be a list item, tab, or menu selection.

        "The attribute is published" is the question, rather than the role,
        because it is the same signal `select()` acts on: AX accepts a write to
        `kAXSelectedAttribute` on exactly the elements that publish it.
        """
        return self._state(self._ax.kAXSelectedAttribute, None) is not None

    @property
    def expanded(self):
        """Whether a tree item or similar disclosure control is open.

        `kAXExpandedAttribute`, None where it is not published. Two cases are
        worth telling apart and this is the platform's own way of telling them:
        a disclosure triangle answers here directly, while an outline row only
        answers where the application propagates the attribute -- so `expanded`
        answering None is a real answer about a widget that publishes no
        disclosure state, and why the interface asks a caller to read
        `expandable` first.
        """
        value = self._state(self._ax.kAXExpandedAttribute, None)
        return None if value is None else bool(value)

    @property
    def expandable(self):
        """Whether the element can be expanded or collapsed, like a tree item.

        The same signal as `selectable`, for the same reason: AX answers by
        publishing `kAXExpandedAttribute`, and `expand` is a write to it.
        Whether a *particular* write is accepted is the element's own business
        -- `expand` reports a refusal rather than pretending it happened.
        """
        return self._state(self._ax.kAXExpandedAttribute, None) is not None

    @property
    def focused(self):
        """Whether the element currently has keyboard focus."""
        return bool(self._state(self._ax.kAXFocusedAttribute, False))

    @property
    def actions(self):
        """The names of the actions this element offers, e.g. 'click'.

        `AXUIElementCopyActionNames`, translated to what this package calls each
        action and *not* reordered: AX lists them in the order the element prefers
        them, and a press is the one a caller reaches for. An action this module has
        no name for is spelled out through `_camel_words`, so a live tree never
        reports an action as nothing at all.

        The function rather than an attribute read, and that is a measurement rather
        than a preference: PyObjC 12.2.2 publishes no `kAXActionNamesAttribute` at
        all, so the attribute route raises `AttributeError` on the element that has
        actions to report -- which is how the first live run of this backend found
        it. Every other constant this module names was present.
        """
        return [
            _AX_ACTIONS.get(name) or _camel_words(name) for name in self._raw_actions()
        ]

    def _raw_actions(self):
        """AX's own action names, memoized -- they cannot change while alive."""
        if self._actions is None:
            self._actions = self._backend._action_names(self.node)
        return self._actions

    @property
    def pid(self):
        """The process this element belongs to.

        `AXUIElementGetPid`, which unlike at-spi's `pid` is always available: an
        AX element is addressed by process in the first place.
        """
        return self._backend._pid(self.node)

    @property
    def alive(self):
        """Whether the underlying widget still exists.

        A role read that succeeds, which is the cheapest question AX answers
        about an element. Measured: a dead element answers `-25202`
        (`kAXErrorInvalidUIElement`), and a process that will not be
        interviewed answers `-25204` (`kAXErrorCannotComplete`) -- the second is
        reported as not alive too, since nothing can be read through it either,
        and a caller holding a stale widget is better told so than told
        nothing.
        """
        err, _value = self._backend._copy(self.node, self._ax.kAXRoleAttribute)
        return err == 0

    def _press(self):
        """Perform `AXPress` where the element publishes it.

        False where it does not, and a raise where it does and AX refused --
        the distinction `click` is built on, since "there is no press to give"
        is a reason to try the toggle route and "the press was refused" is not.
        """
        if "AXPress" not in self._raw_actions():
            return False
        self._backend._perform(self.node, "AXPress")
        return True

    def _write(self, attribute, value, what):
        """Set an attribute, or raise a typed refusal naming what was attempted.

        `AXUIElementSetAttributeValue`, and the AXError it returns is the
        interesting part: `-25205` is "this element has no such attribute" and
        `-25204` is "the process would not answer". Both arrive as the typed
        error the other backends use, with the number in the message -- which is
        the half `CapabilityUnsupported` cannot know on its own.
        """
        err = self._backend._set(self.node, attribute, value)
        if err:
            raise CapabilityUnsupported(
                Capability.ELEMENT_ACTION,
                self._backend.name,
                f"could not {what}: AX answered {err}",
            )

    def click(self):
        """Act on the element directly -- no coordinates, no injection.

        Three routes, in the order AX offers them: `AXPress`, which is a
        button's own "I have been pressed"; a write to `kAXValueAttribute` on a
        check box, radio button or switch, which is the toggle at-spi performs
        for the same roles; and `kAXSelectedAttribute` on a row, tab or menu
        item, which is `select()` spelled as a click.

        A refusal from the route that applies is *not* fallen through -- the
        choice `uia.Element.click` makes, for the reason it gives there: an
        element can publish a press and be disabled, and turning "this button is
        disabled" into a click reported as successful is the one failure this
        package will not trade for a convenience.

        An element that publishes none of the three is clicked by coordinate
        instead, which is the step the refusal used to only advise: the element
        stays the locator, its rectangle is read fresh, and the pointer goes to
        the centre of it. That is `click_by_pointer`, the same delegation
        `double_click` makes below -- and it is what a Mac needs for the widgets
        AX leaves actionless, a static text or a cell in a table. A *refusal* is
        still not one of those cases: this is for an element that published
        nothing, never for one that refused, so the rule above is untouched.

        An element that offers none of them and cannot be aimed at either --
        no session behind it, so no pointer to reach, or no rectangle --
        raises `ElementNotActionable`, the same typed answer the other two
        backends give, with this platform's reason in it rather than theirs.
        """
        if self._press():
            return
        if self.checkable:
            self._write(
                self._ax.kAXValueAttribute, not bool(self.checked), f"toggle {self!r}"
            )
            return
        if self.selectable:
            self.select()
            return
        click_by_pointer(
            self,
            "Accessibility offers it no press action and it publishes nothing "
            "to toggle or select, so there is no accessible action to perform",
        )

    def double_click(self):
        """Double-click the element: locate it, then inject the gesture.

        There is no accessible action to name here, the way `click` names one: a
        double-click is a gesture on the pointer, and AX publishes actions for
        widgets rather than gestures. So the element stays the locator and the
        gesture falls back to the pointer, where this package's real
        double_click lives -- on the Session, which is the only thing holding a
        backend able to move one. Hence the delegation below, and hence
        `_session`.

        Needs `Capability.ELEMENT_GEOMETRY` on top of what `Session.double_click`
        needs, since the rectangle has to be read to find the point, and the
        pointer capabilities on top of that -- which come from `macquartz` on
        this platform, not from this backend.
        """
        if self._session is None:
            raise PyGUITestError(
                f"{self.name!r} carries no session to double-click through -- "
                "it came from a backend directly rather than from a Session. "
                "Use gui.double_click_element(element), or take the element "
                "from the session instead: gui.button(...), gui.element(...), "
                "gui.root_element()"
            )
        self._session.double_click_element(self)

    def _bind_session(self, session):
        """Record which Session handed this element out.

        Named rather than left as a plain attribute, so that Session can offer
        it to any element type without knowing which backend built it: an
        element with no such method simply never gets a session, and
        double_click is the only thing that notices. Elements made while walking
        the tree pass on whatever their source carried, which is what keeps
        `gui.root_element().child(...)` able to double_click.
        """
        self._session = session

    def focus(self):
        """Give the element keyboard focus.

        `kAXFocusedAttribute` written True, which AX accepts on anything that
        can take focus and answers with an error on anything that cannot -- a
        typed refusal naming what was attempted, like `uia.Element.focus`.
        """
        self._write(self._ax.kAXFocusedAttribute, True, f"focus {self!r}")

    def set_text(self, text):
        """Replace the element's text content, through `kAXValueAttribute`.

        Raises `CapabilityUnsupported` where the write is refused -- a read-only
        label, a document, a control that merely looks like a field -- because
        "there is no way in" is the useful part of that answer, where a bare
        AXError would leave the reader to work out which of the two it meant.
        """
        self._write(self._ax.kAXValueAttribute, text, f"replace the text of {self!r}")

    def do_action(self, name):
        """Perform a named accessible action, such as "click" or "activate".

        The names are what `actions` publishes, plus the AX spelling of the same
        action and at-spi's `activate`/`press` for a click, which is the
        leniency `uia.Element.do_action` shows and for the same reason: a script
        written against the Linux backend has to keep working here. An unknown
        name raises ValueError listing what this element does offer, which is
        what `press_key` does with an unknown key and is far more useful than
        doing nothing. A real action this element does not publish raises
        `ElementNotActionable`, so a caller can tell "typo" from "this widget
        cannot".
        """
        action = _ax_action(name)
        if action is None:
            raise ValueError(
                f"{name!r} is not an action this backend performs; {self.role} "
                f"{self.name!r} offers {', '.join(self.actions) or 'none'}"
            )
        if action not in self._raw_actions():
            raise ElementNotActionable(
                self.role,
                self.name,
                f"it publishes no {action} action; it offers "
                f"{', '.join(self.actions) or 'no actions at all'}",
            )
        self._backend._perform(self.node, action)

    def select(self):
        """Select this element, for a list item, tab, or menu entry.

        A typed refusal rather than a silent no-op where the element publishes
        no `kAXSelectedAttribute`, for the same reason `set_text` refuses: a
        caller who asked for a selection and got nothing has no way to tell that
        apart from a selection that happened.
        """
        if not self.selectable:
            raise CapabilityUnsupported(
                Capability.ELEMENT_ACTION,
                self._backend.name,
                f"{self!r} publishes no selection, so it cannot be selected",
            )
        self._write(self._ax.kAXSelectedAttribute, True, f"select {self!r}")

    def expand(self):
        """Open this tree item or similar disclosure control.

        A no-op where `expanded` already reads True: AX's disclosure state is
        written rather than toggled, so the check is about the two backends
        agreeing on this idempotence rather than about a write that would flip.
        `uia.Element.expand` keeps the same check for the other reason that
        applies to a provider with a real Expand method.
        """
        if self.expanded is True:
            return
        if self.expandable:
            self._write(self._ax.kAXExpandedAttribute, True, f"expand {self!r}")
            return
        raise CapabilityUnsupported(
            Capability.ELEMENT_ACTION,
            self._backend.name,
            f"{self!r} publishes no kAXExpandedAttribute, so it cannot be expanded",
        )

    def collapse(self):
        """Close this tree item or similar disclosure control. See `expand`."""
        if self.expanded is False:
            return
        if self.expandable:
            self._write(self._ax.kAXExpandedAttribute, False, f"collapse {self!r}")
            return
        raise CapabilityUnsupported(
            Capability.ELEMENT_ACTION,
            self._backend.name,
            f"{self!r} publishes no kAXExpandedAttribute, so it cannot be collapsed",
        )

    def choose(self, option):
        """Pick `option` from this dropdown by its visible text.

        AX has no value setter for a popup button the way dogtail's `combovalue`
        is on Linux, so this is the two real steps: press the control, because a
        popup's list usually does not exist in the tree until it opens, then find
        the item and select or click it. The press is allowed to fail silently --
        a popup button that is already open treats a second press as the close it
        looks like -- which is the one place a refusal is deliberately not
        propagated, because the alternative is a script that cannot choose
        anything at all.

        The item is looked for among this element's own descendants and then
        under the desktop root, in that order, because AX puts it in different
        places depending on the application: a native popup keeps its menu inside
        the control's own tree, and a menu opened by a press is a sibling of the
        whole window's subtree in some applications. Whatever is found is
        selected where it publishes a selection, and clicked where it only
        offers a press -- which a menu entry usually does. Raises ElementNotFound
        naming the option when neither place has it.
        """
        with contextlib.suppress(CapabilityUnsupported):
            self._press()
        for container in (self, self._backend.root_element()):
            for role in (Role.MENU_ITEM, Role.LIST_ITEM):
                candidate = container.child(role=role, name=option)
                if candidate is None:
                    continue
                if candidate.selectable:
                    candidate.select()
                else:
                    candidate.click()
                return
        raise ElementNotFound(
            f"no list item or menu item named {option!r} is in {self!r} or "
            "anywhere on the desktop"
        )

    def options(self):
        """The choices this dropdown or list offers, as Elements.

        Menu items before list items, matching `atspi.Element.options` -- and an
        empty list is a truthful answer about a popup whose menu has not been
        opened, not a failure.
        """
        found = self.find(role=Role.MENU_ITEM)
        return found or self.find(role=Role.LIST_ITEM)

    def find(self, role=None, name=None):
        """Search this element's descendants by role and/or name.

        Wrapped again here rather than handed back as the backend made them,
        because a backend element carries no session: this passes its own on, so
        `gui.root_element().child(...).double_click()` reaches the pointer the way
        an element the session found does. `atspi.Element.find` and
        `uia.Element.find` do the same thing with the same reason.
        """
        found = self._backend.find_elements(role=role, name=name, within=self)
        return [Element(node.node, self._backend, self._session) for node in found]

    def child(self, role=None, name=None):
        """Return the first descendant matching role and/or name, or None."""
        matches = self.find(role=role, name=name)
        return matches[0] if matches else None

    def is_ancestor_of(self, other):
        """Whether `other` sits somewhere inside this element.

        Walked upwards comparing elements at each step rather than descending:
        the question is about one path, and descending would visit the whole
        tree to answer it. Bounded by `_MAX_DEPTH`, so a tree that reports a
        cycle cannot make this spin. The comparison is CoreFoundation's own
        equality through `_same`, because two references to one element are
        different Python objects as often as not.
        False for the synthetic desktop root, which has no `node` to walk up
        to: nothing here is its ancestor, since it has no AX parent at all.
        """
        if isinstance(other, _Desktop):
            return False
        node = other.node
        for _ in range(_MAX_DEPTH):
            node = self._backend._parent(node)
            if node is None:
                return False
            if self._backend._same(node, self.node):
                return True
        return False

    def __repr__(self):
        """The role and name, which is how an element is written in a script."""
        return f"Element({self.role!r}, {self.name!r})"


_DESKTOP_ROLE = "desktop frame"
"""The role the synthetic root reports.

A real at-spi role name with no `Role` constant beside it, the same situation
`uia.py` describes in `_ATSPI_NAMES_WITHOUT_CONSTANTS`: nothing in this package
could produce it until a Mac needed a root to search from.
"""


def _scale(quartz, display, pixels, points) -> float:
    """DPI divided by 96 for one display, which is what `base.Screen` documents.

    From the display's physical size, and deliberately *not* from the backing
    scale factor: a Retina Mac's factor of 2.0 says nothing about DPI, where
    `scale` is a stated convention of this interface. A display that reports no
    physical size -- a virtual one, which is what the live machine has -- falls
    back to the pixel-per-point ratio: 1.0 at 1x, and the honest 2.0 at 2x, which
    is the answer the DPI route gives for a typical Retina panel too.
    """
    try:
        millimetres = float(quartz.CGDisplayScreenSize(display).width)
    except Exception:  # noqa: BLE001 - a display with no physical size
        millimetres = 0.0
    if millimetres > 0:
        return round(pixels[0] / (millimetres / 25.4) / 96.0, 3)
    return round(pixels[0] / (float(points[0]) or 1.0), 3)


class _Desktop:
    """The root of a Mac's accessible tree: every application, and nothing else.

    A Mac has no single AX element spanning the desktop, which is measured rather
    than assumed: on macOS 26.7 the system-wide element publishes exactly four
    attributes -- `AXFocusedApplication`, `AXFocusedUIElement`, `AXRole` and
    `AXRoleDescription` -- and answers `kAXChildrenAttribute` with `-25205`
    (`kAXErrorAttributeUnsupported`). It is a hit-testing and focus-reporting
    element, not a tree. Each application is its own tree, rooted at
    `AXUIElementCreateApplication(pid)`.

    So the element a caller searches from is this: a synthetic root whose children
    are the applications the backend found, whose role is at-spi's
    `desktop frame`, and which refuses every *action*. There is nothing behind it
    to act on, and a `click()` that silently did nothing would be worse than a
    refusal -- the argument `atspi.Element.click` makes about an element offering
    no action.

    It carries the session like any other element, so
    `gui.root_element().child(...)` double-clicks the way an element the session
    found does. `find` and `child` therefore delegate to the backend, which is the
    only object that can walk from here.
    """

    __slots__ = ("_backend", "_session")

    def __init__(self, backend, session=None):
        """Wrap the backend this root belongs to, and the session above it."""
        self._backend = backend
        self._session = session

    def _bind_session(self, session):
        """Record which Session handed this element out. See `Element`."""
        self._session = session

    def _refuse(self, what):
        """The typed refusal every action here raises."""
        return CapabilityUnsupported(
            Capability.ELEMENT_ACTION,
            self._backend.name,
            f"the desktop root is not a widget, so it cannot {what}",
        )

    @property
    def name(self):
        """The empty string: a desktop has no accessible name."""
        return ""

    @property
    def role(self):
        """`desktop frame`, the at-spi role for exactly this element."""
        return _DESKTOP_ROLE

    @property
    def parent(self):
        """None: this is the root."""
        return None

    @property
    def children(self):
        """One element per application, front-most first."""
        return self._backend._applications()

    @property
    def visible(self):
        """True: the desktop is what everything else is visible in."""
        return True

    @property
    def enabled(self):
        """True, which is the only answer that would not be misleading."""
        return True

    @property
    def description(self):
        """The empty string, for the same reason as `name`."""
        return ""

    @property
    def text(self):
        """None: there is no text here to read."""
        return None

    @property
    def value(self):
        """None: there is no number here to read."""
        return None

    @property
    def checked(self):
        """None: there is nothing here to be checked."""
        return None

    @property
    def checkable(self):
        """False: the desktop is not a toggle."""
        return False

    @property
    def selected(self):
        """None: there is nothing here to be selected."""
        return None

    @property
    def selectable(self):
        """False: nothing here can be selected."""
        return False

    @property
    def expanded(self):
        """None: nothing here can be expanded."""
        return None

    @property
    def expandable(self):
        """False: nothing here can be expanded."""
        return False

    @property
    def focused(self):
        """False: a desktop cannot hold keyboard focus."""
        return False

    @property
    def actions(self):
        """Empty: this element offers none."""
        return []

    @property
    def pid(self):
        """None: no single process owns a desktop."""
        return None

    @property
    def alive(self):
        """True: this element is the backend, which holds the reference."""
        return True

    def click(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("be clicked")

    def double_click(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("be double-clicked")

    def focus(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("take focus")

    def set_text(self, text):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("have its text set")

    def do_action(self, name):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse(f"perform {name!r}")

    def select(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("be selected")

    def expand(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("be expanded")

    def collapse(self):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse("be collapsed")

    def choose(self, option):
        """Refuse: there is no such action on a desktop."""
        raise self._refuse(f"choose {option!r}")

    def options(self):
        """Empty: a desktop presents no choices."""
        return []

    def find(self, role=None, name=None):
        """Search every application's tree. See `Element.find`."""
        return self._backend.find_elements(role=role, name=name, within=self)

    def child(self, role=None, name=None):
        """Return the first descendant matching role and/or name, or None."""
        matches = self.find(role=role, name=name)
        return matches[0] if matches else None

    def is_ancestor_of(self, other):
        """True for anything else: every application in the tree hangs here.

        Worth being plain about rather than walking: the applications are this
        element's own children by construction, and a Mac gives no parent link
        from an application element back to a desktop that does not exist as an
        AX element in the first place.
        """
        return other is not self

    def __repr__(self):
        """The role and name, which is how an element is written in a script."""
        return f"Element({self.role!r}, {self.name!r})"


_UNGATED = frozenset(
    {
        Capability.WINDOW_LIST,
        Capability.WINDOW_STATE,
        Capability.WINDOW_GEOMETRY,
        Capability.WINDOW_ACTIVATE,
        Capability.WINDOW_PID,
        Capability.WINDOW_AT_POINT,
        Capability.SCREEN_INFO,
        Capability.POINTER_QUERY,
    }
)
"""What this backend can serve on a Mac with no grant at all.

CoreGraphics answers every one of them and Accessibility gates none: the window
server's list carries the windows, their owners, their pids, their rectangles and
whether they are on screen; the display list carries the screens; and
`CGEventGetLocation` reads the pointer, which ADR 004 §1 marks "no grant at all".
Activation is here for a different reason -- it goes through AppKit's
`NSRunningApplication` rather than through AX. Whole-screen capture and image search
are absent because `capture.py` and `imagesearch.py` already own those capabilities,
and the clipboard because `clipboard.py` owns `pbpaste`/`pbcopy`. Per-window capture
is *not* here either, and is not grant-free when it does arrive: it is
`_WINDOW_CAPTURE` below, which Screen Recording gates.
"""

_AX_ONLY = frozenset(
    {
        Capability.ELEMENT_TREE,
        Capability.ELEMENT_ACTION,
        Capability.ELEMENT_GEOMETRY,
        Capability.WINDOW_PLACEMENT,
        Capability.WINDOW_RESIZE,
        Capability.WINDOW_MINIMIZE,
    }
)
"""What the Accessibility grant buys, and exactly what it gates.

The three element capabilities and the three AX-backed window writes, and nothing
else -- ADR 004 §6's rule of withdrawing what Accessibility actually gates and no
more. `WINDOW_ACTIVATE` is deliberately not among them even though `AXRaise` can
serve it, because the AppKit route needs no grant: the set describes what the
backend can *do*, not what its most capable route needs.
"""

_WINDOW_CAPTURE = frozenset({Capability.WINDOW_CAPTURE})
"""The one capture route this backend owns, and the only set here that is neither
grant-free nor Accessibility's.

`screencapture -l <window number>` is ADR 004 §1's native per-window route, and it
needs the Screen Recording grant, so it is not `_UNGATED`. It is not `_AX_ONLY`
either: Accessibility buys none of it, and a Mac with AX denied and Screen
Recording granted captures windows perfectly well. So it is its own set, declared
whether or not the grant is in place and checked at the call -- `PermissionRequired`
before the tool is spawned, which is what tells a reader *which* grant to give and
to which binary, where a withheld capability could only have said the window could
not be captured. `capture.py` treats the whole-screen `screencapture` route the same
way, so `supports(WINDOW_CAPTURE)` on a Mac means "there is a route here", not "it
works without a grant"; `Environment.has_screen_recording` is the grant's own answer
and `capture_window_id` is where the refusal happens.

The reason it is declared *here* rather than in `capture.py`, which owns the argv
and the gate: a `Window`'s handle is backend-private, and a composite hands one back
only to the member that issued it -- see `CompositeBackend._issuer` -- so the
provider of `WINDOW_CAPTURE` on a Mac has to be the backend that lists the windows.
That is this one, and `capture_window_id` is what it calls with the number.
"""


class MacosBackend(GUIBackend):
    """Accessibility on macOS: elements and windows, joined to CoreGraphics.

    Registered at 90, in the read-only band beside `atspi` and `uia`: the elements
    it hands out are acted on through Accessibility itself, with no coordinates
    and no injected input, so it needs neither the geometry nor the input
    permission the lower bands are about. It is also the one backend on a Mac that
    serves the `WINDOW_*` family, for the reason ADR 004 gives: AX is the only
    route to a Mac window's identity and placement, and the window server's list
    underneath needs no grant at all.

    One capability here is not in that band's spirit and is worth naming: this is
    the provider of `WINDOW_CAPTURE` on a Mac, because it is what issues the
    window numbers a per-window grab is addressed by. The pixels come from
    `screencapture -l` -- `capture.py`'s argv and gate -- and Screen Recording is
    what that route needs; see `_WINDOW_CAPTURE` and `capture` below.

    Construction never prompts unless a caller says so, which is `opt_in`'s rule
    spelled as an option: `connect(backend="macos", backend_options={"request":
    True})` is the caller asking. That is what keeps this backend in automatic
    composition -- a plain `connect()` on a Mac has to get elements -- where
    `macquartz`, whose constructor asks unconditionally, stays out of it.
    """

    name = "macos"

    _SNAPSHOT_SECONDS = 0.25
    """How long one window-server enumeration keeps answering join questions.

    `CGWindowListCopyWindowInfo` is a full enumeration -- fifty entries with a
    bounds dictionary each on the live machine -- and `visible` asks a join
    question for every element a search walks past, so without this a twenty-node
    walk pays twenty enumerations. A quarter of a second is short enough that no
    window has moved far, and long enough that one search is one round trip.
    """

    def __init__(self, environment=None, request=False):
        """Reach the frameworks, note the grant, and offer to ask for it.

        `request` is the whole of ADR 004 §4's split here. The constructor
        *preflights* -- `_macapi.accessibility_trusted`, which is `ctypes` and
        needs no binding -- and calls the prompting form only when a caller asked
        for it by name. A backend built without the grant is a valid backend: it
        declares `_UNGATED` and answers element searches with an empty tree, which
        is why `_darwin_notes` names the grant rather than leaving a caller to
        infer it.

        `_connection` is what can raise, and it raises `BackendUnavailable` with
        the specific reason -- no binding, or one older than the functions this
        module calls. Automatic composition folds that into "not this session"; a
        caller who named `macos` gets it verbatim. See `register`'s docstring.
        """
        self._ax, self._quartz = _connection()
        self.environment = environment
        self.requested = bool(request)
        self._trusted = bool(_macapi.accessibility_trusted())
        if self.requested and not self._trusted:
            self._trusted = bool(request_accessibility())

    @property
    def trusted(self) -> bool:
        """Whether Accessibility is granted to this process, as it was read.

        Public because it is worth asking directly: the capability set is computed
        from it, and "the elements are empty" and "the grant is missing" look
        identical from a search's side otherwise. Read once, at construction -- a
        grant made while a long test runs is picked up by connecting again, which
        is also what rebuilds the capability set.
        """
        return self._trusted

    @property
    def capabilities(self):
        """`_UNGATED`, plus `_AX_ONLY` where the grant is in place.

        A property rather than a class constant, which is where this backend
        differs from every other in the package: the set depends on a permission,
        so it is computed from what was measured at construction. ADR 004 §6 is
        the whole argument for why the ungated half is what it is, and why
        withdrawing more -- or less -- would misreport the machine.

        `_WINDOW_CAPTURE` is in the set either way, and that is not an oversight:
        it is gated by Screen Recording rather than by Accessibility, so the AX
        grant has nothing to say about it. It is checked at the call instead --
        see that set's own docstring.
        """
        if self._trusted:
            return CapabilitySet(_UNGATED | _AX_ONLY | _WINDOW_CAPTURE)
        return CapabilitySet(_UNGATED | _WINDOW_CAPTURE)

    def screens(self):
        """One `Screen` per active display, in the display list's order.

        `CGGetActiveDisplayList` for the list, `CGDisplayBounds` for each
        rectangle and `CGDisplayPixelsWide`/`High` for the pixel dimensions.
        Measured on the live machine: one display, bounds 0, 0, 1280x800, pixels
        1280x800 -- the same rectangle AX and CoreGraphics report for the windows on
        it -- and `scale` came out **0.75**, because that display reports a physical
        size that works out to 72 DPI. Below 1.0 is the convention answering honestly
        rather than a defect: `base.Screen` defines scale as DPI/96, and a 72 DPI
        virtual display is below 96. `_scale` has the arithmetic and the reason this
        is not the backing scale factor.

        The names are synthesised, and it is worth saying so: CoreGraphics
        enumerates displays by id and names none of them, so the main display is
        `"main"` and the rest are `"display1"`, `"display2"`, ... A caller who
        needs a real name is looking for something this API does not have.

        **There is no count-only call, and this method used to call one.**
        `CGGetActiveDisplayCount` is not a CoreGraphics function -- the real
        family is `CGGetActiveDisplayList`/`CGGetOnlineDisplayList`, which
        answer a count *and* a list -- so this raised `AttributeError: Quartz
        has no attribute 'CGGetActiveDisplayCount'` on the first real Mac it
        ran on (macOS 26.7, 2026-09-26), while the fake in
        `tests/test_macos_backend.py` happily provided the invented name and
        `test_screens_come_back_in_the_display_lists_order` passed. ADR 004's
        note about `kAXActionNamesAttribute` is the same lesson one layer down:
        a fake that answers what the real module does not have is worse than no
        fake, because it converts a live failure into a green CI run. The list
        is now asked for directly, with the size CoreGraphics wants a maximum
        for, and `test_no_quartz_entry_point_is_invented` is what keeps the
        next name honest.
        """
        self.require(Capability.SCREEN_INFO)
        err, displays, count = self._quartz.CGGetActiveDisplayList(
            _MAX_ACTIVE_DISPLAYS, None, None
        )
        if err or not count:
            return []
        screens = []
        for index, display in enumerate(list(displays)[:count]):
            bounds = self._quartz.CGDisplayBounds(display)
            pixels = (
                float(self._quartz.CGDisplayPixelsWide(display)),
                float(self._quartz.CGDisplayPixelsHigh(display)),
            )
            points = (bounds.size.width, bounds.size.height)
            main = bool(self._quartz.CGDisplayIsMain(display))
            screens.append(
                Screen(
                    index,
                    int(pixels[0]),
                    int(pixels[1]),
                    _scale(self._quartz, display, pixels, points),
                    "main" if main else f"display{index}",
                )
            )
        return screens

    def desktop_region(self):
        """The main display in points: what `screencapture` with no region writes.

        See `GUIBackend.desktop_region`. The tool captures the main display
        only, in *device pixels* -- two per point on a Retina panel -- so its
        image is this rectangle at whatever scale the display runs. None where
        no display answers, which leaves the identity reading in place.
        """
        err, displays, count = self._quartz.CGGetActiveDisplayList(
            _MAX_ACTIVE_DISPLAYS, None, None
        )
        if err or not count:
            return None
        for display in list(displays)[:count]:
            if self._quartz.CGDisplayIsMain(display):
                bounds = self._quartz.CGDisplayBounds(display)
                return (
                    int(round(bounds.origin.x)),
                    int(round(bounds.origin.y)),
                    int(round(bounds.size.width)),
                    int(round(bounds.size.height)),
                )
        return None

    def pointer_position(self):
        """The pointer's location in global display space, which needs no grant.

        The same two calls `macquartz.pointer_position` makes, spelled here rather
        than imported, because that module is `opt_in`: without this,
        `gui.pointer_position()` would raise on a Mac whose grant a click does not
        need. That gap is one `docs/validation.md` records and ADR 004 §6 assigns
        to this backend -- and the coordinates are the ones `CGEventPost` accepts,
        so a position read here can be handed straight back to a move.
        """
        self.require(Capability.POINTER_QUERY)
        point = self._quartz.CGEventGetLocation(self._quartz.CGEventCreate(None))
        return int(point.x), int(point.y)

    def _onscreen(self):
        """The window server's on-screen list, cached for `_SNAPSHOT_SECONDS`.

        `kCGWindowListOptionOnScreenOnly`, and the reason it is this list rather
        than the full one that answers ordering questions: this one is ordered
        *front to back*, which the full enumeration does not preserve. Measured on
        the live machine -- in the all-windows list Terminal's window sat behind
        four TextEdit windows, and in the on-screen list it was in front of them,
        which is what the desktop actually showed. So `windows`, `active_window`
        and `window_at` read this list, and only lookups by window number fall back
        to `_all`.
        """
        return self._snapshot_or("onscreen")

    def _snapshot_or(self, which):
        """One cached enumeration, refreshed when it is older than the deadline.

        Each list keeps its own timestamp, and that is not bookkeeping: one clock
        shared by two caches would let a refresh of one extend the other's
        deadline, which shows up as a window list a quarter of a second staler
        than it claims to be.
        """
        now = time.monotonic()
        stamp = f"_cache_{which}_at"
        cached = getattr(self, f"_cache_{which}", None)
        if (
            cached is not None
            and now - getattr(self, stamp, 0.0) < self._SNAPSHOT_SECONDS
        ):
            return cached
        option = (
            self._quartz.kCGWindowListOptionOnScreenOnly
            if which == "onscreen"
            else self._quartz.kCGWindowListOptionAll
        ) | self._quartz.kCGWindowListExcludeDesktopElements
        cached = list(
            self._quartz.CGWindowListCopyWindowInfo(
                option, self._quartz.kCGNullWindowID
            )
            or []
        )
        setattr(self, f"_cache_{which}", cached)
        setattr(self, stamp, now)
        return cached

    def _all(self):
        """Every window the server knows, cached -- the lookup list.

        Separate from `_onscreen` because the two answer different questions, and
        only this one still lists a window the user has minimized: measured, a
        minimized window keeps its entry here and loses the on-screen field.
        Enumerated only when a handle is not in the on-screen list, so the common
        path pays for one enumeration rather than two.
        """
        return self._snapshot_or("all")

    def _forget_windows(self):
        """Drop the cached enumerations, after a call that changes the desktop.

        The whole point of a write is that the previous answer is now wrong: the
        join resolves an AX window by matching frames, so a cached CoreGraphics
        rectangle that predates a move is a lookup that cannot find the window it was
        just moved out from under. Measured, and how this was found: the second write
        of a move-then-resize round trip failed with "no AX window matches its
        handle" for exactly this reason. Dropping both lists costs one enumeration
        per write, which is a price a caller who has just moved a window can afford.
        """
        self._cache_onscreen = None
        self._cache_onscreen_at = 0.0
        self._cache_all = None
        self._cache_all_at = 0.0

    def _entry_for(self, handle):
        """The window server's entry for one window number, or None.

        On-screen first -- already in hand for anything a caller has just listed --
        and then the full list, which is what keeps `geometry` and
        `is_window_viewable` answering about a window the user has since minimized
        rather than reporting it gone.
        """
        for entry in self._onscreen():
            if entry.get("kCGWindowNumber") == handle:
                return entry
        for entry in self._all():
            if entry.get("kCGWindowNumber") == handle:
                return entry
        return None

    def _pids(self, everything=False):
        """Every process with a window, in the list's order, deduplicated.

        On-screen by default, because the pids are asked for so `_titles` can
        enrich the windows `windows()` is about to return, and reading AX trees
        for processes with nothing visible is work whose answer is discarded.

        `everything=True` for `_applications`, which is a different question --
        "which processes have an AX tree to search", not "what can be seen" --
        and answering it from the on-screen list would leave an application whose
        windows are all closed or minimized unsearchable, which is not a Mac's
        behaviour.
        """
        seen = []
        for entry in self._all() if everything else self._onscreen():
            pid = entry.get("kCGWindowOwnerPID")
            if pid is not None and pid not in seen:
                seen.append(pid)
        return seen

    @staticmethod
    def _bounds(entry):
        """An entry's `(x, y, width, height)` as four ints, or None.

        Rounded rather than truncated, and ints rather than floats, because this
        is the same coordinate space `move_window` writes back and a caller
        comparing `geometry()` with what it asked for should see the same numbers:
        AX and CoreGraphics both report whole points for a window in practice.
        """
        bounds = entry.get("kCGWindowBounds") if entry else None
        if not bounds:
            return None
        return (
            int(round(bounds.get("X", 0.0))),
            int(round(bounds.get("Y", 0.0))),
            int(round(bounds.get("Width", 0.0))),
            int(round(bounds.get("Height", 0.0))),
        )

    @staticmethod
    def _visible_entry(entry):
        """Whether one window-server entry is a window a person can see.

        Two fields, and both are needed -- which is the measurement that decides
        this backend's whole window story. Measured on macOS 26.7:

        - `kCGWindowListOptionAll` names 50 windows, 36 of them at layer 0, and 21
          of those layer-0 entries are helper surfaces with no AX counterpart: 1280
          x 30 strips shaped like menu bars, 64 x 64 cursor views, 14 x 14 widget
          views, a 500 x 500 `loginwindow`. Layer alone over-reports.
        - Fifteen entries carry `kCGWindowIsOnscreen` at all, and the on-screen
          ones include the Dock (layer 20), the Control Center widgets (layer 25)
          and the menu bar (layer 24). On-screen alone over-reports too.
        - The pair is exact: the layer-0 entries that are also on screen are
          precisely the six windows a person could see -- Terminal, TextEdit's
          four, and a first-run installer panel.
        """
        return (
            bool(entry.get("kCGWindowIsOnscreen")) and entry.get("kCGWindowLayer") == 0
        )

    def _window_from(self, entry, titles):
        """A `Window` for one window-server entry, enriched from `titles`.

        `titles` is passed in rather than computed here so that one enumeration of
        the window server is one AX read per application rather than one per
        window -- see `_titles`.
        """
        pid = entry.get("kCGWindowOwnerPID")
        return Window(
            entry.get("kCGWindowNumber"),
            self,
            title=titles.get((pid, self._bounds(entry)), "")
            or (entry.get("kCGWindowName") or ""),
            app_id=entry.get("kCGWindowOwnerName") or "",
            pid=pid,
        )

    def _titles(self):
        """`{(pid, (x, y, w, h)): title}` from Accessibility, or empty ungranted.

        The enrichment half of the join, and the half that costs: one
        `kAXWindowsAttribute` read per process the window server names. Computed
        once per listing -- one read per application rather than one per window --
        and not remembered across listings, because a title is cheap enough to
        re-read and a cache would be one more thing that can describe a window
        that has since closed.

        Read only where the grant is in place, so an ungranted Mac pays nothing and
        reports empty titles. That is the honest answer rather than a placeholder:
        it is CoreGraphics that is withholding them and AX that hands them back --
        measured, one of fifteen on-screen entries had a non-empty
        `kCGWindowName`, and it was a Window Server one, while TextEdit's four
        windows answered `live_doc4.txt` and its siblings here.
        """
        if not self._trusted:
            return {}
        titles = {}
        for pid in self._pids():
            err, windows = self._copy(
                self._ax.AXUIElementCreateApplication(pid), self._ax.kAXWindowsAttribute
            )
            if err or not windows:
                continue
            for window in windows:
                frame = self._frame(window)
                title = self._text(window, self._ax.kAXTitleAttribute)
                if frame is not None and title:
                    titles[(pid, frame)] = title
        return titles

    def windows(self):
        """The windows a person can see, **bottommost first**.

        The package's cross-platform order, and the reverse of the window
        server's: `_onscreen` is front to back, and `Session.find_window` and
        `wait_for_window` take the *last* match as the topmost, as they do on
        X11 and Windows. Returned in the server's own order, both picked the
        rearmost of two windows sharing a title -- measured on macOS 26.7 with
        two overlapping Tk windows, where `find_window` named whichever was
        behind, both ways round.

        CoreGraphics is the source of truth and Accessibility is the enrichment,
        which is the join ADR 004 describes and what a live run fixes the shape of.
        The filter is `_visible_entry`'s pair -- on screen *and* layer 0 -- and that
        method carries the numbers that rule out either field on its own.

        Titles come from the join rather than from CoreGraphics, because macOS
        withholds other applications' window names without the Screen Recording
        grant: measured, one of fifteen on-screen entries had a non-empty
        `kCGWindowName` and it was a Window Server one. So a Mac with only
        Accessibility granted still gets real titles, and a Mac with neither gets
        empty strings rather than wrong ones.

        A window the user minimizes leaves this list, which is a genuine difference
        from the other desktops: measured, a minimized window keeps its CoreGraphics
        entry but loses the on-screen field, and nothing grant-free tells it apart
        from the helper surfaces above. `geometry` and `is_window_viewable` answer
        about a handle a caller already holds, so a test that minimized a window
        keeps working from the handle it took before doing so.
        """
        self.require(Capability.WINDOW_LIST)
        titles = self._titles()
        return [
            self._window_from(entry, titles)
            for entry in reversed(self._onscreen())
            if self._visible_entry(entry)
        ]

    def active_window(self):
        """The front-most window, from the window server's own ordering.

        Deliberately *not* from `kAXFocusedApplicationAttribute` and
        `kAXFocusedWindowAttribute`, which is the obvious route and is not reliable
        enough to build on: measured on the live machine, the first answered `err=0`
        with Terminal's pid on one run and `-25204` (`kAXErrorCannotComplete`) on the
        next, from the same shell against the same desktop. A flaky answer behind
        "which window is in front" makes every activation test flake with it, so the
        ordering comes from CoreGraphics -- `_onscreen`'s list is front to back,
        and `windows()` reverses it into the package's bottom-to-top order, so the
        front-most window is its last entry -- and AX supplies only the titles.
        """
        self.require(Capability.WINDOW_STATE)
        windows = self.windows()
        return windows[-1] if windows else None

    def window_at(self, x, y, screen=0):
        """The front-most window containing a point, or None.

        `screen` is accepted and unused, as the interface's default has it: a Mac's
        windows live in one global display space -- the same one `CGEventPost` takes
        coordinates in -- so there is no per-output origin to translate from, and
        nothing here disagrees with a caller who passes 0.

        The same list and the same filter `windows` uses, in the same order, so a
        point on the menu bar, on the Dock or on a helper surface answers None --
        none of those is a window a caller can act on, and the numbers in
        `_visible_entry` are what rule them out.
        """
        self.require(Capability.WINDOW_AT_POINT)
        for entry in self._onscreen():
            if not self._visible_entry(entry):
                continue
            frame = self._bounds(entry)
            if frame is None:
                continue
            left, top, width, height = frame
            if left <= x < left + width and top <= y < top + height:
                return self._window_from(entry, self._titles())
        return None

    def geometry(self, window):
        """A window's `(x, y, width, height)`, in screen points.

        From the window server, so it still answers for a window the user has since
        minimized, and in the same space `move_window` and `resize_window` write.
        Measured: identical to what AX reports for the same window -- Terminal at
        153, 79, 877x499 both ways -- which is what makes reading geometry back
        after a move a fair test rather than a comparison of two coordinate spaces.
        """
        self.require(Capability.WINDOW_GEOMETRY)
        frame = self._bounds(self._entry_for(window.handle))
        if frame is None:
            raise WindowNotFound(f"no window with id {window.handle!r}")
        return frame

    def is_window_viewable(self, window):
        """Whether the window server is still showing `window`.

        The on-screen field on its entry, which is exactly the question: measured,
        fifteen of fifty entries carried the field at all and precisely the visible
        ones carried it true. A window the user closed, minimized or hid answers
        False, and so does a handle the server no longer lists at all -- window
        numbers are recycled the way X11 ids are, and nothing showing means nothing
        showing.
        """
        self.require(Capability.WINDOW_STATE)
        entry = self._entry_for(window.handle)
        return bool(entry and entry.get("kCGWindowIsOnscreen"))

    def capture(self, window=None, path=None, region=None):
        """Write a screenshot of one window's own pixels, and return its path.

        `screencapture -l <window number>` -- the route ADR 004 §1's table names,
        and what `capture.py`'s argv builder has taken a window id *for* since it
        was written. Read from the window server rather than off the screen, so
        whatever is stacked on top of the window is not in the image and a window
        hanging off the edge of a display comes back whole: the same property
        `PrintWindow` gives on Windows and `GetImage` on X11. Measured on the live
        machine and not assumed -- §1's table is what put `-l` here.

        `window` is a `Window` from `windows()`, whose handle *is* the window
        number; a raw number is taken as itself, which is what `_issuer` means by
        a caller who passed a handle rather than a Window. This is a Mac's only
        `WINDOW_CAPTURE` provider and it has to be: a handle is backend-private and
        a composite passes one back only to the member that issued it, so a tool
        backend could never be handed this one -- see `_WINDOW_CAPTURE`.

        Whole-screen capture is deliberately not served here. `window=None`
        therefore refuses naming the member that does serve it, rather than
        reporting a missing capability: on a Mac those are different problems, and
        `capture:screencapture` needs nothing from this backend. `region` beside a
        window is refused by `check_region`, for the reason it is everywhere else
        -- a region is screen-absolute, and pairing the two could only mean one or
        the other.

        Screen Recording is checked before the tool is spawned, so a Mac that has
        denied it raises `PermissionRequired` naming the grant and the interpreter
        rather than writing a black PNG -- see `capture_window_id`, which owns that
        gate, the argv and the "did it write anything" check.

        Measured live on macOS 26.7, against a window created by a separate
        process with a second window stacked over its centre. The image is the
        rectangle `geometry()` reports, exactly -- 320x272 for a window
        `geometry()` also called 320x272 -- once `-o` is passed; the same window
        without it came back 388x340, the drop shadow as black padding. And the
        occluded window's centre pixel was its own colour, where the same
        rectangle cut out of a screen shot held the occluder's: `window_at()`
        named that occluder at those coordinates in the same run, so the
        comparison was against a control rather than an assumption. This is the
        property `PrintWindow` gives on Windows and `GetImage` on X11, now
        confirmed here rather than inferred from ADR 004 §1's table.

        One thing does *not* carry over from the Windows route: a window that has
        closed since its id was read is not detected, and cannot be. See
        `capture_window_id` for what the tool answers there.
        """
        if window is None:
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                "whole-screen capture on a Mac belongs to the tool that ships "
                "with the OS (`capture:screencapture`); this backend serves the "
                "per-window route, which needs a window's own number",
            )
        self.require(Capability.WINDOW_CAPTURE)
        check_region(region, window)
        return capture_window_id(self._window_number(window), path)

    def _window_number(self, window):
        """The window number for a `Window` or a raw handle."""
        return window.handle if isinstance(window, Window) else window

    def move_window(self, window, x, y):
        """Move a window by writing its AX position, with no pointer involved.

        Which is what makes this work on a window that is not front-most, and why
        ADR 004 §6 withdraws `WINDOW_PLACEMENT` when the grant is missing. Measured:
        the AX rectangle and the CoreGraphics one agree exactly, both in points, so
        a `geometry()` read after a move reports what was asked for.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        self._write_frame(
            window,
            self._ax.kAXPositionAttribute,
            self._ax.AXValueCreate(
                self._ax.kAXValueCGPointType, self._quartz.CGPointMake(x, y)
            ),
            Capability.WINDOW_PLACEMENT,
            "move",
        )

    def resize_window(self, window, width, height):
        """Resize a window by writing its AX size. See `move_window`."""
        self.require(Capability.WINDOW_RESIZE)
        self._write_frame(
            window,
            self._ax.kAXSizeAttribute,
            self._ax.AXValueCreate(
                self._ax.kAXValueCGSizeType, self._quartz.CGSizeMake(width, height)
            ),
            Capability.WINDOW_RESIZE,
            "resize",
        )

    def minimize_window(self, window, minimized=True):
        """Minimize or restore a window through `kAXMinimizedAttribute`.

        A write to the window element rather than a click on its minimize button, so
        it depends neither on the button being visible nor on a theme putting it
        where a script expects -- and `WINDOW_MINIMIZE` is withdrawn without the
        grant for the same reason the other AX writes are. Measured on the live
        machine: every AX window answered `kAXMinimizedAttribute` with False, so the
        attribute is there to write on a real window.
        """
        self.require(Capability.WINDOW_MINIMIZE)
        node = self._require_ax_window(window, Capability.WINDOW_MINIMIZE, "minimize")
        err = self._set(node, self._ax.kAXMinimizedAttribute, bool(minimized))
        self._forget_windows()
        if err:
            raise CapabilityUnsupported(
                Capability.WINDOW_MINIMIZE,
                self.name,
                f"could not minimize {window!r}: AX answered {err}",
            )

    def activate_window(self, window):
        """Bring a window forward, by raising it or by activating its application.

        Two routes, neither an AX *window* call -- which is why `WINDOW_ACTIVATE`
        survives without the grant:

        - `AXRaise` on the window element, which raises exactly this window and
          needs the grant.
        - `NSRunningApplication.activateWithOptions_` by pid, which is the
          documented way and needs no permission at all, and which raises the
          application's whole window set -- AppKit has no per-window equivalent.

        So the AX route is used first where the grant is in place, and the AppKit
        one is what keeps this working on a machine without it. A handle the window
        server no longer lists is a `WindowNotFound`; a machine that refuses both
        routes is a typed `CapabilityUnsupported` saying which two answered.
        """
        self.require(Capability.WINDOW_ACTIVATE)
        entry = self._entry_for(window.handle)
        if entry is None:
            raise WindowNotFound(f"no window with id {window.handle!r}")
        pid = window.pid if window.pid is not None else entry.get("kCGWindowOwnerPID")
        raised = False
        if self._trusted:
            node = self._ax_window(window)
            if node is not None:
                try:
                    self._perform(node, "AXRaise")
                    raised = True
                except CapabilityUnsupported:
                    # AX found the window but refused to raise it -- fall through
                    # to the AppKit route below rather than failing an activation
                    # that route could still serve.
                    pass
        # Raising reorders the window server's list, which is what `windows` and
        # `active_window` read, so both caches go -- including where the AX raise was
        # the route that worked.
        self._forget_windows()
        if not raised and pid is not None and not self._activate(pid):
            raise CapabilityUnsupported(
                Capability.WINDOW_ACTIVATE,
                self.name,
                f"could not bring {window!r} forward: AppKit refused the activation "
                "and no AX window matched its handle",
            )

    def root_element(self):
        """The desktop: a synthetic root over every searchable application.

        Not the system-wide element, which is the obvious answer and is measured to
        be the wrong one: it publishes only `AXFocusedApplication`,
        `AXFocusedUIElement`, `AXRole` and `AXRoleDescription`, and answers
        `kAXChildrenAttribute` with `-25205`. `_Desktop` carries the whole argument
        for why a Mac needs a synthetic root at all.
        """
        self.require(Capability.ELEMENT_TREE)
        return _Desktop(self)

    def find_elements(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """Search the accessible tree, breadth first, in the tree's own order.

        Walked here rather than asked of the platform, because AX has no search --
        see `_MAX_NODES` for what that costs and why the budget exists. Breadth
        first so that a front-most application's shallow elements are found before a
        background one's deep elements, which is what makes `find_element` answer
        with the thing a person would point at.

        A tree that lies about its own shape is walked once along each path and not
        into itself: a child that reports an *ancestor* as its own child -- which is
        what a broken application, or a stale reference, looks like from here -- is
        not followed, so the walk cannot leave the tree and one element is never
        reported twice along a path it is its own descendant on. That is a stronger
        claim than `_MAX_DEPTH` can make, and the depth limit and `_MAX_NODES` are
        the second line of defence behind it, for a tree that is merely enormous
        rather than circular.

        `name`/`description` take a plain string (exact match) or a compiled regex
        (`.search()`); `enabled`/`visible` filter on element state; `predicate` is an
        escape hatch for anything else, and together with `Element.parent`,
        `.children` and `.is_ancestor_of` it covers ancestor and descendant queries
        without a dedicated relation API.
        """
        self.require(Capability.ELEMENT_TREE)
        matches = self._build_predicate(
            role, name, enabled, visible, description, predicate
        )
        found = []
        root = within if within is not None else self.root_element()
        queue: list[tuple[object, int, tuple[object, ...]]] = [(root, 0, ())]
        visited = 0
        while queue and visited < _MAX_NODES:
            item, depth, ancestry = queue.pop(0)
            visited += 1
            # The search root itself is never a match, even when `within` is a
            # real Element that happens to satisfy the predicate -- find_elements
            # searches descendants, per base.py's contract, and `item is root`
            # only excludes the one node the queue was seeded with, not any
            # equal-looking descendant reached from elsewhere.
            if item is not root and not isinstance(item, _Desktop) and matches(item):
                found.append(item)
            if depth >= _MAX_DEPTH:
                continue
            # The raw nodes from the search root down to here, which is the set a
            # child must not be in -- `_Desktop` has no node of its own, hence the
            # None that is skipped rather than compared.
            line = ancestry + (getattr(item, "node", None),)
            queue.extend(
                (child, depth + 1, line)
                for child in self._children(item)
                if not any(
                    seen is not None and self._same(child.node, seen) for seen in line
                )
            )
        return found

    def find_element(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """The first match, or None."""
        matches = self.find_elements(
            role=role,
            name=name,
            within=within,
            enabled=enabled,
            visible=visible,
            description=description,
            predicate=predicate,
        )
        return matches[0] if matches else None

    def _build_predicate(self, role, name, enabled, visible, description, predicate):
        """An `Element -> bool` function applying every filter a search was given.

        Mirrors `atspi._build_predicate` and `UiaBackend._build_predicate`, defaults
        included: an element that will not answer `kAXEnabledAttribute` counts as
        enabled, and one that publishes no geometry counts as showing, because a
        caller about to act on the element is better served by trying than by a
        silent "it is not there". The role is compared against every AX role this
        package reports for it *and* every spelling of the role asked for, which is
        what keeps `roles.py`'s alias table working here.
        """
        wanted = _ax_roles(role)

        def matches(element):
            """Whether this element has the wanted role and matches the filters."""
            if wanted is not None and _raw_role(element) not in wanted:
                return False
            if name is not None and not _matches_text(element.name, name):
                return False
            if enabled is not None and element.enabled != enabled:
                return False
            if visible is not None and element.visible != visible:
                return False
            if description is not None and not _matches_text(
                element.description, description
            ):
                return False
            return bool(predicate(element)) if predicate is not None else True

        return matches

    def extents(self, element):
        """`element`'s (x, y, width, height) in screen coordinates, or None.

        AX position and size, read through `AXValueGetValue`. None rather than a
        raise where either is missing or the rectangle is empty -- an ordinary answer
        about an element that occupies no screen space, which is what
        `base.GUIBackend.extents` documents.

        Measured, and worth knowing before aiming a click at one: an AX text area's
        rectangle is its *document* extent rather than its viewport. The one at
        (640, 400) reported y = -6639 and height = 7217, so a rectangle from here can
        be far larger than the screen and its centre can be off it.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        if isinstance(element, _Desktop):
            return None
        frame = self._frame(element.node)
        if frame is None:
            return None
        return None if frame[2] <= 0 or frame[3] <= 0 else frame

    def element_at(self, x, y):
        """The deepest accessible element at a screen coordinate, or None.

        `AXUIElementCopyElementAtPosition` on the system-wide element -- the one
        thing that element is for, measured: it answered with the text area under
        (640, 400), its pid, and that element's own geometry. None where AX declines,
        which it does for a point inside no window's own tree.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        err, element = self._ax.AXUIElementCopyElementAtPosition(
            self._ax.AXUIElementCreateSystemWide(), int(x), int(y), None
        )
        if err or element is None:
            return None
        return Element(element, self)

    def _applications(self):
        """The applications the tree is searched through, as `Element`s.

        Two sources, unioned, because neither is complete on a Mac:

        - Every process with an on-screen window, from the window server's own list,
          so whatever a caller can see is searchable, front-most first.
        - Every *regular* application `NSWorkspace` reports, so an application whose
          windows are all closed or minimized is searched too. Measured from an SSH
          login: 46 processes reported and exactly four were regular -- Finder,
          Terminal, TextEdit and the Command Line Tools installer.

        A Save panel is neither: it is a separate XPC process with
        `activationPolicy` 2, and it is included by the first source anyway, which is
        the point -- its "Save" button lives in its own process's AX tree, and a
        script asking for one after a Save command has to be able to find it.

        Each candidate is checked by reading its `kAXRoleAttribute`, which is what
        the platform offers as "is this an application": measured, every pid the
        window server named answered `AXApplication` except the Window Server
        itself, which answers `-25204` (`kAXErrorCannotComplete`). That check is also
        why this is only ever reached with the grant in place -- the element family
        is withdrawn without it, and `root_element` is its only caller.

        Cost: one AX round trip per candidate, measured at 1.83 ms per node on the
        live machine, so a desktop with twenty processes pays about 35 ms to build a
        search's root.
        """
        pids = list(self._pids(everything=True))
        for pid in self._regular_app_pids():
            if pid not in pids:
                pids.append(pid)
        applications = []
        for pid in pids:
            app = self._ax.AXUIElementCreateApplication(pid)
            err, _role = self._copy(app, self._ax.kAXRoleAttribute)
            if not err:
                applications.append(Element(app, self))
        return applications

    def _regular_app_pids(self):
        """The pids of the applications with a Dock presence, or none.

        `NSWorkspace.runningApplications()` filtered to `activationPolicy() == 0`,
        which is the documented meaning of a regular application: one that appears in
        the Dock and in the application switcher. Measured from an SSH login, so this
        is known to work from a shell with no window server connection of its own.

        Empty where AppKit cannot be imported, which is not a state a Mac reaches in
        practice -- `pyobjc-framework-Quartz` depends on Cocoa -- but is a state this
        has to answer *something* about, and an empty list degrades the application
        list to the window server's pids rather than failing a search outright.
        """
        appkit = _import("AppKit")
        if appkit is None:
            return []
        try:
            running = appkit.NSWorkspace.sharedWorkspace().runningApplications()
        except Exception:  # noqa: BLE001 - a workspace that will not answer
            return []
        return [
            application.processIdentifier()
            for application in running
            if application.activationPolicy() == 0 and not application.isTerminated()
        ]

    def _activate(self, pid):
        """Bring an application forward, through AppKit or through AX.

        True when one of the two routes reported success. AppKit first: it needs no
        grant, and it is the documented call. `kAXFrontmostAttribute` second, a write
        to the application element -- the route left for a machine where AppKit
        cannot be imported, and one that needs the grant the first route does not.
        """
        appkit = _import("AppKit")
        if appkit is not None:
            bridge = appkit.NSRunningApplication
            try:
                running = bridge.runningApplicationWithProcessIdentifier_(pid)
            except Exception:  # noqa: BLE001 - no such application, or no bridge
                running = None
            if running is not None:
                try:
                    if running.activateWithOptions_(
                        appkit.NSApplicationActivateAllWindows
                    ):
                        return True
                except Exception:  # noqa: BLE001 - an activation AppKit refused
                    pass
        app = self._ax.AXUIElementCreateApplication(pid)
        return not self._set(app, self._ax.kAXFrontmostAttribute, True)

    def _action_names(self, node):
        """AX's own action names for one element, or nothing where it offers none.

        `AXUIElementCopyActionNames`, which is a function call rather than an
        attribute read because that is what the binding has: measured on PyObjC
        12.2.2, `ApplicationServices` publishes no `kAXActionNamesAttribute`, so the
        attribute route is an `AttributeError` on every element rather than an error
        code -- the one place this module's accessor shape differs from the AX API
        documentation's.
        """
        try:
            err, names = self._ax.AXUIElementCopyActionNames(node, None)
        except Exception:  # noqa: BLE001 - an element that will not answer
            return []
        return [] if err or not names else list(names)

    def _copy(self, node, attribute):
        """`(AXError, value)` for one attribute read, never raising.

        Every AX read has this shape: an error code plus an out-parameter that is
        left alone when the read fails. A binding that refused outright -- which
        PyObjC does for an argument it cannot marshal -- is reported as a string
        error rather than an exception, so every caller can treat "no value" as one
        case.
        """
        try:
            return self._ax.AXUIElementCopyAttributeValue(node, attribute, None)
        except Exception as exc:  # noqa: BLE001 - a binding that refused
            return (f"{type(exc).__name__}: {exc}", None)

    def _text(self, node, attribute):
        """One string attribute, or "" where nothing is published.

        `-25212` (`kAXErrorNoValue`) is the ordinary answer for an attribute an
        element has but has no value for -- measured on Finder, whose window answered
        exactly that -- and it reads as "" here, the same as an attribute that is not
        published at all. A caller asking for a title is not served by telling those
        two apart.
        """
        err, value = self._copy(node, attribute)
        return "" if err or not isinstance(value, str) else value

    def _frame(self, node):
        """One element's `(x, y, width, height)` in screen points, or None."""
        point = self._point(node)
        size = self._size(node)
        if point is None or size is None:
            return None
        return (
            int(round(point[0])),
            int(round(point[1])),
            int(round(size[0])),
            int(round(size[1])),
        )

    def _point(self, node):
        """The element's position, or None where it publishes none.

        `kAXPositionAttribute` holds an `AXValue`, which is opaque until
        `AXValueGetValue` is handed the type it contains: `kAXValueCGPointType` here,
        `kAXValueCGSizeType` in `_size`. Measured: a mismatched type raises rather
        than answering, which is why both are wrapped rather than trusted.
        """
        err, value = self._copy(node, self._ax.kAXPositionAttribute)
        if err or value is None:
            return None
        try:
            ok, point = self._ax.AXValueGetValue(
                value, self._ax.kAXValueCGPointType, None
            )
        except Exception:  # noqa: BLE001 - a value that will not unwrap
            return None
        return (point.x, point.y) if ok else None

    def _size(self, node):
        """The element's size, or None where it publishes none. See `_point`."""
        err, value = self._copy(node, self._ax.kAXSizeAttribute)
        if err or value is None:
            return None
        try:
            ok, size = self._ax.AXValueGetValue(
                value, self._ax.kAXValueCGSizeType, None
            )
        except Exception:  # noqa: BLE001 - a value that will not unwrap
            return None
        return (size.width, size.height) if ok else None

    def _set(self, node, attribute, value):
        """`AXUIElementSetAttributeValue`, returning its AXError rather than raising.

        An error code rather than an exception because several callers build a
        message out of it -- `-25205` is "this element has no such attribute" and
        `-25204` is "the process would not answer" -- and the difference between
        those two is the whole of what a caller can act on.
        """
        try:
            return self._ax.AXUIElementSetAttributeValue(node, attribute, value)
        except Exception as exc:  # noqa: BLE001 - a binding that refused
            return f"{type(exc).__name__}: {exc}"

    def _perform(self, node, action):
        """Perform an AX action, refusing with the AXError where AX said no.

        A raise rather than a return value, because every caller has already
        established that the element publishes the action: failing past that point
        means "the element is disabled or gone", which is a refusal to report rather
        than a branch to take.
        """
        try:
            err = self._ax.AXUIElementPerformAction(node, action)
        except Exception as exc:  # noqa: BLE001 - an action that will not perform
            raise CapabilityUnsupported(
                Capability.ELEMENT_ACTION,
                self.name,
                f"could not perform {action}: {exc}",
            ) from exc
        if err:
            raise CapabilityUnsupported(
                Capability.ELEMENT_ACTION,
                self.name,
                f"could not perform {action}: AX answered {err}",
            )

    def _pid(self, node):
        """The process an AX element belongs to, or None.

        PyObjC answers `AXUIElementGetPid(node, None)` with an `(error, pid)` pair --
        measured: `(0, 690)` for Terminal -- where the C function writes through a
        pointer, so the pair is unwrapped here. The bare-value case is kept for a
        binding that decides to return one instead.
        """
        try:
            result = self._ax.AXUIElementGetPid(node, None)
        except Exception:  # noqa: BLE001 - an element with no process
            return None
        if isinstance(result, tuple):
            err, pid = result
            return None if err else pid
        return result

    def _parent(self, node):
        """The containing AX element, or None at a root."""
        err, parent = self._copy(node, self._ax.kAXParentAttribute)
        return None if err or parent is None else parent

    def _same(self, first, second):
        """Whether two AX references are the same element.

        CoreFoundation equality, which is what AX offers and what PyObjC's `==`
        implements: two references to one element are different Python objects as
        often as not, so comparing the references themselves would answer False about
        an element and itself.
        """
        try:
            return bool(first == second)
        except Exception:  # noqa: BLE001 - a reference that will not compare
            return first is second

    def _ax_children(self, node):
        """The raw children of one raw AX node, or nothing where there are none."""
        err, children = self._copy(node, self._ax.kAXChildrenAttribute)
        if err or not children:
            return []
        return list(children)

    def _children(self, item):
        """The child *Elements* of an `Element` or of the desktop root.

        Two shapes, because the root is synthetic: a `_Desktop` has applications
        where an AX element has AX children. Everything above treats the two
        identically, which is what lets one breadth-first walk cover a desktop.
        """
        if isinstance(item, _Desktop):
            return self._applications()
        session = getattr(item, "_session", None)
        return [Element(node, self, session) for node in self._ax_children(item.node)]

    def _window_node(self, node):
        """The AX window `node` sits in, or None.

        Walked upwards by *role* rather than by an attribute, because AX publishes no
        "which window am I in": the containing toplevel is the nearest ancestor whose
        role is `AXWindow`. Bounded by `_MAX_DEPTH` so a tree reporting a cycle cannot
        spin this, and it answers for a window element too, since the walk starts at
        `node` itself.
        """
        current = node
        for _ in range(_MAX_DEPTH):
            err, role = self._copy(current, self._ax.kAXRoleAttribute)
            if not err and role == "AXWindow":
                return current
            parent = self._parent(current)
            if parent is None:
                return None
            current = parent
        return None

    def _window_showing(self, node):
        """Whether the window holding `node` is on screen, or None undecided.

        Three answers rather than two, and None is the one that matters: an element
        whose containing window is not in the window server's on-screen list -- a
        hidden application's window, a Dock or menu-bar element, anything whose frame
        the join cannot match -- cannot be judged from here, and `Element.visible`
        turns that into "visible" rather than into a false "hidden".
        """
        window = self._window_node(node)
        if window is None:
            return None
        pid = self._pid(window)
        frame = self._frame(window)
        if pid is None or frame is None:
            return None
        for entry in self._onscreen():
            if entry.get("kCGWindowOwnerPID") == pid and self._bounds(entry) == frame:
                return bool(entry.get("kCGWindowIsOnscreen"))
        return None

    def _ax_window(self, window):
        """The AX element behind a `Window` handle, or None.

        Resolved through the join in the other direction: the handle is a window
        number, so the AX element is the one in that process whose frame matches the
        entry's rectangle. Measured: the two agree exactly -- Terminal at 153, 79,
        877x499 both ways -- which is what makes this lookup work at all. Where an
        application's own window list is malformed (measured: Finder answers
        `kAXWindowsAttribute` with an `AXScrollArea`) there is simply no match, so a
        write is refused rather than aimed at the wrong element.
        """
        entry = self._entry_for(window.handle)
        if entry is None:
            return None
        pid = window.pid if window.pid is not None else entry.get("kCGWindowOwnerPID")
        frame = self._bounds(entry)
        if pid is None or frame is None:
            return None
        err, windows = self._copy(
            self._ax.AXUIElementCreateApplication(pid), self._ax.kAXWindowsAttribute
        )
        if err or not windows:
            return None
        near = None
        for node in windows:
            candidate = self._frame(node)
            if candidate == frame:
                return node
            if candidate is not None and _close(candidate, frame) and near is None:
                near = node
        return near

    def _require_ax_window(self, window, capability, what):
        """The AX element for `window`, or a typed refusal naming what was asked."""
        node = self._ax_window(window)
        if node is None:
            raise CapabilityUnsupported(
                capability,
                self.name,
                f"could not {what} {window!r}: no AX window matches its handle, so "
                "there is nothing to write to",
            )
        return node

    def _write_frame(self, window, attribute, value, capability, what):
        """Write one AX `AXValue` onto a window, or refuse naming why.

        The value is built by the caller, which is where the type belongs: a
        position is `kAXValueCGPointType` around a `CGPoint`, a size is
        `kAXValueCGSizeType` around a `CGSize`, and deriving which of the two
        from the *attribute* -- by comparing it against `kAXPositionAttribute`
        -- is a comparison this backend should not have to make at all.
        Measured while testing: it is also a comparison that silently takes the
        wrong branch when the two attribute objects are not the same object,
        which a fake can arrange and no belief about a binding should depend on.

        The window caches are dropped on the way out, because a caller that has
        just moved a window is about to ask where it is: `_forget_windows`
        carries the measurement that made this necessary.
        """
        node = self._require_ax_window(window, capability, what)
        err = self._set(node, attribute, value)
        self._forget_windows()
        if err:
            raise CapabilityUnsupported(
                capability,
                self.name,
                f"could not {what} {window!r}: AX answered {err}",
            )


def _close(first, second, tolerance: int = 2) -> bool:
    """Whether two rectangles are the same window, allowing a point or two.

    Between the moment an AX write lands and the moment AX and the window server
    agree about it, the two sources can differ by a rounding step -- and the join
    resolves a window by matching one against the other. Two *real* windows of one
    process are never within a tolerance of each other without overlapping outright,
    so this cannot silently address the wrong window, and the alternative is a write
    that fails because the rectangle it just set has not been reported everywhere yet.
    """
    return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=False))


def _raw_role(element) -> str:
    """One element's AX role name, which is what `_ax_roles` filters on.

    The untranslated name -- "AXButton" rather than "push button" -- because the
    table's keys are AX's own names. Read through the element's own cache, since a
    search asks this once per visited element and the walk is the expensive part.
    """
    return element._read(element._ax.kAXRoleAttribute, "")


if TYPE_CHECKING:

    def _conforms(element: Element) -> _ElementInterface:
        """Check, statically only, that `Element` satisfies the interface.

        `Session.element` and the widget finders are annotated with
        `base.Element`, so this class is what makes those annotations true. Renaming
        or dropping a member would otherwise surface as a type error in whoever called
        it, a module away from the cause -- the same guard `uia.py` and `atspi.py`
        keep.
        """
        return element
