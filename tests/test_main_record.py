"""`pyguitest record` -- the shim that hands off to pyguitest-recorder.

The recorder is a separate package that depends on this one, so there is no
import of it to test against and no flags to mirror. What is worth pinning is
the shape of the handoff: that the argument list crosses untouched, that the
recorder's exit status is what the process exits with, and that a machine
without the recorder gets install advice rather than a traceback.

Mirrors test_main_debug.py's approach -- drive main(), assert on captured
output and the exit code -- with sys.modules standing in for the package.
"""

import builtins
import contextlib
import io
import sys
import unittest
from unittest import mock

from pyguitest.__main__ import main


class _FakeRecorderCli:
    """Stand-in for pyguitest_recorder.cli, recording what it was handed."""

    def __init__(self, status=0):
        self.status = status
        self.argv = None

    def main(self, argv):
        self.argv = argv
        return self.status


@contextlib.contextmanager
def _recorder(cli):
    """Make `from pyguitest_recorder.cli import main` find `cli`."""
    package = mock.Mock()
    package.cli = cli
    with mock.patch.dict(
        sys.modules, {"pyguitest_recorder": package, "pyguitest_recorder.cli": cli}
    ):
        yield


@contextlib.contextmanager
def _no_recorder():
    """Make the same import fail, as it does wherever it is not installed.

    None in sys.modules raises ImportError on import, which is how the
    absence is simulated even on a machine that does have the recorder.
    """
    with mock.patch.dict(
        sys.modules, {"pyguitest_recorder": None, "pyguitest_recorder.cli": None}
    ):
        yield


@contextlib.contextmanager
def _import_fails(exc):
    """Fail the recorder's import with `exc`, leaving every other import alone.

    Stands in for an install that is present but unusable -- a dependency of
    the recorder's own gone missing, which raises from inside its import and
    names a module that is not the recorder.
    """
    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.startswith("pyguitest_recorder"):
            raise exc
        return real(name, *args, **kwargs)

    with mock.patch.object(builtins, "__import__", fake):
        yield


class TestRecordCommand(unittest.TestCase):
    def test_arguments_after_record_are_passed_through_unread(self):
        """Flags this parser has never heard of must still reach the recorder.

        The point of the shim: a recorder flag added later works here with
        no change on this side, which only holds if nothing between the two
        inspects the list.
        """
        cli = _FakeRecorderCli()
        with _recorder(cli):
            status = main(["record", "--stop-key", "ctrl+Escape", "-o", "out.py"])
        self.assertEqual(status, 0)
        self.assertEqual(cli.argv, ["--stop-key", "ctrl+Escape", "-o", "out.py"])

    def test_help_after_record_reaches_the_recorder(self):
        """`--help` belongs to the recorder too, not to argparse here.

        Regression guard for the dispatch order: a subparser would have
        printed this parser's help and exited instead.
        """
        cli = _FakeRecorderCli()
        with _recorder(cli):
            status = main(["record", "--help"])
        self.assertEqual(status, 0)
        self.assertEqual(cli.argv, ["--help"])

    def test_bare_record_passes_an_empty_list(self):
        cli = _FakeRecorderCli()
        with _recorder(cli):
            main(["record"])
        self.assertEqual(cli.argv, [])

    def test_recorder_exit_status_is_returned(self):
        cli = _FakeRecorderCli(status=2)
        with _recorder(cli):
            self.assertEqual(main(["record", "--doctor"]), 2)

    def test_missing_recorder_reports_how_to_install_it(self):
        stderr = io.StringIO()
        with _no_recorder(), contextlib.redirect_stderr(stderr):
            status = main(["record"])
        self.assertEqual(status, 2)
        self.assertIn("pip install pyguitest-recorder", stderr.getvalue())

    def test_record_is_listed_in_the_top_level_help(self):
        """Listed whether or not the recorder is installed.

        Discovering the recorder is most useful to the reader who does not
        have it, so the listing must not depend on the import succeeding.
        """
        stdout = io.StringIO()
        with _no_recorder(), contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertIn("record", stdout.getvalue())

    def test_missing_recorder_defers_the_can_it_record_question(self):
        """The install line must not imply the recorder would work here.

        A pure Wayland session cannot offer the XRecord capture the recorder
        uses today, so the message points at the recorder's own --doctor
        rather than answering for it -- a claim this side would otherwise
        have to keep in step with a package it does not depend on.
        """
        stderr = io.StringIO()
        with _no_recorder(), contextlib.redirect_stderr(stderr):
            main(["record"])
        self.assertIn("pyguitest-recorder --doctor", stderr.getvalue())

    def test_broken_install_is_not_reported_as_a_missing_one(self):
        """Telling someone to install what they have sends them the wrong way.

        The recorder imports pyguitest and, on 3.10, tomli. An ImportError
        naming one of those means the recorder is installed and broken, not
        absent, so the real error is what gets printed.
        """
        stderr = io.StringIO()
        broken = ModuleNotFoundError("No module named 'tomli'", name="tomli")
        with _import_fails(broken), contextlib.redirect_stderr(stderr):
            status = main(["record"])
        output = stderr.getvalue()
        self.assertEqual(status, 2)
        self.assertNotIn("pip install", output)
        self.assertIn("tomli", output)

    def test_record_only_dispatches_as_the_first_argument(self):
        """`pyguitest migrate record` is a path called record, not a handoff."""
        with mock.patch("pyguitest.__main__._scan", return_value=0) as scan:
            self.assertEqual(main(["migrate", "record"]), 0)
        scan.assert_called_once_with(["record"])


if __name__ == "__main__":
    unittest.main()
