"""Batch validation for ``load``.

A batch is applied atomically.  The following problems reject the *whole*
batch rather than a single entry:

* a handover ID repeating with different content,
* a revocation ID repeating with different content,
* two handovers for the same booking at the same minute,
* a handover declared earlier than one already stored for that booking
  (backwards time),
* a directed cycle in the escort handover graph of a booking.

An identical repetition (same ID, same content) is an idempotent no-op.
Unknown bookings on a handover, malformed fields and duplicate booking IDs
with conflicting content are also batch errors.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import store

BATCH_SECTIONS = ("bookings", "presence", "qualifications", "revocations", "handovers")


@dataclass
class BatchPlan:
    bookings: list[store.Booking] = field(default_factory=list)
    presence: list[store.Presence] = field(default_factory=list)
    qualifications: list[store.Qualification] = field(default_factory=list)
    revocations: list[store.Revocation] = field(default_factory=list)
    handovers: list[store.Handover] = field(default_factory=list)
    skipped_duplicates: int = 0


def _section(batch: dict, name: str) -> list:
    raw = batch.get(name, [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise store.InputError(f"batch section {name!r} must be a list")
    return raw


def _dedupe_bookings(raw: list) -> list[store.Booking]:
    seen: dict[str, store.Booking] = {}
    for item in raw:
        booking = store.parse_booking(item)
        prior = seen.get(booking.booking_id)
        if prior is not None:
            if prior.content() != booking.content():
                raise store.ConflictError(
                    "booking-conflict",
                    f"booking {booking.booking_id} appears twice with different content",
                )
            continue
        seen[booking.booking_id] = booking
    return list(seen.values())


def _dedupe_presence(raw: list) -> list[store.Presence]:
    seen: set[tuple] = set()
    out: list[store.Presence] = []
    for item in raw:
        presence = store.parse_presence(item)
        key = (presence.person, presence.start, presence.end, presence.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(presence)
    return out


def _dedupe_qualifications(raw: list) -> list[store.Qualification]:
    seen: set[tuple] = set()
    out: list[store.Qualification] = []
    for item in raw:
        qual = store.parse_qualification(item)
        key = (qual.person, qual.area, qual.start, qual.end, qual.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(qual)
    return out


def _dedupe_revocations(
    conn: sqlite3.Connection, raw: list
) -> tuple[list[store.Revocation], int]:
    seen: dict[str, store.Revocation] = {}
    skipped = 0
    for item in raw:
        revocation = store.parse_revocation(item)
        prior = seen.get(revocation.revocation_id)
        if prior is not None:
            if prior.content() != revocation.content():
                raise store.ConflictError(
                    "revocation-conflict",
                    f"revocation {revocation.revocation_id} appears twice "
                    "with different content",
                )
            skipped += 1
            continue
        stored = store.get_revocation(conn, revocation.revocation_id)
        if stored is not None:
            if stored.content() != revocation.content():
                raise store.ConflictError(
                    "revocation-conflict",
                    f"revocation {revocation.revocation_id} already stored "
                    "with different content",
                )
            skipped += 1
            continue
        seen[revocation.revocation_id] = revocation
    return list(seen.values()), skipped


def _dedupe_handovers(
    conn: sqlite3.Connection, raw: list
) -> tuple[list[store.Handover], int]:
    seen: dict[str, store.Handover] = {}
    skipped = 0
    for item in raw:
        handover = store.parse_handover(item)
        prior = seen.get(handover.handover_id)
        if prior is not None:
            if prior.content() != handover.content():
                raise store.ConflictError(
                    "handover-conflict",
                    f"handover {handover.handover_id} appears twice "
                    "with different content",
                )
            skipped += 1
            continue
        stored = store.get_handover(conn, handover.handover_id)
        if stored is not None:
            if stored.content() != handover.content():
                raise store.ConflictError(
                    "handover-conflict",
                    f"handover {handover.handover_id} already stored "
                    "with different content",
                )
            skipped += 1
            continue
        seen[handover.handover_id] = handover
    return list(seen.values()), skipped


def _has_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    """Return one directed cycle as a node list, or None.

    Iterative depth-first search: a handover graph is a simple path in normal
    use but can be thousands of links long, which would blow Python's
    recursion limit if this were recursive.
    """
    adjacency: dict[str, list[str]] = {}
    for old, new in edges:
        adjacency.setdefault(old, []).append(new)

    white, gray, black = 0, 1, 2
    color: dict[str, int] = {}
    for root in list(adjacency):
        if color.get(root, white) != white:
            continue
        # Each frame is (node, index of next neighbour to visit, path position).
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        position: dict[str, int] = {root: 0}
        color[root] = gray
        while stack:
            node, index = stack[-1]
            neighbours = adjacency.get(node, ())
            if index >= len(neighbours):
                stack.pop()
                color[node] = black
                position.pop(node, None)
                path.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = neighbours[index]
            state = color.get(neighbour, white)
            if state == gray:
                cycle_start = position[neighbour]
                return path[cycle_start:] + [neighbour]
            if state == white:
                color[neighbour] = gray
                position[neighbour] = len(path)
                path.append(neighbour)
                stack.append((neighbour, 0))
    return None


def validate_batch(conn: sqlite3.Connection, batch: dict) -> BatchPlan:
    if not isinstance(batch, dict):
        raise store.InputError("batch must be a JSON object")
    unknown = set(batch) - set(BATCH_SECTIONS) - {"batch_id", "note"}
    if unknown:
        raise store.InputError(f"unknown batch fields: {sorted(unknown)}")

    plan = BatchPlan()
    plan.bookings = _dedupe_bookings(_section(batch, "bookings"))
    plan.presence = _dedupe_presence(_section(batch, "presence"))
    plan.qualifications = _dedupe_qualifications(_section(batch, "qualifications"))
    plan.revocations, skipped_rev = _dedupe_revocations(
        conn, _section(batch, "revocations")
    )
    plan.handovers, skipped_ho = _dedupe_handovers(conn, _section(batch, "handovers"))
    plan.skipped_duplicates = skipped_rev + skipped_ho

    # Bookings must exist, either already stored or in this batch.
    known = {b.booking_id for b in plan.bookings}
    known.update(b.booking_id for b in store.list_bookings(conn))

    batch_minute: dict[tuple[str, int], str] = {}
    for handover in plan.handovers:
        if handover.booking_id not in known:
            raise store.InputError(
                f"handover {handover.handover_id} refers to unknown booking "
                f"{handover.booking_id!r}"
            )
        key = (handover.booking_id, handover.at)
        if key in batch_minute:
            raise store.ConflictError(
                "duplicate-minute",
                f"booking {handover.booking_id} has handovers "
                f"{batch_minute[key]} and {handover.handover_id} both at t={handover.at}",
            )
        batch_minute[key] = handover.handover_id

    for booking_id in {h.booking_id for h in plan.handovers}:
        existing = store.list_handovers(conn, booking_id)
        existing_minutes = {h.at: h.handover_id for h in existing}
        last_existing = max(existing_minutes) if existing_minutes else None
        for handover in (h for h in plan.handovers if h.booking_id == booking_id):
            if last_existing is not None and handover.at < last_existing:
                raise store.ConflictError(
                    "backwards-time",
                    f"handover {handover.handover_id} at t={handover.at} is earlier "
                    f"than stored handover {existing_minutes[last_existing]} "
                    f"at t={last_existing} for booking {booking_id}",
                )
            if last_existing is not None and handover.at == last_existing:
                raise store.ConflictError(
                    "duplicate-minute",
                    f"booking {booking_id} already has handover "
                    f"{existing_minutes[last_existing]} at t={handover.at}",
                )

        edges = [(h.old_escort, h.new_escort) for h in existing]
        edges += [
            (h.old_escort, h.new_escort)
            for h in plan.handovers
            if h.booking_id == booking_id
        ]
        cycle = _has_cycle(edges)
        if cycle:
            raise store.ConflictError(
                "handover-cycle",
                f"booking {booking_id} escort handovers form a cycle: "
                + " -> ".join(cycle),
            )

    return plan
