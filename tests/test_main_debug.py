"""`pyguitest debug` -- the diagnostic dump a bug report should carry.

Exercises _debug_data/_format_debug directly rather than only through main(),
so a broken field is caught at the function that produces it rather than
buried in a hundred lines of text output.
"""

import contextlib
import dataclasses
import io
import json
import unittest
from unittest import mock

import pyguitest
from pyguitest.__main__ import (
    _debug_data,
    _env_field_value,
    _format_debug,
    _os_release_pretty,
    _sandbox_kind,
    main,
)
from pyguitest.capabilities import Capability, CapabilitySet


class _FakeBackend:
    name = "fake"

    def report(self):
        return "fake backend report"


class _BareBackend:
    """A backend with no report() -- most of them, composite is the exception."""

    name = "bare"


class _FakeGui:
    def __init__(self, environment, backend=None):
        self.environment = environment
        self.backend = backend or _FakeBackend()
        self.capabilities = CapabilitySet(
            {Capability.PROCESS_LAUNCH, Capability.TIMING}
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _environment(**overrides):
    base = pyguitest.detect(
        {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"}
    )
    return dataclasses.replace(base, **overrides)


class TestEnvFieldValue(unittest.TestCase):
    def test_enum_fields_are_unwrapped_to_their_value(self):
        self.assertEqual(_env_field_value(pyguitest.SessionType.WAYLAND), "wayland")
        self.assertEqual(_env_field_value(pyguitest.Compositor.MUTTER), "mutter")

    def test_other_values_pass_through_unchanged(self):
        self.assertIs(_env_field_value(True), True)
        self.assertEqual(_env_field_value(("a", "b")), ("a", "b"))


class TestSandboxKind(unittest.TestCase):
    def test_flatpak_is_detected(self):
        with mock.patch("os.path.exists", side_effect=lambda p: p == "/.flatpak-info"):
            self.assertEqual(_sandbox_kind(), "flatpak")

    def test_containerenv_is_detected(self):
        with mock.patch(
            "os.path.exists", side_effect=lambda p: p == "/run/.containerenv"
        ):
            self.assertEqual(_sandbox_kind(), "toolbox/podman")

    def test_dockerenv_is_detected(self):
        with mock.patch("os.path.exists", side_effect=lambda p: p == "/.dockerenv"):
            self.assertEqual(_sandbox_kind(), "docker")

    def test_bare_metal_reports_none(self):
        with mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(_sandbox_kind())


class TestOsReleasePretty(unittest.TestCase):
    def test_pretty_name_wins(self):
        path = mock.Mock()
        path.read_text.return_value = (
            'NAME="Fedora Linux"\nVERSION_ID=44\n'
            'PRETTY_NAME="Fedora Linux 44 (Workstation Edition)"\n'
        )
        self.assertEqual(
            _os_release_pretty(path), "Fedora Linux 44 (Workstation Edition)"
        )

    def test_falls_back_to_name_and_version(self):
        path = mock.Mock()
        path.read_text.return_value = 'NAME="Arch Linux"\nVERSION_ID="20260101"\n'
        self.assertEqual(_os_release_pretty(path), "Arch Linux 20260101")

    def test_name_alone_is_enough(self):
        path = mock.Mock()
        path.read_text.return_value = 'NAME="Arch Linux"\n'
        self.assertEqual(_os_release_pretty(path), "Arch Linux")

    def test_missing_file_reports_none(self):
        path = mock.Mock()
        path.read_text.side_effect = OSError
        self.assertIsNone(_os_release_pretty(path))


class TestDebugData(unittest.TestCase):
    def test_reports_the_backend_and_pyguitest_version(self):
        data = _debug_data(_FakeGui(_environment()))
        self.assertEqual(data["backend"], "fake")
        self.assertEqual(data["pyguitest_version"], pyguitest.__version__)
        self.assertIn("fake backend report", data["backend_report"])

    def test_focus_tracking_is_probed_and_reported(self):
        gui = _FakeGui(_environment())
        gui.focus_tracking_works = lambda: True
        self.assertIs(_debug_data(gui)["focus_tracking"], True)
        gui.focus_tracking_works = lambda: False
        self.assertIs(_debug_data(gui)["focus_tracking"], False)

    def test_focus_tracking_is_none_when_the_probe_cannot_run(self):
        # A backend with no element tree cannot answer the question at all,
        # which is a different report from "asked, and focus is not
        # published" -- the diagnostic must not fail either way.
        gui = _FakeGui(_environment())

        def explode():
            raise RuntimeError("no element tree here")

        gui.focus_tracking_works = explode
        self.assertIsNone(_debug_data(gui)["focus_tracking"])

    def test_environment_lists_every_field_true_and_false(self):
        # Regression risk this guards against: summary() only prints
        # mechanisms that are True, which is exactly wrong for a bug
        # report -- has_uinput=True, uinput_writable=False (the most common
        # input failure there is) is invisible in that filtered view.
        env = _environment(has_uinput=True, uinput_writable=False)
        data = _debug_data(_FakeGui(env))
        self.assertIs(data["environment"]["has_uinput"], True)
        self.assertIs(data["environment"]["uinput_writable"], False)

    def test_enum_fields_serialize_to_their_string_value(self):
        data = _debug_data(_FakeGui(_environment()))
        self.assertEqual(data["environment"]["session_type"], "wayland")
        self.assertEqual(data["environment"]["compositor"], "mutter")

    def test_json_round_trips(self):
        # json has no tuple type, so tuples (input_tools, notes, ...) come
        # back as lists -- not a bug, but this comparison has to look past
        # it rather than assert equality of the raw structures.
        data = _debug_data(_FakeGui(_environment()))
        redumped = json.loads(json.dumps(data))

        def _listify(value):
            if isinstance(value, tuple | list):
                return [_listify(v) for v in value]
            if isinstance(value, dict):
                return {k: _listify(v) for k, v in value.items()}
            return value

        self.assertEqual(redumped, _listify(data))

    def test_backend_report_is_none_when_the_backend_has_none(self):
        data = _debug_data(_FakeGui(_environment(), backend=_BareBackend()))
        self.assertIsNone(data["backend_report"])

    def test_tool_groups_cover_all_four_kinds(self):
        data = _debug_data(_FakeGui(_environment()))
        self.assertEqual(set(data["tools"]), {"input", "capture", "window", "image"})
        for group in data["tools"].values():
            self.assertTrue(group)
            for entry in group:
                self.assertIn("name", entry)
                self.assertIn("present", entry)
                self.assertIn("path", entry)
                self.assertIn("version", entry)


class TestFormatDebug(unittest.TestCase):
    def _data(self):
        return _debug_data(_FakeGui(_environment()))

    def test_covers_the_headline_fields(self):
        text = _format_debug(self._data())
        self.assertIn(pyguitest.__version__, text)
        self.assertIn("environment", text)
        self.assertIn("has_uinput", text)
        self.assertIn("fake backend report", text)

    def test_sandbox_and_host_distro_are_reported_when_present(self):
        data = self._data()
        data["sandbox"] = "flatpak"
        data["distro"]["host_pretty"] = "Fedora Linux 44"
        data["distro"]["host_family"] = "fedora"
        text = _format_debug(data)
        self.assertIn("sandbox      flatpak", text)
        self.assertIn("host distro  Fedora Linux 44 (outside the sandbox)", text)

    def test_absent_sandbox_prints_nothing_extra(self):
        data = self._data()
        data["sandbox"] = None
        data["distro"]["host_pretty"] = None
        data["distro"]["host_family"] = None
        text = _format_debug(data)
        self.assertNotIn("sandbox", text)
        self.assertNotIn("host distro", text)


class TestDebugCommand(unittest.TestCase):
    def _run(self, argv):
        with mock.patch(
            "pyguitest.__main__.connect", return_value=_FakeGui(_environment())
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(argv)
            return code, output.getvalue()

    def test_text_mode_is_the_default(self):
        code, text = self._run(["debug"])
        self.assertEqual(code, 0)
        self.assertIn(pyguitest.__version__, text)
        self.assertIn("environment", text)

    def test_json_flag_produces_valid_json(self):
        code, text = self._run(["debug", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(text)
        self.assertEqual(parsed["backend"], "fake")


if __name__ == "__main__":
    unittest.main()
