"""Tests for launching macOS application bundles, and the pid window filter.

Nearly everything here runs on every platform: the detection logic is gated
on a module flag the tests patch rather than on the interpreter, and the
`start_app` routing is driven through fakes. `BundleProcess` is exercised
against a real child process, which is POSIX-only -- not because the class
has platform branches (it has none; it only ever exists for launchd-owned
macOS applications) but because its `os.kill(pid, 0)` liveness probe is
POSIX semantics: on Windows every non-console-event signal value, 0
included, goes through `TerminateProcess`, so the probe would *kill* the
test's child -- measured as exactly 2 failures on the Windows 10 box
(2026-11-05). What cannot run off-mac at all -- NSWorkspace itself -- is
deliberately behind `launch_bundle`, which these tests replace; the live
check for that half is a real TextEdit run on both macOS boxes, recorded in
docs/validation.md rather than assertable here.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyguitest
from pyguitest import WindowNotFound, macapp
from pyguitest.backends.base import GUIBackend, Window
from pyguitest.capabilities import Capability, CapabilitySet

DETECT = pyguitest.detect()
"""Probed once: detect() inspects the live environment, and paying that
per-test turned this file into the slowest in the suite for no coverage."""


def make_bundle(root: Path, name: str = "Probe", executable: str | None = None) -> Path:
    """A minimal .app bundle: Contents/MacOS/<exe> plus an Info.plist."""
    bundle = root / f"{name}.app"
    binary = bundle / "Contents" / "MacOS" / (executable or name)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    with open(bundle / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleExecutable": executable or name}, f)
    return bundle


class TestBundleTarget(unittest.TestCase):
    """What counts as a bundle launch, and -- mostly -- what does not."""

    def setUp(self):
        patcher = mock.patch.object(macapp, "_DARWIN", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = make_bundle(self.root)

    def test_off_darwin_is_never_a_bundle_launch(self):
        with mock.patch.object(macapp, "_DARWIN", False):
            self.assertIsNone(macapp.bundle_target([str(self.bundle)]))

    def test_a_string_command_is_never_a_bundle_launch(self):
        """The string form goes through the shell, per start_app's contract."""
        self.assertIsNone(macapp.bundle_target(str(self.bundle)))

    def test_an_empty_command_is_not_a_bundle_launch(self):
        self.assertIsNone(macapp.bundle_target([]))

    def test_the_bundle_itself_resolves(self):
        target = macapp.bundle_target([str(self.bundle), "--flag"])
        self.assertEqual(target, (str(self.bundle), ["--flag"]))

    def test_a_dot_app_path_that_is_not_a_directory_does_not(self):
        self.assertIsNone(macapp.bundle_target([str(self.root / "Ghost.app")]))

    def test_the_bundles_own_executable_resolves_to_the_bundle(self):
        binary = self.bundle / "Contents" / "MacOS" / "Probe"
        target = macapp.bundle_target([str(binary)])
        self.assertEqual(target, (str(self.bundle), []))

    def test_a_helper_next_to_the_executable_does_not(self):
        """Only CFBundleExecutable counts as the app.

        Launching the whole app in place of a helper would run something
        the caller did not name.
        """
        helper = self.bundle / "Contents" / "MacOS" / "helper"
        helper.write_text("#!/bin/sh\n")
        self.assertIsNone(macapp.bundle_target([str(helper)]))

    def test_a_file_elsewhere_in_the_bundle_does_not(self):
        script = self.bundle / "Contents" / "Resources" / "tool"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        self.assertIsNone(macapp.bundle_target([str(script)]))

    def test_a_bare_name_on_path_is_left_to_popen(self):
        """PATH wins.

        A real executable named like an app keeps launching exactly as
        before this existed.
        """
        with mock.patch.object(macapp.shutil, "which", return_value="/usr/bin/probe"):
            with mock.patch.object(
                macapp, "_application_path", return_value=str(self.bundle)
            ):
                self.assertIsNone(macapp.bundle_target(["probe"]))

    def test_a_bare_installed_app_name_resolves(self):
        with mock.patch.object(macapp.shutil, "which", return_value=None):
            with mock.patch.object(
                macapp, "_application_path", return_value=str(self.bundle)
            ):
                target = macapp.bundle_target(["Probe", "doc.txt"])
        self.assertEqual(target, (str(self.bundle), ["doc.txt"]))

    def test_a_bare_name_that_is_no_app_either_is_left_to_popen(self):
        """So the caller still gets Popen's FileNotFoundError, not ours."""
        with mock.patch.object(macapp.shutil, "which", return_value=None):
            with mock.patch.object(macapp, "_application_path", return_value=None):
                self.assertIsNone(macapp.bundle_target(["no-such-thing"]))

    def test_a_name_with_a_slash_is_not_treated_as_bare(self):
        with mock.patch.object(macapp.shutil, "which", return_value=None):
            with mock.patch.object(
                macapp, "_application_path", return_value=str(self.bundle)
            ) as resolver:
                self.assertIsNone(macapp.bundle_target(["./probe"]))
        resolver.assert_not_called()

    def test_the_executable_resolves_however_the_volume_spells_it(self):
        """A case variant is the same file where the filesystem says it is.

        APFS is case-insensitive by default, so `.../MacOS/probe` names the
        very file the plist calls `Probe` -- and comparing component text
        called those different, dropping the launch onto the Popen path this
        module exists to replace. The assertion forks on what the volume
        running the test actually does, because that is the only honest
        answer: the same path string is one file on APFS and two on a
        case-sensitive volume.
        """
        binary = self.bundle / "Contents" / "MacOS" / "Probe"
        variant = self.bundle / "Contents" / "MacOS" / "probe"
        target = macapp.bundle_target([str(variant)])
        if os.path.exists(variant):  # case-insensitive volume: the same file
            self.assertEqual(target, (str(self.bundle), []))
        else:  # case-sensitive: a different file, and not the app
            self.assertIsNone(target)
        self.assertEqual(macapp.bundle_target([str(binary)]), (str(self.bundle), []))

    def test_a_bare_name_is_resolved_against_the_env_that_will_launch_it(self):
        """PATH wins means the PATH Popen would search, not this process's.

        `Popen(env=...)` searches the PATH inside `env`, so detection has to
        ask the same question the launch will. Asked against os.environ
        instead, a caller's own executable lost to an installed application
        of the same name -- the PATH-wins rule, inverted.

        The search is asserted through `which`'s own `path=` argument rather
        than by planting a real file: what counts as executable is
        platform-specific (an extensionless `Probe` is not on Windows, where
        `PATHEXT` decides, and this file's detection tests otherwise all run
        everywhere), while the path handed to the search is the fix itself.
        """
        tools = self.root / "tools"
        with mock.patch.object(
            macapp, "_application_path", return_value=str(self.bundle)
        ):
            with mock.patch.object(macapp.shutil, "which", return_value=None) as which:
                macapp.bundle_target(["Probe"], env={"PATH": str(tools)})
        self.assertEqual(which.call_args.kwargs["path"], str(tools))

    def test_the_env_search_path_falls_back_the_way_popen_does(self):
        """env=None searches this process's PATH, which is Popen's rule too."""
        with mock.patch.object(
            macapp, "_application_path", return_value=str(self.bundle)
        ):
            with mock.patch.object(macapp.shutil, "which", return_value=None) as which:
                macapp.bundle_target(["Probe"])
        self.assertEqual(
            which.call_args.kwargs["path"], os.pathsep.join(os.get_exec_path(None))
        )

    def test_an_executable_on_the_envs_path_beats_the_installed_app(self):
        """The behaviour the search path exists for, where `which` can see it.

        POSIX-only: it plants a real executable, and an extensionless file
        is not one on Windows. The assertion above covers the plumbing
        everywhere; this one covers the outcome where it can be had.
        """
        if os.name != "posix":
            self.skipTest("an extensionless file is not executable on Windows")
        tools = self.root / "tools"
        tools.mkdir()
        executable = tools / "Probe"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        with mock.patch.object(
            macapp, "_application_path", return_value=str(self.bundle)
        ):
            # On the env's PATH: the caller's own program, left to Popen.
            self.assertIsNone(macapp.bundle_target(["Probe"], env={"PATH": str(tools)}))
            # Not on it: nothing for Popen to find, so the app resolves.
            self.assertEqual(
                macapp.bundle_target(["Probe"], env={"PATH": str(self.root)}),
                (str(self.bundle), []),
            )


