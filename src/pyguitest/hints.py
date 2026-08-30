"""Tell the user what to install to unlock more capabilities.

Most of what this package can do depends on pieces pip cannot supply: the
GNOME Python stack, which does not build from source cleanly, and the desktop's
own screenshot and input tools. An end user should not have to work out which
package provides which, so this maps the gaps onto commands they can paste.

Package names differ per distribution, so the family is detected from
/etc/os-release. An unrecognised distribution still gets the generic advice --
the component names, without a command.
"""

from __future__ import annotations

import os
import pathlib
import textwrap
from collections.abc import Iterator

from .capabilities import Capability, CapabilitySet
from .session import Environment

__all__ = ["Hint", "detect_distro", "hints_for", "advice"]

# Package names per distribution family, keyed by the component we need.
_PACKAGES = {
    "fedora": {
        "install": "sudo dnf install",
        "atspi": "python3-gobject python3-pyatspi at-spi2-core",
        "capture": "gnome-screenshot",
        "input": "ydotool python3-evdev",
        "imagemagick": "ImageMagick",
    },
    "debian": {
        "install": "sudo apt install",
        "atspi": "python3-gi python3-pyatspi gir1.2-atspi-2.0",
        "capture": "gnome-screenshot",
        "input": "ydotool python3-evdev",
        "imagemagick": "imagemagick",
    },
    "arch": {
        "install": "sudo pacman -S",
        "atspi": "python-gobject python-atspi at-spi2-core",
        "capture": "grim",
        "input": "ydotool python-evdev",
        "imagemagick": "imagemagick",
    },
    "suse": {
        "install": "sudo zypper install",
        "atspi": "python3-gobject python3-atspi at-spi2-core",
        "capture": "gnome-screenshot",
        "input": "ydotool python3-evdev",
        "imagemagick": "ImageMagick",
    },
}

# The screenshot tool that actually works on a given compositor, which the
# per-distro `capture` entry above cannot express: it is one name per
# distribution, but the right answer is per *desktop*. Recommending
# gnome-screenshot on KDE or sway installs a tool that captures nothing
# there -- it reads the X root window (see tools.py's x_root_only), so it
# is wrong for the same reason grim was already special-cased here.
_CAPTURE_TOOL_BY_COMPOSITOR = {
    "wlroots": "grim",
    "kwin": "spectacle",
}

_DEFAULT_CAPTURE_TOOL = "gnome-screenshot"
"""Named when neither the compositor nor the distribution settles it.

Only reached on an unrecognised distribution, where there is no package
name to give -- so this is the tool to go looking for, not a command to
run. Better than naming nothing at all, which is what a reader of an
unrecognised distro used to get."""

# os-release ID or ID_LIKE values that map onto a family above.
_FAMILIES = {
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "debian": "debian",
    "ubuntu": "debian",
    "arch": "arch",
    "manjaro": "arch",
    "opensuse": "suse",
    "suse": "suse",
}


class Hint:
    """One missing piece, and how to install it."""

    __slots__ = ("component", "why", "command", "installable", "packages")

    def __init__(
        self,
        component: str,
        why: str,
        command: str | None,
        installable: bool = True,
        packages: str | None = None,
    ) -> None:
        """Describe a gap, what it costs, and the command that closes it.

        `command` is None either because a package exists but its name for
        this distro isn't known, or because nothing can be installed at all
        (`installable=False`) -- advice() renders those two cases
        differently: the former still points at "your distribution", the
        latter would be telling the user to install something that does not
        exist.

        `packages` names the tool(s) to look for even when `command` is
        None -- which backend needs which tool (grim vs. xdotool vs.
        gnome-screenshot) is decided before the distro's package manager is
        known, so this lets an unrecognised distro still get a concrete
        name instead of a bare "ask your distribution".
        """
        self.component = component
        self.why = why
        self.command = command
        self.installable = installable
        self.packages = packages

    def __repr__(self) -> str:
        return f"Hint({self.component!r})"


