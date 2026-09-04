#!/usr/bin/env python3
"""One-off live validation of the Clipboard portal path (`portal`, GNOME).

The one part of the Wayland gap plan's stage 1 that shipped without ever
running against a real desktop, and the one that least deserves the
benefit of the doubt. `PortalBackend.set_clipboard` hands the portal no
bytes: `SetSelection` declares *ownership*, and the portal comes back
later -- once per paste -- with a `SelectionTransfer` signal that a GLib
loop on a daemon thread has to answer. Every unit test of that path calls
`_on_transfer` itself, so all of them still pass on a desktop where the
signal never arrives, and the symptom there is not an exception: the
clipboard reads as **empty** to the whole desktop.

So the question is not "did `set_clipboard()` return" but "can a
*different process* paste what it wrote". Five phases, in this order:

0. Connect (`portal` with `clipboard=True` -- one consent dialog) and
   read whatever the desktop already had, through the portal.
1. **The read path, and a control.** An external tool writes a known
   value and `get_clipboard()` must see it. Run first because a failure
   here means phase 2 cannot tell a broken write from a broken bridge.
2. **The write path -- the decisive phase.** `set_clipboard()`, then read
   with the external tool. Then read a second time (the loop has to
   answer every paste, not just the first), then write a second value
   (which must replace the first rather than append to it).
3. **PRIMARY.** Both halves must refuse it: the portal interface has no
   PRIMARY selection at all, and answering from the clipboard proper
   would answer a different question than the one asked.
4. **Lifetime.** After `close()` this process must stop answering
   transfers -- that, and not "the content is gone", is what the portal
   path actually promises. The two are easy to confuse from outside: on
   GNOME the content stayed pasteable after `close()` while *no* transfer
   fired, because Mutter's XWayland bridge had cached the bytes. The
   counter below is what tells those apart.

`_on_transfer` is wrapped in a counter for the whole run, because how
many times the portal actually asked for the bytes is the most
diagnostic fact available: zero is "the compositor never asked", which is
a different bug from "we answered with the wrong bytes", and the two look
identical from the outside.

**The external reader is the awkward part on GNOME** -- the desktop this
whole path exists for. Mutter implements no wlr-data-control, so
`wl-paste` cannot read the clipboard there at all; that is *why* this
backend was written. `xclip` can, through XWayland, because Mutter
bridges the X11 selection to the Wayland one. That makes the clipboard
the one job an `x11_only` tool does correctly on Wayland, which is why
this script picks its reader by hand instead of asking
`tools.CLIPBOARD_TOOLS` -- that helper excludes `xclip` on a Wayland
session on principle, and for window enumeration it is right to.

    python3 _portal_clipboard_validate.py
"""

import os
import shutil
import subprocess
import sys
import time

import pyguitest
from pyguitest import BackendUnavailable, Capability, Compositor, PermissionRequired
from pyguitest.errors import CapabilityUnsupported

TOOL_TIMEOUT = 5.0
"""Bound on each external tool call. A paste that needs longer than this
has already failed the thing being tested -- a real one is a round trip to
a compositor on the same machine."""

SETTLE = 3.0
"""Seconds `read_until` will keep re-reading before giving up. Long enough
that a slow selection hand-over is not mistaken for a broken one, short
enough that a genuine failure does not stall the run."""


class TransferCounter:
    """Count the `SelectionTransfer` signals the portal actually delivers.

    Installed on the backend instance before anything is written, so the
    subscription `_start_serving` makes on the first `set_clipboard` picks
    up this wrapper rather than the original bound method.
    """

    def __init__(self, backend):
        """Wrap `backend`'s transfer handler, leaving its behaviour alone."""
        self.count = 0
        self._inner = backend._on_transfer
        backend._on_transfer = self

    def __call__(self, *args, **kwargs):
        self.count += 1
        return self._inner(*args, **kwargs)


