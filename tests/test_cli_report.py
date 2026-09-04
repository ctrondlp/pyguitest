"""`pyguitest` (no subcommand) -- gating the advice block correctly.

Regression: `_report()` printed advice() whenever gui.capabilities.missing
was non-empty, which is true on virtually every desktop because tier 6 is
unreachable on Wayland by design -- not because a package is missing. That
printed a table full of [ no] followed by advice() truthfully reporting
nothing installable is missing. hints_for() is what actually reasons about
installed components, and is what the gate should check instead.
"""

import contextlib
import io
import unittest
from unittest import mock

import pyguitest
from pyguitest.__main__ import _report


class _FakeGui:
    def __init__(self, environment, capabilities=None):
        self.environment = environment
        self.capabilities = (
            capabilities if capabilities is not None else pyguitest.CapabilitySet()
        )

    def report(self):
        return "fake report"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestReportAdviceGating(unittest.TestCase):
    def _run(self, gui):
        with mock.patch("pyguitest.__main__.connect", return_value=gui):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                _report()
            return output.getvalue()

    def test_advice_is_suppressed_when_nothing_installable_is_missing(self):
        # capabilities.missing is non-empty (tier 6 on Wayland), but
        # hints_for() finds nothing installable missing.
        environment = pyguitest.detect(
            {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"}
        )
        import dataclasses

        complete = dataclasses.replace(
            environment,
            has_atspi=True,
            has_pygobject=True,
            has_dogtail=True,
            capture_tools=("grim",),
            input_tools=("wdotool",),
            image_tools=("compare",),
            # Likewise stands in for a clipboard path: GNOME has none from
            # a tool, so without this the clipboard hint fires and there is
            # always something missing here.
            clipboard_tools=("wl-copy",),
        )
        # WINDOW_PLACEMENT stands in for "the GNOME Shell extension is
        # active" -- compositor is MUTTER here (XDG_CURRENT_DESKTOP=GNOME),
        # and without it hints_for() would (correctly) report the extension
        # as missing, which is not what this test is exercising.
        complete_capabilities = pyguitest.CapabilitySet(
            {pyguitest.Capability.WINDOW_PLACEMENT}
        )
        text = self._run(_FakeGui(complete, capabilities=complete_capabilities))
        self.assertIn("fake report", text)
        self.assertNotIn("Nothing missing", text)
        self.assertNotIn("unlock more capabilities", text)

    def test_advice_still_appears_when_something_installable_is_missing(self):
        environment = pyguitest.detect(
            {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"}
        )
        import dataclasses

        incomplete = dataclasses.replace(environment, has_atspi=False)
        text = self._run(_FakeGui(incomplete))
        self.assertIn("unlock more capabilities", text)


if __name__ == "__main__":
    unittest.main()
