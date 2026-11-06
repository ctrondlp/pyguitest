"""Launch a Windows packaged application the one way Windows supports.

The Windows twin of `macapp` — same gap, different costume, measured live
on Windows 10 22H2 (2026-11-05) rather than inferred:

- **`start_app(["calc.exe"])` tracks a stub, not the Calculator.** The
  System32 `calc.exe` is a launcher that hands off to the packaged
  Calculator and exits: measured, `is_running()` answered False with
  `returncode` 0 within three seconds of a live Calculator window
  appearing, the window's pid did not match `app.pid`, and
  `app.terminate()` left the Calculator open. The same shape as macOS's
  `open -a`, and the same consequence — an `Application` whose lifecycle
  calls operate on a process that no longer matters.

A packaged (UWP/Store) application's platform-blessed launch is
`IApplicationActivationManager::ActivateApplication`, which takes the
application's AUMID — its Application User Model ID, the stable
`<PackageFamilyName>!<AppId>` identifier PowerShell's `Get-StartApps`
lists — and **returns the launched application's real process id**. That
pid is wrapped here in :class:`PackagedAppProcess`, the `Popen` surface
over a process handle. Windows is more generous to this adapter than
POSIX was to `macapp.BundleProcess`: a handle to a non-child can be
waited on (`WaitForSingleObject`) and asked for its **real** exit code
(`GetExitCodeProcess`), so there is no observed-gone 0-sentinel here and
`wait()` blocks properly instead of polling.

comtypes is imported lazily, inside the launch, for the reason the uia
backend's `comtypes_client` gives: `import pyguitest` has to work
everywhere, and only the caller who actually activates a packaged app
should pay for (or need) COM.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from typing import Any

from .backends import _winapi

__all__ = ["PackagedAppProcess", "app_id_target", "launch_app_id"]

_WINDOWS = os.name == "nt"
"""Module-level rather than checked inline, so tests off-Windows can
exercise the detection logic by patching one name — `macapp._DARWIN`'s
reason, applied here."""

_SHELL_PREFIX = "shell:appsfolder\\"
"""The documented Explorer spelling for a packaged app, case-insensitive:
`explorer.exe shell:AppsFolder\\<AUMID>` is how Windows itself writes it."""

_AUMID = re.compile(r"[A-Za-z0-9][\w.\-]*_[a-z0-9]{13}![\w.\-]+")
"""The `<PackageFamilyName>!<AppId>` shape. The 13-character tail of a
package family name is a fixed-length base32 hash of the publisher, so the
pattern is distinctive enough that nothing a shell would launch collides
with it — and PATH is still checked first anyway."""

_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_MAX_WAIT_MS = 0xFFFFFFFE
"""The longest *finite* wait `WaitForSingleObject` can be asked for: one
millisecond below `INFINITE`, which shares its encoding with the largest
`DWORD`. `wait()` slices long timeouts against this rather than converting
them in one step, where `timeout=4294967.295` would land exactly on the
sentinel and wait forever."""
_PROCESS_RIGHTS = 0x0001 | 0x1000 | 0x00100000
"""PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE —
the three things the adapter does, and nothing it does not."""


def app_id_target(command: str | Sequence[str]) -> tuple[str, list[str]] | None:
    r"""The (AUMID, arguments) a list-form command names, or None.

    None is the common answer and means "not a packaged-app launch — Popen
    it the ordinary way". A string command is never one (it goes through
    the shell, per `start_app`'s documented contract), and neither is
    anything off Windows. Two spellings resolve:

    - the Explorer spelling, ``start_app(["shell:AppsFolder\\\\<AUMID>"])``
      — what Windows' own documentation writes;
    - a bare AUMID, ``start_app(["Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"])``
      — the identifier `Get-StartApps` lists. PATH is checked first on
      purpose: a real executable that somehow carries this name keeps
      launching exactly as before.

    What deliberately does **not** resolve: `calc.exe` and its stub
    relatives. A stub is indistinguishable from a real program until it
    has already handed off and exited, and rerouting every short-lived
    executable on a guess would be magic. The stub trap is documented on
    `start_app` instead, with this module as the way out.
    """
    if not _WINDOWS or isinstance(command, str) or not command:
        return None
    first, *rest = command
    if first.lower().startswith(_SHELL_PREFIX):
        app_id = first[len(_SHELL_PREFIX) :]
        return (app_id, list(rest)) if app_id else None
    if _AUMID.fullmatch(first) and shutil.which(first) is None:
        return first, list(rest)
    return None


def _last_error() -> int:
    """The last Windows error ctypes saved at the failing call's return.

    `ctypes.get_last_error()`, not a second `GetLastError()` call -- the
    read CPython's docs warn is unreliable, and the reason
    `_winapi._load` opens its DLLs `use_last_error=True`. The twin of
    `session._last_error`; kept here rather than imported to leave this
    module's only intra-package dependency the one DLL accessor.
    """
    import ctypes  # noqa: PLC0415

    getter = getattr(ctypes, "get_last_error", None)
    return 0 if getter is None else int(getter())


def _now_filetime() -> int:
    """The current time as a FILETIME tick count, for `created_at`'s clock.

    `GetSystemTimeAsFileTime` rather than anything in `time`: a process's
    creation time is reported as a FILETIME, and the comparison has to
    happen in that clock's own units to mean anything. Its granularity is
    the system timer tick, and it truncates -- so a timestamp taken here is
    never *later* than the real instant, which is the direction that
    matters: a process created after this call can never read as older
    than it.
    """
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    now = wintypes.FILETIME()
    _winapi.kernel32().GetSystemTimeAsFileTime(ctypes.byref(now))
    return (now.dwHighDateTime << 32) | now.dwLowDateTime


class PackagedAppProcess:
    """A `Popen` look-alike over a packaged application's process handle.

    The same role `macapp.BundleProcess` plays for launchd-owned macOS
    applications, with one honest difference in the caller's favour:
    Windows hands out real process handles for non-children, so
    `returncode` is the application's **actual** exit code read with
    `GetExitCodeProcess`, and `wait()` blocks on the handle rather than
    polling. The streams are None, as on the macOS path: there is no pipe
    to a process DCOM activated.

    `terminate()` and `kill()` are both `TerminateProcess`, which is
    exactly `subprocess.Popen`'s own documented behaviour on Windows — no
    save-your-state courtesy exists to offer.
    """

    stdin = None
    stdout = None
    stderr = None

    def __init__(self, pid: int, args: Sequence[str]) -> None:
        """Open the launched application's process by id."""
        if pid <= 0:
            raise ValueError(
                f"refusing pid {pid}: 0 and negatives name process groups, "
                "and OpenProcess on one is never what a launch meant"
            )
        self.pid = pid
        self.args = list(args)
        self.returncode: int | None = None
        # The prototypes live in _winapi's kernel32 (HANDLE restypes, one
        # place, the module whose job that is) rather than being redeclared
        # here on the process-global windll. Held on the instance so __del__
        # needs no import -- a lazy import at interpreter shutdown dies with
        # "sys.meta_path is None", measured on the Windows 10 box.
        self._k32 = _winapi.kernel32()
        handle = self._k32.OpenProcess(_PROCESS_RIGHTS, False, pid)
        if not handle:
            # _last_error, not a second GetLastError() call: _winapi opens
            # its DLLs use_last_error=True precisely so this reads the error
            # saved at OpenProcess's return rather than one ctypes clobbered.
            raise OSError(
                f"could not open the activated application's process {pid} "
                f"(WinError {_last_error()})"
            )
        self._handle = handle

    def created_at(self) -> int | None:
        """When this process started, as a FILETIME tick count, or None.

        The number is only ever compared against another one from the same
        clock (`_now_filetime`), which is what lets `launch_app_id` tell an
        application it started from one that was already running. None
        where `GetProcessTimes` fails -- a handle whose rights were revoked,
        a process gone between the open and this call -- and the caller
        treats that as "cannot tell" rather than as either answer.
        """
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        created = wintypes.FILETIME()
        unused = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        if not self._k32.GetProcessTimes(
            self._handle,
            ctypes.byref(created),
            *(ctypes.byref(each) for each in unused),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def _exit_code_now(self) -> int:
        """The exit code, read only after the handle is known signalled.

        Callers reach this only once `WaitForSingleObject` has reported the
        process gone, so `GetExitCodeProcess` cannot be answering the
        running-process `STILL_ACTIVE` (259) here -- the signalled state is
        what rules out that classic trap, never the number. The real code
        is the point of this adapter (a Windows handle carries it, where
        macOS's `BundleProcess` can only observe gone-or-not); only if the
        read itself fails against an already-signalled handle -- effectively
        impossible with our rights -- is 0 reported, the same observed-gone
        answer the macOS path gives throughout.
        """
        import ctypes  # noqa: PLC0415

        code = ctypes.c_uint32()
        if not self._k32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            return 0
        return int(code.value)

    def poll(self) -> int | None:
        """The exit code if it has finished, or None. Does not block.

        Only a signalled handle (`WAIT_OBJECT_0`) resolves it; `WAIT_FAILED`
        and anything else leave it None -- poll is the non-blocking,
        best-effort query, so "cannot tell yet" is the honest answer there,
        and `wait()` is where a failed wait is surfaced.
        """
        if self.returncode is None:
            state = self._k32.WaitForSingleObject(self._handle, 0)
            if state == _WAIT_OBJECT_0:
                self.returncode = self._exit_code_now()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block on the handle; `subprocess.TimeoutExpired` past `timeout`.

        A finite `timeout` is waited out in bounded slices rather than in
        one call, because `dwMilliseconds` is a `DWORD` and the sentinel
        for "wait forever" is a value inside the range a caller can ask
        for: `timeout=4294967.295` converts to exactly `INFINITE`, which
        would turn a bounded wait into a permanent one, and anything larger
        wraps to a *short* wait that expires early. Both are silent. The
        deadline is tracked in `time.monotonic()` across the slices, so the
        timeout a caller asked for is the timeout they get.
        """
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            self._wait_slice(_INFINITE, None)
            assert self.returncode is not None
            return self.returncode
        deadline = time.monotonic() + timeout
        while self.returncode is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            # Rounded up, not truncated: `int()` turns the sub-millisecond
            # the deadline arithmetic itself costs into a wait a tick
            # shorter than the caller asked for, and a timeout that expires
            # early is the bug this method is otherwise fixing. The deadline
            # re-check above is what keeps the rounding from overshooting.
            slice_ms = min(math.ceil(remaining * 1000), _MAX_WAIT_MS)
            self._wait_slice(slice_ms, timeout)
        return self.returncode

    def _wait_slice(self, ms: int, timeout: float | None) -> None:
        """One `WaitForSingleObject`, latching `returncode` if it signalled.

        A `WAIT_TIMEOUT` here is not the caller's timeout expiring -- it is
        one slice of a longer wait ending -- so it simply returns and lets
        `wait()` re-check the deadline. `timeout` is carried only to name it
        in the `TimeoutExpired` an `INFINITE` slice can never raise.
        """
        state = self._k32.WaitForSingleObject(self._handle, ms)
        if state == _WAIT_FAILED:
            # Not fed to the exit read: a failed wait says nothing about
            # the exit code, and treating it as "signalled" is how a bogus
            # 259/STILL_ACTIVE would get reported as the real code.
            raise OSError(
                f"waiting on process {self.pid} failed (WinError {_last_error()})"
            )
        if state == _WAIT_OBJECT_0:
            self.returncode = self._exit_code_now()
        elif ms == _INFINITE:
            # An unbounded wait that neither signalled nor failed has no
            # honest retry: report it rather than looping on it.
            raise subprocess.TimeoutExpired(self.args, timeout or 0)

    def terminate(self) -> None:
        """Stop the application outright -- Popen's own meaning here.

        A failed `TerminateProcess` against a still-running process raises
        `OSError`, exactly as `subprocess.Popen.terminate()` does on
        Windows: the alternative measured in review was a silent FALSE
        return letting `Application.stop()` fall through to an unbounded
        `wait()` and hang forever on a process it could not kill (a
        protected process, or revoked handle rights). A visible error beats
        a wedged suite. Already-gone is a no-op, idempotent as before.

        The poll is repeated *after* a failed `TerminateProcess`, because
        the ordinary way for that call to fail is the application having
        exited on its own in the window between the first poll and the
        call -- Windows rejects terminating an already-dead process. Raising
        there would mean `Application.stop()` throwing while unwinding
        someone else's exception, replacing the real failure with a
        complaint about an application that had, in fact, stopped. The error
        is read before the second poll so that a process still running is
        still reported with the error that actually belongs to it.
        """
        if self.poll() is not None:
            return
        if not self._k32.TerminateProcess(self._handle, 1):
            error = _last_error()
            if self.poll() is not None:
                return
            raise OSError(f"could not terminate process {self.pid} (WinError {error})")

    def kill(self) -> None:
        """The same `TerminateProcess` -- Popen's own equivalence here."""
        self.terminate()

    def send_signal(self, sig: int) -> None:
        """SIGTERM only, and it means `TerminateProcess`.

        Matching Popen's Windows contract as far as it can be matched.
        Popen additionally accepts the two console events; an activated
        packaged application shares no console with this process, so there
        is nothing they could reach and they are refused the way Popen
        refuses every other number.
        """
        if sig == signal.SIGTERM:
            self.terminate()
            return
        raise ValueError(f"Unsupported signal: {sig}")

    def __del__(self) -> None:
        """Close the handle; the process itself is not touched.

        Everything is swallowed: this can run at interpreter shutdown,
        where half the world is already gone and a traceback here only
        buries whatever the program was actually saying.
        """
        try:
            handle = getattr(self, "_handle", None)
            if handle:
                self._k32.CloseHandle(handle)
                self._handle = 0
        except Exception:  # noqa: BLE001 -- shutdown-time cleanup
            pass

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        """(None, None) after waiting -- a pipe-less Popen's exact answer.

        `Application` forwards `communicate` by name and documents it as the
        bounded wait; the lookalike answers it so a cross-platform helper
        does not fall over only on the packaged-app path. `input` is refused
        the way Popen refuses it with no stdin pipe.
        """
        if input is not None:
            raise ValueError("no stdin pipe exists to write input to")
        self.wait(timeout=timeout)
        return (None, None)

    def __repr__(self) -> str:
        state = "running" if self.poll() is None else f"exited {self.returncode}"
        return f"<PackagedAppProcess pid={self.pid} {state}>"


_CLSID_ApplicationActivationManager = "{45BA127D-10A8-46EA-8AB7-56EA9078943C}"
_IID_IApplicationActivationManager = "{2e941141-7f97-4756-ba1d-9decde894a3d}"


_activation_manager_cls: Any = None
"""Cache for `_activation_manager_class`; see there for why it is built once."""


def _activation_manager_class() -> Any:
    """The `IApplicationActivationManager` comtypes interface, built once.

    Defined at first call, not per launch: a comtypes interface class
    registers itself in `comtypes.com_interface_registry` under its IID, so
    rebuilding it each `launch_app_id` -- an `app.restart()` loop does
    exactly that -- would replace the class live pointers from earlier
    launches were built against, and leak a type per iteration. Cached in a
    module global so the import and the class body stay lazy (the reason the
    uia backend keeps comtypes out of module scope).
    """
    global _activation_manager_cls
    if _activation_manager_cls is not None:
        return _activation_manager_cls
    import ctypes  # noqa: PLC0415
    from ctypes import POINTER, c_uint32  # noqa: PLC0415
    from ctypes.wintypes import LPCWSTR  # noqa: PLC0415

    import comtypes  # noqa: PLC0415 -- lazy for comtypes_client's reason

    class IApplicationActivationManager(comtypes.IUnknown):
        _iid_ = comtypes.GUID(_IID_IApplicationActivationManager)
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                ctypes.HRESULT,  # type: ignore[attr-defined]
                "ActivateApplication",
                (["in"], LPCWSTR, "appUserModelId"),
                (["in"], LPCWSTR, "arguments"),
                (["in"], c_uint32, "options"),
                (["out"], POINTER(c_uint32), "processId"),
            ),
        ]

    _activation_manager_cls = IApplicationActivationManager
    return IApplicationActivationManager


