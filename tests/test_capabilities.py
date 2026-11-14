"""The capability enum, and the set operations callers run on it.

`.report()`, `.missing`, `.by_tier()`, and the set operators that would
otherwise hand back a plain frozenset and drop those three methods.
"""

import unittest

from pyguitest.capabilities import Capability, CapabilitySet, Tier


class TestCapabilityEnum(unittest.TestCase):
    def test_no_aliasing(self):
        # Regression: members were first written as `NAME = Tier.X`, which made
        # every member after the first in each tier an alias of it -- 25
        # capabilities collapsed silently to 6.
        self.assertEqual(len(list(Capability)), len(Capability.__members__))
        self.assertEqual(len({c.value for c in Capability}), len(list(Capability)))

    def test_every_capability_has_tier_and_description(self):
        for cap in Capability:
            with self.subTest(cap=cap.name):
                self.assertIsInstance(cap.tier, Tier)
                self.assertTrue(cap.description)

    def test_tiers_are_ordered_by_cost(self):
        self.assertLess(Tier.PORTABLE, Tier.DIRECT)
        self.assertLess(Tier.DIRECT, Tier.COMPOSITOR)
        self.assertLess(Tier.COMPOSITOR, Tier.PRIVILEGED)
        self.assertLess(Tier.PRIVILEGED, Tier.NO_PATH)


class TestCapabilitySet(unittest.TestCase):
    def setUp(self):
        self.caps = CapabilitySet({Capability.PROCESS_LAUNCH, Capability.TIMING})

    def test_membership_and_missing_partition(self):
        self.assertIn(Capability.PROCESS_LAUNCH, self.caps)
        self.assertNotIn(Capability.WINDOW_LIST, self.caps)
        self.assertEqual(len(self.caps) + len(self.caps.missing), len(list(Capability)))
        self.assertEqual(self.caps & self.caps.missing, frozenset())

    def test_by_tier(self):
        self.assertEqual(len(self.caps.by_tier(Tier.PORTABLE)), 2)
        self.assertEqual(len(self.caps.by_tier(Tier.NO_PATH)), 0)

    def test_report_covers_every_capability(self):
        report = self.caps.report()
        for cap in Capability:
            self.assertIn(cap.name, report)

    def test_report_says_which_scale_its_tier_numbers_are_on(self):
        """The table is pasted into bug reports from all three platforms.

        The tiers are Wayland implementation costs ("core Wayland protocol",
        "per-desktop backend"), and `pyguitest debug` prints this table on
        Windows and macOS too, where "T6 deliberately prevented" beside a
        capability reads as a verdict on that desktop. The caption is what
        stops the numbers being read as one.
        """
        report = self.caps.report()
        self.assertIn("costs on Wayland", report)
        self.assertLess(
            report.index("costs on Wayland"), report.index("PROCESS_LAUNCH")
        )

    def test_set_operators_stay_a_capability_set(self):
        # Regression: frozenset's operators return a plain frozenset even
        # on a subclass instance, silently dropping .report() and .missing
        # -- the two methods this class exists for.
        unioned = self.caps | {Capability.WINDOW_LIST}
        self.assertIsInstance(unioned, CapabilitySet)
        self.assertTrue(hasattr(unioned, "missing"))

        intersected = self.caps & CapabilitySet({Capability.TIMING})
        self.assertIsInstance(intersected, CapabilitySet)

        subtracted = self.caps - CapabilitySet({Capability.TIMING})
        self.assertIsInstance(subtracted, CapabilitySet)

        xored = self.caps ^ CapabilitySet({Capability.WINDOW_LIST})
        self.assertIsInstance(xored, CapabilitySet)

    def test_named_set_methods_stay_a_capability_set(self):
        # Regression: the four operators above were fixed and these were
        # not. `union()` and its three siblings are ordinary methods on
        # frozenset and never go through `__or__`, so a merge written with
        # one of them -- or a `copy()` taken before handing the merge on --
        # dropped .report() and .missing all over again.
        for method, argument in (
            ("union", {Capability.WINDOW_LIST}),
            ("intersection", CapabilitySet({Capability.TIMING})),
            ("difference", CapabilitySet({Capability.TIMING})),
            ("symmetric_difference", CapabilitySet({Capability.WINDOW_LIST})),
        ):
            with self.subTest(method=method):
                result = getattr(self.caps, method)(argument)
                self.assertIsInstance(result, CapabilitySet)
                self.assertTrue(hasattr(result, "missing"))

        copied = self.caps.copy()
        self.assertIsInstance(copied, CapabilitySet)
        self.assertEqual(copied, self.caps)

    def test_reflected_operators_stay_a_capability_set(self):
        # Regression, and the reason the four operators above were not
        # enough on their own: with a frozenset on the left, Python asks the
        # *subclass* side first, so these four reflected methods are the
        # ones actually consulted -- and without them
        # `frozenset({...}) | caps` came back a frozenset.
        other = frozenset({Capability.WINDOW_LIST, Capability.TIMING})
        self.assertIsInstance(other | self.caps, CapabilitySet)
        self.assertIsInstance(other & self.caps, CapabilitySet)
        self.assertIsInstance(other ^ self.caps, CapabilitySet)
        self.assertIsInstance(other - self.caps, CapabilitySet)
        # The values are still the right ones, not merely the right type.
        self.assertEqual(other - self.caps, CapabilitySet({Capability.WINDOW_LIST}))

    def test_a_set_on_the_left_is_accepted_too(self):
        # A mutable set is a sibling of frozenset rather than a subclass, so
        # `{...} | caps` is answered by set.__or__ and never reaches these --
        # but `{...} - caps` and the rest do, and set algebra between two
        # real sets is exactly what they are for.
        other = {Capability.WINDOW_LIST, Capability.TIMING}
        self.assertEqual(other - self.caps, CapabilitySet({Capability.WINDOW_LIST}))

    def test_an_operand_that_is_not_a_set_is_refused_as_the_builtin_refuses_it(self):
        # `frozenset(other)` accepts any iterable, so a list or a mapping
        # came back a CapabilitySet where the builtin being stood in for
        # raises TypeError -- set algebra against a list is a mistake, not a
        # shorthand. NotImplemented is how an operator says so.
        for operand in ([Capability.TIMING], {"timing": 1}, "timing", 3):
            with self.subTest(operand=operand):
                for operation in (
                    lambda a, b: a | b,
                    lambda a, b: a & b,
                    lambda a, b: a ^ b,
                    lambda a, b: a - b,
                ):
                    with self.assertRaises(TypeError):
                        operation(operand, self.caps)

    def test_a_non_set_operand_on_the_right_raises_the_clean_type_error(self):
        # The forward operators had the mirror of the reflected ones' bug and
        # no guard: `caps | ["x"]` wrapped frozenset.__or__'s NotImplemented
        # in `CapabilitySet(...)`, which iterated NotImplemented and raised a
        # confusing `TypeError: 'NotImplementedType' object is not iterable`
        # instead of the builtin's clean `unsupported operand type(s)`.
        for operand in ([Capability.TIMING], {"timing": 1}, "timing", 3):
            with self.subTest(operand=operand):
                for operation in (
                    lambda a, b: a | b,
                    lambda a, b: a & b,
                    lambda a, b: a ^ b,
                    lambda a, b: a - b,
                ):
                    with self.assertRaises(TypeError) as ctx:
                        operation(self.caps, operand)
                    message = str(ctx.exception)
                    self.assertIn("unsupported operand type", message)
                    self.assertNotIn("NotImplementedType", message)


if __name__ == "__main__":
    unittest.main()
