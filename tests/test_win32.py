"""The Windows key tables, as data.

No Windows machine: virtual-key codes and keysym spellings are fixed by the
documentation, so what is worth checking is that the tables agree with each
other and that they cover every key this package can already name -- the
property that decides whether `press_key("Return")` can work on Windows. The
declared structures are checked here too, for the same reason: `ctypes` computes
their offsets from the field types, so a transcription error is visible on this
machine rather than only on a Windows one.
"""

import ctypes
import os
import subprocess
import sys
import unittest
from pathlib import Path

from pyguitest.backends import _winapi
from pyguitest.backends.base import _SENDKEYS_SHIFTED, GUIBackend
from pyguitest.backends.win32 import (
    _EXTENDED_VK,
    _KEY_NAMES_BY_VK,
    _KEYSYM_VK,
    _MODIFIER_KEYS,
    _SENDKEYS_LONG_NAMES,
    VK,
    Win32Backend,
    key_name_for_virtual_key,
    key_names_for_virtual_key,
    virtual_key_code,
)


def merged_aliases():
    """`KEY_ALIASES` with the Windows long spellings added, as the backend has it.

    Recomputed from the two source tables rather than read off the class, so a
    backend that quietly stopped merging one of them fails here instead of
    agreeing with itself; the equality with the class attribute is asserted
    separately, and it is the half that keeps the recomputation honest.
    """
    merged = dict(GUIBackend.KEY_ALIASES)
    merged.update(
        {
            long_name: GUIBackend.KEY_ALIASES[short]
            for long_name, short in _SENDKEYS_LONG_NAMES.items()
        }
    )
    return merged


class TestTheVirtualKeyTable(unittest.TestCase):
    def test_codes_are_unique(self):
        self.assertEqual(len(set(VK.values())), len(VK))

    def test_codes_are_in_the_range_a_virtual_key_occupies(self):
        for name, code in VK.items():
            with self.subTest(name=name):
                self.assertGreater(code, 0)
                self.assertLess(code, 0xFF)

    def test_the_runs_windows_defines_are_where_windows_puts_them(self):
        self.assertEqual(VK["VK_F1"], 0x70)
        self.assertEqual(VK["VK_F24"], 0x87)
        self.assertEqual(VK["VK_NUMPAD0"], 0x60)
        self.assertEqual(VK["VK_NUMPAD9"], 0x69)

    def test_the_side_agnostic_modifiers_are_the_codes_windows_defines(self):
        # The three `_SIDELESS_VK` redirects `is_key_pressed` asks
        # `GetAsyncKeyState` about, and the only VK_* constants that live in
        # `_winapi` rather than in `VK`. VK_MENU was transcribed as 0x18 --
        # VK_FINAL, an IME state key -- which no other test could see, because
        # nothing else reads these three and pressing Alt goes through
        # VK_LMENU/VK_RMENU instead. The symptom was an Alt query that
        # answered False with Alt held down.
        self.assertEqual(_winapi.VK_SHIFT, 0x10)
        self.assertEqual(_winapi.VK_CONTROL, 0x11)
        self.assertEqual(_winapi.VK_MENU, 0x12)


class TestTheKeysymTable(unittest.TestCase):
    def test_every_value_is_a_key_the_virtual_key_table_defines(self):
        for keysym, name in _KEYSYM_VK.items():
            with self.subTest(keysym=keysym):
                self.assertIn(name, VK)

    def test_keys_are_lowercased_because_that_is_how_they_are_looked_up(self):
        for keysym in _KEYSYM_VK:
            with self.subTest(keysym=keysym):
                self.assertEqual(keysym, keysym.lower())

    def test_every_key_name_this_package_can_emit_has_a_virtual_key(self):
        # `KEY_ALIASES` is what send_keys resolves `{BAC}`, `{ENT}` and the
        # rest to, and each of those values is a name press_key has to find a
        # Windows key for. Without this, a Windows backend would raise
        # "unknown key name" on a key every recorded script uses.
        for name in sorted(set(GUIBackend.KEY_ALIASES.values())):
            with self.subTest(name=name):
                self.assertIn(name.lower(), _KEYSYM_VK)

    def test_print_is_print_screen_rather_than_a_printer_key(self):
        self.assertEqual(_KEYSYM_VK["print"], "VK_SNAPSHOT")

    def test_altgr_is_the_right_alt_key(self):
        # Windows has no AltGr key: the keysym is the right Alt, which the
        # layout combines with Ctrl.
        self.assertEqual(_KEYSYM_VK["iso_level3_shift"], "VK_RMENU")


