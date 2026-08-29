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

That exercise found a real defect, now fixed: ImageMagick formats scores
with `%g`, which switches to exponent notation below about 1e-4 -- reachable
for a near-exact match -- and the original parser both failed on
`1.2e-06` and, worse, silently read the absolute score of `6.55e+04` as
`04`. See `_NUMBER`.

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

_SUBPROCESS_TIMEOUT = 15

_SUPPORTED = {"compare"}


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

    @property
    def name(self):
        """Identifier for this backend, e.g. 'imagesearch:compare'."""
        return f"imagesearch:{self.tool.name}"

    @property
    def capabilities(self):
        """Template matching only."""
        return CapabilitySet({Capability.IMAGE_LOCATE})

    def _run(self, argv, allowed_returncodes=(0,)):
        """Run `argv`, raising unless the exit code is one of `allowed_returncodes`.

        Separate from capture.py's _run, which treats any nonzero exit as
        failure: compare's exit 1 means "images differ", the expected
        outcome of a subimage search, not an error.
        """
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
            )
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(f"{' '.join(argv)} timed out") from exc
        if result.returncode not in allowed_returncodes:
            raise PyGUITestError(
                f"{' '.join(argv)} failed ({result.returncode}): "
                f"{result.stderr.strip() or 'no output'}"
            )
        return result

    def _template_size(self, template):
        """The template image's own (width, height), via identify."""
        result = self._runner(["identify", "-format", "%w %h", template])
        width, height = result.stdout.split()
        return int(width), int(height)

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
        """
        self.require(Capability.IMAGE_LOCATE)
        search_target = haystack
        crop_path = None
        offset_x = offset_y = 0
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
                [
                    "compare",
                    "-metric",
                    metric,
                    "-subimage-search",
                    search_target,
                    template,
                    "null:",
                ],
                allowed_returncodes=(0, 1),
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

            width, height = self._template_size(template)
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
