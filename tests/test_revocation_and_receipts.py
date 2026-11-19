"""Revocations, receipt preservation, and admission replay."""

from __future__ import annotations

import unittest

from support import DatabaseCase, handover
from visitor_pass import service, store


class RevocationInvalidatesChainCase(DatabaseCase):
    def test_revoking_early_qualification_kills_two_later_handovers(self):
        """E2 loses qualification from t=40: H2 (at 70) and the E2 stretch fail.

        E1 -> E2 at 30 is still valid because it only needs E2's window up to
        t=32.  E2 -> E3 at 70 needs E2 up to 72, which the revocation removes,
        so H2 fails and E3 never becomes designated.
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
                {"person": "E1", "start": 0, "end": 32},
                {"person": "E2", "start": 28, "end": 72},
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
        before = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in before["coverage"]], [(0, 100)]
        )
        self.assertTrue(service.admit(self.conn, "B1", 90, "pre")["receipt"]["admitted"])

        self.load(
            "revoke",
            revocations=[
                {"revocation_id": "R-E2", "person": "E2", "area": "A", "effective": 40}
            ],
        )
        after = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in after["coverage"]], [(0, 40)]
        )
        self.assertEqual([(g["start"], g["end"]) for g in after["gaps"]], [(40, 100)])
        decisions = {h["handover_id"]: h for h in after["handovers"]}
        self.assertTrue(decisions["H1"]["applied"])   # window only to 32
        self.assertFalse(decisions["H2"]["applied"])  # needs E2 to 72
        self.assertIn(
            decisions["H2"]["reason"], ("old-not-continuously-covered",)
        )
        # New adjudication refuses.
        self.assertFalse(service.admit(self.conn, "B1", 90, "post")["receipt"]["admitted"])

class RevocationReachesBackCase(DatabaseCase):
    def test_revocation_from_historic_time_removes_tail(self):
        self.load_base()
        # Sanity: fully covered.
        self.assertTrue(service.admit(self.conn, "B1", 95, "a")["receipt"]["admitted"])
        self.load(
            "r1",
            revocations=[
                {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 50}
            ],
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]], [(0, 50)]
        )
        # Minutes before the effective point are untouched.
        self.assertTrue(service.admit(self.conn, "B1", 49, "b")["receipt"]["admitted"])
        self.assertFalse(service.admit(self.conn, "B1", 50, "c")["receipt"]["admitted"])

    def test_revocation_only_hits_its_own_area(self):
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "BA",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 5,
                    "home_escort": "E1",
                },
                {
                    "booking_id": "BB",
                    "visitor": "V2",
                    "area": "B",
                    "start": 0,
                    "end": 100,
                    "quota": 5,
                    "home_escort": "E1",
                },
            ],
            presence=[{"person": "E1", "start": 0, "end": 100}],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E1", "area": "B", "start": 0, "end": 100},
            ],
        )
        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 0}
            ],
        )
        self.assertEqual(service.coverage(self.conn, "BA")["passable"], [])
        self.assertEqual(
            [(run["start"], run["end"]) for run in service.coverage(self.conn, "BB")["coverage"]],
            [(0, 100)],
        )


class ReceiptPreservationCase(DatabaseCase):
    def _setup_chain(self):
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
            presence=[{"person": "E1", "start": 0, "end": 100}],
            qualifications=[{"person": "E1", "area": "A", "start": 0, "end": 100}],
        )

    def test_old_receipt_is_kept_and_annotated(self):
        self._setup_chain()
        first = service.admit(self.conn, "B1", 60, "R1")["receipt"]
        self.assertTrue(first["admitted"])
        self.assertEqual(first["impacted_by"], [])

        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV1", "person": "E1", "area": "A", "effective": 50}
            ],
        )
        replay = service.admit(self.conn, "B1", 60, "R1")
        self.assertTrue(replay["idempotent"])
        receipt = replay["receipt"]
        # First verdict is untouched...
        self.assertTrue(receipt["admitted"])
        self.assertEqual(receipt["reason"], "escort-covered")
        # ...and a later audit note records the change and its first cause.
        self.assertEqual(len(receipt["impacted_by"]), 1)
        impact = receipt["impacted_by"][0]
        self.assertEqual(impact["revocation_id"], "RV1")
        self.assertEqual(impact["verdict_before"], "admitted")
        self.assertEqual(impact["verdict_after"], "rejected")
        self.assertIn("RV1", impact["first_cause"])

        # New request on the same instant is refused.
        fresh = service.admit(self.conn, "B1", 60, "R2")["receipt"]
        self.assertFalse(fresh["admitted"])
        self.assertEqual(fresh["reason"], "no-escort-coverage")

    def test_receipt_is_returned_verbatim_even_if_requested_target_differs(self):
        self._setup_chain()
        original = service.admit(self.conn, "B1", 10, "R1")["receipt"]
        replay = service.admit(self.conn, "B1", 20, "R1")
        self.assertTrue(replay["idempotent"])
        self.assertIsNotNone(replay["request_mismatch"])
        self.assertEqual(replay["receipt"]["at"], 10)
        self.assertEqual(replay["receipt"]["created_at"], original["created_at"])

    def test_receipt_survives_schema_round_trip(self):
        self._setup_chain()
        service.admit(self.conn, "B1", 60, "R1")
        self.reopen()
        replay = service.admit(self.conn, "B1", 60, "R1")
        self.assertTrue(replay["receipt"]["admitted"])
        self.assertEqual(replay["receipt"]["request_id"], "R1")

    def test_verify_flags_hard_drift_with_registered_impact(self):
        self._setup_chain()
        service.admit(self.conn, "B1", 60, "R1")
        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV1", "person": "E1", "area": "A", "effective": 50}
            ],
        )
        report = service.verify(self.conn)
        self.assertTrue(report["ok"])
        hard = [d for d in report["receipt_drift"] if d["severity"] == "hard"]
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0]["issue"], "receipt-no-longer-covered")
        self.assertTrue(hard[0]["impact_registered"])


class MultipleRevocationsFirstCauseCase(DatabaseCase):
    def test_first_cause_names_earliest_effective_revocation(self):
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
            presence=[{"person": "E1", "start": 0, "end": 100}],
            qualifications=[{"person": "E1", "area": "A", "start": 0, "end": 100}],
        )
        service.admit(self.conn, "B1", 80, "R1")
        # Two revocations in one batch, the earlier one is the first cause.
        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV-LATE", "person": "E1", "area": "A",
                 "effective": 70},
                {"revocation_id": "RV-EARLY", "person": "E1", "area": "A",
                 "effective": 60},
            ],
        )
        receipt = service.admit(self.conn, "B1", 80, "R1")["receipt"]
        self.assertEqual(len(receipt["impacted_by"]), 1)
        self.assertEqual(receipt["impacted_by"][0]["revocation_id"], "RV-EARLY")

    def test_composed_revocations_reduce_to_earliest(self):
        """Two revocations for one person/area compose to the earliest tail cut."""
        self.load_base()
        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV-LATE", "person": "E1", "area": "A",
                 "effective": 70},
                {"revocation_id": "RV-EARLY", "person": "E1", "area": "A",
                 "effective": 40},
            ],
        )
        self.assertEqual(
            store.effective_qualification(self.conn, "E1", "A"), [(0, 40)]
        )
        cov = service.coverage(self.conn, "B1")
        self.assertEqual(
            [(run["start"], run["end"]) for run in cov["coverage"]], [(0, 40)]
        )

    def test_replay_subset_isolates_each_revocation(self):
        self.load_base()
        self.load(
            "r",
            revocations=[
                {"revocation_id": "RV-LATE", "person": "E1", "area": "A",
                 "effective": 70},
                {"revocation_id": "RV-EARLY", "person": "E1", "area": "A",
                 "effective": 40},
            ],
        )
        self.assertEqual(
            store.effective_qualification(self.conn, "E1", "A", set()),
            [(0, 100)],
        )
        self.assertEqual(
            store.effective_qualification(self.conn, "E1", "A", {"RV-LATE"}),
            [(0, 70)],
        )
        self.assertEqual(
            store.effective_qualification(self.conn, "E1", "A", {"RV-EARLY"}),
            [(0, 40)],
        )


if __name__ == "__main__":
    unittest.main()
