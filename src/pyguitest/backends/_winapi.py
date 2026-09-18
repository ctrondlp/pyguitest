"""The Windows API: what is loaded, how it is declared, and the structures.

One place, for two reasons. A prototype is a claim about a signature -- wrong
`argtypes` truncate a pointer or a 64-bit handle, and the symptom appears
somewhere else -- so every one of them is here, where a reader can check it
against Microsoft's documentation in a single pass. And it is the seam the
tests drive: `tests/test_win32.py` replaces `user32()` or `kernel32()` with a
fake and asserts the calls, the same shape as `tests/test_x11.py` faking the
`Xlib` modules in `sys.modules`.

Nothing here runs at import time. The DLLs are loaded on first use, through
accessors, because `import pyguitest` has to work on Linux and macOS -- where
`ctypes` has no `WinDLL` at all -- and because a DLL that will not load is a
condition to report rather than an ImportError to raise. `available()` is the
question a backend factory asks first.

The structures are declared on every platform, deliberately: `ctypes` computes
offsets at runtime from the declared field types, so `sizeof(INPUT)` and the
position of `KEYBDINPUT` inside it can be asserted on Linux. One documented
layout -- 40 bytes on 64-bit Windows, 28 on 32-bit, with the union following the
`type` field at pointer alignment (offset 8 where pointers are 8 bytes, 4 where
they are not) -- is what a transcription has to get right, and a test that pins
it is the cheapest possible check of that.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

__all__ = [
    "available",
    "user32",
    "kernel32",
    "gdi32",
    "shcore",
    "dwmapi",
    "get_window_ex_style",
    "set_thread_dpi_awareness",
    "wide_string",
    "MAKEINTRESOURCE",
    "INPUT",
    "KEYBDINPUT",
    "MOUSEINPUT",
    "HARDWAREINPUT",
    "MONITORINFOEXW",
    "CURSORINFO",
    "BITMAPINFOHEADER",
    "MSG",
    "WNDENUMPROC",
    "MONITORENUMPROC",
    "WINEVENTPROC",
]

# -- constants -------------------------------------------------------------
#
# Named as the documentation names them, because a reader checking one against
# the SDK should not have to translate first. Grouped by the calls that use
# them.

WM_SETTEXT = 0x000C
"""Set a control's text from another process, where `SetWindowTextW` is for
top-level windows. Both work cross-process; the difference is which one a
given window class responds to."""

SMTO_ABORTIFHUNG = 0x0002
"""`SendMessageTimeout`'s flag for "give up if the target is not pumping
messages". The platform expects applications to hang, and this is the one
setting that turns a hung target from a hung caller into an error."""

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2
"""`INPUT.type`'s values, which say which member of the union is live."""


GWL_EXSTYLE = -20
"""`GetWindowLongPtr`'s index for a window's extended style."""

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
"""The two extended styles that decide whether a window belongs in a task
list: a tool window is a palette or a floating toolbar and never does, and an
owned window only does when it says so."""

GW_OWNER = 4
"""`GetWindow`'s index for a window's owner, which is what separates a dialog
from a toplevel in `EnumWindows`' output."""

GA_ROOT = 2
"""`GetAncestor`'s flag for the top-level ancestor -- how a point becomes a
window rather than a control inside one."""

SW_MINIMIZE = 6
SW_RESTORE = 9
"""`ShowWindow`'s commands: minimize, and restore whatever the window was
before it was minimized."""

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
"""`SetWindowPos`'s flags. NOACTIVATE is on every call made from here: moving
or resizing a window must not steal focus, which is the rule the Wayland
backends follow too, and the opposite of what `activate_window` is for."""

