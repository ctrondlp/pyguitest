"""Runtime environment detection.

Which mechanisms exist is not knowable at install time -- it depends on the
session type, the compositor, the portal implementation, and group membership,
all of which vary per login. Everything here is probed when the process starts.

Probes are cheap and non-invasive: no dialog is raised, no device is opened, no
D-Bus call is made. A probe reporting True means "worth attempting", not
"permission granted" -- the portal consent dialog only appears when a capability
is first exercised.
"""

from __future__ import annotations

import ctypes.util
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import tools as _tools

__all__ = [
    "SessionType",
    "Compositor",
    "Environment",
    "detect",
    "toolkit_accessibility",
    "assistive_technology_enabled",
]

_INTERFACE_SCHEMA = "org.gnome.desktop.interface"
_TOOLKIT_ACCESSIBILITY_KEY = "toolkit-accessibility"
_GSETTINGS_TIMEOUT = 5
"""Seconds to wait for `gsettings get`. Bounded because this runs inside
`doctor`/`debug`, and a diagnostic that hangs is worse than one that says
it could not tell."""


class SessionType(Enum):
    """What kind of display server the session is running."""

    WAYLAND = "wayland"
    X11 = "x11"
    XWAYLAND = "xwayland"
    """An X11 connection inside a Wayland session. XTest reaches X11 clients
    here but never native Wayland ones -- the trap the Perl module documents."""
    WIN32 = "win32"
    """A native Windows desktop, which is not a display server in the Wayland
    sense at all: there is no socket to check and no protocol to be a client
    of. Spelled the way `sys.platform` spells it, so the two connect without
    a table, and deliberately *not* "windows" -- `backends/windows.py` is
    already the compositor-IPC module for sway, Hyprland, niri and KWin,
    where "windows" means GUI windows."""
    HEADLESS = "headless"
    UNKNOWN = "unknown"


class Compositor(Enum):
    """Compositor family, which decides the window backend.

    The families differ in what they expose, not merely in branding:
    WLROOTS implements both foreign-toplevel protocols plus the unprivileged
    virtual-device protocols; KWIN implements foreign-toplevel and scripting;
    MUTTER implements neither foreign-toplevel protocol, so window work there
    needs a Shell extension.
    """

    MUTTER = "mutter"
    KWIN = "kwin"
    WLROOTS = "wlroots"
    DWM = "dwm"
    """Windows' own compositor, and the one member that is effectively a
    constant: composition cannot be turned off since Windows 8. It is
    declared rather than left to `NONE` because "a session with no
    compositor" would be false, and because `for_compositor()` branches on
    this member -- the Windows window backend is `win32`, not a
    foreign-toplevel client, and the branch has to be able to say so."""
    OTHER = "other"
    NONE = "none"


_WLROOTS_HINTS = ("sway", "hyprland", "river", "wayfire", "labwc", "niri")


def _lib(name: str) -> bool:
    """True if a shared library is loadable by name.

    Every failure is False rather than an exception, because this is a probe
    and `detect()` is documented to answer rather than raise: a library that is
    not there is exactly what False means, and no caller has anything better to
    do with a lookup that went wrong. `find_library` is best-effort by design
    and its implementation differs per platform -- on Windows it walks
    `os.environ['PATH']` and raises `KeyError` when that is unset, which is not
    hypothetical: it took down `detect()` on a real Windows 11 box the moment a
    caller ran it with a cleared environment.
    """
    try:
        return ctypes.util.find_library(name) is not None
    except Exception:  # noqa: BLE001 - a probe that failed found nothing
        return False


def _module(name: str) -> bool:
    """True if a Python module is genuinely importable.

    Not `find_spec(name) is not None`: an empty directory on sys.path becomes a
    namespace package with a spec but no loader and no contents, so the naive
    check reports success for a package that is not installed. Requiring a
    loader rejects those.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.loader is not None


def _platform() -> str:
    """`sys.platform`, read through a call rather than a module constant.

    The Windows branch has to be taken *before* any environment variable is
    read, which makes the platform the one fact about the host that
    `detect()`'s `env` argument cannot express -- and a module-level
    `sys.platform == "win32"` would decide the question at import, where no
    test could drive it from Linux. A function for the same reason
    tests/test_x11.py installs its fakes in sys.modules: patching this one
    name switches the branch, while patching `sys.platform` itself would
    change what `importlib`, `os` and `pathlib` do inside the same call.
    """
    return sys.platform


def _uinput() -> tuple[bool, bool]:
    """(present, writable) for /dev/uinput."""
    path = "/dev/uinput"
    if not os.path.exists(path):
        return False, False
    return True, os.access(path, os.W_OK)


def _has_input_group() -> bool:
    """Whether an 'input' group exists for a user to be added to.

    Asked of the machine rather than inferred from the platform, which is
    how everything else here decides things -- and it is not only the BSDs
    that lack the group; a minimal container image can too. FreeBSD has
    /dev/uinput through cuse, owned root:wheel 0600, and no 'input' group,
    so the advice built on one had nothing to attach to.

    `grp` is imported here rather than at module scope, and that is a
    portability fix as much as a local one: the module does not exist on
    Windows, so a top-level import made this file -- and therefore
    `import pyguitest` -- fail outright there, before any probe ran.
    """
    try:
        import grp
    except ImportError:
        # No Unix user database, so no group to be a member of.
        return False
    try:
        grp.getgrnam("input")  # type: ignore[attr-defined]
    except KeyError:
        return False
    return True


def _portal(env: Mapping[str, str]) -> bool:
    """Heuristic: a session bus plus an installed portal service.

    Deliberately does not call org.freedesktop.portal.Desktop -- that would
    need a D-Bus dependency, and this package has none. Takes `env` so a
    fake environment passed to detect() is honoured here too, rather than
    this one probe quietly falling back to the real process environment
    regardless of what detect() was asked to simulate.
    """
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    if shutil.which("xdg-desktop-portal"):
        return True
    return any(
        os.path.exists(p)
        for p in (
            "/usr/libexec/xdg-desktop-portal",
            "/usr/lib/xdg-desktop-portal",
            "/usr/share/xdg-desktop-portal",
        )
    )


_WINDOWS = "win32"
"""What `sys.platform` says on a native Windows session, and the string
`SessionType.WIN32` uses for the same fact."""

_WINSTA0 = "WinSta0"
"""The one window station an interactive desktop is attached to. Every other
name -- `Service-0x0-3e7$` and the rest -- belongs to a session with no
desktop to draw on, which is what session 0 isolation means."""

_UOI_NAME = 2
"""`GetUserObjectInformationW`'s index for a user object's name."""

_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
"""`OpenProcessToken`'s desired access, and `GetTokenInformation`'s class for
a token's integrity level."""