@unittest.skipUnless(
    os.name == "posix",
    "BundleProcess only ever exists on macOS; its os.kill(pid, 0) liveness "
    "probe is POSIX semantics, and on Windows that call terminates the "
    "child instead of probing it",
)
class TestBundleProcess(unittest.TestCase):
    """The Popen surface, against a real (non-child-of-launchd) process."""

    def child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        # LIFO: kill runs first, then the wait reaps -- registered the other
        # way around, the wait blocked on the full sleep before the kill ran.
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_alive_then_terminated_then_observed_gone(self):
        real = self.child()
        wrapped = macapp.BundleProcess(real.pid, ["probe"])
        self.assertIsNone(wrapped.poll())
        wrapped.terminate()
        real.wait(timeout=10)  # reap, so the pid stops answering kill(0)
        self.assertEqual(wrapped.wait(timeout=10), 0)
        self.assertEqual(wrapped.returncode, 0)

    def test_wait_times_out_while_it_runs(self):
        real = self.child()
        wrapped = macapp.BundleProcess(real.pid, ["probe"])
        with self.assertRaises(subprocess.TimeoutExpired):
            wrapped.wait(timeout=0.2)

    def test_signalling_an_exited_process_is_silent(self):
        real = self.child()
        wrapped = macapp.BundleProcess(real.pid, ["probe"])
        real.kill()
        real.wait(timeout=10)
        wrapped.terminate()
        wrapped.kill()
        self.assertEqual(wrapped.poll(), 0)

    def test_permissionerror_from_a_signal_does_not_escape(self):
        """A recycled pid this process may not signal answers EPERM.

        stop() runs during cleanup and must not raise; poll() already
        tolerates EPERM and send_signal now does too.
        """
        wrapped = macapp.BundleProcess(999999, ["probe"])
        with mock.patch("os.kill", side_effect=PermissionError):
            wrapped.terminate()  # must not raise
        self.assertIsNone(wrapped.returncode)

    def test_the_exit_watch_binds_to_the_process_not_the_pid(self):
        """The identity that keeps stop() off a recycled pid.

        Where kqueue is available the watch is registered against the
        process at construction, so once that process has gone the object
        reports it gone *and stays that way* -- the pid number answering
        `kill(0)` again later (a recycled pid) cannot make it read as
        alive, which is what would otherwise aim SIGTERM at a stranger.
        """
        real = self.child()
        wrapped = macapp.BundleProcess(real.pid, ["probe"])
        if wrapped._exit_watch is None:
            self.skipTest("no kqueue here; the pid-probe fallback is in force")
        real.kill()
        real.wait(timeout=10)
        self.assertEqual(wrapped.poll(), 0)
        # A pid that answers kill(0) must not revive it: the watch already
        # reported this process's exit, and poll() has latched it.
        with mock.patch("os.kill", return_value=None):
            self.assertEqual(wrapped.poll(), 0)

    def test_a_pid_that_cannot_be_watched_falls_back_to_the_probe(self):
        wrapped = macapp.BundleProcess(999999, ["probe"])
        self.assertIsNone(wrapped._exit_watch)
        self.assertEqual(wrapped.poll(), 0)  # ProcessLookupError: gone


