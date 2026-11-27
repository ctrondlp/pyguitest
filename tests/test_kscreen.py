"""The KDE SCREEN_INFO member, over an injected `kscreen-doctor --json`.

KWin was the one Wayland compositor with no SCREEN_INFO provider: `kdotool`
reports no outputs and subtracts the capability, so `gui.screens()` raised on
KDE. The cost went beyond callers asking for screens -- the recorder's
resolver asks `screens()` to decide whether anything is scaled, caught the
refusal and answered False, so on a KDE session scaled to 200% it positively
believed the session was unscaled.

The JSON here is the shape measured live on a Fedora 44 KWin session at
scale 1 and again at scale 2: `size` is the mode size (1280x800) at both,
and `scale` is the compositor's factor.
"""

import json
import unittest

from pyguitest.backends.kscreen import KScreenBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import PyGUITestError

MEASURED = {
    "outputs": [
        {
            "id": 1,
            "name": "Virtual-1",
            "connected": True,
            "enabled": True,
            "scale": 1,
            "pos": {"x": 0, "y": 0},
            "size": {"width": 1280, "height": 800},
            "currentModeId": "1",
            "modes": [
                {
                    "id": "1",
                    "name": "1280x800@60",
                    "refreshRate": 60,
                    "size": {"width": 1280, "height": 800},
                }
            ],
        },
        # A VM presents eight connectors; seven are off and publish no mode.
        {"id": 2, "name": "Virtual-2", "connected": False, "enabled": False},
    ]
}


def _backend(report):
    text = report if isinstance(report, str) else json.dumps(report)
    return KScreenBackend(environment=None, runner=lambda: text)


class TestKScreenBackend(unittest.TestCase):
    def test_it_offers_screen_info_and_nothing_else(self):
        # A separate single-capability member, like kwinevents beside kdotool.
        self.assertEqual(set(_backend(MEASURED).capabilities), {Capability.SCREEN_INFO})

    def test_it_reports_the_mode_size_and_the_scale(self):
        screens = _backend(MEASURED).screens()
        self.assertEqual(len(screens), 1)
        screen = screens[0]
        self.assertEqual((screen.width, screen.height), (1280, 800))
        self.assertEqual(screen.scale, 1.0)
        self.assertEqual(screen.name, "Virtual-1")
        self.assertEqual(screen.index, 0)

    def test_a_scaled_output_keeps_physical_pixels_and_reports_the_factor(self):
        # Measured at 200%: `size` stays the mode size and `scale` becomes 2,
        # which is what `Screen` is defined to carry -- physical pixels in
        # width/height, the factor in scale.
        scaled = json.loads(json.dumps(MEASURED))
        scaled["outputs"][0]["scale"] = 2
        screen = _backend(scaled).screens()[0]
        self.assertEqual((screen.width, screen.height), (1280, 800))
        self.assertEqual(screen.scale, 2.0)

    def test_a_fractional_scale_is_kept_as_a_float(self):
        scaled = json.loads(json.dumps(MEASURED))
        scaled["outputs"][0]["scale"] = 1.25
        self.assertEqual(_backend(scaled).screens()[0].scale, 1.25)

    def test_disabled_outputs_are_left_out_and_indices_stay_contiguous(self):
        report = json.loads(json.dumps(MEASURED))
        second = json.loads(json.dumps(report["outputs"][0]))
        second.update({"id": 3, "name": "Virtual-3"})
        report["outputs"].append(second)
        screens = _backend(report).screens()
        self.assertEqual([s.name for s in screens], ["Virtual-1", "Virtual-3"])
        self.assertEqual([s.index for s in screens], [0, 1])

    def test_an_output_with_no_usable_size_is_skipped_rather_than_guessed(self):
        report = {
            "outputs": [
                {"name": "Virtual-1", "enabled": True, "size": {}},
                {"name": "Virtual-2", "enabled": True, "size": {"width": "wide"}},
            ]
        }
        self.assertEqual(_backend(report).screens(), [])

    def test_a_non_numeric_scale_falls_back_to_one_rather_than_raising(self):
        report = json.loads(json.dumps(MEASURED))
        report["outputs"][0]["scale"] = None
        self.assertEqual(_backend(report).screens()[0].scale, 1.0)

    def test_output_that_is_not_json_is_reported_as_such(self):
        with self.assertRaises(PyGUITestError) as caught:
            _backend("kscreen-doctor: command not found").screens()
        self.assertIn("did not return JSON", str(caught.exception))

    def test_a_report_with_no_outputs_key_is_empty_not_an_error(self):
        self.assertEqual(_backend({}).screens(), [])


if __name__ == "__main__":
    unittest.main()