_MANDATORY_HIGH_RID = 0x3000
"""The lowest integrity level UIPI treats as elevated. A normal logged-in
process runs at medium (0x2000) and is blocked from driving an elevated
window; an elevated one runs at high or above."""

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
"""`OpenProcess`'s narrowest query right, and the one that reaches *upwards*:
it is granted for a higher-integrity process where the older
`PROCESS_QUERY_INFORMATION` is refused, which is the whole point of asking
about the window in front rather than only about this process."""

_DPI_CONTEXTS = {
    -1: "unaware",
    -2: "system",
    -3: "per-monitor",
    -4: "per-monitor-v2",
    -5: "unaware (GDI-scaled)",
}
"""`GetThreadDpiAwarenessContext`'s pseudo-handles, as the signed values they
are -- negative so that they cannot collide with a real handle."""

_PROCESS_DPI_AWARENESS = {0: "unaware", 1: "system", 2: "per-monitor"}
"""`GetProcessDpiAwareness`'s modes: the older answer, kept as a fallback for
builds that have no thread context to ask."""

_WGC_MIN_BUILD = 17134
"""Windows 10 1803, the first build with Windows.Graphics.Capture."""

_WINDOWS_11_BUILD = 22000
"""The first Windows 11 build. The version line Windows itself does not draw.

Every version floor in this package is a build number, and the marketing
version is only ever a convenience -- so this one comparison exists to stop
`Windows 10 Pro (build 22631)` being printed, which is what the registry
alone says.
"""

_LOW_LEVEL_HOOKS_DEFAULT_MS = 300
"""Windows' documented default for `LowLevelHooksTimeout`."""


def _win32_lib(name: str) -> Any:
    """Load one Windows DLL, or None where that is not possible.

    The single place a Windows function is reached through, so the loader is
    checked once. None comes back on a non-Windows host, where `ctypes` has no
    `WinDLL` attribute at all -- asked with `getattr` rather than through
    `_platform()`, so that a probe driven from Linux by a faked platform
    answers "cannot tell" instead of raising.

    No handle is held open and no function is called through the result:
    `detect()` runs inside `connect()`, `doctor` and every test process, and
    a side effect there is what this module's docstring rules out.

    `use_last_error=True` is what makes `_last_error()` trustworthy: it tells
    ctypes to swap the thread's Windows error code into a private copy around
    every call made through this library, so a caller reading it back gets the
    value *this* call set. Without it, ctypes' own machinery is free to make
    Windows calls between the failure and the read, and the documentation says
    so -- the error is then whatever happened last, which for
    `_windows_process_cpu_seconds` decides whether a live-but-protected
    process is reported as denied or silently as exited.
    """
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        return loader(name, use_last_error=True)
    except OSError:
        return None


def _last_error() -> int:
    """The last Windows error set by a call made through `_win32_lib`.

    `ctypes.get_last_error()` rather than `kernel32.GetLastError()`, and the
    difference is not stylistic: reading it back through a *second* foreign
    call is the thing Python's own documentation warns is unreliable, since
    ctypes may call Windows functions of its own in between. This reads the
    private copy ctypes saved at the moment the failing call returned -- see
    `_win32_lib`'s `use_last_error`.

    A function rather than an inline call so the tests have a seam to patch,
    the same shape `_platform` and `_win32_lib` already provide: a fake DLL
    cannot set a real thread's error code, so there is nothing for a test to
    arrange otherwise. Zero off Windows, where `get_last_error` still exists
    but nothing has ever set it.
    """
    getter = getattr(ctypes, "get_last_error", None)
    return 0 if getter is None else int(getter())


def _interactive_window_station() -> bool:
    """Whether this process is attached to the interactive window station.

    The difference this makes is between "no window matches" and "no window
    is visible from here": a process on session 0, or on a service's own
    station, can enumerate nothing at all, and nothing about the silence says
    so.

    True where the question cannot be asked. Answering False when `user32`
    will not load would raise a session-0 alarm on a machine with no window
    station API at all, and a hint pointing the wrong way is worse than no
    hint -- the rule this module already applies to `toolkit_accessibility`.
    """
    user32 = _win32_lib("user32")
    if user32 is None:
        return True
    try:
        user32.GetProcessWindowStation.restype = ctypes.c_void_p
        user32.GetUserObjectInformationW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        station = user32.GetProcessWindowStation()
        if not station:
            return True
        name = ctypes.create_unicode_buffer(64)
        needed = ctypes.c_ulong()
        found = user32.GetUserObjectInformationW(
            station, _UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)
        )
    except OSError:
        # Only OSError: both functions exist on every Windows this package can
        # run on, so an AttributeError here means a name is misspelled in this
        # file -- and swallowing that would report "cannot tell" forever while
        # nothing on a Windows box ever said why.
        return True
    if not found:
        return True
    return name.value.upper() == _WINSTA0.upper()


def _integrity_rid(buffer: Any) -> int:
    """The last subauthority of the SID inside a TOKEN_MANDATORY_LABEL.

    The buffer holds a `SID_AND_ATTRIBUTES`, so its first pointer is the SID;
    a SID's `SubAuthorityCount` is one byte at offset 1 and its
    subauthorities are 32-bit values from offset 8 on, with the integrity
    level last of all. That is why this is arithmetic on a raw buffer rather
    than a field read: no binding here declares the structure.
    """
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
    if not count:
        return 0
    last = ctypes.cast(sid + 8 + 4 * count - 4, ctypes.POINTER(ctypes.c_uint32))
    return int(last[0])


def _elevated() -> bool:
    """Whether this process runs at high integrity, which is what UIPI gates.

    Windows does not prompt when a process that is not elevated aims input at
    an elevated window: the call succeeds, and the target never sees the
    event. The caller's token is the only thing that decides it, so it is
    reported rather than worked around, and the note in `detect()` says what
    the answer means.

    The token is closed before returning -- `detect()` runs in every process
    that connects, and a handle left open is exactly the kind of side effect
    this module refuses to have. False where the token cannot be read: a
    process that cannot be asked is treated as an ordinary unelevated one,
    which is the safe direction for a note that only ever warns.
    """
    advapi32 = _win32_lib("advapi32")
    kernel32 = _win32_lib("kernel32")
    if advapi32 is None or kernel32 is None:
        return False
    token = ctypes.c_void_p()
    try:
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        advapi32.OpenProcessToken.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetTokenInformation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        opened = advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        )
        if not opened:
            return False
        size = ctypes.c_ulong()
        # The first call is the sizing one, and is expected to fail.
        advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)
        )
        buffer = ctypes.create_string_buffer(size.value)
        read = advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, buffer, size.value, ctypes.byref(size)
        )
        if not read:
            return False
        rid = _integrity_rid(buffer)
    except (OSError, ValueError):
        return False
    finally:
        if token:
            kernel32.CloseHandle(token)
    return rid >= _MANDATORY_HIGH_RID


