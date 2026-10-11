import unittest

from pyguitest.roles import Role, canonical_role, spellings


class TestSpellings(unittest.TestCase):
    def test_a_single_role_with_no_alias_is_itself(self):
        self.assertEqual(spellings("check box"), frozenset({"check box"}))

    def test_a_single_role_with_an_alias_includes_both_spellings(self):
        self.assertEqual(spellings("push button"), frozenset({"push button", "button"}))
        self.assertEqual(spellings("button"), frozenset({"push button", "button"}))

    def test_a_sequence_of_roles_is_the_union_of_each_ones_spellings(self):
        # role=(...) is what find_elements(role=...) passes through when a
        # caller asks for several roles in one search -- see
        # find_elements's own docstring for why that is one walk, not N.
        self.assertEqual(
            spellings((Role.CHECK_BOX, Role.PUSH_BUTTON)),
            frozenset({"check box", "push button", "button"}),
        )

    def test_a_sequence_with_one_role_matches_the_plain_string_form(self):
        self.assertEqual(spellings(("check box",)), spellings("check box"))

    def test_an_empty_sequence_matches_nothing(self):
        self.assertEqual(spellings(()), frozenset())


class TestCanonicalRole(unittest.TestCase):
    def test_the_newer_spelling_normalizes_to_the_role_constant(self):
        # at-spi2 2.61.1 reports a push button's roleName as "button";
        # Element.role reads that back through this so a script comparing
        # against Role.PUSH_BUTTON is not at the mercy of which at-spi2
        # version is running.
        self.assertEqual(canonical_role("button"), Role.PUSH_BUTTON)

    def test_the_already_canonical_spelling_passes_through(self):
        self.assertEqual(canonical_role("push button"), "push button")

    def test_a_role_with_no_alias_passes_through_unchanged(self):
        self.assertEqual(canonical_role("check box"), "check box")


if __name__ == "__main__":
    unittest.main()
