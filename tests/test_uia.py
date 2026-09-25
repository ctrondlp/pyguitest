"""The UIA control-type table, as data.

No Windows machine and no `comtypes`: this is a translation table, and what
is worth checking about it is that it covers the whole range UIA defines,
that every value is a role name this package can match on, and that the
handful of values with no `Role` constant are exactly the ones two sets name.
"""

import unittest

from pyguitest.backends.uia import (
    _ATSPI_NAMES_WITHOUT_CONSTANTS,
    _NO_ATSPI_COUNTERPART,
    _UIA_ROLES,
    _UNKNOWN_ROLE,
)
from pyguitest.roles import Role


def role_values():
    """Every string `Role` names -- the vocabulary this table maps into."""
    return {
        value
        for name, value in vars(Role).items()
        if name.isupper() and isinstance(value, str)
    }


class TestTheControlTypeTable(unittest.TestCase):
    def test_every_control_type_id_is_present(self):
        # 50000 through 50040 inclusive. The ids are contiguous in the UIA
        # documentation, so a gap is a bug rather than a decision.
        self.assertEqual(set(_UIA_ROLES), set(range(50000, 50041)))

    def test_the_endpoints_are_the_ids_the_documentation_names(self):
        self.assertEqual(_UIA_ROLES[50000], Role.PUSH_BUTTON)
        self.assertEqual(_UIA_ROLES[50040], Role.TOOL_BAR)

    def test_values_read_like_role_names(self):
        for control_type, role in _UIA_ROLES.items():
            with self.subTest(control_type=control_type):
                self.assertEqual(role, role.lower())
                self.assertEqual(role, role.strip())
                self.assertTrue(role)


class TestTheValuesAreNamesACallerCouldHaveLearned(unittest.TestCase):
    """Nothing here is invented silently."""

    def test_a_value_is_a_role_constant_or_a_documented_name(self):
        known = role_values() | _ATSPI_NAMES_WITHOUT_CONSTANTS | _NO_ATSPI_COUNTERPART
        for control_type, role in _UIA_ROLES.items():
            with self.subTest(control_type=control_type):
                self.assertIn(role, known)

    def test_the_two_sets_agree_with_role_and_with_each_other(self):
        names = role_values()
        self.assertFalse(names & _ATSPI_NAMES_WITHOUT_CONSTANTS)
        self.assertFalse(names & _NO_ATSPI_COUNTERPART)
        self.assertFalse(_ATSPI_NAMES_WITHOUT_CONSTANTS & _NO_ATSPI_COUNTERPART)

    def test_the_sets_say_exactly_what_they_claim_to(self):
        self.assertEqual(
            _ATSPI_NAMES_WITHOUT_CONSTANTS,
            {"calendar", "header", "menu bar", "tool tip"},
        )
        self.assertEqual(
            _NO_ATSPI_COUNTERPART, {"custom", "semantic zoom", "thumb", "title bar"}
        )

    def test_every_name_in_either_set_is_actually_produced(self):
        # A set that has drifted away from the table would otherwise sit there
        # looking authoritative while describing nothing.
        values = set(_UIA_ROLES.values())
        for name in _ATSPI_NAMES_WITHOUT_CONSTANTS | _NO_ATSPI_COUNTERPART:
            with self.subTest(name=name):
                self.assertIn(name, values)


class TestTheMismatchesAreTheDocumentedOnes(unittest.TestCase):
    """The four collapses 3.1 of the Windows analysis calls the interesting part."""

    def test_there_is_no_toggle_button_control_type(self):
        # UIA has one button type; whether it toggles is a pattern, so a role
        # of "toggle button" can never come back from a Windows session.
        self.assertNotIn(Role.TOGGLE_BUTTON, _UIA_ROLES.values())

    def test_the_menu_item_family_collapses_to_one_value(self):
        self.assertEqual(_UIA_ROLES[50011], Role.MENU_ITEM)
        values = set(_UIA_ROLES.values())
        self.assertNotIn(Role.CHECK_MENU_ITEM, values)
        self.assertNotIn(Role.RADIO_MENU_ITEM, values)

    def test_uia_text_covers_what_at_spi_calls_label_and_text(self):
        # The sentence the docs have to carry: a Windows label is found by
        # role="text", and "label" matches nothing there.
        self.assertEqual(_UIA_ROLES[50020], Role.TEXT)
        self.assertNotIn(Role.LABEL, _UIA_ROLES.values())

    def test_a_pane_and_a_group_both_land_on_panel(self):
        # "Pane is at-spi's panel only most of the time" -- and Group, which is
        # a different control type, lands there too.
        self.assertEqual(_UIA_ROLES[50026], Role.PANEL)
        self.assertEqual(_UIA_ROLES[50033], Role.PANEL)

    def test_the_edit_control_type_is_an_entry_not_a_text(self):
        # at-spi splits them by whether text can be typed into the widget.
        self.assertEqual(_UIA_ROLES[50004], Role.ENTRY)

    def test_only_the_scroll_bar_itself_is_a_scroll_bar(self):
        # A scroll bar's thumb (50027) is a child element of the bar (50014).
        # Calling it a scroll bar too would make find_elements(role=SCROLL_BAR)
        # report two elements per bar on Windows against one on Linux, which is
        # the divergence the role table exists to prevent.
        self.assertEqual(_UIA_ROLES[50014], Role.SCROLL_BAR)
        self.assertEqual(
            [
                control_type
                for control_type, role in _UIA_ROLES.items()
                if role == Role.SCROLL_BAR
            ],
            [50014],
        )


class TestTheFallbackRole(unittest.TestCase):
    """The one value the table does not produce, and why it exists.

    `UiaBackend` answers with it for a control type outside the documented
    range, which is the only way to reach it -- the range above is total over
    the ids UIA defines.
    """

    def test_it_is_the_at_spi_name_for_a_role_that_could_not_be_told(self):
        self.assertEqual(_UNKNOWN_ROLE, "unknown")

    def test_it_is_not_one_of_the_values_the_table_produces(self):
        self.assertNotIn(_UNKNOWN_ROLE, set(_UIA_ROLES.values()))

    def test_it_belongs_to_neither_of_the_two_documented_sets(self):
        # It needs no new vocabulary the way the five names above do, and it is
        # not a role at-spi lacks: it is the name at-spi gives a widget whose
        # role it could not determine.
        self.assertNotIn(_UNKNOWN_ROLE, _ATSPI_NAMES_WITHOUT_CONSTANTS)
        self.assertNotIn(_UNKNOWN_ROLE, _NO_ATSPI_COUNTERPART)


if __name__ == "__main__":
    unittest.main()
