"""ToolCaptureBackend: the region normalization and the crop fallback.

The interesting behaviour is not "does it build a command line" but what it
does with a region the tool cannot express. Two of the four tools have no
exact-rectangle mode at all, and the earlier code refused a region on those
outright -- so a caller had to know which tool the session had picked
before it could ask for a rectangle. These pin the normalized contract:
every tool takes the same (x, y, width, height), whether it has a flag for
it or not.
"""

import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pyguitest import tools
from pyguitest.backends.base import check_region, fit_region
from pyguitest.backends.capture import (
    _SUBPROCESS_TIMEOUT,
    ToolCaptureBackend,
    capture_window_id,
)
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, PermissionRequired, PyGUITestError
from pyguitest.png import write_rgb

BY_NAME = {t.name: t for t in tools.CAPTURE_TOOLS}

SHOT = os.path.join(tempfile.gettempdir(), "pyguitest-shot.png")
"""Where the stub tools below actually write.

Under the platform's own temp directory rather than a literal `/tmp`: the
fake runner really does open this path, so on Windows -- which has no `/tmp`
-- every test in this file failed with a FileNotFoundError naming a directory
that was never the point. The path also appears in the argv assertions, so it
has to be one value rather than a literal repeated twenty-six times."""

NEVER_WRITTEN = os.path.join(tempfile.gettempdir(), "pyguitest-never-written.png")
"""A destination the stub deliberately does not create, for the empty-result check."""

REGION = (10, 20, 100, 50)


