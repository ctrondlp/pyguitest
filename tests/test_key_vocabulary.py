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

import inspect
import unittest

from pyguitest.backends import macquartz, uinput, win32
from pyguitest.backends.base import GUIBackend
from pyguitest.errors import CapabilityUnsupported, PyGUITestError

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
the refusal is a typed `CapabilityUnsupported` naming the vocabulary --
these are real keys that this *desktop* lacks, which is the half of
`Session.send_keys`' contract that type is for; a name that is not a key at
all is the `ValueError` half. Pinned
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


class TestEveryBraceAliasResolvesOnEveryBackend(unittest.TestCase):
    """The `{CAP}`-style abbreviations, in the direction nothing checked.

    The tests above resolve `VOCABULARY` -- the X11 key *names* -- against
    each backend's translation table. The alias table is a second,
    independent vocabulary: `sendkeys.py` looks a brace token up in the
    KEY_EVENT provider's own `KEY_ALIASES` and falls back to the raw token,
    so an alias a backend omits is handed to `press_key` verbatim and
    refused there. Nothing asked whether each backend's table covers the
    portable one, so nine aliases were unreachable on macOS -- `{CAP}` among
    them, while `press_key("Caps_Lock")` worked on the same Mac -- and the
    table's own docstring miscounted them as seven.

    A backend may still omit an alias, but it has to say so here, which is
    what keeps the docstrings' claims honest.
    """

    PORTABLE = GUIBackend.KEY_ALIASES

    NO_MAC_KEY = frozenset({"BRE", "CAN", "INS", "MNU", "NUM", "PRT", "SCR"})
    """Aliases a Mac keyboard has no key for; each must also lack a keycode."""

    NO_STANDARD_EVDEV_KEY = frozenset({"BRE", "CAN", "HEL"})
    """Aliases with no settled evdev equivalent on an ordinary keyboard."""

    def test_macos_covers_every_portable_alias_but_its_declared_exceptions(self):
        missing = set(self.PORTABLE) - set(macquartz.MacquartzBackend.KEY_ALIASES)
        self.assertEqual(missing, set(self.NO_MAC_KEY))

    def test_the_macos_alias_exceptions_really_have_no_key(self):
        """Both directions, so the exception list cannot grow by assumption."""
        for alias in sorted(self.NO_MAC_KEY):
            resolved = macquartz._key_name(self.PORTABLE[alias])
            with self.subTest(alias=alias):
                self.assertIsNone(
                    macquartz._KEYCODES.get(resolved),
                    f"{alias} is listed as a key a Mac lacks, but "
                    f"{resolved!r} has a keycode -- map the alias instead",
                )

    def test_uinput_covers_every_portable_alias_but_its_declared_exceptions(self):
        missing = set(self.PORTABLE) - set(uinput.UinputBackend.KEY_ALIASES)
        self.assertEqual(missing, set(self.NO_STANDARD_EVDEV_KEY))

    def test_every_alias_a_backend_does_map_names_a_key_it_knows(self):
        for alias, target in sorted(macquartz.MacquartzBackend.KEY_ALIASES.items()):
            with self.subTest(backend="macquartz", alias=alias):
                known = (
                    macquartz._KEYCODES.get(target) is not None
                    or target in macquartz._MODIFIER_FLAGS
                )
                self.assertTrue(known, f"{alias} -> {target!r} is not a key")


class TestOneExceptionTypePerFailureKind(unittest.TestCase):
    """`Session.send_keys` documents two failure types; backends disagreed.

    The contract, from its own docstring: "A `ValueError` means the string
    itself is wrong: a character with no static key mapping, malformed `{}`
    syntax, or a `{}` name that is not a key at all. A
    `CapabilityUnsupported` means the string is fine and this *desktop*
    cannot serve it."

    For a name nothing resolves, x11, uinput, win32 and portal raised
    `ValueError`; macquartz raised `CapabilityUnsupported` and eiinput a bare
    `PyGUITestError`. Neither is a `ValueError` -- they share no ancestor but
    `Exception` -- so the documented `except ValueError` caught nothing on
    macOS and nothing on a libei-driven Wayland session, and eiinput
    collapsed both halves of the contract into one unclassifiable type.
    """

    UNKNOWN = "Nonexistent_Key"

    def test_the_two_documented_types_are_unrelated(self):
        # Which is why picking the wrong one is not a cosmetic difference.
        self.assertFalse(issubclass(CapabilityUnsupported, ValueError))
        self.assertFalse(issubclass(PyGUITestError, ValueError))

    def test_macquartz_refuses_an_unknown_name_as_a_value_error(self):
        backend = object.__new__(macquartz.MacquartzBackend)
        with self.assertRaises(ValueError):
            backend._keycode(self.UNKNOWN)

    def test_uinput_refuses_an_unknown_name_as_a_value_error(self):
        try:
            from evdev import ecodes
        except Exception:  # noqa: BLE001 - evdev is an optional extra
            self.skipTest("evdev is not installed")
        backend = object.__new__(uinput.UinputBackend)
        backend._ecodes = ecodes
        with self.assertRaises(ValueError):
            backend._keycode(self.UNKNOWN)

    def test_no_key_event_backend_raises_the_bare_base_class(self):
        """The shape eiinput had: a failure neither documented type covers.

        Read from the source of each resolver, because constructing these
        backends needs their platform. A bare `PyGUITestError` for a key
        name is what this forbids; the two documented types both remain
        available, and `CapabilityUnsupported` is itself a `PyGUITestError`,
        so the check is for the *bare* spelling at a raise.
        """
        from pyguitest.backends import eiinput

        source = inspect.getsource(eiinput.LibeiBackend._resolve_name)
        self.assertNotIn("raise PyGUITestError(", source)
        self.assertIn("raise ValueError(", source)
        self.assertIn("raise CapabilityUnsupported(", source)


if __name__ == "__main__":
    unittest.main()
