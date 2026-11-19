"""Interval algebra: touching merge, huge spans, subtraction."""

from __future__ import annotations

import unittest

import support  # noqa: F401  (puts the repo root on sys.path)
from visitor_pass import store


class MergeTests(unittest.TestCase):
    def test_touching_ranges_merge(self):
        self.assertEqual(store.merge_intervals([(0, 5), (5, 9)]), [(0, 9)])
        self.assertEqual(
            store.merge_intervals([(0, 5), (5, 9), (10, 12), (11, 20)]),
            [(0, 9), (10, 20)],
        )

    def test_gap_of_one_minute_is_not_merged(self):
        # [0,5) and [6,9) leave minute 5 uncovered.
        self.assertEqual(
            store.merge_intervals([(0, 5), (6, 9)]), [(0, 5), (6, 9)]
        )

    def test_empty_and_degenerate_ranges_dropped(self):
        self.assertEqual(store.merge_intervals([(5, 5), (3, 1)]), [])

    def test_unsorted_input_is_sorted(self):
        self.assertEqual(
            store.merge_intervals([(10, 20), (0, 5), (3, 12)]), [(0, 20)]
        )

    def test_huge_range_no_minute_iteration(self):
        merged = store.merge_intervals([(0, 150_000_000), (150_000_000, 260_000_000)])
        self.assertEqual(merged, [(0, 260_000_000)])
        self.assertEqual(store.interval_length(merged), 260_000_000)


class IntersectTests(unittest.TestCase):
    def test_intersection_of_overlaps(self):
        self.assertEqual(
            store.intersect_intervals([(0, 10), (20, 30)], [(5, 25)]),
            [(5, 10), (20, 25)],
        )

    def test_touching_intersection_is_empty(self):
        self.assertEqual(store.intersect_intervals([(0, 5)], [(5, 9)]), [])


class ContainmentTests(unittest.TestCase):
    def test_contains_needs_full_cover(self):
        intervals = [(0, 10), (10, 20)]  # merges to [0,20)
        self.assertTrue(store.interval_contains(intervals, 5, 15))
        self.assertFalse(store.interval_contains([(0, 10), (11, 20)], 5, 15))

    def test_half_open_right_edge(self):
        self.assertFalse(store.interval_contains([(0, 10)], 0, 11))
        self.assertTrue(store.interval_contains([(0, 10)], 9, 10))


class FirstUncoveredTests(unittest.TestCase):
    def test_reports_first_hole(self):
        self.assertEqual(store.first_uncovered([(0, 5), (7, 9)], 0, 9), 5)

    def test_none_when_covered(self):
        self.assertIsNone(store.first_uncovered([(0, 9)], 0, 9))

    def test_gap_before_first_interval(self):
        self.assertEqual(store.first_uncovered([(3, 9)], 0, 9), 0)


if __name__ == "__main__":
    unittest.main()
