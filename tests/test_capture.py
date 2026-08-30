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
import unittest
from types import SimpleNamespace
from unittest import mock

from pyguitest import tools
from pyguitest.backends.capture import _SUBPROCESS_TIMEOUT, ToolCaptureBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, PyGUITestError

BY_NAME = {t.name: t for t in tools.CAPTURE_TOOLS}

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
        path = gui.capture(path="/tmp/shot.png")
        self.assertEqual(self.runner.calls[0], ["grim", "/tmp/shot.png"])
        self.assertEqual(path, "/tmp/shot.png")

    def test_gnome_screenshot_flag_order(self):
        gui = self._backend("gnome-screenshot")
        gui.capture(path="/tmp/shot.png")
        self.assertEqual(
            self.runner.calls[0], ["gnome-screenshot", "-f", "/tmp/shot.png"]
        )

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
            gui.capture(window="some-window-handle", path="/tmp/shot.png")
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
        gui.capture(path="/tmp/shot.png", region=REGION)
        self.assertEqual(
            self.runner.calls[0],
            ["grim", "-g", "10,20 100x50", "/tmp/shot.png"],
        )

    def test_import_gets_imagemagick_syntax(self):
        gui = self._backend("import")
        gui.capture(path="/tmp/shot.png", region=REGION)
        self.assertEqual(
            self.runner.calls[0],
            [
                "import",
                "-window",
                "root",
                "-crop",
                "100x50+10+20",
                "/tmp/shot.png",
            ],
        )

    def test_floats_are_accepted_and_truncated(self):
        # geometry() is documented as returning ints, but a caller computing
        # a rectangle (a midpoint, a scale factor) easily produces floats,
        # and every tool's command line needs integers.
        gui = self._backend("grim")
        gui.capture(path="/tmp/shot.png", region=(10.0, 20.9, 100.0, 50.0))
        self.assertEqual(self.runner.calls[0][2], "10,20 100x50")

    def test_a_malformed_region_is_rejected(self):
        gui = self._backend("grim")
        for bad in ("0,0 100x100", (1, 2, 3), (1, 2, 3, 4, 5), ()):
            with self.subTest(region=bad), self.assertRaises(ValueError):
                gui.capture(path="/tmp/shot.png", region=bad)
        self.assertEqual(self.runner.calls, [])

    def test_an_empty_rectangle_is_rejected(self):
        # ImageMagick's -crop 0x0 quietly yields the *whole* image, so a
        # caller who asked for nothing would get everything.
        gui = self._backend("import")
        for bad in ((0, 0, 0, 10), (0, 0, 10, 0), (0, 0, -5, 5)):
            with self.subTest(region=bad), self.assertRaises(ValueError):
                gui.capture(path="/tmp/shot.png", region=bad)
        self.assertEqual(self.runner.calls, [])


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
        path = gui.capture(path="/tmp/shot.png", region=REGION)

        shot, crop = self.runner.calls
        self.assertEqual(shot[0], "gnome-screenshot")
        # The whole-screen shot goes to a temporary file, NOT to the path
        # the caller asked a rectangle for -- otherwise a failing crop
        # leaves a full-screen image sitting under a name that promises a
        # rectangle.
        self.assertNotEqual(shot[2], "/tmp/shot.png")

        self.assertIn(crop[0], ("magick", "convert"))
        self.assertEqual(crop[1], shot[2])
        self.assertEqual(crop[2:5], ["-crop", "100x50+10+20", "+repage"])
        self.assertEqual(crop[5], "/tmp/shot.png")
        self.assertEqual(path, "/tmp/shot.png")

    def test_spectacle_captures_then_crops(self):
        gui = self._backend("spectacle")
        gui.capture(path="/tmp/shot.png", region=REGION)
        shot, crop = self.runner.calls
        self.assertEqual(shot[0], "spectacle")
        # -f is spectacle's fullscreen mode; -r would open its selector.
        self.assertIn("-f", shot)
        self.assertNotIn("-r", shot)
        self.assertEqual(crop[2:4], ["-crop", "100x50+10+20"])

    def test_no_region_takes_the_direct_path_with_no_crop(self):
        gui = self._backend("gnome-screenshot")
        gui.capture(path="/tmp/shot.png")
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
        gui.capture(path="/tmp/shot.png", region=REGION)
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
            gui.capture(path="/tmp/shot.png", region=REGION)
        intermediate = seen[0][2]
        self.assertFalse(os.path.exists(intermediate))


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
                gui.capture(path="/tmp/shot.png")
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
                gui.capture(path="/tmp/shot.png")
        message = str(caught.exception)
        self.assertNotIn("backend=", message)
        # Short enough to read inside a warning that quotes it.
        self.assertLess(len(message), 200)

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
                gui.capture(path="/tmp/shot.png")
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
            gui.capture(path="/tmp/shot.png")
        self.assertIn("grim", str(caught.exception))
        self.assertIn("empty", str(caught.exception))

    def test_a_missing_file_is_the_same_actionable_error(self):
        gui = ToolCaptureBackend(BY_NAME["grim"], runner=lambda argv: argv)
        with self.assertRaises(PyGUITestError):
            gui.capture(path="/tmp/pyguitest-never-written.png")

    def test_an_empty_intermediate_is_caught_before_the_crop_even_runs(self):
        seen = []

        def leaves_an_empty_file(argv):
            seen.append(argv)
            open(argv[-1], "wb").close()
            return argv

        gui = ToolCaptureBackend(BY_NAME["spectacle"], runner=leaves_an_empty_file)
        with self.assertRaises(PyGUITestError):
            gui.capture(path="/tmp/shot.png", region=REGION)
        # Only the screenshot ran; the crop step never got a chance to.
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
