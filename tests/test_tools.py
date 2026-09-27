"""Tool discovery and ranking: what is found, and which tool wins.

The per-desktop constraints are most of it. A tool that only works under
wlroots, or only under X11, has to be excluded where it does not apply, and
one that needs a real X server has to be excluded rather than fail obscurely.
"""

import shutil
import subprocess
import sys
import unittest
from unittest import mock

from pyguitest import tools
from pyguitest.capabilities import Capability

REAL_BINARY = "cmd" if sys.platform == "win32" else "sh"
"""The name of a program that is genuinely on PATH, whatever the platform.

Several tests below need `ExternalTool.path()` to find something real, and
used `sh` -- which does not exist on Windows, so every one of them reported a
missing binary as a failure of the discovery logic. `cmd` is the equivalent
guarantee there. Asserted at import rather than assumed, because the whole
point of these tests is what happens when a tool is and is not present."""

assert shutil.which(REAL_BINARY), f"{REAL_BINARY} should be on PATH"


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
        fake = tools.ExternalTool(REAL_BINARY, frozenset(), x11_only=True)
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
        group = [tools.ExternalTool(REAL_BINARY, frozenset(), wlroots_only=True)]
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

    def test_a_failed_run_does_not_report_its_error_as_a_version(self):
        # Regression, seen in a real `pyguitest debug` on a Wayland
        # session: `xclip --version` cannot open a display, exits 1, and
        # its complaint was printed in the version column.
        fake = tools.ExternalTool("xclip", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/xclip"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="xclip: Error: Can't open display: (null)\n",
            )
            self.assertIsNone(fake.version())

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

    def test_the_probe_never_inherits_the_callers_stdin(self):
        # A probe that read the caller's stdin would consume input meant for
        # whoever is reading the report -- and the clipboard tools take their
        # content exactly that way.
        fake = tools.ExternalTool("wdotool", frozenset())
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/bin/wdotool"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            fake.version()
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class TestMacosClipboardTool(unittest.TestCase):
    """pbcopy: the tool macOS ships, and the only unprobeable one.

    It sits in CLIPBOARD_TOOLS the way `screencapture` sits in
    CAPTURE_TOOLS -- last, because the OS ships it, so there is nothing to
    rank it above -- and it carries a flag none of the others do: asking it
    for its version would rewrite the pasteboard.

    Measured live on macOS 26.7: with a sentinel value on the clipboard,
    `pbcopy --version < /dev/null` exited 0 having cleared it, and `pbcopy -h`
    cleared it too. Probing that tool would make `pyguitest doctor` destroy
    whatever the user had copied.
    """

    def _by_name(self):
        return {t.name: t for t in tools.CLIPBOARD_TOOLS}

    def test_it_is_listed_with_pbpaste_as_its_second_half(self):
        self.assertEqual(self._by_name()["pbcopy"].also_needs, "pbpaste")

    def test_it_carries_none_of_the_linux_only_flags(self):
        tool = self._by_name()["pbcopy"]
        self.assertFalse(tool.x11_only)
        self.assertFalse(tool.mutter_incompatible)
        self.assertFalse(tool.wlroots_only)

    def test_it_is_last_so_it_only_wins_when_nothing_else_can(self):
        # The X11 tools must stay ahead of it: a Linux box carrying shim
        # pbcopy/pbpaste wrappers still prefers its real clipboard tool.
        names = [t.name for t in tools.CLIPBOARD_TOOLS]
        self.assertEqual(names[-1], "pbcopy")
        self.assertLess(names.index("xclip"), names.index("pbcopy"))

    def test_it_is_the_one_tool_that_is_not_probed(self):
        self.assertFalse(self._by_name()["pbcopy"].probe_version)
        self.assertEqual(
            [t.name for t in tools.CLIPBOARD_TOOLS if not t.probe_version],
            ["pbcopy"],
        )

    def test_a_probe_of_it_never_runs_it(self):
        fake = tools.ExternalTool("pbcopy", frozenset(), probe_version=False)
        with (
            mock.patch("pyguitest.tools.shutil.which", return_value="/usr/bin/pbcopy"),
            mock.patch("pyguitest.tools.subprocess.run") as run,
        ):
            self.assertIsNone(fake.version())
            run.assert_not_called()


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
        fake = tools.ExternalTool(
            REAL_BINARY, frozenset(), also_needs="definitely-not-real"
        )
        self.assertIsNotNone(fake.path())  # the binary really is there
        self.assertFalse(fake.present)  # the second half is not

    def test_present_is_unaffected_when_there_is_no_second_half(self):
        fake = tools.ExternalTool(REAL_BINARY, frozenset())
        self.assertTrue(fake.present)


class TestImageSearchTools(unittest.TestCase):
    """Two ImageMagick entry points, and which one a machine has is platform.

    `compare`/`identify` are separate commands that a Linux or macOS package
    installs. The `winget` install on Windows -- the one docs/install.md and
    `pyguitest doctor` both name -- lays down ImageMagick 7 with `magick.exe`
    and no legacy commands at all, verified live on Windows 11 build 26200,
    so `magick` has to be selectable or that machine has no IMAGE_LOCATE while
    the package sits there installed.
    """

    def test_both_entry_points_are_registered(self):
        self.assertEqual([t.name for t in tools.IMAGE_TOOLS], ["compare", "magick"])

    def test_both_serve_image_locate(self):
        for tool in tools.IMAGE_TOOLS:
            with self.subTest(tool=tool.name):
                self.assertEqual(
                    tool.capabilities, frozenset({Capability.IMAGE_LOCATE})
                )

    def test_magick_alone_is_selected_when_it_is_all_there_is(self):
        def only_magick(name, *args, **kwargs):
            return "/usr/bin/magick" if name == "magick" else None

        with mock.patch("pyguitest.tools.shutil.which", side_effect=only_magick):
            self.assertEqual(tools.best(tools.IMAGE_TOOLS).name, "magick")

    def test_compare_still_wins_where_both_are_installed(self):
        # The registry order is the preference order, and `compare` holds the
        # first place it has always held: adding `magick` must not change what
        # a Linux or macOS machine already selected.
        with mock.patch("pyguitest.tools.shutil.which", return_value="/usr/bin/x"):
            self.assertEqual(tools.best(tools.IMAGE_TOOLS).name, "compare")


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
        fake = tools.ExternalTool(REAL_BINARY, frozenset(), mutter_incompatible=True)
        self.assertEqual(
            tools.discover([fake], allow_mutter_incompatible=True), (fake,)
        )
        self.assertEqual(tools.discover([fake], allow_mutter_incompatible=False), ())
        # wlroots_only=False by default: the wlroots filter must not also
        # catch this tool as a side effect of the other one being set.
        self.assertEqual(tools.discover([fake], allow_wlroots_only=False), (fake,))
