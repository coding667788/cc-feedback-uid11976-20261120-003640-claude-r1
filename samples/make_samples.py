"""Generate the small self-contained samples used by the README and demo.

Run from the repository root::

    python samples/make_samples.py

The script writes a handful of JSON batch files into ``samples/``.  Each file
is a complete, independent ``load`` input; running the CLI over them in order
(see README) reproduces the walkthrough end to end.
"""

from __future__ import annotations

import json
import os


def write(name, batch):
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(batch, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def confirmation(person, booking_id, area, at):
    return {"person": person, "booking_id": booking_id, "area": area, "at": at}


def handover(handover_id, old, new, at, booking_id="B-DEMO", area="A1"):
    return {
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


def main():
    # 1. Booking, escorts, and two valid chained handovers.
    write(
        "01-base.json",
        {
            "batch_id": "demo-01-base",
            "bookings": [
                {
                    "booking_id": "B-DEMO",
                    "visitor": "Visitor-Chen",
                    "area": "A1",
                    "start": 0,
                    "end": 120,
                    "quota": 3,
                    "home_escort": "Escort-Li",
                }
            ],
            "presence": [
                {"person": "Escort-Li", "start": 0, "end": 52},
                {"person": "Escort-Wang", "start": 48, "end": 92},
                {"person": "Escort-Zhao", "start": 88, "end": 120},
                # A fourth escort is fully available but is never handed to.
                {"person": "Escort-Sun", "start": 0, "end": 120},
            ],
            "qualifications": [
                {"person": "Escort-Li", "area": "A1", "start": 0, "end": 120},
                {"person": "Escort-Wang", "area": "A1", "start": 0, "end": 120},
                {"person": "Escort-Zhao", "area": "A1", "start": 0, "end": 120},
                {"person": "Escort-Sun", "area": "A1", "start": 0, "end": 120},
            ],
            "handovers": [
                handover("HO-1", "Escort-Li", "Escort-Wang", 50),
                handover("HO-2", "Escort-Wang", "Escort-Zhao", 90),
            ],
        },
    )

    # 2. Admission receipts: distinct requests fill the three slots; a fourth
    #    is refused, and a repeated request ID replays the first receipt.
    #    (Run as `admit` commands, not `load`; this file documents the calls.)
    write(
        "02-admissions.json",
        {
            "note": "Commands to run, in order, for the quota demo.",
            "commands": [
                "admit --booking B-DEMO --at 10  --request REQ-1",
                "admit --booking B-DEMO --at 60  --request REQ-2",
                "admit --booking B-DEMO --at 100 --request REQ-3",
                "admit --booking B-DEMO --at 110 --request REQ-4",
                "admit --booking B-DEMO --at 10  --request REQ-1",
            ],
        },
    )

    # 3. Revocation that removes the tail of Escort-Zhao's qualification from
    #    t=100.  Receipt REQ-3 (t=100) was escorted by Zhao: it is re-adjudged
    #    as no longer covered, while HO-2 (at 90, needs Zhao up to 92) survives.
    write(
        "03-revocation.json",
        {
            "batch_id": "demo-03-revocation",
            "revocations": [
                {
                    "revocation_id": "REV-ZHAO",
                    "person": "Escort-Zhao",
                    "area": "A1",
                    "effective": 100,
                }
            ],
        },
    )

    # 4. A batch that must be rejected as a whole: HO-3 and HO-4 share a minute.
    write(
        "04-rejected-duplicate-minute.json",
        {
            "note": "Expected: rejected (duplicate-minute); nothing is stored.",
            "batch_id": "demo-04-duplicate",
            "handovers": [
                handover("HO-3", "Escort-Li", "Escort-Wang", 20),
                handover("HO-4", "Escort-Wang", "Escort-Zhao", 20),
            ],
        },
    )

    # 5. Self-contained micro case for the "one minute short" overlap rule.
    write(
        "05-one-minute-short.json",
        {
            "batch_id": "demo-05-short",
            "bookings": [
                {
                    "booking_id": "B-SHORT",
                    "visitor": "Visitor-Lin",
                    "area": "A2",
                    "start": 0,
                    "end": 40,
                    "quota": 1,
                    "home_escort": "E-A",
                }
            ],
            "presence": [
                {"person": "E-A", "start": 0, "end": 40},
                # E-B arrives at 19, one minute later than t-2=18.
                {"person": "E-B", "start": 19, "end": 40},
            ],
            "qualifications": [
                {"person": "E-A", "area": "A2", "start": 0, "end": 40},
                {"person": "E-B", "area": "A2", "start": 0, "end": 40},
            ],
            "handovers": [handover("HO-SHORT", "E-A", "E-B", 20, "B-SHORT", "A2")],
        },
    )

    print("wrote samples to", os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()
