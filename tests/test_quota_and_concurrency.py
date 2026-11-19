"""Quota, idempotency and two-thread races for the last slot."""

from __future__ import annotations

import threading
import unittest

from support import DatabaseCase
from visitor_pass import service, store


class QuotaCase(DatabaseCase):
    def test_quota_is_per_booking(self):
        self.load(
            "base",
            bookings=[
                {
                    "booking_id": "B1",
                    "visitor": "V1",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 1,
                    "home_escort": "E1",
                },
                {
                    "booking_id": "B2",
                    "visitor": "V2",
                    "area": "A",
                    "start": 0,
                    "end": 100,
                    "quota": 1,
                    "home_escort": "E2",
                },
            ],
            presence=[
                {"person": "E1", "start": 0, "end": 100},
                {"person": "E2", "start": 0, "end": 100},
            ],
            qualifications=[
                {"person": "E1", "area": "A", "start": 0, "end": 100},
                {"person": "E2", "area": "A", "start": 0, "end": 100},
            ],
        )
        self.assertTrue(service.admit(self.conn, "B1", 10, "a")["receipt"]["admitted"])
        self.assertFalse(service.admit(self.conn, "B1", 20, "b")["receipt"]["admitted"])
        # B2 still has its own slot.
        self.assertTrue(service.admit(self.conn, "B2", 10, "c")["receipt"]["admitted"])

    def test_rejected_request_consumes_no_slot(self):
        self.load_base(quota=1)
        self.load(
            "revoke",
            revocations=[
                {"revocation_id": "RV", "person": "E1", "area": "A", "effective": 50}
            ],
        )
        self.assertFalse(service.admit(self.conn, "B1", 60, "denied")["receipt"]["admitted"])
        # The denied request did not take the (single) slot.
        self.assertTrue(service.admit(self.conn, "B1", 10, "ok")["receipt"]["admitted"])
        self.assertEqual(
            service.verify(self.conn)["quota"][0]["admitted_receipts"], 1
        )

    def test_idempotent_repeat_does_not_take_second_slot(self):
        self.load_base(quota=2)
        first = service.admit(self.conn, "B1", 10, "R1")["receipt"]
        self.assertTrue(first["admitted"])
        replay = service.admit(self.conn, "B1", 10, "R1")["receipt"]
        self.assertTrue(replay["admitted"])
        # Still one admitted receipt; a different request lands in slot 2.
        self.assertTrue(service.admit(self.conn, "B1", 20, "R2")["receipt"]["admitted"])
        quota = service.verify(self.conn)["quota"][0]
        self.assertEqual(quota["admitted_receipts"], 2)
        self.assertEqual(quota["remaining"], 0)
        # Third distinct request refused.
        self.assertFalse(service.admit(self.conn, "B1", 30, "R3")["receipt"]["admitted"])


class ConcurrentLastSlotCase(DatabaseCase):
    def test_two_threads_race_for_one_slot(self):
        self.load_base(quota=1)
        start = threading.Barrier(2)
        results: list[tuple] = []
        lock = threading.Lock()

        def worker(request_id):
            conn = store.connect(self.db_path)
            try:
                start.wait(timeout=10)
                result = service.admit(conn, "B1", 10, request_id)
                with lock:
                    results.append(
                        (
                            request_id,
                            result["receipt"]["admitted"],
                            result["receipt"]["reason"],
                        )
                    )
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=(f"R{i}",)) for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(len(results), 2)
        admitted = [row for row in results if row[1]]
        refused = [row for row in results if not row[1]]
        self.assertEqual(len(admitted), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0][2], "quota-exhausted")

        # Exactly one admitted receipt exists; verify agrees.
        self.reopen()
        quota = service.verify(self.conn)["quota"][0]
        self.assertEqual(quota["admitted_receipts"], 1)
        self.assertFalse(quota["over_quota"])
        self.assertTrue(service.verify(self.conn)["ok"])

    def test_many_threads_only_quota_slots_succeed(self):
        self.load_base(quota=3)
        start = threading.Barrier(6)
        results: list[bool] = []
        lock = threading.Lock()

        def worker(request_id):
            conn = store.connect(self.db_path)
            try:
                start.wait(timeout=10)
                admitted = service.admit(
                    conn, "B1", 10, request_id
                )["receipt"]["admitted"]
                with lock:
                    results.append(admitted)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=(f"R{i}",)) for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sum(1 for value in results if value), 3)
        self.reopen()
        report = service.verify(self.conn)
        self.assertTrue(report["ok"])
        self.assertEqual(report["quota"][0]["admitted_receipts"], 3)


if __name__ == "__main__":
    unittest.main()
