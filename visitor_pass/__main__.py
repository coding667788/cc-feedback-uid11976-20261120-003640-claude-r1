"""Command-line entry point: load, coverage, admit, verify.

Usage::

    python -m visitor_pass --db park.db load batch.json
    python -m visitor_pass --db park.db coverage --booking B1
    python -m visitor_pass --db park.db coverage --all
    python -m visitor_pass --db park.db admit --booking B1 --at 10 --request R1
    python -m visitor_pass --db park.db verify

Every command prints one JSON object on stdout and exits 0 on success, 2 on a
rejected batch or input error, 3 on an unexpected internal error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import service, store


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
    sys.stdout.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m visitor_pass",
        description="Offline visitor-pass adjudicator (SQLite, stdlib only).",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="apply one JSON batch atomically")
    load.add_argument("batch", help="path to a batch JSON file, or - for stdin")

    cov = sub.add_parser("coverage", help="show maximal passable runs")
    cov.add_argument("--booking", help="booking id")
    cov.add_argument("--all", action="store_true", help="every booking")
    cov.add_argument("--from", dest="from_minute", type=int, default=None)
    cov.add_argument("--to", dest="to_minute", type=int, default=None)

    adm = sub.add_parser("admit", help="adjudicate one admission request")
    adm.add_argument("--booking", required=True)
    adm.add_argument("--at", type=int, required=True)
    adm.add_argument("--request", required=True, help="idempotency key")

    sub.add_parser("verify", help="recompute every invariant, mutate nothing")
    return parser


def _read_batch(source: str) -> dict:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise store.InputError(f"batch is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise store.InputError("batch must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        conn = store.connect(args.db)
        try:
            if args.command == "load":
                _emit(service.load(conn, _read_batch(args.batch)))
                return 0
            if args.command == "coverage":
                if args.all:
                    _emit(service.coverage_all(conn))
                    return 0
                if not args.booking:
                    raise store.InputError("coverage needs --booking or --all")
                _emit(
                    service.coverage(
                        conn, args.booking, args.from_minute, args.to_minute
                    )
                )
                return 0
            if args.command == "admit":
                _emit(service.admit(conn, args.booking, args.at, args.request))
                return 0
            if args.command == "verify":
                result = service.verify(conn)
                _emit(result)
                return 0 if result["ok"] else 1
        finally:
            conn.close()
    except store.ConflictError as exc:
        _emit(exc.as_dict())
        return 2
    except store.InputError as exc:
        _emit({"ok": False, "error": "input", "detail": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001 - last-resort CLI guard
        _emit({"ok": False, "error": "internal", "detail": repr(exc)})
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
