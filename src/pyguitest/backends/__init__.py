"""Backend registry and selection.

Backends are registered by name and asked, in priority order, whether they can
drive a given Environment. Selection is explicit rather than clever: the caller
can always name one, and detection is only the default.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import BackendUnavailable, PyGUITestError
from . import atspi as _atspi
from .base import GUIBackend, ImageMatch, Screen, Window
from .capture import ToolCaptureBackend
from .composite import CompositeBackend
from .eiinput import LibeiBackend
from .imagesearch import ToolImageSearchBackend
from .input import ToolInputBackend
from .null import NullBackend

__all__ = [
    "GUIBackend",
    "Screen",
    "Window",
    "ImageMatch",
    "NullBackend",
    "CompositeBackend",
    "ToolInputBackend",
    "ToolCaptureBackend",
    "ToolImageSearchBackend",
    "LibeiBackend",
    "register",
    "select",
    "available",
]

_REGISTRY: list[tuple[int, str, Callable, bool]] = []


def register(factory, name, priority=50, opt_in=False):
    """Register a backend factory.

    `factory(environment)` returns a GUIBackend, or None if it cannot drive
    this environment. Higher priority is tried first.

    `opt_in=True` excludes it from automatic composition (a plain
    `connect()`) entirely -- reserved for a factory whose construction has a
    side effect no caller should hit by surprise, such as raising an
    interactive consent dialog that blocks until a human answers it. It
    remains reachable by name: `connect(backend=name)`.
    """
    _REGISTRY.append((priority, name, factory, opt_in))
    _REGISTRY.sort(key=lambda r: -r[0])
    return factory


def available():
    """Registered backend names, in priority order."""
    return [name for _, name, _, _ in _REGISTRY]


def select(environment, name=None, options=None):
    """Pick a backend for `environment`, or the one called `name`.

    `options` is forwarded to the named factory as keyword arguments, and is
    only meaningful with `name`: automatic composition builds every factory,
    so there would be no way to say which one an option was meant for. Not
    every factory accepts extras -- passing options a factory has no
    parameter for is a TypeError, which is the right outcome, since the
    alternative is silently ignoring what the caller asked for.
    """
    if name is not None:
        for _, candidate, factory, _opt_in in _REGISTRY:
            if candidate == name:
                backend = factory(environment, **(options or {}))
                if backend is None:
                    raise BackendUnavailable(
                        f"backend {name!r} cannot drive this session"
                    )
                return backend
        raise BackendUnavailable(
            f"unknown backend {name!r}; available: {', '.join(available()) or 'none'}"
        )

    # Compose every applicable, non-opt-in backend: no single mechanism
    # covers a desktop, and each contributes the capabilities the others
    # lack. An opt-in factory is skipped here specifically so a plain
    # connect() can never trigger its side effect unasked.
    members = [
        b
        for b in (f(environment) for _, _, f, opt_in in _REGISTRY if not opt_in)
        if b is not None
    ]
    if len(members) == 1:
        return members[0]
    if members:
        try:
            return CompositeBackend(members)
        except Exception:
            # Every already-built member -- an open X display, a uinput
            # device, a live IPC socket -- would otherwise be dropped
            # without close() if composing them failed.
            for member in members:
                member.close()
            raise

    return NullBackend(
        f"no backend for a {environment.session_type.value} session "
        f"on {environment.compositor.value}"
    )


def _atspi_factory(environment):
    """Build the AT-SPI backend, or None when dogtail is unavailable.

    AT-SPI leads: it needs neither geometry nor injection permission, and
    behaves identically under X11 and Wayland.
    """
    if not _atspi.available():
        return None
    try:
        return _atspi.AtspiBackend(environment)
    except BackendUnavailable:
        return None


register(_atspi_factory, "atspi", priority=90)


def _gnomeshell_factory(environment):
    """Build the GNOME Shell extension backend, or None off Mutter.

    Outranks AT-SPI (90): it sees every window Mutter manages regardless of
    whether the client is native Wayland or XWayland -- something neither
    AT-SPI's geometry (unreliable under Wayland) nor X11Backend (blind to
    native Wayland clients) can offer -- and reports real compositor state
    for placement, minimize and hit-testing that AT-SPI does not expose at
    all. Requires the pyguitest-window-control extension installed and
    enabled by hand; see gnome-shell-extension/README.md.
    """
    from ..session import Compositor
    from . import gnomeshell as _gnomeshell

    if environment is not None and environment.compositor is not Compositor.MUTTER:
        return None
    if not _gnomeshell.available():
        return None
    try:
        return _gnomeshell.GnomeShellBackend()
    except BackendUnavailable:
        return None


register(_gnomeshell_factory, "gnomeshell", priority=93)

_FALLBACK_SCREEN_SIZE = (1920, 1080)
"""Used only when the real geometry cannot be determined at all."""


def _screen_size(environment):
    """Best-effort (width, height) of the session's screen space.

    uinput's absolute-pointer axes must be declared with a fixed maximum at
    device creation time, and that maximum only means the right thing if it
    matches the real display: on a 4K output a device declared for 1920x1080
    makes every move_mouse() land somewhere else on screen. This queries the
    session's own tools for the actual bounding box before falling back to
    the historical constant, rather than guessing blind.
    """
    import json
    import shutil
    import subprocess

    from ..session import Compositor, SessionType

    def run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=2).stdout

    try:
        if environment.session_type in (
            SessionType.X11,
            SessionType.XWAYLAND,
        ) and shutil.which("xrandr"):
            import re

            match = re.search(r"current (\d+) x (\d+)", run(["xrandr", "--current"]))
            if match:
                return (int(match.group(1)), int(match.group(2)))
        if environment.compositor is Compositor.WLROOTS:
            rects = []
            if shutil.which("swaymsg"):
                rects = [
                    o["rect"]
                    for o in json.loads(run(["swaymsg", "-t", "get_outputs", "-r"]))
                ]
            elif shutil.which("hyprctl"):
                rects = json.loads(run(["hyprctl", "monitors", "-j"]))
            if rects:
                right = max(r["x"] + r["width"] for r in rects)
                bottom = max(r["y"] + r["height"] for r in rects)
                return (right, bottom)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        pass
    return _FALLBACK_SCREEN_SIZE


def _input_factory(environment):
    """Pick an input transport.

    Ranked by correctness first, then cost:

    1. keymap-safe tools (wdotool, wtype) -- the client supplies its own
       keymap, so typed text arrives as written;
    2. in-process uinput via python-evdev -- keymap-unsafe, but holds one
       device open instead of spawning a process per event;
    3. keymap-unsafe tools (ydotool) -- same limitation, plus the spawn.

    xdotool sits in group 1 because XTest resolves keysyms against the
    server's live map, but it only reaches X11 clients.
    """
    from .. import tools

    # X11-only tools cannot reach native Wayland clients, so they are not a
    # usable transport outside an X11 or XWayland session even when
    # installed -- XWayland still carries a real X connection, which is
    # exactly what XTest-based tools need; they just cannot see the
    # session's native Wayland clients through it.
    from ..session import Compositor, SessionType
    from . import uinput as _uinput

    x11 = environment.session_type in (SessionType.X11, SessionType.XWAYLAND)
    usable = tools.discover(
        tools.INPUT_TOOLS,
        allow_x11_only=x11,
        allow_wlroots_only=environment.compositor is Compositor.WLROOTS or x11,
    )

    for tool in usable:
        if not tool.keymap_safe:
            continue
        try:
            return ToolInputBackend(tool)
        except BackendUnavailable:
            continue

    if _uinput.available():
        try:
            return _uinput.UinputBackend(screen_size=_screen_size(environment))
        except (BackendUnavailable, PyGUITestError):
            pass

    for tool in usable:
        try:
            return ToolInputBackend(tool)
        except BackendUnavailable:
            continue
    return None


register(_input_factory, "input", priority=70)


def _window_factory(environment):
    """Compositor IPC: the only source of window geometry and placement.

    Prefers a socket transport, so no CLI tool needs to be installed.
    """
    from . import windows as _windows

    try:
        return _windows.for_compositor(environment.compositor)
    except OSError:
        return None


def _capture_factory(environment):
    """Build a capture backend from the first usable screenshot tool."""
    from .. import tools
    from ..session import SessionType

    x11 = environment.session_type in (SessionType.X11, SessionType.XWAYLAND)
    usable = tools.discover(
        tools.CAPTURE_TOOLS,
        allow_x11_only=x11,
        # Root-reading tools need a real X server. Under XWayland they do
        # not merely capture the wrong thing -- gnome-screenshot hangs for
        # the full subprocess timeout before failing, which is why they are
        # excluded here rather than left for CompositeBackend's fallback to
        # absorb. The fallback stays as the safety net for a tool that is
        # broken for some reason this cannot predict.
        allow_x_root_only=environment.session_type is SessionType.X11,
    )
    for tool in usable:
        try:
            return ToolCaptureBackend(tool)
        except PyGUITestError:
            continue
    return None


def _image_factory(environment):
    """Build the ImageMagick-backed template-matching backend.

    Unlike the input/capture/window factories, this needs no session-type or
    compositor awareness at all: compare/identify/convert operate purely on
    already-captured pixel files, so presence on PATH is the whole test.
    """
    from .. import tools

    tool = tools.best(tools.IMAGE_TOOLS)
    if tool is None:
        return None
    try:
        return ToolImageSearchBackend(tool)
    except PyGUITestError:
        return None


# Window IPC outranks AT-SPI: both can list windows, but only IPC reports
# geometry that is trustworthy under Wayland.
register(_window_factory, "windows", priority=95)
register(_capture_factory, "capture", priority=60)
register(_image_factory, "imagesearch", priority=55)


def _x11_factory(environment):
    """X11: the only backend serving tier-6, and the only route to the BSDs.

    Registered below the Wayland-native backends so a Wayland session prefers
    them, but above null so an X11 or XWayland session always gets it.
    """
    from ..session import SessionType
    from . import x11 as _x11

    if environment.session_type not in (SessionType.X11, SessionType.XWAYLAND):
        return None
    if not _x11.available():
        return None
    try:
        return _x11.X11Backend(environment)
    except BackendUnavailable:
        return None


register(_x11_factory, "x11", priority=40)


def _portal_factory(environment, **options):
    """Build the RemoteDesktop portal backend, by name only.

    opt_in=True: constructing this can raise an interactive consent dialog
    that blocks until a human clicks Allow -- a side effect no caller should
    hit from a plain connect(). Use connect(backend="portal") deliberately.
    """
    from . import portal as _portal

    if not _portal.available():
        return None
    try:
        return _portal.PortalBackend(**options)
    except BackendUnavailable:
        return None


register(_portal_factory, "portal", priority=80, opt_in=True)


def _eiinput_factory(environment, **options):
    """Build the libei pointer backend, by name only.

    opt_in=True for the same reason as portal: constructing this raises a
    real RemoteDesktop consent dialog and blocks until a human answers it.
    Use connect(backend="eiinput") deliberately.
    """
    from . import eiinput as _eiinput

    if not _eiinput.available():
        return None
    try:
        return _eiinput.LibeiBackend(**options)
    except BackendUnavailable:
        return None


register(_eiinput_factory, "eiinput", priority=80, opt_in=True)


def _portalcapture_factory(environment, **options):
    """Build the Screenshot portal capture backend, by name only.

    opt_in=True for a narrower reason than portal/eiinput: constructing
    this raises no dialog, but the first *capture* does on desktops that
    prompt for screenshot permission. Automatic composition would put it in
    front of the screenshot tools on any session with a portal, turning a
    plain `capture()` -- which grim or gnome-screenshot answer silently --
    into a consent prompt. Ask for it deliberately:
    `connect(backend="portalcapture")`.
    """
    from . import portalcapture as _portalcapture

    if not _portalcapture.available():
        return None
    try:
        return _portalcapture.PortalCaptureBackend(**options)
    except BackendUnavailable:
        return None


register(_portalcapture_factory, "portalcapture", priority=58, opt_in=True)
