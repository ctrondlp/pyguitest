"""`pyguitest inspect` -- the accessible-tree dump, exercised without AT-SPI.

tree_data/format_tree take a plain gui/Element duck type, so these drive them
against fakes rather than a live accessibility bus -- the same shape
test_main_debug.py uses for _debug_data/_format_debug.
"""

import json
import unittest

from pyguitest.inspect import format_tree, tree_data
from pyguitest.roles import Role


class _FakeElement:
    def __init__(
        self,
        role,
        name,
        *,
        parent=None,
        children=None,
        enabled=True,
        visible=True,
        checkable=False,
        checked=False,
        selectable=False,
        selected=False,
        actions=(),
    ):
        self.role = role
        self.name = name
        self.parent = parent
        self.children = list(children) if children else []
        self.enabled = enabled
        self.visible = visible
        self.checkable = checkable
        self.checked = checked
        self.selectable = selectable
        self.selected = selected
        self.actions = list(actions)


class _FakeGui:
    """elements(role=...) served from a fixed role -> elements mapping.

    Real Session.elements() also filters by name/within, but tree_data only
    ever calls it with role= -- see inspect.tree_data.
    """

    def __init__(self, by_role):
        self._by_role = by_role

    def elements(self, role=None, name=None, within=None):
        return list(self._by_role.get(role, []))


class TestTreeData(unittest.TestCase):
    def test_empty_desktop_produces_no_applications(self):
        self.assertEqual(tree_data(_FakeGui({})), [])

    def test_groups_windows_by_owning_application(self):
        app = _FakeElement(role="application", name="editor")
        one = _FakeElement(role=Role.FRAME, name="Untitled", parent=app)
        two = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        gui = _FakeGui({Role.FRAME: [one, two]})

        data = tree_data(gui)

        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["application"], "editor")
        names = [w["name"] for w in data[0]["windows"]]
        self.assertEqual(names, ["Untitled", "Preferences"])

    def test_windows_under_different_applications_get_separate_entries(self):
        app_a = _FakeElement(role="application", name="editor")
        app_b = _FakeElement(role="application", name="calculator")
        one = _FakeElement(role=Role.FRAME, name="Untitled", parent=app_a)
        two = _FakeElement(role=Role.WINDOW, name="Calculator", parent=app_b)
        gui = _FakeGui({Role.FRAME: [one], Role.WINDOW: [two]})

        data = tree_data(gui)

        self.assertEqual([app["application"] for app in data], ["editor", "calculator"])

    def test_children_are_captured_recursively(self):
        app = _FakeElement(role="application", name="editor")
        checkbox = _FakeElement(role=Role.CHECK_BOX, name="Enable notifications")
        panel = _FakeElement(role=Role.PANEL, name="", children=[checkbox])
        window = _FakeElement(
            role=Role.FRAME, name="Preferences", parent=app, children=[panel]
        )
        gui = _FakeGui({Role.FRAME: [window]})

        data = tree_data(gui)

        window_data = data[0]["windows"][0]
        self.assertEqual(window_data["children"][0]["role"], Role.PANEL)
        self.assertEqual(
            window_data["children"][0]["children"][0]["name"], "Enable notifications"
        )

    def test_checked_and_selected_are_omitted_when_not_applicable(self):
        app = _FakeElement(role="application", name="editor")
        button = _FakeElement(role=Role.PUSH_BUTTON, name="Save")
        window = _FakeElement(
            role=Role.FRAME, name="Editor", parent=app, children=[button]
        )
        gui = _FakeGui({Role.FRAME: [window]})

        node = tree_data(gui)[0]["windows"][0]["children"][0]

        self.assertNotIn("checked", node)
        self.assertNotIn("selected", node)

    def test_checked_and_selected_are_included_when_the_role_supports_them(self):
        app = _FakeElement(role="application", name="editor")
        checkbox = _FakeElement(
            role=Role.CHECK_BOX,
            name="Enable notifications",
            checkable=True,
            checked=True,
        )
        item = _FakeElement(
            role=Role.LIST_ITEM, name="Item 1", selectable=True, selected=False
        )
        window = _FakeElement(
            role=Role.FRAME, name="Editor", parent=app, children=[checkbox, item]
        )
        gui = _FakeGui({Role.FRAME: [window]})

        children = tree_data(gui)[0]["windows"][0]["children"]

        self.assertIs(children[0]["checked"], True)
        self.assertIs(children[1]["selected"], False)

    def test_actions_are_included_only_when_present(self):
        app = _FakeElement(role="application", name="editor")
        button = _FakeElement(role=Role.PUSH_BUTTON, name="Save", actions=["click"])
        label = _FakeElement(role=Role.LABEL, name="Status")
        window = _FakeElement(
            role=Role.FRAME, name="Editor", parent=app, children=[button, label]
        )
        gui = _FakeGui({Role.FRAME: [window]})

        children = tree_data(gui)[0]["windows"][0]["children"]

        self.assertEqual(children[0]["actions"], ["click"])
        self.assertNotIn("actions", children[1])

    def test_window_filter_narrows_by_title_regex(self):
        app = _FakeElement(role="application", name="editor")
        one = _FakeElement(role=Role.FRAME, name="Untitled Document", parent=app)
        two = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        gui = _FakeGui({Role.FRAME: [one, two]})

        data = tree_data(gui, window="Prefer")

        self.assertEqual(len(data), 1)
        self.assertEqual([w["name"] for w in data[0]["windows"]], ["Preferences"])

    def test_json_serializable(self):
        app = _FakeElement(role="application", name="editor")
        checkbox = _FakeElement(role=Role.CHECK_BOX, name="Enable", checked=True)
        window = _FakeElement(
            role=Role.FRAME, name="Preferences", parent=app, children=[checkbox]
        )
        gui = _FakeGui({Role.FRAME: [window]})

        json.dumps(tree_data(gui))  # raises if anything is not JSON-safe


