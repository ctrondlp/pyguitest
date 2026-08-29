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
from collections.abc import Iterator

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

    __slots__ = ("component", "why", "command")

    def __init__(self, component: str, why: str, command: str | None) -> None:
        """Describe a gap, what it costs, and the command that closes it."""
        self.component = component
        self.why = why
        self.command = command

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


def hints_for(environment: Environment, distro: str | None = None) -> Iterator[Hint]:
    """Yield a Hint for each capability the environment is missing."""
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
        # grim is the wlroots-native tool; recommending gnome-screenshot
        # there installs a tool that captures nothing on that compositor.
        if environment.compositor is Compositor.WLROOTS:
            capture_command = f"{installer} grim" if installer else None
        else:
            capture_command = command("capture")
        yield Hint("screenshots", "screen capture", capture_command)
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
        )
    if not environment.image_tools:
        yield Hint(
            "template matching",
            "locating a control by an image of it, for widgets AT-SPI cannot see",
            command("imagemagick"),
        )
    if not environment.input_tools and not environment.uinput_writable:
        # ydotool is ranked last in tools.py precisely because it is
        # keymap-unsafe; recommending it unconditionally here contradicts
        # that ranking on any desktop that has a better option. xdotool
        # (X11, via XTest) and wtype (wlroots, typing only) are both
        # ordinarily packaged, unlike wdotool below.
        if environment.session_type is SessionType.X11:
            input_command = f"{installer} xdotool" if installer else None
            ydotool_is_last_resort = False
        elif environment.compositor is Compositor.WLROOTS:
            input_command = f"{installer} wtype" if installer else None
            ydotool_is_last_resort = False
        else:
            input_command = command("input")
            ydotool_is_last_resort = True
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
        )
        # xdotool and wtype need no /dev/uinput access at all -- only the
        # ydotool/uinput path this hint exists for does.
        if ydotool_is_last_resort:
            yield Hint(
                "membership of the 'input' group",
                "/dev/uinput is root-only by default. Groups are set at "
                "login and inherited, so a new terminal is not enough -- run "
                "`newgrp input` for the current shell, or log out and back "
                "in for the session. On some distributions (Fedora included) "
                "the group is necessary but not sufficient -- see below",
                f"sudo usermod -aG input {os.environ.get('USER', '$USER')}",
            )


def advice(environment: Environment, distro: str | None = None) -> str:
    """Render the hints as text, or say that nothing is missing."""
    found = list(hints_for(environment, distro))
    if not found:
        return "Nothing missing: every capability this desktop can offer is available."

    lines = ["To unlock more capabilities:"]
    for hint in found:
        lines.append(f"\n  {hint.component} -- {hint.why}")
        if hint.command:
            lines.append(f"      {hint.command}")
        else:
            lines.append("      (install it through your distribution)")
    # The "input" group hint only appears alongside the uinput/ydotool
    # recommendation (xdotool and wtype need no /dev/uinput access), so its
    # presence is the signal that this caveat -- specific to that path -- applies.
    if any(h.component == "membership of the 'input' group" for h in found):
        lines.append(
            "\n  For input, python3-evdev needs no daemon -- pyguitest drives "
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
