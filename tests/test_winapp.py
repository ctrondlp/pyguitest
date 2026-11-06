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
