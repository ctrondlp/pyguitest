"""PortalCaptureBackend against a stand-in Gio connection.

The Screenshot portal is the only capture path that needs nothing
installed -- no grim, no ImageMagick, no X connection -- which makes it the
one that works inside a Flatpak sandbox, where the tools cannot be reached
at all. It is also the only one whose reply is a URI rather than a path,
and whose output file belongs to someone else.

Reuses test_portal.py's fake Gio: both backends drive the same portal
request/response plumbing (portalrequest.py), so a second fake would only
be a second thing to keep in step.
"""

import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

from pyguitest.capabilities import Capability
from pyguitest.errors import PermissionRequired, PyGUITestError
from test_portal import FakeConnection, install_fake_gi


class ScreenshotConnection(FakeConnection):
    """A FakeConnection that answers Screenshot with a file:// uri."""

    def __init__(self, uri=None, code=0):
        # Written to a real file so the copy/crop/unlink path is exercised
        # against something that actually exists on disk.
        descriptor, self.produced = tempfile.mkstemp(suffix=".png")
        os.write(descriptor, b"\x89PNG\r\n\x1a\npretend pixels")
        os.close(descriptor)
        results = {} if uri == "" else {"uri": uri or f"file://{self.produced}"}
        super().__init__(responses={"Screenshot": (code, results)})


class PortalCaptureTestCase(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_gi()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import portalcapture

        self.module = portalcapture

    def backend(self, connection=None, **kwargs):
        connection = connection or ScreenshotConnection()
        self.connection = connection
        self.addCleanup(
            lambda: (
                os.path.exists(connection.produced) and os.unlink(connection.produced)
            )
        )
        return self.module.PortalCaptureBackend(connection=connection, **kwargs)


class TestCapabilities(PortalCaptureTestCase):
    def test_screen_capture_is_declared(self):
        self.assertIn(Capability.SCREEN_CAPTURE, self.backend().capabilities)

    def test_window_capture_is_not_claimed(self):
        # The interface has no per-window call at all.
        self.assertNotIn(Capability.WINDOW_CAPTURE, self.backend().capabilities)

    def test_available_when_gi_imports(self):
        self.assertTrue(self.module.available())

    def test_construction_raises_no_dialog(self):
        # Unlike the RemoteDesktop portal, there is no session to negotiate,
        # so nothing is called until the first capture. That is what lets
        # this be constructed cheaply -- the consent prompt comes later.
        connection = ScreenshotConnection()
        self.backend(connection)
        self.assertEqual(connection.calls, [])


class TestCapture(PortalCaptureTestCase):
    def _out(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_the_portal_file_is_copied_to_the_requested_path(self):
        gui = self.backend()
        out = self._out()
        self.assertEqual(gui.capture(path=out), out)
        with open(out, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\x89PNG"))

    def test_the_portals_own_copy_is_removed(self):
        # One file per call otherwise, and an unattended run makes a lot of
        # calls into whatever directory the portal writes to.
        gui = self.backend()
        gui.capture(path=self._out())
        self.assertFalse(os.path.exists(self.connection.produced))

    def test_the_call_carries_an_empty_parent_and_the_options(self):
        gui = self.backend()
        gui.capture(path=self._out())
        method, args = self.connection.calls[0]
        self.assertEqual(method, "Screenshot")
        parent, options = args
        self.assertEqual(parent, "")
        self.assertIn("handle_token", options)
        self.assertFalse(options["interactive"].value)

    def test_interactive_is_opt_in_and_reaches_the_portal(self):
        # True opens a picker and blocks on a human; it must never be the
        # default for an unattended run, but a person at a REPL may want it.
        gui = self.backend(interactive=True)
        gui.capture(path=self._out())
        _method, (_parent, options) = self.connection.calls[0]
        self.assertTrue(options["interactive"].value)

    def test_a_window_is_refused_rather_than_silently_shooting_the_screen(self):
        gui = self.backend()
        with self.assertRaises(PyGUITestError):
            gui.capture(window="w")
        self.assertEqual(self.connection.calls, [])

    def test_window_and_region_together_are_refused(self):
        gui = self.backend()
        with self.assertRaises(PyGUITestError):
            gui.capture(window="w", region=(0, 0, 5, 5))

    def test_a_region_is_cropped_out_of_the_full_screen_shot(self):
        # The interface takes no rectangle -- the only options are `modal`
        # and `interactive` -- so a region is served the same way it is for
        # gnome-screenshot and spectacle: shoot everything, cut it down.
        gui = self.backend()
        out = self._out()
        commands = []
        with mock.patch.object(
            self.module._crop, "crop", side_effect=lambda *a, **k: commands.append(a)
        ):
            gui.capture(path=out, region=(1, 2, 3, 4))
        self.assertEqual(commands, [(self.connection.produced, (1, 2, 3, 4), out)])
        # Still only one call to the portal: the crop is local.
        self.assertEqual(len(self.connection.calls), 1)


class TestFailures(PortalCaptureTestCase):
    def test_a_dismissed_prompt_raises_permission_required(self):
        # Response code 1 is the portal's "cancelled", which for a
        # screenshot means the user said no -- a distinct outcome from the
        # portal itself failing.
        gui = self.backend(ScreenshotConnection(code=1))
        with self.assertRaises(PermissionRequired):
            gui.capture()

    def test_any_other_response_code_raises(self):
        gui = self.backend(ScreenshotConnection(code=2))
        with self.assertRaises(PyGUITestError):
            gui.capture()

    def test_success_with_no_uri_is_not_treated_as_success(self):
        gui = self.backend(ScreenshotConnection(uri=""))
        with self.assertRaises(PyGUITestError) as caught:
            gui.capture()
        self.assertIn("no uri", str(caught.exception))

    def test_a_non_file_uri_is_refused(self):
        gui = self.backend(ScreenshotConnection(uri="https://example.invalid/x.png"))
        with self.assertRaises(PyGUITestError) as caught:
            gui.capture()
        self.assertIn("https", str(caught.exception))

    def test_a_percent_encoded_path_is_decoded(self):
        # The portal returns a URI, so a directory with a space in it comes
        # back as %20. Treating the URI path as a filename verbatim would
        # fail on exactly the paths a person is most likely to have.
        directory = tempfile.mkdtemp(prefix="pyguitest shots ")
        target = os.path.join(directory, "shot one.png")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\npretend pixels")
        self.addCleanup(lambda: os.path.isdir(directory) and os.rmdir(directory))

        connection = ScreenshotConnection()
        connection.responses["Screenshot"] = (
            0,
            {"uri": "file://" + quote(target)},
        )
        gui = self.backend(connection)
        descriptor, out = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(out) and os.unlink(out))
        self.assertEqual(gui.capture(path=out), out)
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
