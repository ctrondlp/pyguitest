"""The X11::GUITest 0.29 migration table, as data.

All 50 symbols from `@EXPORT_OK`, each mapped to the capability that serves it
and the tier it lands in on Wayland. This is the audit in machine-readable
form: docs/developers/wayland-audit.md is the same table with the reasoning.

It exists so migration tooling can answer "what happens to my script?" without
a developer re-reading the audit:

    from pyguitest.compat import LEGACY, unavailable
    LEGACY["GetMousePos"].tier          -> Tier.NO_PATH
    [f.name for f in unavailable()]     -> the 6 Wayland refuses

Nothing here is executable. `replacement` names the operation that serves the
call now, or None where nothing in the package spells it -- which is only
`GetScreenDepth` and `GetWindowsFromPid`. A tier is a statement about Wayland,
not about availability: all six Tier.NO_PATH rows do have replacements, served
on X11, Windows and (for `pointer_position`) macOS, and raise
CapabilityUnsupported on Wayland. `unavailable()` is named for that tier, so
read it as "cannot be ported to Wayland", not "does not exist".
"""

from __future__ import annotations

from collections import namedtuple

from .capabilities import Capability as C
from .capabilities import Tier

__all__ = [
    "LegacyFunction",
    "LEGACY",
    "CONSTANTS",
    "by_tier",
    "constant",
    "unavailable",
    "unported",
    "portable",
]

LegacyFunction = namedtuple(
    "LegacyFunction", "name x11 capability tier replacement note"
)


def _f(name, x11, capability, tier, replacement, note=""):
    """Build a LegacyFunction, keeping the table below readable."""
    return LegacyFunction(name, x11, capability, tier, replacement, note)