def _foreground_is_elevated() -> bool | None:
    """Whether the foreground window's process runs at high integrity.

    The other half of `_elevated()`, and the half a caller cannot work out
    for themselves: UIPI drops input aimed at a window *more* privileged than
    the process sending it, so this process's own integrity says nothing
    about whether the window about to be driven will accept a keystroke. The
    foreground window is the best available stand-in for "the application
    under test" -- a suite normally starts one and it comes up in front --
    and it is the foreground window rather than the window a `Session` last
    touched because `detect()` runs before anything has been touched at all.

    None where the question cannot be asked: no foreground window, or a
    process that will not open for querying, which is the ordinary answer for
    a window belonging to a protected process. That is a third answer rather
    than False on purpose -- a hint gated on this has to stay silent when the
    answer is unknown instead of announcing that all is well.

    Nothing is left open: both handles are closed before returning, because
    `detect()` runs in every process that connects.
    """
    user32 = _win32_lib("user32")
    kernel32 = _win32_lib("kernel32")
    advapi32 = _win32_lib("advapi32")
    if user32 is None or kernel32 is None or advapi32 is None:
        return None
    window, process, token = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        advapi32.OpenProcessToken.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetTokenInformation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        window = user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        if window:
            user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return None
        process = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not process:
            # Access denied is the usual reason, and it is not a failure to
            # report: a protected process refusing to be asked about is not
            # the same finding as a process that is running elevated.
            return None
        if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = ctypes.c_ulong()
        # The first call is the sizing one, and is expected to fail.
        advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)
        )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, buffer, size.value, ctypes.byref(size)
        ):
            return None
        rid = _integrity_rid(buffer)
    except (OSError, ValueError):
        return None
    finally:
        if token:
            kernel32.CloseHandle(token)
        if process:
            kernel32.CloseHandle(process)
    return rid >= _MANDATORY_HIGH_RID


def _signed_pointer(value: int) -> int:
    """A ctypes `c_void_p` result read back as the signed integer it stands for.

    `c_void_p` hands values back unsigned, and several Windows APIs return
    small negative pseudo-handles through one. Written as masking arithmetic
    rather than `ctypes.c_ssize_t(value).value`, which raises `OverflowError`
    for a genuinely large handle -- the case this exists to survive.
    """
    width = 8 * ctypes.sizeof(ctypes.c_void_p)
    return value - (1 << width) if value >= (1 << (width - 1)) else value


def _dpi_context_name(user32: Any, context: int) -> str:
    """A `DPI_AWARENESS_CONTEXT` as one of `_DPI_CONTEXTS`' names, or "".

    A context is an **opaque handle**, and that is the whole reason this is a
    function rather than a dictionary lookup. The `DPI_AWARENESS_CONTEXT_*`
    constants are the small negative pseudo-handles `_DPI_CONTEXTS` is keyed
    by, but `GetThreadDpiAwarenessContext` is under no obligation to hand one
    of those *same values* back -- and does not: measured on Windows 11 build
    26200, where the thread context came back as `18` and matched none of
    them, so this probe reported "" on a machine whose awareness was perfectly
    well defined. `AreDpiAwarenessContextsEqual` is the comparison Microsoft
    documents for exactly this, and `18` compares equal to `-3` through it.

    The per-monitor pair is why the comparison has to be this one rather than
    `GetAwarenessFromDpiAwarenessContext`, which collapses per-monitor and
    per-monitor-v2 into a single `DPI_AWARENESS` value -- and telling those two
    apart is the reason `_dpi_awareness` prefers the thread context at all.

    Falls back to the direct lookup where the comparison function is missing,
    which means a Windows older than 10 1607: no context there can be anything
    but one of the documented values, because the API that introduced the
    others is the one that is absent.
    """
    comparer = getattr(user32, "AreDpiAwarenessContextsEqual", None)
    if comparer is None:
        return _DPI_CONTEXTS.get(_signed_pointer(context), "")
    comparer.argtypes = (ctypes.c_ssize_t, ctypes.c_ssize_t)
    comparer.restype = ctypes.c_int
    signed = _signed_pointer(context)
    for value, name in _DPI_CONTEXTS.items():
        if comparer(signed, value):
            return name
    return ""


def _dpi_awareness() -> str:
    """The process's DPI awareness mode as a name, or "" if unknowable.

    This is the coordinate-space question, and it fails silently either way:
    an unaware process is shown a virtualised, scaled desktop and gets scaled
    coordinates back from window queries, while `SendInput` takes physical
    pixels -- so a click that lands where it was aimed on one setting lands
    somewhere else on another, with no error in between.

    The thread context is asked first, because `GetProcessDpiAwareness`
    cannot express per-monitor-v2 (build 15063): it reports that as plain
    per-monitor, which is precisely the mode whose coordinates differ.
    """
    user32 = _win32_lib("user32")
    if user32 is not None:
        try:
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            context = user32.GetThreadDpiAwarenessContext()
            if context:
                name = _dpi_context_name(user32, context)
                if name:
                    return name
        except (AttributeError, OSError):
            pass
    shcore = _win32_lib("shcore")
    if shcore is None:
        return ""
    try:
        shcore.GetProcessDpiAwareness.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        awareness = ctypes.c_int()
        result = shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
    except (AttributeError, OSError):
        return ""
    # An HRESULT, not a boolean: S_OK is 0 and every failure is negative, so
    # this is the one call in the module where success is the falsy value.
    if result != 0:
        return ""
    return _PROCESS_DPI_AWARENESS.get(awareness.value, "")


def _windows_version() -> tuple[int, str]:
    """(`sys.getwindowsversion().build`, the registry's `ProductName`).

    The build is the honest discriminator and the edition is not: Windows 11
    still writes `ProductName` as "Windows 10 Pro" in the registry, a
    documented quirk that misleads anything reading the name alone, so build
    22000 is what says which is which. The two are reported together for
    that reason.

    `platform.win32_ver()` is not used: it reads the same registry and
    returns a version tuple with no edition in it.
    """
    version = getattr(sys, "getwindowsversion", None)
    if version is None:
        return 0, ""
    try:
        build = int(version().build)
    except (AttributeError, ValueError):
        return 0, ""
    return build, _product_name()


