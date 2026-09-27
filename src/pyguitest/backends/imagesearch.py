"""Locate a template image inside a screenshot, via ImageMagick's compare.

`compare -subimage-search` always reports its best-match location even when
the two images overall differ -- which they normally do, since a screenshot
is rarely the exact size of the button being searched for -- so exit code 1
is the *expected* outcome here, not a failure, unlike every other tool this
package shells out to (`capture.py`'s `_run` treats any nonzero exit as
failure; this module cannot reuse that).

Getting the template's own width/height needs a separate `identify` call: no
image library is introduced here, matching capture.py's own "hand the caller
a path, not pixels" stance. Both `compare` and `identify` ship in the same
ImageMagick package as `import` (already relied on for capture), so presence
of `compare` on PATH is trusted as a proxy for `identify` too -- the same
trust-the-binary-implies-the-package pattern tools.py already uses for
`import`.

ImageMagick 7 offers two ways to reach those two operations, and which one a
machine has is not something this module can assume: the legacy commands
`compare`/`identify` are a separate installer option, and the Windows
package -- `winget install ImageMagick.ImageMagick`, the command the docs
give -- lays down `magick.exe` and nothing else, measured on Windows 11 build
26200 with ImageMagick 7.1.2-31 Q16-HDRI, whose install directory contains no
`compare.exe` and no `identify.exe`. So `magick compare ...` and `magick
identify ...` are driven as first-class too: `tools.IMAGE_TOOLS` carries both
entry points and `_search_argv` picks the shape from the tool that was found.
Before that, a stock Windows install had the package *present* -- and
`crop.py` using it happily -- while `IMAGE_LOCATE` was reported missing
because `compare` was not on PATH.

Verified live against ImageMagick 7.1.2-27 Q16-HDRI (2026-08-26) rather
than written from documentation alone. Confirmed by running the exact argv
this module builds:

- exit codes are 0 = identical, 1 = differ (the normal subimage-search
  outcome), 2 = a real error -- so `allowed_returncodes=(0, 1)` is right;
- `identify -format "%w %h"` returns e.g. `20 14` on stdout;
- `-crop WxH+X+Y +repage` produces exactly the requested rectangle, and
  adding the crop offset back onto compare's match reproduces the
  uncropped coordinates;
- compare's own build reports `fftw` among its delegates with HDRI, so the
  FFT-accelerated search path is available here.

The same argv was then run against the Windows install described above, which
is the case that changed this module. Its build has **no** `fftw` delegate --
`magick -list delegate` lists none -- so every search takes the slow path, at
a cost that scales with the *search area* rather than with the template:
480x270 took 5.07s, 800x600 took 9.15s, 960x540 took 15.08s and 1920x1080
took 51.35s, i.e. roughly 20-40us per pixel of haystack. Against the flat 15s
`_SUBPROCESS_TIMEOUT` that made a full-screen search on a 1080p desktop fail
outright -- on the one platform where no distro build hands you the FFT
delegate, and where `within=` cannot help, since the desktop is what gets
searched. Hence `_search_timeout`: the budget is derived from the area
actually being searched, floor 15s, ceiling `_MAX_SUBPROCESS_TIMEOUT`.

That exercise found a real defect, now fixed: ImageMagick formats scores
with `%g`, which switches to exponent notation below about 1e-4 -- reachable
for a near-exact match -- and the original parser both failed on
`1.2e-06` and, worse, silently read the absolute score of `6.55e+04` as
`04`. See `_NUMBER`.

That exercise found a second defect, on macOS, and the worst kind this module
can have: **a haystack and a template whose channel sets differ make compare
answer with the wrong offset rather than with an error.** `screencapture`
writes PNGs with an alpha channel, so every haystack this backend is handed on
a Mac is RGBA, while a template cut out by any other tool comes back RGB -- a
crop of a fully opaque image loses its alpha in the PNG encoder, which then
palettises it. Measured on macOS 26 with ImageMagick 7.1.2-31 Q16-HDRI, a
400x300 crop of the real screen searched for a 200x60 crop of itself taken at
+100+80:

    RGBA haystack, RGBA template    0 (0) @ 100,80            found
    RGBA haystack, RGB template     15423.5 (0.235347) @ 0,0  wrong, exit 1
    RGB haystack,  RGBA template    0 (0) @ 100,80            found
    RGB haystack,  RGB template     0 (0) @ 100,80            found

Synthetic noise behaved the same way, so it is the channel sets rather than the
content, and it is not a threshold artifact: `-dissimilarity-threshold 1.0` did
not change it, and `-channel RGB` located the template in all four combinations
of the same images. `locate_image()` reporting "found it at (0,0), score
0.235" for a template that is at (100,80) -- the top-left corner of whatever was
searched, which is what compare falls back to when it finds nothing -- is
precisely the failure a caller cannot detect, so the search argv carries
`-channel RGB`: a match is made on colour, and alpha is not part of one.

Only RMSE is trusted. Live testing found NCC reporting a *different and
wrong* offset for images RMSE located correctly, and PHASE failing to
finish within two minutes on a 200x120 haystack, far beyond
`_SUBPROCESS_TIMEOUT`. `metric` remains caller-selectable, but anything
other than the default is unverified.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from ..capabilities import Capability, CapabilitySet
from ..errors import PyGUITestError
from . import crop as _crop
from .base import GUIBackend, ImageMatch

__all__ = ["ToolImageSearchBackend"]

_SUBPROCESS_TIMEOUT = 15.0
"""Floor on how long one tool call may run, in seconds.