def detect_distro(os_release: str | None = None) -> str | None:
    """Return the distribution family, or None if it is not recognised.

    Reads /etc/os-release unless `os_release` supplies its contents, which is
    how the tests drive it.
    """
    if os_release is None:
        path = pathlib.Path("/etc/os-release")
        if not path.exists():
            return None
        os_release = path.read_text(errors="replace")

    # Collected separately and tried in this order regardless of which line
    # comes first in the file: ID is the specific claim, ID_LIKE the
    # fallback family, and a file is not guaranteed to list them in that
    # order even though the common ones do.
    ids: list[str] = []
    likes: list[str] = []
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key == "ID":
            ids.extend(value.strip().strip('"').split())
        elif key == "ID_LIKE":
            likes.extend(value.strip().strip('"').split())

    for identifier in ids + likes:
        if identifier in _FAMILIES:
            return _FAMILIES[identifier]
    return None


def hints_for(
    environment: Environment,
    distro: str | None = None,
    capabilities: CapabilitySet | None = None,
) -> Iterator[Hint]:
    """Yield a Hint for each capability the environment is missing.

    `capabilities` is the set a live `connect()` actually assembled, which is
    the only way to know whether the pyguitest-window-control GNOME Shell
    extension is installed and enabled: that requires a real D-Bus call, and
    Environment/detect() deliberately makes none, so this is the one hint
    below that reasons from the connection already made rather than from
    `environment` alone. Passing None just skips that one hint.
    """
    from .session import Compositor, SessionType

    family = _PACKAGES.get(distro or detect_distro() or "", {})
    installer = family.get("install")

    def command(component: str) -> str | None:
        packages = family.get(component)
        return f"{installer} {packages}" if installer and packages else None

    if not environment.can_use_atspi:
        yield Hint(
            "AT-SPI",
            "element automation: buttons, text boxes, dropdowns. The only "
            "backend that works on GNOME",
            command("atspi"),
            # Upstream project names rather than package names: on an
            # unrecognised distribution these are what to search that
            # distribution's own repository for.
            packages="PyGObject, pyatspi, at-spi2-core",
        )
    # X11Backend is the only backend that serves any of tier 6 -- it needs a
    # real X11 connection, which XWayland still carries even though the
    # session is otherwise native Wayland. On a session with no X11
    # connection at all (pure Wayland, no XWayland), these five stay
    # NO_PATH regardless of what gets installed, so there is nothing to
    # hint there.
    if (
        environment.session_type in (SessionType.X11, SessionType.XWAYLAND)
        and not environment.has_xlib
    ):
        yield Hint(
            "tier-6 queries",
            "reading the global pointer position or keyboard/button state, "
            "rewriting another window's title, lowering a window, and "
            "querying a window's cursor -- impossible for an ordinary "
            "Wayland client on any compositor, but this session carries a "
            "real X11 connection (XWayland included), where X11Backend "
            "serves them. Pure pip, no distro package needed. (The cursor "
            "query is the weak one: it compares against the classic X "
            "cursor font, so it reads False on a themed desktop)",
            "pip install 'pyguitest[x11]'",
        )
    # WINDOW_PLACEMENT is a reliable stand-in for "the extension joined the
    # composite": AT-SPI never provides it (Mutter exposes no
    # foreign-toplevel protocol AT-SPI could read placement from), so on
    # Mutter it comes from nowhere else.
    if (
        environment.compositor is Compositor.MUTTER
        and capabilities is not None
        and Capability.WINDOW_PLACEMENT not in capabilities
    ):
        yield Hint(
            "window control extension",
            "moving, resizing, minimizing or hit-testing a window, and "
            "mapping one to its pid -- Mutter implements no foreign-toplevel "
            "protocol, so only the pyguitest-window-control GNOME Shell "
            "extension can reach this. Install it by hand: "
            "see gnome-shell-extension/README.md, then "
            "`gnome-extensions enable pyguitest-window-control@pyguitest.local`",
            None,
            installable=False,
        )
    # Capture has three routes and the advice differs sharply between them.
    # can_capture, not capture_tools: python-xlib on a real X session and
    # the Screenshot portal both capture with no tool installed, and
    # telling someone who already has either to install gnome-screenshot
    # is advice they do not need.
    captures_natively = (
        environment.has_xlib and environment.session_type is SessionType.X11
    )
    if not environment.can_capture:
        # The compositor decides before the distribution does: grim on
        # wlroots and spectacle on KWin are the tools that actually capture
        # there, and the per-distro `capture` name is only right for a
        # desktop with no native tool of its own.
        capture_package: str | None = (
            _CAPTURE_TOOL_BY_COMPOSITOR.get(environment.compositor.value)
            or family.get("capture")
            or _DEFAULT_CAPTURE_TOOL
        )
        capture_command = (
            f"{installer} {capture_package}" if installer and capture_package else None
        )
        yield Hint(
            "screenshots", "screen capture", capture_command, packages=capture_package
        )
    elif not environment.capture_tools and not captures_natively:
        # Only the portal can capture here, and automatic composition never
        # reaches it -- it is opt-in because its first use prompts. Without
        # this, `doctor` is silent on a session where a plain connect()
        # cannot screenshot at all, because can_capture is satisfied by a
        # backend the caller has to ask for by name.
        #
        # Installing a tool is deliberately not offered: on this session
        # no screenshot tool can work. The root-reading ones need a real X
        # server, and gnome-screenshot has not been able to reach GNOME
        # Shell's own screenshot interface since GNOME 42.
        yield Hint(
            "screenshots",
            "screen capture: no screenshot tool can work on this session, "
            'but the Screenshot portal can -- connect(backend="portalcapture"). '
            "It prompts for consent once, then the desktop remembers it",
            None,
            installable=False,
        )
    if not environment.image_tools:
        yield Hint(
            "template matching",
            "locating a control by an image of it, for widgets AT-SPI cannot see",
            command("imagemagick"),
            packages="ImageMagick",
        )
    uinput_usable = environment.uinput_writable and environment.has_evdev
    if not environment.input_tools and not uinput_usable:
        # ydotool is ranked last in tools.py precisely because it is
        # keymap-unsafe; recommending it unconditionally here contradicts
        # that ranking on any desktop that has a better option. xdotool
        # (X11, via XTest) and wtype (wlroots, typing only) are both
        # ordinarily packaged, unlike wdotool below.
        input_package: str | None
        if environment.session_type is SessionType.X11:
            input_package = "xdotool"
            ydotool_is_last_resort = False
        elif environment.compositor is Compositor.WLROOTS:
            input_package = "wtype"
            ydotool_is_last_resort = False
        else:
            # Same fallback as capture: on an unrecognised distribution
            # there is no package name to give, but ydotool is still the
            # tool to go looking for -- naming it beats naming nothing.
            input_package = family.get("input") or "ydotool"
            ydotool_is_last_resort = True
        input_command = (
            f"{installer} {input_package}" if installer and input_package else None
        )
        yield Hint(
            "input injection",
            "moving the pointer and typing"
            + (
                ". Both routes below go through /dev/uinput, so typed text "
                "follows the session's keyboard layout"
                if ydotool_is_last_resort
                else ""
            ),
            input_command,
            packages=input_package,
        )
        # xdotool and wtype need no /dev/uinput access at all -- only the
        # ydotool/uinput path this hint exists for does. And skip this one
        # when the device is already writable: what's missing there is
        # python-evdev, not group membership, and telling someone to
        # usermod/newgrp for a permission they already have is a dead end.
        if ydotool_is_last_resort and not environment.uinput_writable:
            yield Hint(
                "membership of the 'input' group",
                "/dev/uinput is root-only by default. Groups are set at "
                "login and inherited, so a new terminal is not enough -- run "
                "`newgrp input` for the current shell, or log out and back "
                "in for the session. On some distributions (Fedora included) "
                "the group is necessary but not sufficient -- see below",
                f"sudo usermod -aG input {os.environ.get('USER', '$USER')}",
            )


