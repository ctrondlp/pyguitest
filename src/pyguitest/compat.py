"""The X11::GUITest 0.29 migration table, as data.

All 50 symbols from `@EXPORT_OK`, each mapped to the capability that serves it
and the tier it lands in on Wayland. This is the audit in machine-readable
form: docs/wayland-audit.html is the same table with the reasoning.

It exists so migration tooling can answer "what happens to my script?" without
a human re-reading the audit:

    from pyguitest.compat import LEGACY, unavailable
    LEGACY["GetMousePos"].tier          -> Tier.NO_PATH
    [f.name for f in unavailable()]     -> the 6 to remove

Nothing here is executable. `replacement` names the new API where one is
planned, or None where the operation is being dropped.
"""

from __future__ import annotations

from collections import namedtuple

from .capabilities import Capability as C
from .capabilities import Tier

__all__ = ["LegacyFunction", "LEGACY", "by_tier", "unavailable", "portable"]

LegacyFunction = namedtuple(
    "LegacyFunction", "name x11 capability tier replacement note"
)


def _f(name, x11, capability, tier, replacement, note=""):
    """Build a LegacyFunction, keeping the table below readable."""
    return LegacyFunction(name, x11, capability, tier, replacement, note)


_ALL = [
    # -- T1: no display server involved ------------------------------------
    _f("StartApp", "fork/exec", C.PROCESS_LAUNCH, Tier.PORTABLE, "start_app"),
    _f("RunApp", "system()", C.PROCESS_LAUNCH, Tier.PORTABLE, "run_app"),
    _f("WaitSeconds", "sleep()", C.TIMING, Tier.PORTABLE, "wait"),
    _f(
        "SetEventSendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session(event_delay=)",
        "Better as a constructor argument",
    ),
    _f(
        "GetEventSendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session.event_delay",
    ),
    _f(
        "SetKeySendDelay",
        "module global",
        C.TIMING,
        Tier.PORTABLE,
        "Session(key_delay=)",
    ),
    _f(
        "GetKeySendDelay", "module global", C.TIMING, Tier.PORTABLE, "Session.key_delay"
    ),
    _f(
        "QuoteStringForSendKeys",
        "string substitution",
        None,
        Tier.PORTABLE,
        "quote_for_type",
        "The {} grammar was kept; also now escapes # (Meta), which the "
        "original omitted",
    ),
    _f(
        "QSfSK", "alias", None, Tier.PORTABLE, "quote_for_type", "Drop the abbreviation"
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
        "Degenerate: every compositor is 8 bits per channel",
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
        "Returns only toplevels; no recursive descent",
    ),
    _f(
        "WaitWindowLike",
        "poll FindWindowLike",
        C.WINDOW_EVENTS,
        Tier.COMPOSITOR,
        "wait_for_window",
        "Session.wait_for_window works everywhere WINDOW_LIST does, polling "
        "find_windows; it only uses real event notification -- faster, "
        "race-free -- where WINDOW_EVENTS is also available",
    ),
    _f(
        "WaitWindowClose",
        "poll IsWindow",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        "wait_window_close",
        "Matches by Window.handle, not title; event-driven only where "
        "WINDOW_EVENTS is available, polling windows() otherwise",
    ),
    _f(
        "WaitWindowViewable",
        "poll XGetWindowAttributes",
        C.WINDOW_STATE,
        Tier.COMPOSITOR,
        "is_window_viewable",
        "A one-shot state read rather than a wait -- pair it with your own "
        "poll loop, or with wait_for_window/wait_window_close. kdotool has "
        "no mapped/visibility query and refuses this one specifically",
    ),
    _f(
        "IsWindow",
        "XGetWindowAttributes",
        C.WINDOW_LIST,
        Tier.COMPOSITOR,
        None,
        "No replacement yet; check windows() for the handle, or catch "
        "WindowNotFound from geometry()",
    ),
    _f(
        "IsWindowViewable",
        "IsViewable attribute",
        C.WINDOW_STATE,
        Tier.COMPOSITOR,
        None,
        "No replacement tracks a viewable flag yet",
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
        "Prefer element(...).click(); inherits both gaps, AT-SPI needs neither",
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
        None,
        "Track the position you last set",
    ),
    _f(
        "IsKeyPressed",
        "XQueryKeymap",
        C.INPUT_STATE_QUERY,
        Tier.NO_PATH,
        None,
        "Global input state is not observable",
    ),
    _f(
        "IsMouseButtonPressed",
        "XQueryPointer mask",
        C.INPUT_STATE_QUERY,
        Tier.NO_PATH,
        None,
    ),
    _f(
        "SetWindowName",
        "XSetWMName",
        C.WINDOW_TITLE_SET,
        Tier.NO_PATH,
        None,
        "A title belongs to the client that owns the surface",
    ),
    _f(
        "LowerWindow",
        "XLowerWindow",
        C.WINDOW_LOWER,
        Tier.NO_PATH,
        None,
        "No restack operation in any foreign-toplevel protocol",
    ),
    _f(
        "IsWindowCursor",
        "XTestCompareCursorWithWindow",
        C.WINDOW_CURSOR_QUERY,
        Tier.NO_PATH,
        None,
        "No workaround anywhere",
    ),
]

LEGACY = {f.name: f for f in _ALL}


def by_tier(tier: Tier) -> list[LegacyFunction]:
    """Every legacy function in one tier."""
    return [f for f in _ALL if f.tier == tier]


def unavailable() -> list[LegacyFunction]:
    """The functions with no Wayland path, which should not be reimplemented."""
    return by_tier(Tier.NO_PATH)


def portable() -> list[LegacyFunction]:
    """The functions that port unchanged, with no display server involved."""
    return by_tier(Tier.PORTABLE)