def _winreg_module() -> Any:
    """The `winreg` module, or None where there is no registry.

    Typed `Any` deliberately: `winreg` exists only on Windows, so a type
    checker reading this file on Linux is looking at a module whose every
    member is guarded away, and reports each of the calls below as an unknown
    attribute. Binding the module once here keeps both registry readers
    readable and keeps that in one place instead of scattered through them.
    """
    try:
        import winreg
    except ImportError:
        return None
    return winreg


def _product_name() -> str:
    """The registry's `ProductName`, or "" where it cannot be read."""
    registry = _winreg_module()
    if registry is None:
        return ""
    try:
        with registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            name, _ = registry.QueryValueEx(key, "ProductName")
    except OSError:
        return ""
    return name if isinstance(name, str) else ""


def _windows_edition_name(build: int, edition: str) -> str:
    """`ProductName`, corrected for the version Windows does not write down.

    The registry still says "Windows 10 Pro" on a Windows 11 machine -- a
    documented quirk -- so the build is what decides, and 22000 up is Windows
    11 whatever `ProductName` calls it. Measured on a real build 26200 box,
    where the uncorrected string is exactly what was reported.

    One function because there are two readers and they were drifting: the
    `Environment.windows_edition` field, which `summary()` prints, and
    `hints._windows_release()`, which stands where a distribution family goes
    in `report --json`. Correcting it in only one of them is what made
    `doctor` say "Windows 10 Pro" and `report` say "Windows 11 Pro (build
    26200)" about the same machine in the same run.

    Returns "" unchanged, which is the registry having been unreadable rather
    than an edition to fix up; the callers each say what they want in its
    place, since "" means different things in a summary line and in a
    distribution slot.
    """
    if build >= _WINDOWS_11_BUILD and edition.startswith("Windows 10"):
        return "Windows 11" + edition[len("Windows 10") :]
    return edition


def _low_level_hooks_timeout_ms() -> int:
    """How long Windows waits for a low-level hook before removing it.

    A keyboard or mouse hook (`WH_KEYBOARD_LL`, `WH_MOUSE_LL`) runs on the
    thread that installed it, and the system unhooks it outright if that
    thread is busy past this limit -- raising nothing, anywhere. A recording
    then stops, or gains gaps, and no part of the process can tell that it
    happened. The number is what makes the caveat concrete instead of
    rhetorical.

    Windows' documented default, 300 ms, is what comes back when the
    registry says nothing.
    """
    registry = _winreg_module()
    if registry is None:
        return _LOW_LEVEL_HOOKS_DEFAULT_MS
    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER, r"Control Panel\Desktop"
        ) as key:
            value, _ = registry.QueryValueEx(key, "LowLevelHooksTimeout")
    except OSError:
        return _LOW_LEVEL_HOOKS_DEFAULT_MS
    try:
        return int(value)
    except (TypeError, ValueError):
        return _LOW_LEVEL_HOOKS_DEFAULT_MS


def _sendinput_available() -> bool:
    """Whether user32 exposes `SendInput`.

    It does on every Windows this package will run on -- SendInput has been
    the documented way to inject input since Windows 2000 -- so this is not
    here to catch an absence. It is here because an `Environment` that
    reports a transport it cannot use is the failure this module is written
    to avoid, and the question costs one symbol lookup.
    """
    user32 = _win32_lib("user32")
    return user32 is not None and getattr(user32, "SendInput", None) is not None


def _windows_environment() -> dict[str, Any]:
    """The Windows-only `Environment` fields, probed.

    Called only once `_classify()` has said this is a Windows session, so
    every probe here may assume Windows. Each is a plain call with no lasting
    effect, and none of them holds a device or a token open.

    Nothing COM-shaped is constructed. `has_comtypes` asks whether the
    binding is importable, which is what decides whether the element half of
    a session can exist at all; opening the UIA client belongs to the backend,
    which does it on whichever thread constructs it -- initializing that
    thread's apartment itself, since COM is per-thread -- and can report a
    refusal with its reason instead of having it swallowed by `detect()`. See
    `backends.uia._initialize_com` and `_connection`.

    The prototypes behind these probes are transcribed from the documented
    signatures and have not run on a Windows machine yet -- this is the
    machine-free half of the work, and saying so is cheaper than implying
    otherwise. What a real box reports is what will confirm them.
    """
    build, edition = _windows_version()
    return {
        "has_comtypes": _module("comtypes"),
        # pywin32 installs several top-level modules; win32gui is the one a
        # caller would actually import, so that is what is asked.
        "has_pywin32": _module("win32gui"),
        "has_uiautomation": _module("uiautomation"),
        "has_sendinput": _sendinput_available(),
        "is_interactive_desktop": _interactive_window_station(),
        "is_elevated": _elevated(),
        "foreground_is_elevated": _foreground_is_elevated(),
        "dpi_awareness": _dpi_awareness(),
        "windows_build": build,
        "windows_edition": _windows_edition_name(build, edition),
        "has_wgc": build >= _WGC_MIN_BUILD,
        "low_level_hooks_timeout_ms": _low_level_hooks_timeout_ms(),
    }


