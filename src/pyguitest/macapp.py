"""Launch a macOS application bundle the one way macOS supports.

`start_app()`'s plain `subprocess.Popen` works for ordinary executables on
every platform, but a macOS *application* is a `.app` bundle, and both ways
a caller might hand one to Popen fail — measured live on macOS 26.7
(2026-11-05), not inferred:

- **The bundle's own binary, exec'd directly**
  (`.../TextEdit.app/Contents/MacOS/TextEdit`) is killed with SIGKILL
  almost immediately — exit status 137, deterministic across runs, no
  crash report, no window. The OS reserves the right to kill a system
  app's binary that was not launched through LaunchServices (launch
  constraints); whatever the enforcement path, the measured result is
  that the Popen route cannot work.
- **`open -a Name`** launches the app, but `open` itself exits in under a
  second after handing the request to launchd. An `Application` built on
  that Popen tracks `open`, not the app: `pid` names a dead process,
  `is_running()` answers False while the window is on screen, and
  `terminate()`/`stop()` signal nothing that matters — measured: the app
  outlived `app.terminate()` untouched.

This module is the supported path: `NSWorkspace` launches the bundle (the
same machinery Finder uses), reports the real process, and
:class:`BundleProcess` wraps that process in the `Popen` surface
`Application` already speaks — so `start_app("/path/To.app")` hands back
an `Application` whose `pid`/`is_running()`/`stop()` mean what they say.

AppKit is imported lazily, inside the launch, for the reason `_macapi`
gives for avoiding framework loads on every `Environment`: only the caller
who actually launches a bundle pays for it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["BundleProcess", "bundle_target", "launch_bundle"]

_DARWIN = sys.platform == "darwin"
"""Module-level rather than checked inline, so tests off-mac can exercise
the detection logic by patching one name instead of the interpreter."""

_LAUNCH_TIMEOUT = 30.0
"""Seconds to wait for launchd to report the new process. A cold app launch
on spinning rust is slow, but half a minute of nothing is an answer."""


def _bundle_executable(bundle: Path) -> str | None:
    """The bundle's CFBundleExecutable, or None where it cannot be read."""
    import plistlib  # noqa: PLC0415 -- only a bundle launch pays for pyexpat

    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            value = plistlib.load(f).get("CFBundleExecutable")
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _application_path(name: str) -> str | None:
    """The .app bundle LaunchServices resolves `name` to, or None.

    Lazy AppKit import; answers None off-mac or where AppKit is absent, so
    the caller's fallback (let Popen raise its usual FileNotFoundError)
    stays reachable.
    """
    try:
        from AppKit import NSWorkspace  # noqa: PLC0415 -- lazy by design
    except Exception:
        return None
    found = NSWorkspace.sharedWorkspace().fullPathForApplication_(name)
    return str(found) if found else None


def bundle_target(command: str | Sequence[str]) -> tuple[str, list[str]] | None:
    """The (.app bundle, arguments) a list-form command names, or None.

    None is the common answer and means "not a bundle launch — Popen it the
    ordinary way". A string command is never a bundle launch (it goes
    through the shell, by `start_app`'s documented contract), and neither
    is anything off macOS. Three spellings resolve, each measured to fail
    under plain Popen (see the module docstring):

    - the bundle itself: ``start_app(["/System/Applications/TextEdit.app"])``
    - the bundle's own main executable:
      ``start_app([".../TextEdit.app/Contents/MacOS/TextEdit"])`` — only
      when the named file *is* the bundle's CFBundleExecutable; any other
      file inside a bundle (a helper in the same directory, a Resources
      script) is left to Popen, since launching the whole app in its place
      would run something the caller did not name.
    - a bare application name that is not on PATH but is an installed app:
      ``start_app(["TextEdit"])``. PATH wins on purpose — a real executable
      named like an app keeps launching exactly as before — so this only
      engages where Popen was guaranteed to raise FileNotFoundError.
    """
    if not _DARWIN or isinstance(command, str) or not command:
        return None
    first, *rest = command
    # Path() first, string tests second: a trailing slash from tab
    # completion ("TextEdit.app/") and a case variant (".APP", real on
    # APFS's default-insensitive volumes) both name the same bundle, and
    # testing the raw string missed them -- falling through to the exact
    # Popen paths this module exists to replace.
    path = Path(first)
    if path.suffix.lower() == ".app" and path.is_dir():
        return str(path), list(rest)
    for parent in path.parents:
        if parent.suffix.lower() != ".app":
            continue
        relative = path.relative_to(parent)
        if (
            relative.parts[:2] == ("Contents", "MacOS")
            and len(relative.parts) == 3
            and relative.parts[2] == _bundle_executable(parent)
        ):
            return str(parent), list(rest)
        return None  # inside a bundle, but not its main executable
    if "/" not in first and shutil.which(first) is None:
        found = _application_path(first)
        if found is not None:
            return found, list(rest)
    return None


