"""Escort-chain semantics required by the task prompt."""

from __future__ import annotations

import unittest

from support import DatabaseCase, confirmation, handover
from visitor_pass import service


class TouchingSegmentsMergeCase(DatabaseCase):
    def test_same_person_segments_stitch_across_touch_points(self):
        """Presence and qualification split into touching pieces still merge."""
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 30},
                {"person": "E1", "start": 30, "end": 60},
                {"person": "E1", "start": 60, "end": 100},
                {"person": "E2", "start": 28, "end": 50},
                {"person": "E2", "start": 50, "end": 72},
                {"person": "E3", "start": 68, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 40},
                {"person": "E2", "area": "A", "start": 40, "end": 100},
                {"person": "E3", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[
                handover("H1", "B1", "A", "E1", "E2", 30),
                handover("H2", "B1", "A", "E2", "E3", 70),
            ],
        )
        cov = service.coverage(self.conn, "B1")
        # A single contiguous passable interval: both handovers applied because
        # each person's own touching segments cover the required window.
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]], [(0, 100)]
        )
        self.assertEqual(
            [(seg["start"], seg["end"], seg["escort"]) for seg in cov["passable"]],
            [(0, 30, "E1"), (30, 70, "E2"), (70, 100, "E3")],
        )
        self.assertEqual([h["handover_id"] for h in cov["changes"]], ["H1", "H2"])
        self.assertTrue(all(h["applied"] for h in cov["handovers"]))
        # Quota/behaviour at a stitched minute:
        self.assertTrue(service.admit(self.conn, "B1", 55, "R55")["receipt"]["admitted"])


class HandoverWindowCase(DatabaseCase):
    """[t-2, t+2) must be continuously covered on both sides."""

    def _load_with_escort_window(self, new_start, old_end=100):
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": old_end},
                {"person": "E2", "start": new_start, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[handover("H1", "B1", "A", "E1", "E2", 30)],
        )
        return service.coverage(self.conn, "B1")

    def test_exact_four_minute_window_applies(self):
        cov = self._load_with_escort_window(new_start=28)
        self.assertTrue(cov["handovers"][0]["applied"])
        self.assertEqual([c["at"] for c in cov["changes"]], [30])

    def test_one_minute_short_of_overlap_is_rejected(self):
        cov = self._load_with_escort_window(new_start=29)
        self.assertFalse(cov["handovers"][0]["applied"])
        self.assertEqual(
            cov["handovers"][0]["reason"], "new-not-continuously-covered"
        )
        self.assertEqual(cov["changes"], [])
        # The new escort never becomes passable anywhere.
        self.assertEqual([seg["escort"] for seg in cov["passable"]], ["E1"])

    def test_old_escort_short_by_one_minute_is_rejected(self):
        cov = self._load_with_escort_window(new_start=28, old_end=31)
        self.assertEqual(cov["handovers"][0]["reason"], "old-not-continuously-covered")
        self.assertFalse(cov["handovers"][0]["applied"])

    def test_old_escort_exact_at_t_plus_2_applies(self):
        cov = self._load_with_escort_window(new_start=28, old_end=32)
        self.assertTrue(cov["handovers"][0]["applied"])


