"""Cropping a rectangle out of an already-captured image.

Shared because two callers need exactly the same thing for different
reasons. `imagesearch.py` crops so `compare` only searches the rectangle it
was asked about; the capture backends crop because most screenshot tools
have no exact-rectangle mode of their own -- gnome-screenshot and spectacle
have none at all, and the portal's Screenshot interface returns the whole
screen or nothing. Capturing everything and then cutting the rectangle out
gives all of them a region argument that behaves identically.

No image library is introduced here either: ImageMagick is already a
dependency of the image-search path and ships `import` for capture, so the
crop costs nothing that was not already assumed present.
"""

import shutil
import subprocess

from ..errors import PyGUITestError

__all__ = ["available", "crop_command", "crop"]

_SUBPROCESS_TIMEOUT = 15


def crop_command():
    """argv[0] for cropping: `magick` where it exists, else `convert`.

    ImageMagick 7 prints a deprecation warning on every `convert`
    invocation ("The convert command is deprecated in IMv7, use magick
    instead", seen live on 7.1.2-27), and IMv8 is expected to drop the
    legacy name entirely. `magick <in> -crop ... <out>` is the direct
    equivalent and takes the same argument order, so preferring it costs
    nothing and keeps working when `convert` goes away.
    """
    return "magick" if shutil.which("magick") else "convert"


def available():
    """Whether either ImageMagick crop command is on PATH."""
    return shutil.which("magick") is not None or shutil.which("convert") is not None


def argv(source, region, destination):
    """The crop command line, for a caller that runs it itself.

    `+repage` is not optional decoration. Without it ImageMagick keeps the
    crop's offset in the output's page geometry, and every later operation
    -- a second crop, a subimage search -- sees coordinates shifted by that
    offset. Resetting the page makes the result an ordinary image whose
    top-left is (0, 0), which is what every caller here assumes.
    """
    x, y, width, height = region
    return [
        crop_command(),
        source,
        "-crop",
        f"{width}x{height}+{x}+{y}",
        "+repage",
        destination,
    ]


def crop(source, region, destination, runner=None):
    """Write `region` of `source` to `destination`, and return that path."""
    command = argv(source, region, destination)
    (runner or _run)(command)
    return destination


def _run(command):
    """Run a crop, raising if ImageMagick reports failure or hangs."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
        )
    except FileNotFoundError as exc:
        raise PyGUITestError(
            f"{command[0]} is not installed; a capture region needs "
            "ImageMagick to crop the rectangle out"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PyGUITestError(f"{' '.join(command)} timed out") from exc
    if result.returncode != 0:
        raise PyGUITestError(
            f"{' '.join(command)} failed ({result.returncode}): "
            f"{result.stderr.strip() or 'no output'}"
        )
    return result
