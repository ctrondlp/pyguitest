"""Screen capture via the Screenshot XDG desktop portal.

The gap this fills: every other capture path needs something installed.
grim is wlroots-only, gnome-screenshot and spectacle each belong to one
desktop, `import` needs an X server, and X11Backend needs python-xlib and an
X connection. `org.freedesktop.portal.Screenshot` is the cross-desktop,
unprivileged, consent-based route -- the same shape as the RemoteDesktop
portal this package already uses for injection, and available on GNOME, KDE
and anything else shipping a portal backend, including inside a Flatpak
sandbox where none of the tools can be reached at all.

What it does not do is regions. The interface takes no rectangle: the only
options are `modal` and `interactive`, and `interactive` means "let the user
pick", which is exactly the interactive selector an unattended run must
never open. So a region here is served the same way it is for
gnome-screenshot and spectacle -- capture everything, crop the rectangle out
with ImageMagick.

The reply is a URI, not a path, and the file behind it belongs to the
portal. It is copied to where the caller asked and the original is removed,
so a long test run does not quietly fill the screenshot directory with one
file per capture.

Signatures are transcribed from the actual portal XML
(org.freedesktop.portal.Screenshot.xml in the xdg-desktop-portal source)
and checked against a live xdg-desktop-portal advertising version 2.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from urllib.parse import unquote, urlparse

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, PermissionRequired, PyGUITestError
from . import crop as _crop
from . import portalrequest as _portalrequest
from .base import GUIBackend, check_region

__all__ = ["PortalCaptureBackend", "available"]

_INTERFACE = "org.freedesktop.portal.Screenshot"

# Response codes shared by every portal Request, per the Request XML.
_SUCCESS = 0
_CANCELLED = 1


def available():
    """Whether the library this backend needs is importable."""
    return _portalrequest.available()


class PortalCaptureBackend(GUIBackend):
    """Screen capture through the Screenshot portal."""

    name = "portalcapture"

    def __init__(self, connection=None, interactive=False, modal=True):
        """Connect to the session bus, or use an injected `connection`.

        Nothing is negotiated here. Unlike the RemoteDesktop portal, the
        Screenshot portal has no session to create -- each call stands
        alone -- so construction is cheap and raises no dialog. The consent
        prompt, where the desktop shows one, comes on the first capture.

        `interactive` asks the portal to let the user choose what to shoot
        before returning. It defaults to False and should stay there for any
        unattended run: True opens a picker and blocks until a human answers
        it. It is exposed because a person driving this from a REPL may
        genuinely want it.
        """
        modules = _portalrequest.gio()
        if modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs (see README)"
            )
        self._Gio, self._GLib = modules
        self._interactive = interactive
        self._modal = modal
        if connection is not None:
            self._connection = connection
        else:
            try:
                self._connection = self._Gio.bus_get_sync(
                    self._Gio.BusType.SESSION, None
                )
            except Exception as exc:
                raise BackendUnavailable(
                    f"cannot reach the session bus: {exc}"
                ) from exc

    @property
    def capabilities(self):
        """Whole-screen capture.

        Not WINDOW_CAPTURE: the portal has no per-window call. A composite
        resolves a window through its geometry member and passes the
        rectangle here as a region.
        """
        return CapabilitySet({Capability.SCREEN_CAPTURE})

    def _options(self):
        """The options dict for a Screenshot call."""
        return {
            "modal": self._GLib.Variant("b", self._modal),
            "interactive": self._GLib.Variant("b", self._interactive),
        }

    def _shoot(self):
        """Ask the portal for a screenshot, returning the file it wrote.

        `parent_window` is passed empty. It is the identifier of a window to
        parent the consent dialog to, in the portal's own "x11:<xid>" or
        "wayland:<handle>" form, and this package has no toplevel of its own
        to name -- an empty string is what the portal documents for exactly
        that case.
        """
        code, results = _portalrequest.request(
            (self._Gio, self._GLib),
            self._connection,
            _INTERFACE,
            "Screenshot",
            "(sa{sv})",
            ("", self._options()),
        )
        if code == _CANCELLED:
            raise PermissionRequired(
                Capability.SCREEN_CAPTURE,
                self.name,
                "the screenshot request was refused or dismissed; the "
                "portal shows this prompt once per application",
            )
        if code != _SUCCESS:
            raise PyGUITestError(f"the Screenshot portal failed with code {code}")
        uri = results.get("uri")
        if not uri:
            raise PyGUITestError(
                "the Screenshot portal reported success but returned no uri"
            )
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise PyGUITestError(
                f"the Screenshot portal returned a {parsed.scheme!r} uri; "
                "only file:// can be read as a local path"
            )
        return unquote(parsed.path)

    def capture(self, window=None, path=None, region=None):
        """Write a screenshot and return its path.

        `window` is refused rather than silently ignored: the portal takes
        no window argument, and returning a whole-screen image from a call
        that asked for one window would be a plausible-looking image of the
        wrong thing. In a composite the geometry member resolves it to a
        region before this is reached.
        """
        self.require(Capability.SCREEN_CAPTURE)
        if window is not None:
            raise PyGUITestError(
                "the Screenshot portal has no per-window call; compose this "
                "backend with one providing WINDOW_GEOMETRY, which resolves "
                "a window to a region"
            )
        region = check_region(region, window)
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)

        produced = self._shoot()
        try:
            if region is None:
                shutil.copyfile(produced, path)
            else:
                _crop.crop(produced, region, path)
        finally:
            # The portal's own copy is ours to clean up -- it writes one
            # file per call, and an unattended run makes a lot of calls.
            # Best-effort: a portal implementation that hands back a file it
            # still owns is not a reason to fail a capture that succeeded.
            with contextlib.suppress(OSError):
                os.unlink(produced)
        return path
