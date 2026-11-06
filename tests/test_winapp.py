"""Tests for launching Windows packaged applications by AUMID.

The same split test_macapp.py explains for its half: detection and
`start_app` routing run everywhere (gated on a module flag the tests patch,
driven through fakes), while `PackagedAppProcess` needs a real process
handle and `ctypes.windll`, so its process-backed tests run only on
Windows -- which the deployed suite on the Windows boxes does exercise.
What cannot run off a packaged-app launch at all -- COM activation itself
-- is behind `launch_app_id`, which these tests replace; the live check is
a real Calculator activation on the Windows 10 box, recorded in
docs/validation.md.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import unittest
from unittest import mock

import pyguitest
from pyguitest import winapp
from pyguitest.backends.base import GUIBackend, Window
from pyguitest.capabilities import Capability, CapabilitySet

DETECT = pyguitest.detect()
"""Probed once -- test_macapp.py's reason."""

AUMID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"


class TestAppIdTarget(unittest.TestCase):
    """What counts as a packaged-app launch, and what does not."""

    def setUp(self):
        patcher = mock.patch.object(winapp, "_WINDOWS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_windows_is_never_a_packaged_launch(self):
        with mock.patch.object(winapp, "_WINDOWS", False):
            self.assertIsNone(winapp.app_id_target([AUMID]))

    def test_a_string_command_is_never_a_packaged_launch(self):
        self.assertIsNone(winapp.app_id_target(AUMID))

    def test_an_empty_command_is_not_a_packaged_launch(self):
        self.assertIsNone(winapp.app_id_target([]))

    def test_the_explorer_spelling_resolves(self):
        target = winapp.app_id_target([f"shell:AppsFolder\\{AUMID}", "doc"])
        self.assertEqual(target, (AUMID, ["doc"]))

    def test_the_explorer_spelling_is_case_insensitive(self):
        target = winapp.app_id_target([f"SHELL:appsfolder\\{AUMID}"])
        self.assertEqual(target, (AUMID, []))

    def test_the_explorer_spelling_with_nothing_after_it_does_not(self):
        self.assertIsNone(winapp.app_id_target(["shell:AppsFolder\\"]))

    def test_a_bare_aumid_resolves(self):
        with mock.patch.object(winapp.shutil, "which", return_value=None):
            target = winapp.app_id_target([AUMID, "a", "b"])
        self.assertEqual(target, (AUMID, ["a", "b"]))

    def test_path_wins_over_a_bare_aumid(self):
        """A real executable that somehow carries the name keeps winning."""
        with mock.patch.object(
            winapp.shutil, "which", return_value="C:\\odd\\name.exe"
        ):
            self.assertIsNone(winapp.app_id_target([AUMID]))

    def test_an_ordinary_executable_name_does_not_resolve(self):
        self.assertIsNone(winapp.app_id_target(["calc.exe"]))
        self.assertIsNone(winapp.app_id_target(["notepad.exe", "file.txt"]))

    def test_the_hash_must_be_exactly_thirteen_characters(self):
        short = "Microsoft.WindowsCalculator_8wekyb3d8bbw!App"
        long = "Microsoft.WindowsCalculator_8wekyb3d8bbwez!App"
        with mock.patch.object(winapp.shutil, "which", return_value=None):
            self.assertIsNone(winapp.app_id_target([short]))
            self.assertIsNone(winapp.app_id_target([long]))


@unittest.skipUnless(
    os.name == "nt",
    "PackagedAppProcess opens a real process handle through ctypes.windll; "
    "the deployed suite on the Windows boxes runs these",
)
class TestPackagedAppProcess(unittest.TestCase):
    """The Popen surface, against a real process tracked by handle only."""

    def child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_alive_then_terminated_with_a_real_exit_code(self):
        real = self.child()
        wrapped = winapp.PackagedAppProcess(real.pid, ["probe"])
        self.assertIsNone(wrapped.poll())
        wrapped.terminate()
        # TerminateProcess(handle, 1): the REAL code comes back, not a
        # sentinel -- the honest difference from macapp.BundleProcess.
        self.assertEqual(wrapped.wait(timeout=10), 1)
        self.assertEqual(wrapped.returncode, 1)

    def test_wait_times_out_while_it_runs(self):
        real = self.child()
        wrapped = winapp.PackagedAppProcess(real.pid, ["probe"])
        with self.assertRaises(subprocess.TimeoutExpired):
            wrapped.wait(timeout=0.2)

    def test_signalling_an_exited_process_is_silent(self):
        real = self.child()
        wrapped = winapp.PackagedAppProcess(real.pid, ["probe"])
        real.kill()
        real.wait(timeout=10)
        wrapped.terminate()
        wrapped.kill()
        self.assertIsNotNone(wrapped.poll())

    def test_send_signal_takes_sigterm_and_nothing_else(self):
        real = self.child()
        wrapped = winapp.PackagedAppProcess(real.pid, ["probe"])
        with self.assertRaises(ValueError):
            wrapped.send_signal(signal.SIGINT)
        wrapped.send_signal(signal.SIGTERM)
        self.assertEqual(wrapped.wait(timeout=10), 1)

    def test_communicate_refuses_input_and_otherwise_waits(self):
        real = self.child()
        wrapped = winapp.PackagedAppProcess(real.pid, ["probe"])
        with self.assertRaises(ValueError):
            wrapped.communicate(input=b"x")
        wrapped.terminate()
        self.assertEqual(wrapped.communicate(timeout=10), (None, None))


class TestPackagedAppProcessSurface(unittest.TestCase):
    """The parts that touch no process, on every platform."""

    def test_there_are_no_streams(self):
        self.assertIsNone(winapp.PackagedAppProcess.stdin)
        self.assertIsNone(winapp.PackagedAppProcess.stdout)
        self.assertIsNone(winapp.PackagedAppProcess.stderr)

    def test_a_zero_or_negative_pid_is_refused(self):
        """A zero or negative pid is refused before any OpenProcess.

        Guarded before the ctypes call, so it runs off Windows too: 0 and
        negatives name process groups, never a launched application.
        """
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                winapp.PackagedAppProcess(bad, ["probe"])


class _FakeKernel32:
    """The three kernel32 calls `PackagedAppProcess` makes, scripted.

    `waits` is the sequence `WaitForSingleObject` returns, and every
    millisecond it was asked for is recorded -- which is the point for the
    timeout tests, where the bug was a value *handed to Windows*, not a
    wrong answer coming back.
    """

    def __init__(self, waits=(), terminate_ok=True, exit_code=1):
        self.waits = list(waits)
        self.terminate_ok = terminate_ok
        self.exit_code = exit_code
        self.asked_ms = []
        self.terminations = 0

    def WaitForSingleObject(self, _handle, ms):  # noqa: N802 -- Win32's spelling
        self.asked_ms.append(ms)
        return self.waits.pop(0) if self.waits else winapp._WAIT_TIMEOUT

    def GetExitCodeProcess(self, _handle, code):  # noqa: N802
        code._obj.value = self.exit_code
        return 1

    def TerminateProcess(self, _handle, _code):  # noqa: N802
        self.terminations += 1
        return 1 if self.terminate_ok else 0


def packaged(kernel32):
    """A `PackagedAppProcess` over scripted kernel32, no real handle.

    Built with `object.__new__` rather than through `__init__`, which would
    `OpenProcess` -- so the wait and terminate logic is exercised on every
    platform rather than only where the suite happens to run on Windows.
    """
    process = object.__new__(winapp.PackagedAppProcess)
    process.pid = 4242
    process.args = ["probe"]
    process.returncode = None
    process._k32 = kernel32
    process._handle = 1
    return process


class TestWaitTimeouts(unittest.TestCase):
    """The DWORD conversion, where the sentinel sits inside a caller's range."""

    def test_a_huge_finite_timeout_is_never_sent_as_infinite(self):
        """4294967.295s converted to exactly INFINITE: a wait with no end.

        The deadline is reached by the second call here, so the test also
        shows the slicing terminating rather than looping forever.
        """
        kernel32 = _FakeKernel32(waits=[winapp._WAIT_TIMEOUT, winapp._WAIT_TIMEOUT])
        process = packaged(kernel32)
        with mock.patch.object(
            winapp.time, "monotonic", side_effect=[0.0, 0.0, 1e9, 1e9]
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=4294967.295)
        self.assertNotIn(winapp._INFINITE, kernel32.asked_ms)
        self.assertLessEqual(max(kernel32.asked_ms), winapp._MAX_WAIT_MS)

    def test_a_timeout_past_a_dword_does_not_wrap_to_a_short_wait(self):
        kernel32 = _FakeKernel32(waits=[winapp._WAIT_TIMEOUT])
        process = packaged(kernel32)
        with mock.patch.object(winapp.time, "monotonic", side_effect=[0.0, 0.0, 1e12]):
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=1e9)
        self.assertEqual(kernel32.asked_ms, [winapp._MAX_WAIT_MS])

    def test_an_ordinary_timeout_is_one_wait_of_that_length(self):
        kernel32 = _FakeKernel32(waits=[winapp._WAIT_OBJECT_0])
        process = packaged(kernel32)
        self.assertEqual(process.wait(timeout=5), 1)
        self.assertEqual(kernel32.asked_ms, [5000])

    def test_no_timeout_still_waits_infinitely_in_one_call(self):
        kernel32 = _FakeKernel32(waits=[winapp._WAIT_OBJECT_0])
        process = packaged(kernel32)
        self.assertEqual(process.wait(), 1)
        self.assertEqual(kernel32.asked_ms, [winapp._INFINITE])

    def test_a_signal_across_several_slices_resolves(self):
        kernel32 = _FakeKernel32(
            waits=[winapp._WAIT_TIMEOUT, winapp._WAIT_OBJECT_0], exit_code=3
        )
        process = packaged(kernel32)
        with mock.patch.object(
            winapp.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 2.0]
        ):
            self.assertEqual(process.wait(timeout=4294967.295), 3)

    def test_a_failed_wait_is_raised_not_read_as_an_exit_code(self):
        kernel32 = _FakeKernel32(waits=[winapp._WAIT_FAILED])
        with self.assertRaises(OSError):
            packaged(kernel32).wait(timeout=1)


