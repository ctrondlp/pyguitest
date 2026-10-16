"""The migration scanner: what it finds in a Perl source, and what it reports.

Each call gets a tier, and the exit status is what a port can be gated on in
CI -- so both the report and the status are checked, not only the text.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pyguitest.__main__ import main


class TestMigrationScanner(unittest.TestCase):
    def _scan(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.pl"
            path.write_text(source, encoding="utf-8")
            self.output = io.StringIO()
            with contextlib.redirect_stdout(self.output):
                return main(["migrate", str(path)])

    def test_clean_script_exits_zero(self):
        source = "use X11::GUITest qw(StartApp SendKeys);\nStartApp('xterm');\n"
        self.assertEqual(self._scan(source), 0)

    def test_no_path_call_exits_nonzero(self):
        # GetMousePos ports to X11, Windows and macOS but no compositor will
        # ever answer it, so which target the port is for is a decision a
        # human has to make -- worth a failing exit code so a migration check
        # can gate CI.
        source = "my ($x, $y) = GetMousePos();\n"
        self.assertEqual(self._scan(source), 1)

    def test_call_with_no_spelling_at_all_exits_nonzero(self):
        # The other half of the exit status, and a different problem: this
        # one has no replacement on any backend, so the call itself has to
        # go rather than be translated.
        self.assertEqual(self._scan("GetScreenDepth();\n"), 1)
        self.assertIn("no pyguitest spelling at all", self.output.getvalue())

    def test_empty_script_is_clean(self):
        self.assertEqual(self._scan("print qq{hello}\n"), 0)

    def test_report_names_the_blocked_call_and_its_replacement(self):
        # Regression: this reported "no replacement -- rethink this call" for
        # all six Tier.NO_PATH names long after they shipped, so the one
        # tool a migrating user is told to trust sent them away from an
        # operation that works on the desktop they are porting from.
        self._scan("my ($x, $y) = GetMousePos();\n")
        self.assertIn("GetMousePos", self.output.getvalue())
        self.assertIn("pointer_position", self.output.getvalue())
        self.assertNotIn("no replacement", self.output.getvalue())

    def test_substring_names_are_not_confused(self):
        # IsWindow is a prefix of IsWindowCursor and IsWindowViewable; a naive
        # alternation would match the short name first and mis-tier the call.
        self.assertEqual(self._scan("IsWindowCursor($w, 1);\n"), 1)
        self.assertEqual(self._scan("IsWindow($w);\n"), 0)

    def test_summary_counts_calls_not_distinct_names(self):
        # Regression: the per-tier summary counted len(distinct names) even
        # though the column reads as a call count, so a script calling
        # GetMousePos forty times reported 1.
        source = "GetMousePos();\n" * 40
        self._scan(source)
        output = self.output.getvalue()
        self.assertIn("40 call(s) across 1 distinct name(s)", output)

    def test_blocked_line_sums_occurrences_across_names(self):
        source = "GetMousePos();\nIsKeyPressed('a');\nIsKeyPressed('b');\n"
        self._scan(source)
        output = self.output.getvalue()
        self.assertIn("3 call(s) across 2 distinct name(s)", output)

    def test_report_gives_line_numbers(self):
        # A count says how much work a port is; a line number says where.
        self._scan("print 1;\nSendKeys('a');\nprint 2;\nSendKeys('b');\n")
        self.assertIn("lines 2, 4", self.output.getvalue())

    def test_constants_are_reported_too(self):
        # `qw(:ALL)` exports the :CONST integers as well, and a script using
        # M_RIGHT or a cursor shape got no guidance at all before.
        self._scan("ClickMouseButton(M_RIGHT);\nIsWindowCursor($w, XC_WATCH);\n")
        output = self.output.getvalue()
        self.assertIn("M_RIGHT", output)
        self.assertIn("click(3)", output)
        self.assertIn("XC_*", output)

    def test_a_script_of_only_constants_is_still_reported(self):
        self._scan("my $button = M_MIDDLE;\n")
        self.assertIn("M_MIDDLE", self.output.getvalue())
        self.assertNotIn("No X11::GUITest calls found", self.output.getvalue())

    def test_every_cursor_shape_shares_one_row(self):
        self._scan("IsWindowCursor($w, XC_WATCH);\nIsWindowCursor($w, XC_XTERM);\n")
        output = self.output.getvalue()
        # One folded row rather than a near-identical line per cursor name;
        # the shapes differ only in the integer. XC_WATCH still appears
        # inside that row's note, as the worked example.
        self.assertIn("XC_* x2", output)
        self.assertNotIn("-> XC_WATCH", output)
        self.assertEqual(output.count("the same integer"), 1)


class TestScanInputs(unittest.TestCase):
    """What the scanner accepts: files, directories, and --json."""

    def _run(self, argv):
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            return main(["migrate", *argv])

    def test_a_directory_is_walked_for_perl_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lib").mkdir()
            (root / "lib" / "Driver.pm").write_text("SendKeys('a');\n")
            (root / "t").mkdir()
            (root / "t" / "smoke.t").write_text("StartApp('xterm');\n")
            (root / "notes.txt").write_text("GetMousePos();\n")
            self.assertEqual(self._run([str(root)]), 0)
        output = self.output.getvalue()
        self.assertIn("Driver.pm", output)
        self.assertIn("smoke.t", output)
        # Not a Perl suffix, so not lexed -- and its GetMousePos, which would
        # have failed the scan, is not counted.
        self.assertNotIn("notes.txt", output)

    def test_a_named_file_is_read_whatever_it_is_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy-driver"
            path.write_text("SendKeys('a');\n")
            self.assertEqual(self._run([str(path)]), 0)
        self.assertIn("send_keys", self.output.getvalue())

    def test_json_output_carries_the_tiers_and_the_two_blocked_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.pl"
            path.write_text("GetMousePos();\nGetScreenDepth();\nSendKeys(M_LEFT);\n")
            self.assertEqual(self._run(["--json", str(path)]), 1)
        data = json.loads(self.output.getvalue())
        self.assertEqual(data["summary"]["wayland_blocked"], ["GetMousePos"])
        self.assertEqual(data["summary"]["unported"], ["GetScreenDepth"])
        self.assertEqual(data["summary"]["calls"], 3)
        self.assertEqual(data["summary"]["constants"], 1)
        self.assertEqual(data["summary"]["by_tier"]["NO_PATH"], 1)
        calls = {call["name"]: call for call in data["files"][str(path)]["calls"]}
        self.assertEqual(calls["GetMousePos"]["replacement"], "pointer_position")
        self.assertEqual(calls["GetMousePos"]["lines"], [1])
        self.assertIsNone(calls["GetScreenDepth"]["replacement"])
        constants = data["files"][str(path)]["constants"]
        self.assertEqual([c["name"] for c in constants], ["M_LEFT"])


if __name__ == "__main__":
    unittest.main()
