"""Backend registry and selection.

Backends are registered by name and asked, in priority order, whether they can
drive a given Environment. Selection is explicit rather than clever: the caller
can always name one, and detection is only the default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..errors import BackendUnavailable, PyGUITestError
from . import atspi as _atspi
from .base import Element, GUIBackend, ImageMatch, Screen, Window
from .capture import ToolCaptureBackend
from .clipboard import ToolClipboardBackend
from .composite import CompositeBackend
from .eiinput import LibeiBackend
from .imagesearch import ToolImageSearchBackend
from .input import ToolInputBackend
from .kwinevents import KWinEventsBackend
from .null import NullBackend

__all__ = [
    "GUIBackend",
    "Element",
    "Screen",
    "Window",
    "ImageMatch",
    "NullBackend",
    "CompositeBackend",
    "ToolInputBackend",
    "ToolCaptureBackend",
    "ToolClipboardBackend",
    "ToolImageSearchBackend",
    "KWinEventsBackend",
    "LibeiBackend",
    "register",
    "select",
    "available",
]

_logger = logging.getLogger(__name__)
"""Never configured from here -- see `_auto_build` and `select` for what
this records. An application wires up handlers and levels; this package
only ever calls `.debug()`/`.info()`/`.warning()` on a logger of its own."""

_REGISTRY: list[tuple[int, str, Callable, bool]] = []


def register(factory, name, priority=50, opt_in=False):
    """Register a backend factory.

    `factory(environment)` returns a GUIBackend, or None if it plainly
    cannot drive this environment -- wrong compositor, a library not
    importable, a tool not on PATH. Higher priority is tried first.

    It may also *raise* `BackendUnavailable` rather than return None, for a
    failure only construction itself can discover and that has a real,
    specific reason worth keeping -- a portal that refused the request, a
    D-Bus service that is not running. Automatic composition (a plain
    `connect()`) still treats that exactly like None, so no factory has to
    protect it for that path; but naming this backend directly
    (`connect(backend=name)`) lets the raised exception propagate verbatim
    instead of being replaced by a generic "cannot drive this session" --
    which is why raising here, when there is something specific to say, is
    worth doing rather than swallowing it into a bare None as before.

    `opt_in=True` excludes it from automatic composition entirely --
    reserved for a factory whose construction has a side effect no caller
    should hit by surprise, such as raising an interactive consent dialog
    that blocks until a human answers it. It remains reachable by name.
    """
    _REGISTRY.append((priority, name, factory, opt_in))
    _REGISTRY.sort(key=lambda r: -r[0])
    return factory


def available():
    """Registered backend names, in priority order."""
    return [name for _, name, _, _ in _REGISTRY]


def _factory_for(name):
    """The registered factory called `name`, or raise naming what exists."""
    for _, candidate, factory, _opt_in in _REGISTRY:
        if candidate == name:
            return factory
    raise BackendUnavailable(
        f"unknown backend {name!r}; available: {', '.join(available()) or 'none'}"
    )


def _combine(members):
    """One member as itself, several as a composite, closing all on failure.

    A single member is returned bare rather than wrapped: `select(env, "x")`
    and `select(env, ["x"])` should hand back the same object, and a
    composite of one adds a layer of dispatch that routes everything to the
    only member there is.
    """
    if len(members) == 1:
        return members[0]
    try:
        return CompositeBackend(members)
    except BaseException:
        # Every already-built member -- an open X display, a uinput device,
        # a live IPC socket -- would otherwise be dropped without close() if
        # composing them failed.
        for member in members:
            member.close()
        raise


def _named_options(names, options):
    """`options` keyed by backend name, or raise if it cannot be read that way.

    Only for the sequence form. The shape of `backend` decides the shape of
    `backend_options`: one name takes the flat dict it always took, several
    names require the keyed form, and neither is inferred from the contents
    -- a flat dict says nothing about which backend an option was meant for,
    and guessing would hand `persist_mode` to whichever factory happened to
    accept it. A key naming a backend that was not asked for is reported
    rather than ignored, since it is otherwise a silently dropped request.
    """
    if not options:
        return {name: {} for name in names}
    unknown = [key for key in options if key not in names]
    if unknown:
        raise ValueError(
            f"backend_options names {', '.join(map(repr, unknown))}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not among the backends "
            f"asked for ({', '.join(names)}). With a sequence of backends, "
            'options are keyed by backend name: {"eiinput": {"persist_mode": 2}}'
        )
    return {name: dict(options.get(name) or {}) for name in names}


def _build_named(environment, names, options):
    """Build each named backend in order, closing them all if one fails.

    Deliberately does not catch `BackendUnavailable` from the factory call
    itself -- unlike `_auto_build` below, which automatic composition uses.
    A factory that raised one had something specific to say (see
    `register`'s docstring), and a caller who named this backend directly
    asked for exactly this answer, not a generic one. The `except
    BaseException` here is only cleanup: it closes what was already built
    and re-raises the same exception, whichever it was.

    A factory that instead returns `None` -- "plainly cannot drive this
    environment", nothing more specific to say -- still gets the generic
    message below. Unlike automatic composition, which reads None as "not
    applicable to this desktop" and moves on: a name is a request, and
    quietly dropping it would return a session missing the very capability
    that motivated asking.
    """
    built = []
    try:
        for name in names:
            backend = _factory_for(name)(environment, **options[name])
            if backend is None:
                raise BackendUnavailable(f"backend {name!r} cannot drive this session")
            built.append(backend)
    except BaseException:
        for backend in built:
            backend.close()
        raise
    return built


def _auto_build(name, factory, environment):
    """Build via `factory` for automatic composition, or None if it fails.

    The counterpart to `_build_named` not catching: this is the one place
    that does, so no individual factory has to protect itself against
    automatic composition's list comprehension crashing outright. A
    construction failure here is exactly as unusable as a factory that
    declined by returning None itself, so the two are folded together --
    but only here. A caller who named this backend directly goes through
    `_build_named` instead, which lets the same exception -- carrying
    whatever specific reason the factory raised it for -- propagate
    verbatim rather than being discarded and replaced.

    Both ways of declining are logged at debug level, with `name` -- the
    registry has it, `factory` alone does not -- because this is the one
    place a backend can drop out of a session silently otherwise: no
    exception reaches `connect()`, nothing about it is in the final
    capability set, and "why doesn't this desktop have window listing"
    has no answer without it.
    """
    try:
        built = factory(environment)
    except BackendUnavailable as exc:
        _logger.debug("backend %r declined: %s", name, exc)
        return None
    if built is None:
        _logger.debug("backend %r does not apply to this session", name)
    else:
        _logger.debug("backend %r built", name)
    return built


def select(environment, name=None, options=None):
    """Pick a backend for `environment`, or compose the ones named.

    `name` is one backend name, or a sequence of them. A sequence composes
    exactly those, **in the order given** -- the caller's order is the
    precedence, so `["x11", "atspi"]` and `["atspi", "x11"]` are different
    requests, and the first member that has a capability serves it. That is
    the opposite of automatic composition below, which orders by registry
    priority because nobody expressed a preference.

    Naming backends is also the only way to compose an `opt_in` one, which
    is the point: automatic composition skips those so a consent dialog is
    never raised by surprise, and naming one *is* the caller asking for it.

    A named backend that cannot build raises rather than being skipped --
    see `_build_named`. `options` is forwarded to the factories as keyword
    arguments: a flat dict for a single name, keyed by backend name for
    several (see `_named_options`). Not every factory accepts extras --
    passing options a factory has no parameter for is a TypeError, which is
    the right outcome, since the alternative is silently ignoring what the
    caller asked for.
    """
    if isinstance(name, str):
        _logger.debug("connecting named backend %r", name)
        return _combine(_build_named(environment, [name], {name: dict(options or {})}))

    if name is not None:
        names = list(name)
        if not names:
            raise ValueError(
                "backend was an empty sequence; pass None to detect "
                "automatically, or name at least one backend"
            )
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            # A second copy could never win a capability the first already
            # serves, so this is a mistake rather than a preference.
            raise ValueError(f"backend names repeat: {', '.join(sorted(duplicates))}")
        _logger.debug("connecting named backends %s", names)
        return _combine(
            _build_named(environment, names, _named_options(names, options))
        )

    # Compose every applicable, non-opt-in backend: no single mechanism
    # covers a desktop, and each contributes the capabilities the others
    # lack. An opt-in factory is skipped here specifically so a plain
    # connect() can never trigger its side effect unasked.
    members = [
        b
        for b in (
            _auto_build(name, f, environment)
            for _, name, f, opt_in in _REGISTRY
            if not opt_in
        )
        if b is not None
    ]
    if members:
        _logger.info(
            "composed %d backend(s): %s", len(members), [m.name for m in members]
        )
        return _combine(members)

    reason = (
        f"no backend for a {environment.session_type.value} session "
        f"on {environment.compositor.value}"
    )
    _logger.warning("%s", reason)
    return NullBackend(reason)


def _atspi_factory(environment):
    """Build the AT-SPI backend, or None when dogtail is unavailable.

    AT-SPI leads: it needs neither geometry nor injection permission, and
    behaves identically under X11 and Wayland. Construction can still raise
    `BackendUnavailable` for a reason `available()` cannot see -- see
    `register`'s docstring on what happens to that.
    """
    if not _atspi.available() and _atspi.a11y_bus_reachable():
        return None
    # available() answers False for two quite different reasons. dogtail
    # not being installed means this backend does not apply here, and
    # automatic composition should move on -- that is the None above. An
    # accessibility bus that did not answer means it *does* apply, and a
    # caller who named `atspi` deserves that reason rather than the
    # registry's generic "cannot drive this session"; constructing is what
    # raises it. Automatic composition is unaffected either way: _auto_build
    # folds a raise and a None together.
    return _atspi.AtspiBackend(environment)


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

    Construction can raise `BackendUnavailable` for a reason `available()`
    cannot see -- the extension present but disabled, say -- see
    `register`'s docstring on what happens to that.
    """
    from ..session import Compositor
    from . import gnomeshell as _gnomeshell

    if environment is not None and environment.compositor is not Compositor.MUTTER:
        return None
    if not _gnomeshell.available():
        return None
    return _gnomeshell.GnomeShellBackend()


register(_gnomeshell_factory, "gnomeshell", priority=93)

_FALLBACK_SCREEN_SIZE = (1920, 1080)
"""Used only when the real geometry cannot be determined at all."""


def _query(argv):
    """Stdout of a short-lived query tool."""
    import subprocess

    return subprocess.run(argv, capture_output=True, text=True, timeout=2).stdout


def _xrandr_size():
    """The X screen's current size, or None.

    Also the answer for a Wayland session that has XWayland: the
    compositor sizes the X root to its own layout, so xrandr reports what
    the compositor decided rather than what X thinks. See _screen_size for
    when that is worth asking.
    """
    import re
    import shutil

    if not shutil.which("xrandr"):
        return None
    match = re.search(r"current (\d+) x (\d+)", _query(["xrandr", "--current"]))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _wlroots_size():
    """The output layout's bounding box on sway or Hyprland, or None."""
    import json
    import shutil

    rects = []
    if shutil.which("swaymsg"):
        rects = [
            o["rect"]
            for o in json.loads(_query(["swaymsg", "-t", "get_outputs", "-r"]))
        ]
    elif shutil.which("hyprctl"):
        rects = json.loads(_query(["hyprctl", "monitors", "-j"]))
    if not rects:
        return None
    return (
        max(r["x"] + r["width"] for r in rects),
        max(r["y"] + r["height"] for r in rects),
    )


def _mutter_size():
    """The logical layout's bounding box on GNOME, or None.

    Mutter's DisplayConfig answers any session-bus client without an
    extension or a portal prompt, which is what makes it askable from
    here, before any backend exists. The module it lives in owns the
    arithmetic, which GnomeShellBackend.screens() shares.
    """
    from . import displayconfig

    return displayconfig.layout_size()


def _kscreen_size():
    """The output layout's bounding box on KDE, or None.

    Parses `kscreen-doctor -o`, not its JSON, and that is deliberate:
    libkscreen serialises the panel's mode as `size` and leaves the
    logical size out of the JSON altogether, so reconstructing the layout
    from it means redoing the scale and rotation arithmetic that
    `Output::geometry()` has already done. The human-readable listing is
    the only place that rect is printed.

    The colour escapes around each label are stripped rather than assumed
    absent, since the tool writes them whether or not stdout is a
    terminal. A disabled output prints an invalid geometry -- `0,0 0x0` --
    which contributes nothing to a box measured from the origin, so
    nothing here has to filter those separately.
    """
    import re
    import shutil

    if not shutil.which("kscreen-doctor"):
        return None
    listing = re.sub(r"\x1b\[[0-9;]*m", "", _query(["kscreen-doctor", "-o"]))
    rects = [
        tuple(int(group) for group in match.groups())
        for match in re.finditer(r"Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", listing)
    ]
    if not rects:
        return None
    return (
        max(x + width for x, _y, width, _height in rects),
        max(y + height for _x, y, _width, height in rects),
    )


def _screen_size(environment):
    """Best-effort (width, height) of the session's screen space.

    uinput's absolute-pointer axes must be declared with a fixed maximum at
    device creation time, and that maximum only means the right thing if it
    matches the real display: on a 4K output a device declared for 1920x1080
    makes every move_mouse() land somewhere else on screen. This asks the
    session's own authority for the actual bounding box before falling back
    to the historical constant, rather than guessing blind.

    There is no portable question to ask, so each compositor family gets the
    source that can answer for it: swaymsg or hyprctl on wlroots, Mutter's
    DisplayConfig on GNOME, kscreen-doctor on KDE. The last two were the
    gap -- a default GNOME or KDE Wayland session matched none of the
    branches here and silently took the constant, which is wrong on every
    desktop that is not 1920x1080 and says nothing when it is.

    A compositor-native source is tried first, ahead of xrandr, on purpose:
    GNOME and sway both size XWayland's X root to their own logical layout,
    so under fractional scaling xrandr and the compositor's own answer can
    round differently, and the compositor's answer is the one `geometry()`
    and `screens()` already agree with. xrandr is the fallback for X11 and
    XWayland sessions -- the only source at all on plain X11, and the one
    left to try when a Wayland compositor's own answer did not arrive
    (kscreen-doctor not installed, PyGObject missing, some other compositor
    entirely).

    Every source is best-effort and may return None; a source that raises
    is treated the same way, since the only thing worse than not knowing
    the screen size here is failing to build an input backend over it.
    """
    import subprocess

    from ..session import Compositor, SessionType

    sources = []
    if environment.compositor is Compositor.WLROOTS:
        sources.append(_wlroots_size)
    elif environment.compositor is Compositor.MUTTER:
        sources.append(_mutter_size)
    elif environment.compositor is Compositor.KWIN:
        sources.append(_kscreen_size)
    if environment.session_type in (SessionType.X11, SessionType.XWAYLAND):
        sources.append(_xrandr_size)

    for source in sources:
        try:
            size = source()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            continue
        if size:
            return size
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
        # The compositor alone decides this one -- an X connection is not a
        # substitute for the wlroots protocols wtype needs. `or x11` here
        # let wtype through on a plain X11 session (no Wayland at all) and
        # on GNOME/KDE XWayland, where it cannot work; being keymap-safe it
        # then outranked xdotool, so a session xdotool would have driven
        # correctly got a backend that fails on every call. A wlroots
        # session with
        # XWayland is unaffected: its compositor is WLROOTS either way.
        allow_wlroots_only=environment.compositor is Compositor.WLROOTS,
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


def _clipboard_factory(environment):
    """Build a clipboard backend from the first usable clipboard tool.

    No member here on Mutter -- wl-clipboard is the only entry in
    CLIPBOARD_TOOLS that is not x11_only, and it is excluded there by
    mutter_incompatible. That leaves GNOME with no clipboard backend at all
    rather than a broken one; see clipboard.py's module docstring on why.
    """
    from .. import tools
    from ..session import Compositor, SessionType

    x11 = environment.session_type in (SessionType.X11, SessionType.XWAYLAND)
    usable = tools.discover(
        tools.CLIPBOARD_TOOLS,
        allow_x11_only=x11,
        allow_mutter_incompatible=environment.compositor is not Compositor.MUTTER,
    )
    for tool in usable:
        try:
            return ToolClipboardBackend(tool)
        except PyGUITestError:
            continue
    return None


def _kwinevents_factory(environment):
    """Build the KWin-script-backed WINDOW_EVENTS backend, KDE only.

    kdotool (the "windows" member on KDE) has no event-subscription
    mechanism to speak; this is a second, separate composite member
    providing only WINDOW_EVENTS, the same way capture/clipboard/
    imagesearch sit alongside kdotool rather than being folded into it.
    Compositor-gated rather than tried everywhere: the KWin script this
    loads needs a live `org.kde.KWin` on the session bus, which nothing
    but KWin provides.

    Construction can raise `BackendUnavailable` for a reason `available()`
    cannot see -- KWin's Scripting interface unreachable, the script
    failing to load -- see `register`'s docstring on what happens to that.
    """
    from ..session import Compositor
    from . import kwinevents as _kwinevents

    if environment.compositor is not Compositor.KWIN:
        return None
    if not _kwinevents.available():
        return None
    return KWinEventsBackend()


# Window IPC outranks AT-SPI: both can list windows, but only IPC reports
# geometry that is trustworthy under Wayland.
register(_window_factory, "windows", priority=95)
register(_capture_factory, "capture", priority=60)
register(_clipboard_factory, "clipboard", priority=58)
register(_kwinevents_factory, "kwinevents", priority=57)
register(_image_factory, "imagesearch", priority=55)


def _x11_factory(environment, **options):
    """X11: the only backend serving tier-6, and the only route to the BSDs.

    Registered below the Wayland-native backends so a Wayland session prefers
    them, but above null so an X11 or XWayland session always gets it.
    Construction can still raise `BackendUnavailable` -- no display to
    connect to, say -- see `register`'s docstring on what happens to that.

    `display_name` is forwarded because `X11Backend` has always accepted it
    and nothing could reach it: `connect(backend="x11",
    backend_options={"display_name": ":99"})` raised TypeError here, leaving
    `$DISPLAY` as the only way to choose a display. That matters to anything
    driving a server other than the ambient one -- a nested or virtual X
    server, or a recorder capturing one display while the session runs on
    another.
    """
    from ..session import SessionType
    from . import x11 as _x11

    if environment.session_type not in (SessionType.X11, SessionType.XWAYLAND):
        return None
    if not _x11.available():
        return None
    return _x11.X11Backend(environment, display_name=options.get("display_name"))


register(_x11_factory, "x11", priority=40)


def _portal_factory(environment, **options):
    """Build the RemoteDesktop portal backend, by name only.

    opt_in=True: constructing this can raise an interactive consent dialog
    that blocks until a human clicks Allow -- a side effect no caller should
    hit from a plain connect(). Use connect(backend="portal") deliberately.
    A declined dialog, or any other construction failure, raises
    `BackendUnavailable` naming the real reason -- see `register`'s
    docstring on what happens to that.
    """
    from . import portal as _portal

    if not _portal.available():
        return None
    return _portal.PortalBackend(**options)


register(_portal_factory, "portal", priority=80, opt_in=True)


def _eiinput_factory(environment, **options):
    """Build the libei pointer backend, by name only.

    opt_in=True for the same reason as portal: constructing this raises a
    real RemoteDesktop consent dialog and blocks until a human answers it.
    Use connect(backend="eiinput") deliberately. A missing PyGObject, a
    declined dialog, or any other construction failure raises
    `BackendUnavailable` naming the real reason -- see `register`'s
    docstring on what happens to that.
    """
    from . import eiinput as _eiinput

    if not _eiinput.available():
        return None
    return _eiinput.LibeiBackend(**options)


register(_eiinput_factory, "eiinput", priority=80, opt_in=True)


def _inputcapture_factory(environment, **options):
    """Build the InputCapture portal backend, by name only.

    opt_in=True for the same reason as portal/eiinput, with a sharper edge:
    a *successful* activation of this backend's one operation exclusively
    diverts the user's real pointer input away from their desktop for as
    long as it takes to release it. Use connect(backend="inputcapture")
    deliberately. A missing PyGObject, a declined dialog, or any other
    construction failure raises `BackendUnavailable` naming the real
    reason -- see `register`'s docstring on what happens to that.
    """
    from . import inputcapture as _inputcapture

    if not _inputcapture.available():
        return None
    return _inputcapture.InputCaptureBackend(**options)


register(_inputcapture_factory, "inputcapture", priority=80, opt_in=True)


def _portalcapture_factory(environment, **options):
    """Build the Screenshot portal capture backend, by name only.

    opt_in=True for a narrower reason than portal/eiinput: constructing
    this raises no dialog, but the first *capture* does on desktops that
    prompt for screenshot permission. Automatic composition would put it in
    front of the screenshot tools on any session with a portal, turning a
    plain `capture()` -- which grim or gnome-screenshot answer silently --
    into a consent prompt. Ask for it deliberately:
    `connect(backend="portalcapture")`. Construction can still raise
    `BackendUnavailable` for a reason `available()` cannot see -- see
    `register`'s docstring on what happens to that.
    """
    from . import portalcapture as _portalcapture

    if not _portalcapture.available():
        return None
    return _portalcapture.PortalCaptureBackend(**options)


register(_portalcapture_factory, "portalcapture", priority=58, opt_in=True)