class TestTerminationRace(unittest.TestCase):
    """An application that exits between the poll and the TerminateProcess."""

    def test_an_exit_in_the_race_window_is_not_an_error(self):
        """stop() must not throw while unwinding someone else's exception."""
        kernel32 = _FakeKernel32(
            waits=[winapp._WAIT_TIMEOUT, winapp._WAIT_OBJECT_0],
            terminate_ok=False,
            exit_code=7,
        )
        process = packaged(kernel32)
        process.terminate()  # must not raise
        self.assertEqual(process.returncode, 7)

    def test_a_process_that_really_is_still_running_still_raises(self):
        kernel32 = _FakeKernel32(
            waits=[winapp._WAIT_TIMEOUT, winapp._WAIT_TIMEOUT], terminate_ok=False
        )
        with self.assertRaises(OSError):
            packaged(kernel32).terminate()


class _FakeProcess:
    """Popen-shaped enough for Application's stop()/pid on any platform."""

    stdin = stdout = stderr = None

    def __init__(self, pid):
        self.pid = pid
        self.returncode = 0  # already gone, so stop() has nothing to do

    def poll(self):
        return self.returncode


class WindowedBackend(GUIBackend):
    name = "fake"

    @property
    def capabilities(self):
        return CapabilitySet({Capability.WINDOW_LIST})

    def windows(self):
        return [Window("a", self, title="Calculator", pid=4242)]


