"""Screen capture, adapting the desktop's own screenshot tool.

Worth stating plainly: X11::GUITest has no screenshot function. Nothing in its
50 exports captures pixels, so this is a new feature rather than a port -- the
planning note's compatibility table listed it in error.

Each desktop ships a capture tool that already handles its own portal or
protocol negotiation, so this writes to a file and returns the path. No image
library is pulled in; callers can hand the path to Pillow if they want pixels.

macOS is the one platform here whose tool comes with the OS rather than with a
desktop, and the one tool whose failure under a missing permission is silent
-- black pixels and exit 0 -- so it is refused before it runs; see
`require_screen_recording`.

Regions are normalized here rather than in the caller. The tools disagree --
grim wants "x,y WxH", ImageMagick wants "WxH+X+Y", and gnome-screenshot and
spectacle have no exact-rectangle syntax at all, only an interactive selector
that would hang an unattended run. Callers pass the same `(x, y, width,
height)` tuple `geometry()` returns and this picks the mechanism: the tool's
own flag where one exists, and otherwise a whole-screen capture cropped down
with ImageMagick.
"""

import os
import subprocess
import sys
import tempfile

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, PermissionRequired, PyGUITestError
from . import _macapi
from . import crop as _crop
from .base import GUIBackend, check_region

__all__ = ["ToolCaptureBackend"]

_SUBPROCESS_TIMEOUT = 15

# Tools with no exact-rectangle mode. Their region flags (-a, -r) open an
# interactive selector, so a region here is served by capturing the whole
# screen and cropping instead of by anything on their command line.
_NO_REGION_FLAG = {"gnome-screenshot", "spectacle"}

# Whole-screen capture to a named file, plus each tool's own region syntax
# where it has one. `region` arrives as a validated (x, y, width, height).
_COMMANDS = {
    "grim": lambda path, region: (
        ["grim", "-g", "{},{} {}x{}".format(*region), path]
        if region
        else ["grim", path]
    ),
    "gnome-screenshot": lambda path, region: ["gnome-screenshot", "-f", path],
    "spectacle": lambda path, region: ["spectacle", "-b", "-n", "-f", "-o", path],
    "import": lambda path, region: (
        ["import", "-window", "root", "-crop", "{2}x{3}+{0}+{1}".format(*region), path]
        if region
        else ["import", "-window", "root", path]
    ),
    # macOS, and the one entry here that ships with the platform rather than
    # with a desktop. Its argv is built by `screencapture_argv` below rather
    # than inline, because a second caller wants a window id rather than a
    # region: `capture_window_id`, which MacosBackend drives for the native,
    # un-occluded window capture. A *tool backend* still cannot be handed a
    # window -- the class below turns one into a region, never a handle --
    # so that route lives at module level beside the argv builder instead.
    "screencapture": lambda path, region: screencapture_argv(path, region),
}


def screencapture_argv(path, region=None, window_id=None):
    """The `screencapture` command line that writes `path`.

    One builder for two callers: the table above, and `capture_window_id`,
    which MacosBackend drives with the CGWindowID `macos.py`'s window join
    reads off each window. One builder rather than two because two would be
    two places for the rectangle syntax to be wrong.

    `-x` is not decoration. Without it the tool plays the camera shutter
    sound; an unattended suite should not be doing that to the machine it
    runs on.

    `-o` is the other flag that earns its place, and the live run on macOS
    26.7 is what put it there. A window capture carries the drop shadow as
    padding unless the tool is told not to: the same window measured 388x340
    without `-o` against a `geometry()` of 320x272 -- 68 pixels more in each
    direction, black in the corners -- and exactly 320x272 with it. Both are
    honest pictures of the window; only one is the rectangle `geometry()`
    reports, which is the rectangle the Windows route sizes from too, so a
    caller who measures a window and then screenshots it gets that size back
    on either platform.
    """
    argv = ["screencapture", "-x"]
    if window_id is not None:
        argv += ["-o", "-l", str(window_id)]
    elif region is not None:
        argv += ["-R", "{},{},{},{}".format(*region)]
    return argv + [path]


