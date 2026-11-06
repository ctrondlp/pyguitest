"""External tool adapters.

Most of what this package needs already exists as a maintained command-line
tool, owned by the project that understands the mechanism best. Shelling out to
those costs no Python dependency and inherits their upstream maintenance, which
is why input injection and screen capture are adapters rather than bindings.

The ordering within each group is not arbitrary. Input tools are ranked by
whether they preserve keymap correctness:

  wdotool   libei + RemoteDesktop portal + wlr virtual-keyboard/pointer
  wtype     wlr virtual-keyboard; client supplies its own keymap
  ydotool   /dev/uinput; injects scancodes *below* the compositor, so the
            active xkb layout is applied and typed text can differ
  xdotool   XTest; X11 sessions only, never reaches native Wayland clients

That ranking is the keymap trap from the audit, encoded as preference order.

Rank is not the whole story: a tool must also be *carryable* by the session.
Being installed proves nothing -- wtype runs happily on GNOME and does nothing,
because Mutter implements none of the wlroots protocols it needs. `discover`
filters on that before rank is consulted.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .capabilities import Capability

__all__ = [
    "ExternalTool",
    "INPUT_TOOLS",
    "CAPTURE_TOOLS",
    "WINDOW_TOOLS",
    "IMAGE_TOOLS",
    "CLIPBOARD_TOOLS",
    "discover",
    "best",
]

_VERSION_TIMEOUT = 3.0
"""Bound on version(), in seconds. Well under the 15s a real capture or
input call tolerates -- a version probe that hangs is a diagnostic detail,
not something worth making a caller wait on."""

_VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "hyprctl": ("version",),
}
"""Per-tool override for the argument that prints a version.

Most of these tools accept --version, which is what version() tries by
default. hyprctl is a subcommand dispatcher and uses `version` instead of a
flag. Anything not listed here is tried with --version; when that guess is
wrong the tool exits nonzero or is silent, and version() reports None rather
than raising -- an incorrect guess costs one missing line, not a failure."""


_NOTHING_WITHHELD: Mapping[str, frozenset[Capability]] = MappingProxyType({})
"""The default for `ExternalTool.withheld_on`: no capability withheld anywhere.

