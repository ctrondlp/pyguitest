"""Clipboard text, adapting the desktop's own clipboard tool.

Another capability X11::GUITest never had, and another operation with no
core Wayland protocol: reading and writing the clipboard is deliberately
scoped to a client that owns a surface and (for reading) has focus, which
this package's backends generally do not hold. What each desktop offers
instead is one of the same escape hatches used elsewhere in this
package -- a CLI tool speaking a compositor-specific mechanism -- so this
follows capture.py's shape: pick a tool, shell out to it, return text.

wl-clipboard (wl-copy/wl-paste) speaks wlr-data-control-unstable-v1, an
unprivileged protocol with no consent dialog. Confirmed live on KDE Plasma
6 as well as the wlroots compositors it was written for -- KWin is not a
wlroots compositor, but implements this protocol anyway (see
tools.ExternalTool.mutter_incompatible). Mutter implements it not at all,
so GNOME has no member in CLIPBOARD_TOOLS and no *tool* can serve the
clipboard there.

GNOME is served instead by `portal.py`, through
org.freedesktop.portal.Clipboard on the RemoteDesktop session that backend
already negotiates -- `connect(backend="portal", backend_options={
"clipboard": True})`. This module's earlier claim that Mutter had "neither
this protocol nor a reachable portal path" was true when written and is
not now: xdg-desktop-portal 1.22 with xdg-desktop-portal-gnome 51 carries
the Clipboard interface (probed live, 2026-09-01). Note the two paths differ
in lifetime as well as mechanism -- see PortalBackend.set_clipboard: the tools
here fork a daemon that outlives the process, while the portal path holds
the selection only while the Session does.

Persistence is the one real trap, and it is not only about waiting for the
right process to exit. The X11 and Wayland clipboard protocols both work
by asking the client that last claimed ownership of the selection to hand
over its content on demand, rather than storing it centrally -- so a
`set_clipboard()` implemented as "run a tool, let it exit" would clear the
clipboard the instant that process exits, often before the paste that was
the whole point. wl-copy, xclip and xsel all handle this the same way:
each forks into the background on write and keeps running there to keep
answering paste requests, confirmed live for wl-copy on KDE/KWin, where
the forked process was still resident in `ps` after a plain shell
invocation had already returned 0.

That fork is also what makes `subprocess.run(..., capture_output=True)`
the wrong way to call the write side -- confirmed live, the hard way,
immediately after the fact above was confirmed the easy way. A forked
child inherits its parent's file descriptors, pipes included, so the
daemonized grandchild ends up holding the write end of the very
stdout/stderr pipes `communicate()` is reading from, open, even though it
never writes to them again. `communicate()` waits for both the tracked
process to exit *and* those pipes to reach EOF; the process exits
immediately, but the pipes never close, so the read hangs for the full
subprocess timeout. `echo text | wl-copy` from a shell does not hit this,
because the terminal's stdout/stderr are not pipes `communicate()` is
waiting to drain -- which is exactly why the by-hand spike that motivated
this backend looked clean and the first real run through
`subprocess.run()` was not. The fix (see `_run`) is to give the write call
`DEVNULL` rather than `PIPE` for stdout/stderr, so there is no pipe left
open for the daemon to inherit.

`primary=True` reaches PRIMARY instead of the clipboard proper -- the
X11/Wayland selection that middle-click paste reads, which every tool
here supports as a second named selection rather than a separate command,
so this is one argument, not a second backend.
"""

import subprocess

from ..capabilities import Capability, CapabilitySet
from ..errors import PyGUITestError
from .base import GUIBackend

__all__ = ["ToolClipboardBackend"]

_SUBPROCESS_TIMEOUT = 15

# Read (stdout -> text) and write (text -> stdin) argv per tool, keyed by
# which selection: False is the clipboard proper, True is PRIMARY. Text
# goes over stdin/stdout rather than argv so arbitrary content -- including
# whatever a shell would treat specially -- never needs quoting, and so
# there is no argv length limit to hit on a large paste.
_READ = {
    "wl-copy": {
        False: ["wl-paste", "--no-newline"],
        True: ["wl-paste", "--no-newline", "--primary"],
    },
    "xclip": {
        False: ["xclip", "-selection", "clipboard", "-out"],
        True: ["xclip", "-selection", "primary", "-out"],
    },
    "xsel": {
        False: ["xsel", "--clipboard", "--output"],
        True: ["xsel", "--primary", "--output"],
    },
}

_WRITE = {
    "wl-copy": {
        False: ["wl-copy"],
        True: ["wl-copy", "--primary"],
    },
    "xclip": {
        False: ["xclip", "-selection", "clipboard"],
        True: ["xclip", "-selection", "primary"],
    },
    "xsel": {
        False: ["xsel", "--clipboard", "--input"],
        True: ["xsel", "--primary", "--input"],
    },
}


class ToolClipboardBackend(GUIBackend):
    """Read and write clipboard text through whichever tool is installed."""

    def __init__(self, tool, runner=None):
        """Drive `tool`, optionally through an injected `runner`."""
        if tool.name not in _READ:
            raise PyGUITestError(f"no clipboard commands for {tool.name!r}")
        self.tool = tool
        self._runner = runner or self._run

    # A read-only override of GUIBackend's plain, writable `name` attribute
    # -- see the same note in capture.py and input.py.
    @property
    def name(self) -> str:  # type: ignore[override]
        """Identifier for this backend, e.g. 'clipboard:wl-copy'."""
        return f"clipboard:{self.tool.name}"

    @property
    def capabilities(self):
        """Clipboard text only."""
        return CapabilitySet({Capability.CLIPBOARD})

    def _run(self, argv, input_text=None):
        """Run `argv`, feeding `input_text` on stdin, and return its stdout.

        `input_text is not None` is also the write/read switch that decides
        stdout/stderr's fate: a write forks a daemon that inherits whatever
        those are, so they must be DEVNULL, not PIPE -- see the module
        docstring on the hang that confirmed this the hard way. A read
        needs its stdout captured and never forks, so PIPE is safe there.
        Losing stderr text on a write failure is the accepted cost; the
        alternative is a 15-second hang on every successful write.
        """
        capture = input_text is None
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(
                f"{' '.join(argv)} did not finish within "
                f"{_SUBPROCESS_TIMEOUT}s; it is installed but not responding"
            ) from exc
        if result.returncode != 0:
            detail = (
                (result.stderr or "").strip() or "no output"
                if capture
                else "stderr not captured on a write call, to avoid a hang "
                "on a tool that forks into the background -- see clipboard.py"
            )
            raise PyGUITestError(
                f"{' '.join(argv)} failed ({result.returncode}): {detail}"
            )
        return result.stdout or ""

    def get_clipboard(self, primary=False):
        """The current text content of the clipboard, or of PRIMARY."""
        self.require(Capability.CLIPBOARD)
        return self._runner(_READ[self.tool.name][primary])

    def set_clipboard(self, text, primary=False):
        """Replace the text content of the clipboard, or of PRIMARY.

        See the module docstring and `_run` on why stdout/stderr must be
        DEVNULL rather than PIPE for this call specifically: the tool forks
        into the background on its own before this returns, which is what
        keeps the clipboard answering after it does, and a PIPE the fork
        inherits never reaches EOF.
        """
        self.require(Capability.CLIPBOARD)
        self._runner(_WRITE[self.tool.name][primary], input_text=text)