class TestThePublicKeyVocabulary(unittest.TestCase):
    """The translation in both directions, which is what a caller outside here needs.

    A Windows input hook reports a virtual-key *code* and a generated script has
    to say a *name*. Both directions are public because that boundary belongs to
    this module: a caller reaching into `_KEYSYM_VK` for the reverse one would be
    depending on a private table.
    """

    def test_every_keysym_the_table_holds_resolves_to_its_own_code(self):
        for keysym, name in _KEYSYM_VK.items():
            with self.subTest(keysym=keysym):
                self.assertEqual(virtual_key_code(keysym), VK[name])

    def test_the_spellings_a_script_writes_resolve(self):
        self.assertEqual(virtual_key_code("Return"), VK["VK_RETURN"])
        self.assertEqual(virtual_key_code("BackSpace"), VK["VK_BACK"])
        self.assertEqual(virtual_key_code("F5"), VK["VK_F5"])
        self.assertEqual(virtual_key_code("space"), VK["VK_SPACE"])
        self.assertEqual(virtual_key_code("a"), VK["VK_A"])
        self.assertEqual(virtual_key_code("7"), VK["VK_7"])
        self.assertEqual(virtual_key_code("kp_5"), VK["VK_NUMPAD5"])

    def test_a_virtual_key_name_resolves_to_its_code(self):
        # Accepted because it costs nothing: the tables hold these names, so a
        # caller that speaks Windows' vocabulary rather than X11's is understood.
        self.assertEqual(virtual_key_code("VK_RETURN"), VK["VK_RETURN"])
        self.assertEqual(virtual_key_code("vk_return"), VK["VK_RETURN"])

    def test_a_control_character_resolves_to_the_key_it_means(self):
        self.assertEqual(virtual_key_code("\n"), VK["VK_RETURN"])
        self.assertEqual(virtual_key_code("\t"), VK["VK_TAB"])

    def test_a_character_that_is_no_keysym_name_is_an_unknown_name(self):
        # The character route belongs to the backend, and it is where a shifted
        # character is refused rather than downgraded; this answers about the
        # vocabulary alone.
        for key in ("!", "?", "\u00e9", "NoSuchKey"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    virtual_key_code(key)

    def test_a_printable_character_with_a_key_name_of_its_own_is_not_a_name(self):
        # `-` and ` ` are keys rather than characters here: their keysym names
        # are `minus` and `space`. `press_key` takes the character as well,
        # through the SendKeys table; this lookup does not, which is what the
        # refusal it raises says.
        for char, name in (("-", "minus"), (" ", "space")):
            with self.subTest(char=char):
                with self.assertRaises(ValueError) as caught:
                    virtual_key_code(char)
                self.assertIn("press_key", str(caught.exception))
                self.assertEqual(virtual_key_code(name), VK[_KEYSYM_VK[name]])

    def test_the_backend_resolves_names_exactly_as_the_module_does(self):
        # `self` is None because none of these names reaches the character
        # route, which is the only part of `_virtual_key` that uses the backend.
        for key in ("Return", "BackSpace", "F5", "space", "a", "7", "kp_5", "\n"):
            with self.subTest(key=key):
                self.assertEqual(
                    Win32Backend._virtual_key(None, key), virtual_key_code(key)
                )

    def test_a_code_resolves_to_a_name_a_script_can_say(self):
        self.assertEqual(key_name_for_virtual_key(VK["VK_RETURN"]), "return")
        self.assertEqual(key_name_for_virtual_key(VK["VK_F5"]), "f5")
        self.assertEqual(key_name_for_virtual_key(VK["VK_A"]), "a")
        self.assertEqual(key_name_for_virtual_key(VK["VK_OEM_MINUS"]), "minus")

    def test_a_code_this_package_cannot_name_answers_nothing(self):
        # VK_KANA (0x15) is a real Windows virtual key with no keysym
        # counterpart here, and an empty answer is how a caller finds that out
        # rather than being handed a name that would not replay.
        self.assertEqual(key_names_for_virtual_key(0x15), ())
        self.assertIsNone(key_name_for_virtual_key(0x15))

    def test_the_five_codes_with_two_names_answer_both_canonical_first(self):
        for code, expected in (
            (VK["VK_RETURN"], ("return", "kp_enter")),
            (VK["VK_LWIN"], ("meta_l", "super_l")),
            (VK["VK_RWIN"], ("meta_r", "super_r")),
            (VK["VK_CANCEL"], ("cancel", "break")),
            (VK["VK_RMENU"], ("alt_r", "iso_level3_shift")),
        ):
            with self.subTest(code=code):
                self.assertEqual(key_names_for_virtual_key(code), expected)

    def test_the_pairs_written_down_are_every_pair_in_the_table(self):
        # The five above are only honest if they are all of them. `VK_RWIN` was
        # in the table, in neither this list nor the prose that counts it --
        # and every other assertion here still passed, because a test can only
        # find the pairs somebody remembered to write down.
        with_two_names = {
            code for code, names in _KEY_NAMES_BY_VK.items() if len(names) > 1
        }
        self.assertEqual(
            with_two_names,
            {
                VK["VK_RETURN"],
                VK["VK_LWIN"],
                VK["VK_RWIN"],
                VK["VK_CANCEL"],
                VK["VK_RMENU"],
            },
        )

    def test_the_reverse_table_holds_exactly_the_names_the_codes_have(self):
        expected = {}
        for keysym, name in _KEYSYM_VK.items():
            expected.setdefault(VK[name], []).append(keysym)
        self.assertEqual(
            _KEY_NAMES_BY_VK, {code: tuple(n) for code, n in expected.items()}
        )

    def test_the_two_directions_round_trip(self):
        # The property that makes this a translation rather than two tables: a
        # name read back off a code has to press that same key again.
        for code, names in _KEY_NAMES_BY_VK.items():
            with self.subTest(code=code):
                self.assertEqual(virtual_key_code(names[0]), code)
                self.assertEqual(virtual_key_code(key_name_for_virtual_key(code)), code)

    def test_the_module_offers_them_as_its_own_api(self):
        from pyguitest.backends import win32

        for name in (
            "virtual_key_code",
            "key_names_for_virtual_key",
            "key_name_for_virtual_key",
        ):
            with self.subTest(name=name):
                self.assertIn(name, win32.__all__)


class TestTheModifierKeys(unittest.TestCase):
    def test_the_characters_are_the_ones_send_keys_defines(self):
        self.assertEqual(set(_MODIFIER_KEYS), set(GUIBackend.MODIFIER_KEYS))

    def test_every_modifier_is_a_virtual_key(self):
        for name in _MODIFIER_KEYS.values():
            with self.subTest(name=name):
                self.assertIn(name, VK)

    def test_meta_is_the_windows_key(self):
        self.assertEqual(_MODIFIER_KEYS["#"], "VK_LWIN")

    def test_altgr_is_the_right_alt_key_here_too(self):
        # Which is exactly why an `&(...)` group cannot be handled like the
        # other four: the key alone does not mean AltGr on Windows.
        self.assertEqual(_MODIFIER_KEYS["&"], "VK_RMENU")


class TestTheExtendedKeySet(unittest.TestCase):
    def test_every_member_is_a_virtual_key(self):
        for name in _EXTENDED_VK:
            with self.subTest(name=name):
                self.assertIn(name, VK)

    def test_the_arrow_and_navigation_keys_are_extended(self):
        for name in (
            "VK_LEFT",
            "VK_UP",
            "VK_RIGHT",
            "VK_DOWN",
            "VK_HOME",
            "VK_END",
            "VK_INSERT",
            "VK_DELETE",
            "VK_PRIOR",
            "VK_NEXT",
            "VK_RCONTROL",
            "VK_RMENU",
            "VK_LWIN",
            "VK_RWIN",
        ):
            with self.subTest(name=name):
                self.assertIn(name, _EXTENDED_VK)

    def test_enter_is_not_extended_because_both_enters_share_its_key(self):
        # The main Enter and the numeric keypad's are one virtual key; only
        # the scan code separates them, which is what this table cannot say.
        self.assertNotIn("VK_RETURN", _EXTENDED_VK)

    def test_the_two_irregular_sequences_are_left_out(self):
        # Print Screen and Pause have multi-byte sequences rather than a
        # prefix, so claiming them here would be a guess.
        self.assertNotIn("VK_SNAPSHOT", _EXTENDED_VK)
        self.assertNotIn("VK_PAUSE", _EXTENDED_VK)

    def test_no_function_key_is_extended(self):
        self.assertFalse(any(name.startswith("VK_F") for name in _EXTENDED_VK))


class TestTheSendKeysLongNames(unittest.TestCase):
    def test_every_long_name_is_a_second_name_for_a_short_one(self):
        for long_name, short in _SENDKEYS_LONG_NAMES.items():
            with self.subTest(name=long_name):
                self.assertIn(short, GUIBackend.KEY_ALIASES)

    def test_the_merge_keeps_every_short_name_intact(self):
        merged = merged_aliases()
        for short, name in GUIBackend.KEY_ALIASES.items():
            with self.subTest(short=short):
                self.assertEqual(merged[short], name)

    def test_the_long_forms_resolve_to_the_same_keys(self):
        merged = merged_aliases()
        self.assertEqual(merged["ENTER"], merged["ENT"])
        self.assertEqual(merged["BACKSPACE"], merged["BAC"])
        self.assertEqual(merged["PGUP"], merged["PGU"])
        self.assertGreater(len(merged), len(GUIBackend.KEY_ALIASES))

    def test_the_backend_uses_exactly_that_merge(self):
        # The two halves of the claim: the merge above is recomputed from the
        # tables, and the class attribute is what the backend actually looks up
        # through when `send_keys` resolves a `{BAC}`-style name.
        self.assertEqual(Win32Backend.KEY_ALIASES, merged_aliases())

    def test_no_long_name_is_a_single_character(self):
        # `~` is the case this guards. It is Enter in Windows' own dialect and
        # a printable tilde here, and an alias table that took it would break
        # typing one.
        for long_name in _SENDKEYS_LONG_NAMES:
            with self.subTest(name=long_name):
                self.assertGreater(len(long_name), 1)

    def test_the_tilde_stays_a_character(self):
        self.assertEqual(_SENDKEYS_SHIFTED["~"], "grave")
        self.assertNotIn("~", _SENDKEYS_LONG_NAMES)
        self.assertNotIn("~", merged_aliases())


class TestTheDeclaredLayout(unittest.TestCase):
    """The structures, where a transcription error is visible on any machine.

    `ctypes` computes offsets from the declared field types, so a mistake in
    `_winapi`'s transcription shows up as a wrong size here -- 56 bytes instead
    of 40 was the real one, from `ctypes.wintypes`' eight-byte Linux `LONG` --
    rather than as garbage handed to `SendInput` on a machine nobody is looking
    at. That is why every field is declared as a fixed-width alias.
    """

    POINTER = ctypes.sizeof(ctypes.c_void_p)

    def test_input_is_the_documented_size_for_this_abi(self):
        # 40 bytes on 64-bit Windows, 28 on 32-bit.
        self.assertEqual(ctypes.sizeof(_winapi.INPUT), 40 if self.POINTER == 8 else 28)

    def test_the_union_follows_the_type_field_at_its_aligned_offset(self):
        # The union contains a pointer, so it is 8-byte aligned where pointers
        # are 8 bytes and 4-byte aligned where they are not.
        self.assertEqual(_winapi.INPUT.type.offset, 0)
        self.assertEqual(_winapi.INPUT.u.offset, 8 if self.POINTER == 8 else 4)

    def test_each_event_kind_starts_with_the_field_its_semantics_need(self):
        self.assertEqual(_winapi.MOUSEINPUT.dx.offset, 0)
        self.assertEqual(_winapi.MOUSEINPUT.dy.offset, 4)
        self.assertEqual(_winapi.MOUSEINPUT.mouseData.offset, 8)
        self.assertEqual(_winapi.MOUSEINPUT.dwFlags.offset, 12)
        self.assertEqual(_winapi.KEYBDINPUT.wVk.offset, 0)
        self.assertEqual(_winapi.KEYBDINPUT.wScan.offset, 2)
        self.assertEqual(_winapi.KEYBDINPUT.dwFlags.offset, 4)

    def test_the_anonymous_union_lets_a_caller_name_the_member_directly(self):
        # Which is the difference between writing `event.ki.wVk` and
        # `event.u.ki.wVk` at every call site.
        event = _winapi.INPUT()
        event.ki.wVk = 0x0D
        self.assertEqual(event.u.ki.wVk, 0x0D)

    def test_a_wide_character_is_one_code_unit(self):
        self.assertEqual(ctypes.sizeof(_winapi.WCHAR), 2)
        self.assertEqual(ctypes.sizeof(_winapi.INT32), 4)
        self.assertEqual(ctypes.sizeof(_winapi.UINT32), 4)
        self.assertEqual(ctypes.sizeof(_winapi.UINT16), 2)

    def test_a_device_name_is_read_as_utf16_rather_than_wchar_t(self):
        # `ctypes.wstring_at` would read four bytes per character on Linux,
        # which is how a test of the monitor name could pass on Windows and
        # fail everywhere else.
        buffer = (_winapi.WCHAR * 32)()
        for position, char in enumerate(r"\\.\DISPLAY1"):
            buffer[position] = ord(char)
        self.assertEqual(_winapi.wide_string(buffer), r"\\.\DISPLAY1")


_PROBE = "from pyguitest import backends; print(','.join(backends.available()))"
"""What a process that has only imported the package registers.

Run in a subprocess rather than read out of the live registry, because
tests/test_backends.py swaps that registry out to exercise composition and
sometimes does not put it back -- and the claim under test is about what
*importing the package* does, which a fresh process is the only place with an
unpolluted answer to.
"""


class TestRegistration(unittest.TestCase):
    """Both Windows backends are registered; neither claims this machine.

    The phase-0 test asserted that both names were absent -- the honest answer
    while the backends did not exist. What it goes on asserting is the part that
    mattered and still does: a backend may only claim a session it can drive, so
    on a host with no user32.dll and no UIAutomationCore.dll both factories
    answer None and automatic composition is left exactly as it was.
    """

    def fresh_registry(self):
        """`available()` as a process that has only imported the package sees it."""
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [name for name in result.stdout.strip().split(",") if name]

    def test_the_win32_backend_is_registered(self):
        self.assertIn("win32", self.fresh_registry())

    def test_the_uia_backend_is_registered(self):
        # The element half, at priority 90 beside atspi: nothing here proves it
        # works against Windows, only that importing the package offers it.
        self.assertIn("uia", self.fresh_registry())

    def test_the_factory_answers_for_the_machine_it_is_actually_on(self):
        # Off Windows there is no user32.dll, so automatic composition must
        # see None and be left exactly as it was. *On* Windows the same
        # factory has to build something -- asserting None unconditionally
        # made this a test of the developer's laptop rather than of the
        # factory, and it failed on the first real Windows 11 run for the one
        # reason that is not a bug: the backend was available.
        from pyguitest.backends import _win32_factory

        backend = _win32_factory(None)
        if _winapi.available():
            self.assertIsInstance(backend, Win32Backend)
        else:
            self.assertIsNone(backend)

    def test_the_element_factory_answers_for_the_machine_it_is_actually_on(self):
        # The same, one DLL further: `uia.available()` is a type-library load,
        # so this is None wherever comtypes is missing or UIAutomationCore.dll
        # will not register -- which is every non-Windows host, and a Windows
        # host without the `windows` extra.
        from pyguitest.backends import _uia_factory
        from pyguitest.backends import uia as _uia

        backend = _uia_factory(None)
        if _uia.available():
            self.assertIsNotNone(backend)
        else:
            self.assertIsNone(backend)


if __name__ == "__main__":
    unittest.main()