Module-level and shared, because it is immutable and every tool that withholds
nothing can point at the same one. See that field's docstring for why it is
reached through a `default_factory`.
"""


@dataclass(frozen=True)
class ExternalTool:
    """One external command-line tool and what it can do."""

    name: str
    capabilities: frozenset[Capability]
    note: str = ""
    keymap_safe: bool = True
    """False if the tool injects scancodes below the compositor, so typed text
    depends on the session's active keyboard layout."""

    compositors: frozenset[str] | None = None
    """The compositors this tool can work on, by `Compositor.value`, or None for
    any compositor. Replaces the old `wlroots_only` boolean, which could not say
    "wlroots *and* COSMIC": `wtype` needs `zwp_virtual_keyboard_manager_v1`,
    which both implement, while `grim` needs `wlr-screencopy`, which only
    wlroots implements. A tool with this set is dropped when `discover()`'s
    `compositor` argument names a compositor not in it."""

    withheld_on: Mapping[str, frozenset[Capability]] = field(
        default_factory=lambda: _NOTHING_WITHHELD
    )
    """Capabilities this tool must not be credited with on a given compositor,
    by `Compositor.value`. Applied by `discover()` when it is told which
    compositor the session is on.

    Finer-grained than `compositors`, and needed because a tool can be
    genuinely usable on a compositor for one capability and silently useless
    for another. `wtype` on COSMIC is the measured case: its *typing* reaches
    a Qt application there, and its *named keys* do not -- `wtype -k Return`
    exits successfully and nothing receives it. Dropping the whole tool is the
    wrong answer, because wtype is also the only keymap-safe way to type on
    that compositor; what has to go is its claim on `KEY_EVENT`, so the
    uinput backend beside it serves that instead.

    Nothing hashes an `ExternalTool`, so a mapping here is safe; the default is
    a shared read-only empty one, reached through `default_factory` rather than
    written as the default directly. That is not style: a `mappingproxy`
    *as a dataclass default* is rejected outright on Python 3.11, whose
    dataclasses refuse any default whose class is unhashable, and accepted on
    3.10 (which tested `isinstance(default, (list, dict, set))` instead) and on
    3.12+ (where a mappingproxy became hashable again). CI caught it on the one
    version in the middle. `default_factory` sidesteps the check on every
    version, and the factory still hands back the one shared immutable
    instance rather than a fresh mutable dict."""

    x11_only: bool = False
    """True if the tool talks to an X server and so cannot see native Wayland
    clients, even when an X display is reachable through XWayland."""

    wayland_only: bool = False
    """True if the tool speaks only the Wayland protocol, so it needs a Wayland
    compositor to connect to and not merely a session that looks modern.

    The mirror image of `x11_only`, and it exists because the asymmetry was a
    live bug: `wl-copy` is first in CLIPBOARD_TOOLS and was gated on
    `mutter_incompatible` alone, which answers "can this work on GNOME" and
    nothing about whether a Wayland compositor is running at all. On any X11
    session whose compositor is not Mutter -- Xfce, i3, MATE, Cinnamon, LXQt,
    KDE-on-X11 -- with wl-clipboard installed, `_clipboard_factory` therefore
    built a wl-copy backend, every clipboard call failed with "Failed to
    connect to a Wayland server", and `xclip` was never tried though it was
    installed and next in line. Confirmed on a private X server with both
    tools present. `doctor` reported CLIPBOARD available and "Nothing
    missing", which is the part that made it expensive rather than obvious.

    Distinct from a `compositors` allowlist for the same reason
    `mutter_incompatible` is: the question here is whether there is a Wayland
    display to talk to, not which compositor is drawing it.
    """

    x_root_only: bool = False
    """True if the tool captures by reading the X *root window*, which needs a
    real X server and not merely a reachable X display.

    Distinct from x11_only, and stricter. An x11_only tool is still useful
    under XWayland for the X11 clients it can see; an x_root_only one is not
    useful there at all, because XWayland refuses GetImage on the root
    outright -- verified on GNOME Shell 50.4, where a 1x1 request fails
    exactly as a full-screen one does, under every pixmap format and plane
    mask. Native Wayland surfaces are never composited into that root, so
    there is nothing there to read even in principle.

    The failure is not quiet, which is the reason this flag exists rather
    than being left to the capture backend's own error handling:
    gnome-screenshot hangs for the full subprocess timeout before giving up.
    """

    mutter_incompatible: bool = False
    """True if the tool cannot work on Mutter at all, whatever the session type.

    Two very different mechanisms land here, which is why the flag is named
    for the compositor it fails on rather than for either protocol.
    `wl-copy` needs wlr-data-control-unstable-v1, which Mutter does not
    implement. That is distinct from a `compositors` allowlist: KWin is not a
    wlroots compositor but does implement this protocol -- confirmed live on
    KDE Plasma 6, where wl-copy/wl-paste round-tripped correctly and wl-copy
    forked into the background to keep serving the selection, so a
    `compositors={wlroots}` allowlist would incorrectly exclude KWin.
    `spectacle` reaches the screen only through KWin's own screenshot API and
    exits non-zero everywhere else -- measured on Fedora 44 / GNOME Shell
    50.0, where `spectacle -b -n -f -o out.png` prints "Spectacle requires
    KDE Plasma's KWin compositor" and writes nothing. Left unflagged it
    advertised SCREEN_CAPTURE on a session where no whole-screen capture
    could succeed."""

    also_needs: str = ""
    """A second binary this tool's own operations need, checked by `present`
    alongside `name`. wl-clipboard ships as two commands -- wl-copy to write,
    wl-paste to read -- and a session with only one half installed cannot be
    offered as a working clipboard backend."""

    probe_version: bool = True
    """False for a tool that must not be run merely to ask its version, because
    running it does something.

    `pbcopy` is the case this exists for: every invocation rewrites the macOS
    pasteboard, and the conventional version flag is no exception. Measured
    live on macOS 26.7 -- with a sentinel value on the clipboard, `pbcopy
    --version < /dev/null` exited **0** and left the clipboard empty, and
    `pbcopy -h` cleared it too. Probing it would make `pyguitest doctor`
    quietly destroy whatever the user had copied, so `version()` answers None
    here instead. The cost is one blank version column for a tool that has no
    version to report anyway.
    """

    def path(self) -> str | None:
        """Full path to the tool, or None if it is not on PATH."""
        return shutil.which(self.name)

    @property
    def present(self) -> bool:
        """Whether the tool -- and its second half, if it has one -- is installed."""
        return self.path() is not None and (
            not self.also_needs or shutil.which(self.also_needs) is not None
        )

    def version(self) -> str | None:
        """This tool's own reported version, first line only, or None.

        Best-effort and never raises: not on PATH, an incorrect --version
        guess, or a hang all come back as None. Nothing in this package
        depends on this succeeding -- it exists for diagnostics, where
        "unknown" is a fine answer and blocking a bug report on a hung
        subprocess is not.
        """
        path = self.path()
        if path is None or not self.probe_version:
            return None
        argv = [path, *_VERSION_ARGS.get(self.name, ("--version",))]
        try:
            # stdin is DEVNULL rather than inherited: a probe that reads the
            # caller's stdin would consume input meant for whoever is running
            # the report, and the clipboard tools in particular take their
            # content that way.
            result = subprocess.run(
                argv,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                timeout=_VERSION_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        # stderr counts only when the command succeeded. Several of these
        # tools print their version there rather than on stdout, which is
        # why the fallback exists -- but a *failed* run's stderr is an
        # error message, and printing that as a version is worse than
        # printing nothing. Seen in a real `pyguitest debug`: on a session
        # with no X display, xclip's version read "xclip: Error: Can't
        # open display: (null)".
        output = (result.stdout or "").strip()
        if not output and result.returncode == 0:
            output = (result.stderr or "").strip()
        return output.splitlines()[0] if output else None


_INPUT = frozenset(
    {
        Capability.POINTER_MOVE,
        Capability.POINTER_BUTTON,
        Capability.POINTER_SCROLL,
        Capability.KEY_EVENT,
        Capability.TEXT_ENTRY,
    }
)

INPUT_TOOLS = (
    ExternalTool(
        "wdotool",
        _INPUT,
        "libei via the RemoteDesktop portal, with wlr virtual-device fallback",
    ),
    ExternalTool(
        "wtype",
        frozenset({Capability.KEY_EVENT, Capability.TEXT_ENTRY}),
        "wlr virtual-keyboard; typing only, on wlroots and COSMIC. Its named "
        "keys do not arrive on COSMIC, so KEY_EVENT is withheld there",
        compositors=frozenset({"wlroots", "cosmic"}),
        withheld_on=MappingProxyType({"cosmic": frozenset({Capability.KEY_EVENT})}),
    ),
    ExternalTool(
        "ydotool",
        _INPUT,
        "uinput; needs the input group and cannot guarantee typed text on a "
        "non-US layout",
        keymap_safe=False,
    ),
    ExternalTool(
        "xdotool",
        _INPUT,
        "XTest; X11 clients only, never native Wayland ones",
        x11_only=True,
    ),
)

CAPTURE_TOOLS = (
    ExternalTool(
        "grim",
        frozenset({Capability.SCREEN_CAPTURE}),
        "wlroots; reads frames over wlr-screencopy, a protocol Mutter and "
        "KWin do not implement -- installed and run there it captures "
        "nothing",
        compositors=frozenset({"wlroots"}),
    ),
    ExternalTool(
        "gnome-screenshot",
        frozenset({Capability.SCREEN_CAPTURE}),
        "GNOME; needs a real X11 session -- since GNOME 42 it is not on the "
        "allowlist for the Shell's own screenshot interface, so it falls back "
        "to reading the X root, which is empty under Wayland",
        x_root_only=True,
    ),
    ExternalTool(
        "spectacle",
        frozenset({Capability.SCREEN_CAPTURE}),
        "KDE Plasma; it captures through KWin and refuses to start without "
        "it, exiting non-zero with \"Spectacle requires KDE Plasma's KWin "
        'compositor" -- so on GNOME it advertises a capture that can never '
        "succeed",
        mutter_incompatible=True,
    ),
    ExternalTool(
        "import",
        frozenset({Capability.SCREEN_CAPTURE}),
        "ImageMagick; captures the X root only, so it misses Wayland clients "
        "and cannot capture at all under XWayland",
        x11_only=True,
        x_root_only=True,
    ),
    # Last, and the only one that is not a desktop's own tool: macOS ships
    # `screencapture` with the OS, so it is not a package anyone installs and
    # there is nothing to prefer it over. Listed here for the same reason the
    # others are -- `Environment.capture_tools` is how `doctor` says what it
    # found, and a capture path that exists without being named is a path a
    # reader cannot see. It also serves the session with no `macos` extra,
    # where `macos.py`'s WINDOW_CAPTURE is not available to reach it through.
    ExternalTool(
        "screencapture",
        frozenset({Capability.SCREEN_CAPTURE}),
        "macOS; ships with the OS, and the window form needs a CGWindowID "
        "that only the macos backend can hand it",
    ),
)

_WINDOW = frozenset(
    {
        Capability.WINDOW_LIST,
        Capability.WINDOW_STATE,
        Capability.WINDOW_GEOMETRY,
        Capability.WINDOW_PLACEMENT,
        Capability.WINDOW_RESIZE,
        Capability.WINDOW_ACTIVATE,
    }
)

_WLROOTS = frozenset({"wlroots"})
"""The compositor family sway, Hyprland, river, wayfire, labwc and niri all
report as -- see `_WLROOTS_HINTS` in session.py.

Every tool below is one desktop's own IPC client and can drive nothing under a
compositor that does not speak it, so each carries an allowlist. Without one
they were offered everywhere: a GNOME box with sway installed for the headless
harness had `pyguitest doctor` list `swaymsg` among the tools it could use.

Family-level, not per-compositor, because that is the granularity
`Compositor` has -- all six of those sessions are one enum member, so this
cannot say "sway but not niri" and does not pretend to. `for_compositor` in
backends/windows.py makes the finer distinction where it actually matters, by
each compositor's own environment signature (SWAYSOCK, HYPRLAND_INSTANCE_
SIGNATURE, NIRI_SOCKET) rather than by which binaries are installed. So a
wlroots session with all three on PATH still lists all three here, and still
builds exactly one backend -- the one whose compositor is really running.
"""

WINDOW_TOOLS = (
    ExternalTool(
        "swaymsg",
        _WINDOW,
        "sway IPC; full window control including geometry, which no Wayland "
        "protocol exposes",
        compositors=_WLROOTS,
    ),
    ExternalTool("hyprctl", _WINDOW, "Hyprland IPC", compositors=_WLROOTS),
    ExternalTool(
        "niri",
        _WINDOW - {Capability.WINDOW_PLACEMENT},
        "niri IPC; a scrolling tiler sizes a window but never places one, so "
        "this is the one window tool that cannot move anything",
        compositors=_WLROOTS,
    ),
    ExternalTool(
        "kdotool",
        _WINDOW,
        "KWin scripting over D-Bus; the only window path on KDE Plasma",
        compositors=frozenset({"kwin"}),
    ),
    # wmctrl (EWMH) is deliberately not listed: no backend consumes it --
    # for_compositor() only handles WLROOTS and KWIN, and for_tool("wmctrl")
    # returns None. Advertising it in Environment.window_tools would name a
    # path that does not exist. Serving Mutter/X11 via EWMH is real feature
    # work, not something to half-declare here.
)

IMAGE_TOOLS = (
    ExternalTool(
        "compare",
        frozenset({Capability.IMAGE_LOCATE}),
        "ImageMagick; subimage-search via pixel comparison, optionally "
        "FFT-accelerated on an FFTW+HDRI build for the NCC, MSE, RMSE, "
        "PSNR, PHASE and DPC metrics",
    ),
    # Second only, and that ordering is the whole point of it being here:
    # a machine with both entry points keeps selecting `compare`, so this
    # entry changes nothing on Linux or macOS, where the legacy commands are
    # what packages install. It is on Windows that it decides the outcome.
    # `winget install ImageMagick.ImageMagick` -- the command docs/install.md
    # and `pyguitest doctor` both name -- lays down ImageMagick 7 with
    # magick.exe and no legacy commands at all (verified live: the install
    # directory holds exactly magick.exe and unins000.exe), so before this
    # entry existed the package was installed, crop.py was using it, and
    # IMAGE_LOCATE still reported itself missing because `compare` was not on
    # PATH. `magick compare` and `magick identify` are the same two
    # operations under IM7's dispatcher.
    ExternalTool(
        "magick",
        frozenset({Capability.IMAGE_LOCATE}),
        "ImageMagick 7's entry point, which is all a Windows install "
        "provides: `magick compare` and `magick identify` are the same "
        "subimage search and the same size lookup the legacy `compare` and "
        "`identify` perform",
    ),
)

CLIPBOARD_TOOLS = (
    ExternalTool(
        "wl-copy",
        frozenset({Capability.CLIPBOARD}),
        "wl-clipboard; wlr-data-control-unstable-v1. Works on wlroots "
        "compositors and, confirmed live on KDE Plasma 6, KWin -- but not "
        "Mutter, which implements neither this protocol nor a portal path "
        "this package can reach without a RemoteDesktop session. Both "
        "halves fork into the background to keep serving a selection after "
        "this process exits.",
        also_needs="wl-paste",
        mutter_incompatible=True,
        wayland_only=True,
    ),
    ExternalTool(
        "xclip",
        frozenset({Capability.CLIPBOARD}),
        "X11 selections; forks into the background on write for the same "
        "reason wl-copy does",
        x11_only=True,
    ),
    ExternalTool(
        "xsel",
        frozenset({Capability.CLIPBOARD}),
        "X11 selections; same fork-on-write behavior as xclip",
        x11_only=True,
    ),
    # Last for the same reason `screencapture` is last in CAPTURE_TOOLS: macOS
    # ships it with the OS, so there is nothing to rank it above, and listing
    # it is how `doctor` says the capability exists on a Mac at all. Two ways
    # in which it is the odd one out among these: the pasteboard is held by
    # the `pbs` server rather than by the process that set it, so nothing has
    # to stay alive to keep answering a paste; and there is one selection, so
    # `primary=True` is refused rather than aliased (see clipboard.py).
    ExternalTool(
        "pbcopy",
        frozenset({Capability.CLIPBOARD}),
        "macOS; ships with the OS. The pasteboard is stored by the pasteboard "
        "server, not by the process that wrote it, so no daemon is left "
        "behind and the value survives the call -- and there is one "
        "selection, with no PRIMARY to reach",
        also_needs="pbpaste",
        probe_version=False,
    ),
)


def discover(
    group: Sequence[ExternalTool],
    allow_x11_only: bool = True,
    allow_x_root_only: bool = True,
    allow_mutter_incompatible: bool = True,
    allow_wayland_only: bool = True,
    compositor: str | None = None,
) -> tuple[ExternalTool, ...]:
    """Every usable tool in `group`, in preference order.

    Being on PATH is not enough; a tool is excluded when the session cannot
    carry it:

    allow_x11_only=False
        drops tools that only see the X server. They run, but address an almost
        empty XWayland root rather than the desktop.
    compositor="wlroots"
        drops tools whose `compositors` allowlist does not include the named
        compositor (by `Compositor.value`). Pass the session's compositor so a
        tool that needs a protocol that compositor lacks -- wtype on GNOME, grim
        on COSMIC -- is not offered; it would run, exit successfully, and do
        nothing, the worst failure of the three. None performs no compositor
        filtering, which `best()` relies on.
    allow_wayland_only=False
        drops tools that need a Wayland compositor to connect to. Pass False on
        a session with no Wayland display -- a real X11 session -- where
        wl-clipboard exits non-zero with "Failed to connect to a Wayland
        server". The mirror of `allow_x11_only`, and on XWayland both are True,
        which is right: both kinds of client are reachable there.

    allow_x_root_only=False
        drops tools that capture by reading the X root window. Pass False for
        anything but a real X11 session, XWayland included: the root is
        unreadable there, and the tools do not fail quickly -- one hangs for
        the whole subprocess timeout first.
    allow_mutter_incompatible=False
        drops tools that cannot work on Mutter at all -- wl-clipboard, which
        needs a protocol Mutter lacks, and spectacle, which needs KWin's own
        screenshot API. Distinct from the compositor allowlist: KWin needs this
        flag rather than that one, being neither a wlroots compositor nor a
        Mutter one.
    A tool whose `withheld_on` names the given compositor is handed back with
    those capabilities removed rather than dropped, so the rest of what it can
    do is still used. With no `compositor` named nothing is withheld, which is
    what `best()` relies on.
    """
    usable = tuple(
        t
        for t in group
        if t.present
        and (allow_x11_only or not t.x11_only)
        and (compositor is None or t.compositors is None or compositor in t.compositors)
        and (allow_x_root_only or not t.x_root_only)
        and (allow_mutter_incompatible or not t.mutter_incompatible)
        and (allow_wayland_only or not t.wayland_only)
    )
    if compositor is None:
        return usable
    return tuple(
        replace(t, capabilities=t.capabilities - t.withheld_on[compositor])
        if compositor in t.withheld_on
        else t
        for t in usable
    )


def best(
    group: Sequence[ExternalTool],
    capability: Capability | None = None,
    keymap_safe_only: bool = False,
) -> ExternalTool | None:
    """The most preferred present tool, optionally filtered."""
    for tool in discover(group):
        if capability is not None and capability not in tool.capabilities:
            continue
        if keymap_safe_only and not tool.keymap_safe:
            continue
        return tool
    return None