class GapIsPermanentCase(DatabaseCase):
    def test_blank_time_stays_impassable_and_never_resumes(self):
        # E1 on duty [0,30) and again [40,100): the 30..40 blank stays blank.
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 30},
                {"person": "E1", "start": 40, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
            ],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]],
            [(0, 30), (40, 100)],
        )
        self.assertEqual([(g["start"], g["end"]) for g in cov["gaps"]], [(30, 40)])
        self.assertTrue(service.admit(self.conn, "B1", 20, "R20")["receipt"]["admitted"])
        self.assertFalse(service.admit(self.conn, "B1", 35, "R35")["receipt"]["admitted"])
        self.assertTrue(service.admit(self.conn, "B1", 80, "R80")["receipt"]["admitted"])

    def test_blank_time_cannot_be_filled_by_another_escort(self):
        # E2 stands fully ready but there is no handover.
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 30},
                {"person": "E2", "start": 0, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]], [(0, 30)]
        )
        self.assertEqual(cov["changes"], [])
        self.assertFalse(service.admit(self.conn, "B1", 60, "R60")["receipt"]["admitted"])

    def test_handover_after_a_gap_can_still_apply(self):
        """The chain is not "dead" after a gap: it just cannot cross it.

        E1 is absent [30,40) then back.  A handover at 45 is judged on E1's and
        E2's own windows at [43,47) and is valid; the gap itself remains
        impassable and no escort skips it.
        """
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 30},
                {"person": "E1", "start": 40, "end": 100},
                {"person": "E2", "start": 0, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[
                handover("H1", "B1", "A", "E1", "E2", 45),
            ],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(p["start"], p["end"], p["escort"]) for p in cov["passable"]],
            [(0, 30, "E1"), (40, 45, "E1"), (45, 100, "E2")],
        )
        self.assertEqual([(g["start"], g["end"]) for g in cov["gaps"]], [(30, 40)])
        self.assertTrue(cov["handovers"][0]["applied"])
        self.assertFalse(service.admit(self.conn, "B1", 35, "in-gap")["receipt"]["admitted"])
        self.assertTrue(service.admit(self.conn, "B1", 42, "after-gap")["receipt"]["admitted"])

    def test_handover_whose_old_window_spans_the_gap_is_rejected(self):
        # H1 at 36 needs E1 over [34,38); E1 is absent [30,40), so it fails.
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 30},
                {"person": "E1", "start": 40, "end": 100},
                {"person": "E2", "start": 0, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[handover("H1", "B1", "A", "E1", "E2", 36)],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertFalse(cov["handovers"][0]["applied"])
        self.assertEqual(cov["handovers"][0]["reason"], "old-not-continuously-covered")
        self.assertEqual({seg["escort"] for seg in cov["passable"]}, {"E1"})


class InvalidHandoverBlocksLaterOnesCase(DatabaseCase):
    def test_second_handover_that_relies_on_failed_first_is_rejected(self):
        # H1 fails because E2 only arrives at 29 (needs 28).
        # H2 says E2 -> E3 at 70.  Since H1 never applied, E2 is not the
        # designated escort, so H2 must be rejected too.
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 100},
                {"person": "E2", "start": 29, "end": 100},
                {"person": "E3", "start": 68, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
                {"person": "E3", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[
                handover("H1", "B1", "A", "E1", "E2", 30),
                handover("H2", "B1", "A", "E2", "E3", 70),
            ],
        )
        cov = service.coverage(self.conn, "B1")
        decisions = {h["handover_id"]: h for h in cov["handovers"]}
        self.assertFalse(decisions["H1"]["applied"])
        self.assertEqual(decisions["H1"]["reason"], "new-not-continuously-covered")
        self.assertFalse(decisions["H2"]["applied"])
        self.assertEqual(decisions["H2"]["reason"], "not-designated-escort")
        # Only E1 is ever the escort.
        self.assertEqual({seg["escort"] for seg in cov["passable"]}, {"E1"})

    def test_unconfirmed_handover_is_inert(self):
        bad = handover("H1", "B1", "A", "E1", "E2", 30)
        # Drop the new escort's confirmation.
        bad["confirmations"] = [confirmation("E1", "B1", "A", 30)]
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 9,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 100},
                {"person": "E2", "start": 0, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
            handovers=[bad],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(cov["handovers"][0]["reason"], "new-not-confirmed")
        self.assertEqual({seg["escort"] for seg in cov["passable"]}, {"E1"})
        self.assertTrue(service.admit(self.conn, "B1", 50, "R")["receipt"]["admitted"])
        self.assertEqual(
            service.admit(self.conn, "B1", 50, "R")["receipt"]["escort"], "E1"
        )


if __name__ == "__main__":
    unittest.main()
