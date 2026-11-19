"""Atomic batch rejection: duplicate minute, cycles, backwards time, conflicts."""

from __future__ import annotations

import unittest

from support import DatabaseCase, handover
from visitor_pass import service, store


class BatchRejectionCase(DatabaseCase):
    def _seed_escorts(self):
        self.load_base()

    # -- same minute twice --------------------------------------------------

    def test_two_handovers_same_minute_in_one_batch(self):
        self._seed_escorts()
        with self.assertRaises(store.ConflictError) as caught:
            self.load(
                "bad",
                handovers=[
                    handover("H1", "B1", "A", "E1", "E2", 30),
                    handover("H2", "B1", "A", "E2", "E3", 30),
                ],
            )
        self.assertEqual(caught.exception.kind, "duplicate-minute")

    def test_same_minute_in_later_batch(self):
        self._seed_escorts()
        self.load("ok", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("bad", handovers=[handover("H2", "B1", "A", "E2", "E3", 30)])
        self.assertEqual(caught.exception.kind, "duplicate-minute")

    def test_rejected_batch_leaves_no_trace(self):
        self._seed_escorts()
        before = service.verify(self.conn)
        with self.assertRaises(store.ConflictError):
            self.load(
                "bad",
                handovers=[
                    handover("H1", "B1", "A", "E1", "E2", 30),
                    handover("H2", "B1", "A", "E2", "E3", 30),
                ],
            )
        after = service.verify(self.conn)
        self.assertEqual(before["state_version"], after["state_version"])
        self.assertEqual(after["chains"][0]["applied_handovers"], [])

    # -- cycles -------------------------------------------------------------

    def test_two_cycle(self):
        self._seed_escorts()
        with self.assertRaises(store.ConflictError) as caught:
            self.load(
                "bad",
                handovers=[
                    handover("H1", "B1", "A", "E1", "E2", 10),
                    handover("H2", "B1", "A", "E2", "E1", 20),
                ],
            )
        self.assertEqual(caught.exception.kind, "handover-cycle")

    def test_three_cycle(self):
        self._seed_escorts()
        with self.assertRaises(store.ConflictError) as caught:
            self.load(
                "bad",
                handovers=[
                    handover("H1", "B1", "A", "E1", "E2", 10),
                    handover("H2", "B1", "A", "E2", "E3", 20),
                    handover("H3", "B1", "A", "E3", "E1", 40),
                ],
            )
        self.assertEqual(caught.exception.kind, "handover-cycle")

    def test_cycle_across_stored_and_new_batches(self):
        self._seed_escorts()
        self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 10)])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("two", handovers=[handover("H2", "B1", "A", "E2", "E1", 20)])
        self.assertEqual(caught.exception.kind, "handover-cycle")

    # -- backwards time -----------------------------------------------------

    def test_backwards_time_against_stored(self):
        self._seed_escorts()
        self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("two", handovers=[handover("H2", "B1", "A", "E2", "E3", 20)])
        self.assertEqual(caught.exception.kind, "backwards-time")

    def test_out_of_order_within_one_batch(self):
        """Sorted application makes out-of-order input legal; order is by ``at``."""
        self._seed_escorts()
        self.load(
            "ok",
            handovers=[
                handover("H2", "B1", "A", "E2", "E3", 70),
                handover("H1", "B1", "A", "E1", "E2", 30),
            ],
        )
        cov = service.coverage(self.conn, "B1")
        # E2's window is the whole booking, so chaining works.
        self.assertEqual(
            [seg["escort"] for seg in cov["passable"]], ["E1", "E2", "E3"]
        )

    # -- duplicate IDs ------------------------------------------------------

    def test_duplicate_handover_id_different_content_in_batch(self):
        self._seed_escorts()
        with self.assertRaises(store.ConflictError) as caught:
            self.load(
                "bad",
                handovers=[
                    handover("H1", "B1", "A", "E1", "E2", 30),
                    handover("H1", "B1", "A", "E1", "E3", 40),
                ],
            )
        self.assertEqual(caught.exception.kind, "handover-conflict")

    def test_duplicate_handover_id_different_content_against_stored(self):
        self._seed_escorts()
        self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("two", handovers=[handover("H1", "B1", "A", "E1", "E3", 40)])
        self.assertEqual(caught.exception.kind, "handover-conflict")

    def test_identical_duplicate_is_idempotent(self):
        self._seed_escorts()
        first = self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        self.assertTrue(first["applied"])
        # Same batch id + same digest -> idempotent replay.
        replay = self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        self.assertTrue(replay["idempotent"])
        self.assertFalse(replay["applied"])
        # New batch id but identical handover row -> ignored as duplicate.
        again = self.load("two", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        self.assertEqual(again["counts"]["duplicates_ignored"], 1)
        self.assertEqual(again["counts"]["handovers_new"], 0)
        # Exactly one decision row remains.
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(len(cov["handovers"]), 1)

    def test_batch_id_reuse_conflicting_content(self):
        self._seed_escorts()
        self.load("one", handovers=[handover("H1", "B1", "A", "E1", "E2", 30)])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("one", revocations=[
                {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 0}
            ])
        self.assertEqual(caught.exception.kind, "batch-conflict")

    def test_duplicate_revocation_id_conflict(self):
        self._seed_escorts()
        self.load("one", revocations=[
            {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 50}
        ])
        with self.assertRaises(store.ConflictError) as caught:
            self.load("two", revocations=[
                {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 60}
            ])
        self.assertEqual(caught.exception.kind, "revocation-conflict")

    def test_duplicate_booking_id_conflicting_content(self):
        self._seed_escorts()
        with self.assertRaises(store.ConflictError) as caught:
            self.load(
                "bad",
                bookings=[
                    {
                        "booking_id": "B1",
                        "visitor": "V1",
                        "area": "A",
                        "start": 0,
                        "end": 200,        # differs from stored end=100
                        "quota": 9,
                        "home_escort": "E1",
                    }
                ],
            )
        self.assertEqual(caught.exception.kind, "booking-conflict")


class ValidationInputCase(DatabaseCase):
    def test_unknown_booking_on_handover(self):
        with self.assertRaises(store.InputError):
            self.load("bad", handovers=[handover("H1", "NOPE", "A", "E1", "E2", 5)])

    def test_invalid_interval(self):
        with self.assertRaises(store.InputError):
            self.load(
                "bad",
                bookings=[
                    {
                        "booking_id": "B1",
                        "visitor": "V1",
                        "area": "A",
                        "start": 10,
                        "end": 10,
                        "quota": 1,
                        "home_escort": "E1",
                    }
                ],
            )

    def test_boolean_is_not_an_integer(self):
        with self.assertRaises(store.InputError):
            self.load(
                "bad",
                bookings=[
                    {
                        "booking_id": "B1",
                        "visitor": "V1",
                        "area": "A",
                        "start": True,
                        "end": 10,
                        "quota": 1,
                        "home_escort": "E1",
                    }
                ],
            )

    def test_unknown_batch_field(self):
        with self.assertRaises(store.InputError):
            self.load("bad", mystery=[{"x": 1}])

    def test_empty_batch_id(self):
        with self.assertRaises(store.InputError):
            service.load(self.conn, {"batch_id": ""})


if __name__ == "__main__":
    unittest.main()
