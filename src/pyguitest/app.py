"""A launched program, and the guarantee that it stops again.

`start_app()` used to hand back a bare `subprocess.Popen`, which gives a
caller everything needed to *start* a program and nothing to help them stop
one. Stopping one properly is four steps -- terminate, wait with a bound,
kill if that bound expires, wait again -- and every script that wanted its
app gone wrote those four steps out by hand. In this repository alone that
dance was copied into five example scripts, two of which only got as far as
`terminate()` and would hang on the very dialog the fourth step exists for.

That is the boring argument. The sharper one is that the dance is easy to
get *subtly* wrong in ways nobody notices. Writing it by hand here on
2026-09-01 produced two separate leaks in one afternoon: a process whose
handle only existed once the work around it had succeeded, so an interrupt
part-way through left an orphaned window on the user's desktop with nothing
holding a reference to close it; and, after that was fixed with a context
manager, an exception raised inside `__enter__` -- which never reaches
`__exit__`, because the `with` body was never entered -- leaking the window
all over again. Both were found by a developer noticing a window that should
not have been there.

`Application` is that dance, written once, plus the two things the roadmap
asked for alongside it: `is_running()` and `restart()`. It is deliberately a
wrapper over `subprocess` and not a reimplementation of it -- the real
`Popen` stays reachable as `.process`, and the members a caller actually
uses are forwarded, so code written against the old return type keeps
working unchanged.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import IO, Any, Literal

__all__ = ["Application"]


class Application:
    """A program started by `Session.start_app`, and its lifecycle.

    Use it as a context manager when the program should not outlive the
    block -- which is almost always::

        with gui.start_app([EDITOR, path]) as app:
            window = gui.wait_for_window("Editor", timeout=10)
            ...
        # terminated, and killed if it would not terminate

    Unlike a hand-rolled `try`/`finally`, there is no gap between starting
    the program and owning it: the process already exists by the time this
    object does, so nothing can be raised in between and leak it.
    """

    def __init__(
        self,
        process: subprocess.Popen[Any],
        command: str | Sequence[str],
        launch: Callable[[], subprocess.Popen[Any]],
    ) -> None:
        """Wrap an already-started `process`.

        `launch` re-runs the same command with the same options, which is
        what `restart()` calls. It is a callable rather than a reference to
        the Session so this module does not have to import one.
        """
        self.process = process
        """The underlying `subprocess.Popen`, for anything not forwarded."""
        self.command = command
        """What was launched, as passed to `start_app`."""
        self._launch = launch

    # -- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        """Whether the program is still running.

        A `poll()` underneath, so calling this is also what reaps an exited
        process and fills in `returncode`.

        This is the launched process's state, which is not always the
        application's: a launcher that spawns the real program and exits --
        macOS `open -a`, a wrapper script -- reads as *not* running while the
        window it opened is still on screen. Where that matters, wait on the
        window or an element rather than on this. See `pid` for the same
        distinction.
        """
        return self.process.poll() is None

    def stop(self, timeout: float = 5.0) -> int | None:
        """Stop the program, and do not come back until it is stopped.

        SIGTERM first, because a GUI program asked to quit politely gets to
        save its state; `SIGKILL` only if it has not gone within `timeout`.
        That fallback is not theoretical: an editor holding unsaved text
        answers SIGTERM by *opening a "Save changes?" dialog* rather than
        exiting, so a plain `terminate()` plus an unbounded `wait()` hangs
        until someone clicks it (see docs/validation.md, where that cost a
        live run).

        Idempotent, and never raises: this is what runs while an exception
        is already unwinding, and a cleanup path that throws replaces the
        real failure with its own. Returns the exit status.
        """
        process = self.process
        if process.poll() is not None:
            return process.returncode
        process.terminate()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()

    def restart(self, timeout: float = 5.0) -> Application:
        """Stop the program and start it again, same command, same options.

        Returns self, so `app.restart().pid` reads. The new process replaces
        `.process`; the old one is stopped first by the rules above.

        **The old process's captured output is discarded.** Its pipes -- the
        `stdin`/`stdout`/`stderr` a `start_app(..., stdout=PIPE)` gave it -- are
        closed here, because nothing can reach them once `.process` is replaced;
        left open, each was only reclaimed by the garbage collector, with a
        `ResourceWarning` apiece under `-X dev`. Closed rather than drained with
        `communicate()`: a program started through the shell can leave a
        grandchild holding the pipe, and a drain would then block on it. Read
        `app.stdout` before calling this if the old output matters.
        """
        self.stop(timeout)
        old = self.process
        for stream in (old.stdin, old.stdout, old.stderr):
            if stream is not None:
                stream.close()
        self.process = self._launch()
        return self

    def __enter__(self) -> Application:
        """Return self; the process already exists by the time this runs."""
        return self

    def __exit__(self, *_exc: object) -> Literal[False]:
        """Stop the application on the way out, and never swallow the error."""
        # Literal[False], matching Session.__exit__: a plain `bool` lets a
        # type checker read this as "may return True", i.e. a context
        # manager that swallows the exception it was cleaning up after.
        self.stop()
        return False

    # -- the subprocess.Popen surface --------------------------------------
    #
    # Written out rather than left to __getattr__ below, for the reason
    # Session gives about its own backend forwarding: a dynamic forward is
    # invisible to an editor and to a type checker, so `app.wait(timeout=5)`
    # would offer no completion, no signature, and no warning for a typo.
    # These are also exactly the members the scripts that predate this class
    # were calling, which is what keeps them working unchanged.

    @property
    def pid(self) -> int:
        """The process id -- what `wait_for_idle` and friends take.

        The process `start_app` started, which is not always the process that
        draws the window. A launcher that spawns the real application and
        exits -- macOS `open -a`, a wrapper shell script, a `.desktop`
        handler -- leaves this pointing at the launcher, so it will not match
        the `Element.pid` or `Window.pid` of the application on screen. Check
        with `pyguitest inspect` when a pid-scoped element query comes back
        empty for an application that is plainly running.

        The string form of `command` is a common cause, and which way it
        goes depends on what `/bin/sh` is on the machine. Measured three
        ways rather than assumed:

        - `/bin/sh` is **bash** (Fedora): bash `exec`s a simple command, no
          shell is left behind, and `start_app("editor file")` reports the
          editor's own pid.
        - `/bin/sh` is **dash** (Debian 13, dash 0.5.12-12; Pop!_OS, dash
          0.5.12-6ubuntu5): the shell does *not* exec. `sleep 5` ran as a
          child of a surviving `sh`, so the pid here is the **shell's**, and
          a pid-scoped element query finds nothing. Debian and Ubuntu are
          both in this group, so it is the common case, not the exotic one.

        A string the shell cannot finish with an exec at all -- a background
        job, a pipeline, a trap -- leaves the shell here on every platform.

        Pass the list form, `start_app(["editor", "file"])`, whenever the
        pid matters: it runs no shell, so the pid is always the program's.
        """
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """The exit status, or None if nothing has reaped the process yet.

        Read straight off the `Popen`, which means it is None both while the
        program is running *and* after it has exited but before anything
        asked -- `returncode` is filled in by `poll()`, `wait()`, `stop()` or
        `is_running()`, not by the process exiting. So a bare read right
        after the program quits still answers None; call `poll()` first, or
        read what `stop()` returns.
        """
        return self.process.returncode

    @property
    def stdin(self) -> IO[Any] | None:
        """The process's stdin, or None unless `start_app` was given a pipe.

        None is the ordinary answer: `start_app` passes `**kwargs` to
        `subprocess.Popen`, so a stream exists only where the caller asked
        for one with `stdin=subprocess.PIPE`.
        """
        return self.process.stdin

    @property
    def stdout(self) -> IO[Any] | None:
        """The process's stdout, or None unless `start_app` was given a pipe.

        None unless the caller passed `stdout=subprocess.PIPE`, and a GUI
        program often writes nothing useful there anyway.

        `restart()` closes the old process's pipes, so anything still
        unread is gone -- but do not reach for `app.stdout.read()` to get
        in first: `read()` waits for EOF, which a *running* application
        will not give, so the call blocks instead of returning what has
        been written. Draining one pipe while the other fills can deadlock
        for the same reason the stdlib warns about. Use
        `communicate(timeout=...)` on `app.process`, or read incrementally
        on a thread, if the output is wanted while the program is alive.
        """
        return self.process.stdout

    @property
    def stderr(self) -> IO[Any] | None:
        """The process's stderr, or None unless `start_app` was given a pipe.

        None unless the caller passed `stderr=subprocess.PIPE`. Worth piping
        when an application fails to appear at all: the reason is usually on
        this stream, and `restart()` closes it, so read it first.
        """
        return self.process.stderr

    def poll(self) -> int | None:
        """The exit status if it has finished, or None. Does not block.

        Also the call that fills in `returncode`, so this is the one to reach
        for in a loop that is waiting without committing to a deadline.
        """
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        """Block until it exits, or raise `subprocess.TimeoutExpired`.

        Unbounded with no `timeout`, which on a GUI program is a real risk
        rather than a theoretical one: an editor holding unsaved text answers
        a quit request by opening a "Save changes?" dialog and waits for a
        human. `stop()` is the bounded version and the one a test wants.
        """
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        """Ask the program to quit -- SIGTERM on Unix. Returns immediately.

        Does not wait, and does not escalate: a program that answers the
        request with a "Save changes?" dialog simply stays up. `stop()` is
        the bounded four-step version, and the one to use in cleanup.

        On Windows there are no POSIX signals -- this calls
        `TerminateProcess`, which the program cannot decline or handle, so
        there is no save-your-state courtesy there and no difference between
        this and `kill()`. Both also return silently once `returncode` has
        been filled in.
        """
        self.process.terminate()

    def kill(self) -> None:
        """Stop the program outright -- SIGKILL on Unix. Not declinable.

        No chance to save state, and no cleanup. `stop()` already escalates
        to this when a polite request goes unanswered within its timeout,
        which is usually the call to reach for instead.

        On Windows this *is* `terminate()` -- `subprocess` maps both to
        `TerminateProcess` -- so a script that tries one and then the other
        is doing the same thing twice there.
        """
        self.process.kill()

    def send_signal(self, signal: int) -> None:
        """Send one signal to the process, by number.

        Unix only in any real sense. On Windows `subprocess` accepts just
        `SIGTERM` (which it turns into `TerminateProcess`), `CTRL_C_EVENT`
        and `CTRL_BREAK_EVENT`, and raises `ValueError` for anything else --
        so a suite that signals a process needs a platform branch rather
        than a portable call.

        Accepted is not the same as delivered, and the two console events
        carry conditions `start_app` does not set up by default. A console
        event reaches a process only through a shared console, and
        `CTRL_BREAK_EVENT` needs the child in its own process group --
        launch it with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`
        or it will not arrive. `CTRL_C_EVENT` cannot be aimed at one group
        at all on Windows, so it reaches every process sharing the console,
        this one included. Signalling a process already known to have
        exited returns silently on both platforms.
        """
        self.process.send_signal(signal)

    def __getattr__(self, attr: str) -> Any:
        """Delegate anything unnamed to the running process.

        That is how `communicate`, `args`, and whatever a future Python adds
        to Popen stay reachable without mirroring the whole surface here.
        """
        # Whatever the section above does not name -- `communicate`, the
        # `args` attribute, anything a future Python adds. Reads `process`
        # through object.__getattribute__ for the reason Session documents:
        # this hook only runs once normal lookup has failed, so if `process`
        # itself were ever missing (an exception part-way through __init__,
        # unpickling, a subclass skipping super().__init__), `self.process`
        # would re-enter here and recurse until RecursionError, burying the
        # real problem.
        if attr.startswith("_"):
            raise AttributeError(attr)
        process = object.__getattribute__(self, "process")
        return getattr(process, attr)

    def __repr__(self) -> str:
        """The command, its pid, and whether it is still running."""
        state = "running" if self.is_running() else f"exited {self.returncode}"
        return f"<Application {self.command!r} pid={self.pid} {state}>"