def capture_window_id(window_id, path=None):
    """Write a screenshot of one native window, and return the path.

    macOS's per-window route, and the reason `screencapture_argv` takes a
    window id at all: `-l` asks the window server for one window's own
    pixels, so anything stacked on top of it stays out of the image and a
    window hanging off the edge of a display comes back whole -- the same
    kind of grab `PrintWindow` is on Windows and `GetImage` is on X11.

    Called by `MacosBackend.capture`, because that backend is what issues
    the CGWindowIDs: a `Window`'s handle is backend-private, and the
    composite only passes one to a member that issued it. `window_id` here
    is therefore the raw number, already unwrapped.

    The Screen Recording grant is checked **before** the tool runs, through
    the same `require_screen_recording` the whole-screen route uses, and
    for the same reason -- a denied grant is exit 0 and a black image, so
    a route that ran first and inspected pixels after would have nothing to
    inspect. The file is then checked for content, because exit 0 alone has
    already been measured to mean nothing here (see `_check_written`).

    What `-l` does with a window that has closed since its id was read is
    neither asserted nor detectable, and the live run measured why. The tool
    exits **0** and writes a *black* image of the shadow-padded size -- 2976
    bytes of it on the run that closed the window under its own id -- so
    `_check_written`'s non-empty test passes and the caller gets a picture of
    nothing. An id that never named a window is the other case: exit 1,
    `could not create image from window`, no file at all, which `_run_tool`
    raises on. The two cannot be told apart from the file, and a stale id
    cannot be told from a window that is genuinely black, so this is written
    down rather than checked for. Read the window's geometry again before
    capturing if a window may have closed.
    """
    require_screen_recording()
    if path is None:
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
    _run_tool(screencapture_argv(path, window_id=window_id))
    _check_written(path, "screencapture")
    return path


def require_screen_recording() -> None:
    """Raise `PermissionRequired` unless Screen Recording has been granted.

    Checked *before* the tool runs rather than by inspecting what it wrote:
    a uniformly black PNG is what a denied Screen Recording grant produces,
    so this refuses exactly what ADR 004's "detect the black image" refuses
    -- but it needs no PNG decoder (`png.py` encodes and does not decode),
    it costs one C call instead of a whole screenshot, and it can *say* the
    reason rather than inferring it from pixels.

    The message names the grant, the pane and the interpreter. TCC records
    consent against a binary, so a reader who granted it to their system
    Python has to be able to see which binary this one is; and "Screen
    Recording" is what the System Settings search field matches.
    """
    if _macapi.screen_recording_allowed():
        return
    raise PermissionRequired(
        Capability.SCREEN_CAPTURE,
        "screencapture",
        "Screen Recording is not granted to this process, so the capture "
        f"would be a uniformly black image; grant it to {sys.executable} "
        "under System Settings > Privacy & Security > Screen Recording",
    )


_PERMISSION_GATES = {"screencapture": require_screen_recording}
"""Tools that fail *successfully* when a permission is missing.

The others fail loudly -- grim exits non-zero with an error, and the one tool
whose exit code means nothing is caught by `_check_written`. `screencapture`
is the exception, and the worst of both: exit 0, no stderr, black image. It
is refused before it runs instead. Keyed by name and consulted in `capture()`,
which is where a caller finds out.
"""


def _run_tool(argv):
    """Run one capture tool's command line, raising if it fails or hangs.

    Module level rather than a method because there are two callers: the
    `ToolCaptureBackend` below, and `capture_window_id` above, which
    MacosBackend drives for a per-window grab. One implementation of the
    timeout and the two failure messages rather than a second copy of them
    in macos.py, drifting from this one.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # Deliberately short, and deliberately free of advice about
        # what to try instead. This message gets embedded in a
        # composite's fallback warning and again in its "everything
        # failed" summary, once per member -- an advice paragraph
        # here becomes three copies of itself in a single traceback,
        # and worse, it recommends backends the composite has already
        # tried and reported failing two lines further down. Guidance
        # belongs where the whole picture is known: composite._grab.
        #
        # The fact worth stating is that the tool is installed and
        # unresponsive, which is a different condition from missing
        # and needs a different fix.
        raise PyGUITestError(
            f"{' '.join(argv)} did not finish within "
            f"{_SUBPROCESS_TIMEOUT}s; it is installed but not "
            "responding (these tools front for a desktop screenshot "
            "service and can hang on it rather than failing)"
        ) from exc
    if result.returncode != 0:
        raise PyGUITestError(
            f"{' '.join(argv)} failed ({result.returncode}): "
            f"{result.stderr.strip() or 'no output'}"
        )
    return result


def _check_written(path, tool_name):
    """Raise unless `path` holds a real image, not just a name.

    A tool's exit code is not proof of a screenshot: confirmed live on
    KDE Plasma 6, `spectacle -b -n -f -o path` exited 0 and left `path`
    at 0 bytes, silently, with nothing on stderr -- four times in a
    row before a fifth attempt produced a real image, with no code or
    environment change in between. Whatever KWin's screenshot service
    was doing, spectacle itself gave no sign anything had failed.
    Checked here, once, rather than trusted through to a caller that
    would otherwise get a path to a corrupt image back from a call
    that claimed to succeed.

    `tool_name` is passed rather than read off a backend so that
    `capture_window_id` can use this without building one, and so the
    message names the tool that actually ran.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise PyGUITestError(
            f"{tool_name} exited successfully but left {path!r} "
            "empty -- the desktop's screenshot service silently failed; "
            "try again"
        )