class TestFormatTree(unittest.TestCase):
    def test_empty_tree_renders_to_empty_string(self):
        self.assertEqual(format_tree([]), "")

    def test_application_heading_and_window_label(self):
        app = _FakeElement(role="application", name="editor")
        window = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        text = format_tree(tree_data(_FakeGui({Role.FRAME: [window]})))

        self.assertIn("Application: editor", text)
        self.assertIn("Window: Preferences [frame]", text)

    def test_nested_children_are_indented_with_tree_connectors(self):
        app = _FakeElement(role="application", name="editor")
        checkbox = _FakeElement(role=Role.CHECK_BOX, name="Enable notifications")
        panel = _FakeElement(role=Role.PANEL, name="General", children=[checkbox])
        window = _FakeElement(
            role=Role.FRAME, name="Preferences", parent=app, children=[panel]
        )
        text = format_tree(tree_data(_FakeGui({Role.FRAME: [window]})))

        self.assertIn("└── Window: Preferences [frame]", text)
        self.assertIn("    └── panel: General", text)
        self.assertIn("        └── check box: Enable notifications", text)

    def test_multiple_windows_use_both_connectors(self):
        app = _FakeElement(role="application", name="editor")
        one = _FakeElement(role=Role.FRAME, name="Untitled", parent=app)
        two = _FakeElement(role=Role.FRAME, name="Preferences", parent=app)
        text = format_tree(tree_data(_FakeGui({Role.FRAME: [one, two]})))

        self.assertIn("├── Window: Untitled [frame]", text)
        self.assertIn("└── Window: Preferences [frame]", text)

    def test_checked_and_disabled_flags_are_shown(self):
        app = _FakeElement(role="application", name="editor")
        checkbox = _FakeElement(
            role=Role.CHECK_BOX,
            name="Enable notifications",
            checkable=True,
            checked=True,
            enabled=False,
        )
        window = _FakeElement(
            role=Role.FRAME, name="Preferences", parent=app, children=[checkbox]
        )
        text = format_tree(tree_data(_FakeGui({Role.FRAME: [window]})))

        self.assertIn("check box: Enable notifications [disabled, checked]", text)

    def test_enabled_and_visible_elements_carry_no_flags(self):
        app = _FakeElement(role="application", name="editor")
        button = _FakeElement(role=Role.PUSH_BUTTON, name="Save")
        window = _FakeElement(
            role=Role.FRAME, name="Editor", parent=app, children=[button]
        )
        text = format_tree(tree_data(_FakeGui({Role.FRAME: [window]})))

        self.assertTrue(text.endswith("push button: Save"))


if __name__ == "__main__":
    unittest.main()
