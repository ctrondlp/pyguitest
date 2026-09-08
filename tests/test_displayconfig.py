"""Mutter's DisplayConfig, read without the shell extension.

The fakes come from test_gnomeshell, which is where the shapes of a live
`GetCurrentState` were transcribed and where the other caller of this module
is tested; sharing them keeps one copy of the nesting to be wrong about.

What is verified here is the half GnomeShellBackend does not exercise: a
bounding box across several monitors, and every way of not knowing coming
back as None rather than as an exception, since the caller that made this a
module -- `_screen_size`, sizing a uinput device before any backend exists --
has nowhere to report a failure to.
"""

import unittest
from unittest import mock

from pyguitest.backends import displayconfig
from test_gnomeshell import (
    FakeDisplayConfig,
    _logical,
    _mode,
    _monitor,
    install_fake_gi,
)


class _Fixture(unittest.TestCase):
    def size(self, **kwargs):
        """layout_size() against a fake DisplayConfig built from kwargs."""
        display = FakeDisplayConfig(**kwargs)
        patcher = install_fake_gi(display=display)
        patcher.start()
        self.addCleanup(patcher.stop)
        return displayconfig.layout_size()


class TestLayoutSize(_Fixture):
    """The bounding box an absolute pointer device is mapped onto."""

    def test_one_monitor_is_its_own_size(self):
        self.assertEqual(self.size(), (1920, 1080))

    def test_two_monitors_side_by_side_span_both(self):
        # The failure this exists for: taking the first monitor, or the
        # largest, puts every click on a dual-head desktop in the wrong half.
        size = self.size(
            monitors=[
                _monitor("DP-1", [_mode(1920, 1080, True)]),
                _monitor("DP-2", [_mode(2560, 1440, True)]),
            ],
            logical=[_logical(["DP-1"]), _logical(["DP-2"], x=1920)],
        )
        self.assertEqual(size, (4480, 1440))

    def test_a_monitor_stacked_below_extends_the_height(self):
        size = self.size(
            monitors=[
                _monitor("DP-1", [_mode(1920, 1080, True)]),
                _monitor("DP-2", [_mode(1920, 1080, True)]),
            ],
            logical=[_logical(["DP-1"]), _logical(["DP-2"], y=1080)],
        )
        self.assertEqual(size, (1920, 2160))

    def test_a_fractional_scale_gives_the_logical_box(self):
        # 3840/1.5 = 2560: the units the compositor's coordinate space is
        # made of, not the panel's pixels. A device declared for 3840 would
        # put the pointer at half again the distance asked for.
        size = self.size(
            monitors=[_monitor("DP-1", [_mode(3840, 2160, True)])],
            logical=[_logical(["DP-1"], scale=1.5)],
        )
        self.assertEqual(size, (2560, 1440))

    def test_a_rotated_monitor_is_measured_after_the_turn(self):
        size = self.size(
            monitors=[_monitor("DP-1", [_mode(1920, 1080, True)])],
            logical=[_logical(["DP-1"], transform=1)],
        )
        self.assertEqual(size, (1080, 1920))


class TestNotKnowing(_Fixture):
    """Every failure is None, and None is not (0, 0)."""

    def test_a_bus_failure_is_none(self):
        self.assertIsNone(self.size(fails=RuntimeError("ServiceUnknown")))

    def test_no_pygobject_is_none(self):
        with mock.patch.object(displayconfig, "_gio", return_value=None):
            self.assertIsNone(displayconfig.layout_size())

    def test_a_state_with_no_usable_monitor_is_none(self):
        # Not (0, 0), which max() over an empty layout would have to invent
        # and which the caller cannot tell from a real answer.
        size = self.size(
            monitors=[_monitor("DP-1", [_mode(1920, 1080)])],  # nothing current
            logical=[_logical(["DP-1"])],
        )
        self.assertIsNone(size)

    def test_a_reply_shaped_wrong_is_none(self):
        # An unpack that raises must not escape into a caller that is
        # building an input device.
        display = FakeDisplayConfig()
        display.monitors = "not a list of monitors"
        patcher = install_fake_gi(display=display)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(displayconfig.layout_size())


class TestLogicalMonitors(unittest.TestCase):
    """The arithmetic itself, with no D-Bus in the way."""

    def test_a_mirrored_pair_is_one_entry_named_first(self):
        state = (
            1,
            [
                _monitor("DP-1", [_mode(1920, 1080, True)]),
                _monitor("HDMI-1", [_mode(1920, 1080, True)]),
            ],
            [_logical(["DP-1", "HDMI-1"])],
            {"layout-mode": 1},
        )
        self.assertEqual(
            displayconfig.logical_monitors(state),
            [(0, 0, 1920, 1080, 1.0, "DP-1")],
        )

    def test_physical_layout_mode_does_not_divide(self):
        state = (
            1,
            [_monitor("DP-1", [_mode(2560, 1600, True)])],
            [_logical(["DP-1"], scale=2.0)],
            {"layout-mode": 2},
        )
        (monitor,) = displayconfig.logical_monitors(state)
        self.assertEqual(monitor[2:4], (2560, 1600))


if __name__ == "__main__":
    unittest.main()