class ToolCaptureBackend(GUIBackend):
    """Capture through whichever screenshot tool is installed."""

    def __init__(self, tool, runner=None):
        """Drive `tool`, optionally through an injected `runner`."""
        if tool.name not in _COMMANDS:
            raise PyGUITestError(f"no capture command for {tool.name!r}")
        self.tool = tool
        self._build = _COMMANDS[tool.name]
        self._runner = runner or self._run
        # None for every tool whose failure is loud enough to need no gate --
        # see _PERMISSION_GATES, which is one entry long today.
        self._gate = _PERMISSION_GATES.get(tool.name)

    # A read-only override of GUIBackend's plain, writable `name` attribute
    # -- see the same note in input.py. Nothing assigns to it externally.
    @property
    def name(self) -> str:  # type: ignore[override]
        """Identifier for this backend, e.g. 'capture:grim'."""
        return f"capture:{self.tool.name}"

    @property
    def capabilities(self):
        """Screen capture only.

        Not WINDOW_CAPTURE: a tool that captures pixels still cannot turn a
        Window into a rectangle. In a composite the geometry member supplies
        that, and this is handed the resulting region -- see
        CompositeBackend.capture.
        """
        return CapabilitySet({Capability.SCREEN_CAPTURE})

    @property
    def crops_regions(self):
        """Whether a region here costs a whole-screen capture plus a crop."""
        return self.tool.name in _NO_REGION_FLAG

    def _run(self, argv):
        """Run `argv`, raising if the tool reports failure or hangs.

        The implementation is module level -- see `_run_tool` above -- so the
        per-window route in macos.py runs the same tool under the same
        timeout and the same two messages without a second copy of either.
        """
        return _run_tool(argv)

    def capture(self, window=None, path=None, region=None):
        """Write a screenshot and return its path.

        `region` is the normalized `(x, y, width, height)` in screen
        coordinates that every backend here takes; the tool's own syntax is
        built from it. Where the tool has no exact-rectangle mode this
        captures the whole screen and crops the rectangle out with
        ImageMagick rather than falling through to an interactive selector
        -- a prompt nobody unattended is there to click, which is what the
        earlier code did, and it hung until someone did.

        `window` needs geometry this backend has no access to: it cannot
        resolve a Window to a rectangle on its own. Passed here, it raises
        rather than silently capturing the whole screen and returning a
        plausible-looking image of the wrong thing. A composite that also
        has a WINDOW_GEOMETRY member resolves it before this is reached, so
        the refusal only surfaces for a bare capture backend.
        """
        self.require(Capability.SCREEN_CAPTURE)
        if self._gate is not None:
            # Before the window check below, and before anything is written:
            # a tool gated on a permission fails *successfully*, so there is
            # nothing after this point that could tell it had.
            self._gate()
        if window is not None:
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                "per-window capture needs WINDOW_GEOMETRY to resolve a "
                "rectangle, which this backend does not have; compose it "
                "with a backend that provides one",
            )
        region = check_region(region, window)
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        if region is not None and self.crops_regions:
            return self._capture_then_crop(path, region)
        self._runner(self._build(path, region))
        _check_written(path, self.tool.name)
        return path

    def _capture_then_crop(self, path, region):
        """Whole-screen capture into a temporary file, cropped onto `path`.

        The full screenshot goes to a temporary file rather than to `path`
        itself so a failed crop cannot leave a whole-screen image sitting at
        the path the caller asked for a rectangle at -- again, the wrong
        image under a plausible name.
        """
        descriptor, full = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        try:
            self._runner(self._build(full, None))
            _check_written(full, self.tool.name)
            _crop.crop(full, region, path, runner=self._runner)
            _check_written(path, self.tool.name)
        finally:
            if os.path.exists(full):
                os.unlink(full)
        return path
