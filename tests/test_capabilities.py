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


if __name__ == "__main__":
    unittest.main()