HWND_BOTTOM = 1
"""`SetWindowPos`'s "put it at the back", as a pseudo-handle cast to HWND."""

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
"""The virtual desktop's bounding box -- every monitor's union, whose origin
is negative when a monitor sits left of or above the primary. This is the
rectangle `SendInput`'s normalized coordinates are mapped onto."""

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
"""The mouse-input flags. VIRTUALDESK is what makes ABSOLUTE's 0-65535 range
cover every monitor instead of the primary one; without it, a coordinate on a
second monitor lands somewhere else entirely."""

WHEEL_DELTA = 120
"""One wheel detent, in the units `MOUSEINPUT.mouseData` counts in -- the
Windows counterpart of the detent `scroll()` is documented in."""

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
"""`mouseData`'s value for the two extra buttons, on an XDOWN/XUP event."""

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
"""The virtual keys that answer "is this held right now": `GetAsyncKeyState`
takes a virtual key, and these are the ones a button or a modifier needs. The
side-agnostic codes are deliberate -- a caller asking whether Shift is held
means either Shift."""

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
"""The keyboard-input flags. UNICODE is the route `type_text` uses: the code
unit goes in `wScan` and the system synthesises a keystroke from it, so the
active layout is never consulted."""

CF_UNICODETEXT = 13
"""The clipboard format this package reads and writes: UTF-16, NUL-
terminated."""

GMEM_MOVEABLE = 0x0002
"""The allocation flag `SetClipboardData` requires -- the system takes
ownership of a movable block, not of a fixed one."""

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
"""`BitBlt`'s raster operation, and the flag that includes layered windows in
the result. Without CAPTUREBLT a screenshot silently omits every window drawn
with `WS_EX_LAYERED`, which is most modern application windows."""

DIB_RGB_COLORS = 0
BI_RGB = 0
"""`GetDIBits`'s colour-table convention and the uncompressed DIB format, the
pair that makes the returned buffer plain BGR rows."""

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
"""The thread DPI-awareness context a backend sets around its own calls. A
negative pseudo-handle, like the `-1`..`-5` values `session.py` names, so it
cannot collide with a real handle."""

MDT_EFFECTIVE_DPI = 0
"""`GetDpiForMonitor`'s DPI type. The effective DPI is the one that scales
what the user sees; the raw one ignores the scale factor they chose."""

DWMWA_CLOAKED = 14
"""`DwmGetWindowAttribute`'s attribute for cloaking: a suspended UWP
application's windows stay enumerable and visible by every style test while
being nowhere on screen. This is the only way to tell."""

MONITORINFOF_PRIMARY = 0x00000001
"""`MONITORINFOEXW.dwFlags`'s bit for the primary monitor -- the monitor Windows
has and Wayland has no concept of, and therefore what `Screen` index 0 is on
this platform."""

WM_QUIT = 0x0012
"""What `PostThreadMessageW` posts to end the event-hook thread's message
loop: `GetMessageW` returns 0 for this message and nothing else, which is
the documented way to stop a message-only thread from outside it."""

EVENT_SYSTEM_FOREGROUND = 0x0003
"""`SetWinEventHook`'s event for a change of foreground window -- the
`WindowEvent(change="focus")` source."""

EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
"""One contiguous range, covering the `WindowEvent(change="new")` and
`change="close"` sources. Windows fires both for every window-class object on
the desktop, not only the toplevels this package lists -- `OBJID_WINDOW` and
`CHILDID_SELF` below narrow that to whole windows rather than their controls,
and `Win32Backend._is_listable` narrows it the rest of the way."""

EVENT_OBJECT_NAMECHANGE = 0x800C
"""The `WindowEvent(change="title")` source. Fires for a caption change on
any window-class object, with the same over-broad reach `EVENT_OBJECT_CREATE`
has."""

OBJID_WINDOW = 0x00000000
CHILDID_SELF = 0
"""What a `WinEventProc`'s `idObject`/`idChild` mean when the event is about
the window itself rather than one of its accessible children -- a button
inside a dialog is its own `HWND` and generates its own `OBJID_WINDOW` events,
so this pair is what tells "the window" from "something inside it", not what
tells one window from another."""

