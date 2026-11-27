"""Output size and scale on KDE, through KScreen's own reporting tool.

KWin was the one Wayland compositor with no `SCREEN_INFO` provider at all.
Every other one has had something to ask -- `gnomeshell` for Mutter, the
compositor sockets for sway/Hyprland/niri, COSMIC's own protocol -- while on
KDE `gui.screens()` raised `CapabilityUnsupported`, because `kdotool`, the
"windows" member there, reports no outputs and declines the capability (see
`KdotoolBackend.capabilities`, which subtracts it).

That absence was not confined to callers asking for screens. Measured on a
Fedora 44 KWin session scaled to 200%: the recorder's resolver asks
`session.screens()` to decide whether anything is scaled
(`_any_screen_scaled`), catches the refusal, and answers False -- so on KDE
it positively believed the session was unscaled while captured coordinates
were double the space it was hit-testing them against. The one safeguard
against the Wayland physical/logical mismatch was inert on the one
compositor that could not answer.

**Why a tool rather than D-Bus.** `org.kde.KWin.supportInformation()` does
report outputs, and needs no tool, but it answers in prose meant for bug
reports and gives *logical* geometry (`Geometry: 0,0,640x400` at scale 2),
so the physical pixels `Screen` is defined in would have to be
reconstructed by multiplying back up. `kscreen-doctor --json` answers
structured JSON carrying both the mode size and the scale, which is exactly
the pair wanted, and it ships with plasma-workspace rather than being a
third-party add-on. Measured at scale 1 and scale 2 on the same output:
`size` is the mode size (1280x800) at both, and `scale` is 1 then 2.

**What `scale` means here.** KWin's own scale factor, which is what GNOME's
member already reports (Mutter's logical layout factor) rather than the
DPI-over-96 `Screen.scale` is defined as. On this output the EDID physical
size reads 0x0mm, so a DPI cannot be computed from it at all. Reporting the
compositor's factor matches the dominant existing behaviour and is the
number a caller reasoning about a scaled session needs; the wider question
of `Screen.scale` carrying different quantities per backend is not settled
here.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from ..capabilities import Capability, CapabilitySet
from ..errors import PyGUITestError
from .base import GUIBackend, Screen

__all__ = ["KScreenBackend", "available"]

TOOL = "kscreen-doctor"

_TIMEOUT = 5.0
"""Seconds a `kscreen-doctor --json` call gets.

It talks to KWin over the session bus, so it can hang exactly as any other
compositor call can. Short, because this is one read of a handful of
outputs, and a caller asking for screens is usually in the middle of
something.
"""


def available() -> bool:
    """Whether KScreen's reporting tool is installed."""
    return shutil.which(TOOL) is not None


class KScreenBackend(GUIBackend):
    """Output size and scale on KDE, read from `kscreen-doctor --json`."""

    name = "kscreen"

    def __init__(self, environment=None, runner=None):
        """Read outputs through `runner`, or `kscreen-doctor` by default."""
        self.environment = environment
        self._runner = runner or self._run

    @property
    def capabilities(self):
        """Output enumeration only; every other operation is another member's."""
        return CapabilitySet({Capability.SCREEN_INFO})

    def _run(self) -> str:
        """`kscreen-doctor --json`'s stdout, or raise naming what went wrong."""
        try:
            completed = subprocess.run(
                [TOOL, "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=_TIMEOUT,
                check=False,
            )
        except OSError as exc:
            raise PyGUITestError(f"could not run {TOOL}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(
                f"{TOOL} did not answer within {_TIMEOUT:g}s; KWin may be wedged"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            raise PyGUITestError(
                f"{TOOL} failed: {detail[-1] if detail else completed.returncode}"
            )
        return completed.stdout

    def screens(self):
        """Every enabled output, with its mode size and KWin's scale factor.

        Disabled and disconnected outputs are left out: `screens()` is "every
        output" in the sense of every one the desktop is using, and KScreen
        lists eight `Virtual-*` connectors on a VM of which one is live.
        """
        self.require(Capability.SCREEN_INFO)
        try:
            report = json.loads(self._runner())
        except ValueError as exc:
            raise PyGUITestError(f"{TOOL} did not return JSON: {exc}") from exc
        found: list[Screen] = []
        for output in report.get("outputs") or ():
            if not output.get("enabled"):
                continue
            size = output.get("size") or {}
            width, height = size.get("width"), size.get("height")
            if not isinstance(width, int) or not isinstance(height, int):
                continue
            try:
                scale = float(output.get("scale", 1.0))
            except (TypeError, ValueError):
                scale = 1.0
            found.append(
                Screen(
                    index=len(found),
                    width=width,
                    height=height,
                    scale=scale,
                    name=str(output.get("name") or ""),
                )
            )
        return found