@dataclass(frozen=True)
class Environment:
    """What the current login actually offers."""

    session_type: SessionType
    compositor: Compositor
    desktop: str = ""
    display: str = ""
    wayland_display: str = ""

    has_libei: bool = False
    has_uinput: bool = False
    uinput_writable: bool = False
    has_atspi: bool = False
    has_pygobject: bool = False
    has_dogtail: bool = False
    has_evdev: bool = False
    # Defaults True so a hand-built Environment keeps the advice it always
    # got; detect() always sets it from the machine. See _has_input_group.
    has_input_group: bool = True
    has_portal: bool = False
    has_xtest: bool = False
    has_xlib: bool = False

    # Windows-only probes, every one of them defaulted so that a hand-built
    # Environment, and every Linux session, keeps the answers it always had.
    # See _windows_environment for how each is asked; none of them means
    # anything on another platform, and the defaults say so.
    has_comtypes: bool = False
    """Whether `comtypes` -- and with it UI Automation -- is importable. The
    one field that decides whether the element half of a Windows session can
    exist at all; the 'windows' extra installs it."""
    has_pywin32: bool = False
    """Whether `pywin32` is importable. Reported because a Windows reader will
    ask, not because anything here needs it: everything this package calls is
    declared in ctypes, and pywin32 is the convenience wrapper it does
    without."""
    has_uiautomation: bool = False
    """Whether the third-party `uiautomation` wrapper is importable -- another
    commonly used route to UI Automation, and another one this package does
    not take."""
    has_sendinput: bool = False
    """Whether user32 exposes `SendInput`, the Windows input transport."""
    is_interactive_desktop: bool = True
    """Whether this process is attached to the interactive window station.

    True off Windows on purpose: the question is about Windows' own window
    stations, and a session that has none is not a more limited one, so a
    hint gated on this stays silent wherever it does not apply."""
    is_elevated: bool = False
    """Whether this process runs at high integrity. UIPI drops input aimed at
    an elevated window from a process that is not, with no error and no
    prompt."""
    foreground_is_elevated: bool | None = None
    """Whether the window in the foreground belongs to a high-integrity
    process, or None where that could not be asked -- see
    `_foreground_is_elevated`.

    Reported beside `is_elevated` rather than folded into it because the
    failure that matters needs both: it is a *more* privileged window
    refusing input from a less privileged process. None off Windows, where
    the distinction does not exist, and None on a Windows box where the
    question could not be answered.
    """
    dpi_awareness: str = ""
    """The process's DPI awareness mode, or "" where it could not be read: one
    of "unaware", "system", "per-monitor", "per-monitor-v2" or "unaware
    (GDI-scaled)"."""
    windows_build: int = 0
    """`sys.getwindowsversion().build`, or 0 where it cannot be read. The
    version floor every Windows mechanism is conditional on -- 17134 for
    Windows.Graphics.Capture, 15063 for per-monitor-v2 DPI, 22000 for
    Windows 11."""
    windows_edition: str = ""
    """The Windows edition (`Windows 11 Pro`), or "" where it could not be
    read.

    The registry's `ProductName` with one correction already applied: it
    still says "Windows 10" on a Windows 11 machine, and `_windows_edition_name`
    uses windows_build to settle which this is -- so what is stored here is
    the answer rather than the raw value, and every reader of it agrees."""
    has_wgc: bool = False
    """Whether Windows.Graphics.Capture exists on this build. That the API is
    there, not that any particular window may be captured: a protected or
    DRM-protected one refuses, which is a per-window question."""
    low_level_hooks_timeout_ms: int = 0
    """How long the system waits for a low-level hook to return before
    removing it, or 0 off Windows. See _low_level_hooks_timeout_ms: this is
    the recorder's silent-gap window, and 300 ms is the platform default."""

    input_tools: tuple[str, ...] = field(default_factory=tuple)
    capture_tools: tuple[str, ...] = field(default_factory=tuple)
    window_tools: tuple[str, ...] = field(default_factory=tuple)
    image_tools: tuple[str, ...] = field(default_factory=tuple)
    clipboard_tools: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_inject_input(self) -> bool:
        """Whether any input mechanism is worth attempting.

        has_portal does not count on its own: nothing in this package can
        actually drive a portal transport yet, only external CLI tools and
        in-process uinput. Counting it would report "injectable" on a
        machine where nothing can actually inject, which suppresses the
        "install an input tool" hint exactly where it is needed most.

        Windows answers from its own field, before any of those are read:
        every mechanism named below is a Linux one, so without this branch a
        Windows session injected input perfectly well and reported False
        here -- and every hint gated on that then sent the reader off to
        install ydotool.
        """
        if self.session_type is SessionType.WIN32:
            return self.has_sendinput
        return (
            bool(self.input_tools)
            or self.has_libei
            or (self.uinput_writable and self.has_evdev)
        )

    @property
    def can_use_atspi(self) -> bool:
        """Whether the accessibility layer is actually reachable.

        Needs all three pieces: the libatspi service and the PyGObject
        binding, both from the distro, plus dogtail, the one part pip
        actually supplies (see the 'atspi' extra). The distro halves alone
        are common on a fresh box where nobody has run `pip install
        pyguitest[atspi]` yet, and reporting that as support would silence
        the hint that says so -- see hints.py -- while AtspiBackend still
        fails to construct.

        False on Windows by definition rather than by absence: AT-SPI is a
        Linux accessibility bus and there is none there, so nothing is
        missing. The Windows element tree is UI Automation, and
        `has_comtypes` is what says whether it can be opened -- deliberately
        not folded in here, because this property's name would then mean two
        different things on two platforms, which is the trap the rest of the
        Windows work exists to avoid.
        """
        if self.session_type is SessionType.WIN32:
            return False
        return self.has_atspi and self.has_pygobject and self.has_dogtail

    @property
    def can_capture(self) -> bool:
        """Whether any screenshot path is available, not just a CLI tool.

        Three routes reach pixels and only one of them is a tool on PATH.
        X11Backend captures and encodes a PNG itself given python-xlib and
        an X connection, and the Screenshot portal needs nothing installed
        at all beyond PyGObject. Reporting "no screen capture" on a session
        that has either would send someone off to install a tool they do
        not need.

        The portal route is the least certain of the three: has_portal is
        `_portal()`'s own "a portal service is reachable" heuristic, not
        proof that its Screenshot interface is actually implemented behind
        it -- confirmed false on a real box where the running backend was
        xdg-desktop-portal-gtk, which implements neither Screenshot nor
        RemoteDesktop. Narrowing has_portal further would need either a
        live D-Bus introspection call at detect() time (a real dependency
        this package has deliberately avoided so far) or a per-desktop
        allowlist with no live evidence yet for anything but GNOME/Mutter
        -- so this can still answer True on a session where the Screenshot
        portal will not actually work. What changed instead: a portal call
        against an interface nobody implements now raises the typed
        BackendUnavailable (see backends/portalrequest.py's call()) rather
        than a raw D-Bus exception -- PortalCaptureBackend negotiates
        nothing at construction, so a wrong answer here still lets
        connect(backend="portalcapture") succeed; the clean failure lands
        on the first actual capture() call instead, not surfacing an error
        outside this package's own model.
        """
        # Windows answers first and from no tool at all: `Win32Backend` blits
        # the screen through GDI and encodes the PNG itself, exactly as
        # X11Backend does on X11, so a tool on PATH is not the question there.
        # Without this the property said False on a session whose own backend
        # declares SCREEN_CAPTURE -- confirmed on Windows 11, where
        # `can_capture` was False beside `supports(SCREEN_CAPTURE)` True. The
        # same shape as the `can_inject_input` branch above, which the Windows
        # work added while these two were missed.
        if self.session_type is SessionType.WIN32:
            return True
        if self.capture_tools:
            return True
        # X11 only, not XWayland. X11Backend withdraws SCREEN_CAPTURE
        # there because native Wayland surfaces are never composited into
        # the X root window, so counting it here would suppress the
        # "install a screenshot tool" hint on exactly the session that
        # needs it -- see X11Backend.capabilities.
        if self.has_xlib and self.session_type is SessionType.X11:
            return True
        return self.has_portal and self.has_pygobject

    @property
    def can_use_clipboard(self) -> bool:
        """Whether the clipboard is reachable on this session.

        On Linux and the BSDs there is one route and it is a CLI tool:
        nothing in this package speaks a clipboard protocol directly, so
        clipboard_tools is the whole answer -- see tools.CLIPBOARD_TOOLS on
        why Mutter is the one desktop where that can come back empty.

        Windows is the exception, and answers before that: `Win32Backend`
        opens the clipboard itself through user32 and kernel32, so no tool is
        involved and an empty `clipboard_tools` says nothing. Confirmed on
        Windows 11, where this was False beside a backend declaring
        CLIPBOARD -- the same miss as `can_capture` above.
        """
        if self.session_type is SessionType.WIN32:
            return True
        return bool(self.clipboard_tools)

    @property
    def preferred_input(self) -> str | None:
        """The input *tool* to try first, or None if none is installed.

        Deliberately narrow: it ranks `input_tools`, and a session that
        injects input perfectly well through no tool at all answers None
        here. `input_transport` is the wider question -- what will
        actually carry the events -- and is what `summary()` reports.
        """
        if self.input_tools:
            return self.input_tools[0]
        return None

    @property
    def input_transport(self) -> str | None:
        """What will actually inject input here, whether or not it is a tool.

        `preferred_input` answers the narrower question and is None on a
        machine served by in-process uinput -- which made `summary()`
        print "input none available" on a session whose own `mechanisms`
        line, two rows below it, correctly said `uinput`.

        The order mirrors `backends._input_factory`, the thing that
        actually chooses: keymap-safe tools first, then in-process uinput,
        then keymap-unsafe tools, which uinput outranks because it costs no
        process per event and is no less safe. libei comes last and says
        it is opt-in -- automatic composition never selects it, so naming
        it plainly would describe a session nobody gets without asking.

        These are labels for a person reading `summary()`, not names to
        pass to `connect(backend=...)`.

        Windows is answered first and on its own: SendInput is in-process,
        needs no tool and opens no device, so there is nothing on that
        platform for the ranking below to choose between.
        """
        if self.session_type is SessionType.WIN32:
            return "SendInput" if self.has_sendinput else None
        keymap_safe = {t.name for t in _tools.INPUT_TOOLS if t.keymap_safe}
        for name in self.input_tools:
            if name in keymap_safe:
                return name
        if self.uinput_writable and self.has_evdev:
            return "uinput (in-process)"
        if self.input_tools:
            return self.input_tools[0]
        if self.has_libei:
            return 'libei (opt-in: connect(backend="eiinput"))'
        return None

    def summary(self) -> str:
        """A short, user-readable description of this environment."""
        session = self.session_type.value
        # The platform facts go on the session line because that is what they
        # are answers to: which window station this process sits on, and
        # whether it is elevated, decide what the whole session can reach.
        if self.session_type is SessionType.WIN32:
            station = (
                "interactive window station"
                if self.is_interactive_desktop
                else "not on an interactive desktop"
            )
            level = "elevated" if self.is_elevated else "not elevated"
            session += f" ({station}, {level})"
        lines = [
            f"session      {session}",
            f"compositor   {self.compositor.value}"
            + (f" ({self.desktop})" if self.desktop else ""),
            f"input        {self.input_transport or 'none available'}",
            "tools        "
            + (
                ", ".join(
                    self.input_tools
                    + self.capture_tools
                    + self.window_tools
                    + self.image_tools
                    + self.clipboard_tools
                )
                or "none found on PATH"
            ),
            # The `or` must apply to the joined names, not to the whole
            # line -- a non-empty prefix is always truthy.
            "mechanisms   "
            + (
                ", ".join(
                    n
                    for n, ok in (
                        ("libei", self.has_libei),
                        ("portal", self.has_portal),
                        ("uinput", self.uinput_writable and self.has_evdev),
                        ("at-spi", self.can_use_atspi),
                        ("xtest", self.has_xtest),
                        # Windows' two, listed here so that the line cannot
                        # say "none detected" beside an `input` line that
                        # says SendInput -- the disagreement that made this
                        # line worth having in the first place.
                        ("sendinput", self.has_sendinput),
                        ("comtypes", self.has_comtypes),
                    )
                    if ok
                )
                or "none detected"
            ),
        ]
        if self.session_type is SessionType.WIN32:
            lines.append(
                "windows      "
                f"build {self.windows_build or 'unknown'}"
                + (f", {self.windows_edition}" if self.windows_edition else "")
                + f", dpi {self.dpi_awareness or 'unknown'}"
            )
        lines.extend(f"note         {n}" for n in self.notes)
        return "\n".join(lines)