class SelectionReadLog:
    """Record which MIME type each `SelectionRead` asked for, and which won.

    The read path tries several spellings of "text" in turn, because the
    owner of the selection picks what it offers and is under no obligation
    to agree with us -- see `_CLIPBOARD_READ_MIMES`. Which spelling
    actually answers is invisible from outside and worth knowing: if one
    of them never wins, it can come off the list.
    """

    def __init__(self, backend):
        """Wrap `backend`'s fd call, leaving its behaviour alone."""
        self.attempted = []
        self.won = []
        self._inner = backend._call_for_fd
        backend._call_for_fd = self

    def __call__(self, method, signature, args):
        if method != "SelectionRead":
            return self._inner(method, signature, args)
        self.attempted.append(args[-1])
        result = self._inner(method, signature, args)
        self.won.append(args[-1])
        return result

    def report(self):
        """One line naming the type that answered, and the ones that did not."""
        if not self.won:
            return f"no type answered; tried {', '.join(self.attempted) or 'none'}"
        winner = self.won[-1]
        refused = list(dict.fromkeys(m for m in self.attempted if m not in self.won))
        if not refused:
            return winner
        return f"{winner} (after {', '.join(refused)})"


def external_tool(environment):
    """Pick a tool that reads this desktop's clipboard from outside.

    Returns `(name, read_argv, write_argv)`, or None where nothing here
    can serve as an independent witness. On Mutter that is `xclip` or
    `xsel` over XWayland and never `wl-paste`; everywhere else
    wl-clipboard speaks the compositor's own protocol and is the better
    witness, because it shares no transport with the X server at all.
    """
    on_mutter = environment.compositor is Compositor.MUTTER
    if not on_mutter and shutil.which("wl-paste") and shutil.which("wl-copy"):
        return ("wl-clipboard", ["wl-paste", "--no-newline"], ["wl-copy"])
    if not os.environ.get("DISPLAY"):
        return None
    if shutil.which("xclip"):
        return (
            "xclip",
            ["xclip", "-selection", "clipboard", "-o"],
            ["xclip", "-selection", "clipboard", "-i"],
        )
    if shutil.which("xsel"):
        return (
            "xsel",
            ["xsel", "--clipboard", "--output"],
            ["xsel", "--clipboard", "--input"],
        )
    return None


def read_external(argv, quiet=False):
    """Read the clipboard with the external tool. None if the tool failed.

    An empty string is a real answer and a distinct one -- it is exactly
    what a broken `SetSelection` looks like from outside -- so it is never
    folded into the failure case. `quiet` is for `read_until`, which would
    otherwise print the same complaint once per poll.
    """
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=TOOL_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if not quiet:
            print(f"  {argv[0]} could not run: {exc}")
        return None
    if done.returncode != 0:
        if not quiet:
            message = done.stderr.decode("utf-8", errors="replace").strip()
            print(f"  {argv[0]} exited {done.returncode}: {message or '(silently)'}")
        return None
    return done.stdout.decode("utf-8", errors="replace").rstrip("\n")


def read_until(argv, wanted, match=True):
    """Read until the value matches (or stops matching), or SETTLE runs out.

    Selection ownership is asynchronous everywhere, and on GNOME it also
    has to cross Mutter's XWayland bridge, so a read fired in the same
    millisecond as the write can legitimately still see the old owner.
    Polling reports a working path as fast as it works, and still fails a
    broken one -- where a fixed sleep would have to guess, and guess long.
    """
    deadline = time.monotonic() + SETTLE
    while True:
        value = read_external(argv, quiet=True)
        if (value == wanted) is match or time.monotonic() >= deadline:
            if value is None:
                read_external(argv)  # once, loudly, to show why
            return value
        time.sleep(0.2)