def _write_fake_image(argv):
    """Simulate a real tool actually writing pixels to its destination.

    Every command here (screenshot or crop) ends in a destination path;
    `capture()` now checks that path is non-empty before trusting a 0 exit
    code, so a stub runner that never wrote anything would fail that check
    on every call.
    """
    destination = argv[-1]
    if isinstance(destination, str) and destination.endswith(".png"):
        with open(destination, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        _write_fake_image(argv)
        return argv


class TestCapture(unittest.TestCase):
    def _backend(self, name):
        self.runner = Recorder()
        return ToolCaptureBackend(BY_NAME[name], runner=self.runner)

    def test_grim_whole_screen(self):
        gui = self._backend("grim")
        path = gui.capture(path=SHOT)
        self.assertEqual(self.runner.calls[0], ["grim", SHOT])
        self.assertEqual(path, SHOT)

    def test_gnome_screenshot_flag_order(self):
        gui = self._backend("gnome-screenshot")
        gui.capture(path=SHOT)
        self.assertEqual(self.runner.calls[0], ["gnome-screenshot", "-f", SHOT])

    def test_a_temporary_path_is_allocated_when_none_given(self):
        gui = self._backend("grim")
        path = gui.capture()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(path.endswith(".png"))
        self.assertIn(path, self.runner.calls[0])

    def test_capability_is_declared(self):
        gui = self._backend("grim")
        self.assertIn(Capability.SCREEN_CAPTURE, gui.capabilities)

    def test_window_capture_is_not_claimed(self):
        # A tool that captures pixels still cannot resolve a Window to a
        # rectangle; the composite joins it to a geometry backend instead.
        gui = self._backend("grim")
        self.assertNotIn(Capability.WINDOW_CAPTURE, gui.capabilities)

    def test_unmapped_tool_is_refused(self):
        fake = tools.ExternalTool("future-shot", frozenset())
        with self.assertRaises(PyGUITestError):
            ToolCaptureBackend(fake)

    def test_window_argument_is_refused_rather_than_silently_dropped(self):
        # Regression: capture(window=...) used to build the same whole-screen
        # command as capture() with no window, so a caller screenshotting one
        # window got a plausible-looking image of the wrong thing.
        gui = self._backend("grim")
        with self.assertRaises(CapabilityUnsupported):
            gui.capture(window="some-window-handle", path=SHOT)
        self.assertEqual(self.runner.calls, [])


class TestRegionIsNormalized(unittest.TestCase):
    """One (x, y, width, height) tuple; each tool's own syntax is built here.

    Previously the caller had to check `self.tool.name` and hand grim
    "x,y WxH" or ImageMagick "WxH+X+Y" itself, which meant no caller could
    ask for a region without first knowing which tool got picked.
    """

    def _backend(self, name):
        self.runner = Recorder()
        return ToolCaptureBackend(BY_NAME[name], runner=self.runner)

    def test_grim_gets_its_own_geometry_syntax(self):
        gui = self._backend("grim")
        gui.capture(path=SHOT, region=REGION)
        self.assertEqual(
            self.runner.calls[0],
            ["grim", "-g", "10,20 100x50", SHOT],
        )

    def test_import_gets_imagemagick_syntax(self):
        gui = self._backend("import")
        gui.capture(path=SHOT, region=REGION)
        self.assertEqual(
            self.runner.calls[0],
            [
                "import",
                "-window",
                "root",
                "-crop",
                "100x50+10+20",
                SHOT,
            ],
        )

    def test_floats_are_covered_not_truncated(self):
        # geometry() is documented as returning ints, but a caller computing
        # a rectangle (a midpoint, a scale factor, element extents on a 150%
        # display) easily produces floats, and every tool's command line
        # needs integers. The origin floors and the far edge ceils, so every
        # pixel the caller named is inside the capture: y=20.9 starts the
        # row that contains 20.9, and the bottom edge at 70.9 is kept by the
        # taller height, not shaved off as int() used to.
        gui = self._backend("grim")
        gui.capture(path=SHOT, region=(10.0, 20.9, 100.0, 50.0))
        self.assertEqual(self.runner.calls[0][2], "10,20 100x51")

    def test_a_malformed_region_is_rejected(self):
        gui = self._backend("grim")
        for bad in ("0,0 100x100", (1, 2, 3), (1, 2, 3, 4, 5), ()):
            with self.subTest(region=bad), self.assertRaises(ValueError):
                gui.capture(path=SHOT, region=bad)
        self.assertEqual(self.runner.calls, [])

    def test_an_empty_rectangle_is_rejected(self):
        # ImageMagick's -crop 0x0 quietly yields the *whole* image, so a
        # caller who asked for nothing would get everything.
        gui = self._backend("import")
        for bad in ((0, 0, 0, 10), (0, 0, 10, 0), (0, 0, -5, 5)):
            with self.subTest(region=bad), self.assertRaises(ValueError):
                gui.capture(path=SHOT, region=bad)
        self.assertEqual(self.runner.calls, [])


class TestCheckRegion(unittest.TestCase):
    """The rounding policy itself, pinned where every backend shares it."""

    def test_an_integer_region_passes_through_unchanged(self):
        self.assertEqual(check_region((10, 20, 100, 50)), (10, 20, 100, 50))

    def test_a_fractional_region_is_covered(self):
        # Floor the origin, ceil the far edge: (0.5, 0.5, 1.0, 1.0) spans
        # pixel rows 0-1 and columns 0-1, so covering it is 2x2 at (0, 0).
        self.assertEqual(check_region((0.5, 0.5, 1.0, 1.0)), (0, 0, 2, 2))

    def test_a_negative_fractional_origin_floors_leftward(self):
        # int() rounds toward zero, so x=-0.5 on a monitor left of the
        # primary used to move *right* while every positive x moved left.
        # Flooring moves every origin the same way.
        self.assertEqual(check_region((-0.5, -0.5, 1.0, 1.0)), (-1, -1, 2, 2))

    def test_a_non_finite_region_is_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                check_region((bad, 0, 10, 10))
            with self.subTest(size=bad), self.assertRaises(ValueError):
                check_region((0, 0, bad, 10))

    def test_an_overflowing_far_edge_is_rejected_not_a_bare_overflow(self):
        # Each value is finite on its own, but x + width is infinity, and
        # ceil(inf) would raise a bare OverflowError naming neither the
        # argument nor the caller -- the same hole the NaN check closes.
        with self.assertRaises(ValueError):
            check_region((1e308, 0, 1e308, 10))
        with self.assertRaises(ValueError):
            check_region((0, 1e308, 10, 1e308))

    def test_an_int_beyond_double_range_is_rejected_as_a_value_error(self):
        # float(10**400) raises OverflowError, which the old int() path
        # never did -- it must come out as the same "four numbers" error,
        # not escape bare.
        with self.assertRaises(ValueError):
            check_region((10**400, 0, 10, 10))

    def test_an_origin_beyond_float_precision_is_rejected(self):
        # At 2**60 the float spacing exceeds the width, so x + width == x
        # and the covered size collapses to zero -- which would hand
        # ImageMagick a `-crop 0x...` that quietly yields the whole image.
        with self.assertRaises(ValueError):
            check_region((2**60, 0, 10, 10))
        with self.assertRaises(ValueError):
            check_region((0, 2**60, 10, 10))


class TestFitRegion(unittest.TestCase):
    """The shared bounds-fit every capture path applies: see fit_region."""

    def test_a_region_inside_passes_through_unchanged(self):
        self.assertEqual(fit_region((10, 20, 50, 30), (200, 100)), (10, 20, 50, 30))

    def test_one_pixel_of_overhang_is_fitted_per_side(self):
        self.assertEqual(fit_region((-1, 0, 50, 50), (200, 100)), (0, 0, 49, 50))
        self.assertEqual(fit_region((0, -1, 50, 50), (200, 100)), (0, 0, 50, 49))
        self.assertEqual(fit_region((151, 0, 50, 50), (200, 100)), (151, 0, 49, 50))
        self.assertEqual(fit_region((0, 51, 50, 50), (200, 100)), (0, 51, 50, 49))

    def test_two_pixels_of_overhang_is_refused(self):
        for region in ((-2, 0, 50, 50), (0, 0, 202, 50), (0, 0, 50, 102)):
            with self.subTest(region=region), self.assertRaises(ValueError):
                fit_region(region, (200, 100))

    def test_a_region_wholly_outside_is_refused(self):
        with self.assertRaises(ValueError):
            fit_region((300, 0, 50, 50), (200, 100))

    def test_the_error_names_both_rectangles(self):
        with self.assertRaises(ValueError) as caught:
            fit_region((300, 0, 50, 50), (200, 100), "drawable")
        self.assertIn("200x100", str(caught.exception))
        self.assertIn("drawable", str(caught.exception))


class TestCropFallback(unittest.TestCase):
    """gnome-screenshot and spectacle: capture everything, then crop.

    Neither has an exact-rectangle flag -- their -a/-r selectors are
    interactive and would hang an unattended run, which is why a region used
    to be refused outright on GNOME and KDE. Capturing the screen and
    cutting the rectangle out with ImageMagick gives them the same contract
    as grim.
    """

    def _backend(self, name):
        self.runner = Recorder()
        return ToolCaptureBackend(BY_NAME[name], runner=self.runner)

    def test_gnome_screenshot_captures_then_crops(self):
        gui = self._backend("gnome-screenshot")
        path = gui.capture(path=SHOT, region=REGION)

        shot, crop = self.runner.calls
        self.assertEqual(shot[0], "gnome-screenshot")
        # The whole-screen shot goes to a temporary file, NOT to the path
        # the caller asked a rectangle for -- otherwise a failing crop
        # leaves a full-screen image sitting under a name that promises a
        # rectangle.
        self.assertNotEqual(shot[2], SHOT)

        self.assertIn(crop[0], ("magick", "convert"))
        self.assertEqual(crop[1], shot[2])
        self.assertEqual(crop[2:5], ["-crop", "100x50+10+20", "+repage"])
        self.assertEqual(crop[5], SHOT)
        self.assertEqual(path, SHOT)

    def test_spectacle_captures_then_crops(self):
        gui = self._backend("spectacle")
        gui.capture(path=SHOT, region=REGION)
        shot, crop = self.runner.calls
        self.assertEqual(shot[0], "spectacle")
        # -f is spectacle's fullscreen mode; -r would open its selector.
        self.assertIn("-f", shot)
        self.assertNotIn("-r", shot)
        self.assertEqual(crop[2:4], ["-crop", "100x50+10+20"])

    def test_no_region_takes_the_direct_path_with_no_crop(self):
        gui = self._backend("gnome-screenshot")
        gui.capture(path=SHOT)
        self.assertEqual(len(self.runner.calls), 1)

    def test_which_tools_crop_is_declared(self):
        self.assertTrue(self._backend("gnome-screenshot").crops_regions)
        self.assertTrue(self._backend("spectacle").crops_regions)
        self.assertFalse(self._backend("grim").crops_regions)
        self.assertFalse(self._backend("import").crops_regions)

    def test_the_intermediate_screenshot_is_cleaned_up(self):
        # It is a real file: mkstemp creates it. Leaving one per capture
        # behind would fill /tmp over a long unattended run.
        created = []

        def runner(argv):
            created.append(argv)
            _write_fake_image(argv)
            return argv

        gui = ToolCaptureBackend(BY_NAME["gnome-screenshot"], runner=runner)
        gui.capture(path=SHOT, region=REGION)
        intermediate = created[0][2]
        self.assertFalse(os.path.exists(intermediate))

    def test_the_intermediate_is_cleaned_up_even_when_the_crop_fails(self):
        # The finally: clause, which is the whole reason the whole-screen
        # shot goes to a temporary file rather than straight to `path`.
        seen = []

        def runner(argv):
            seen.append(argv)
            if argv[0] in ("magick", "convert"):
                raise PyGUITestError("crop blew up")
            _write_fake_image(argv)
            return argv

        gui = ToolCaptureBackend(BY_NAME["gnome-screenshot"], runner=runner)
        with self.assertRaises(PyGUITestError):
            gui.capture(path=SHOT, region=REGION)
        intermediate = seen[0][2]
        self.assertFalse(os.path.exists(intermediate))


class TestCropRefusesARegionTheScreenshotDoesNotContain(unittest.TestCase):
    """ImageMagick answers an overhanging crop with a smaller image.

    Measured on 7.1.2: `-crop 50x50+150+5` of a 200x100 image is 50x50, but
    `+-10+5` is 40x50 and `+300+5` is a 1x1 image with a warning and exit 0.
    Each is a plausible picture of the wrong thing, so the crop path refuses
    them first, naming the screenshot's size.
    """

    SCREEN = (200, 100)

    def _backend(self):
        self.calls = []

        def runner(argv):
            self.calls.append(argv)
            if argv[0] == "gnome-screenshot":
                write_rgb(
                    argv[2], *self.SCREEN, (bytes(3 * self.SCREEN[0]),) * self.SCREEN[1]
                )
            else:
                _write_fake_image(argv)
            return argv

        return ToolCaptureBackend(BY_NAME["gnome-screenshot"], runner=runner)

    def _refused(self, region):
        gui = self._backend()
        with self.assertRaises(ValueError) as caught:
            gui.capture(path=SHOT, region=region)
        self.assertIn("200x100", str(caught.exception))
        self.assertEqual(len(self.calls), 1, "the crop must not have run")

    def test_a_region_hanging_off_the_right_edge_is_refused(self):
        self._refused((160, 10, 50, 50))

    def test_a_region_hanging_off_the_bottom_is_refused(self):
        self._refused((10, 60, 50, 50))

    def test_a_region_wholly_outside_is_refused(self):
        self._refused((300, 5, 50, 50))

    def test_a_negative_origin_is_refused(self):
        self._refused((-10, 5, 50, 50))
        self._refused((5, -10, 50, 50))

    def test_a_region_filling_the_screenshot_exactly_is_cropped(self):
        gui = self._backend()
        gui.capture(path=SHOT, region=(0, 0, 200, 100))
        self.assertEqual(len(self.calls), 2)

    def test_one_pixel_of_overhang_is_clamped_not_refused(self):
        # check_region covers fractional coordinates outward, so an element
        # flush against the screen edge on a fractional-scale display
        # legitimately produces a region one pixel past it -- one pixel is
        # exactly as much as covering can add, so exactly that much is
        # fitted back instead of failing the capture it was computed for.
        gui = self._backend()
        gui.capture(path=SHOT, region=(160, 10, 41, 50))  # right edge at 201
        self.assertEqual(len(self.calls), 2, "the crop must have run")
        self.assertIn("40x50+160+10", self.calls[1])

    def test_a_fractional_region_flush_with_the_edge_is_captured(self):
        # End to end through check_region: the covering ceils 160.5 + 39.9
        # = 200.4 to 201, one past the 200-wide screen, and the clamp hands
        # the crop the visible 40 columns -- the exact HiDPI-edge case the
        # covering exists to serve, which the old int() truncation served
        # only by silently shaving every other fractional region.
        gui = self._backend()
        gui.capture(path=SHOT, region=(160.5, 10.0, 39.9, 50.0))
        self.assertEqual(len(self.calls), 2)
        self.assertIn("40x50+160+10", self.calls[1])

    def test_a_single_negative_pixel_of_origin_is_clamped(self):
        # floor(-0.4) is -1: an element poking a fraction off the left edge
        # covers to origin -1, and the visible part starts at 0.
        gui = self._backend()
        gui.capture(path=SHOT, region=(-1, 5, 50, 50))
        self.assertEqual(len(self.calls), 2)
        self.assertIn("49x50+0+5", self.calls[1])

    def test_two_pixels_of_overhang_is_still_refused(self):
        # Two pixels cannot be covering -- it adds at most one per side --
        # so past one pixel the region is a caller error, refused as before.
        self._refused((160, 10, 42, 50))

    def test_the_intermediate_is_cleaned_up_after_a_refusal(self):
        gui = self._backend()
        with self.assertRaises(ValueError):
            gui.capture(path=SHOT, region=(300, 5, 50, 50))
        self.assertFalse(os.path.exists(self.calls[0][2]))


class TestFailuresAreActionable(unittest.TestCase):
    """A hung tool must say it is hung, and say nothing more than that.

    Live on GNOME Shell 50.4, `gnome-screenshot -f` took the session's
    capture duty and never returned. Two things had to be true of the
    message. It must distinguish "installed but unresponsive" from
    "missing", because `pyguitest doctor` reports the tool as present and
    nothing else flags it.

    And it must NOT carry advice about what to try instead. This string
    gets embedded in a composite's fallback warning and again, once per
    member, in its "everything failed" summary -- an advice paragraph here
    appeared three times in one traceback, recommending backends that the
    same traceback reported failing two lines further down. Guidance
    belongs where the whole picture is known.
    """

    def test_a_timeout_says_installed_but_unresponsive(self):
        gui = ToolCaptureBackend(BY_NAME["gnome-screenshot"], runner=None)
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                "gnome-screenshot", _SUBPROCESS_TIMEOUT
            ),
        ):
            with self.assertRaises(PyGUITestError) as caught:
                gui.capture(path=SHOT)
        message = str(caught.exception)
        self.assertIn("gnome-screenshot", message)
        self.assertIn("did not finish within 15s", message)
        self.assertIn("installed but not responding", message)

    def test_a_timeout_does_not_recommend_other_backends(self):
        gui = ToolCaptureBackend(BY_NAME["gnome-screenshot"], runner=None)
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                "gnome-screenshot", _SUBPROCESS_TIMEOUT
            ),
        ):
            with self.assertRaises(PyGUITestError) as caught:
                gui.capture(path=SHOT)
        message = str(caught.exception)
        self.assertNotIn("backend=", message)
        # Short enough to read inside a warning that quotes it -- measured
        # without the destination path, which the caller supplies and which no
        # wording here can bound: the same message was 226 characters on
        # Windows purely because its temp directory is longer than /tmp.
        self.assertLess(len(message) - len(SHOT), 200)

    def test_a_nonzero_exit_still_reports_the_tools_own_stderr(self):
        # Distinct from a hang: the tool answered, and what it said is
        # the most useful thing to show.
        gui = ToolCaptureBackend(BY_NAME["grim"], runner=None)
        with mock.patch(
            "subprocess.run",
            return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="compositor does not support wlr"
            ),
        ):
            with self.assertRaises(PyGUITestError) as caught:
                gui.capture(path=SHOT)
        self.assertIn("compositor does not support wlr", str(caught.exception))