_ALL = [
    # -- T1: no display server involved ------------------------------------
    _f(
        "StartApp",
        "fork/exec",
        C.PROCESS_LAUNCH,
        Tier.PORTABLE,
        "start_app",
        "Returns an Application (a Popen wrapper), not a pid, and does not "
        "sleep. The original slept 1 s and returned the pid only if the "
        "child was still running, else undef: `app.poll() is None` after a "
        "wait is the same check. A list command that cannot start raises "
        "OSError rather than returning nothing",
    ),
    _f(
        "RunApp",
        "system()",
        C.PROCESS_LAUNCH,
        Tier.PORTABLE,
        "run_app",
        "Returns a subprocess.CompletedProcess. system() returned the wait "
        "status (exit code x 256, -1 if it could not start), so `== 0` "
        "becomes `.returncode == 0`, which is already the plain exit code",
    ),
    _f("WaitSeconds", "sleep()", C.TIMING, Tier.PORTABLE, "wait"),
    _f(
        "SetEventSendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session(event_delay=)",
        "SECONDS, not milliseconds: SetEventSendDelay(10) is event_delay=0.01. "
        "The original returned the old value and defaulted to 10 ms; the "
        "Session attribute defaults to 0.0 and the setter returns nothing",
    ),
    _f(
        "GetEventSendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session.event_delay",
        "Reads seconds, where the original returned milliseconds -- "
        "multiply by 1000 to compare",
    ),
    _f(
        "SetKeySendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session(key_delay=)",
        "SECONDS, not milliseconds: SetKeySendDelay(50) is key_delay=0.05. "
        "The original returned the old value; the default is 0 in both",
    ),
    _f(
        "GetKeySendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session.key_delay",
        "Reads seconds, where the original returned milliseconds -- "
        "multiply by 1000 to compare",
    ),
    _f(
        "QuoteStringForSendKeys",
        "string substitution",
        None,
        Tier.PORTABLE,
        "quote_for_type",
        "The {} grammar was kept; also now escapes # (Meta), which the "
        "original omitted. & (AltGr) is still not escaped, as upstream: "
        "write {&} yourself for a literal one",
    ),
    _f(
        "QSfSK",
        "alias",
        None,
        Tier.PORTABLE,
        "quote_for_type",
        "Drop the abbreviation; same & caveat as QuoteStringForSendKeys",
    ),
    # -- T2: core Wayland protocol -----------------------------------------
    _f(
        "GetScreenRes",
        "DisplayWidth/Height",
        C.SCREEN_INFO,
        Tier.DIRECT,
        "Screen.size",
        "wl_output.mode also gives scale and refresh",
    ),
    _f(
        "ScreenCount",
        "ScreenCount()",
        C.SCREEN_INFO,
        Tier.DIRECT,
        "len(screens)",
        "Semantics shift: X11 screens are rare, Wayland outputs are monitors",
    ),
    _f(
        "DefaultScreen",
        "DefaultScreen()",
        C.SCREEN_INFO,
        Tier.DIRECT,
        "screens[0]",
        "No such concept; first advertised output by convention",
    ),
    _f(
        "GetScreenDepth",
        "DefaultDepth()",
        C.SCREEN_INFO,
        Tier.DIRECT,
        None,
        "Degenerate: every display server this package supports is 8 bits per channel",
    ),
    # -- T4: input injection -----------------------------------------------
    _f(
        "MoveMouseAbs",
        "XTestFakeMotionEvent",
        C.POINTER_MOVE,
        Tier.PRIVILEGED,
        "move_mouse",
    ),
    _f(
        "PressMouseButton",
        "XTestFakeButtonEvent",
        C.POINTER_BUTTON,
        Tier.PRIVILEGED,
        "press_button",
    ),
    _f(
        "ReleaseMouseButton",
        "XTestFakeButtonEvent",
        C.POINTER_BUTTON,
        Tier.PRIVILEGED,
        "release_button",
    ),
    _f(
        "ClickMouseButton",
        "press+release",
        C.POINTER_BUTTON,
        Tier.PRIVILEGED,
        "click",
        "Buttons 4/5 must become scroll axis events",
    ),
    _f("PressKey", "XTestFakeKeyEvent", C.KEY_EVENT, Tier.PRIVILEGED, "press_key"),
    _f("ReleaseKey", "XTestFakeKeyEvent", C.KEY_EVENT, Tier.PRIVILEGED, "release_key"),
    _f("PressReleaseKey", "press+release", C.KEY_EVENT, Tier.PRIVILEGED, "tap_key"),
    _f(
        "SendKeys",
        "XKeysymToKeycode + XGetKeyboardMapping",
        C.KEY_EVENT,
        Tier.PRIVILEGED,
        "send_keys",
        "Grammar ported as its own method, over press_key/release_key/"
        "type_text; static ASCII key mapping, not the dynamic keysym "
        "lookup. See the keymap trap for why that part does not carry over",
    ),
    # -- T3: per-desktop window backends -----------------------------------
    _f(
        "GetWindowName",
        "XFetchName, _NET_WM_NAME",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        "Window.title",
    ),
    _f(
        "FindWindowLike",
        "XQueryTree + regex",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        "find_windows",
        "Returns only toplevels; no recursive descent. The pattern was a Perl "
        "regex matched with /i: unanchored and case-INsensitive, and the "
        "result a list. A pyguitest string is a case-sensitive literal "
        "substring, so a ported pattern needs re.compile(pattern, re.I) -- "
        "and a regex metacharacter in it now needs the compiled form too",
    ),
    _f(
        "WaitWindowLike",
        "poll FindWindowLike",
        C.WINDOW_EVENTS,
        Tier.COMPOSITOR,
        "wait_for_window",
        "Session.wait_for_window works everywhere WINDOW_LIST does, polling "
        "find_windows; it only uses real event notification -- faster, "
        "race-free -- where WINDOW_EVENTS is also available. Same pattern "
        "caveat as FindWindowLike (literal, case-sensitive). The original "
        "returned a list, empty on timeout, and waited 10 s by default; "
        "this returns the Window or None, and timeout=None (the default) "
        "waits forever",
    ),
    _f(
        "WaitWindowClose",
        "poll IsWindow",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        "wait_window_close",
        "Matches by Window.handle, not title; event-driven only where "
        "WINDOW_EVENTS is available, polling windows() otherwise. Returns "
        "bool for the original's 1/0, but the original timed out after 10 s "
        "by default where timeout=None here waits forever",
    ),
    _f(
        "WaitWindowViewable",
        "poll XGetWindowAttributes",
        C.WINDOW_STATE,
        Tier.COMPOSITOR,
        "is_window_viewable",
        "A one-shot state read rather than a wait: the original waited (10 s "
        "by default) for a matching window that was viewable and returned a "
        "list of them. Port it as wait_for_window followed by a poll of "
        "is_window_viewable on the result, or a loop of your own. kdotool "
        "has no mapped/visibility query and refuses this one specifically",
    ),
    _f(
        "IsWindow",
        "XGetWindowAttributes",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        "is_window_open",
        "Asked of windows() rather than of the handle, which does not know "
        "it has died; refresh_window() answers the same question and hands "
        "back the current title with it",
    ),
    _f(
        "IsWindowViewable",
        "IsViewable attribute",
        C.WINDOW_STATE,
        Tier.COMPOSITOR,
        "is_window_viewable",
        "Minimized state, not X11's IsViewable attribute -- same caveat as "
        "WaitWindowViewable, and kdotool refuses it outright",
    ),
    _f(
        "GetWindowPid",
        "_NET_WM_PID",
        C.WINDOW_PID,
        Tier.COMPOSITOR,
        "Window.pid",
        "Carried by no foreign-toplevel protocol",
    ),
    _f(
        "GetWindowsFromPid",
        "scan tree for _NET_WM_PID",
        C.WINDOW_PID,
        Tier.COMPOSITOR,
        None,
        "No pid= filter exists yet; [w for w in windows() if w.pid == "
        "target] until it does. Prefer app_id",
    ),
    _f(
        "GetWindowPos",
        "XTranslateCoordinates",
        C.WINDOW_GEOMETRY,
        Tier.COMPOSITOR,
        "geometry",
        "No protocol reports this",
    ),
    _f("MoveWindow", "XMoveWindow", C.WINDOW_PLACEMENT, Tier.COMPOSITOR, "move_window"),
    _f(
        "ResizeWindow",
        "XResizeWindow",
        C.WINDOW_RESIZE,
        Tier.COMPOSITOR,
        "resize_window",
    ),
    _f(
        "RaiseWindow",
        "XRaiseWindow",
        C.WINDOW_ACTIVATE,
        Tier.COMPOSITOR,
        "activate_window",
        "Also transfers focus; no raise-only operation",
    ),
    _f(
        "SetInputFocus",
        "XSetInputFocus",
        C.WINDOW_ACTIVATE,
        Tier.COMPOSITOR,
        "activate_window",
        "May be refused under focus-stealing policy",
    ),
    _f(
        "GetInputFocus",
        "XGetInputFocus",
        C.WINDOW_STATE,
        Tier.COMPOSITOR,
        "active_window",
        "wlroots activated flag; absent from ext-foreign-toplevel",
    ),
    _f(
        "IconifyWindow",
        "XIconifyWindow",
        C.WINDOW_MINIMIZE,
        Tier.COMPOSITOR,
        "minimize_window",
    ),
    _f(
        "UnIconifyWindow",
        "XMapWindow",
        C.WINDOW_MINIMIZE,
        Tier.COMPOSITOR,
        "minimize_window",
        "minimize_window(window, minimized=False)",
    ),
    _f(
        "GetWindowFromPoint",
        "stacking-order scan",
        C.WINDOW_AT_POINT,
        Tier.COMPOSITOR,
        "window_at",
        "Needs geometry and stacking order",
    ),
    _f(
        "ClickWindow",
        "GetWindowPos + move + click",
        C.WINDOW_GEOMETRY,
        Tier.COMPOSITOR,
        "Element.click",
        "Prefer element(...).click(); inherits both gaps, AT-SPI needs neither. "
        "The original clicked the window's top-left corner (GetWindowPos) "
        "plus the x/y offsets, M_LEFT by default, and returned 1 or 0; the "
        "port is `x, y, _, _ = gui.geometry(w); gui.move_mouse(x + dx, y + "
        "dy); gui.click()`. That corner can be a decoration, not the content",
    ),
    # -- T5: served by AT-SPI instead --------------------------------------
    _f(
        "GetRootWindow",
        "RootWindow()",
        C.ELEMENT_TREE,
        Tier.REWORK,
        "root_element",
        "No root window exists; this is the accessible-tree root, not a window",
    ),
    _f(
        "GetChildWindows",
        "recursive XQueryTree",
        C.ELEMENT_TREE,
        Tier.REWORK,
        "Element.children",
        "Roles and labels, not anonymous ids",
    ),
    _f(
        "GetParentWindow",
        "XQueryTree parent",
        C.ELEMENT_TREE,
        Tier.REWORK,
        "Element.parent",
    ),
    _f(
        "IsChild",
        "scan GetChildWindows",
        C.ELEMENT_TREE,
        Tier.REWORK,
        "Element.is_ancestor_of",
    ),
    # -- T6: no path -------------------------------------------------------
    _f(
        "GetMousePos",
        "XQueryPointer",
        C.POINTER_QUERY,
        Tier.NO_PATH,
        "pointer_position",
        "X11, Windows and macOS; no Wayland compositor reports it, and an "
        "XWayland answer can be silently stale -- prefer window_at/element_at "
        "to check where a click will land",
    ),
    _f(
        "IsKeyPressed",
        "XQueryKeymap",
        C.INPUT_STATE_QUERY,
        Tier.NO_PATH,
        "is_key_pressed",
        "X11 and Windows; global input state is what a keylogger reads, so no "
        "Wayland compositor exposes it",
    ),
    _f(
        "IsMouseButtonPressed",
        "XQueryPointer mask",
        C.INPUT_STATE_QUERY,
        Tier.NO_PATH,
        "is_button_pressed",
        "X11 and Windows, as above; same button numbering as click()",
    ),
    _f(
        "SetWindowName",
        "XSetWMName",
        C.WINDOW_TITLE_SET,
        Tier.NO_PATH,
        "set_window_title",
        "X11 and Windows; a title belongs to the client that owns the "
        "surface, so no Wayland protocol offers it -- and the application may "
        "rename it back",
    ),
    _f(
        "LowerWindow",
        "XLowerWindow",
        C.WINDOW_LOWER,
        Tier.NO_PATH,
        "lower_window",
        "X11 and Windows; no restack operation in any foreign-toplevel "
        "protocol, which exposes activate and close only",
    ),
    _f(
        "IsWindowCursor",
        "XTestCompareCursorWithWindow",
        C.WINDOW_CURSOR_QUERY,
        Tier.NO_PATH,
        "is_window_cursor",
        "X11 and Windows; `shape` is the raw cursor-font number the XC_* "
        "constants carried, which this package does not re-export. No "
        "Wayland workaround anywhere",
    ),
]

