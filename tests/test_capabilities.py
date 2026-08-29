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


if __name__ == "__main__":
    unittest.main()
