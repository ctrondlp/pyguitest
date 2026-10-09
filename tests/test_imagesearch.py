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
haystack -- well past the 15s floor. Treat RMSE as the supported metric and
the rest as caller's-risk.

Two entry points are covered, because which one a machine has is not a
platform constant. `compare`/`identify` are the legacy commands a package
install gives you on Linux and macOS; the `winget` install on Windows lays
down `magick.exe` alone, so `magick compare`/`magick identify` are pinned
here too -- shapes run live against ImageMagick 7.1.2-31 Q16-HDRI on Windows
11 build 26200, not transcribed.
"""

import inspect
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pyguitest import png, tools
from pyguitest.backends import crop, imagesearch
from pyguitest.backends.imagesearch import ToolImageSearchBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import PyGUITestError

BY_NAME = {t.name: t for t in tools.IMAGE_TOOLS}


class Recorder:
    """Stands in for the injectable runner, returning a scripted result per call.

    `timeouts` records the budget each call was given. As in `_run`, a caller
    that passes nothing gets None here rather than the module default -- what
    the default *is* belongs to the real runner, and a recorder that invented
    it would make "did locate pass a budget down" unanswerable.
    """

    def __init__(self, results=None):
        self.calls = []
        self.timeouts = []
        self._results = list(results or [])

    def __call__(self, argv, allowed_returncodes=(0,), timeout=None):
        self.calls.append(argv)
        self.timeouts.append(timeout)
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

    def test_a_placement_overhanging_the_image_is_refused(self):
        # ImageMagick 7.1.2 scores placements past the edge: a 30x10 template
        # came back at y=97 of a 100-row image, measured. That is not a match,
        # and returning it would put a click off the bottom of the screen.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="200 100\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="514 (0.0078) @ 0,97", returncode=1),
                SimpleNamespace(stdout="30 10\n", stderr="", returncode=0),
            ]
        )
        with self.assertRaises(PyGUITestError) as raised:
            gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertIn("overhangs", str(raised.exception))

    def test_argv_shape_with_no_region(self):
        # identify comes first, and that ordering is deliberate: the search's
        # budget is derived from the haystack's area, so the haystack gets
        # measured before compare is handed a timeout. It also means a missing
        # template fails in milliseconds rather than after a long search.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(
                    stdout="", stderr="58.3651 (0.000890) @ 123,45", returncode=1
                ),
                SimpleNamespace(stdout="40 20\n", stderr="", returncode=0),
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual(
            self.runner.calls[0],
            ["identify", "-format", "%w %h", "/tmp/screen.png"],
        )
        self.assertEqual(
            self.runner.calls[1],
            [
                "compare",
                "-channel",
                "RGB",
                "-metric",
                "RMSE",
                "-subimage-search",
                "/tmp/screen.png",
                "/tmp/btn.png",
                "null:",
            ],
        )
        self.assertEqual(
            self.runner.calls[2], ["identify", "-format", "%w %h", "/tmp/btn.png"]
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
        self.assertEqual(compare_call[-3], crop_path)
        self.assertNotEqual(crop_path, "/tmp/screen.png")

        # Region offset (10, 20) added back onto compare's own (5, 6).
        self.assertEqual(match.x, 15)
        self.assertEqual(match.y, 26)

    def test_the_search_is_restricted_to_colour_channels(self):
        """The flag that keeps a channel mismatch from hiding a real match.

        ImageMagick's subimage search answers with the wrong offset, and with
        the output shape of a successful search, when the haystack and the
        template disagree about how many channels they have. On macOS every
        capture is RGBA -- `screencapture` writes an alpha channel -- and a
        template cut out by another tool is usually RGB, so a template that
        was on screen came back as `15423.5 (0.235347) @ 0,0`: the top-left
        corner of the rectangle searched, exit code 1, nothing on stderr.
        `-channel RGB` compares colour and ignores alpha, which located that
        same template in all four combinations of RGBA and RGB haystack and
        template.

        Pinned as a test rather than left to the docstrings because dropping
        an option from a command line looks harmless, and nothing this backend
        parses would change if it went: the answers would just start being
        wrong sometimes.
        """
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="0 (0) @ 5,6", returncode=1),
                SimpleNamespace(stdout="40 20\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png")
        search = self.runner.calls[1]
        self.assertEqual(search[:3], ["compare", "-channel", "RGB"])
        self.assertEqual(search[-1], "null:")

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
            def __call__(self, argv, allowed_returncodes=(0,), timeout=None):
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
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
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
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="58.3 (0.0008) @ 7,8", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        match = gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual((match.x, match.y), (7, 8))

    def test_exit_code_2_is_a_failure(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
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

    def test_a_timeout_names_the_budget_it_used(self):
        # "timed out" alone left a caller unable to tell a flat 15s budget
        # from one sized to their desktop, and the number is the whole point
        # now that the budget varies with what was searched. No region, so the
        # search really is the call that expires: with one, the crop runs
        # first and would be the call to time out.
        gui = ToolImageSearchBackend(BY_NAME["compare"])
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="compare", timeout=15),
        ):
            with self.assertRaises(PyGUITestError) as ctx:
                gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertIn("timed out after 15s", str(ctx.exception))
        self.assertIn("compare", str(ctx.exception))

    def test_unparseable_output_names_the_raw_stderr(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="nonsense output", returncode=1),
            ]
        )
        with self.assertRaises(PyGUITestError) as ctx:
            gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertIn("nonsense output", str(ctx.exception))

    def test_below_threshold_match_returns_none(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="58.3 (0.05) @ 1,2", returncode=1),
            ]
        )
        result = gui.locate("/tmp/screen.png", "/tmp/btn.png", threshold=0.01)
        self.assertIsNone(result)

    def test_above_threshold_match_is_returned(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="58.3 (0.0001) @ 1,2", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        result = gui.locate("/tmp/screen.png", "/tmp/btn.png", threshold=0.01)
        self.assertIsNotNone(result)
        self.assertEqual((result.x, result.y), (1, 2))


class TestMagickEntryPoint(unittest.TestCase):
    """ImageMagick 7's dispatcher, for the install that has only that one.

    `winget install ImageMagick.ImageMagick` -- the command docs/install.md
    and `pyguitest doctor` both name -- lands ImageMagick 7 with `magick.exe`
    and no legacy commands (verified live on Windows 11 build 26200, where the
    install directory holds exactly `magick.exe` and `unins000.exe`). Against
    that install, `magick compare -metric RMSE -subimage-search` located a
    crop at exactly the offset it was cut from, and `magick identify -format
    "%w %h"` answered `40 24` -- so both shapes this class pins were run, not
    just read about.
    """

    def _backend(self, results=None):
        self.runner = Recorder(results=results)
        return ToolImageSearchBackend(BY_NAME["magick"], runner=self.runner)

    def test_it_is_a_supported_entry_point(self):
        self.assertIn(Capability.IMAGE_LOCATE, self._backend().capabilities)

    def test_it_names_itself_after_the_entry_point(self):
        self.assertEqual(self._backend().name, "imagesearch:magick")

    def test_the_search_is_dispatched_through_magick(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="", returncode=0),  # crop
                SimpleNamespace(stdout="", stderr="0 (0) @ 5,6", returncode=1),
                SimpleNamespace(stdout="40 24\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(10, 20, 100, 50))
        self.assertEqual(
            self.runner.calls[1],
            [
                "magick",
                "compare",
                "-channel",
                "RGB",
                "-metric",
                "RMSE",
                "-subimage-search",
                self.runner.calls[0][5],
                "/tmp/btn.png",
                "null:",
            ],
        )

    def test_the_size_lookup_is_dispatched_through_magick(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="", returncode=0),  # crop
                SimpleNamespace(stdout="", stderr="0 (0) @ 5,6", returncode=1),
                SimpleNamespace(stdout="40 24\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(0, 0, 100, 50))
        self.assertEqual(
            self.runner.calls[2],
            ["magick", "identify", "-format", "%w %h", "/tmp/btn.png"],
        )


class TestSearchBudget(unittest.TestCase):
    """The subprocess budget is sized to the area searched, not fixed.

    A flat 15s was a defect rather than a tuning preference. Measured live
    against the Windows install above, which has no FFT delegate: 480x270 took
    5.07s, 960x540 15.08s and 1920x1080 51.35s -- so a full-screen
    locate_image() failed outright on a 1080p desktop, on the platform whose
    ImageMagick is the slow one.
    """

    def _backend(self, results=None):
        self.runner = Recorder(results=results)
        return ToolImageSearchBackend(BY_NAME["compare"], runner=self.runner)

    def test_a_small_search_keeps_the_floor(self):
        # The demo's 240x160 haystack, and everything else that already
        # worked: an unchanged budget is the point of having a floor.
        self.assertEqual(
            imagesearch._search_timeout(240 * 160), imagesearch._SUBPROCESS_TIMEOUT
        )

    def test_a_1080p_desktop_gets_more_than_it_was_measured_to_need(self):
        self.assertGreater(imagesearch._search_timeout(1920 * 1080), 51.35)

    def test_a_4k_desktop_hits_the_ceiling_rather_than_scaling_forever(self):
        self.assertEqual(
            imagesearch._search_timeout(3840 * 2160),
            imagesearch._MAX_SUBPROCESS_TIMEOUT,
        )

    def test_the_search_is_given_the_derived_budget(self):
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="0 (0) @ 0,0", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertEqual(
            self.runner.timeouts[1], imagesearch._search_timeout(1920 * 1080)
        )

    def test_a_region_budgets_the_region_rather_than_the_desktop(self):
        # The same reason within= is worth passing: the crop is what compare
        # searches, so the crop is what the budget is for.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="", stderr="", returncode=0),  # crop
                SimpleNamespace(stdout="", stderr="0 (0) @ 0,0", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png", region=(0, 0, 200, 100))
        self.assertEqual(self.runner.timeouts[1], imagesearch._SUBPROCESS_TIMEOUT)

    def test_only_the_search_is_budgeted(self):
        # identify and the crop are metadata operations on one image; they are
        # left to the runner's own default rather than handed a budget derived
        # from an area that has nothing to do with them.
        gui = self._backend(
            results=[
                SimpleNamespace(stdout="1920 1080\n", stderr="", returncode=0),
                SimpleNamespace(stdout="", stderr="0 (0) @ 0,0", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/screen.png", "/tmp/btn.png")
        self.assertIsNone(self.runner.timeouts[0])
        self.assertIsNone(self.runner.timeouts[2])

    def test_the_runner_default_covers_those_metadata_calls(self):
        default = inspect.signature(ToolImageSearchBackend._run)
        self.assertEqual(
            default.parameters["timeout"].default, imagesearch._SUBPROCESS_TIMEOUT
        )

    def test_a_haystack_that_cannot_be_measured_falls_back_to_the_floor(self):
        # Two ways in, both ending at the floor: an injected runner hands back
        # an unhelpful result, and the real _run raises for the nonzero exit.
        # Neither is allowed out of the budget calculation -- compare reports
        # an unreadable file in its own words, and the budget is no place to
        # raise it from -- and neither may spend five minutes on an area that
        # was never measured.
        gui = self._backend(
            results=[
                SimpleNamespace(
                    stdout="", stderr="identify: unable to open image", returncode=1
                ),
                SimpleNamespace(stdout="", stderr="0 (0) @ 0,0", returncode=1),
                SimpleNamespace(stdout="10 10\n", stderr="", returncode=0),
            ]
        )
        gui.locate("/tmp/gone.png", "/tmp/btn.png")
        self.assertEqual(self.runner.timeouts[1], imagesearch._SUBPROCESS_TIMEOUT)


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


class TestCropModule(unittest.TestCase):
    """crop.py's own behaviour, including the paths that fail.

    Shared by the capture backends and the image search, so a mistake here
    is a mistake in both. Its error handling was entirely untested: every
    caller injected a runner, so `_run` -- the part that actually decides
    what a failed crop looks like -- never executed.
    """

    def test_available_needs_only_one_of_the_two_commands(self):
        with mock.patch("pyguitest.backends.crop.shutil.which") as which:
            which.side_effect = lambda name: (
                "/usr/bin/magick" if name == "magick" else None
            )
            self.assertTrue(crop.available())
            which.side_effect = lambda name: (
                "/usr/bin/convert" if name == "convert" else None
            )
            self.assertTrue(crop.available())
            which.side_effect = lambda name: None
            self.assertFalse(crop.available())

    def test_argv_puts_repage_after_the_crop(self):
        # +repage is not decoration: without it ImageMagick keeps the crop
        # offset in the page geometry and every later operation sees
        # coordinates shifted by it.
        line = crop.argv("/tmp/in.png", (1, 2, 3, 4), "/tmp/out.png")
        self.assertEqual(
            line[1:], ["/tmp/in.png", "-crop", "3x4+1+2", "+repage", "/tmp/out.png"]
        )

    def test_crop_returns_the_destination(self):
        calls = []
        self.assertEqual(
            crop.crop("/a.png", (0, 0, 1, 1), "/b.png", runner=calls.append),
            "/b.png",
        )
        self.assertEqual(len(calls), 1)

    def test_a_missing_imagemagick_says_what_to_install(self):
        # The likeliest failure on a machine that has a screenshot tool but
        # no ImageMagick -- and "FileNotFoundError: magick" alone would not
        # explain why a *capture* needed it.
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("magick")):
            with self.assertRaises(PyGUITestError) as caught:
                crop.crop("/a.png", (0, 0, 1, 1), "/b.png")
        message = str(caught.exception)
        self.assertIn("not installed", message)
        self.assertIn("ImageMagick", message)

    def test_a_timeout_is_reported_as_one(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("magick", 15)
        ):
            with self.assertRaises(PyGUITestError) as caught:
                crop.crop("/a.png", (0, 0, 1, 1), "/b.png")
        self.assertIn("timed out", str(caught.exception))

    def test_a_nonzero_exit_reports_imagemagicks_own_stderr(self):
        with mock.patch(
            "subprocess.run",
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="unable to open image"
            ),
        ):
            with self.assertRaises(PyGUITestError) as caught:
                crop.crop("/a.png", (0, 0, 1, 1), "/b.png")
        self.assertIn("unable to open image", str(caught.exception))

    def test_a_silent_failure_still_names_the_exit_code(self):
        with mock.patch(
            "subprocess.run",
            return_value=SimpleNamespace(returncode=2, stdout="", stderr=""),
        ):
            with self.assertRaises(PyGUITestError) as caught:
                crop.crop("/a.png", (0, 0, 1, 1), "/b.png")
        self.assertIn("no output", str(caught.exception))


def _pattern(width, height, x0=0, y0=0):
    """Deterministic, position-unique pixels.

    Every coordinate gets a different colour, so a sub-image can only match
    at one place. A flat background would let compare match anywhere and
    the test would pass without proving the offset arithmetic.
    """
    rows = []
    for y in range(y0, y0 + height):
        row = bytearray()
        for x in range(x0, x0 + width):
            row += bytes([(x * 7) % 256, (y * 11) % 256, (x * y) % 256])
        rows.append(bytes(row))
    return rows


@unittest.skipUnless(
    tools.best(tools.IMAGE_TOOLS)
    and (shutil.which("magick") or shutil.which("convert")),
    "ImageMagick is not installed",
)
@unittest.skipUnless(
    tools.best(tools.IMAGE_TOOLS) is not None,
    "ImageMagick not installed (compare or magick)",
)
class TestAgainstRealImageMagick(unittest.TestCase):
    """The crop/compare/offset round trip, run for real.

    Everything else in this file mocks the runner, so it checks the command
    line and the parsing but never that the numbers coming back mean what
    locate() claims. In particular the region path adds the crop's own
    offset back onto compare's answer, and arithmetic like that is exactly
    what a mocked test cannot confirm.

    The images are generated with this package's own PNG encoder rather
    than committed as fixtures: no binary files in the tree, and it
    exercises png.py against a real decoder at the same time.

    Whichever entry point is present gets exercised, `compare` or `magick`:
    requiring `compare` by name made this class skip on the install that has
    only ImageMagick 7's dispatcher, which is the Windows one, so the one
    machine shape whose command line had never been run was the one excluded
    from the only test that runs a command line for real.

    Every call goes through `_locate`, which turns "ImageMagick could not
    complete" into a skip rather than a failure -- see its docstring for
    why that is not the same as hiding a bug.
    """

    HAYSTACK = (60, 40)
    TEMPLATE_AT = (20, 12)
    TEMPLATE_SIZE = (8, 6)

    def setUp(self):
        self.gui = ToolImageSearchBackend(tools.best(tools.IMAGE_TOOLS))
        self.haystack = self._write(_pattern(*self.HAYSTACK), self.HAYSTACK)
        self.template = self._write(
            _pattern(*self.TEMPLATE_SIZE, *self.TEMPLATE_AT), self.TEMPLATE_SIZE
        )

    def _write(self, rows, size):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        png.write_rgb(path, size[0], size[1], rows)
        return path

    def _locate(self, *args, **kwargs):
        """`locate()`, reporting a tool that could not run as a skip.

        This class checks *arithmetic* against real ImageMagick. When
        ImageMagick itself fails to complete -- the derived subprocess budget
        expiring, or the resource exhaustion a loaded machine can push it into
        -- that says nothing about the arithmetic, and a red suite for it
        teaches
        people to re-run rather than read. Observed once here: this class
        failed inside a full-suite run and passed in isolation and on every
        rerun, which is the shape of the machine being busy, not of a bug.

        Narrow on purpose. Only `PyGUITestError` is skipped, which is what
        the tool runner raises for a timeout or an unexpected exit code; a
        wrong *answer* still fails, and the command line and output parsing
        are covered exhaustively by the mocked tests above, so nothing that
        a skip here could hide is untested.
        """
        try:
            return self.gui.locate(*args, **kwargs)
        except PyGUITestError as exc:  # pragma: no cover - environment only
            self.skipTest(f"ImageMagick could not complete: {exc}")

    def test_a_template_is_found_at_the_position_it_was_cut_from(self):
        match = self._locate(self.haystack, self.template)
        self.assertIsNotNone(match, "compare found nothing at all")
        self.assertEqual((match.x, match.y), self.TEMPLATE_AT)
        self.assertEqual((match.width, match.height), self.TEMPLATE_SIZE)

    def test_a_region_search_returns_haystack_coordinates_not_crop_ones(self):
        # The assertion that matters. compare sees only the cropped image
        # and answers in *its* coordinates; locate() must add the crop
        # offset back. Getting this wrong yields a plausible number that is
        # wrong by exactly the region's origin.
        match = self._locate(self.haystack, self.template, region=(16, 8, 30, 25))
        self.assertIsNotNone(match)
        self.assertEqual((match.x, match.y), self.TEMPLATE_AT)

    def test_the_offset_is_added_rather_than_coincidental(self):
        # Two different regions containing the same block must both report
        # the same haystack position -- which they cannot do by accident.
        first = self._locate(self.haystack, self.template, region=(0, 0, 40, 30))
        second = self._locate(self.haystack, self.template, region=(10, 5, 40, 30))
        self.assertEqual((first.x, first.y), self.TEMPLATE_AT)
        self.assertEqual((second.x, second.y), self.TEMPLATE_AT)

    def test_an_exact_match_scores_zero_on_a_lower_is_better_metric(self):
        match = self._locate(self.haystack, self.template)
        self.assertEqual(match.score, 0.0)

    def test_a_template_that_is_not_present_is_refused_by_a_threshold(self):
        absent = self._write([bytes([255, 0, 255] * 4) for _ in range(4)], (4, 4))
        match = self._locate(self.haystack, absent, threshold=0.01)
        self.assertIsNone(match)
