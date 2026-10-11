"""CLI input adapters: the command built, and what a failure does.

Command construction is checked closely because the tools disagree about
flags, and the rest is honesty: an adapter that cannot do something has to
say so rather than appear to have tried.
"""

import unittest
import warnings
from unittest import mock

from pyguitest import tools
from pyguitest.backends.input import KeymapWarning, ToolInputBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import BackendUnavailable, CapabilityUnsupported, PyGUITestError

BY_NAME = {t.name: t for t in tools.INPUT_TOOLS}


class Recorder:
    """Stands in for subprocess.run so argv can be asserted without a session."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return argv


class TestCommandConstruction(unittest.TestCase):
    def _backend(self, name):
        self.runner = Recorder()
        return ToolInputBackend(BY_NAME[name], runner=self.runner)

    def test_wdotool_is_xdotool_compatible(self):
        gui = self._backend("wdotool")
        gui.move_mouse(100, 200)
        gui.press_button(1)
        gui.type_text("hello")
        self.assertEqual(
            self.runner.calls,
            [
                ["wdotool", "mousemove", "--", "100", "200"],
                ["wdotool", "mousedown", "1"],
                ["wdotool", "type", "--", "hello"],
            ],
        )

    def test_type_uses_a_double_dash_so_text_is_never_read_as_flags(self):
        gui = self._backend("wdotool")
        gui.type_text("--version")
        self.assertEqual(self.runner.calls[0], ["wdotool", "type", "--", "--version"])

    def test_ydotool_uses_evdev_button_codes_not_x11_numbers(self):
        gui = self._backend("ydotool")
        gui.press_button(1)
        gui.release_button(1)
        # 0x40 is press, 0x80 release; left button is evdev 0x00.
        self.assertEqual(self.runner.calls[0], ["ydotool", "click", "0x40"])
        self.assertEqual(self.runner.calls[1], ["ydotool", "click", "0x80"])

    def test_ydotool_all_three_buttons_map_correctly(self):
        # Regression: middle and right were swapped -- X11 button 2 (middle)
        # went to ydotool's 0x01 (RIGHT), and button 3 (right) to 0x02
        # (MIDDLE). A test asserting only button 1 passed under either table.
        gui = self._backend("ydotool")
        gui.press_button(1)
        gui.press_button(2)
        gui.press_button(3)
        self.assertEqual(
            self.runner.calls,
            [
                ["ydotool", "click", "0x40"],  # left
                ["ydotool", "click", "0x42"],  # middle
                ["ydotool", "click", "0x41"],  # right
            ],
        )

    def test_ydotool_unmapped_button_is_a_typed_error(self):
        gui = self._backend("ydotool")
        with self.assertRaises(CapabilityUnsupported):
            gui.press_button(4)

    def test_ydotool_absolute_motion(self):
        gui = self._backend("ydotool")
        gui.move_mouse(5, 6)
        self.assertIn("--absolute", self.runner.calls[0])

    def test_scroll_honours_magnitude_on_both_axes(self):
        gui = self._backend("wdotool")
        gui.scroll(dx=2, dy=3)
        self.assertEqual(
            self.runner.calls[0],
            ["wdotool", "click", "--repeat", "3", "4", "click", "--repeat", "2", "7"],
        )

    def test_pure_horizontal_scroll_touches_no_vertical_button(self):
        gui = self._backend("xdotool")
        gui.scroll(dx=-1)
        self.assertEqual(
            self.runner.calls[0], ["xdotool", "click", "--repeat", "1", "6"]
        )

    def test_scroll_with_nothing_to_do_runs_no_command(self):
        gui = self._backend("wdotool")
        gui.scroll()
        self.assertEqual(self.runner.calls, [])


class TestSendKeysMapping(unittest.TestCase):
    """The tables send_keys() reads from ToolInputBackend.

    xdotool/wdotool/wtype share GUIBackend's X11-keysym-name defaults
    unmodified; ydotool alone needs numeric evdev codes, chosen per instance
    since it depends on which tool this backend wraps.
    """

    def _backend(self, name):
        return ToolInputBackend(BY_NAME[name], runner=Recorder())

    def test_xdotool_like_tools_use_the_x11_keysym_defaults(self):
        for name in ("xdotool", "wdotool", "wtype"):
            with self.subTest(tool=name):
                gui = self._backend(name)
                self.assertEqual(gui.MODIFIER_KEYS["^"], "Control_L")
                self.assertEqual(gui.KEY_ALIASES["BAC"], "BackSpace")
                self.assertEqual(gui.resolve_char_key("A"), ("a", True))

    def test_ydotool_uses_numeric_evdev_codes(self):
        gui = self._backend("ydotool")
        self.assertEqual(
            gui.MODIFIER_KEYS,
            {"^": "29", "%": "56", "+": "42", "#": "125", "&": "100"},
        )
        self.assertEqual(gui.KEY_ALIASES["BAC"], "14")
        self.assertEqual(gui.KEY_ALIASES["ENT"], "28")
        self.assertEqual(gui.KEY_ALIASES["F1"], "59")

    def test_ydotool_resolves_characters_to_numeric_codes(self):
        gui = self._backend("ydotool")
        self.assertEqual(gui.resolve_char_key("a"), ("30", False))
        self.assertEqual(gui.resolve_char_key("A"), ("30", True))
        self.assertEqual(gui.resolve_char_key("1"), ("2", False))
        self.assertEqual(gui.resolve_char_key("!"), ("2", True))

    def test_ydotool_uncertain_aliases_are_left_out(self):
        gui = self._backend("ydotool")
        for name in ("BRE", "CAN", "HEL", "PRT"):
            with self.subTest(name=name):
                self.assertNotIn(name, gui.KEY_ALIASES)


class TestCapabilityHonesty(unittest.TestCase):
    def test_wtype_advertises_no_pointer_capabilities(self):
        gui = ToolInputBackend(BY_NAME["wtype"], runner=Recorder())
        self.assertIn(Capability.TEXT_ENTRY, gui.capabilities)
        self.assertNotIn(Capability.POINTER_MOVE, gui.capabilities)
        with self.assertRaises(CapabilityUnsupported):
            gui.move_mouse(1, 1)

    def test_unmapped_tool_is_refused_rather_than_half_working(self):
        fake = tools.ExternalTool("some-future-tool", frozenset())
        with self.assertRaises(BackendUnavailable):
            ToolInputBackend(fake)


class TestWtypeModifierBatching(unittest.TestCase):
    """wtype batches a held chord into one invocation instead of one per key.

    Found live on COSMIC: send_keys("^(a)") issued as three separate wtype
    processes (press Control_L, tap a, release Control_L) typed a plain "a"
    and selected nothing, because wtype's own man page says held modifiers
    "get released automatically once the program terminates" -- the first
    process was long gone before the second even started. Every other tool
    here holds state outside the CLI process (the X server for xdotool, a
    uinput device for ydotool), so only wtype needs this.
    """

    def _backend(self):
        self.runner = Recorder()
        return ToolInputBackend(BY_NAME["wtype"], runner=self.runner)

    def test_a_held_modifier_and_key_become_one_invocation(self):
        gui = self._backend()
        gui.press_key("Control_L")
        gui.press_key("a")
        gui.release_key("a")
        gui.release_key("Control_L")
        self.assertEqual(
            self.runner.calls,
            [["wtype", "-M", "ctrl", "-P", "a", "-p", "a", "-m", "ctrl"]],
        )

    def test_a_recognized_modifier_uses_dash_m_not_dash_capital_p(self):
        # -P Control_L presses the physical key but does not register as a
        # held *modifier* the way -M ctrl does -- confirmed live on COSMIC
        # by testing both directly against the same app, one call apart.
        gui = self._backend()
        for name, short in [
            ("Control_L", "ctrl"),
            ("Shift_R", "shift"),
            ("Alt_L", "alt"),
            ("Super_L", "logo"),
            ("ISO_Level3_Shift", "altgr"),
        ]:
            with self.subTest(name=name):
                gui.press_key(name)
                gui.release_key(name)
                self.assertEqual(
                    self.runner.calls[-1], ["wtype", "-M", short, "-m", short]
                )

    def test_a_plain_tap_still_becomes_one_invocation(self):
        gui = self._backend()
        gui.press_key("a")
        gui.release_key("a")
        self.assertEqual(self.runner.calls, [["wtype", "-P", "a", "-p", "a"]])

    def test_nothing_runs_until_every_held_key_is_released(self):
        gui = self._backend()
        gui.press_key("Control_L")
        self.assertEqual(self.runner.calls, [])
        gui.press_key("Shift_L")
        self.assertEqual(self.runner.calls, [])

    def test_a_nested_group_still_batches_as_one_call(self):
        # send_keys("^(+(l))"): Ctrl-Shift-l.
        gui = self._backend()
        gui.press_key("Control_L")
        gui.press_key("Shift_L")
        gui.press_key("l")
        gui.release_key("l")
        gui.release_key("Shift_L")
        gui.release_key("Control_L")
        self.assertEqual(
            self.runner.calls,
            [
                [
                    "wtype",
                    "-M", "ctrl", "-M", "shift", "-P", "l",
                    "-p", "l", "-m", "shift", "-m", "ctrl",
                ]
            ],
        )  # fmt: skip


class TestYdotoolConstructionGate(unittest.TestCase):
    """A ydotool the command table cannot drive is refused at construction.

    Ubuntu/Debian still ship 0.1.8, whose CLI takes none of the 1.0 syntax
    the adapter emits -- found live on Pop!_OS 24.04, where it would have
    advertised pointer capabilities that exit with a usage error on every
    call. An injected runner skips the probe, which is what keeps every
    other test in this file hermetic.
    """

    class _Help:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def _construct(self, help_text, socket_exists=False, uinput_writable=False):
        with mock.patch(
            "pyguitest.backends.input.subprocess.run",
            return_value=self._Help(help_text),
        ):
            with mock.patch("os.path.exists", return_value=socket_exists):
                with mock.patch("os.access", return_value=uinput_writable):
                    with mock.patch.dict("os.environ", {}, clear=False):
                        return ToolInputBackend(BY_NAME["ydotool"])

    def test_the_pre_1_0_cli_is_refused(self):
        with self.assertRaises(BackendUnavailable) as caught:
            self._construct("Usage: mousemove [--delay <ms>] <x> <y>")
        self.assertIn("predates 1.0", str(caught.exception))

    def test_1_0_with_the_daemon_socket_constructs(self):
        backend = self._construct("-a, --absolute", socket_exists=True)
        self.assertEqual(backend.tool.name, "ydotool")

    def test_1_0_without_daemon_or_uinput_access_is_refused(self):
        with self.assertRaises(BackendUnavailable) as caught:
            self._construct("-a, --absolute")
        self.assertIn("ydotoold is not running", str(caught.exception))

    def test_1_0_without_daemon_but_writable_uinput_constructs(self):
        backend = self._construct("-a, --absolute", uinput_writable=True)
        self.assertEqual(backend.tool.name, "ydotool")

    def test_a_missing_binary_is_refused_not_crashed_on(self):
        with mock.patch(
            "pyguitest.backends.input.subprocess.run", side_effect=OSError("no ydotool")
        ):
            with self.assertRaises(BackendUnavailable):
                ToolInputBackend(BY_NAME["ydotool"])


class TestKeymapSafety(unittest.TestCase):
    def test_keymap_unsafe_tool_warns_when_typing(self):
        gui = ToolInputBackend(BY_NAME["ydotool"], runner=Recorder())
        with self.assertWarns(KeymapWarning):
            gui.type_text("hello")

    def test_keymap_unsafe_tool_can_be_refused_outright(self):
        gui = ToolInputBackend(BY_NAME["ydotool"], runner=Recorder())
        with self.assertRaises(PyGUITestError):
            gui.type_text("hello", allow_keymap_unsafe=False)

    def test_keymap_safe_tool_types_without_warning(self):
        gui = ToolInputBackend(BY_NAME["wdotool"], runner=Recorder())
        with warnings.catch_warnings():
            warnings.simplefilter("error", KeymapWarning)
            gui.type_text("hello")


class TestFailurePropagation(unittest.TestCase):
    def test_nonzero_exit_raises_rather_than_returning_a_falsy_value(self):
        # X11::GUITest returned zero here, indistinguishable from a missed
        # click. A failing tool must be an exception.
        class Failing:
            returncode = 1
            stderr = "no protocol"
            stdout = ""

        gui = ToolInputBackend(BY_NAME["wdotool"])
        gui._runner = lambda argv: gui._run(argv)
        import subprocess

        original = subprocess.run
        subprocess.run = lambda *a, **k: Failing()
        try:
            with self.assertRaises(PyGUITestError) as ctx:
                gui.move_mouse(1, 1)
            self.assertIn("no protocol", str(ctx.exception))
        finally:
            subprocess.run = original

    def test_falls_back_to_stdout_when_stderr_is_empty(self):
        # Live regression: ydotool failed with an empty stderr, and the
        # exception said "no output" even though the real reason was sitting
        # on stdout the whole time.
        class FailingQuietStderr:
            returncode = 2
            stderr = ""
            stdout = "ydotool: could not connect to ydotoold"

        gui = ToolInputBackend(BY_NAME["wdotool"])
        gui._runner = lambda argv: gui._run(argv)
        import subprocess

        original = subprocess.run
        subprocess.run = lambda *a, **k: FailingQuietStderr()
        try:
            with self.assertRaises(PyGUITestError) as ctx:
                gui.move_mouse(1, 1)
            self.assertIn("could not connect to ydotoold", str(ctx.exception))
        finally:
            subprocess.run = original

    def test_no_output_at_all_still_says_so(self):
        class FailingSilently:
            returncode = 1
            stderr = ""
            stdout = ""

        gui = ToolInputBackend(BY_NAME["wdotool"])
        gui._runner = lambda argv: gui._run(argv)
        import subprocess

        original = subprocess.run
        subprocess.run = lambda *a, **k: FailingSilently()
        try:
            with self.assertRaises(PyGUITestError) as ctx:
                gui.move_mouse(1, 1)
            self.assertIn("no output", str(ctx.exception))
        finally:
            subprocess.run = original


if __name__ == "__main__":
    unittest.main()
