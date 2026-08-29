"""Tests for the ImageMagick-backed template-matching backend.

The argv shapes and the stderr parsing have now been checked against a real
ImageMagick (7.1.2-27 Q16-HDRI): `compare -metric RMSE -subimage-search`,
`identify -format "%w %h"` and the `-crop WxH+X+Y +repage` round trip were
all run on generated images, and the strings in TestRealCompareOutput are
captured from that run rather than transcribed from documentation. That
exercise found a real bug -- scores in exponent form were mis-parsed
silently -- which is fixed and pinned below.

The subprocess itself is still faked here (an injected `runner`), so the
suite needs no ImageMagick installed. What remains unverified: metrics
other than RMSE. NCC in particular reported a *different, wrong* offset for
the same images, and PHASE did not finish within two minutes on a 200x120
haystack -- well past this backend's 15s timeout. Treat RMSE as the
supported metric and the rest as caller's-risk.
"""

import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from pyguitest import tools
from pyguitest.backends import crop, imagesearch
from pyguitest.backends.imagesearch import ToolImageSearchBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import PyGUITestError

BY_NAME = {t.name: t for t in tools.IMAGE_TOOLS}


class Recorder:
    """Stands in for the injectable runner, returning a scripted result per call."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def __call__(self, argv, allowed_returncodes=(0,)):
        self.calls.append(argv)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)


class TestImageSearch(unittest.TestCase):
    def _backend(self, results=None):
        self.runner = Recorder(results=results)
        return ToolImageSearchBackend(BY_NAME["compare"], runner=self.runner)

    def test_capability_is_declared(self):
        gui = self._backend()
        self.assertIn(Capability.IMAGE_LOCATE, gui.capabilities)

    def test_unmapped_tool_is_refused(self):
        fake = tools.ExternalTool("future-compare", frozenset())
        with self.assertRaises(PyGUITestError):
            ToolImageSearchBackend(fake)

    def test_argv_shape_with_no_region(self):
        gui = self._backend(
            results=[
                SimpleNamespace(
                    stdout="", stderr="58.3651 (0.000890) @ 123,45", returncode=1
                ),
                SimpleNamespace(stdout="40 20\n", stderr="", returncode=0),
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual(
            self.runner.calls[0],
            [
                "compare",
                "-metric",
                "RMSE",
                "-subimage-search",
                "/tmp/screen.png",
                "/tmp/btn.png",
                "null:",
            ],
        )
        self.assertEqual(
            self.runner.calls[1], ["identify", "-format", "%w %h", "/tmp/btn.png"]
        )
        self.assertEqual(match.x, 123)
        self.assertEqual(match.y, 45)
        self.assertEqual(match.width, 40)
        self.assertEqual(match.height, 20)
        self.assertAlmostEqual(match.score, 0.000890)

    def test_argv_shape_with_region_crops_first(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="", returncode=0),  # convert
                SimpleNamespace(
                    stdout="", stderr="12.0 (0.001) @ 5,6", returncode=1
                ),  # compare
                SimpleNamespace(stdout="40 20\n", stderr="", returncode=0),  # identify
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(10, 20, 100, 50))
        convert_call = self.runner.calls[0]
        # `magick` where installed, `convert` otherwise -- asserting one or
        # the other would pass or fail depending on the machine's
        # ImageMagick, which is exactly the kind of environment-dependent
        # test that hides real breakage. crop.crop_command() is pinned
        # separately below.
        self.assertIn(convert_call[0], ("magick", "convert"))
        self.assertEqual(convert_call[1], "/tmp/screen.png")
        self.assertEqual(convert_call[2], "-crop")
        self.assertEqual(convert_call[3], "100x50+10+20")
        self.assertEqual(convert_call[4], "+repage")
        crop_path = convert_call[5]

        compare_call = self.runner.calls[1]
        self.assertEqual(compare_call[4], crop_path)
        self.assertNotEqual(crop_path, "/tmp/screen.png")

        # Region offset (10, 20) added back onto compare's own (5, 6).
        self.assertEqual(match.x, 15)
        self.assertEqual(match.y, 26)

    def test_crop_temp_file_is_deleted_after_success(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="", returncode=0),  # convert
                SimpleNamespace(stdout="", stderr="1.0 (0.0) @ 0,0", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(0, 0, 10, 10))
        crop_path = self.runner.calls[0][5]
        self.assertFalse(os.path.exists(crop_path))

    def test_crop_temp_file_is_deleted_even_when_compare_fails(self):
        class FailingRecorder(Recorder):
            def __call__(self, argv, allowed_returncodes=(0,)):
                self.calls.append(argv)
                if argv and argv[0] == "compare":
                    raise PyGUITestError("boom")
                return SimpleNamespace(stdout="", stderr="", returncode=0)

        self.runner = FailingRecorder()
        gui = ToolImageSearchBackend(BY_NAME["compare"], runner=self.runner)
        with self.assertRaises(PyGUITestError):
            gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(0, 0, 10, 10))
        crop_path = self.runner.calls[0][5]
        self.assertFalse(os.path.exists(crop_path))

    def test_exit_code_0_is_treated_as_success(self):
        # Images identical -- still a valid (perfect) match.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="0 (0) @ 0,0", returncode=0),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual((match.x, match.y), (0, 0))

    def test_exit_code_1_is_treated_as_success(self):
        # Regression: compare's exit 1 ("images differ") is the *normal*
        # outcome of a subimage search, not a failure.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="58.3 (0.0008) @ 7,8", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual((match.x, match.y), (7, 8))

    def test_exit_code_2_is_a_failure(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="no such file", returncode=2),
            ]
        )
        with self.assertRaises(PyGUITestError):
            gui.locate("/tmp/screen.png", "/tmp/btn.png")

    def test_timeout_is_a_failure(self):
        # Exercises the default _run, not an injected runner: the timeout
        # catch lives in _run itself, same as capture.py's own runner.
        gui = ToolImageSearchBackend(BY_NAME["compare"])
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="compare", timeout=15),
        ):
            with self.assertRaises(PyGUITestError):
                gui.locate("/tmp/screen.png", "/tmp/btn.png")

    def test_unparseable_output_names_the_raw_stderr(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="nonsense output", returncode=1),
            ]
        )
        with self.assertRaises(PyGUITestError) as ctx:
            gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertIn("nonsense output", str(ctx.exception))

    def test_below_threshold_match_returns_none(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="58.3 (0.05) @ 1,2", returncode=1),
            ]
        )
        result = gui.locate("/tmp/screen.png", "/tmp/btn.png", threshold=0.01)
        self.assertIsNone(result)

    def test_above_threshold_match_is_returned(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="58.3 (0.0001) @ 1,2", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        result = gui.locate("/tmp/screen.png", "/tmp/btn.png", threshold=0.01)
        self.assertIsNotNone(result)
        self.assertEqual((result.x, result.y), (1, 2))


class TestCropCommand(unittest.TestCase):
    """Which binary crops: `magick` is preferred, `convert` is the fallback."""

    def test_prefers_magick_when_present(self):
        with mock.patch(
            "pyguitest.backends.crop.shutil.which",
            return_value="/usr/bin/magick",
        ):
            self.assertEqual(crop.crop_command(), "magick")

    def test_falls_back_to_convert_when_magick_is_absent(self):
        with mock.patch("pyguitest.backends.crop.shutil.which", return_value=None):
            self.assertEqual(crop.crop_command(), "convert")


class TestRealCompareOutput(unittest.TestCase):
    """_MATCH_RE against stderr captured from a real ImageMagick 7.1.2-27.

    Every string here was produced by running the exact argv locate() builds
    against real images, not transcribed from documentation -- including the
    exponent forms that an earlier bare-decimal pattern mis-parsed silently.
    """

    def _parse(self, text):
        found = imagesearch._MATCH_RE.search(text)
        self.assertIsNotNone(found, f"did not parse {text!r}")
        return found.group(1), found.group(2), int(found.group(3)), int(found.group(4))

    def test_exact_match_exit_zero(self):
        self.assertEqual(self._parse("0 (0) @ 63,41 [0]"), ("0", "0", 63, 41))

    def test_ordinary_imperfect_match(self):
        self.assertEqual(
            self._parse("1285 (0.0196078) @ 63,41 [0.0196072]"),
            ("1285", "0.0196078", 63, 41),
        )

    def test_small_scores_print_in_exponent_form(self):
        # %g switches to exponent notation below ~1e-4, which a near-exact
        # match reaches. This raised PyGUITestError before the fix.
        self.assertEqual(
            self._parse("1.4013e-45 (1.2e-06) @ 10,20 [1e-06]"),
            ("1.4013e-45", "1.2e-06", 10, 20),
        )

    def test_large_scores_in_exponent_form_are_not_truncated(self):
        # Regression for silent corruption: the old pattern parsed the
        # absolute score of "6.55e+04" as "04" and carried on.
        self.assertEqual(
            self._parse("6.55e+04 (0.5) @ 1,2 [0]"), ("6.55e+04", "0.5", 1, 2)
        )

    def test_a_warning_appended_to_the_result_line_is_tolerated(self):
        # compare glues warnings straight onto the result with no separator
        # and still exits 0.
        self.assertEqual(
            self._parse(
                "0 (0) @ 0,0 [0.40985]compare: images too dissimilar "
                "`big.png' @ warning/compare.c/CompareImagesCommand/1183."
            ),
            ("0", "0", 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
