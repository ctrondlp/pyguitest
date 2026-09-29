"""The message contract of the typed failures.

`errors.py` explains why every failure here is typed: a bare zero cannot say
whether a click missed or a desktop cannot click at all. This covers the other
half of that -- what the message says -- because for most callers the message
is the entire encounter with the type.
"""

import unittest

from pyguitest import Capability
from pyguitest.errors import (
    CapabilityUnsupported,
    PermissionRequired,
    PyGUITestError,
    WindowNotFound,
)


class TestUnsupportedMessagesNameTheFix(unittest.TestCase):
    def test_the_ordinary_case_points_at_the_report(self):
        """A capability no backend here serves is the commonest one there is.

        It used to name the capability, the backend and the reason, and stop
        there -- everything except the one thing a reader needs. Found in the
        recorder's generated `gui.require(...)` preambles, which are written
        to fail on exactly this exception on a weaker desktop.
        """
        message = str(
            CapabilityUnsupported(Capability.WINDOW_LOWER, backend="gnomeshell")
        )
        self.assertIn("WINDOW_LOWER is unsupported on gnomeshell", message)
        self.assertIn("pyguitest doctor", message)

    def test_a_reason_still_reads_as_one_sentence(self):
        message = str(
            CapabilityUnsupported(Capability.POINTER_QUERY, reason="no readback")
        )
        self.assertTrue(
            message.startswith("POINTER_QUERY is unsupported: no readback."),
            message,
        )

    def test_a_missing_grant_names_the_grant(self):
        """The one case no install closes, so it gets the other tail."""
        message = str(PermissionRequired(Capability.POINTER_BUTTON, backend="portal"))
        self.assertIn("Grant it", message)

    def test_a_call_site_can_replace_the_tail(self):
        message = str(
            CapabilityUnsupported(Capability.CLIPBOARD, hint=". Install wl-clipboard.")
        )
        self.assertIn("Install wl-clipboard", message)
        self.assertNotIn("pyguitest doctor", message)

    def test_the_attributes_a_caller_catches_on_are_still_all_there(self):
        error = CapabilityUnsupported(
            Capability.SCREEN_CAPTURE, backend="x11", reason="no capture tool"
        )
        self.assertIs(error.capability, Capability.SCREEN_CAPTURE)
        self.assertEqual(error.backend, "x11")
        self.assertEqual(error.reason, "no capture tool")
        self.assertIsInstance(error, PyGUITestError)

    def test_the_hierarchy_is_unchanged(self):
        self.assertTrue(issubclass(PermissionRequired, CapabilityUnsupported))
        self.assertTrue(issubclass(CapabilityUnsupported, PyGUITestError))
        self.assertFalse(issubclass(WindowNotFound, CapabilityUnsupported))


if __name__ == "__main__":
    unittest.main()