LEGACY = {f.name: f for f in _ALL}


# The `:CONST` tag, which `@EXPORT_OK` does not carry and the table above
# therefore does not cover. A script written against `qw(:ALL)` uses these
# as freely as the functions, and every one of them is a plain integer with
# no pyguitest spelling: the button numbers are passed to click() directly,
# the cursor shapes to is_window_cursor(), and the one timeout became an
# argument. Keyed by the name as written, except the ~85 cursor shapes,
# which share the `XC_*` entry -- their values are X11 cursor-font offsets
# and only X11 can compare one anyway.
CONSTANTS = {
    "DEF_WAIT": (
        "an explicit timeout",
        "The module's 10-second default for the WaitWindow* family. A "
        "pyguitest `timeout=None` waits indefinitely instead, so pass the "
        "number rather than relying on a default",
    ),
    "M_LEFT": ("1", "click(1) -- the default, so usually nothing to write"),
    "M_MIDDLE": ("2", "click(2)"),
    "M_RIGHT": ("3", "click(3)"),
    "M_BTN1": ("1", "click(1)"),
    "M_BTN2": ("2", "click(2)"),
    "M_BTN3": ("3", "click(3)"),
    "M_UP": (
        "scroll(dy=1)",
        "A wheel detent, not a button: X11 button 4 is now an axis event",
    ),
    "M_DOWN": (
        "scroll(dy=-1)",
        "A wheel detent, not a button: X11 button 5 is now an axis event",
    ),
    "M_BTN4": ("scroll(dy=1)", "As M_UP -- see the scroll() docstring on sign"),
    "M_BTN5": ("scroll(dy=-1)", "As M_DOWN"),
    "XC_*": (
        "the same integer",
        "Cursor-font shape numbers, passed through to is_window_cursor() "
        "unchanged; this package does not re-export the ~85 names, so keep "
        "the number (XC_WATCH is 150) or define your own constant",
    ),
}


