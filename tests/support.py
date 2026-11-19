"""Shared fixtures for the visitor-pass test suite."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from visitor_pass import service, store  # noqa: E402


def confirmation(person, booking_id, area, at):
    return {"person": person, "booking_id": booking_id, "area": area, "at": at}


def handover(handover_id, booking_id, area, old, new, at, **extra):
    payload = {
        "handover_id": handover_id,
        "booking_id": booking_id,
        "area": area,
        "old_escort": old,
        "new_escort": new,
        "at": at,
        "confirmations": [
            confirmation(old, booking_id, area, at),
            confirmation(new, booking_id, area, at),
        ],
    }
    payload.update(extra)
    return payload


class DatabaseCase(unittest.TestCase):
    """A fresh on-disk SQLite database per test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="visitor-pass-")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = store.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    def reopen(self):
        self.conn.close()
        self.conn = store.connect(self.db_path)
        return self.conn

    def load(self, batch_id, **sections):
        batch = {"batch_id": batch_id, **sections}
        return service.load(self.conn, batch)

    def base_batch(
        self,
        booking_id="B1",
        start=0,
        end=100,
        quota=9,
        home_escort="E1",
        escorts=("E1", "E2", "E3"),
        area="A",
    ):
        return {
            "batch_id": "base",
            "bookings": [
                {
                    "booking_id": booking_id,
                    "visitor": "V1",
                    "area": area,
                    "start": start,
                    "end": end,
                    "quota": quota,
                    "home_escort": home_escort,
                }
            ],
            "presence": [
                {"person": person, "start": start, "end": end}
                for person in escorts
            ],
            "qualifications": [
                {"person": person, "area": area, "start": start, "end": end}
                for person in escorts
            ],
        }

    def load_base(self, **kwargs):
        batch = self.base_batch(**kwargs)
        batch.pop("batch_id", None)
        self.load("base", **batch)