class _FakeComtypes:
    """Just the comtypes surface `launch_app_id` touches.

    A stand-in rather than the real thing so the activation logic -- the
    COM reference count it owes, and the ownership question it has to ask
    about the pid that comes back -- is tested on every platform. The COM
    call itself is what the live Windows run in docs/validation.md covers.
    """

    CLSCTX_LOCAL_SERVER = 4

    def __init__(self, pid=4242, changed_mode=False):
        self.pid = pid
        self.changed_mode = changed_mode
        self.initializes = 0
        self.uninitializes = 0
        self.released = False
        self.activations = []

    def GUID(self, text):  # noqa: N802 -- comtypes' own spelling
        return text

    def CoInitialize(self):  # noqa: N802
        if self.changed_mode:
            # RPC_E_CHANGED_MODE: this thread is already in an apartment, so
            # nothing was initialized and nothing is owed.
            raise OSError("RPC_E_CHANGED_MODE")
        self.initializes += 1

    def CoUninitialize(self):  # noqa: N802
        self.uninitializes += 1

    def CoCreateInstance(self, _clsid, interface=None, clsctx=None):  # noqa: N802
        return _FakeManager(self)


class _FakeManager:
    """The activation manager, which reports a pid and notes its release."""

    def __init__(self, comtypes):
        self._comtypes = comtypes

    def ActivateApplication(self, app_id, arguments, options):  # noqa: N802
        self._comtypes.activations.append((app_id, arguments, options))
        return self._comtypes.pid

    def __del__(self):
        self._comtypes.released = True


class _FakeTracked:
    """Stands in for PackagedAppProcess, with a scripted creation time."""

    def __init__(self, created):
        self._created = created
        self.pid = 4242

    def created_at(self):
        return self._created


