"""Application lifecycle, against real processes.

No display server and no GUI: these drive `sleep` and `sh`, the same way
tests/test_api.py::TestWaitForProcess already drives a real process to check
wait_for_process. Real ones matter here rather than a mock Popen -- the
behaviour under test is what happens to an actual process that declines to
die politely, and a mock would only ever confirm which methods were called.
"""

from __future__ import annotations

import os
import subprocess
import time
import unittest

import pyguitest
from pyguitest import Application

# A process that ignores SIGTERM, which is what makes the kill fallback
# necessary rather than decorative. The real-world shape of this is an
# editor answering SIGTERM with a "Save changes?" dialog instead of exiting
# -- see docs/validation.md, where exactly that hung a live run.
STUBBORN = ["sh", "-c", 'trap "" TERM; sleep 30']


def session():
    """A session over NullBackend -- deliberately not `connect()`.

    `start_app` is tier-1: it shells out to subprocess and never touches a
    backend, so nothing here needs a real one. Calling `connect()` instead
    composed live backends against the developer's actual desktop, which
    was wrong three ways at once: it raised a GNOME approval prompt at
    them, it left a cached D-Bus connection that broke
    test_portal_dbusmock's private bus when the suite ran in order, and it
    turned a 2-second file into a 56-second one. tests/test_api.py's own
    `session()` helper has always done it this way.
    """
    return pyguitest.Session(pyguitest.NullBackend(), pyguitest.detect())


class TestStopping(unittest.TestCase):
    def test_the_context_manager_stops_the_program(self):
        gui = session()
        with gui.start_app(["sleep", "30"]) as app:
            self.assertTrue(app.is_running())
            pid = app.pid
        self.assertFalse(app.is_running())
        self.assertIsNotNone(app.returncode)
        # Gone from the OS, not merely marked exited in this object. The pid
        # has been reaped by wait(), so signal 0 -- the "does this process
        # exist" probe, which sends nothing -- must find nothing.
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_it_stops_even_when_the_block_raises(self):
        # The case the hand-rolled versions kept getting wrong: an exception
        # part way through must not leave the program running.
        gui = session()
        app = gui.start_app(["sleep", "30"])
        with self.assertRaises(ZeroDivisionError):
            with app:
                1 / 0
        self.assertFalse(app.is_running())

    def test_a_program_that_ignores_sigterm_is_killed(self):
        # Without the fallback this hangs forever; with a fallback that only
        # pretends, the process survives. Both are caught here.
        gui = session()
        app = gui.start_app(STUBBORN)
        time.sleep(0.3)  # let sh install its trap before signalling
        started = time.monotonic()
        status = app.stop(timeout=0.5)
        elapsed = time.monotonic() - started

        self.assertFalse(app.is_running())
        self.assertLess(elapsed, 10, "stop() did not bound its wait")
        # It took the kill path rather than exiting on SIGTERM: a killed
        # process reports -SIGKILL, a terminated one -SIGTERM or 0. Asserting
        # only "it is gone" would pass against a wrapper with no fallback.
        self.assertEqual(status, -9)

    def test_stop_is_idempotent(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        first = app.stop()
        second = app.stop()
        self.assertEqual(first, second)

    def test_stopping_an_already_exited_program_is_harmless(self):
        gui = session()
        app = gui.start_app(["true"])
        app.wait(timeout=5)
        self.assertEqual(app.stop(), 0)


class TestState(unittest.TestCase):
    def test_is_running_follows_the_process(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        try:
            self.assertTrue(app.is_running())
        finally:
            app.stop()
        self.assertFalse(app.is_running())

    def test_repr_says_which_program_and_whether_it_is_alive(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        try:
            self.assertIn("running", repr(app))
            self.assertIn("sleep", repr(app))
        finally:
            app.stop()
        self.assertIn("exited", repr(app))


class TestRestart(unittest.TestCase):
    def test_restart_gives_a_live_process_with_a_new_pid(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        try:
            first = app.pid
            self.assertIs(app.restart(), app)
            self.assertTrue(app.is_running())
            self.assertNotEqual(app.pid, first)
        finally:
            app.stop()

    def test_restart_reruns_the_same_command_and_options(self):
        # The options are rebuilt per launch rather than reused from a dict
        # the first launch mutated, so a restart is the same command run
        # again -- including the pipe this one asked for.
        gui = session()
        app = gui.start_app(["echo", "hello"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(app.stdout.read().strip(), "hello")
            app.restart()
            self.assertEqual(app.stdout.read().strip(), "hello")
            self.assertEqual(app.command, ["echo", "hello"])
        finally:
            app.stop()

    def test_restarting_an_already_exited_program_starts_it_again(self):
        gui = session()
        app = gui.start_app(["true"])
        app.wait(timeout=5)
        try:
            app.restart()
            self.assertIsNotNone(app.process)
        finally:
            app.stop()


class TestItStillBehavesLikeAPopen(unittest.TestCase):
    """start_app used to return a Popen, and scripts were written for it.

    The forwarding is what lets those keep working, so it is pinned here
    rather than left to be discovered by someone's script breaking.
    """

    def test_the_members_scripts_actually_used_are_forwarded(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        try:
            self.assertIsInstance(app.pid, int)
            self.assertIsNone(app.poll())
            self.assertIsNone(app.returncode)
            app.terminate()
            self.assertIsInstance(app.wait(timeout=5), int)
        finally:
            app.stop()

    def test_the_hand_rolled_dance_still_works_unchanged(self):
        # Verbatim shape of what examples/04 and friends were written as.
        gui = session()
        process = gui.start_app(["sleep", "30"])
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - timing
            process.kill()
            process.wait()
        self.assertFalse(process.is_running())

    def test_anything_not_written_out_falls_through_to_the_popen(self):
        gui = session()
        app = gui.start_app(["echo", "hi"], stdout=subprocess.PIPE, text=True)
        try:
            # communicate() is not one of the forwarded members.
            out, _err = app.communicate(timeout=5)
            self.assertEqual(out.strip(), "hi")
            self.assertEqual(app.args, ["echo", "hi"])
        finally:
            app.stop()

    def test_a_private_attribute_raises_rather_than_recursing(self):
        gui = session()
        app = gui.start_app(["true"])
        try:
            with self.assertRaises(AttributeError):
                app._not_a_real_attribute
        finally:
            app.stop()

    def test_the_real_popen_is_reachable(self):
        gui = session()
        app = gui.start_app(["sleep", "30"])
        try:
            self.assertIsInstance(app.process, subprocess.Popen)
        finally:
            app.stop()


class TestConstructedDirectly(unittest.TestCase):
    def test_an_application_can_wrap_a_process_it_did_not_start(self):
        # Not the usual path, but the constructor is public and the launch
        # callable is what restart() needs -- so it should be usable.
        process = subprocess.Popen(["sleep", "30"])
        app = Application(process, ["sleep", "30"], lambda: process)
        try:
            self.assertIs(app.process, process)
            self.assertTrue(app.is_running())
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
