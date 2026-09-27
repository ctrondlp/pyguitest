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
in lifetime as well as mechanism: the tools here fork a daemon that outlives
the process, while the portal path stops serving the selection when the
Session closes. Even that is an upper bound rather than a promise about
what a given application then sees -- see PortalBackend.set_clipboard for
the caching measured underneath it on GNOME.

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
X11/Wayland selection that middle-click paste reads, which the Linux tools
here support as a second named selection rather than a separate command, so
this is one argument, not a second backend. It is also the one thing that
does not carry over to macOS: the pasteboard there is a single selection, so
`_no_primary` refuses the flag rather than answering it from the clipboard --
the same read `portal.py` makes for the Clipboard interface, which has no
PRIMARY either.

macOS is served by `pbcopy`/`pbpaste`, which ship with the OS. That is the
route `screencapture` takes for capture and the reason a bare `pip install
pyguitest` reaches the clipboard on a Mac at all; ADR 004 left the choice
open between this and an `NSPasteboard` element in the `macos` backend and
named this one the obvious way in. Measured live on macOS 26.7 over SSH,
against the console user's own pasteboard: `printf abc123 | pbcopy` followed
by `pbpaste` in a *later* process returned exactly `abc123`, byte for byte,
with no trailing newline added, and `héllo — 日本語 ✅` survived the same
round trip. That is the reverse of the X11/Wayland story above and is worth
naming, because it is why `_FORKS_ON_WRITE` does not include `pbcopy`: the
pasteboard is stored by the `pbs` server, so the writing process exits and
the value stays, and nothing inherited by a fork can hold a pipe open. Its
write call therefore keeps `PIPE` and reports stderr like any other failure.

`pbcopy` is also why `tools.ExternalTool` grew `probe_version`. Every run of
it rewrites the pasteboard, and the version flag is no exception -- measured
with a sentinel value on the clipboard, `pbcopy --version < /dev/null` exited
0 having left it empty. A diagnostic that silently destroys what the user had
copied is worse than a blank version column, so `doctor` does not probe it.
"""

import subprocess

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, PyGUITestError
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
    # No True key, deliberately: macOS has one pasteboard, so a call asking
    # for PRIMARY is refused by `_no_primary` rather than answered from the
    # clipboard. `-Prefer txt`/`-Prefer rtf` would select a *flavor* of the
    # same selection, not a second one, so neither belongs here.
    "pbcopy": {
        False: ["pbpaste"],
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
    "pbcopy": {
        False: ["pbcopy"],
    },
}

# Tools that fork into the background on a write, and so must not be handed
# PIPE for stdout/stderr -- see the module docstring. macOS's `pbcopy` is
# deliberately absent: the pasteboard lives in the `pbs` server, so the
# command is an ordinary client that exits, and capturing its stderr costs
# nothing and buys a real error message on failure.
_FORKS_ON_WRITE = frozenset({"wl-copy", "xclip", "xsel"})


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

        `input_text is not None` is the write/read switch for stdout/stderr
        as well as for stdin: a write to a tool that forks a daemon leaves
        that daemon holding whatever those are, so for `_FORKS_ON_WRITE` they
        must be DEVNULL rather than PIPE -- see the module docstring on the
        hang that confirmed this the hard way, and the same docstring on why
        `pbcopy`, whose selection lives in the pasteboard server rather than
        in a fork, is not one of them and keeps them captured. Losing stderr
        text on a forking write failure is the accepted cost there; the
        alternative is a 15-second hang on every successful write.
        """
        capture = input_text is None or self.tool.name not in _FORKS_ON_WRITE
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
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

    def _no_primary(self, primary):
        """Refuse PRIMARY for a tool whose platform has only one selection.

        macOS keeps a single pasteboard, so `_READ`/`_WRITE` have no `True`
        key for `pbcopy` at all. Answering a `primary=True` call from the
        clipboard would answer a different question than the one asked -- and
        the two selections are independent everywhere they do exist, so the
        difference matters to a caller. This is the same typed refusal
        `portal.py` makes for the Clipboard D-Bus interface, which has no
        PRIMARY either.
        """
        if primary and True not in _READ[self.tool.name]:
            raise CapabilityUnsupported(
                Capability.CLIPBOARD,
                self.name,
                "this tool's platform keeps a single selection: macOS's "
                "pasteboard has no PRIMARY, and answering from the clipboard "
                "would be answering a different question",
            )

    def get_clipboard(self, primary=False):
        """The current text content of the clipboard, or of PRIMARY."""
        self.require(Capability.CLIPBOARD)
        self._no_primary(primary)
        return self._runner(_READ[self.tool.name][primary])

    def set_clipboard(self, text, primary=False):
        """Replace the text content of the clipboard, or of PRIMARY.

        See the module docstring and `_run` on why stdout/stderr must be
        DEVNULL rather than PIPE for this call on the Linux tools
        specifically: those fork into the background on their own before this
        returns, which is what keeps the clipboard answering after they do,
        and a PIPE the fork inherits never reaches EOF. `pbcopy` does not
        fork, for the reason named in the module docstring.
        """
        self.require(Capability.CLIPBOARD)
        self._no_primary(primary)
        self._runner(_WRITE[self.tool.name][primary], input_text=text)