class TestActivation(unittest.TestCase):
    """What `launch_app_id` owes COM, and what it refuses to own."""

    def activate(self, comtypes, created, **kwargs):
        """Run launch_app_id against the fakes, with `created` as the pid's."""
        with mock.patch.dict(sys.modules, {"comtypes": comtypes}):
            with mock.patch.object(winapp, "_activation_manager_class"):
                with mock.patch.object(winapp, "_now_filetime", return_value=1000):
                    with mock.patch.object(
                        winapp,
                        "PackagedAppProcess",
                        return_value=_FakeTracked(created),
                    ):
                        return winapp.launch_app_id(AUMID, **kwargs)

    def test_every_initialize_that_returns_is_balanced(self):
        """S_FALSE counts too, which is what an app.restart() loop leaks."""
        comtypes = _FakeComtypes()
        self.activate(comtypes, created=2000)
        self.assertEqual(comtypes.initializes, 1)
        self.assertEqual(comtypes.uninitializes, 1)

    def test_a_refused_initialize_owes_nothing(self):
        """RPC_E_CHANGED_MODE initialized nothing, so it balances nothing."""
        comtypes = _FakeComtypes(changed_mode=True)
        self.activate(comtypes, created=2000)
        self.assertEqual(comtypes.uninitializes, 0)

    def test_the_balance_holds_when_activation_fails(self):
        comtypes = _FakeComtypes()
        with mock.patch.object(
            _FakeManager, "ActivateApplication", side_effect=OSError("no such AUMID")
        ):
            with self.assertRaises(OSError):
                self.activate(comtypes, created=2000)
        self.assertEqual(comtypes.uninitializes, 1)

    def test_the_manager_is_released_before_the_apartment_closes(self):
        comtypes = _FakeComtypes()
        self.activate(comtypes, created=2000)
        self.assertTrue(comtypes.released)

    def test_a_freshly_launched_process_is_owned(self):
        """Created after the activation began: ours to stop."""
        app = self.activate(_FakeComtypes(), created=2000)
        self.assertEqual(app.pid, 4242)

    def test_an_instance_that_was_already_running_is_not_adopted(self):
        """Terminating it would close a window this session never opened."""
        with self.assertRaises(OSError) as caught:
            self.activate(_FakeComtypes(), created=500)
        self.assertIn("already running", str(caught.exception))
        self.assertIn("adopt_existing", str(caught.exception))

    def test_an_existing_instance_can_be_adopted_deliberately(self):
        app = self.activate(_FakeComtypes(), created=500, adopt_existing=True)
        self.assertEqual(app.pid, 4242)

    def test_an_unreadable_creation_time_does_not_block_the_launch(self):
        """A failed probe must not fail a launch that may be perfectly fine."""
        app = self.activate(_FakeComtypes(), created=None)
        self.assertEqual(app.pid, 4242)


class TestStartAppRouting(unittest.TestCase):
    """start_app hands a detected AUMID to launch_app_id, nothing else."""

    def session(self):
        return pyguitest.Session(WindowedBackend(), DETECT)

    def test_an_aumid_command_routes_to_launch_app_id(self):
        with mock.patch.object(winapp, "app_id_target", return_value=(AUMID, ["doc"])):
            with mock.patch.object(
                winapp, "launch_app_id", return_value=_FakeProcess(4242)
            ) as launcher:
                app = self.session().start_app([AUMID, "doc"])
        launcher.assert_called_once_with(AUMID, ["doc"])
        self.assertEqual(app.pid, 4242)

    def test_every_popen_kwarg_is_refused(self):
        """env= included: activation offers no environment control."""
        with mock.patch.object(winapp, "app_id_target", return_value=(AUMID, [])):
            with mock.patch.object(winapp, "launch_app_id") as launcher:
                with self.assertRaises(ValueError):
                    self.session().start_app([AUMID], env={"A": "1"})
        launcher.assert_not_called()

    def test_a_none_valued_kwarg_is_a_no_op_not_a_refusal(self):
        """env=None is a no-op, not a refusal.

        It is what generic forwarding passes; the bundle path already
        tolerated it, so the packaged path must too.
        """
        with mock.patch.object(winapp, "app_id_target", return_value=(AUMID, [])):
            with mock.patch.object(
                winapp, "launch_app_id", return_value=_FakeProcess(4242)
            ) as launcher:
                app = self.session().start_app([AUMID], env=None)
        self.assertEqual(app.pid, 4242)
        launcher.assert_called_once_with(AUMID, [])

    def test_restart_activates_again(self):
        with mock.patch.object(winapp, "app_id_target", return_value=(AUMID, [])):
            with mock.patch.object(
                winapp,
                "launch_app_id",
                side_effect=[_FakeProcess(4242), _FakeProcess(4243)],
            ):
                app = self.session().start_app([AUMID])
                self.assertEqual(app.pid, 4242)
                app.restart()
                self.assertEqual(app.pid, 4243)

    def test_an_ordinary_command_never_consults_the_launcher(self):
        with mock.patch.object(winapp, "app_id_target", return_value=None):
            with mock.patch.object(winapp, "launch_app_id") as launcher:
                with self.session().start_app([sys.executable, "-c", "pass"]) as app:
                    app.wait(timeout=10)
        launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
