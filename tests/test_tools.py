import unittest
from unittest import mock

from pyguitest import tools
from pyguitest.capabilities import Capability


class TestToolRanking(unittest.TestCase):
    def test_input_tools_are_ranked_keymap_safe_first(self):
        # The audit's keymap trap as preference order: tools that let the
        # client supply a keymap must outrank ones injecting raw scancodes.
        names = [t.name for t in tools.INPUT_TOOLS]
        self.assertLess(names.index("wdotool"), names.index("ydotool"))
        self.assertLess(names.index("wtype"), names.index("ydotool"))

    def test_ydotool_is_marked_keymap_unsafe(self):
        by_name = {t.name: t for t in tools.INPUT_TOOLS}
        self.assertFalse(by_name["ydotool"].keymap_safe)
        self.assertTrue(by_name["wdotool"].keymap_safe)

    def test_wtype_does_not_claim_pointer_capabilities(self):
        by_name = {t.name: t for t in tools.INPUT_TOOLS}
        self.assertIn(Capability.TEXT_ENTRY, by_name["wtype"].capabilities)
        self.assertNotIn(Capability.POINTER_MOVE, by_name["wtype"].capabilities)

    def test_best_filters_by_capability(self):
        self.assertIsNone(tools.best((), Capability.TEXT_ENTRY))

    def test_discover_returns_only_present_tools(self):
        for tool in tools.discover(tools.INPUT_TOOLS):
            self.assertTrue(tool.present)

    def test_absent_tool_reports_no_path(self):
        fake = tools.ExternalTool("definitely-not-a-real-binary", frozenset())
        self.assertIsNone(fake.path())
        self.assertFalse(fake.present)


if __name__ == "__main__":
    unittest.main()


class TestX11OnlyTools(unittest.TestCase):
    """Regression from a live XWayland run: ImageMagick `import` claimed capture.

    It is installed and it runs, but it captures the XWayland root, which holds
    no native Wayland clients -- a screenshot that appears to work and is wrong.
    """

    def test_x11_only_tools_are_flagged(self):
        by_name = {t.name: t for t in tools.CAPTURE_TOOLS + tools.INPUT_TOOLS}
        self.assertTrue(by_name["import"].x11_only)
        self.assertTrue(by_name["xdotool"].x11_only)
        self.assertFalse(by_name["grim"].x11_only)
        self.assertFalse(by_name["wdotool"].x11_only)

    def test_discover_can_exclude_them(self):
        fake = tools.ExternalTool("sh", frozenset(), x11_only=True)
        self.assertEqual(tools.discover([fake], allow_x11_only=True), (fake,))
        self.assertEqual(tools.discover([fake], allow_x11_only=False), ())


class TestWlrootsOnlyTools(unittest.TestCase):
    """Regression: wtype was selected on GNOME, where it cannot work.

    Mutter implements none of the wlroots protocols wtype needs
    (zwp_virtual_keyboard_manager_v1), so it installs, runs, exits zero and
    types nothing. Being installed is not the same as being usable.
    """

    def test_wtype_is_flagged_wlroots_only(self):
        by_name = {t.name: t for t in tools.INPUT_TOOLS}
        self.assertTrue(by_name["wtype"].wlroots_only)
        self.assertFalse(by_name["ydotool"].wlroots_only)
        self.assertFalse(by_name["wdotool"].wlroots_only)

    def test_discover_excludes_it_when_the_compositor_lacks_the_protocol(self):
        wtype = next(t for t in tools.INPUT_TOOLS if t.name == "wtype")
        group = [tools.ExternalTool("sh", frozenset(), wlroots_only=True)]
        self.assertEqual(len(tools.discover(group, allow_wlroots_only=True)), 1)
        self.assertEqual(tools.discover(group, allow_wlroots_only=False), ())
        self.assertTrue(wtype.wlroots_only)


class TestRootReadingToolsNeedARealXServer(unittest.TestCase):
    """x_root_only: stricter than x11_only, and separate from it.

    Proven on GNOME Shell 50.4 with scripts/diagnose-x11-capture.py:
    XWayland refuses GetImage on the root window outright -- a 1x1 request
    fails exactly as a full-screen one does, under every pixmap format and
    plane mask -- because native Wayland surfaces are never composited into
    it. So a tool that captures by reading the root is not merely degraded
    under XWayland, it cannot work at all.

    It matters that this is a separate flag. An x11_only tool is still
    genuinely useful under XWayland for the X11 clients it can see, so the
    existing flag is allowed there; these must not be.
    """

    def _by_name(self, name):
        return next(t for t in tools.CAPTURE_TOOLS if t.name == name)

    def test_gnome_screenshot_and_import_are_flagged(self):
        self.assertTrue(self._by_name("gnome-screenshot").x_root_only)
        self.assertTrue(self._by_name("import").x_root_only)

    def test_the_wayland_native_tools_are_not(self):
        self.assertFalse(self._by_name("grim").x_root_only)
        self.assertFalse(self._by_name("spectacle").x_root_only)

    def test_they_are_dropped_when_the_root_is_unreadable(self):
        group = (
            tools.ExternalTool("rooty", frozenset(), x_root_only=True),
            tools.ExternalTool("fine", frozenset()),
        )
        with mock.patch.object(tools.ExternalTool, "present", True):
            names = [t.name for t in tools.discover(group, allow_x_root_only=False)]
        self.assertEqual(names, ["fine"])

    def test_they_are_kept_on_a_real_x_server(self):
        group = (tools.ExternalTool("rooty", frozenset(), x_root_only=True),)
        with mock.patch.object(tools.ExternalTool, "present", True):
            names = [t.name for t in tools.discover(group, allow_x_root_only=True)]
        self.assertEqual(names, ["rooty"])

    def test_the_flag_is_independent_of_x11_only(self):
        # import carries both; a tool may carry either alone.
        group = (tools.ExternalTool("rooty", frozenset(), x_root_only=True),)
        with mock.patch.object(tools.ExternalTool, "present", True):
            kept = tools.discover(group, allow_x11_only=True, allow_x_root_only=False)
        self.assertEqual(kept, ())
