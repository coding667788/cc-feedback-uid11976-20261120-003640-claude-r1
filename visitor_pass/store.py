"""Domain model, schema and SQLite persistence.

All intervals are half-open integer minute ranges ``[start, end)``.  A booking
fixes a visitor, an area, a quota and a home escort.  Presence records and area
qualifications are also half-open ranges per person.  Handovers stitch two
escorts together at minute ``t``; they are usable only when both parties cover
``[t-2, t+2)`` continuously and both confirm the same booking, area and ``t``.

Qualification rows and revocations are append-only.  A revocation cancels the
affected qualification *from its effective minute onwards*, reaching back into
history: an interval recorded before the revocation that extends past the
effective minute is trimmed at that minute.  Because every revocation removes a
tail, the effective qualification of a person/area is simply the union of its
rows, truncated at the earliest allowed revocation minute.

This is deliberately a *tail cut*, not an order-sensitive edit: once a
revocation exists for a person/area, nothing at or after its effective minute
counts as qualified, whether the row was recorded before or after the
revocation.  A revocation is therefore a hard floor for that person/area; there
is no in-place re-grant that punches a hole through it.  This keeps replay
deterministic and order-independent.  *Clarification*: several revocations for
one person/area compose to the earliest effective minute (each removes a tail,
and a tail minus a tail is the shorter tail), so the stored set only needs to be
reduced to its minimum effective minute when computing windows.

Only the standard library and SQLite are used.  Nothing outside the database
file is consulted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

SCHEMA_VERSION = 1
HANDOVER_HALF_WINDOW = 2
FAR_FUTURE = 1 << 62

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking (
    booking_id  TEXT PRIMARY KEY,
    visitor     TEXT NOT NULL,
    area        TEXT NOT NULL,
    start       INTEGER NOT NULL,
    end         INTEGER NOT NULL,
    quota       INTEGER NOT NULL CHECK (quota >= 0),
    home_escort TEXT NOT NULL,
    CHECK (start < end)
);

CREATE TABLE IF NOT EXISTS presence (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    person    TEXT NOT NULL,
    start     INTEGER NOT NULL,
    end       INTEGER NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    UNIQUE (person, start, end, source_id),
    CHECK (start < end)
);

CREATE TABLE IF NOT EXISTS qualification (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    person    TEXT NOT NULL,
    area      TEXT NOT NULL,
    start     INTEGER NOT NULL,
    end       INTEGER NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    UNIQUE (person, area, start, end, source_id),
    CHECK (start < end)
);

-- A revocation cancels the qualification of ``person`` in ``area`` from
-- ``effective`` onwards, for qualification rows recorded before it.  Rows are
-- kept for audit.
CREATE TABLE IF NOT EXISTS revocation (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    revocation_id TEXT NOT NULL UNIQUE,
    person        TEXT NOT NULL,
    area          TEXT NOT NULL,
    effective     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS handover (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL DEFAULT '',
    handover_id   TEXT NOT NULL UNIQUE,
    booking_id    TEXT NOT NULL,
    area          TEXT NOT NULL,
    old_escort    TEXT NOT NULL,
    new_escort    TEXT NOT NULL,
    at            INTEGER NOT NULL,
    confirmations TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS admission (
    request_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    at         INTEGER NOT NULL,
    admitted   INTEGER NOT NULL,
    eligible   INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    escort     TEXT,
    payload    TEXT NOT NULL,
    snapshot   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- One audit row per admitted receipt per revocation that invalidates it.
-- The original admission row is never modified.
CREATE TABLE IF NOT EXISTS admission_impact (
    request_id     TEXT NOT NULL,
    revocation_id  TEXT NOT NULL,
    batch_id       TEXT NOT NULL DEFAULT '',
    first_cause    TEXT NOT NULL,
    verdict_before TEXT NOT NULL,
    verdict_after  TEXT NOT NULL,
    escort_before  TEXT,
    escort_after   TEXT,
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (request_id, revocation_id)
);

CREATE TABLE IF NOT EXISTS applied_batch (
    batch_id   TEXT PRIMARY KEY,
    digest     TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_presence_person ON presence(person, start, end);
CREATE INDEX IF NOT EXISTS idx_qual_person_area ON qualification(person, area, seq);
CREATE INDEX IF NOT EXISTS idx_handover_booking ON handover(booking_id, at);
CREATE INDEX IF NOT EXISTS idx_admission_booking ON admission(booking_id, admitted);
CREATE INDEX IF NOT EXISTS idx_impact_request ON admission_impact(request_id);
"""


