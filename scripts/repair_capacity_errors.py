"""Undo the damage of simulations recorded as ERROR for reasons that were not the candidate.

    python3 scripts/repair_capacity_errors.py            # dry run: what would change
    python3 scripts/repair_capacity_errors.py --apply    # do it

Until 2026-09-22 the Executor stored ANY failure as the candidate's permanent result, and
`scan` taught each one to its bandit arm as a failed trial. The ones this repairs are
infrastructure failures -- see client.TRANSIENT_ERROR_MARKERS: slots full
(CONCURRENT_SIMULATION_LIMIT_EXCEEDED, 21,074 rows), a dead session (authenticate()
rate-limited or demanding a captcha, 3,914), network blips, the daily simulation cap, a
poll deadline. Those candidates were never simulated (or their result was never fetched).
This returns them to the queue:

  1. copies every matching `sims` row into `sims_capacity_errors_<date>` (restorable);
  2. subtracts, per arm, the scan-origin rows (parent_id IS NULL) from
     `arms.scan_trials` (floored at scan_pass) and `arms.trials` -- refine variants
     never taught the bandit per row, so nothing is subtracted for them;
  3. deletes those `sims` rows, so the candidates are pending again;
  4. records the counts in kv `repair_<backup table name>`.

All in one transaction. Matches ONLY those markers; a genuine error ("Invalid data field",
"unknown variable") stays. Running it again finds nothing. Stop autoloop first. Then re-run the work:
`scan --pending --budget N` and `refine --redo-incomplete --top N`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.client import TRANSIENT_ERROR_MARKERS


def _like(marker: str) -> str:
    esc = marker.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    return f"s.error LIKE '%{esc}%' ESCAPE '\\'"


# One list, shared with the Executor: what it now refuses to store is exactly what this removes.
WHERE = "s.status='ERROR' AND (" + " OR ".join(_like(m) for m in TRANSIENT_ERROR_MARKERS) + ")"


def plan(conn: sqlite3.Connection) -> dict:
    by_stage = dict(conn.execute(
        f"SELECT c.stage, COUNT(*) FROM sims s JOIN candidates c ON c.id=s.candidate_id "
        f"WHERE {WHERE} GROUP BY c.stage").fetchall())
    per_arm = dict(conn.execute(
        f"SELECT c.theme || '::' || c.template, COUNT(*) FROM sims s "
        f"JOIN candidates c ON c.id=s.candidate_id "
        f"WHERE {WHERE} AND c.parent_id IS NULL GROUP BY 1").fetchall())
    total = conn.execute(f"SELECT COUNT(*) FROM sims s WHERE {WHERE}").fetchone()[0]
    return {"total": total, "by_stage": by_stage, "per_arm": per_arm}


def apply(conn: sqlite3.Connection, backup_table: str) -> dict:
    p = plan(conn)
    if not p["total"]:
        return {**p, "applied": False}
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (backup_table,)).fetchone()
        if exists:
            conn.execute(f"INSERT INTO {backup_table} SELECT s.* FROM sims s WHERE {WHERE}")
        else:
            conn.execute(f"CREATE TABLE {backup_table} AS SELECT s.* FROM sims s WHERE {WHERE}")
        for arm, n in p["per_arm"].items():
            conn.execute("UPDATE arms SET scan_trials=MAX(scan_trials-?, scan_pass), "
                         "trials=MAX(trials-?, 0) WHERE arm=?", (n, n, arm))
        conn.execute(f"DELETE FROM sims WHERE candidate_id IN "
                     f"(SELECT s.candidate_id FROM sims s WHERE {WHERE})")
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)",
                     (f"repair_{backup_table}", json.dumps({"at": time.time(), "backup_table": backup_table,
                                  "total": p["total"], "by_stage": p["by_stage"],
                                  "per_arm": p["per_arm"]})))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {**p, "applied": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="data/machine.db")
    ap.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    ap.add_argument("--backup-table", default=f"sims_capacity_errors_{time.strftime('%Y%m%d')}")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=60.0, isolation_level=None)
    r = apply(conn, args.backup_table) if args.apply else plan(conn)
    print(f"matching rows: {r['total']:,}   by stage: {r['by_stage']}")
    print(f"arms to correct: {len(r['per_arm'])}   scan trials to subtract: "
          f"{sum(r['per_arm'].values()):,}")
    if not args.apply:
        print("dry run -- nothing changed. Re-run with --apply.")
    elif r.get("applied"):
        print(f"applied. rows saved in {args.backup_table}; candidates are pending again.")
    else:
        print("nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