def _classify(env: Mapping[str, str]) -> SessionType:
    """Decide the session type from the environment variables.

    The platform is asked first, and on Windows it is the only thing asked:
    no variable in `env` means what a Linux session means by it, and several
    of them can be set there by something that is not the OS.

    `WAYLAND_DISPLAY`/`DISPLAY` are trusted over `XDG_SESSION_TYPE` where
    they disagree, not the other way around -- confirmed live on a
    machine offering several session types (Plasma X11, Plasma Wayland,
    GNOME) at login: logind reported `XDG_SESSION_TYPE=wayland` for a
    session the user had explicitly chosen as `plasmax11`
    (`DESKTOP_SESSION=plasmax11`), with `DISPLAY=:0` set and
    `WAYLAND_DISPLAY` entirely absent -- no Wayland socket to connect to
    at all. The declared type here is set at the seat/greeter level and
    is not guaranteed to track which session variant was actually picked,
    while a display socket either exists or does not. An earlier version
    checked `declared == "wayland"` before ever looking at `display`, so
    this reported `SessionType.WAYLAND` on a real X11 session -- eight
    tier-3/tier-6 capabilities only `X11Backend` serves then read `[ no]`
    on `01_what_can_i_do.py`, because a backend that would have connected
    fine was never even tried.
    """
    if _platform() == _WINDOWS:
        # Before any variable is read, and that ordering is the point.
        # `DISPLAY` and `WAYLAND_DISPLAY` are both settable *on Windows*: an
        # X server (Xming, VcXsrv, Exceed), a Cygwin or MSYS2 session, or
        # WSLg each set one -- and none of them means what it means on Linux,
        # because an X connection on Windows reaches the X clients drawing
        # into that server and no native window at all. Reading the display
        # first handed X11Backend a fraction of a desktop and reported full
        # support for it.
        return SessionType.WIN32

    wayland = env.get("WAYLAND_DISPLAY", "")
    display = env.get("DISPLAY", "")
    declared = env.get("XDG_SESSION_TYPE", "").lower()

    if wayland and display:
        return SessionType.XWAYLAND
    if wayland:
        return SessionType.WAYLAND
    if display:
        return SessionType.X11
    if declared == "wayland":
        return SessionType.WAYLAND
    if declared == "x11":
        return SessionType.X11
    return SessionType.HEADLESS if not declared else SessionType.UNKNOWN