class InputError(ValueError):
    """Raised when a command receives malformed input."""


class ConflictError(RuntimeError):
    """Raised when a batch must be rejected as a whole."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class Booking:
    booking_id: str
    visitor: str
    area: str
    start: int
    end: int
    quota: int
    home_escort: str

    def content(self) -> tuple:
        return (self.visitor, self.area, self.start, self.end, self.quota,
                self.home_escort)

    def as_dict(self) -> dict:
        return {
            "booking_id": self.booking_id,
            "visitor": self.visitor,
            "area": self.area,
            "start": self.start,
            "end": self.end,
            "quota": self.quota,
            "home_escort": self.home_escort,
        }


@dataclass(frozen=True)
class Presence:
    person: str
    start: int
    end: int
    source_id: str = ""


@dataclass(frozen=True)
class Qualification:
    person: str
    area: str
    start: int
    end: int
    source_id: str = ""


@dataclass(frozen=True)
class Revocation:
    revocation_id: str
    person: str
    area: str
    effective: int

    def content(self) -> tuple:
        return (self.person, self.area, self.effective)

    def as_dict(self) -> dict:
        return {
            "revocation_id": self.revocation_id,
            "person": self.person,
            "area": self.area,
            "effective": self.effective,
        }


@dataclass(frozen=True)
class Confirmation:
    person: str
    booking_id: str
    area: str
    at: int


@dataclass(frozen=True)
class Handover:
    handover_id: str
    booking_id: str
    area: str
    old_escort: str
    new_escort: str
    at: int
    confirmations: tuple[Confirmation, ...] = ()

    def content(self) -> tuple:
        return (
            self.booking_id,
            self.area,
            self.old_escort,
            self.new_escort,
            self.at,
            tuple(sorted((c.person, c.booking_id, c.area, c.at)
                         for c in self.confirmations)),
        )

    def confirms(self, person: str, booking: Booking) -> bool:
        return any(
            c.person == person
            and c.booking_id == booking.booking_id
            and c.area == booking.area
            and c.at == self.at
            for c in self.confirmations
        )

    def as_dict(self) -> dict:
        return {
            "handover_id": self.handover_id,
            "booking_id": self.booking_id,
            "area": self.area,
            "old_escort": self.old_escort,
            "new_escort": self.new_escort,
            "at": self.at,
            "confirmations": [
                {
                    "person": c.person,
                    "booking_id": c.booking_id,
                    "area": c.area,
                    "at": c.at,
                }
                for c in self.confirmations
            ],
        }


# --------------------------------------------------------------------------
# Connection / schema
# --------------------------------------------------------------------------

def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('state_version', '0')"
    )


def state_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'state_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def bump_state_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'state_version'"
    )
    return state_version(conn)


# --------------------------------------------------------------------------
# Parsing / validation of raw batch fields
# --------------------------------------------------------------------------

def _as_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{field} must be an integer, got {value!r}")
    return value


def _as_str(value, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _opt_str(value, field: str) -> str:
    if value is None:
        return ""
    return _as_str(value, field)


def parse_booking(raw: dict) -> Booking:
    if not isinstance(raw, dict):
        raise InputError("booking entry must be an object")
    booking_id = _as_str(raw.get("booking_id"), "booking_id")
    start = _as_int(raw.get("start"), "start")
    end = _as_int(raw.get("end"), "end")
    quota = _as_int(raw.get("quota"), "quota")
    if start >= end:
        raise InputError(f"booking {booking_id}: start must be < end")
    if quota < 0:
        raise InputError(f"booking {booking_id}: quota must be >= 0")
    return Booking(
        booking_id=booking_id,
        visitor=_as_str(raw.get("visitor"), "visitor"),
        area=_as_str(raw.get("area"), "area"),
        start=start,
        end=end,
        quota=quota,
        home_escort=_as_str(raw.get("home_escort"), "home_escort"),
    )


def parse_presence(raw: dict) -> Presence:
    if not isinstance(raw, dict):
        raise InputError("presence entry must be an object")
    start = _as_int(raw.get("start"), "start")
    end = _as_int(raw.get("end"), "end")
    if start >= end:
        raise InputError("presence start must be < end")
    return Presence(
        person=_as_str(raw.get("person"), "person"),
        start=start,
        end=end,
        source_id=_opt_str(raw.get("source_id"), "source_id"),
    )


def parse_qualification(raw: dict) -> Qualification:
    if not isinstance(raw, dict):
        raise InputError("qualification entry must be an object")
    start = _as_int(raw.get("start"), "start")
    end = _as_int(raw.get("end"), "end")
    if start >= end:
        raise InputError("qualification start must be < end")
    return Qualification(
        person=_as_str(raw.get("person"), "person"),
        area=_as_str(raw.get("area"), "area"),
        start=start,
        end=end,
        source_id=_opt_str(raw.get("source_id"), "source_id"),
    )


def parse_revocation(raw: dict) -> Revocation:
    if not isinstance(raw, dict):
        raise InputError("revocation entry must be an object")
    return Revocation(
        revocation_id=_as_str(raw.get("revocation_id"), "revocation_id"),
        person=_as_str(raw.get("person"), "person"),
        area=_as_str(raw.get("area"), "area"),
        effective=_as_int(raw.get("effective"), "effective"),
    )


def parse_confirmation(raw: dict) -> Confirmation:
    if not isinstance(raw, dict):
        raise InputError("confirmation entry must be an object")
    return Confirmation(
        person=_as_str(raw.get("person"), "person"),
        booking_id=_as_str(raw.get("booking_id"), "booking_id"),
        area=_as_str(raw.get("area"), "area"),
        at=_as_int(raw.get("at"), "at"),
    )


def parse_handover(raw: dict) -> Handover:
    if not isinstance(raw, dict):
        raise InputError("handover entry must be an object")
    handover_id = _as_str(raw.get("handover_id"), "handover_id")
    confirmations_raw = raw.get("confirmations", [])
    if not isinstance(confirmations_raw, list):
        raise InputError("confirmations must be a list")
    confirmations = [parse_confirmation(c) for c in confirmations_raw]
    old_escort = _as_str(raw.get("old_escort"), "old_escort")
    new_escort = _as_str(raw.get("new_escort"), "new_escort")
    booking_id = _as_str(raw.get("booking_id"), "booking_id")
    area = _as_str(raw.get("area"), "area")
    at = _as_int(raw.get("at"), "at")
    # Convenience booleans merged into the confirmation list.
    for person, key in ((old_escort, "old_confirmed"),
                        (new_escort, "new_confirmed")):
        if raw.get(key) is True and not any(c.person == person for c in confirmations):
            confirmations.append(Confirmation(person, booking_id, area, at))
    return Handover(
        handover_id=handover_id,
        booking_id=booking_id,
        area=area,
        old_escort=old_escort,
        new_escort=new_escort,
        at=at,
        confirmations=tuple(confirmations),
    )


# --------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------

def _row_to_booking(row: sqlite3.Row) -> Booking:
    return Booking(
        booking_id=row["booking_id"],
        visitor=row["visitor"],
        area=row["area"],
        start=row["start"],
        end=row["end"],
        quota=row["quota"],
        home_escort=row["home_escort"],
    )


def get_booking(conn: sqlite3.Connection, booking_id: str) -> Booking | None:
    row = conn.execute(
        "SELECT booking_id, visitor, area, start, end, quota, home_escort "
        "FROM booking WHERE booking_id = ?",
        (booking_id,),
    ).fetchone()
    return _row_to_booking(row) if row else None


def list_bookings(conn: sqlite3.Connection) -> list[Booking]:
    rows = conn.execute(
        "SELECT booking_id, visitor, area, start, end, quota, home_escort "
        "FROM booking ORDER BY booking_id"
    )
    return [_row_to_booking(r) for r in rows]


def list_presence(conn: sqlite3.Connection, person: str) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT start, end FROM presence WHERE person = ? ORDER BY start, end",
        (person,),
    )
    return [(r["start"], r["end"]) for r in rows]


def qualification_rows(
    conn: sqlite3.Connection, person: str, area: str
) -> list[tuple[int, int, int]]:
    rows = conn.execute(
        "SELECT seq, start, end FROM qualification "
        "WHERE person = ? AND area = ? ORDER BY seq",
        (person, area),
    )
    return [(r["seq"], r["start"], r["end"]) for r in rows]


def revocation_rows(
    conn: sqlite3.Connection,
    person: str,
    area: str,
    allowed: set[str] | None = None,
) -> list[tuple[int, str, int]]:
    rows = conn.execute(
        "SELECT seq, revocation_id, effective FROM revocation "
        "WHERE person = ? AND area = ? ORDER BY seq",
        (person, area),
    )
    return [
        (r["seq"], r["revocation_id"], r["effective"])
        for r in rows
        if allowed is None or r["revocation_id"] in allowed
    ]


def all_revocation_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        r["revocation_id"]
        for r in conn.execute("SELECT revocation_id FROM revocation")
    }


def get_revocation(conn: sqlite3.Connection, revocation_id: str) -> Revocation | None:
    row = conn.execute(
        "SELECT revocation_id, person, area, effective FROM revocation "
        "WHERE revocation_id = ?",
        (revocation_id,),
    ).fetchone()
    if row is None:
        return None
    return Revocation(
        row["revocation_id"], row["person"], row["area"], row["effective"]
    )


def _row_to_handover(row: sqlite3.Row) -> Handover:
    return Handover(
        handover_id=row["handover_id"],
        booking_id=row["booking_id"],
        area=row["area"],
        old_escort=row["old_escort"],
        new_escort=row["new_escort"],
        at=row["at"],
        confirmations=tuple(
            parse_confirmation(c) for c in json.loads(row["confirmations"])
        ),
    )


_HANDOVER_COLUMNS = (
    "handover_id, booking_id, area, old_escort, new_escort, at, confirmations"
)


def list_handovers(conn: sqlite3.Connection, booking_id: str) -> list[Handover]:
    rows = conn.execute(
        f"SELECT {_HANDOVER_COLUMNS} FROM handover WHERE booking_id = ? "
        "ORDER BY at, seq",
        (booking_id,),
    )
    return [_row_to_handover(r) for r in rows]


def list_all_handovers(conn: sqlite3.Connection) -> list[Handover]:
    rows = conn.execute(
        f"SELECT {_HANDOVER_COLUMNS} FROM handover ORDER BY booking_id, at, seq"
    )
    return [_row_to_handover(r) for r in rows]


def get_handover(conn: sqlite3.Connection, handover_id: str) -> Handover | None:
    row = conn.execute(
        f"SELECT {_HANDOVER_COLUMNS} FROM handover WHERE handover_id = ?",
        (handover_id,),
    ).fetchone()
    return _row_to_handover(row) if row else None


def admission_count(
    conn: sqlite3.Connection,
    booking_id: str,
    exclude_request_id: str | None = None,
) -> int:
    if exclude_request_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM admission "
            "WHERE booking_id = ? AND admitted = 1",
            (booking_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM admission "
            "WHERE booking_id = ? AND admitted = 1 AND request_id <> ?",
            (booking_id, exclude_request_id),
        ).fetchone()
    return int(row["n"])


_ADMISSION_COLUMNS = (
    "request_id, booking_id, at, admitted, eligible, reason, escort, payload, "
    "snapshot, created_at"
)


def get_admission(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_ADMISSION_COLUMNS} FROM admission WHERE request_id = ?",
        (request_id,),
    ).fetchone()


def list_admissions(
    conn: sqlite3.Connection, booking_id: str | None = None
) -> list[sqlite3.Row]:
    if booking_id is None:
        return list(
            conn.execute(
                f"SELECT {_ADMISSION_COLUMNS} FROM admission "
                "ORDER BY created_at, rowid"
            )
        )
    return list(
        conn.execute(
            f"SELECT {_ADMISSION_COLUMNS} FROM admission WHERE booking_id = ? "
            "ORDER BY created_at, rowid",
            (booking_id,),
        )
    )


def list_impacts(conn: sqlite3.Connection, request_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT request_id, revocation_id, first_cause, verdict_before, "
            "verdict_after, escort_before, escort_after, recorded_at "
            "FROM admission_impact WHERE request_id = ? "
            "ORDER BY recorded_at, revocation_id",
            (request_id,),
        )
    )


def get_applied_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT batch_id, digest, applied_at FROM applied_batch WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# Interval algebra over half-open integer ranges
# --------------------------------------------------------------------------

Interval = tuple[int, int]


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of half-open intervals; touching ranges are merged."""
    ordered = sorted((s, e) for s, e in intervals if s < e)
    if not ordered:
        return []
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # touching (start == last_end) also merges
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def intersect_intervals(
    left: Iterable[Interval], right: Iterable[Interval]
) -> list[Interval]:
    a = merge_intervals(left)
    b = merge_intervals(right)
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            out.append((start, end))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return out


