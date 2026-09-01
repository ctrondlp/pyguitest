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
all over again. Both were found by a human noticing a window that should
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
        """Whether the program is still running."""
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
        """
        self.stop(timeout)
        self.process = self._launch()
        return self

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_exc: object) -> Literal[False]:
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
        """The process id -- what `wait_for_idle` and friends take."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """The exit status, or None while it is still running."""
        return self.process.returncode

    @property
    def stdin(self) -> IO[Any] | None:
        """The process's stdin, if it was piped."""
        return self.process.stdin

    @property
    def stdout(self) -> IO[Any] | None:
        """The process's stdout, if it was piped."""
        return self.process.stdout

    @property
    def stderr(self) -> IO[Any] | None:
        """The process's stderr, if it was piped."""
        return self.process.stderr

    def poll(self) -> int | None:
        """The exit status if it has finished, or None. Does not block."""
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        """Block until it exits, or raise `subprocess.TimeoutExpired`."""
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        """Send SIGTERM. See `stop()` for the bounded version."""
        self.process.terminate()

    def kill(self) -> None:
        """Send SIGKILL."""
        self.process.kill()

    def send_signal(self, signal: int) -> None:
        """Send an arbitrary signal."""
        self.process.send_signal(signal)

    def __getattr__(self, attr: str) -> Any:
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
        state = "running" if self.is_running() else f"exited {self.returncode}"
        return f"<Application {self.command!r} pid={self.pid} {state}>"