def _desktop_name(env: Mapping[str, str]) -> str:
    """The desktop's own name for itself, for display rather than matching.

    XDG_CURRENT_DESKTOP is authoritative where set, but a session started
    through an older or minimal display manager -- or a hand-rolled
    ~/.xinitrc -- may only export XDG_SESSION_DESKTOP, or the legacy
    DESKTOP_SESSION. Same three variables _compositor searches, read here in
    priority order for one display string instead of as a keyword haystack.
    """
    return (
        env.get("XDG_CURRENT_DESKTOP")
        or env.get("XDG_SESSION_DESKTOP")
        or env.get("DESKTOP_SESSION")
        or ""
    )


def _compositor(env: Mapping[str, str], session_type: SessionType) -> Compositor:
    """Identify the compositor family from the desktop name."""
    if session_type is SessionType.WIN32:
        # Read off the session type rather than the platform, so that the
        # `sys.platform` question is asked in exactly one place. Not from
        # XDG_CURRENT_DESKTOP: a Cygwin or MSYS2 session inherits a
        # Linux-shaped environment and may well set it, and on Windows it
        # describes nothing at all. DWM is the compositor -- and since
        # Windows 8 it cannot be turned off, which is why this member exists
        # rather than leaving NONE to mean "no compositor" here.
        return Compositor.DWM

    desktop = env.get("XDG_CURRENT_DESKTOP", "")
    haystack = " ".join(
        (desktop, env.get("XDG_SESSION_DESKTOP", ""), env.get("DESKTOP_SESSION", ""))
    ).lower()

    no_wayland = not env.get("WAYLAND_DISPLAY")
    if (
        session_type in (SessionType.X11, SessionType.HEADLESS)
        and no_wayland
        and not haystack.strip()
    ):
        return Compositor.NONE
    if "gnome" in haystack:
        return Compositor.MUTTER
    if "kde" in haystack or "plasma" in haystack:
        return Compositor.KWIN
    if any(h in haystack for h in _WLROOTS_HINTS) or env.get("SWAYSOCK"):
        return Compositor.WLROOTS
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return Compositor.WLROOTS
    # WAYLAND_DISPLAY being set proves a compositor is running even when
    # none of the desktop-name variables identify which one -- NONE means
    # "no compositor", which would be a contradiction here.
    if haystack.strip() or env.get("WAYLAND_DISPLAY"):
        return Compositor.OTHER
    return Compositor.NONE