WINEVENT_OUTOFCONTEXT = 0x0000
"""`SetWinEventHook`'s flag for an out-of-process hook: no DLL is injected
into the target, and the callback is invoked on the *hooking* thread, but
only while that thread calls `GetMessageW` or `PeekMessageW` -- which is why
installing one commits this backend to running a message loop for as long as
a caller is subscribed."""

WINEVENT_SKIPOWNPROCESS = 0x0002
"""`SetWinEventHook`'s flag for "never call me about my own process's
windows". This package does not normally open any, so this mostly guards
against a future one causing its own event stream to loop back on itself."""

PM_NOREMOVE = 0x0000
"""`PeekMessageW`'s flag for "look, do not take": a thread's message queue is
created lazily on its first `GetMessageW`/`PeekMessageW` call, and
`PostThreadMessageW` documents that it fails silently against a thread that
has not made one yet. The event pump calls `PeekMessageW` once, before
signalling that it is ready, purely to force that queue into existence
deterministically -- so a `PostThreadMessageW(WM_QUIT, ...)` sent the moment
a caller decides to stop is never a race against the pump's first real
`GetMessageW`."""


IDC_ARROW = 32512
IDC_IBEAM = 32513
IDC_WAIT = 32514
IDC_CROSS = 32515
IDC_UPARROW = 32516
IDC_SIZENWSE = 32642
IDC_SIZENESW = 32643
IDC_SIZEWE = 32644
IDC_SIZENS = 32645
IDC_SIZEALL = 32646
IDC_NO = 32648
IDC_HAND = 32649
IDC_APPSTARTING = 32650
IDC_HELP = 32651
"""The system cursors `LoadCursorW(NULL, ...)` resolves without a resource.
They are what `is_window_cursor` compares against: a cursor's identity is its
handle, and these are the only handles obtainable without loading a scheme."""

CURSOR_SHOWING = 0x00000001
"""`CURSORINFO.flags`' bit for "a cursor is being displayed", as opposed to
the pointer merely being somewhere."""


# -- structures ------------------------------------------------------------
#
# A union's member order is the SDK's, not a preference: it decides nothing at
# runtime (every member starts at the same offset) but it is what a reader
# compares against the header.
#
# The fixed-width aliases below exist because `ctypes.wintypes` is only right
# *on Windows*. On Linux it defines DWORD as `c_ulong` and LONG as `c_long`,
# which are 8 bytes there against Windows' 4, so a structure built from them
# lays out differently here than on the machine it describes -- measured, not
# assumed: INPUT came out 56 bytes instead of 40 before this was fixed, and a
# declaration that only checks out on one platform is not a declaration of the
# Windows ABI. Every field that is not a pointer is one of these instead, so
# the layout is the same everywhere and can be asserted anywhere.

INT32 = ctypes.c_int32
"""Windows' `LONG`: 32 bits, signed."""

UINT32 = ctypes.c_uint32
"""Windows' `DWORD`: 32 bits, unsigned."""

UINT16 = ctypes.c_uint16
"""Windows' `WORD`: 16 bits, unsigned."""

WCHAR = ctypes.c_uint16
"""A wide character as Windows means it: one UTF-16 code unit. `ctypes.c_wchar`
is four bytes on Linux, which is one more reason not to use it in a layout
anything depends on."""

DWORD = wintypes.DWORD
"""Windows' `DWORD` as `wintypes` declares it -- one of the two it is written
*two ways* in this module, because `UINT32` above is a struct field and this is
an out-parameter: `GetDpiForMonitor` writes through a `POINTER(DWORD)`, and
`byref` type-matches on the exact ctypes type rather than on the size."""