class TestBundleProcessSurface(unittest.TestCase):
    """The parts of the surface that touch no process, on every platform."""

    def test_there_are_no_streams(self):
        wrapped = macapp.BundleProcess(1, ["probe"])
        self.assertIsNone(wrapped.stdin)
        self.assertIsNone(wrapped.stdout)
        self.assertIsNone(wrapped.stderr)

    def test_a_zero_or_negative_pid_is_refused(self):
        """A gone/invalid pid must be refused.

        -1 is what NSRunningApplication answers for a gone process, and
        os.kill(-1, SIGTERM) would signal the whole session.
        """
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                macapp.BundleProcess(bad, ["probe"])

    def test_communicate_refuses_input_and_otherwise_waits(self):
        wrapped = macapp.BundleProcess(1, ["probe"])
        with self.assertRaises(ValueError):
            wrapped.communicate(input=b"x")
        wrapped.returncode = 0  # already gone, so the wait is immediate
        self.assertEqual(wrapped.communicate(), (None, None))


class _Launched:
    """An NSRunningApplication-shaped object for the disown path."""

    def __init__(self, declines: bool = False) -> None:
        self.declines = declines
        self.calls: list[str] = []

    def terminate(self) -> bool:
        self.calls.append("terminate")
        return not self.declines

    def forceTerminate(self) -> bool:  # noqa: N802 -- Cocoa's own spelling
        self.calls.append("forceTerminate")
        return True


class TestAbandonedLaunchCleanup(unittest.TestCase):
    """What happens to an application that arrives after the wait gave up.

    LaunchServices cannot be told to cancel a launch, so the only place the
    late application can still be reached is the completion handler --
    which is why it, and not the caller, does the cleaning up. NSWorkspace
    itself is off-mac unreachable; what is asserted here is the disown
    decision and the termination it leads to.
    """

    def test_an_application_that_declines_to_quit_is_forced(self):
        application = _Launched(declines=True)
        macapp._terminate_abandoned(application)
        self.assertEqual(application.calls, ["terminate", "forceTerminate"])

    def test_one_that_quits_politely_is_not_forced(self):
        application = _Launched()
        macapp._terminate_abandoned(application)
        self.assertEqual(application.calls, ["terminate"])

    def test_nothing_to_clean_up_is_not_an_error(self):
        macapp._terminate_abandoned(None)  # must not raise

    def test_a_termination_that_itself_fails_is_swallowed(self):
        """There is no caller left to raise to on an AppKit queue."""
        broken = mock.Mock()
        broken.terminate.side_effect = OSError("gone")
        macapp._terminate_abandoned(broken)  # must not raise