_WIDTH = 78
"""Wrap column for a hint's prose. Fixed rather than read from the terminal:
`doctor` output is routinely pasted into bug reports and issue comments, and
a report that reflows to whatever width the reporter's terminal happened to
be is harder to read than one that is always the same shape."""


def _wrap(text: str, indent: str = "  ") -> str:
    """Wrap one hint's prose to `_WIDTH`, indenting every line.

    Long-standing readability problem this fixes: each hint was emitted as
    one unbroken line, and the richer ones ran to several hundred
    characters -- a wall of text in any terminal.

    Continuation lines are indented four spaces past `indent` so the
    component name at the start of the first line stays scannable when
    several hints are listed together.
    """
    return textwrap.fill(
        text,
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent + "    ",
        # A hint names commands, flags and paths (`niri msg`, `pip install
        # 'pyguitest[x11]'`, /dev/uinput). Breaking those apart would turn
        # something the reader is meant to copy into two half-lines.
        break_long_words=False,
        break_on_hyphens=False,
    )


def advice(
    environment: Environment,
    distro: str | None = None,
    capabilities: CapabilitySet | None = None,
) -> str:
    """Render the hints as text, or say that nothing is missing."""
    found = list(hints_for(environment, distro, capabilities=capabilities))
    if not found:
        return "Nothing missing: every capability this desktop can offer is available."

    lines = ["To unlock more capabilities:"]
    for hint in found:
        lines.append("\n" + _wrap(f"{hint.component} -- {hint.why}"))
        # Blank line first: now that the prose above wraps to several
        # indented lines, a command at the same indent reads as one more
        # sentence rather than as the thing to run.
        if hint.command:
            lines.append(f"\n      {hint.command}")
        elif hint.installable:
            if hint.packages:
                lines.append(
                    f"\n      ({hint.packages} -- install through your distribution)"
                )
            else:
                lines.append("\n      (install it through your distribution)")
    # The "input" group hint only appears alongside the uinput/ydotool
    # recommendation (xdotool and wtype need no /dev/uinput access), so its
    # presence is the signal that this caveat -- specific to that path -- applies.
    if any(h.component == "membership of the 'input' group" for h in found):
        lines.append(
            "\n  For input, python3-evdev needs no daemon -- pyguitest drives"
            "\n  /dev/uinput itself. ydotool is the alternative, and needs "
            "ydotoold running."
            "\n  Neither is keymap-safe. The only keymap-safe option on GNOME "
            "is wdotool,"
            "\n  which no distribution packages yet: "
            "https://github.com/cushycush/wdotool"
        )
        # Joining the group does nothing if the device node grants that
        # group nothing, which is the Fedora default (root:root 0600) --
        # a silent dead end: `id -nG` looks right, the group is real, and
        # every write still fails. Checked and reported here rather than
        # left for the user to discover.
        lines.append(
            "\n  Check the device node itself, not just the group:"
            "\n      ls -l /dev/uinput      # want: crw-rw---- root input"
            "\n  If it is root:root 0600, the 'input' group grants nothing "
            "and a"
            "\n  udev rule is needed as well:"
            '\n      echo \'KERNEL=="uinput", GROUP="input", MODE="0660", '
            'OPTIONS+="static_node=uinput"\' \\'
            "\n          | sudo tee /etc/udev/rules.d/99-uinput.rules"
            "\n      sudo udevadm control --reload-rules"
            "\n  That applies from the next boot; for the running node now:"
            "\n      sudo chgrp input /dev/uinput && sudo chmod g+rw /dev/uinput"
        )
    if any(h.component == "AT-SPI" for h in found):
        lines.append(
            "\n  Then, for AT-SPI:  pip install 'pyguitest[atspi]'"
            "\n  (from a checkout:  pip install '.[atspi]')"
        )
    return "\n".join(lines)