def constant(name: str) -> tuple[str, str] | None:
    """What one `:CONST` symbol becomes, or None if it is not one of them.

    Folds every `XC_<shape>` onto the single `XC_*` entry, which is how the
    cursor shapes are tabulated -- there are ~85 of them, they differ only
    in the integer, and the answer is the same for all.
    """
    if name.startswith("XC_"):
        name = "XC_*"
    return CONSTANTS.get(name)


def by_tier(tier: Tier) -> list[LegacyFunction]:
    """Every legacy function in one tier."""
    return [f for f in _ALL if f.tier == tier]


def unavailable() -> list[LegacyFunction]:
    """The functions with no Wayland path, X11 and Windows only.

    Each one does have a replacement -- `pointer_position`, `is_key_pressed`,
    `is_button_pressed`, `set_window_title`, `lower_window`,
    `is_window_cursor` -- which raises CapabilityUnsupported on Wayland, by
    design rather than for want of an implementation. For the calls with no
    spelling at all, see `unported`.
    """
    return by_tier(Tier.NO_PATH)


def unported() -> list[LegacyFunction]:
    """The functions this package does not spell at all, on any backend.

    Two, and both for reasons the table records: `GetScreenDepth` asks a
    question with one answer everywhere, and `GetWindowsFromPid` has no
    `pid=` filter yet, though a comprehension over `windows()` does it.
    """
    return [f for f in _ALL if f.replacement is None]


def portable() -> list[LegacyFunction]:
    """The functions that port unchanged, with no display server involved."""
    return by_tier(Tier.PORTABLE)
