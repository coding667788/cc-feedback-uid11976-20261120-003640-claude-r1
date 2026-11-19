"""Random small cases compared against the per-minute reference oracle.

Each iteration builds a random set of escorts, presence/qualification
intervals, revocations and handovers, feeds them through the swept engine, and
compares the passable intervals, contiguous runs, escort identity and admit
verdict at *every* minute against :mod:`visitor_pass.reference`.

The generator deliberately mixes two regimes:

* *dense* -- every escort is on duty and qualified across the booking, so
  handovers actually fire and the chain machinery is exercised;
* *sparse* -- short random duty windows, so gaps, broken overlap windows and
  rejected handovers dominate.

A final diversity assertion makes a vacuous "all rejected" pass impossible.
"""

from __future__ import annotations

import random
import unittest

from support import DatabaseCase, confirmation
from visitor_pass import reference, service


def _span(rng, lo, hi):
    """One non-degenerate half-open range inside [lo, hi)."""
    start = rng.randint(lo, hi - 1)
    end = rng.randint(start + 1, hi)
    return start, end


class RandomReferenceCase(DatabaseCase):
    ITERATIONS = 120
    SPAN = 24
    ESCORTS = ("A", "B", "C", "D")
    AREAS = ("X", "Y")

    # ------------------------------------------------------------------ build

    def _duty(self, rng, escorts, area, start, end):
        presence = []
        qualifications = []
        for escort in escorts:
            presence.append({"person": escort, "start": start, "end": end})
            qualifications.append(
                {"person": escort, "area": area, "start": start, "end": end}
            )
            if rng.random() < 0.4:
                # Punch one hole into duty so dense cases still show gaps.
                hole_lo, hole_hi = _span(rng, start, end)
                presence = [
                    item for item in presence
                    if not (item["person"] == escort
                            and item["start"] == start
                            and item["end"] == end)
                ]
                if hole_lo > start:
                    presence.append(
                        {"person": escort, "start": start, "end": hole_lo}
                    )
                if hole_hi < end:
                    presence.append(
                        {"person": escort, "start": hole_hi, "end": end}
                    )
        return presence, qualifications

    def _sparse(self, rng, escorts, area, start, end):
        presence = []
        qualifications = []
        for escort in escorts:
            for _ in range(rng.randint(1, 4)):
                lo, hi = _span(rng, start, end)
                presence.append({"person": escort, "start": lo, "end": hi})
            for _ in range(rng.randint(1, 4)):
                lo, hi = _span(rng, start, end)
                qualifications.append(
                    {"person": escort, "area": area, "start": lo, "end": hi}
                )
            if rng.random() < 0.3:
                lo, hi = _span(rng, start, end)
                qualifications.append(
                    {
                        "person": escort,
                        "area": "Y" if area == "X" else "X",
                        "start": lo,
                        "end": hi,
                    }
                )
        return presence, qualifications

    def _build_case(self, rng):
        area = rng.choice(self.AREAS)
        start, end = 0, self.SPAN
        escorts = rng.sample(self.ESCORTS, k=rng.randint(2, len(self.ESCORTS)))
        home = escorts[0]
        if rng.random() < 0.5:
            presence, qualifications = self._duty(rng, escorts, area, start, end)
        else:
            presence, qualifications = self._sparse(
                rng, escorts, area, start, end
            )

        handovers = []
        chain = list(escorts)
        rng.shuffle(chain)
        current = home
        used = {current}
        candidate_minutes = list(range(start + 1, end))
        rng.shuffle(candidate_minutes)
        used_minutes: set[int] = set()
        for index in range(rng.randint(0, 2)):
            if not candidate_minutes:
                break
            at = candidate_minutes.pop()
            if at in used_minutes:
                continue
            remaining = [escort for escort in escorts if escort not in used]
            if not remaining:
                break
            used_minutes.add(at)
            new = rng.choice(remaining)
            old = current
            current = new
            used.add(new)
            confirmations = [confirmation(old, "B1", area, at)]
            if rng.random() < 0.9:
                confirmations.append(confirmation(new, "B1", area, at))
            if rng.random() < 0.1:
                confirmations = [
                    confirmation(old, "B1", area, at),
                    confirmation(new, "B1", "Z", at),
                ]
            handovers.append(
                {
                    "handover_id": f"H{index}",
                    "booking_id": "B1",
                    "area": area if rng.random() < 0.97 else "Y",
                    "old_escort": old,
                    "new_escort": new,
                    "at": at,
                    "confirmations": confirmations,
                }
            )
        handovers.sort(key=lambda item: item["at"])

        revocations = []
        if rng.random() < 0.35:
            for index, escort in enumerate(escorts):
                if rng.random() < 0.3:
                    revocations.append(
                        {
                            "revocation_id": f"RV{index}",
                            "person": escort,
                            "area": area,
                            "effective": rng.randint(start, end),
                        }
                    )

        batch = {
            "batch_id": "rand",
            "bookings": [
                {
                    "booking_id": "B1",
                    "visitor": "V",
                    "area": area,
                    "start": start,
                    "end": end,
                    "quota": 999,
                    "home_escort": home,
                }
            ],
            "presence": presence,
            "qualifications": qualifications,
            "handovers": handovers,
        }
        if revocations:
            batch["revocations"] = revocations
        return area, home, batch, handovers, revocations

    # -------------------------------------------------------------- ref input

    def _reference_inputs(self, presence, qualifications, revocations, handovers):
        presence_by_person: dict[str, list[tuple[int, int]]] = {}
        for item in presence:
            presence_by_person.setdefault(item["person"], []).append(
                (item["start"], item["end"])
            )
        quals = [
            (item["person"], item["area"], item["start"], item["end"])
            for item in qualifications
        ]
        revs = [
            (item["person"], item["area"], item["effective"])
            for item in revocations
        ]
        hs = [
            {
                "id": item["handover_id"],
                "booking_id": item["booking_id"],
                "area": item["area"],
                "old": item["old_escort"],
                "new": item["new_escort"],
                "at": item["at"],
                "confirmations": [
                    (c["person"], c["booking_id"], c["area"], c["at"])
                    for c in item["confirmations"]
                ],
            }
            for item in handovers
        ]
        return presence_by_person, quals, revs, hs

    # ------------------------------------------------------------------- test

    def test_random_cases_match_reference(self):
        stats = {"admitted": 0, "rejected": 0, "multiple_runs": 0, "gaps": 0,
                 "handover_applied": 0, "handover_rejected": 0}
        for seed in range(self.ITERATIONS):
            with self.subTest(seed=seed):
                self._run_one(seed, stats)
        # A "pass" is only meaningful if both regimes were exercised.
        self.assertGreater(stats["admitted"], 100, stats)
        self.assertGreater(stats["rejected"], 100, stats)
        self.assertGreater(stats["multiple_runs"], 20, stats)
        self.assertGreater(stats["gaps"], 20, stats)
        self.assertGreater(stats["handover_applied"], 10, stats)
        self.assertGreater(stats["handover_rejected"], 10, stats)

    def _run_one(self, seed, stats):
        rng = random.Random(seed)
        self.tearDown()
        self.setUp()

        area, home, batch, handovers, revocations = self._build_case(rng)
        service.load(self.conn, batch)
        cov = service.coverage(self.conn, "B1")

        duty, quals, revs, ref_handovers = self._reference_inputs(
            batch["presence"], batch["qualifications"], revocations, handovers
        )
        booking = ("B1", area, 0, self.SPAN, home)
        passable, designated, _usable = reference.coverage_minutes(
            booking, duty, quals, revs, ref_handovers
        )

        expected_runs = reference.contiguous_runs(passable)
        actual_runs = [(run["start"], run["end"]) for run in cov["coverage"]]
        self.assertEqual(
            actual_runs, expected_runs,
            f"seed={seed} batch={batch} expected={expected_runs} got={actual_runs}",
        )
        if len(expected_runs) > 1:
            stats["multiple_runs"] += 1
        if any(
            expected_runs[i][1] < expected_runs[i + 1][0]
            for i in range(len(expected_runs) - 1)
        ):
            stats["gaps"] += 1
        for decision in cov["handovers"]:
            key = "handover_applied" if decision["applied"] else "handover_rejected"
            stats[key] += 1

        for minute in range(self.SPAN):
            expected_admitted, expected_escort = reference.admit(
                booking, duty, quals, revs, ref_handovers, minute
            )
            receipt = service.admit(
                self.conn, "B1", minute, f"S{seed}-{minute}"
            )["receipt"]
            stats["admitted" if receipt["admitted"] else "rejected"] += 1
            self.assertEqual(
                receipt["admitted"], expected_admitted,
                f"seed={seed} minute={minute} admitted mismatch "
                f"chain={cov['handovers']} batch={batch}",
            )
            if expected_admitted:
                self.assertEqual(
                    receipt["escort"], expected_escort,
                    f"seed={seed} minute={minute} escort mismatch",
                )
                self.assertEqual(
                    cov["passable"][0]["escort"] is not None, True
                )

        # Whole passable set, not just its runs, must agree.
        actual_minutes = set()
        for run in expected_runs:
            actual_minutes.update(range(run[0], run[1]))
        self.assertEqual(actual_minutes, passable, f"seed={seed} run mismatch")


if __name__ == "__main__":
    unittest.main()