def toolkit_accessibility() -> bool | None:
    """Whether GTK's AT-SPI bridge is switched on, or None if unknowable.

    A GTK application only loads its accessibility bridge when the GNOME
    setting `org.gnome.desktop.interface toolkit-accessibility` is true. A
    GNOME session sets it; a KDE one does not, and with it off **nothing
    reports a problem**: the packages are installed, `can_use_atspi` is
    true, dogtail connects, and element queries simply return nothing --
    indistinguishable from an application that genuinely has no widgets.
    Confirmed live on KDE (2026-09-01); see docs/validation.md.

    Read by running `gsettings`, not by importing Gio and asking GSettings
    in this process, and that is not a stylistic choice. An in-process
    GSettings read goes through dconf, which opens a session-bus
    connection -- and GDBus *caches* the session bus for the lifetime of
    the process, ignoring any later change to
    `$DBUS_SESSION_BUS_ADDRESS`. That is fatal to
    tests/test_portal_dbusmock.py, which swaps that variable for a private
    dbus-daemon: once anything has cached the real bus, those tests
    negotiate against the real xdg-desktop-portal instead of their mock --
    raising real consent dialogs on the developer's desktop, and failing
    only when the whole suite runs in order. Measured, not guessed: adding
    the in-process version made exactly that happen. A subprocess has no
    such reach.

    Deliberately a function rather than an `Environment` field, so
    `detect()` -- and therefore every `connect()` -- does not pay for it.

    Three answers, not two. None means the question could not be asked:
    no `gsettings` binary, or no GNOME schemas installed, which is the
    normal state of a minimal or non-GNOME box and is not a fault.
    """
    try:
        result = subprocess.run(
            ["gsettings", "get", _INTERFACE_SCHEMA, _TOOLKIT_ACCESSIBILITY_KEY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GSETTINGS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        # Non-zero means the schema is not installed, which is a real and
        # ordinary answer ("cannot ask"), not an error worth raising.
        return None
    answer = (result.stdout or "").strip()
    if answer == "true":
        return True
    if answer == "false":
        return False
    return None


def assistive_technology_enabled() -> bool | None:
    """Whether an assistive technology is announced, or None if unknowable.

    The Chromium half of the accessibility story, and the reason a
    Chromium-family application can be completely absent from the
    accessibility tree while `windows()` lists it happily.

    Chromium -- and so Electron, and so VS Code, Slack and the rest --
    builds no accessibility tree at all until something says an AT is
    running, which on Linux means `org.a11y.Status.IsEnabled` on the
    accessibility bus. Until then it never registers with AT-SPI, so it is
    not a missing *frame* in the tree: there is no application node for it
    either. `windows()` still sees the window, because that comes from the
    compositor, which has no opinion about accessibility.

    Measured on a GNOME Shell 51 desktop (2026-09-05): `IsEnabled` false,
    and neither Google Chrome nor VS Code present in the tree while both
    were open and listed by `windows()`. That is the whole of what
    docs/validation.md previously recorded as "the two traversal paths
    disagree for Chromium clients", which was the wrong diagnosis.

    Setting it true makes those applications publish, at a real
    performance cost to them -- so this reports and does not change it.
    `--force-renderer-accessibility` on the application's own command line
    is the other way, and the better one for a test that launches the
    application itself.

    A subprocess for the same reason `toolkit_accessibility` uses one: an
    in-process Gio call caches the session bus for the life of the
    process. Three answers, and None means the question could not be asked
    -- no `gdbus`, or no accessibility bus to ask.
    """
    try:
        result = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.a11y.Status",
                "IsEnabled",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GSETTINGS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    answer = (result.stdout or "").strip()
    if "true" in answer:
        return True
    if "false" in answer:
        return False
    return None


def detect(env: Mapping[str, str] | None = None) -> Environment:
    """Probe the current environment. Pass `env` to test against a fake one.

    Only affects the parts of detection that read environment variables:
    session classification, compositor identification, and portal detection.
    Library presence (has_libei, has_atspi, has_pygobject, has_dogtail,
    has_evdev), /dev/uinput access, and installed tools are always probed
    against the real host --
    a fake `env` cannot make evdev importable or put swaymsg on PATH. A test
    against those needs to mock the underlying probe (_lib, _module, _uinput,
    tools.discover) directly rather than routing through `env`.

    The Windows probes run only when `_classify()` has said this is a Windows
    session, so a fake `env` cannot bring them to life either: driving them
    means patching `_platform`, and everything they then report is read from
    the machine the test is running on, which is why they are asserted on
    what they report where they cannot ask rather than on Windows-only values.
    """
    env = os.environ if env is None else env

    session_type = _classify(env)
    compositor = _compositor(env, session_type)
    # Probed only once the session type says Windows, which is also what
    # keeps every one of these calls off a Linux session entirely.
    windows = _windows_environment() if session_type is SessionType.WIN32 else {}
    uinput_present, uinput_writable = _uinput()
    has_atspi = _lib("atspi")
    has_pygobject = _module("gi.repository")
    has_dogtail = _module("dogtail")
    has_evdev = _module("evdev")
    notes = []

    if session_type is SessionType.WIN32:
        # Three notes, and each is about something the reader cannot see for
        # themselves: Windows prompts nothing and reports nothing, so without
        # these the failure is a test that quietly does not work.
        if not windows["is_interactive_desktop"]:
            notes.append(
                "this process is not on an interactive desktop; no windows "
                "are visible from here -- and no prompt or error will say so"
            )
        if windows["foreground_is_elevated"] and not windows["is_elevated"]:
            # Both halves, not just this process's own integrity. UIPI only
            # bites where the *target* is the more privileged of the two, and
            # "not elevated" on its own is the ordinary state of every normal
            # login -- a note that fired on it would appear in every Windows
            # report ever printed, which is how a reader learns to skip the
            # notes. `foreground_is_elevated` is None where the question could
            # not be asked, and None is not a finding either. Same gate as
            # hints._windows_hints' matching row, deliberately.
            notes.append(
                "the window in the foreground is elevated and this process is "
                "not, so input aimed at it will be dropped silently: UIPI "
                "blocks it, with no error and no prompt"
            )
        if not windows["has_comtypes"]:
            notes.append(
                "elements unavailable; install the 'windows' extra "
                "(pip install 'pyguitest[windows]') for UI Automation"
            )

    if session_type is SessionType.XWAYLAND:
        notes.append(
            "XWayland: synthetic input reaches X11 clients only, never native "
            "Wayland ones"
        )
    if compositor is Compositor.MUTTER:
        notes.append(
            "Mutter implements no foreign-toplevel protocol; window capabilities "
            "need a Shell extension"
        )
    if has_atspi and not has_pygobject:
        notes.append(
            "libatspi is present but PyGObject is not; install the 'atspi' extra "
            "to use element automation"
        )
    if has_atspi and has_pygobject and not has_dogtail:
        notes.append(
            "libatspi and PyGObject are present but dogtail is not; install "
            "the 'atspi' extra (pip install 'pyguitest[atspi]') to use "
            "element automation"
        )
    if uinput_writable and not has_evdev:
        notes.append(
            "/dev/uinput is writable but python-evdev is not installed; "
            "install the 'uinput' extra (pip install 'pyguitest[uinput]') to "
            "use it -- building it needs a C compiler and the matching "
            "kernel headers"
        )
    # A tool that only talks to an X server cannot see native Wayland
    # clients, so it is not a usable transport in a pure Wayland session --
    # but XWayland still carries a real X connection, which is what these
    # tools need; the "XWayland: reaches X11 clients only" note above is the
    # limitation that remains once they are included, not a reason to
    # exclude them.
    x11_session = session_type in (SessionType.X11, SessionType.XWAYLAND)
    # A wlroots-only tool needs a wlroots compositor, and nothing else
    # substitutes: an X connection does not, which is why this is not
    # `or x11_session`. See _input_factory in backends/__init__.py, which
    # makes the same call for the backend this list only describes.
    wlroots = compositor is Compositor.WLROOTS
    input_tools = tuple(
        t.name
        for t in _tools.discover(
            _tools.INPUT_TOOLS,
            allow_x11_only=x11_session,
            allow_wlroots_only=wlroots,
        )
    )
    if input_tools and not _tools.best(_tools.INPUT_TOOLS, keymap_safe_only=True):
        notes.append(
            f"only keymap-unsafe input tools found ({', '.join(input_tools)}); "
            "typed text may differ on a non-US layout"
        )
    input_group = _has_input_group()
    if uinput_present and not uinput_writable:
        if input_group:
            notes.append(
                "/dev/uinput exists but is not writable: add yourself to the "
                "'input' group, then run `newgrp input` or log in again"
            )
        else:
            # FreeBSD reaches here: it has /dev/uinput via cuse, owned
            # root:wheel 0600, and no 'input' group at all. Naming the
            # group anyway sent the reader after something that does not
            # exist, and the udev rule behind it after a system with no
            # udev.
            notes.append(
                "/dev/uinput exists but is not writable, and there is no "
                "'input' group on this system to join: its ownership or "
                "mode has to be changed directly"
            )

    return Environment(
        session_type=session_type,
        compositor=compositor,
        desktop=_desktop_name(env),
        display=env.get("DISPLAY", ""),
        wayland_display=env.get("WAYLAND_DISPLAY", ""),
        has_libei=_lib("ei"),
        has_uinput=uinput_present,
        uinput_writable=uinput_writable,
        has_input_group=input_group,
        **windows,
        has_atspi=has_atspi,
        has_pygobject=has_pygobject,
        has_dogtail=has_dogtail,
        has_evdev=has_evdev,
        has_portal=_portal(env),
        has_xtest=_lib("Xtst"),
        has_xlib=_module("Xlib"),
        input_tools=input_tools,
        capture_tools=tuple(
            t.name
            for t in _tools.discover(
                _tools.CAPTURE_TOOLS,
                allow_x11_only=x11_session,
                # A real X server, not merely a reachable X display: the
                # root-reading tools cannot capture under XWayland at all.
                allow_x_root_only=session_type is SessionType.X11,
            )
        ),
        window_tools=tuple(t.name for t in _tools.discover(_tools.WINDOW_TOOLS)),
        image_tools=tuple(t.name for t in _tools.discover(_tools.IMAGE_TOOLS)),
        clipboard_tools=tuple(
            t.name
            for t in _tools.discover(
                _tools.CLIPBOARD_TOOLS,
                allow_x11_only=x11_session,
                allow_mutter_incompatible=compositor is not Compositor.MUTTER,
            )
        ),
        notes=tuple(notes),
    )