def MAKEINTRESOURCE(value):
    r"""An integer resource id as the `LPCWSTR` an API expects.

    Windows overloads one parameter with two meanings -- `LoadCursorW`'s second
    argument is either a string or a small integer -- and the documented idiom
    for the integer form is a cast rather than a second function. `\#define
    MAKEINTRESOURCE(i)` is a macro in C, so the cast is what stands in for it
    here; the low word carries the id and the high word is zero.
    """
    return ctypes.cast(ctypes.c_void_p(value), ctypes.c_wchar_p)


class POINT(ctypes.Structure):
    """An x/y pair, in physical pixels."""

    _fields_ = [("x", INT32), ("y", INT32)]


class RECT(ctypes.Structure):
    """A rectangle, left/top inclusive and right/bottom exclusive."""

    _fields_ = [
        ("left", INT32),
        ("top", INT32),
        ("right", INT32),
        ("bottom", INT32),
    ]


class MONITORINFOEXW(ctypes.Structure):
    """One monitor: both its rectangles, its flags, and its device name.

    `rcMonitor` is the whole screen; `rcWork` excludes the taskbar and any
    other app bar, which is what answers "where will a window actually be
    allowed to sit". The wide variant is the one carrying `szDevice`, whose
    size is fixed by the SDK at 32 wide characters.
    """

    _fields_ = [
        ("cbSize", UINT32),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", UINT32),
        ("szDevice", WCHAR * 32),
    ]


