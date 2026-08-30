import subprocess
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


class TestVersion(unittest.TestCase):
    """ExternalTool.version() -- best-effort, and never raises.

    Patches shutil.which (what path() calls) and subprocess.run directly,
    rather than ExternalTool.path itself: path is a plain method, not a
    property, so replacing it on the class needs the same care as any
    other patched method, and going through its own dependency is simpler.
    """

    def test_absent_tool_has_no_version(self):
        fake = tools.ExternalTool("definitely-not-a-real-binary", frozenset())
        self.assertIsNone(fake.version())

    def test_present_tool_runs_dash_dash_version_by_default(self):
        fake = tools.ExternalTool("wdotool", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/wdotool"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0, stdout="wdotool 1.2.3\nextra ignored line\n", stderr=""
            )
            self.assertEqual(fake.version(), "wdotool 1.2.3")
            self.assertEqual(run.call_args.args[0], ["/bin/wdotool", "--version"])

    def test_hyprctl_uses_its_own_version_subcommand(self):
        fake = tools.ExternalTool("hyprctl", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/usr/bin/hyprctl"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0, stdout="Hyprland 0.41.0\n", stderr=""
            )
            self.assertEqual(fake.version(), "Hyprland 0.41.0")
            self.assertEqual(run.call_args.args[0], ["/usr/bin/hyprctl", "version"])

    def test_falls_back_to_stderr_when_stdout_is_empty(self):
        fake = tools.ExternalTool("ydotool", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/ydotool"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0, stdout="", stderr="ydotool 1.0.4\n"
            )
            self.assertEqual(fake.version(), "ydotool 1.0.4")

    def test_no_output_at_all_reports_none(self):
        fake = tools.ExternalTool("xdotool", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/xdotool"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            self.assertIsNone(fake.version())

    def test_a_hang_reports_none_rather_than_raising(self):
        fake = tools.ExternalTool("xdotool", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/xdotool"),
            mock.patch(
                "pyguitest.tools.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="xdotool", timeout=3.0),
            ),
        ):
            self.assertIsNone(fake.version())

    def test_a_run_time_oserror_reports_none(self):
        # path() said present, but the binary is gone or unexecutable by the
        # time subprocess actually runs it -- not this method's job to
        # prevent, only to survive.
        fake = tools.ExternalTool("wtype", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/wtype"),
            mock.patch("pyguitest.tools.subprocess.run", side_effect=OSError),
        ):
            self.assertIsNone(fake.version())


class TestDualBinaryTools(unittest.TestCase):
    """also_needs: wl-clipboard ships as two commands, not one.

    wl-copy writes, wl-paste reads. A session with only one of the two
    cannot be offered as a working clipboard backend -- present must check
    both, not just the primary name.
    """

    def test_wl_copy_is_flagged_with_wl_paste_as_its_second_half(self):
        by_name = {t.name: t for t in tools.CLIPBOARD_TOOLS}
        self.assertEqual(by_name["wl-copy"].also_needs, "wl-paste")
        self.assertEqual(by_name["xclip"].also_needs, "")

    def test_present_requires_both_binaries(self):
        fake = tools.ExternalTool("sh", frozenset(), also_needs="definitely-not-real")
        self.assertIsNotNone(fake.path())  # sh is real
        self.assertFalse(fake.present)  # the second half is not

    def test_present_is_unaffected_when_there_is_no_second_half(self):
        fake = tools.ExternalTool("sh", frozenset())
        self.assertTrue(fake.present)


class TestMutterIncompatibleTools(unittest.TestCase):
    """mutter_incompatible: distinct from wlroots_only.

    Confirmed live on KDE Plasma 6: wl-copy/wl-paste round-trip correctly
    on KWin, which is not a wlroots compositor. wlroots_only would wrongly
    exclude KWin from a tool that actually works there, so wl-clipboard's
    real constraint (Mutter lacks wlr-data-control-unstable-v1) needs its
    own flag rather than reusing that one.
    """

    def test_wl_copy_is_flagged_mutter_incompatible_not_wlroots_only(self):
        by_name = {t.name: t for t in tools.CLIPBOARD_TOOLS}
        self.assertTrue(by_name["wl-copy"].mutter_incompatible)
        self.assertFalse(by_name["wl-copy"].wlroots_only)
        self.assertFalse(by_name["xclip"].mutter_incompatible)

    def test_discover_excludes_it_only_via_its_own_flag(self):
        fake = tools.ExternalTool("sh", frozenset(), mutter_incompatible=True)
        self.assertEqual(
            tools.discover([fake], allow_mutter_incompatible=True), (fake,)
        )
        self.assertEqual(tools.discover([fake], allow_mutter_incompatible=False), ())
        # wlroots_only=False by default: the wlroots filter must not also
        # catch this tool as a side effect of the other one being set.
        self.assertEqual(tools.discover([fake], allow_wlroots_only=False), (fake,))
