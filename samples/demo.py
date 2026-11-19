"""End-to-end demo: run the CLI over samples/ against a throwaway database.

Run from the repository root::

    python samples/demo.py

It loads the sample batches, issues admissions, applies a revocation, replays an
old receipt, and finally runs ``verify``.  Each step prints a short summary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def run(db, *args):
    completed = subprocess.run(
        [sys.executable, "-m", "visitor_pass", "--db", db, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": completed.stdout, "stderr": completed.stderr}
    return completed.returncode, payload


def main():
    tmp = tempfile.mkdtemp(prefix="visitor-pass-demo-")
    db = os.path.join(tmp, "demo.db")

    print("== load base batch ==")
    code, out = run(db, "load", os.path.join(SAMPLES, "01-base.json"))
    print("exit", code, "counts", out.get("counts"))

    print("\n== coverage ==")
    code, out = run(db, "coverage", "--booking", "B-DEMO")
    for segment in out["passable"]:
        print(
            f"  [{segment['start']:>3},{segment['end']:>3}) "
            f"{segment['escort']:<14} entered_by={segment['entered_by']}"
        )

    print("\n== admissions (quota = 3) ==")
    for at, request in ((10, "REQ-1"), (60, "REQ-2"), (100, "REQ-3"),
                        (110, "REQ-4"), (10, "REQ-1")):
        code, out = run(db, "admit", "--booking", "B-DEMO", "--at", str(at),
                        "--request", request)
        receipt = out["receipt"]
        replay = " (idempotent replay)" if out["idempotent"] else ""
        print(
            f"  {request:<6} at={at:<4} admitted={receipt['admitted']!s:<5} "
            f"reason={receipt['reason']:<22} escort={receipt['escort']}{replay}"
        )

    print("\n== revocation: Escort-Zhao lost A1 qualification from t=100 ==")
    code, out = run(db, "load", os.path.join(SAMPLES, "03-revocation.json"))
    print("exit", code, "counts", out.get("counts"))
    code, out = run(db, "coverage", "--booking", "B-DEMO")
    for segment in out["passable"]:
        print(
            f"  [{segment['start']:>3},{segment['end']:>3}) {segment['escort']}"
        )
    print("  rejected handovers:",
          [h["handover_id"] for h in out["handovers"] if not h["applied"]])

    print("\n== replay REQ-3: first receipt preserved, audit appended ==")
    code, out = run(db, "admit", "--booking", "B-DEMO", "--at", "100",
                    "--request", "REQ-3")
    receipt = out["receipt"]
    print("  still admitted:", receipt["admitted"], "escort:", receipt["escort"])
    for impact in receipt["impacted_by"]:
        print("  impact:", impact["verdict_before"], "->",
              impact["verdict_after"], "|", impact["first_cause"])

    print("\n== a batch that must be rejected whole ==")
    code, out = run(db, "load", os.path.join(SAMPLES, "04-rejected-duplicate-minute.json"))
    print("  exit", code, "error", out.get("error"), "-", out.get("detail"))

    print("\n== one-minute-short overlap ==")
    code, out = run(db, "load", os.path.join(SAMPLES, "05-one-minute-short.json"))
    code, out = run(db, "coverage", "--booking", "B-SHORT")
    print("  handover:", out["handovers"][0]["handover_id"],
          "applied:", out["handovers"][0]["applied"],
          "reason:", out["handovers"][0]["reason"])

    print("\n== verify ==")
    code, out = run(db, "verify")
    print("  exit", code, "ok", out["ok"], out["invariants"])
    print("\n  drift is expected for REQ-3:")
    for entry in out["receipt_drift"]:
        print("   ", entry)


if __name__ == "__main__":
    main()
