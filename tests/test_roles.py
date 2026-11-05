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


class TestGtk4Roles(unittest.TestCase):
    """The three roles GTK4 publishes that no existing constant named.

    Each measured live, GTK4 4.24.1 under at-spi2 -- see roles.py's own
    comment on each for why it is not simply an alias of something that
    already existed.
    """

    def test_switch(self):
        self.assertEqual(Role.SWITCH, "switch")

    def test_tree_table(self):
        self.assertEqual(Role.TREE_TABLE, "tree table")

    def test_level_bar(self):
        self.assertEqual(Role.LEVEL_BAR, "level bar")

    def test_none_of_the_three_is_aliased_to_an_existing_role(self):
        for new_role in (Role.SWITCH, Role.TREE_TABLE, Role.LEVEL_BAR):
            with self.subTest(role=new_role):
                self.assertEqual(spellings(new_role), frozenset({new_role}))


class TestToplevelRoles(unittest.TestCase):
    """The two toplevel roles GTK and Qt publish besides frame/window/dialog.

    Measured live on seven desktops -- see roles.py's own note on each.
    Neither is an alias: a message box is not a dialog on the bus, and a
    `dialog` search must not find one.
    """

    def test_alert(self):
        self.assertEqual(Role.ALERT, "alert")

    def test_file_chooser(self):
        self.assertEqual(Role.FILE_CHOOSER, "file chooser")

    def test_both_count_as_windows(self):
        self.assertIn(Role.ALERT, Role.WINDOW_ROLES)
        self.assertIn(Role.FILE_CHOOSER, Role.WINDOW_ROLES)

    def test_a_main_window_still_outranks_them_by_position(self):
        # WINDOW_ROLES is in precedence order where one title matches
        # several toplevels; the classic three keep their places ahead of
        # the two added.
        self.assertEqual(Role.WINDOW_ROLES[:3], (Role.FRAME, Role.WINDOW, Role.DIALOG))

    def test_neither_is_aliased_to_an_existing_role(self):
        for new_role in (Role.ALERT, Role.FILE_CHOOSER):
            with self.subTest(role=new_role):
                self.assertEqual(spellings(new_role), frozenset({new_role}))


if __name__ == "__main__":
    unittest.main()
