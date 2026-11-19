"""Transaction-scoped service: load, coverage, admit, verify.

Every mutating command runs in ``BEGIN IMMEDIATE`` so that revocation loads,
receipt adjudication and quota updates are serialised against one snapshot.
Read-only commands run in a deferred transaction, so they also read a single
consistent snapshot.

``admit`` is idempotent on ``request_id``: a repeat always returns the stored
first receipt verbatim (plus audit notes appended later by revocations).  It
never re-adjudicates and never consumes a second quota slot.  A refreshed
booking must therefore be adjudicated under a new request ID.

Quota is per booking and counts admitted receipts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import chain as chain_mod
from . import store
from . import validation


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@contextmanager
def _transaction(conn: sqlite3.Connection, immediate: bool) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")


def write_transaction(conn: sqlite3.Connection):
    return _transaction(conn, True)


def read_transaction(conn: sqlite3.Connection):
    return _transaction(conn, False)


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

def load(conn: sqlite3.Connection, batch: dict) -> dict:
    if not isinstance(batch, dict):
        raise store.InputError("batch must be a JSON object")
    batch_id = batch.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise store.InputError("batch_id must be a non-empty string")
    digest = canonical_digest(batch)

    with write_transaction(conn):
        prior = store.get_applied_batch(conn, batch_id)
        if prior is not None:
            if prior["digest"] != digest:
                raise store.ConflictError(
                    "batch-conflict",
                    f"batch {batch_id!r} was already applied with different "
                    "content; use a new batch_id",
                )
            return {
                "ok": True,
                "batch_id": batch_id,
                "applied": False,
                "idempotent": True,
                "counts": {},
            }

        plan = validation.validate_batch(conn, batch)
        now = utc_now()
        counts = {
            "bookings_new": 0,
            "bookings_existing": 0,
            "presence_new": 0,
            "qualifications_new": 0,
            "revocations_new": 0,
            "handovers_new": 0,
            "duplicates_ignored": plan.skipped_duplicates,
            "receipts_reaudited": 0,
        }

        for booking in plan.bookings:
            cursor = conn.execute(
                "INSERT INTO booking(booking_id, visitor, area, start, end, "
                "quota, home_escort) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(booking_id) DO NOTHING",
                (
                    booking.booking_id,
                    booking.visitor,
                    booking.area,
                    booking.start,
                    booking.end,
                    booking.quota,
                    booking.home_escort,
                ),
            )
            if cursor.rowcount:
                counts["bookings_new"] += 1
                continue
            counts["bookings_existing"] += 1
            stored = store.get_booking(conn, booking.booking_id)
            if stored is None or stored.content() != booking.content():
                raise store.ConflictError(
                    "booking-conflict",
                    f"booking {booking.booking_id} is already stored with "
                    "different content; bookings are immutable",
                )

        for presence in plan.presence:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO presence(person, start, end, source_id) "
                "VALUES (?, ?, ?, ?)",
                (presence.person, presence.start, presence.end,
                 presence.source_id),
            )
            counts["presence_new"] += cursor.rowcount

        for qual in plan.qualifications:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO qualification"
                "(person, area, start, end, source_id) VALUES (?, ?, ?, ?, ?)",
                (qual.person, qual.area, qual.start, qual.end, qual.source_id),
            )
            counts["qualifications_new"] += cursor.rowcount

        for revocation in plan.revocations:
            conn.execute(
                "INSERT INTO revocation(revocation_id, person, area, effective) "
                "VALUES (?, ?, ?, ?)",
                (revocation.revocation_id, revocation.person, revocation.area,
                 revocation.effective),
            )
            counts["revocations_new"] += 1

        for handover in plan.handovers:
            conn.execute(
                "INSERT INTO handover(batch_id, handover_id, booking_id, area, "
                "old_escort, new_escort, at, confirmations) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    handover.handover_id,
                    handover.booking_id,
                    handover.area,
                    handover.old_escort,
                    handover.new_escort,
                    handover.at,
                    json.dumps(
                        [
                            {
                                "person": c.person,
                                "booking_id": c.booking_id,
                                "area": c.area,
                                "at": c.at,
                            }
                            for c in handover.confirmations
                        ],
                        sort_keys=True,
                    ),
                ),
            )
            counts["handovers_new"] += 1

        if plan.revocations:
            counts["receipts_reaudited"] = _audit_revocations(
                conn, plan.revocations, now
            )

        conn.execute(
            "INSERT INTO applied_batch(batch_id, digest, applied_at) "
            "VALUES (?, ?, ?)",
            (batch_id, digest, now),
        )
        store.bump_state_version(conn)

    return {
        "ok": True,
        "batch_id": batch_id,
        "applied": True,
        "idempotent": False,
        "counts": counts,
    }


# --------------------------------------------------------------------------
# Revocation impact on already-issued receipts
# --------------------------------------------------------------------------

def _audit_revocations(
    conn: sqlite3.Connection, new_revocations: list[store.Revocation], now: str
) -> int:
    """Append one audit row per admitted receipt whose conclusion changes.

    The receipt row itself is never modified.  The row names the first newly
    loaded revocation (in effective-minute order) whose application changes
    the escort at the receipt minute -- the first evidence for the change.
    """
    new_ids = {rev.revocation_id for rev in new_revocations}
    all_ids = store.all_revocation_ids(conn)
    before_ids = all_ids - new_ids
    relevant_areas = {rev.area for rev in new_revocations}

    audited = 0
    for row in store.list_admissions(conn):
        if not int(row["admitted"]):
            continue
        booking = store.get_booking(conn, row["booking_id"])
        if booking is None or booking.area not in relevant_areas:
            continue
        minute = int(row["at"])
        escort_before = chain_mod.build_chain(
            conn, booking, before_ids
        ).escort_at(minute)
        escort_after = chain_mod.build_chain(
            conn, booking, all_ids
        ).escort_at(minute)
        if escort_before == escort_after:
            continue

        relevant = sorted(
            (rev for rev in new_revocations if rev.area == booking.area),
            key=lambda rev: (rev.effective, rev.revocation_id),
        )
        allowed = set(before_ids)
        current = escort_before
        first_cause = relevant[0]
        for rev in relevant:
            allowed.add(rev.revocation_id)
            step = chain_mod.build_chain(conn, booking, allowed).escort_at(minute)
            if step != current:
                first_cause = rev
                break
            current = step

        conn.execute(
            "INSERT OR IGNORE INTO admission_impact("
            "request_id, revocation_id, batch_id, first_cause, verdict_before, "
            "verdict_after, escort_before, escort_after, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["request_id"],
                first_cause.revocation_id,
                "",
                _describe_first_cause(first_cause, minute, escort_before,
                                      escort_after),
                "admitted",
                "admitted" if escort_after is not None else "rejected",
                escort_before,
                escort_after,
                now,
            ),
        )
        audited += 1
    return audited


def _describe_first_cause(
    revocation: store.Revocation,
    minute: int,
    escort_before: str | None,
    escort_after: str | None,
) -> str:
    return (
        f"revocation {revocation.revocation_id}: qualification of "
        f"{revocation.person} for {revocation.area} is cancelled from "
        f"t={revocation.effective}; escort at t={minute} changes from "
        f"{escort_before or 'none'} to {escort_after or 'none'}"
    )


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

def coverage(
    conn: sqlite3.Connection,
    booking_id: str,
    from_minute: int | None = None,
    to_minute: int | None = None,
) -> dict:
    with read_transaction(conn):
        booking = store.get_booking(conn, booking_id)
        if booking is None:
            raise store.InputError(f"unknown booking {booking_id!r}")
        chain = chain_mod.build_chain(conn, booking)
        version = store.state_version(conn)
    return _coverage_payload(booking, chain, from_minute, to_minute, version)


def coverage_all(conn: sqlite3.Connection) -> dict:
    with read_transaction(conn):
        bookings = store.list_bookings(conn)
        chains = [
            (booking, chain_mod.build_chain(conn, booking))
            for booking in bookings
        ]
        version = store.state_version(conn)
    return {
        "ok": True,
        "state_version": version,
        "bookings": [
            _coverage_payload(booking, chain, None, None, version)
            for booking, chain in chains
        ],
    }


def _clip_segments(
    segments: list[chain_mod.Segment], lo: int, hi: int
) -> list[chain_mod.Segment]:
    clipped = []
    for start, end, escort, entered_by in segments:
        new_start = max(start, lo)
        new_end = min(end, hi)
        if new_start < new_end:
            clipped.append((new_start, new_end, escort, entered_by))
    return clipped


def _coverage_payload(
    booking: store.Booking,
    chain: chain_mod.Chain,
    from_minute: int | None,
    to_minute: int | None,
    version: int,
) -> dict:
    lo = booking.start if from_minute is None else max(booking.start, from_minute)
    hi = booking.end if to_minute is None else min(booking.end, to_minute)
    if lo >= hi:
        segments: list[chain_mod.Segment] = []
        gaps: list[store.Interval] = []
    else:
        segments = _clip_segments(chain.segments, lo, hi)
        gaps = [
            (max(start, lo), min(end, hi))
            for start, end in chain.gaps
            if max(start, lo) < min(end, hi)
        ]

    passable = [
        {
            "start": start,
            "end": end,
            "escort": escort,
            "minutes": end - start,
            "entered_by": entered_by,
        }
        for start, end, escort, entered_by in segments
    ]

    # Actual escort changes within the reported range, time-sorted.
    changes: list[dict] = []
    previous: str | None = None
    for start, _end, escort, entered_by in segments:
        if previous is not None and escort != previous:
            changes.append(
                {
                    "at": start,
                    "from": previous,
                    "to": escort,
                    "handover_id": entered_by,
                }
            )
        previous = escort

    largest = None
    for run_start, run_end in store.merge_intervals(
        (start, end) for start, end, _, _ in segments
    ):
        if largest is None or run_end - run_start > largest["minutes"]:
            largest = {"start": run_start, "end": run_end,
                       "minutes": run_end - run_start}
    if largest is not None:
        inside = [
            segment for segment in segments
            if largest["start"] <= segment[0] < largest["end"]
        ]
        largest["escorts"] = [segment[2] for segment in inside]

    return {
        "ok": True,
        "booking_id": booking.booking_id,
        "visitor": booking.visitor,
        "area": booking.area,
        "booking_window": {"start": booking.start, "end": booking.end},
        "state_version": version,
        "coverage": [
            {"start": start, "end": end, "minutes": end - start}
            for start, end in store.merge_intervals(
                (start, end) for start, end, _, _ in segments
            )
        ],
        "passable": passable,
        "gaps": [{"start": start, "end": end} for start, end in gaps],
        "largest": largest,
        "changes": changes,
        "passable_minutes": store.interval_length(
            store.merge_intervals((start, end) for start, end, _, _ in segments)
        ),
        "handovers": [decision.as_dict() for decision in chain.handovers],
        "scan": {
            "method": "event-endpoint-scan",
            "handover_events": len(chain.handovers),
            "interval_endpoints": 2 * len(segments),
            "minutes_iterated": 0,
        },
    }


# --------------------------------------------------------------------------
# admit
# --------------------------------------------------------------------------

def admit(
    conn: sqlite3.Connection, booking_id: str, at: int, request_id: str
) -> dict:
    if not isinstance(booking_id, str) or not booking_id:
        raise store.InputError("booking must be a non-empty string")
    if isinstance(at, bool) or not isinstance(at, int):
        raise store.InputError("at must be an integer")
    if not isinstance(request_id, str) or not request_id:
        raise store.InputError("request_id must be a non-empty string")

    with write_transaction(conn):
        existing = store.get_admission(conn, request_id)
        if existing is not None:
            mismatch = None
            if existing["booking_id"] != booking_id or existing["at"] != at:
                mismatch = {
                    "requested_booking": booking_id,
                    "requested_at": at,
                    "note": "idempotency key already answered another target; "
                            "the first receipt is returned unchanged",
                }
            return {
                "ok": True,
                "idempotent": True,
                "request_mismatch": mismatch,
                "receipt": _render_receipt(conn, existing),
            }

        booking = store.get_booking(conn, booking_id)
        if booking is None:
            raise store.InputError(f"unknown booking {booking_id!r}")

        snapshot = store.state_version(conn)
        chain = chain_mod.build_chain(conn, booking)
        in_window = booking.start <= at < booking.end
        covered = in_window and chain.covers(at, at + 1)
        evidence = chain_mod.evidence_at(chain, at)
        escort = evidence["escort"]

        if not in_window:
            admitted = False
            reason = "outside-booking-window"
            detail = {
                "booking_window": {"start": booking.start, "end": booking.end}
            }
        elif not covered:
            admitted = False
            gap = chain.gap_at(at)
            reason = "no-escort-coverage" if gap is not None else "not-covered"
            detail = {
                "gap": {"start": gap[0], "end": gap[1]} if gap else None,
                "gaps": [{"start": s, "end": e} for s, e in chain.gaps],
            }
        else:
            used = store.admission_count(conn, booking_id)
            if used >= booking.quota:
                admitted = False
                reason = "quota-exhausted"
                detail = {
                    "quota": booking.quota,
                    "used": used,
                    "escort": escort,
                }
            else:
                admitted = True
                reason = "escort-covered"
                detail = {
                    "quota": booking.quota,
                    "used_before": used,
                    "remaining_after": booking.quota - used - 1,
                    "segment": evidence["segment"],
                    "handovers_used": evidence["handovers_used"],
                }

        created_at = utc_now()
        receipt = {
            "request_id": request_id,
            "booking_id": booking_id,
            "at": at,
            "admitted": admitted,
            "reason": reason,
            "escort": escort,
            "eligible": covered,
            "snapshot": snapshot,
            "evidence": evidence if admitted else None,
            "detail": detail,
            "created_at": created_at,
        }
        conn.execute(
            "INSERT INTO admission(request_id, booking_id, at, admitted, "
            "eligible, reason, escort, payload, snapshot, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                booking_id,
                at,
                int(admitted),
                int(covered),
                reason,
                escort,
                json.dumps(receipt, sort_keys=True),
                snapshot,
                created_at,
            ),
        )
        store.bump_state_version(conn)

    receipt["impacted_by"] = []
    return {
        "ok": True,
        "idempotent": False,
        "request_mismatch": None,
        "receipt": receipt,
    }


def _render_receipt(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    try:
        receipt = json.loads(row["payload"])
    except (TypeError, ValueError):
        receipt = {
            "request_id": row["request_id"],
            "booking_id": row["booking_id"],
            "at": row["at"],
            "admitted": bool(row["admitted"]),
            "reason": row["reason"],
            "escort": row["escort"],
            "eligible": bool(row["eligible"]),
            "snapshot": row["snapshot"],
            "created_at": row["created_at"],
        }
    receipt["impacted_by"] = [
        {
            "revocation_id": impact["revocation_id"],
            "first_cause": impact["first_cause"],
            "verdict_before": impact["verdict_before"],
            "verdict_after": impact["verdict_after"],
            "escort_before": impact["escort_before"],
            "escort_after": impact["escort_after"],
            "recorded_at": impact["recorded_at"],
        }
        for impact in store.list_impacts(conn, row["request_id"])
    ]
    return receipt


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify(conn: sqlite3.Connection) -> dict:
    """Recompute everything and report drift; never mutate the database."""
    with read_transaction(conn):
        bookings = store.list_bookings(conn)
        chains = {
            booking.booking_id: chain_mod.build_chain(conn, booking)
            for booking in bookings
        }
        receipts = store.list_admissions(conn)

        quota = []
        for booking in bookings:
            used = store.admission_count(conn, booking.booking_id)
            quota.append(
                {
                    "booking_id": booking.booking_id,
                    "quota": booking.quota,
                    "admitted_receipts": used,
                    "remaining": max(0, booking.quota - used),
                    "over_quota": used > booking.quota,
                }
            )

        drift: list[dict] = []
        unregistered = 0
        receipt_checks: list[dict] = []
        for row in receipts:
            booking = store.get_booking(conn, row["booking_id"])
            impacts = store.list_impacts(conn, row["request_id"])
            payload_ok = True
            try:
                payload = json.loads(row["payload"])
                payload_ok = (
                    payload.get("request_id") == row["request_id"]
                    and payload.get("booking_id") == row["booking_id"]
                    and payload.get("at") == row["at"]
                    and bool(payload.get("admitted")) == bool(row["admitted"])
                )
            except (TypeError, ValueError):
                payload_ok = False
            receipt_checks.append(
                {
                    "request_id": row["request_id"],
                    "booking_id": row["booking_id"],
                    "at": row["at"],
                    "admitted": bool(row["admitted"]),
                    "receipt_intact": payload_ok,
                    "impact_rows": len(impacts),
                }
            )
            if not payload_ok:
                drift.append(
                    {
                        "request_id": row["request_id"],
                        "issue": "receipt-payload-corrupt",
                        "severity": "hard",
                        "impact_registered": bool(impacts),
                    }
                )
                unregistered += 1
            if booking is None:
                drift.append(
                    {
                        "request_id": row["request_id"],
                        "booking_id": row["booking_id"],
                        "issue": "booking-missing",
                        "severity": "hard",
                        "impact_registered": bool(impacts),
                    }
                )
                unregistered += 1
                continue
            if not row["admitted"]:
                continue
            minute = int(row["at"])
            current = chains[booking.booking_id].escort_at(minute)
            stored = row["escort"]
            if current is None:
                drift.append(
                    {
                        "request_id": row["request_id"],
                        "booking_id": row["booking_id"],
                        "at": minute,
                        "issue": "receipt-no-longer-covered",
                        "severity": "hard",
                        "receipt_escort": stored,
                        "current_escort": None,
                        "impact_registered": bool(impacts),
                    }
                )
                if not impacts:
                    unregistered += 1
            elif current != stored:
                drift.append(
                    {
                        "request_id": row["request_id"],
                        "booking_id": row["booking_id"],
                        "at": minute,
                        "issue": "escort-changed-since-receipt",
                        "severity": "soft",
                        "receipt_escort": stored,
                        "current_escort": current,
                        "impact_registered": bool(impacts),
                    }
                )
                if not impacts:
                    unregistered += 1

        known_receipts = {row["request_id"] for row in receipts}
        orphan_impacts = [
            {
                "request_id": impact["request_id"],
                "revocation_id": impact["revocation_id"],
            }
            for impact in conn.execute(
                "SELECT request_id, revocation_id FROM admission_impact"
            )
            if impact["request_id"] not in known_receipts
        ]

        known_bookings = {booking.booking_id for booking in bookings}
        orphan_handovers = [
            handover.handover_id
            for handover in store.list_all_handovers(conn)
            if handover.booking_id not in known_bookings
        ]

        chain_report = []
        for booking in bookings:
            chain = chains[booking.booking_id]
            chain_report.append(
                {
                    "booking_id": booking.booking_id,
                    "segments": len(chain.segments),
                    "passable_minutes": chain.passable_minutes,
                    "coverage": [[start, end] for start, end in chain.coverage],
                    "gaps": [[start, end] for start, end in chain.gaps],
                    "applied_handovers": [
                        decision.handover_id
                        for decision in chain.applied_handovers()
                    ],
                    "rejected_handovers": [
                        decision.handover_id
                        for decision in chain.handovers
                        if not decision.applied
                    ],
                }
            )

        version = store.state_version(conn)

    over_quota = any(entry["over_quota"] for entry in quota)
    receipts_intact = all(check["receipt_intact"] for check in receipt_checks)
    invariants = {
        "quota_respected": not over_quota,
        "receipts_intact": receipts_intact,
        "drifted_receipts_registered": unregistered == 0,
        "no_orphan_impacts": not orphan_impacts,
        "no_orphan_handovers": not orphan_handovers,
    }
    return {
        "ok": all(invariants.values()),
        "state_version": version,
        "bookings": len(bookings),
        "receipts": len(receipts),
        "chains": chain_report,
        "quota": quota,
        "receipt_checks": receipt_checks,
        "receipt_drift": drift,
        "orphan_impacts": orphan_impacts,
        "orphan_handovers": orphan_handovers,
        "invariants": invariants,
    }
