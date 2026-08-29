"""send_keys(): the ported X11::GUITest SendKeys grammar.

Exercised against a RecordingBackend rather than a real one, since the
grammar itself -- modifiers, grouping, {} aliases, repeats, PAUSE -- is
backend-independent logic sitting on top of press_key/release_key/wait.
"""

import unittest
from unittest import mock

import pyguitest
from pyguitest.backends.base import GUIBackend
from pyguitest.capabilities import Capability, CapabilitySet


class RecordingBackend(GUIBackend):
    """Records every key event in order, instead of touching a display."""

    name = "recording"

    def __init__(self):
        """Start with no recorded events."""
        self.events = []

    @property
    def capabilities(self):
        """What a keyboard-only fake backend can do."""
        return CapabilitySet({Capability.KEY_EVENT, Capability.TEXT_ENTRY})

    def press_key(self, key):
        """Record a press."""
        self.events.append(("press", key))

    def release_key(self, key):
        """Record a release."""
        self.events.append(("release", key))

    def type_text(self, text, delay=0.0, allow_keymap_unsafe=True):
        """Record a type_text call; send_keys should never reach this."""
        self.events.append(("type", text))


def session():
    """A Session over a fresh RecordingBackend."""
    return pyguitest.Session(RecordingBackend(), pyguitest.detect())


def tap(name):
    """The (press, release) pair send_keys emits for one key."""
    return [("press", name), ("release", name)]


class TestPlainText(unittest.TestCase):
    def test_lowercase_letters_need_no_shift(self):
        gui = session()
        gui.send_keys("abc")
        self.assertEqual(gui.backend.events, tap("a") + tap("b") + tap("c"))

    def test_uppercase_letter_gets_an_automatic_shift(self):
        gui = session()
        gui.send_keys("A")
        self.assertEqual(
            gui.backend.events,
            [("press", "Shift_L")] + tap("a") + [("release", "Shift_L")],
        )

    def test_tilde_and_newline_both_mean_enter(self):
        for keys in ("~", "\n"):
            with self.subTest(keys=keys):
                gui = session()
                gui.send_keys(keys)
                self.assertEqual(gui.backend.events, tap("Return"))

    def test_tab_character(self):
        gui = session()
        gui.send_keys("\t")
        self.assertEqual(gui.backend.events, tap("Tab"))


class TestModifiers(unittest.TestCase):
    def test_lone_modifier_is_pressed_and_released_alone(self):
        gui = session()
        gui.send_keys("+")
        self.assertEqual(gui.backend.events, tap("Shift_L"))

    def test_modifier_without_parens_does_not_combine_with_what_follows(self):
        # Faithful to X11::GUITest: a modifier only combines with a group.
        gui = session()
        gui.send_keys("^a")
        self.assertEqual(gui.backend.events, tap("Control_L") + tap("a"))

    def test_alt_f_then_plain_q(self):
        gui = session()
        gui.send_keys("%(f)q")
        self.assertEqual(
            gui.backend.events,
            [("press", "Alt_L")] + tap("f") + [("release", "Alt_L")] + tap("q"),
        )

    def test_uppercase_via_held_shift(self):
        gui = session()
        gui.send_keys("+(abc)")
        self.assertEqual(
            gui.backend.events,
            [("press", "Shift_L")]
            + tap("a")
            + tap("b")
            + tap("c")
            + [("release", "Shift_L")],
        )

    def test_nested_groups_combine_and_release_together(self):
        gui = session()
        gui.send_keys("^(+(l))")
        self.assertEqual(
            gui.backend.events,
            [("press", "Control_L"), ("press", "Shift_L")]
            + tap("l")
            + [("release", "Shift_L"), ("release", "Control_L")],
        )

    def test_sequential_groups(self):
        gui = session()
        gui.send_keys("%(fa)^(m)")
        self.assertEqual(
            gui.backend.events,
            [("press", "Alt_L")]
            + tap("f")
            + tap("a")
            + [("release", "Alt_L"), ("press", "Control_L")]
            + tap("m")
            + [("release", "Control_L")],
        )


