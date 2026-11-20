"""One key vocabulary, honoured by every backend that translates it.

`press_key`/`tap_key` take backend-native key names, and this package's
documented vocabulary is the X11 keysym spelling: `GUIBackend.KEY_ALIASES`
maps every brace name (`{ESC}`) to one, and each non-X11 backend carries a
table translating those into its own names -- `_KEYSYM_VK` on Windows,
`_X11_KEYSYMS` on macOS, `_KEYSYM_NAMES` for evdev.

A name missing from one of those tables is not a local problem: the same
script then works on one box and raises on the next, at the far end of a
run rather than at the call. That is how `press_key("Prior")` came to work
on X11, uinput and Windows and refuse on macOS, where `{PGU}` had reached
the key all along.

Pure table lookups, so this runs on every platform and needs no desktop --
which is the point. It is the gate that was missing when the gap went in.
"""

import unittest

from pyguitest.backends import macquartz, win32
from pyguitest.backends.base import GUIBackend

VOCABULARY = sorted(set(GUIBackend.KEY_ALIASES.values()))
"""Every key name this package documents, as the brace aliases expand."""

KEYS_NO_MAC_KEYBOARD_HAS = frozenset(
    {
        "Break",
        "Cancel",
        "Insert",
        "Menu",
        "Num_Lock",
        "Print",
        "Scroll_Lock",
    }
)
"""The names macOS refuses on purpose, because the hardware has no such key.

`macquartz` declines rather than aliasing them onto something else -- its own
rule, that a refusal a caller can see beats a silent change of meaning, and
the refusal is a typed `CapabilityUnsupported` naming the vocabulary. Pinned
as an exact set so that teaching macOS one of these reddens this test and
gets it removed from the list, rather than the list quietly growing.
"""


class TestEveryBackendKnowsTheDocumentedNames(unittest.TestCase):
    """The vocabulary resolves everywhere, bar the macOS hardware exceptions."""

    def test_windows_translates_every_documented_name(self):
        for name in VOCABULARY:
            with self.subTest(key=name):
                self.assertIn(
                    name.lower(),
                    win32._KEYSYM_VK,
                    f"{name!r} is documented but win32 cannot resolve it",
                )

    def test_macos_translates_every_documented_name_it_has_a_key_for(self):
        for name in VOCABULARY:
            if name in KEYS_NO_MAC_KEYBOARD_HAS:
                continue
            with self.subTest(key=name):
                resolved = macquartz._key_name(name)
                known = (
                    macquartz._KEYCODES.get(resolved) is not None
                    or resolved in macquartz._MODIFIER_FLAGS
                )
                self.assertTrue(
                    known,
                    f"{name!r} is documented but macOS resolved it to "
                    f"{resolved!r}, which is not a key it knows",
                )

    def test_the_macos_exceptions_are_exactly_the_keys_it_cannot_reach(self):
        """So the list stays honest in both directions."""
        refused = set()
        for name in VOCABULARY:
            resolved = macquartz._key_name(name)
            if (
                macquartz._KEYCODES.get(resolved) is None
                and resolved not in macquartz._MODIFIER_FLAGS
            ):
                refused.add(name)
        self.assertEqual(refused, set(KEYS_NO_MAC_KEYBOARD_HAS))

    def test_uinput_translates_every_documented_name(self):
        try:
            from evdev import ecodes
        except Exception:  # noqa: BLE001 - evdev is an optional extra
            self.skipTest("evdev is not installed, so its names cannot be checked")
        from pyguitest.backends import uinput

        backend = object.__new__(uinput.UinputBackend)
        backend._ecodes = ecodes
        for name in VOCABULARY:
            with self.subTest(key=name):
                try:
                    code = backend._keycode(name)
                except Exception as exc:  # noqa: BLE001 - the failure under test
                    self.fail(
                        f"{name!r} is documented but uinput cannot resolve it: {exc}"
                    )
                self.assertIsInstance(code, int)

    def test_the_modifier_table_names_keys_each_backend_can_press(self):
        """`send_keys("#(a)")` presses whatever `MODIFIER_KEYS` names.

        macOS overrides the table (`#` is `command` there, and `&` is dropped
        rather than aliased onto Option), which is why this asks each backend
        about its *own* table rather than the inherited one.
        """
        for name in GUIBackend.MODIFIER_KEYS.values():
            with self.subTest(backend="win32", key=name):
                self.assertIn(name.lower(), win32._KEYSYM_VK)
        for name in macquartz.MacquartzBackend.MODIFIER_KEYS.values():
            with self.subTest(backend="macos", key=name):
                resolved = macquartz._key_name(name)
                self.assertTrue(
                    macquartz._KEYCODES.get(resolved) is not None
                    or resolved in macquartz._MODIFIER_FLAGS,
                    f"macOS names {name!r} as a send_keys modifier but cannot press it",
                )


if __name__ == "__main__":
    unittest.main()