def write_external(argv, text):
    """Put `text` on the clipboard with the external tool, as the control.

    DEVNULL rather than a pipe, and that is not tidiness: every one of
    these tools forks into the background on write to keep answering paste
    requests after the command returns, and the fork inherits the pipe --
    which then never reaches EOF, so a captured write hangs for the whole
    timeout and looks like a tool that failed. `backends/clipboard.py`'s
    `_run` carries the same note; it found this the hard way.
    """
    try:
        done = subprocess.run(
            argv,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TOOL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  {argv[0]} could not run: {exc}")
        return False
    if done.returncode != 0:
        print(f"  {argv[0]} exited {done.returncode}")
        return False
    return True


def marker(label):
    """A value nothing else on this desktop will have put there."""
    return f"pyguitest-portal-clipboard-{label}-{int(time.time())}"


results = []


def record(label, passed, detail=""):
    """Note one check's outcome and print it as it happens."""
    results.append((label, passed))
    mark = "ok  " if passed else "FAIL"
    print(f"  [{mark}] {label}{f' -- {detail}' if detail else ''}")


# ---------------------------------------------------------------------- setup

environment = pyguitest.detect()
witness = external_tool(environment)
if witness is None:
    sys.exit(
        "no external clipboard tool to check against. On GNOME install "
        "xclip (it reads the Wayland clipboard through XWayland, which "
        "Mutter bridges); elsewhere wl-clipboard. Without one this script "
        "can only ask the portal about itself, which is the thing in doubt."
    )
tool_name, read_argv, write_argv = witness
print(f"compositor: {environment.compositor.value}")
print(f"external witness: {tool_name} ({' '.join(read_argv)})")

print("\nconnecting -- a consent dialog will appear; click Allow")
try:
    gui = pyguitest.connect(backend="portal", backend_options={"clipboard": True})
except (BackendUnavailable, PermissionRequired) as exc:
    sys.exit(f"could not open a portal session with clipboard access: {exc}")

print(f"forced backend: {gui.backend.name}")
print("capabilities offered:", sorted(c.name for c in gui.backend.capabilities))
if not gui.supports(Capability.CLIPBOARD):
    gui.close()
    sys.exit(
        "CLIPBOARD is missing from a session that asked for clipboard=True. "
        "The portal grants it at Start, so a RequestClipboard that failed or "
        "arrived late leaves exactly this -- see PortalBackend._negotiate."
    )

transfers = TransferCounter(gui.backend)
reads = SelectionReadLog(gui.backend)

# ---------------------------------------------------------- phase 0: read-only

print("\n[0] what the desktop already had")
try:
    existing = gui.get_clipboard()
    print(f"  portal read {len(existing)} characters: {existing[:60]!r}")
except Exception as exc:  # noqa: BLE001 -- a first read is allowed to fail
    print(f"  portal read raised: {exc}")

# ------------------------------------------------- phase 1: read path + control

print(f"\n[1] read path: {tool_name} writes, the portal reads")
control = marker("control")
if not write_external(write_argv, control):
    record("external control write", False, f"{tool_name} could not write")
    read_path_ok = False
else:
    # Polled for the same reason read_until exists: the tool has claimed
    # the selection asynchronously, and on GNOME Mutter still has to bridge
    # it from X11 to Wayland before the portal can see it at all.
    seen, read_path_ok = "", False
    deadline = time.monotonic() + SETTLE
    while True:
        try:
            seen = gui.get_clipboard()
        except Exception as exc:  # noqa: BLE001 -- phase 2 is the point
            # Retried, not abandoned. An earlier version broke out here,
            # and that is what turned one transient refusal into a
            # reported failure of the read path -- the same type read
            # cleanly on the next run. A hand-over in progress is not an
            # answer; only the deadline is.
            seen = f"<raised {type(exc).__name__}: {exc}>"
        else:
            read_path_ok = seen == control
        if read_path_ok or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    print(f"  answered by: {reads.report()}")
    record(
        "get_clipboard() sees what another process wrote",
        read_path_ok,
        # Not truncated when it is an exception: the first run of this
        # script cut "AttributeError: 'NoneType' object has no attribute
        # 'get'" off at 60 characters, which hid the actual bug.
        seen if seen.startswith("<raised") else f"read {seen[:60]!r}",
    )

# ----------------------------------------------- phase 2: the write path itself

print("\n[2] write path: the portal writes, another process reads")
first = marker("first")
gui.set_clipboard(first)
started = time.monotonic()
pasted = read_until(read_argv, first)
elapsed = time.monotonic() - started
decisive = pasted == first
record(
    f"{tool_name} pastes what set_clipboard() wrote",
    decisive,
    # Mostly the tool's own start-up cost -- this is not a measurement of
    # the portal round trip, only a guard against one that takes seconds.
    # Includes the tool's own start-up, and a poll interval if the
    # selection took a moment to hand over -- a guard against a round trip
    # that takes seconds, not a measurement of one.
    f"read {pasted!r} after {elapsed * 1000:.0f}ms",
)
print(f"  SelectionTransfer fired {transfers.count} time(s) so far")

again = read_until(read_argv, first)
record(
    "a second paste is served too (the loop is not one-shot)",
    again == first,
    f"read {again!r}",
)

second = marker("second")
gui.set_clipboard(second)
replaced = read_until(read_argv, second)
record(
    "a second write replaces the first, not appends",
    replaced == second,
    f"read {replaced!r}",
)
print(f"  SelectionTransfer fired {transfers.count} time(s) in total")

# ------------------------------------------------------------- phase 3: PRIMARY

print("\n[3] PRIMARY, which this interface does not have")
try:
    gui.get_clipboard(primary=True)
    record("get_clipboard(primary=True) refuses", False, "it returned instead")
except CapabilityUnsupported as exc:
    record("get_clipboard(primary=True) refuses", True, str(exc)[:60])
try:
    gui.set_clipboard("primary", primary=True)
    record("set_clipboard(primary=True) refuses", False, "it returned instead")
except CapabilityUnsupported as exc:
    record("set_clipboard(primary=True) refuses", True, str(exc)[:60])

# ------------------------------------------------------------ phase 4: lifetime

print("\n[4] lifetime: this process should stop serving after close()")
before_close = transfers.count
gui.close()
after_close = read_until(read_argv, second, match=False)
served_after_close = transfers.count > before_close
# Three outcomes, not two, and the transfer counter is what separates
# them. Only the middle one is a fault of this package: it means close()
# left the service loop answering. A value that survives with *no*
# transfer came from a cache further down -- measured on GNOME
# 2026-09-04, where Mutter's XWayland bridge kept the bytes -- and says
# nothing about whether this backend stopped serving, which is all
# set_clipboard() ever claimed.
record(
    "close() stopped this process serving the selection",
    not served_after_close,
    f"{transfers.count - before_close} transfer(s) after close()",
)
if after_close == second and not served_after_close:
    print(
        "  NOTE: the content is still pasteable, but no transfer came from\n"
        "  here to produce it -- something downstream cached the bytes. On\n"
        "  GNOME that is Mutter's XWayland selection bridge. A native\n"
        "  Wayland client may well see nothing; this witness cannot tell."
    )
print(f"  external read after close: {after_close!r}")
print(f"  SelectionTransfer fired {transfers.count} time(s) altogether")

# -------------------------------------------------------------------- verdict

print("\nsummary")
failed = [label for label, passed in results if not passed]
for label, passed in results:
    print(f"  {'ok  ' if passed else 'FAIL'}  {label}")

if decisive and transfers.count == 0:
    print(
        "\nNOTE: the paste matched but SelectionTransfer never fired, so the "
        "value came from somewhere other than this process -- suspect a stale "
        "clipboard rather than a working write path."
    )
if not decisive and not read_path_ok:
    print(
        "\nInconclusive rather than damning: the read path failed too, so "
        f"{tool_name} and the portal may simply not share a selection on this "
        "desktop. Check that XWayland is running before blaming set_clipboard."
    )

print(
    f"\nThe clipboard is left holding {after_close!r}. "
    f"({tool_name} forks a daemon on write, so the phase-1 control may still "
    "be resident; that is the tool's behaviour, not this package's.)"
)
sys.exit(1 if failed else 0)
