import unittest
from shelfcheck.models import BrandShare
from shelfcheck.stats import change_flag, comparison, wilson


class StatsTests(unittest.TestCase):
    def test_wilson_edges_and_middle(self):
        self.assertAlmostEqual(wilson(0, 50)[1], .07135, places=4)
        self.assertAlmostEqual(wilson(25, 50)[0], .36644, places=4)
        self.assertAlmostEqual(wilson(50, 50)[0], .92865, places=4)
        for successes in range(51):
            low, high = wilson(successes, 50)
            self.assertTrue(0 <= low <= high <= 1)

    def test_comparison_and_change(self):
        a = BrandShare("A", "x", 20, 50, .4, .3, .5, 2, 5)
        b = BrandShare("B", "x", 18, 50, .36, .28, .48, 2, 5)
        self.assertIn("not distinguishable", comparison(a, b))
        self.assertEqual(change_flag(a, BrandShare("A", "x", 1, 5, .2, .1, .3, None, 0)), "change flagged")
        self.assertEqual(change_flag(a, BrandShare("A", "x", 2, 5, .38, .2, .6, None, 0)), "stable within noise")
