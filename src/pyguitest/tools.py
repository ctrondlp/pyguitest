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
from collections.abc import Sequence
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ExternalTool:
    """One external command-line tool and what it can do."""

    name: str
    capabilities: frozenset[Capability]
    note: str = ""
    keymap_safe: bool = True
    """False if the tool injects scancodes below the compositor, so typed text
    depends on the session's active keyboard layout."""

    wlroots_only: bool = False
    """True if the tool needs a wlroots-only protocol. wtype needs
    zwp_virtual_keyboard_manager_v1, which Mutter does not implement -- it
    installs and runs there, and silently does nothing."""

    x11_only: bool = False
    """True if the tool talks to an X server and so cannot see native Wayland
    clients, even when an X display is reachable through XWayland."""

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
    """True if the tool needs a Wayland clipboard protocol Mutter does not
    implement (wlr-data-control-unstable-v1). Distinct from wlroots_only: KWin
    is not a wlroots compositor but does implement this protocol -- confirmed
    live on KDE Plasma 6, where wl-copy/wl-paste round-tripped correctly and
    wl-copy forked into the background to keep serving the selection, the
    same way it does on a wlroots compositor. wlroots_only would incorrectly
    exclude KWin here."""

    also_needs: str = ""
    """A second binary this tool's own operations need, checked by `present`
    alongside `name`. wl-clipboard ships as two commands -- wl-copy to write,
    wl-paste to read -- and a session with only one half installed cannot be
    offered as a working clipboard backend."""

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
        if path is None:
            return None
        argv = [path, *_VERSION_ARGS.get(self.name, ("--version",))]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=_VERSION_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = (result.stdout or result.stderr or "").strip()
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
        "wlr virtual-keyboard; typing only, and wlroots compositors only",
        wlroots_only=True,
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
    ExternalTool("grim", frozenset({Capability.SCREEN_CAPTURE}), "wlroots"),
    ExternalTool(
        "gnome-screenshot",
        frozenset({Capability.SCREEN_CAPTURE}),
        "GNOME; needs a real X11 session -- since GNOME 42 it is not on the "
        "allowlist for the Shell's own screenshot interface, so it falls back "
        "to reading the X root, which is empty under Wayland",
        x_root_only=True,
    ),
    ExternalTool("spectacle", frozenset({Capability.SCREEN_CAPTURE}), "KDE Plasma"),
    ExternalTool(
        "import",
        frozenset({Capability.SCREEN_CAPTURE}),
        "ImageMagick; captures the X root only, so it misses Wayland clients "
        "and cannot capture at all under XWayland",
        x11_only=True,
        x_root_only=True,
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

WINDOW_TOOLS = (
    ExternalTool(
        "swaymsg",
        _WINDOW,
        "sway IPC; full window control including geometry, which no Wayland "
        "protocol exposes",
    ),
    ExternalTool("hyprctl", _WINDOW, "Hyprland IPC"),
    ExternalTool(
        "niri",
        _WINDOW - {Capability.WINDOW_PLACEMENT},
        "niri IPC; a scrolling tiler sizes a window but never places one, so "
        "this is the one window tool that cannot move anything",
    ),
    ExternalTool(
        "kdotool",
        _WINDOW,
        "KWin scripting over D-Bus; the only window path on KDE Plasma",
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
)


def discover(
    group: Sequence[ExternalTool],
    allow_x11_only: bool = True,
    allow_wlroots_only: bool = True,
    allow_x_root_only: bool = True,
    allow_mutter_incompatible: bool = True,
) -> tuple[ExternalTool, ...]:
    """Every usable tool in `group`, in preference order.

    Being on PATH is not enough; a tool is excluded when the session cannot
    carry it:

    allow_x11_only=False
        drops tools that only see the X server. They run, but address an almost
        empty XWayland root rather than the desktop.
    allow_wlroots_only=False
        drops tools needing wlroots-only protocols. They run, exit
        successfully, and do nothing -- the worst failure of the three.
    allow_x_root_only=False
        drops tools that capture by reading the X root window. Pass False for
        anything but a real X11 session, XWayland included: the root is
        unreadable there, and the tools do not fail quickly -- one hangs for
        the whole subprocess timeout first.
    allow_mutter_incompatible=False
        drops tools needing a Wayland protocol Mutter does not implement.
        Distinct from allow_wlroots_only: KWin needs this flag rather than
        that one, since it is not a wlroots compositor but does carry the
        protocol wl-clipboard needs.
    """
    return tuple(
        t
        for t in group
        if t.present
        and (allow_x11_only or not t.x11_only)
        and (allow_wlroots_only or not t.wlroots_only)
        and (allow_x_root_only or not t.x_root_only)
        and (allow_mutter_incompatible or not t.mutter_incompatible)
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