class TestASilentEmptyResultIsCaught(unittest.TestCase):
    """A 0 exit code is not proof of a screenshot.

    Confirmed live on KDE Plasma 6: `spectacle -b -n -f -o path` exited 0
    and left `path` at 0 bytes, with nothing on stderr, four times in a row
    before a fifth attempt produced a real image. Nothing about the tool's
    own reporting distinguished that from success, so it has to be checked
    here instead of trusted.
    """

    def test_an_empty_file_is_an_actionable_error_not_a_silent_success(self):
        def leaves_an_empty_file(argv):
            open(argv[-1], "wb").close()
            return argv

        gui = ToolCaptureBackend(BY_NAME["grim"], runner=leaves_an_empty_file)
        with self.assertRaises(PyGUITestError) as caught:
            gui.capture(path=SHOT)
        self.assertIn("grim", str(caught.exception))
        self.assertIn("empty", str(caught.exception))

    def test_a_missing_file_is_the_same_actionable_error(self):
        gui = ToolCaptureBackend(BY_NAME["grim"], runner=lambda argv: argv)
        with self.assertRaises(PyGUITestError):
            gui.capture(path=NEVER_WRITTEN)

    def test_an_empty_intermediate_is_caught_before_the_crop_even_runs(self):
        seen = []

        def leaves_an_empty_file(argv):
            seen.append(argv)
            open(argv[-1], "wb").close()
            return argv

        gui = ToolCaptureBackend(BY_NAME["spectacle"], runner=leaves_an_empty_file)
        with self.assertRaises(PyGUITestError):
            gui.capture(path=SHOT, region=REGION)
        # Only the screenshot ran; the crop step never got a chance to.
        self.assertEqual(len(seen), 1)