class WindowedBackend(GUIBackend):
    """Just enough backend for the pid-filtered window lookups."""

    name = "fake"

    def __init__(self):
        self._windows = [
            Window("a", self, title="Untitled", pid=4242),
            Window("b", self, title="Untitled", pid=7),
            Window("c", self, title="No pid here", pid=None),
        ]

    @property
    def capabilities(self):
        return CapabilitySet({Capability.WINDOW_LIST})

    def windows(self):
        return self._windows


class TestPidWindowFilter(unittest.TestCase):
    def session(self):
        return pyguitest.Session(WindowedBackend(), DETECT)

    def test_pid_alone_is_a_valid_filter(self):
        found = self.session().find_windows(pid=4242)
        self.assertEqual([w.pid for w in found], [4242])

    def test_pid_combines_with_title(self):
        gui = self.session()
        self.assertEqual(len(gui.find_windows("Untitled")), 2)
        self.assertEqual(len(gui.find_windows("Untitled", pid=7)), 1)

    def test_a_window_without_a_pid_never_matches_one(self):
        self.assertEqual(self.session().find_windows(pid=0), [])

    def test_no_filter_at_all_still_raises(self):
        with self.assertRaises(ValueError):
            self.session().find_windows()
        with self.assertRaises(ValueError):
            self.session().wait_for_window()

    def test_wait_for_window_polls_by_pid(self):
        window = self.session().wait_for_window(pid=4242, timeout=1)
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.pid, 4242)

    def test_expect_window_names_the_pid_in_its_refusal(self):
        with self.assertRaises(WindowNotFound) as caught:
            self.session().expect_window(pid=9999, timeout=0.1)
        self.assertIn("pid=9999", str(caught.exception))


class TestStartAppRouting(unittest.TestCase):
    """start_app hands a detected bundle to launch_bundle, nothing else."""

    def session(self):
        return pyguitest.Session(WindowedBackend(), DETECT)

    def test_a_bundle_command_routes_to_launch_bundle(self):
        fake = macapp.BundleProcess(4242, ["/Apps/Probe.app"])
        with mock.patch.object(
            macapp, "bundle_target", return_value=("/Apps/Probe.app", ["doc"])
        ):
            with mock.patch.object(
                macapp, "launch_bundle", return_value=fake
            ) as launcher:
                app = self.session().start_app(["/Apps/Probe.app", "doc"])
        launcher.assert_called_once_with("/Apps/Probe.app", ["doc"], env=None)
        self.assertEqual(app.pid, 4242)
        self.assertEqual(app.command, ["/Apps/Probe.app", "doc"])

    def test_env_is_forwarded_and_other_kwargs_are_refused(self):
        fake = macapp.BundleProcess(4242, ["/Apps/Probe.app"])
        with mock.patch.object(
            macapp, "bundle_target", return_value=("/Apps/Probe.app", [])
        ):
            with mock.patch.object(
                macapp, "launch_bundle", return_value=fake
            ) as launcher:
                self.session().start_app(["/Apps/Probe.app"], env={"A": "1"})
                with self.assertRaises(ValueError):
                    self.session().start_app(
                        ["/Apps/Probe.app"], stdout=subprocess.PIPE
                    )
        launcher.assert_called_once_with("/Apps/Probe.app", [], env={"A": "1"})

    def test_the_env_that_will_launch_it_reaches_the_detection(self):
        """Detection has to search the PATH the launch would search."""
        with mock.patch.object(macapp, "bundle_target", return_value=None) as detector:
            with self.session().start_app(
                [sys.executable, "-c", "pass"], env={**os.environ, "A": "1"}
            ) as app:
                app.wait(timeout=10)
        self.assertEqual(detector.call_args.kwargs["env"]["A"], "1")

    def test_restart_launches_a_fresh_instance(self):
        processes = [
            macapp.BundleProcess(4242, ["/Apps/Probe.app"]),
            macapp.BundleProcess(4243, ["/Apps/Probe.app"]),
        ]
        for fake in processes:
            fake.returncode = 0  # already gone, so stop() has nothing to do
        with mock.patch.object(
            macapp, "bundle_target", return_value=("/Apps/Probe.app", [])
        ):
            with mock.patch.object(macapp, "launch_bundle", side_effect=processes):
                app = self.session().start_app(["/Apps/Probe.app"])
                self.assertEqual(app.pid, 4242)
                app.restart()
                self.assertEqual(app.pid, 4243)

    def test_an_ordinary_command_never_consults_the_launcher(self):
        with mock.patch.object(macapp, "bundle_target", return_value=None):
            with mock.patch.object(macapp, "launch_bundle") as launcher:
                with self.session().start_app([sys.executable, "-c", "pass"]) as app:
                    app.wait(timeout=10)
        launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
