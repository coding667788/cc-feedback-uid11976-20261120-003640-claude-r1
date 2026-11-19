"""Escort-chain coverage for one booking.

Model
-----
A booking ``[start, end)`` in ``area`` names a ``home_escort``.  Person ``p``
may escort during minute ``m`` iff ``p`` is on duty at ``m`` *and* holds an
effective qualification for ``area`` at ``m``.  That is the person's
``window`` (presence and qualification are each unions of half-open ranges, so
touching segments of the same person merge with no gap).

The *designated escort* is a step function of time: it starts as the home
escort at ``booking.start`` and changes only at a usable handover, where the
new escort takes over at minute ``t``.  Coverage is then, for every stretch of
the designated timeline, the part of that stretch covered by the designated
escort's window.

Handover ``(old, new, t)`` is *usable* only when

* the currently designated escort at ``t`` is ``old``,
* ``t`` lies inside the booking,
* ``t`` is strictly later than the last usable handover of this booking,
* both parties confirm the same booking, area and ``t``,
* both windows continuously cover ``[t-2, t+2)``.

An unusable handover is inert: it never changes the designated escort.  It
therefore cannot be used to fill the old escort's blank time, and any later
handover whose ``old`` depends on the failed one finds itself not designated
and is rejected too.

A gap in the designated escort's window is impassable and stays impassable --
the same person becoming available again does *not* close it, because a
cross-person (or cross-gap) splice would require a handover.  But the escort's
later availability is still coverage: what remains impassable is exactly the
blank time, not everything after it.  This is what makes ``coverage`` a list of
maximal contiguous passable intervals rather than a single prefix.

Only event endpoints (handover minutes and interval ends) are visited, so a
booking may span more than 100 million minutes without per-minute work.

Batch-level rejection (duplicate handover minute, cycle, backwards time,
conflicting IDs) is handled by :mod:`visitor_pass.validation` before anything
is stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import store
from .store import HANDOVER_HALF_WINDOW, Interval

# (start, end, escort, handover_id that introduced this stretch or None)
Segment = tuple[int, int, str, str | None]


@dataclass
class HandoverDecision:
    handover_id: str
    at: int
    old_escort: str
    new_escort: str
    applied: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "handover_id": self.handover_id,
            "at": self.at,
            "old_escort": self.old_escort,
            "new_escort": self.new_escort,
            "applied": self.applied,
            "reason": self.reason,
        }


@dataclass
class Chain:
    """Evaluated escort chain for one booking."""

    booking: store.Booking
    segments: list[Segment] = field(default_factory=list)
    gaps: list[Interval] = field(default_factory=list)
    handovers: list[HandoverDecision] = field(default_factory=list)
    windows: dict[str, list[Interval]] = field(default_factory=dict)

    @property
    def coverage(self) -> list[Interval]:
        """Maximal contiguous passable intervals, merged and time-sorted."""
        return store.merge_intervals((start, end) for start, end, _, _ in self.segments)

    @property
    def passable_minutes(self) -> int:
        return store.interval_length(self.coverage)

    def segment_at(self, minute: int) -> Segment | None:
        for segment in self.segments:
            if segment[0] <= minute < segment[1]:
                return segment
        return None

    def escort_at(self, minute: int) -> str | None:
        segment = self.segment_at(minute)
        return segment[2] if segment else None

    def gap_at(self, minute: int) -> Interval | None:
        for start, end in self.gaps:
            if start <= minute < end:
                return (start, end)
        return None

    def covers(self, start: int, end: int) -> bool:
        return store.interval_contains(self.coverage, start, end)

    def applied_handovers(self) -> list[HandoverDecision]:
        return [decision for decision in self.handovers if decision.applied]

    def as_dict(self) -> dict:
        return {
            "booking_id": self.booking.booking_id,
            "area": self.booking.area,
            "home_escort": self.booking.home_escort,
            "segments": [
                {
                    "start": start,
                    "end": end,
                    "escort": escort,
                    "entered_by": entered_by,
                }
                for start, end, escort, entered_by in self.segments
            ],
            "coverage": [[start, end] for start, end in self.coverage],
            "gaps": [[start, end] for start, end in self.gaps],
            "handovers": [decision.as_dict() for decision in self.handovers],
        }


def _handover_rejection(
    handover: store.Handover,
    booking: store.Booking,
    designated: str,
    last_applied_at: int | None,
    windows: dict[str, list[Interval]],
) -> str | None:
    if handover.booking_id != booking.booking_id:
        return "booking-mismatch"
    if handover.area != booking.area:
        return "area-mismatch"
    if handover.at < booking.start or handover.at >= booking.end:
        return "outside-booking"
    if last_applied_at is not None and handover.at <= last_applied_at:
        return "backwards-time"
    if handover.old_escort == handover.new_escort:
        return "self-handover"
    if not handover.confirms(handover.old_escort, booking):
        return "old-not-confirmed"
    if not handover.confirms(handover.new_escort, booking):
        return "new-not-confirmed"
    if designated != handover.old_escort:
        return "not-designated-escort"
    lo = handover.at - HANDOVER_HALF_WINDOW
    hi = handover.at + HANDOVER_HALF_WINDOW
    if not store.interval_contains(windows[handover.old_escort], lo, hi):
        return "old-not-continuously-covered"
    if not store.interval_contains(windows[handover.new_escort], lo, hi):
        return "new-not-continuously-covered"
    return None


def build_chain(
    conn: sqlite3.Connection,
    booking: store.Booking,
    allowed_revocations: set[str] | None = None,
) -> Chain:
    """Evaluate the escort chain for ``booking``.

    ``allowed_revocations`` limits which stored revocations apply (``None`` =
    all).  Audit replay uses it to compare a receipt's verdict before and
    after a revocation batch.
    """
    cache: dict[str, list[Interval]] = {}

    def window(person: str) -> list[Interval]:
        if person not in cache:
            presence = store.merge_intervals(store.list_presence(conn, person))
            qualified = store.effective_qualification(
                conn, person, booking.area, allowed_revocations
            )
            cache[person] = store.intersect_intervals(presence, qualified)
        return cache[person]

    window(booking.home_escort)

    # Designated-escort timeline as (start, escort, entered_by).
    periods: list[tuple[int, str, str | None]] = [
        (booking.start, booking.home_escort, None)
    ]
    decisions: list[HandoverDecision] = []
    last_applied_at: int | None = None

    for handover in store.list_handovers(conn, booking.booking_id):
        window(handover.old_escort)
        window(handover.new_escort)
        designated = periods[-1][1]
        reason = _handover_rejection(
            handover, booking, designated, last_applied_at, cache
        )
        applied = reason is None
        decisions.append(
            HandoverDecision(
                handover.handover_id,
                handover.at,
                handover.old_escort,
                handover.new_escort,
                applied,
                "applied" if applied else (reason or "rejected"),
            )
        )
        if applied:
            periods.append((handover.at, handover.new_escort, handover.handover_id))
            last_applied_at = handover.at

    chain = Chain(booking=booking, handovers=decisions, windows=cache)

    for index, (period_start, escort, entered_by) in enumerate(periods):
        period_end = (
            periods[index + 1][0] if index + 1 < len(periods) else booking.end
        )
        start = max(period_start, booking.start)
        end = min(period_end, booking.end)
        if start >= end:
            continue
        covered = store.intersect_intervals([(start, end)], window(escort))
        for cov_start, cov_end in covered:
            chain.segments.append((cov_start, cov_end, escort, entered_by))
        cursor = start
        for cov_start, cov_end in covered:
            if cov_start > cursor:
                chain.gaps.append((cursor, cov_start))
            cursor = max(cursor, cov_end)
        if cursor < end:
            chain.gaps.append((cursor, end))

    chain.gaps = store.merge_intervals(chain.gaps)
    return chain


def evidence_at(chain: Chain, minute: int) -> dict:
    """Escort-chain evidence up to ``minute`` (segments, gaps, handovers)."""
    segment = chain.segment_at(minute)
    timeline: list[dict] = []
    for start, end, escort, entered_by in chain.segments:
        if start > minute:
            break
        timeline.append(
            {
                "kind": "escort",
                "from": start,
                "to": end,
                "escort": escort,
                "entered_by": entered_by,
            }
        )
    for start, end in chain.gaps:
        if start > minute:
            break
        timeline.append({"kind": "gap", "from": start, "to": end})
    timeline.sort(key=lambda item: item["from"])
    return {
        "escort": segment[2] if segment else None,
        "segment": (
            {
                "start": segment[0],
                "end": segment[1],
                "escort": segment[2],
                "entered_by": segment[3],
            }
            if segment
            else None
        ),
        "handovers_used": [
            decision.as_dict()
            for decision in chain.applied_handovers()
            if decision.at <= minute
        ],
        "gaps_before": [
            {"start": start, "end": end}
            for start, end in chain.gaps
            if end <= minute
        ],
        "timeline": timeline,
    }
