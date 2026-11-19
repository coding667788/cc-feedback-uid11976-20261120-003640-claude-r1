"""Scale properties: huge spans and long handover chains never per-minute."""

from __future__ import annotations

import time
import unittest

from support import DatabaseCase
from visitor_pass import service, store


class HugeSpanCase(DatabaseCase):
    def test_hundred_million_minute_booking(self):
        start, end = 0, 200_000_000
        handover_at = 100_000_000
        self.load(
            "huge",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": start,
                    "end": end,
                    "quota": 5,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": handover_at + 3},
                {"person": "E2", "start": handover_at - 2, "end": end},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": end},
                {"person": "E2", "area": "A", "start": 0, "end": end},
            ],
            handovers=[
                {
                    "handover_id": "H1",
                    "booking_id": "B1",
                    "area": "A",
                    "old_escort": "E1",
                    "new_escort": "E2",
                    "at": handover_at,
                    "confirmations": [
                        {"person": "E1", "booking_id": "B1", "area": "A",
                         "at": handover_at},
                        {"person": "E2", "booking_id": "B1", "area": "A",
                         "at": handover_at},
                    ],
                }
            ],
        )
        started = time.perf_counter()
        cov = service.coverage(self.conn, "B1")
        receipt = service.admit(
            self.conn, "B1", 150_000_000, "R"
        )["receipt"]
        elapsed = time.perf_counter() - started

        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]],
            [(0, 200_000_000)],
        )
        self.assertEqual(cov["passable_minutes"], 200_000_000)
        self.assertEqual([c["at"] for c in cov["changes"]], [handover_at])
        self.assertEqual(receipt["escort"], "E2")
        # Endpoint scan only: hundreds of millions of minutes must be
        # microseconds-to-milliseconds, never minutes of looping.
        self.assertLess(elapsed, 2.0)

    def test_huge_gap_reported_without_walking(self):
        self.load(
            "huge",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 150_000_000,
                    "quota": 5,
                    "home_escort": "E1",
                }
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 1},
                {"person": "E1", "start": 149_999_999, "end": 150_000_000},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 150_000_000}
            ],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]],
            [(0, 1), (149_999_999, 150_000_000)],
        )
        self.assertEqual(
            [(gap["start"], gap["end"]) for gap in cov["gaps"]],
            [(1, 149_999_999)],
        )


class LongChainCase(DatabaseCase):
    def test_two_thousand_link_chain(self):
        links = 2000
        escorts = [f"E{i}" for i in range(links + 1)]
        self.load(
            "chain",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 10**9,
                    "quota": 3,
                    "home_escort": "E0",
                }
            ],
            presence=[{"person": person, "start": 0, "end": 10**9}
                      for person in escorts],
            qualifications=[{"person": person, "area": "A", "start": 0,
                             "end": 10**9} for person in escorts],
            handovers=[
                {
                    "handover_id": f"H{i}",
                    "booking_id": "B1",
                    "area": "A",
                    "old_escort": f"E{i}",
                    "new_escort": f"E{i + 1}",
                    "at": 1000 * (i + 1),
                    "confirmations": [
                        {"person": f"E{i}", "booking_id": "B1", "area": "A",
                         "at": 1000 * (i + 1)},
                        {"person": f"E{i + 1}", "booking_id": "B1", "area": "A",
                         "at": 1000 * (i + 1)},
                    ],
                }
                for i in range(links)
            ],
        )
        started = time.perf_counter()
        cov = service.coverage(self.conn, "B1")
        receipt = service.admit(
            self.conn, "B1", 500_000_000, "R"
        )["receipt"]
        report = service.verify(self.conn)
        elapsed = time.perf_counter() - started

        self.assertEqual(len(cov["passable"]), links + 1)
        self.assertEqual(cov["passable_minutes"], 10**9)
        self.assertEqual(receipt["escort"], f"E{links}")
        self.assertEqual(len(receipt["evidence"]["handovers_used"]), links)
        self.assertTrue(report["ok"])
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