def launch_app_id(
    app_id: str,
    args: Sequence[str] = (),
    adopt_existing: bool = False,
) -> PackagedAppProcess:
    """Activate `app_id` and wrap the real process it reports.

    `ActivateApplication` returns the activated application's process id
    directly — the half the `calc.exe` stub can never offer.

    **An application that was already running is not adopted.** A
    *single-instance* packaged application is brought to the front and its
    **existing** pid returned, and this API, unlike macOS's, cannot force a
    fresh instance. Handing that back as a launched process would make it
    test-owned: leaving the `with` block, or `restart()`, would terminate a
    window the test never opened and lose whatever was unsaved in it. So the
    process's creation time is compared against the instant before
    activation, and an older one raises OSError rather than being returned.
    The application has been activated by then — that cannot be undone — but
    it is left running. Pass `adopt_existing=True` to take it anyway, for a
    caller who knows the instance is theirs (a reconnect, a fixture that
    deliberately reuses one); where the creation time cannot be read at all,
    the launch proceeds rather than failing on a probe.

    Raises OSError when activation fails (no such AUMID, COM refusal), with
    the HRESULT in the message. If activation succeeds but the reported
    process cannot be opened, the application is already on screen and
    untracked -- the raised OSError is the signal to go look, the same
    leaked-window shape `app.py` warns about, which no return value here
    can prevent once DCOM has launched it.
    """
    import comtypes  # noqa: PLC0415 -- lazy for comtypes_client's reason

    # RPC_E_CHANGED_MODE -- the thread already being in the other apartment
    # model -- is not an error for this use; the uia backend's
    # _initialize_com documents the same fact at length. getattr-guarded
    # the same way, so a comtypes build without CoInitialize is tolerated
    # rather than raising AttributeError out of a launch.
    initialize = getattr(comtypes, "CoInitialize", None)
    initialized = False
    if initialize is not None:
        try:
            initialize()
        except OSError:
            pass  # RPC_E_CHANGED_MODE: this thread is already in an apartment
        else:
            # S_FALSE -- already initialized on this thread -- does not raise
            # and still counts, so every call that returns owes a
            # CoUninitialize. An `app.restart()` loop activates repeatedly on
            # one thread, and leaving the count high there is what keeps the
            # apartment from ever being torn down.
            initialized = True
    try:
        manager = comtypes.CoCreateInstance(
            comtypes.GUID(_CLSID_ApplicationActivationManager),
            interface=_activation_manager_class(),
            clsctx=comtypes.CLSCTX_LOCAL_SERVER,
        )
        arguments = subprocess.list2cmdline([str(a) for a in args]) if args else None
        before = _now_filetime()
        try:
            pid = manager.ActivateApplication(app_id, arguments, 0)
        except OSError as exc:
            raise OSError(f"Windows refused to activate {app_id!r}: {exc}") from exc
        finally:
            # Released inside the try, so the interface pointer is gone
            # before the CoUninitialize in the finally below runs: dropping
            # a COM reference after its apartment has been uninitialized is
            # the ordering that makes a teardown crash.
            del manager
    finally:
        if initialized:
            uninitialize = getattr(comtypes, "CoUninitialize", None)
            if uninitialize is not None:
                with contextlib.suppress(OSError):
                    uninitialize()

    process = PackagedAppProcess(int(pid), [app_id, *args])
    created = process.created_at()
    if not adopt_existing and created is not None and created < before:
        raise OSError(
            f"{app_id!r} was already running as process {process.pid} and has "
            "been brought to the front rather than launched: this packaged "
            "app is single-instance, and Windows offers no way to force a "
            "new one. It is left running, since stopping it would close a "
            "window this session did not open -- stop it yourself first, "
            "drive the existing instance by window title, or pass "
            "adopt_existing=True to pyguitest.winapp.launch_app_id if that "
            "instance is yours to own."
        )
    return process
