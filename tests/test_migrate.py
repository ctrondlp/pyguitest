import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pyguitest.__main__ import main


class TestMigrationScanner(unittest.TestCase):
    def _scan(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.pl"
            path.write_text(source)
            self.output = io.StringIO()
            with contextlib.redirect_stdout(self.output):
                return main(["migrate", str(path)])

    def test_clean_script_exits_zero(self):
        source = "use X11::GUITest qw(StartApp SendKeys);\nStartApp('xterm');\n"
        self.assertEqual(self._scan(source), 0)

    def test_no_path_call_exits_nonzero(self):
        # GetMousePos cannot be implemented on any compositor, so a script
        # using it needs rethinking, not porting -- worth a failing exit code
        # so a migration check can gate CI.
        source = "my ($x, $y) = GetMousePos();\n"
        self.assertEqual(self._scan(source), 1)

    def test_empty_script_is_clean(self):
        self.assertEqual(self._scan("print qq{hello}\n"), 0)

    def test_report_names_the_blocked_call(self):
        self._scan("my ($x, $y) = GetMousePos();\n")
        self.assertIn("GetMousePos", self.output.getvalue())
        self.assertIn("no replacement", self.output.getvalue())

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


if __name__ == "__main__":
    unittest.main()