class TestTheMacWindowRoute(unittest.TestCase):
    """`capture_window_id`: the argv, the grant and the write check, one place.

    macOS's per-window grab, and the caller `screencapture_argv`'s window id
    existed for. Driven here rather than through `MacosBackend` so the pieces stay
    testable on a machine that is not a Mac: that backend's wiring has its own
    tests in `tests/test_macos_backend.py`, and this is what the wiring calls.
    """

    def grant_screen_recording(self, allowed=True):
        """Patch the TCC preflight, which on anything but a Mac answers no."""
        patched = mock.patch(
            "pyguitest.backends._macapi.screen_recording_allowed",
            return_value=allowed,
        )
        patched.start()
        self.addCleanup(patched.stop)

    def test_the_command_line_addresses_one_window_by_its_number(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            _write_fake_image(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.grant_screen_recording()
        with mock.patch("subprocess.run", run):
            written = capture_window_id(265, SHOT)
        self.assertEqual(written, SHOT)
        # The number arrives as an int from a `Window` handle and leaves as a
        # string: argv is a list of strings, and `-l` wants the number. `-o`
        # is in front of it because the drop shadow would otherwise pad the
        # image past the rectangle `geometry()` reports (measured live; see
        # `screencapture_argv`).
        self.assertEqual(seen, [["screencapture", "-x", "-o", "-l", "265", SHOT]])

    def test_no_path_given_allocates_a_png_of_its_own(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            _write_fake_image(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.grant_screen_recording()
        with mock.patch("subprocess.run", run):
            written = capture_window_id(48)
        self.addCleanup(lambda: os.path.exists(written) and os.unlink(written))
        # The same convention the tool backends use: a temporary file whose suffix
        # matches the format the tool writes.
        self.assertTrue(written.endswith(".png"))
        self.assertEqual(seen, [["screencapture", "-x", "-o", "-l", "48", written]])

    def test_a_denied_grant_refuses_before_the_command_runs(self):
        self.grant_screen_recording(allowed=False)
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(PermissionRequired) as caught:
                capture_window_id(48, NEVER_WRITTEN)
        self.assertIn("Screen Recording", str(caught.exception))
        # The gate's whole point: a black image is never produced, so the tool is
        # never spawned and nothing is written.
        run.assert_not_called()

    def test_an_empty_result_is_refused_with_the_tool_named(self):
        def nothing(argv, **kwargs):
            open(argv[-1], "wb").close()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.grant_screen_recording()
        with mock.patch("subprocess.run", nothing):
            with self.assertRaises(PyGUITestError) as caught:
                capture_window_id(48, SHOT)
        self.assertIn("screencapture", str(caught.exception))
        self.assertIn("empty", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
