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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from . import tools as _tools

__all__ = ["SessionType", "Compositor", "Environment", "detect"]


class SessionType(Enum):
    """What kind of display server the session is running."""

    WAYLAND = "wayland"
    X11 = "x11"
    XWAYLAND = "xwayland"
    """An X11 connection inside a Wayland session. XTest reaches X11 clients
    here but never native Wayland ones -- the trap the Perl module documents."""
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
    OTHER = "other"
    NONE = "none"


_WLROOTS_HINTS = ("sway", "hyprland", "river", "wayfire", "labwc", "niri")


def _lib(name: str) -> bool:
    """True if a shared library is loadable by name."""
    return ctypes.util.find_library(name) is not None


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


def _uinput() -> tuple[bool, bool]:
    """(present, writable) for /dev/uinput."""
    path = "/dev/uinput"
    if not os.path.exists(path):
        return False, False
    return True, os.access(path, os.W_OK)


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
    has_portal: bool = False
    has_xtest: bool = False
    has_xlib: bool = False

    input_tools: tuple[str, ...] = field(default_factory=tuple)
    capture_tools: tuple[str, ...] = field(default_factory=tuple)
    window_tools: tuple[str, ...] = field(default_factory=tuple)
    image_tools: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_inject_input(self) -> bool:
        """Whether any input mechanism is worth attempting.

        has_portal does not count on its own: nothing in this package can
        actually drive a portal transport yet, only external CLI tools and
        in-process uinput. Counting it would report "injectable" on a
        machine where nothing can actually inject, which suppresses the
        "install an input tool" hint exactly where it is needed most.
        """
        return bool(self.input_tools) or self.has_libei or self.uinput_writable

    @property
    def can_use_atspi(self) -> bool:
        """Whether the accessibility layer is actually reachable.

        Needs both halves: the libatspi service and the PyGObject binding. The
        C library alone is common on desktops where nobody installed the Python
        bindings, and reporting that as support would strand the one layer that
        works identically under X11 and Wayland.
        """
        return self.has_atspi and self.has_pygobject

    @property
    def can_capture(self) -> bool:
        """Whether any screenshot path is available, not just a CLI tool.

        Three routes reach pixels and only one of them is a tool on PATH.
        X11Backend captures and encodes a PNG itself given python-xlib and
        an X connection, and the Screenshot portal needs nothing installed
        at all beyond PyGObject. Reporting "no screen capture" on a session
        that has either would send someone off to install a tool they do
        not need.
        """
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
    def preferred_input(self) -> str | None:
        """The input backend to try first.

        libei leads: it is portal-brokered, needs no device node, reaches
        native Wayland clients, and lets the caller supply its own keymap --
        which is what keeps type_text() correct on a non-US layout. uinput is
        the fallback precisely because it cannot do that last part.
        """
        if self.input_tools:
            return self.input_tools[0]
        return None

    def summary(self) -> str:
        """A short human-readable description of this environment."""
        lines = [
            f"session      {self.session_type.value}",
            f"compositor   {self.compositor.value}"
            + (f" ({self.desktop})" if self.desktop else ""),
            f"input        {self.preferred_input or 'none available'}",
            "tools        "
            + (
                ", ".join(
                    self.input_tools
                    + self.capture_tools
                    + self.window_tools
                    + self.image_tools
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
                        ("uinput", self.uinput_writable),
                        ("at-spi", self.can_use_atspi),
                        ("xtest", self.has_xtest),
                    )
                    if ok
                )
                or "none detected"
            ),
        ]
        lines.extend(f"note         {n}" for n in self.notes)
        return "\n".join(lines)


def _classify(env: Mapping[str, str]) -> SessionType:
    """Decide the session type from the environment variables."""
    wayland = env.get("WAYLAND_DISPLAY", "")
    display = env.get("DISPLAY", "")
    declared = env.get("XDG_SESSION_TYPE", "").lower()

    if wayland and display:
        return SessionType.XWAYLAND
    if wayland or declared == "wayland":
        return SessionType.WAYLAND
    if display or declared == "x11":
        return SessionType.X11
    return SessionType.HEADLESS if not declared else SessionType.UNKNOWN


def _compositor(env: Mapping[str, str], session_type: SessionType) -> Compositor:
    """Identify the compositor family from the desktop name."""
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


def detect(env: Mapping[str, str] | None = None) -> Environment:
    """Probe the current environment. Pass `env` to test against a fake one.

    Only affects the parts of detection that read environment variables:
    session classification, compositor identification, and portal detection.
    Library presence (has_libei, has_atspi, has_pygobject), /dev/uinput
    access, and installed tools are always probed against the real host --
    a fake `env` cannot make evdev importable or put swaymsg on PATH. A test
    against those needs to mock the underlying probe (_lib, _module, _uinput,
    tools.discover) directly rather than routing through `env`.
    """
    env = os.environ if env is None else env

    session_type = _classify(env)
    compositor = _compositor(env, session_type)
    uinput_present, uinput_writable = _uinput()
    notes = []

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
    if _lib("atspi") and not _module("gi.repository"):
        notes.append(
            "libatspi is present but PyGObject is not; install the 'atspi' extra "
            "to use element automation"
        )
    # A tool that only talks to an X server cannot see native Wayland
    # clients, so it is not a usable transport in a pure Wayland session --
    # but XWayland still carries a real X connection, which is what these
    # tools need; the "XWayland: reaches X11 clients only" note above is the
    # limitation that remains once they are included, not a reason to
    # exclude them.
    x11_session = session_type in (SessionType.X11, SessionType.XWAYLAND)
    wlroots = compositor is Compositor.WLROOTS or x11_session
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
    if uinput_present and not uinput_writable:
        notes.append(
            "/dev/uinput exists but is not writable: add yourself to the "
            "'input' group, then run `newgrp input` or log in again"
        )

    return Environment(
        session_type=session_type,
        compositor=compositor,
        desktop=env.get("XDG_CURRENT_DESKTOP", ""),
        display=env.get("DISPLAY", ""),
        wayland_display=env.get("WAYLAND_DISPLAY", ""),
        has_libei=_lib("ei"),
        has_uinput=uinput_present,
        uinput_writable=uinput_writable,
        has_atspi=_lib("atspi"),
        has_pygobject=_module("gi.repository"),
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
        notes=tuple(notes),
    )
