"""uinput backend tests.

Runs against a fake evdev when the library is absent, and against the real one
when installed -- the backend takes an injected device, so the real `ecodes`
constants are exercised without touching /dev/uinput.
"""

import sys
import types
import unittest
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import BackendUnavailable, PyGUITestError


class FakeDevice:
    def __init__(self):
        self.events = []
        self.syncs = 0
        self.closed = False

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.syncs += 1

    def close(self):
        self.closed = True


def fake_evdev():
    ecodes = types.SimpleNamespace(
        EV_KEY=1,
        EV_REL=2,
        EV_ABS=3,
        ABS_X=0,
        ABS_Y=1,
        REL_WHEEL=8,
        BTN_LEFT=272,
        BTN_MIDDLE=274,
        BTN_RIGHT=273,
        KEY_ESC=1,
        KEY_MAX=249,
        keys={},
        KEY_LEFTSHIFT=42,
        KEY_A=30,
        KEY_B=48,
        KEY_1=2,
        KEY_SPACE=57,
        KEY_MINUS=12,
        KEY_SEMICOLON=39,
    )
    module = types.ModuleType("evdev")
    module.ecodes = ecodes
    module.UInput = lambda *a, **kw: FakeDevice()
    module.AbsInfo = lambda **kw: kw
    return mock.patch.dict(sys.modules, {"evdev": module})


class UinputTestCase(unittest.TestCase):
    def setUp(self):
        patcher = fake_evdev()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import uinput

        self.device = FakeDevice()
        self.gui = uinput.UinputBackend(screen_size=(1920, 1080), device=self.device)


class TestPointer(UinputTestCase):
    def test_absolute_motion_writes_both_axes_then_syncs(self):
        self.gui.move_mouse(100, 200)
        codes = [(c, v) for _, c, v in self.device.events]
        self.assertIn((0, 100), codes)  # ABS_X
        self.assertIn((1, 200), codes)  # ABS_Y
        self.assertEqual(self.device.syncs, 1)

    def test_button_press_and_release(self):
        self.gui.press_button(1)
        self.gui.release_button(1)
        values = [v for _, c, v in self.device.events if c == 272]
        self.assertEqual(values, [1, 0])

    def test_unsupported_button_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_button(9)

    def test_scroll_uses_a_relative_wheel_axis(self):
        self.gui.scroll(dy=1)
        self.assertEqual(self.device.events[-1], (2, 8, 1))


class TestKeyboard(UinputTestCase):
    def test_key_names_are_normalised(self):
        self.gui.press_key("a")
        self.gui.press_key("KEY_B")
        codes = [c for _, c, _ in self.device.events]
        self.assertEqual(codes, [30, 48])

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_key("nonexistent")

    def test_typing_wraps_capitals_in_shift(self):
        with self.assertWarns(Warning):
            self.gui.type_text("aB")
        codes = [(c, v) for _, c, v in self.device.events]
        self.assertIn((42, 1), codes)  # shift down for 'B'
        self.assertIn((42, 0), codes)  # shift up

    def test_typing_is_keymap_unsafe_and_can_be_refused(self):
        # Same trap as ydotool: injection happens below the compositor.
        self.assertFalse(self.gui.keymap_safe)
        with self.assertRaises(PyGUITestError):
            self.gui.type_text("hello", allow_keymap_unsafe=False)

    def test_untypeable_character_is_reported(self):
        with self.assertWarns(Warning):
            with self.assertRaises(PyGUITestError):
                self.gui.type_text("é")


class TestSendKeysMapping(UinputTestCase):
    """The tables send_keys() reads to drive this backend.

    Evdev names, not X11 keysym names -- the point of overriding them at all.
    """

    def test_resolve_char_key_delegates_to_the_type_text_table(self):
        self.assertEqual(self.gui.resolve_char_key("a"), ("A", False))
        self.assertEqual(self.gui.resolve_char_key("A"), ("A", True))
        self.assertEqual(self.gui.resolve_char_key("1"), ("1", False))
        self.assertEqual(self.gui.resolve_char_key("!"), ("1", True))

    def test_modifier_keys_are_evdev_names(self):
        self.assertEqual(
            self.gui.MODIFIER_KEYS,
            {
                "^": "LEFTCTRL",
                "%": "LEFTALT",
                "+": "LEFTSHIFT",
                "#": "LEFTMETA",
                "&": "RIGHTALT",
            },
        )

    def test_key_aliases_are_evdev_names(self):
        self.assertEqual(self.gui.KEY_ALIASES["BAC"], "BACKSPACE")
        self.assertEqual(self.gui.KEY_ALIASES["ENT"], "ENTER")
        self.assertEqual(self.gui.KEY_ALIASES["TAB"], "TAB")
        self.assertEqual(self.gui.KEY_ALIASES["F1"], "F1")

    def test_uncertain_aliases_are_left_out_rather_than_guessed(self):
        # BRE(ak)/CAN(cel)/HEL(p)/PRT have no settled evdev key on a standard
        # keyboard; send_keys should raise for them here, not press the
        # wrong thing.
        for name in ("BRE", "CAN", "HEL", "PRT"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.gui.KEY_ALIASES)


class TestLifecycle(UinputTestCase):
    def test_capabilities(self):
        for cap in (
            Capability.POINTER_MOVE,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
        ):
            self.assertIn(cap, self.gui.capabilities)
        self.assertNotIn(Capability.WINDOW_LIST, self.gui.capabilities)

    def test_close_releases_the_device(self):
        self.gui.close()
        self.assertTrue(self.device.closed)


class TestUnavailable(unittest.TestCase):
    def test_missing_evdev_refuses_with_an_install_hint(self):
        from pyguitest.backends import uinput

        with mock.patch.object(uinput, "_evdev", return_value=None):
            self.assertFalse(uinput.available())
            with self.assertRaises(BackendUnavailable) as ctx:
                uinput.UinputBackend()
            self.assertIn("pyguitest[uinput]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