def interval_length(intervals: Iterable[Interval]) -> int:
    return sum(end - start for start, end in intervals)


def interval_contains(intervals: list[Interval], start: int, end: int) -> bool:
    """True iff ``[start, end)`` lies wholly inside the union ``intervals``.

    The input does not have to be pre-merged or sorted; it is normalised here.
    Merging is O(n log n) and these lists are per-person window components, so
    this stays cheap while removing a class of caller bugs.
    """
    if start >= end:
        return True
    for s, e in merge_intervals(intervals):
        if s <= start and end <= e:
            return True
        if s > start:
            return False
    return False


def first_uncovered(intervals: list[Interval], start: int, end: int) -> int | None:
    """First minute in ``[start, end)`` not covered, or None when fully covered."""
    if start >= end:
        return None
    cursor = start
    for s, e in intervals:
        if e <= cursor:
            continue
        if s > cursor:
            return cursor
        cursor = max(cursor, e)
        if cursor >= end:
            return None
    return cursor if cursor < end else None


def effective_qualification(
    conn: sqlite3.Connection,
    person: str,
    area: str,
    allowed_revocations: set[str] | None = None,
) -> list[Interval]:
    """Qualification of ``person`` in ``area`` after applying revocations.

    A revocation cancels that person/area qualification from its ``effective``
    minute onwards -- including qualification rows recorded earlier that extend
    past it.  Because every revocation removes a tail, only the earliest
    allowed ``effective`` matters.  ``allowed_revocations`` restricts the
    replay to a chosen subset; ``None`` means every stored revocation.
    """
    base = merge_intervals(
        (start, end) for _, start, end in qualification_rows(conn, person, area)
    )
    revs = revocation_rows(conn, person, area, allowed_revocations)
    if not revs:
        return base
    cut = min(effective for _, _, effective in revs)
    out: list[Interval] = []
    for start, end in base:
        if cut <= start:
            continue
        stop = min(end, cut)
        if start < stop:
            out.append((start, stop))
    return out
