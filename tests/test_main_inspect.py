"""`pyguitest inspect` -- the CLI wiring around inspect.tree_data/format_tree.

Mirrors test_main_debug.py's TestDebugCommand: mock connect(), drive main(),
assert on captured stdout and the exit code.
"""

import contextlib
import io
import json
import unittest
from unittest import mock

import pyguitest
from pyguitest.__main__ import main
from pyguitest.capabilities import Capability, CapabilitySet
from pyguitest.roles import Role


class _FakeElement:
    """Minimal element fake -- only the fields inspect.tree_data reads.

    Duplicated rather than imported from test_inspect.py: this project's
    test modules each define their own fakes (see test_main_debug.py's
    _FakeGui/_FakeBackend) rather than sharing across files.
    """

    def __init__(self, role, name, *, parent=None, children=None):
        self.role = role
        self.name = name
        self.parent = parent
        self.children = list(children) if children else []
        self.enabled = True
        self.visible = True
        self.checkable = False
        self.checked = False
        self.selectable = False
        self.selected = False
        self.actions = []


class _FakeGui:
    def __init__(self, elements_by_role=None, supports_element_tree=True):
        self.environment = pyguitest.detect(
            {"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"}
        )
        self._elements_by_role = elements_by_role or {}
        self._supports_element_tree = supports_element_tree
        self.capabilities = CapabilitySet(
            {Capability.ELEMENT_TREE} if supports_element_tree else set()
        )

    def supports(self, capability):
        if capability is Capability.ELEMENT_TREE:
            return self._supports_element_tree
        return capability in self.capabilities

    def elements(self, role=None, name=None, within=None):
        return list(self._elements_by_role.get(role, []))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(argv, gui):
    with mock.patch("pyguitest.__main__.connect", return_value=gui):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()


class TestInspectCommand(unittest.TestCase):
    def _gui_with_one_window(self):
        app = _FakeElement(role="application", name="editor")
        window = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        return _FakeGui({Role.FRAME: [window]})

    def test_text_mode_is_the_default(self):
        code, text = _run(["inspect"], self._gui_with_one_window())
        self.assertEqual(code, 0)
        self.assertIn("Application: editor", text)
        self.assertIn("Window: Preferences [frame]", text)

    def test_json_flag_produces_valid_json(self):
        code, text = _run(["inspect", "--json"], self._gui_with_one_window())
        self.assertEqual(code, 0)
        parsed = json.loads(text)
        self.assertEqual(parsed[0]["application"], "editor")
        self.assertEqual(parsed[0]["windows"][0]["name"], "Preferences")

    def test_window_filter_is_passed_through(self):
        app = _FakeElement(role="application", name="editor")
        one = _FakeElement(role=Role.FRAME, name="Untitled", parent=app)
        two = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        gui = _FakeGui({Role.FRAME: [one, two]})

        code, text = _run(["inspect", "--window", "Prefer"], gui)

        self.assertEqual(code, 0)
        self.assertIn("Preferences", text)
        self.assertNotIn("Untitled", text)

    def test_missing_capability_prints_advice_and_fails(self):
        gui = _FakeGui(supports_element_tree=False)

        code, text = _run(["inspect"], gui)

        self.assertEqual(code, 1)
        self.assertTrue(text.strip())  # advice() produced something to read


if __name__ == "__main__":
    unittest.main()