class TestBraceSets(unittest.TestCase):
    def test_abbreviated_alias(self):
        gui = session()
        gui.send_keys("{BAC}")
        self.assertEqual(gui.backend.events, tap("BackSpace"))

    def test_unabbreviated_name_passes_through(self):
        gui = session()
        gui.send_keys("{BackSpace}")
        self.assertEqual(gui.backend.events, tap("BackSpace"))

    def test_alias_lookup_is_case_insensitive(self):
        gui = session()
        gui.send_keys("{bac}")
        self.assertEqual(gui.backend.events, tap("BackSpace"))

    def test_multiple_keys_in_one_brace_set(self):
        gui = session()
        gui.send_keys("{F1 F2 F3}")
        self.assertEqual(gui.backend.events, tap("F1") + tap("F2") + tap("F3"))

    def test_repeat_count(self):
        gui = session()
        gui.send_keys("{TAB 3}")
        self.assertEqual(gui.backend.events, tap("Tab") * 3)

    def test_repeat_count_after_a_literal_character(self):
        gui = session()
        gui.send_keys("{SPC 3 a b c}")
        self.assertEqual(
            gui.backend.events, tap("space") * 3 + tap("a") + tap("b") + tap("c")
        )

    def test_pause_sleeps_milliseconds(self):
        gui = session()
        with mock.patch("time.sleep") as sleep:
            gui.send_keys("{PAUSE 500}")
        sleep.assert_called_once_with(0.5)
        self.assertEqual(gui.backend.events, [])

    def test_quote_a_special_character(self):
        gui = session()
        gui.send_keys("{{}")
        self.assertEqual(
            gui.backend.events,
            [("press", "Shift_L")] + tap("bracketleft") + [("release", "Shift_L")],
        )

    def test_doubled_closing_brace_quotes_a_literal_brace(self):
        gui = session()
        gui.send_keys("{}}")
        self.assertEqual(
            gui.backend.events,
            [("press", "Shift_L")] + tap("bracketright") + [("release", "Shift_L")],
        )

    def test_shift_held_by_an_outer_group_is_not_re_added(self):
        gui = session()
        gui.send_keys("+({a b c})")
        self.assertEqual(
            gui.backend.events,
            [("press", "Shift_L")]
            + tap("a")
            + tap("b")
            + tap("c")
            + [("release", "Shift_L")],
        )

    def test_unterminated_brace_raises(self):
        gui = session()
        with self.assertRaises(ValueError):
            gui.send_keys("{TAB")

    def test_empty_brace_raises(self):
        gui = session()
        with self.assertRaises(ValueError):
            gui.send_keys("{}")

    def test_repeat_count_with_nothing_before_it_raises(self):
        gui = session()
        with self.assertRaises(ValueError):
            gui.send_keys("{3}")

    def test_zero_repeat_count_raises(self):
        gui = session()
        with self.assertRaises(ValueError):
            gui.send_keys("{TAB 0}")


class TestCombination(unittest.TestCase):
    def test_the_module_docstrings_own_example(self):
        gui = session()
        with mock.patch("time.sleep") as sleep:
            gui.send_keys("abc+(abc){TAB PAUSE 500}")
        self.assertEqual(
            gui.backend.events,
            tap("a")
            + tap("b")
            + tap("c")
            + [("press", "Shift_L")]
            + tap("a")
            + tap("b")
            + tap("c")
            + [("release", "Shift_L")]
            + tap("Tab"),
        )
        sleep.assert_called_once_with(0.5)

    def test_never_falls_back_to_type_text(self):
        gui = session()
        gui.send_keys("Hello, how are you?\n")
        self.assertNotIn("type", [kind for kind, _ in gui.backend.events])


class TestQuoteForType(unittest.TestCase):
    def test_meta_is_now_escaped(self):
        # Regression: X11::GUITest's QuoteStringForSendKeys omitted # despite
        # it being a modifier there too -- a literal # slipped through
        # unescaped. quote_for_type fixes that.
        self.assertEqual(pyguitest.quote_for_type("#"), "{#}")

    def test_altgr_is_deliberately_left_unescaped(self):
        # Matches upstream: & is the one modifier this helper does not quote.
        self.assertEqual(pyguitest.quote_for_type("&"), "&")

    def test_round_trips_through_send_keys_as_literal_text(self):
        # Every character that needs quoting also has a static key mapping,
        # so quote_for_type's output is fully consumable -- none of it is
        # read back as a modifier or a group.
        text = "Hello: ~%^(){}+#"
        gui = session()
        gui.send_keys(pyguitest.quote_for_type(text))

        backend = RecordingBackend()
        expected = []
        for char in text:
            name, needs_shift = backend.resolve_char_key(char)
            if needs_shift:
                expected += [("press", "Shift_L")]
            expected += tap(name)
            if needs_shift:
                expected += [("release", "Shift_L")]
        self.assertEqual(gui.backend.events, expected)


class TestYdotoolIntegration(unittest.TestCase):
    """send_keys() wired to a real backend, not just the fake.

    ydotool is the one whose key-name vocabulary genuinely differs (numeric
    evdev codes), so it is the case most likely to break silently.
    """

    def _session(self):
        from pyguitest import tools
        from pyguitest.backends.input import ToolInputBackend

        class Recorder:
            def __init__(self):
                self.calls = []

            def __call__(self, argv):
                self.calls.append(argv)
                return argv

        by_name = {t.name: t for t in tools.INPUT_TOOLS}
        runner = Recorder()
        backend = ToolInputBackend(by_name["ydotool"], runner=runner)
        return pyguitest.Session(backend, pyguitest.detect()), runner

    def test_ctrl_c_uses_numeric_evdev_codes(self):
        gui, runner = self._session()
        gui.send_keys("^(c)")
        self.assertEqual(
            runner.calls,
            [
                ["ydotool", "key", "29:1"],  # KEY_LEFTCTRL down
                ["ydotool", "key", "46:1"],  # KEY_C down
                ["ydotool", "key", "46:0"],  # KEY_C up
                ["ydotool", "key", "29:0"],  # KEY_LEFTCTRL up
            ],
        )

    def test_named_key_uses_numeric_code_not_x11_name(self):
        gui, runner = self._session()
        gui.send_keys("{BAC}")
        self.assertEqual(
            runner.calls,
            [["ydotool", "key", "14:1"], ["ydotool", "key", "14:0"]],
        )


if __name__ == "__main__":
    unittest.main()
