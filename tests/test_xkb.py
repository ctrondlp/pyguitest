"""Tests for the XKB keymap binding.

Where libxkbcommon and xkeyboard-config are both present these run against
*real* compiled keymaps -- a US one and a French AZERTY one -- because that
comparison is the entire point of the module: keymap safety means 'a'
resolves to a different physical key on AZERTY than on QWERTY, and only a
real keymap can demonstrate that. They skip cleanly where the library or
its data files are missing.
"""

from __future__ import annotations

import ctypes
import unittest

from pyguitest import xkb

# evdev codes, from <linux/input-event-codes.h>.
KEY_A = 30
KEY_Q = 16
KEY_W = 17
KEY_Z = 44
KEY_1 = 2
KEY_2 = 3
KEY_0 = 11
KEY_ENTER = 28
KEY_LEFTSHIFT = 42
KEY_RIGHTALT = 100


def _real_keymap_text(layout):
    """Compile a real keymap for `layout` via xkeyboard-config, or None."""
    try:
        lib = ctypes.CDLL("libxkbcommon.so.0")
    except OSError:
        return None

    class Names(ctypes.Structure):
        _fields_ = [
            ("rules", ctypes.c_char_p),
            ("model", ctypes.c_char_p),
            ("layout", ctypes.c_char_p),
            ("variant", ctypes.c_char_p),
            ("options", ctypes.c_char_p),
        ]

    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_get_as_string.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.xkb_keymap_get_as_string.restype = ctypes.c_char_p
    context = lib.xkb_context_new(0)
    if not context:
        return None
    names = Names(None, None, layout.encode(), None, None)
    keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
    if not keymap:
        return None
    return lib.xkb_keymap_get_as_string(keymap, 1).decode()


class RealKeymapTestCase(unittest.TestCase):
    layout = "us"

    @classmethod
    def setUpClass(cls):
        if not xkb.available():
            raise unittest.SkipTest("libxkbcommon.so.0 is not available")
        text = _real_keymap_text(cls.layout)
        if text is None:
            raise unittest.SkipTest(
                f"could not compile a real '{cls.layout}' keymap "
                "(xkeyboard-config data missing?)"
            )
        cls.keymap = xkb.Keymap(text)

    @classmethod
    def tearDownClass(cls):
        keymap = getattr(cls, "keymap", None)
        if keymap is not None:
            keymap.close()


class TestUSLayout(RealKeymapTestCase):
    layout = "us"

    def test_plain_letter_needs_no_modifier(self):
        self.assertEqual(self.keymap.for_char("a"), (KEY_A, ()))

    def test_a_digit_is_unshifted(self):
        self.assertEqual(self.keymap.for_char("1"), (KEY_1, ()))

    def test_shifted_symbol_reports_the_shift_key(self):
        self.assertEqual(self.keymap.for_char("!"), (KEY_1, (KEY_LEFTSHIFT,)))

    def test_uppercase_uses_shift(self):
        keycode, modifiers = self.keymap.for_char("A")
        self.assertEqual(keycode, KEY_A)
        self.assertIn(KEY_LEFTSHIFT, modifiers)

    def test_named_keys_resolve(self):
        self.assertEqual(self.keymap.for_name("Return"), (KEY_ENTER, ()))

    def test_a_character_the_layout_cannot_produce_is_none(self):
        # Honest failure: US has no key for this, and guessing one would put
        # the wrong character on screen with no way for a caller to tell.
        self.assertIsNone(self.keymap.for_char("é"))

    def test_unknown_key_name_is_none(self):
        self.assertIsNone(self.keymap.for_name("NoSuchKeyName"))

    def test_a_newline_types_enter_rather_than_linefeed(self):
        # Regression, and the reason keysym_for_char does not simply
        # forward to libxkbcommon: xkb_utf32_to_keysym(0x0A) answers
        # XKB_KEY_Linefeed, which really is on this keymap (evdev 101,
        # KEY_LINEFEED). So the lookup *succeeded* and typing "\n"
        # pressed a key no physical keyboard has and no application
        # treats as Enter -- a silent wrong answer, not a failure.
        self.assertEqual(self.keymap.for_char("\n"), (KEY_ENTER, ()))
        self.assertEqual(self.keymap.for_char("\n"), self.keymap.for_name("Return"))

    def test_the_other_control_characters_need_no_help(self):
        # libxkbcommon already answers these correctly; asserted so that a
        # future table cannot silently start disagreeing with it.
        self.assertEqual(self.keymap.for_char("\r"), (KEY_ENTER, ()))
        self.assertEqual(self.keymap.for_char("\t"), self.keymap.for_name("Tab"))
        self.assertEqual(self.keymap.for_char("\b"), self.keymap.for_name("BackSpace"))
        self.assertEqual(self.keymap.for_char("\x1b"), self.keymap.for_name("Escape"))


class TestFrenchLayout(RealKeymapTestCase):
    """The whole justification for this module.

    On AZERTY the letters sit on different physical keys, so a hardcoded
    US table -- what uinput.py and ydotool use -- types the wrong text.
    """

    layout = "fr"

    def test_a_is_on_the_physical_q_key(self):
        self.assertEqual(self.keymap.for_char("a"), (KEY_Q, ()))

    def test_q_is_on_the_physical_a_key(self):
        self.assertEqual(self.keymap.for_char("q"), (KEY_A, ()))

    def test_z_and_w_are_swapped_too(self):
        self.assertEqual(self.keymap.for_char("z")[0], KEY_W)
        self.assertEqual(self.keymap.for_char("w")[0], KEY_Z)

    def test_digits_need_shift_on_azerty(self):
        keycode, modifiers = self.keymap.for_char("1")
        self.assertEqual(keycode, KEY_1)
        self.assertIn(KEY_LEFTSHIFT, modifiers)

    def test_altgr_symbols_use_a_conventional_modifier_key(self):
        # '@' is AltGr+0 here. The keymap also binds ISO_Level3_Shift to a
        # synthetic <LVL3> keycode no keyboard has; preferring RIGHTALT is
        # what _CONVENTIONAL_MODIFIERS is for.
        keycode, modifiers = self.keymap.for_char("@")
        self.assertEqual(keycode, KEY_0)
        self.assertEqual(modifiers, (KEY_RIGHTALT,))

    def test_accented_letters_are_reachable_here(self):
        self.assertIsNotNone(self.keymap.for_char("é"))

    def test_a_newline_still_types_enter_on_a_non_us_layout(self):
        # The letters move between layouts; Enter does not, and the
        # newline fix must not have hardcoded a US keycode to get there.
        self.assertEqual(self.keymap.for_char("\n"), (KEY_ENTER, ()))


class TestAvailability(unittest.TestCase):
    def test_available_reports_a_bool(self):
        self.assertIsInstance(xkb.available(), bool)

    def test_a_keymap_that_does_not_compile_raises(self):
        if not xkb.available():
            self.skipTest("libxkbcommon.so.0 is not available")
        with self.assertRaises(RuntimeError):
            xkb.Keymap("this is not a keymap")


if __name__ == "__main__":
    unittest.main()
