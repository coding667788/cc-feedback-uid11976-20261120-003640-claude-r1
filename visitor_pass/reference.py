"""Reference adjudication: the slow, literal, per-minute model.

This module is deliberately naive.  It is the oracle the swept implementation is
checked against in the random tests.  It stores nothing in SQLite and walks one
minute at a time, so it must never be used for anything but small inputs.

Inputs are deliberately shaped differently from ``visitor_pass.store`` so the
test is not just re-running the same code:

* ``presence_by_person``: ``{person: [(start, end), ...]}``
* ``qualifications``: ``[(person, area, start, end), ...]``
* ``revocations``: ``[(person, area, effective), ...]``
* ``handovers``: list of dicts with ``id/booking_id/area/old/new/at`` and
  ``confirmations`` as ``(person, booking_id, area, at)`` tuples.
"""

from __future__ import annotations


def _in_any(intervals, minute):
    return any(start <= minute < end for start, end in intervals)


def person_minute_status(
    presence_by_person, qualifications, revocations, person, area, minutes
):
    """Map minute -> bool for one person, checking duty *and* qualification.

    A revocation cancels every qualification minute of that person/area from
    its ``effective`` minute onwards.
    """
    duty = presence_by_person.get(person, [])
    earliest_revocation = min(
        (eff for p, a, eff in revocations if p == person and a == area),
        default=None,
    )
    status = {}
    for minute in minutes:
        on_duty = _in_any(duty, minute)
        qualified = any(
            p == person and a == area and s <= minute < e
            for p, a, s, e in qualifications
        )
        if qualified and earliest_revocation is not None and minute >= earliest_revocation:
            qualified = False
        status[minute] = on_duty and qualified
    return status


def coverage_minutes(booking, presence_by_person, qualifications, revocations,
                     handovers):
    """Return (passable_minutes, designated_per_minute, usable_handovers)."""
    booking_id, area, start, end, home_escort = booking
    minutes = list(range(start, end))
    people = {home_escort}
    for handover in handovers:
        people.add(handover["old"])
        people.add(handover["new"])
    status = {
        person: person_minute_status(
            presence_by_person, qualifications, revocations, person, area, minutes
        )
        for person in people
    }

    def covered(person, minute):
        return status.get(person, {}).get(minute, False)

    def continuous(person, lo, hi):
        return all(covered(person, m) for m in range(lo, hi))

    designated = [home_escort] * (end - start)
    last_applied_at = None
    usable = {}

    for handover in sorted(handovers, key=lambda item: (item["at"], item["id"])):
        hid = handover["id"]
        usable[hid] = False
        if handover["booking_id"] != booking_id:
            continue
        if handover["area"] != area:
            continue
        at = handover["at"]
        if at < start or at >= end:
            continue
        if last_applied_at is not None and at <= last_applied_at:
            continue
        old, new = handover["old"], handover["new"]
        if old == new:
            continue
        confirmations = set(handover["confirmations"])
        if (old, booking_id, area, at) not in confirmations:
            continue
        if (new, booking_id, area, at) not in confirmations:
            continue
        if designated[at - start] != old:
            continue
        if not (continuous(old, at - 2, at + 2)
                and continuous(new, at - 2, at + 2)):
            continue
        usable[hid] = True
        for minute in range(at, end):
            designated[minute - start] = new
        last_applied_at = at

    passable = {
        minute
        for minute in minutes
        if covered(designated[minute - start], minute)
    }
    return passable, designated, usable


def admit(booking, presence_by_person, qualifications, revocations, handovers, at):
    """Return (admitted, escort) judged minute-by-minute."""
    booking_id, area, start, end, _home = booking
    if not (start <= at < end):
        return False, None
    passable, designated, _usable = coverage_minutes(
        booking, presence_by_person, qualifications, revocations, handovers
    )
    if at not in passable:
        return False, None
    return True, designated[at - start]


def contiguous_runs(passable_minutes):
    """Turn a set of minutes into sorted half-open intervals."""
    if not passable_minutes:
        return []
    ordered = sorted(passable_minutes)
    runs = [[ordered[0], ordered[0] + 1]]
    for minute in ordered[1:]:
        if minute == runs[-1][1]:
            runs[-1][1] = minute + 1
        else:
            runs.append([minute, minute + 1])
    return [(start, end) for start, end in runs]