Unchanged from before `_search_timeout` existed, and still the budget the
`identify` and crop calls get: both are metadata operations on a single image,
and a build that cannot answer one of those in 15s is not slow, it is stuck.
"""

_MAX_SUBPROCESS_TIMEOUT = 300.0
"""Ceiling on a template search, in seconds.

A 4K desktop with no FFT delegate is ~8.3M pixels of search area, which the
rate below puts at about five and a half minutes -- and the alternative to
waiting is a search that never completes, on exactly the machines whose
ImageMagick has the slow path. A tool that has genuinely hung still gives up
at worst here.
"""

_SECONDS_PER_PIXEL = 40e-6
"""Measured cost of one pixel of search area, in seconds, on a build with no
FFT delegate -- the slow path.

See the module docstring for the four timings this comes from. 40us is the
slowest per-pixel rate among them, so a derived budget errs towards waiting
rather than towards failing a search that was going to finish.
"""

_SUPPORTED = {"compare", "magick"}
"""ImageMagick entry points that can run a subimage search.

`magick` is IM7's dispatcher -- `magick compare ...` where the legacy command
is `compare ...` -- and on Windows it is the only one an install provides.
"""


def _search_timeout(pixels):
    """Seconds to allow for a subimage search over `pixels` pixels.

    Sized from the area *searched* rather than from the template: cost tracks
    the haystack, and a large template does not make the search slower. At or
    below the floor the answer is the floor, which covers every small search
    and so is the whole budget a session that already worked will notice.
    """
    return min(
        _MAX_SUBPROCESS_TIMEOUT,
        max(_SUBPROCESS_TIMEOUT, pixels * _SECONDS_PER_PIXEL),
    )


def _search_argv(tool_name, command, *arguments):
    """The command line for `command`, shaped for the entry point found.

    `compare` and `identify` are commands in their own right; `magick`
    dispatches both as its first argument. The two shapes are not
    interchangeable, and which one gets built is decided by the tool that was
    discovered, not by what is installed.
    """
    if tool_name == "magick":
        return ["magick", command, *arguments]
    return [command, *arguments]


# A number as ImageMagick's %g prints it, INCLUDING exponent form: %g
# switches to "3.84e-05" below about 1e-4, which a near-exact template match
# reaches easily. An earlier `[\d.]+` pattern silently mis-parsed those --
# "6.55e+04 (0.5) @ 1,2" yielded an absolute score of 04 rather than failing
# loudly -- so the exponent is matched explicitly.
_NUMBER = r"[\d.]+(?:[eE][+-]?\d+)?"

# Verified against ImageMagick 7.1.2-27 Q16-HDRI, not just documentation:
#
#   0 (0) @ 63,41 [0]                          exact match, exit 0
#   1285 (0.0196078) @ 63,41 [0.0196072]       normal case, exit 1
#
# absolute score, an optional normalized score in parens, then the best
# match's top-left offset. The trailing "[...]" is compare's own best-match
# metric and is deliberately not captured. `search()` (not `match()`) also
# matters: compare appends warnings straight onto this line with no
# separator, e.g. "...[0.40985]compare: images too dissimilar `x.png' @
# warning/compare.c/...", and can do so while still exiting 0.
_MATCH_RE = re.compile(rf"({_NUMBER})\s*(?:\(({_NUMBER})\))?\s*@\s*(\d+)\s*,\s*(\d+)")


class ToolImageSearchBackend(GUIBackend):
    """Locate a template image through ImageMagick's compare."""

    def __init__(self, tool, runner=None):
        """Drive `tool`, optionally through an injected `runner`."""
        if tool.name not in _SUPPORTED:
            raise PyGUITestError(f"no image-search command for {tool.name!r}")
        self.tool = tool
        self._runner = runner or self._run

    # A read-only override of GUIBackend's plain, writable `name` attribute
    # -- see the same note in input.py. Nothing assigns to it externally.
    @property
    def name(self) -> str:  # type: ignore[override]
        """Identifier for this backend, e.g. 'imagesearch:compare'."""
        return f"imagesearch:{self.tool.name}"

    @property
    def capabilities(self):
        """Template matching only."""
        return CapabilitySet({Capability.IMAGE_LOCATE})

    def _run(self, argv, allowed_returncodes=(0,), timeout=_SUBPROCESS_TIMEOUT):
        """Run `argv`, raising unless the exit code is one of `allowed_returncodes`.

        Separate from capture.py's _run, which treats any nonzero exit as
        failure: compare's exit 1 means "images differ", the expected
        outcome of a subimage search, not an error.

        `timeout` is a parameter rather than a constant because a subimage
        search is the one call here whose runtime depends on how much desktop
        it was pointed at; `locate` derives it and hands it in.
        """
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(
                f"{' '.join(argv)} timed out after {timeout:g}s"
            ) from exc
        if result.returncode not in allowed_returncodes:
            raise PyGUITestError(
                f"{' '.join(argv)} failed ({result.returncode}): "
                f"{result.stderr.strip() or 'no output'}"
            )
        return result

    def _image_size(self, path):
        """One image's own (width, height), via identify.

        Used for the template, whose size the returned ImageMatch carries, and
        for the haystack, whose size decides the search budget.
        """
        result = self._runner(
            _search_argv(self.tool.name, "identify", "-format", "%w %h", path)
        )
        width, height = result.stdout.split()
        return int(width), int(height)

    def _search_pixels(self, haystack, region):
        """How much of the haystack the search will cover, for `_search_timeout`.

        A region answers this without asking anything: the crop that restricts
        the search is exactly the area the budget is for. Without one, the
        haystack has to be measured, which costs an `identify` call.

        Nothing here is allowed to raise. A haystack that cannot be measured is
        compare's own error to report, with compare's own message, and a budget
        is the wrong place to raise it from -- so an unreadable answer is 0,
        which is the floor.
        """
        if region is not None:
            return region[2] * region[3]
        try:
            width, height = self._image_size(haystack)
        except (PyGUITestError, ValueError, IndexError):
            return 0
        return width * height

    def locate(self, haystack, template, region=None, metric="RMSE", threshold=None):
        """Find `template` in `haystack`, or None if nothing clears `threshold`.

        `region` is (x, y, width, height) in `haystack`'s own pixel space;
        when given, this crops to it first via `convert ... -crop ... +repage`
        into a temp file, so compare only ever searches the requested
        rectangle -- and adds the crop's own offset back onto the match, so
        the returned ImageMatch stays in haystack-space regardless.

        `threshold`, when given, is compared in Python against the
        *normalized* score compare prints in parentheses (falling back to
        the absolute score if a build omits it), rather than via compare's
        own `-dissimilarity-threshold` flag -- that flag can suppress the
        "@ x,y" location entirely on some ImageMagick versions when the
        threshold isn't met, which would make output parsing unreliable.
        Easier to always get a location and decide in Python.

        Threshold direction assumes a lower-is-better metric (RMSE, MSE,
        PHASE, DPC -- the default and the common case). NCC and PSNR are
        higher-is-better; passing threshold with one of those is the
        caller's own responsibility to invert. A known limitation, not
        guessed-at handling, since there is no local ImageMagick install to
        verify metric-by-metric behaviour against.

        The search compares colour channels only -- alpha is not part of a
        match -- because a haystack and a template that disagree about having
        an alpha channel at all make compare answer with a wrong offset
        instead of reporting that it found nothing. On macOS that is every
        call: `screencapture` writes an alpha channel and a template cut by
        another tool usually does not. See the module docstring's channel-shape
        note for the measurements.

        The subprocess budget is derived from the area searched rather than
        fixed: on a build with no FFT delegate a search costs 20-40us per
        pixel of haystack, so a 1080p desktop needs the better part of a
        minute and a 4K one several. A `region` is the cheap answer to that,
        which is what `within=` turns into at the session level -- searching
        one window instead of a desktop is the difference between about a
        second and about fifty.
        """
        self.require(Capability.IMAGE_LOCATE)
        search_target = haystack
        crop_path = None
        offset_x = offset_y = 0
        # Budgeted, and measured, before the search rather than after it. The
        # budget needs the area; and a template that does not exist, or an
        # ImageMagick that cannot answer `identify`, should say so in
        # milliseconds instead of after however long compare spent searching
        # for a result this call could not have returned anyway.
        search_timeout = _search_timeout(self._search_pixels(haystack, region))
        try:
            if region is not None:
                offset_x, offset_y, width, height = region
                descriptor, crop_path = tempfile.mkstemp(suffix=".png")
                os.close(descriptor)
                self._runner(
                    _crop.argv(haystack, (offset_x, offset_y, width, height), crop_path)
                )
                search_target = crop_path

            result = self._runner(
                _search_argv(
                    self.tool.name,
                    "compare",
                    # Colour channels only -- see the channel-shape note in the
                    # module docstring. A capture that carries an alpha channel
                    # (every macOS one) searched for a template that does not
                    # answers with the wrong offset, not with an error.
                    "-channel",
                    "RGB",
                    "-metric",
                    metric,
                    "-subimage-search",
                    search_target,
                    template,
                    "null:",
                ),
                allowed_returncodes=(0, 1),
                timeout=search_timeout,
            )
            found = _MATCH_RE.search(result.stderr)
            if found is None:
                raise PyGUITestError(
                    f"could not parse compare output: {result.stderr.strip()!r}"
                )
            absolute_score = float(found.group(1))
            normalized_score = (
                float(found.group(2)) if found.group(2) is not None else absolute_score
            )
            rel_x, rel_y = int(found.group(3)), int(found.group(4))

            if threshold is not None and normalized_score > threshold:
                return None

            width, height = self._image_size(template)
            return ImageMatch(
                x=offset_x + rel_x,
                y=offset_y + rel_y,
                width=width,
                height=height,
                score=normalized_score,
            )
        finally:
            if crop_path is not None and os.path.exists(crop_path):
                os.unlink(crop_path)