class BundleProcess:
    """A `Popen` look-alike over a process launchd started for us.

    `Application` only ever touches its process through the Popen surface
    — `pid`, `poll`, `wait`, `terminate`, `kill`, `send_signal`,
    `returncode`, and the three streams — so implementing exactly that
    surface is what lets a launchd-owned application ride in the same
    `Application` without that class growing a platform branch.

    Two honest differences from a real Popen, both consequences of the
    process not being our child:

    - **`returncode` is 0 for any observed exit.** A real exit status
      belongs to the parent, and launchd is the parent; all this object
      can see is gone-or-not (`os.kill(pid, 0)` — chosen over
      `NSRunningApplication.isTerminated`, which is updated by key-value
      observation on a runloop this process is not running). So 0 here
      means "observed gone", never "exited successfully".
    - **There are no streams.** `stdin`/`stdout`/`stderr` are always None,
      the same answer a pipe-less Popen gives; there is no pipe to a
      process something else spawned. `start_app` refuses stream kwargs
      for a bundle launch rather than dropping them silently.
    - **The pid is only a pid.** A real Popen's identity survives pid
      reuse through `waitpid`; this object has nothing but the number, and
      launchd reaps immediately, so a long-lived handle to an app that
      exited can watch a recycled pid belonging to someone else. Keep the
      `Application` inside its `with` block and stop it promptly rather
      than holding one across a long run.
    """

    stdin = None
    stdout = None
    stderr = None

    def __init__(self, pid: int, args: Sequence[str]) -> None:
        """Wrap the launched application's real process id."""
        if pid <= 0:
            raise ValueError(
                f"refusing pid {pid}: 0 and negatives address process "
                "groups, and signalling those is never what a caller meant"
            )
        self.pid = pid
        self.args = list(args)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """0 once the process is observed gone, None while it runs."""
        if self.returncode is None:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                self.returncode = 0
            except PermissionError:
                pass  # alive, just not ours to signal
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until it exits; `subprocess.TimeoutExpired` past `timeout`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            time.sleep(0.05)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        """Send `sig`, silently doing nothing once the process is gone.

        `PermissionError` is swallowed for the reason `poll()` swallows it
        (alive, not ours to signal -- a recycled pid, an app that
        re-execed): this call runs inside `Application.stop()`, whose
        contract is to never raise from cleanup, and a signal that cannot
        be delivered is a fact to tolerate there, not an error to raise
        over the failure actually being cleaned up after.
        """
        if self.poll() is not None:
            return
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            self.returncode = 0
        except PermissionError:
            pass

    def terminate(self) -> None:
        """SIGTERM — a Cocoa app treats it as a quit request."""
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """SIGKILL. Not declinable, same as Popen's."""
        self.send_signal(signal.SIGKILL)

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        """(None, None) after waiting -- a pipe-less Popen's exact answer.

        `Application.__getattr__` forwards `communicate` by name and its
        `stdout` docstring recommends `communicate(timeout=...)` as the
        bounded wait, so the lookalike has to answer it; a cross-platform
        helper must not fall over only on the bundle path. `input` is
        refused the way Popen refuses it with no stdin pipe.
        """
        if input is not None:
            raise ValueError("no stdin pipe exists to write input to")
        self.wait(timeout=timeout)
        return (None, None)

    def __repr__(self) -> str:
        state = "running" if self.poll() is None else "gone"
        return f"<BundleProcess pid={self.pid} {state}>"


def launch_bundle(
    bundle: str,
    args: Sequence[str] = (),
    env: dict[str, str] | None = None,
    timeout: float = _LAUNCH_TIMEOUT,
) -> BundleProcess:
    """Launch `bundle` through NSWorkspace and wrap the real process.

    Always a *new* application instance (`createsNewApplicationInstance`),
    because `start_app`'s contract is a process the caller owns: the
    default LaunchServices behaviour reuses a running instance, which
    reactivates someone else's window and hands back a process this
    caller must not stop. (The command-line spelling of the same trap is
    `open -a` versus `open -n -a`.)

    Raises FileNotFoundError for a path that is not there — the error
    Popen raises for a missing executable, kept so a caller's handling
    does not fork by platform — and OSError when LaunchServices refuses
    or the launch never completes within `timeout`. A timeout does not
    *cancel* the launch: the application can still appear afterwards,
    untracked, which is exactly the leaked-window shape `app.py`'s
    docstring warns about — treat a timeout here as "go look at the
    machine", not "nothing happened".

    `env` is handed to `NSWorkspaceOpenConfiguration.environment`; whether
    the platform merges it over launchd's own inherited environment or
    replaces it wholesale is Apple's choice and is not documented, so do
    not lean on the *replacement* semantics Popen's `env=` has -- set the
    variables you need and assume nothing about the rest.
    """
    if not Path(bundle).exists():
        raise FileNotFoundError(f"no such application bundle: {bundle!r}")
    # Lazy for _macapi's reason: only a bundle launch pays for AppKit.
    from AppKit import (  # noqa: PLC0415
        NSWorkspace,
        NSWorkspaceOpenConfiguration,
    )
    from Foundation import NSURL  # noqa: PLC0415

    configuration = NSWorkspaceOpenConfiguration.configuration()
    configuration.setCreatesNewApplicationInstance_(True)
    if args:
        configuration.setArguments_([str(a) for a in args])
    if env is not None:
        configuration.setEnvironment_(dict(env))

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _finished(application: Any, error: Any) -> None:
        outcome["application"] = application
        outcome["error"] = error
        done.set()

    NSWorkspace.sharedWorkspace().openApplicationAtURL_configuration_completionHandler_(
        NSURL.fileURLWithPath_(str(bundle)), configuration, _finished
    )
    if not done.wait(timeout):
        raise OSError(f"launching {bundle!r} did not complete within {timeout}s")
    application = outcome.get("application")
    if application is None:
        error = outcome.get("error")
        detail = error.localizedDescription() if error is not None else "unknown error"
        raise OSError(f"macOS refused to launch {bundle!r}: {detail}")
    pid = int(application.processIdentifier())
    if pid <= 0:
        # NSRunningApplication answers -1 once the process is gone -- an
        # application that crashed at launch, exactly this module's
        # subject. Returned unguarded, that -1 would make poll() probe the
        # broadcast group (os.kill(-1, 0) succeeds) and stop() SIGTERM
        # *every process this user can signal*. Refuse instead.
        raise OSError(
            f"{bundle!r} launched but its process was already gone before "
            "it could be tracked (NSRunningApplication reports no pid)"
        )
    return BundleProcess(pid, [str(bundle), *args])
