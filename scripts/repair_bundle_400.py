"""Return to the queue the valid candidates a bundle-level 400 wrote off as ERROR.

    python3 scripts/repair_bundle_400.py           # dry run
    python3 scripts/repair_bundle_400.py --apply

BRAIN 400s a whole multi-sim bundle when any one member is malformed, answering
with a per-child array in which valid members are `{}`. Until BundleRejected, the
Executor stored that one message against EVERY member. Seen 2026-09-26: refine
asked for CHN/TOP3000 and ASI/ILLIQUID_MINVOL1M (neither exists), and its valid
bundle-mates were marked ERROR for good.

A row is collateral only when BOTH hold, otherwise it is left alone:
  * every reason BRAIN gave is a settings value this script can read
    (universe / neutralization "X is not available") -- an expression error in
    the array could belong to anyone, so such rows are not touched;
  * the candidate's OWN universe and neutralization are not among the rejected.

--apply copies the rows to a backup table, subtracts scan-origin rows (parent_id
IS NULL) from their arm's scan_trials exactly as repair_capacity_errors.py does,
deletes the rows (the candidates are pending again) and records the counts in kv.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REJECT = re.compile(r"(Universe|Neutralization) (\S+) is not available")
_KEYS = re.compile(r'"([A-Za-z_]+)":\s*[\[{]')
_READABLE = {"settings", "universe", "neutralization"}


def is_collateral(error: str, settings: dict) -> bool:
    body = error.split(":", 1)[1] if ":" in error else error
    if not set(_KEYS.findall(body)) <= _READABLE:
        return False
    rejected = _REJECT.findall(body)
    if not rejected:
        return False
    bad_u = {v.rstrip(".") for k, v in rejected if k == "Universe"}
    bad_n = {v.rstrip(".") for k, v in rejected if k == "Neutralization"}
    return settings.get("universe") not in bad_u and settings.get("neutralization") not in bad_n


def plan(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT s.candidate_id, s.error, c.settings, c.parent_id, c.theme, c.template "
        "FROM sims s JOIN candidates c ON c.id=s.candidate_id "
        "WHERE s.status='ERROR' AND s.error LIKE 'simulate [400]%'").fetchall()
    ids, per_arm = [], Counter()
    for cid, err, st, parent, theme, tmpl in rows:
        if is_collateral(err, json.loads(st)):
            ids.append(cid)
            if parent is None:
                per_arm[f"{theme}::{tmpl}"] += 1
    return {"examined": len(rows), "ids": ids, "per_arm": dict(per_arm)}


def apply(conn: sqlite3.Connection, backup_table: str) -> dict:
    p = plan(conn)
    if not p["ids"]:
        return {**p, "applied": False}
    marks = ",".join("?" * len(p["ids"]))
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (backup_table,)).fetchone()
        verb = f"INSERT INTO {backup_table}" if exists else f"CREATE TABLE {backup_table} AS"
        conn.execute(f"{verb} SELECT * FROM sims WHERE candidate_id IN ({marks})", p["ids"])
        for arm, n in p["per_arm"].items():
            conn.execute("UPDATE arms SET scan_trials=MAX(scan_trials-?, scan_pass), "
                         "trials=MAX(trials-?, 0) WHERE arm=?", (n, n, arm))
        conn.execute(f"DELETE FROM sims WHERE candidate_id IN ({marks})", p["ids"])
        conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)",
                     (f"repair_{backup_table}", json.dumps(
                         {"at": time.time(), "backup_table": backup_table,
                          "rows": len(p["ids"]), "per_arm": p["per_arm"]})))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {**p, "applied": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="data/machine.db")
    ap.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    ap.add_argument("--backup-table", default=f"sims_bundle400_{time.strftime('%Y%m%d')}")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=60.0, isolation_level=None)
    r = apply(conn, args.backup_table) if args.apply else plan(conn)
    print(f"bundle-level 400 rows examined: {r['examined']}   collateral: {len(r['ids'])}")
    print(f"scan-origin rows to take back from arms: {sum(r['per_arm'].values())} {r['per_arm']}")
    if not args.apply:
        print("dry run -- nothing changed. Re-run with --apply.")
    elif r.get("applied"):
        print(f"applied. rows saved in {args.backup_table}; candidates are pending again.")
    else:
        print("nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