class MOUSEINPUT(ctypes.Structure):
    """A mouse event: absolute or relative position, buttons, wheel."""

    _fields_ = [
        ("dx", INT32),
        ("dy", INT32),
        ("mouseData", UINT32),
        ("dwFlags", UINT32),
        ("time", UINT32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    """A keyboard event: a virtual key, a scan code or a Unicode unit.

    Which of the three depends on `dwFlags`, and all three go in the same two
    fields -- `wVk` and `wScan`, one of which the corresponding flag makes
    authoritative.
    """

    _fields_ = [
        ("wVk", UINT16),
        ("wScan", UINT16),
        ("dwFlags", UINT32),
        ("time", UINT32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """A hardware event. Declared so the union matches, never constructed."""

    _fields_ = [
        ("uMsg", UINT32),
        ("wParamL", UINT16),
        ("wParamH", UINT16),
    ]


class _INPUTUNION(ctypes.Union):
    """`INPUT`'s payload: exactly one of the three event kinds."""

    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """One event for `SendInput`, tagged by `type`.

    The union is anonymous, so `_anonymous_` lets a caller write
    `event.ki.wVk` rather than `event.u.ki.wVk`. 40 bytes on 64-bit Windows and
    28 on 32-bit, the difference being `dwExtraInfo`'s pointer width -- which
    is also what the union's own offset follows: 8 where pointers are 8 bytes
    and 4 where they are not, never one number for both. A test asserts each.
    """

    _anonymous_ = ("u",)
    _fields_ = [("type", UINT32), ("u", _INPUTUNION)]


class CURSORINFO(ctypes.Structure):
    """What cursor is being shown, and where the pointer is."""

    _fields_ = [
        ("cbSize", UINT32),
        ("flags", UINT32),
        ("hCursor", ctypes.c_void_p),
        ("ptScreenPos", POINT),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    """The DIB header `GetDIBits` fills in and returns pixels under.

    A negative `biHeight` would mean top-down rows; this asks for the
    positive, bottom-up form and reverses the rows on the way to PNG, because
    that is what the documentation calls the compatible default.
    """

    _fields_ = [
        ("biSize", UINT32),
        ("biWidth", INT32),
        ("biHeight", INT32),
        ("biPlanes", UINT16),
        ("biBitCount", UINT16),
        ("biCompression", UINT32),
        ("biSizeImage", UINT32),
        ("biXPelsPerMeter", INT32),
        ("biYPelsPerMeter", INT32),
        ("biClrUsed", UINT32),
        ("biClrImportant", UINT32),
    ]


class MSG(ctypes.Structure):
    """One message from a thread's queue, as `GetMessageW` fills it in.

    Declared for the window-events hook's message loop alone: nothing here
    reads a field of it, because a `WinEventProc` callback fires as a side
    effect of the pump calling `GetMessageW`, not through a message this
    struct carries. The loop pumps the queue so the callback can run, and
    `DispatchMessageW`/`TranslateMessage` take this by pointer because the
    documented loop shape asks for all three regardless of whether the
    message itself is ever inspected. `wParam`/`lParam` are pointer-width
    (`WPARAM`/`LPARAM`), which is why they are `c_size_t`/`c_ssize_t` rather
    than one of the fixed 32-bit aliases the rest of this module prefers --
    the same reasoning `set_thread_dpi_awareness`'s context value uses.
    """

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", UINT32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", UINT32),
        ("pt", POINT),
    ]


LPRECT = ctypes.POINTER(RECT)
LPPOINT = ctypes.POINTER(POINT)
LPINPUT = ctypes.POINTER(INPUT)
LPVOID = ctypes.c_void_p
LPDWORD = ctypes.POINTER(wintypes.DWORD)

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
"""The calling convention these callbacks must have.

`WINFUNCTYPE` exists on Windows only, and this module has to import
everywhere -- so the stdcall type is preferred where it is defined and the
cdecl one keeps the declaration working on Linux, where nothing calls it.
"""

WNDENUMPROC = _FUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
"""`EnumWindows`' callback: a window handle and the caller's value."""

MONITORENUMPROC = _FUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, LPRECT, wintypes.LPARAM
)
"""`EnumDisplayMonitors`' callback: a monitor handle and its rectangle."""

WINEVENTPROC = _FUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    ctypes.c_long,
    ctypes.c_long,
    wintypes.DWORD,
    wintypes.DWORD,
)
"""`SetWinEventHook`'s callback: (hook, event, hwnd, idObject, idChild,
idEventThread, dwmsEventTime), returning nothing. `idObject`/`idChild` are
`LONG` -- signed -- unlike every other parameter here, because `OBJID_*`
includes negative values this package never asks for but the signature has
to admit."""


# -- loading ---------------------------------------------------------------

_DLLS: dict = {}
"""Loaded DLLs, keyed by name. One handle each, and the prototypes below are
declared on the way in rather than on every call."""


def _load(name):
    """Load one DLL, or None where that is not possible.

    `WinDLL` is absent off Windows, which is why this is asked with `getattr`
    rather than through a platform test: a host with no Windows API at all
    answers "not here" instead of raising.
    """
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        return loader(name)
    except OSError:
        return None


def _declare_user32(lib):
    """Declare the user32 prototypes this package uses."""
    lib.EnumDisplayMonitors.argtypes = (
        wintypes.HDC,
        LPRECT,
        MONITORENUMPROC,
        wintypes.LPARAM,
    )
    lib.EnumDisplayMonitors.restype = wintypes.BOOL
    lib.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW))
    lib.GetMonitorInfoW.restype = wintypes.BOOL
    lib.GetSystemMetrics.argtypes = (ctypes.c_int,)
    lib.GetSystemMetrics.restype = ctypes.c_int
    lib.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
    lib.EnumWindows.restype = wintypes.BOOL
    lib.GetForegroundWindow.restype = wintypes.HWND
    lib.SetForegroundWindow.argtypes = (wintypes.HWND,)
    lib.SetForegroundWindow.restype = wintypes.BOOL
    lib.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    lib.GetWindow.restype = wintypes.HWND
    lib.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    lib.GetAncestor.restype = wintypes.HWND
    # 32-bit Windows has no GetWindowLongPtrW -- the Long form is the same
    # call there, and a style value cannot exceed 32 bits on that platform.
    getter = getattr(lib, "GetWindowLongPtrW", None) or lib.GetWindowLongW
    getter.argtypes = (wintypes.HWND, ctypes.c_int)
    getter.restype = ctypes.c_ssize_t
    lib.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    lib.SetWindowPos.restype = wintypes.BOOL
    lib.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    lib.ShowWindow.restype = wintypes.BOOL
    lib.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    lib.SetWindowTextW.restype = wintypes.BOOL
    lib.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    lib.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    lib.WindowFromPoint.argtypes = (POINT,)
    lib.WindowFromPoint.restype = wintypes.HWND
    lib.GetCursorPos.argtypes = (LPPOINT,)
    lib.GetCursorPos.restype = wintypes.BOOL
    lib.GetCursorInfo.argtypes = (ctypes.POINTER(CURSORINFO),)
    lib.GetCursorInfo.restype = wintypes.BOOL
    lib.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    lib.GetAsyncKeyState.restype = ctypes.c_short
    lib.LoadCursorW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
    lib.LoadCursorW.restype = wintypes.HANDLE
    lib.SendInput.argtypes = (wintypes.UINT, LPINPUT, ctypes.c_int)
    lib.SendInput.restype = wintypes.UINT
    # c_ssize_t rather than a handle type: the context is a *negative*
    # pseudo-handle, and an unsigned pointer type turns -4 into a huge value
    # that no longer means what the constant says.
    #
    # getattr rather than a bare attribute: this export arrived in Windows 10
    # 1607, and `WinDLL` raises AttributeError for a symbol a DLL does not
    # have -- which would take the whole backend down on an older machine for
    # a call that only improves the numbers. The backend asks whether the
    # declaration happened before setting anything.
    dpi_context = getattr(lib, "SetThreadDpiAwarenessContext", None)
    if dpi_context is not None:
        dpi_context.argtypes = (ctypes.c_ssize_t,)
        dpi_context.restype = ctypes.c_ssize_t
    lib.GetDC.argtypes = (wintypes.HWND,)
    lib.GetDC.restype = wintypes.HDC
    lib.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    lib.ReleaseDC.restype = ctypes.c_int
    lib.GetWindowRect.argtypes = (wintypes.HWND, LPRECT)
    lib.GetWindowRect.restype = wintypes.BOOL
    lib.GetWindowThreadProcessId.argtypes = (wintypes.HWND, LPDWORD)
    lib.GetWindowThreadProcessId.restype = wintypes.DWORD
    lib.IsWindowVisible.argtypes = (wintypes.HWND,)
    lib.IsWindowVisible.restype = wintypes.BOOL
    lib.IsWindow.argtypes = (wintypes.HWND,)
    lib.IsWindow.restype = wintypes.BOOL
    lib.IsIconic.argtypes = (wintypes.HWND,)
    lib.IsIconic.restype = wintypes.BOOL
    lib.OpenClipboard.argtypes = (wintypes.HWND,)
    lib.OpenClipboard.restype = wintypes.BOOL
    lib.CloseClipboard.argtypes = ()
    lib.CloseClipboard.restype = wintypes.BOOL
    lib.EmptyClipboard.argtypes = ()
    lib.EmptyClipboard.restype = wintypes.BOOL
    lib.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    lib.IsClipboardFormatAvailable.restype = wintypes.BOOL
    lib.GetClipboardData.argtypes = (wintypes.UINT,)
    lib.GetClipboardData.restype = wintypes.HANDLE
    lib.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    lib.SetClipboardData.restype = wintypes.HANDLE
    lib.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    lib.GetWindowTextLengthW.restype = ctypes.c_int
    lib.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    lib.GetWindowTextW.restype = ctypes.c_int
    lib.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    lib.GetClassNameW.restype = ctypes.c_int
    lib.SetWinEventHook.argtypes = (
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
        WINEVENTPROC,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    lib.SetWinEventHook.restype = wintypes.HANDLE
    lib.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    lib.UnhookWinEvent.restype = wintypes.BOOL
    lib.GetMessageW.argtypes = (
        ctypes.POINTER(MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    )
    # BOOL in the SDK, but documented to return -1 on error as well as 0 and
    # nonzero -- c_int rather than wintypes.BOOL so that -1 survives rather
    # than becoming whatever an unsigned reading of it would be.
    lib.GetMessageW.restype = ctypes.c_int
    lib.PeekMessageW.argtypes = (
        ctypes.POINTER(MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    lib.PeekMessageW.restype = wintypes.BOOL
    lib.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
    lib.TranslateMessage.restype = wintypes.BOOL
    lib.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
    # LRESULT is pointer-width; nothing here reads it, but a truncated
    # pointer-sized return is exactly the kind of wrong `restype` this
    # module's declarations exist to avoid.
    lib.DispatchMessageW.restype = ctypes.c_ssize_t
    lib.PostThreadMessageW.argtypes = (
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    lib.PostThreadMessageW.restype = wintypes.BOOL


def _declare_kernel32(lib):
    """Declare the kernel32 prototypes this package uses."""
    lib.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    lib.GlobalAlloc.restype = wintypes.HANDLE
    lib.GlobalLock.argtypes = (wintypes.HANDLE,)
    lib.GlobalLock.restype = LPVOID
    lib.GlobalUnlock.argtypes = (wintypes.HANDLE,)
    lib.GlobalUnlock.restype = wintypes.BOOL
    lib.GlobalFree.argtypes = (wintypes.HANDLE,)
    lib.GlobalFree.restype = wintypes.HANDLE
    lib.GlobalSize.argtypes = (wintypes.HANDLE,)
    lib.GlobalSize.restype = ctypes.c_size_t
    lib.GetCurrentThreadId.argtypes = ()
    lib.GetCurrentThreadId.restype = wintypes.DWORD


def _declare_gdi32(lib):
    """Declare the gdi32 prototypes the screen capture needs."""
    lib.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    lib.CreateCompatibleDC.restype = wintypes.HDC
    lib.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    lib.CreateCompatibleBitmap.restype = wintypes.HANDLE
    lib.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
    lib.SelectObject.restype = wintypes.HANDLE
    lib.BitBlt.argtypes = (
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    )
    lib.BitBlt.restype = wintypes.BOOL
    lib.GetDIBits.argtypes = (
        wintypes.HDC,
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.UINT,
        LPVOID,
        ctypes.POINTER(BITMAPINFOHEADER),
        wintypes.UINT,
    )
    lib.GetDIBits.restype = ctypes.c_int
    lib.DeleteObject.argtypes = (wintypes.HANDLE,)
    lib.DeleteObject.restype = wintypes.BOOL
    lib.DeleteDC.argtypes = (wintypes.HDC,)
    lib.DeleteDC.restype = wintypes.BOOL


def _declare_shcore(lib):
    """Declare `GetDpiForMonitor`, which is what `Screen.scale` divides by 96."""
    lib.GetDpiForMonitor.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        LPDWORD,
        LPDWORD,
    )
    lib.GetDpiForMonitor.restype = ctypes.c_long


def _declare_dwmapi(lib):
    """Declare the Desktop Window Manager calls this package makes.

    Only cloaking so far: `DWMWA_EXTENDED_FRAME_BOUNDS` is the other
    candidate, and it exists to answer a question `WINDOW_CAPTURE` will ask
    rather than one `geometry()` asks.
    """
    lib.DwmGetWindowAttribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        LPVOID,
        wintypes.DWORD,
    )
    lib.DwmGetWindowAttribute.restype = ctypes.c_long


_DECLARATIONS = {
    "user32": _declare_user32,
    "kernel32": _declare_kernel32,
    "gdi32": _declare_gdi32,
    "shcore": _declare_shcore,
    "dwmapi": _declare_dwmapi,
}
"""Library name -> the function that declares its prototypes."""


def _dll(name):
    """One DLL, loaded and declared on first use, or None.

    The result is cached either way, including a failure: a machine without
    `shcore.dll` should not have every DPI question retry a failing load.
    """
    if name not in _DLLS:
        library = _load(name)
        if library is not None:
            _DECLARATIONS[name](library)
        _DLLS[name] = library
    return _DLLS[name]


def user32():
    """user32.dll: windows, monitors, input injection, cursors, clipboard."""
    return _dll("user32")


def kernel32():
    """kernel32.dll: the global memory the clipboard is handed."""
    return _dll("kernel32")


def gdi32():
    """gdi32.dll: the blit and the DIB the screen capture goes through."""
    return _dll("gdi32")


def shcore():
    """shcore.dll: `GetDpiForMonitor`. Absent before Windows 8.1."""
    return _dll("shcore")


def dwmapi():
    """dwmapi.dll: window attributes the window manager owns, cloaking first."""
    return _dll("dwmapi")


def available():
    """Whether the Windows API is here at all.

    Asked by the backend factory before anything is constructed. Only user32
    is required: every Windows machine has it, and the graphics, DPI and
    window-manager libraries are reached lazily so that one missing there
    costs a capability rather than the whole backend.
    """
    return user32() is not None


def wide_string(buffer):
    """A fixed-size wide-character buffer as a `str`, up to its NUL.

    Written out rather than done with `ctypes.wstring_at`, whose character
    width is `wchar_t`'s and is therefore four bytes on Linux -- a device name
    read that way on this machine would be nonsense, which is exactly the kind
    of thing that makes a layout test meaningless. Every unit here is one UTF-16
    unit, which is what the structures hold; a surrogate pair would come back as
    two characters, and no device name or class name contains one.
    """
    chars = []
    for unit in buffer:
        if not unit:
            break
        chars.append(chr(unit))
    return "".join(chars)


def get_window_ex_style(hwnd):
    """A window's extended style, through whichever getter this Windows has.

    The one call with a 32-bit fallback, kept here so the fallback is written
    once: `GetWindowLongPtrW` does not exist on 32-bit Windows and
    `GetWindowLongW` is the same call there, since a style value cannot exceed
    32 bits anyway. Returns 0 where user32 is absent, which is not a claim
    about the window -- callers reach this only after `available()` said yes.
    """
    lib = user32()
    if lib is None:
        return 0
    getter = getattr(lib, "GetWindowLongPtrW", None) or lib.GetWindowLongW
    return getter(hwnd, GWL_EXSTYLE)


def set_thread_dpi_awareness():
    """Make this thread's coordinates physical, per-monitor-v2 aware.

    Both Windows backends call this at the same point in their construction,
    because both answer in, and are asked in, physical pixels: `win32` mixes
    `GetWindowRect`/`GetCursorPos`/`GetMonitorInfo` with `SendInput` -- which
    is *always* physical -- and `uia` reports UIA's bounding rectangles and
    accepts `ElementFromPoint` coordinates in the same space. A thread that
    has not said which it is gets a virtualised desktop, so the numbers it
    reads are internally consistent and not pixels; asking here removes that
    whole class of quiet wrongness for both, and for a session that composes
    them.

    Per thread rather than process-wide: `SetProcessDpiAwarenessContext`
    cannot be undone, and it re-lays-out the *host application's* own windows,
    so a library that called it during `connect()` would be changing the
    meaning of the program that imported it. A caller who needs the same
    numbers in their own code sets the process-wide context themselves, before
    creating any window -- see docs/developers/adr-003-windows.md for the
    alternatives and why this one was chosen.

    Failures are swallowed, and there is one honest reason: the call arrived in
    Windows 10 1607, and this module deliberately does not declare it where the
    export is missing. Nothing here depends on it -- every call is physical
    either way on an unscaled display -- and refusing to construct a backend on
    an older machine over a thread setting would cost every capability for a
    rounding difference.
    """
    lib = user32()
    if lib is None:
        return
    setter = getattr(lib, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        return
    try:
        setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except OSError:
        return
