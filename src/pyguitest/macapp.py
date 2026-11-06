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
`Application` already speaks — so `start_app(["/path/To.app"])` hands back
an `Application` whose `pid`/`is_running()`/`stop()` mean what they say.
(The list form, since a string command goes through the shell by
`start_app`'s contract and is never a bundle launch.)

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


def _is_bundle_executable(bundle: Path, path: Path) -> bool:
    """Whether `path` *is* `bundle`'s own `Contents/MacOS/<CFBundleExecutable>`.

    Asked with `os.path.samefile` rather than by comparing path components,
    because the comparison has to answer to the filesystem and not to the
    spelling. APFS volumes are case-insensitive by default, so
    `.../MacOS/textedit` and `.../Contents/macos/TextEdit` are the very same
    file as the plist's `TextEdit` -- and a name comparison called those
    different, dropping them onto the exact Popen path this module exists to
    replace (measured as SIGKILL at launch for a system app). `samefile`
    compares device and inode, so every spelling the filesystem accepts
    resolves, while a *different* file -- a helper beside the executable, a
    distinct case variant on a case-sensitive volume -- stays on the Popen
    path, where launching the whole application in its place would run
    something the caller did not name.
    """
    executable = _bundle_executable(bundle)
    if executable is None:
        return False
    try:
        return os.path.samefile(path, bundle / "Contents" / "MacOS" / executable)
    except OSError:
        return False  # one of them is not there; not a bundle launch


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


def bundle_target(
    command: str | Sequence[str], env: dict[str, str] | None = None
) -> tuple[str, list[str]] | None:
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

    `env` is the environment the same command would have been Popen'd with,
    and it is consulted for one thing only: the PATH that decides the third
    spelling above. It has to be the *effective* search path rather than
    this process's own, because `Popen(env=...)` searches the PATH in `env`
    -- so a caller who put a real `TextEdit` executable on their own PATH
    would otherwise get the installed application launched instead of their
    program, the precise inversion of the PATH-wins rule.
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
        if _is_bundle_executable(parent, path):
            return str(parent), list(rest)
        return None  # inside a bundle, but not its main executable
    # os.get_exec_path(env) is exactly what Popen's own POSIX exec path
    # search uses, env=None included (it falls back to os.environ, then to
    # os.defpath) -- so "is it on PATH?" is asked here the same way the
    # launch that would otherwise happen asks it.
    search_path = os.pathsep.join(os.get_exec_path(env))
    if "/" not in first and shutil.which(first, path=search_path) is None:
        found = _application_path(first)
        if found is not None:
            return found, list(rest)
    return None


def _exit_watch(pid: int) -> Any | None:
    """A kqueue watching `pid`'s process for exit, or None if unavailable.

    `EVFILT_PROC` with `NOTE_EXIT` is the BSD/macOS way to be told that a
    process one did not fork has exited. Registration resolves the pid to a
    process *once*, here, and the kernel holds that reference -- which is the
    whole point: it makes the watch an identity for the process rather than
    for the number, so `BundleProcess` cannot signal a stranger that later
    inherits the pid.

    None where there is no `kqueue` (every non-BSD platform, which is where
    the off-mac tests run) or where the registration is refused -- a process
    this user may not watch, or one already gone. The caller falls back to
    the pid probe, so a None here costs the identity guarantee and nothing
    else.

    The `select` members are reached through `getattr` rather than named
    directly, for the reason `winapp._last_error` reads `get_last_error`
    that way: they exist only on the BSDs, and a direct reference is an
    attribute error waiting on every other platform -- and an unresolvable
    name to a type checker running on one.
    """
    try:
        import select  # noqa: PLC0415 -- lazy: only a bundle launch needs it
    except ImportError:  # pragma: no cover -- select is always built in
        return None
    kqueue = getattr(select, "kqueue", None)
    kevent = getattr(select, "kevent", None)
    filter_proc = getattr(select, "KQ_FILTER_PROC", None)
    note_exit = getattr(select, "KQ_NOTE_EXIT", None)
    add = getattr(select, "KQ_EV_ADD", 0) | getattr(select, "KQ_EV_ENABLE", 0)
    if kqueue is None or kevent is None or filter_proc is None or note_exit is None:
        return None
    try:
        queue = kqueue()
    except OSError:
        return None
    try:
        queue.control(
            [kevent(pid, filter=filter_proc, flags=add, fflags=note_exit)], 0, 0
        )
    except OSError:
        queue.close()
        return None
    return queue


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
    - **Identity is the process, not the pid — where the kernel will say
      so.** A real Popen's identity survives pid reuse through `waitpid`,
      which is not available for someone else's child. The stand-in is a
      `kqueue` `EVFILT_PROC`/`NOTE_EXIT` registration taken at
      construction: the kernel binds that watch to *this* process, so the
      exit it reports is this application's exit and a later process that
      inherits the recycled pid number can never be mistaken for it --
      which matters because `stop()` signals through this object, and a
      pid-number check would aim SIGTERM at whatever now holds it. Where
      the registration cannot be taken (no `kqueue`, or a process this
      user may not watch) the probe falls back to `os.kill(pid, 0)`, which
      *is* only the number: hold such an `Application` inside its `with`
      block and stop it promptly rather than across a long run.
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
        self._exit_watch = _exit_watch(pid)

    def poll(self) -> int | None:
        """0 once the process is observed gone, None while it runs."""
        if self.returncode is None and self._observed_gone():
            self.returncode = 0
        return self.returncode

    def _observed_gone(self) -> bool:
        """Whether *this* process -- not merely its pid number -- has exited.

        The kqueue watch answers first where there is one: its NOTE_EXIT is
        the kernel reporting the exit of the process registered at
        construction, so a recycled pid cannot read as alive and cannot be
        signalled. `os.kill(pid, 0)` is the fallback, and the honest
        difference is documented on the class.
        """
        watch = self._exit_watch
        if watch is not None:
            # A one-shot read: NOTE_EXIT is delivered once, and poll()
            # latches `returncode` from it, so the event is never needed
            # twice. Nothing here blocks -- timeout 0.
            try:
                return bool(watch.control(None, 1, 0))
            except OSError:
                self._exit_watch = None  # fall through to the pid probe
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # alive, just not ours to signal
        return False

    def __del__(self) -> None:
        """Close the kqueue descriptor; the process itself is not touched.

        Everything is swallowed for `winapp.PackagedAppProcess.__del__`'s
        reason: this can run at interpreter shutdown, where a traceback
        only buries whatever the program was actually saying.
        """
        try:
            watch = getattr(self, "_exit_watch", None)
            if watch is not None:
                watch.close()
                self._exit_watch = None
        except Exception:  # noqa: BLE001 -- shutdown-time cleanup
            pass

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
        (alive, not ours to signal -- an app that re-execed under another
        identity, a sandbox refusal): this call runs inside
        `Application.stop()`, whose contract is to never raise from
        cleanup, and a signal that cannot be delivered is a fact to
        tolerate there, not an error to raise over the failure actually
        being cleaned up after. A refusal does leave the process running,
        including after `kill()` -- which is why `Application.stop()`
        bounds the wait that follows its own `kill()` and reports the
        process as still running rather than blocking on an exit that a
        refused SIGKILL guarantees will never come.
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


def _terminate_abandoned(application: Any) -> None:
    """Stop an application nobody will ever hold a handle to.

    Reached only when a launch completed after its waiter gave up, where the
    alternative is a window on the user's desktop that no `Application` can
    close. `terminate()` first -- the application may have state to save --
    and `forceTerminate()` only if it declines, since there is nobody left
    to answer a "Save changes?" dialog. Failures are swallowed: this runs on
    an AppKit queue with no caller to raise to, and the launch it is
    cleaning up after has already been reported as a timeout.
    """
    if application is None:
        return
    try:
        if not application.terminate():
            application.forceTerminate()
    except Exception:  # noqa: BLE001 -- no caller left to raise to
        pass


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
    or the launch never completes within `timeout`.

    A timeout cannot *cancel* the launch — LaunchServices offers no such
    call — so what it does instead is disown it: the completion handler
    stays registered, and if the application does arrive after the wait
    gave up, the handler terminates it rather than leaving a window on
    screen that no `Application` will ever stop. That is the
    leaked-window shape `app.py`'s docstring warns about, closed on the
    one path that can still see the application. The handoff is taken
    under a lock, so a completion that lands in the same instant as the
    timeout is either owned by the caller (and returned normally) or
    cleaned up — never both and never neither.

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
    handoff = threading.Lock()
    abandoned = False

    def _finished(application: Any, error: Any) -> None:
        """Take the launch result, or clean it up if nobody is waiting.

        Runs on a queue of AppKit's choosing, after
        `openApplicationAtURL_...` has returned -- so by the time it fires
        the waiter below may already have timed out and raised. `handoff`
        is what makes the two exclusive.
        """
        with handoff:
            if not abandoned:
                outcome["application"] = application
                outcome["error"] = error
                done.set()
                return
        _terminate_abandoned(application)

    NSWorkspace.sharedWorkspace().openApplicationAtURL_configuration_completionHandler_(
        NSURL.fileURLWithPath_(str(bundle)), configuration, _finished
    )
    if not done.wait(timeout):
        with handoff:
            # Not simply `abandoned = True`: the handler may have taken the
            # lock first and already published a successful launch, in which
            # case that application is the caller's and disowning it would
            # terminate the very app they asked for.
            abandoned = not outcome
        if abandoned:
            raise OSError(
                f"launching {bundle!r} did not complete within {timeout}s; "
                "the launch is disowned, and an application that arrives "
                "after this point is terminated rather than left untracked"
            )
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
