"""Re-derive every candidate's theme under the current THEME_PATTERNS, then rebuild the bandit.

    python3 scripts/migrate_themes.py            # dry run: what would change
    python3 scripts/migrate_themes.py --apply    # do it
    python3 scripts/migrate_themes.py --json out.json

`infer_theme()` had two bugs. `macro` carried an unanchored `rate`, which matches
inside corporate / accurate / generated / separate / strategy, and macro was ordered
above risk/model/pv/fundamental -- so each false positive also stole a field from one
of those four. And the `fundamental` pattern never contained the word "fundamental",
so a dataset categorised Fundamental fell through to `other`. Measured on the
293-dataset USA cache: 14 of 31 Price Volume datasets read as macro, and only 2 of 22
Fundamental datasets reached `fundamental`.

Theme is half of the bandit's arm identity (`arm = f"{theme}::{template}"`) and the key
of config.TYPE_DEFAULTS, so a mislabelled field is both mis-scored and mis-simulated.
Fixing the regex alone would strand the accumulated evidence: the corrected field opens
a fresh arm at the pessimistic prior while its history stays on the old name.

So this does three things, in order:

  1. re-derives `candidates.theme` from the field caches on disk;
  2. rebuilds `arms` and `bandit_taught.arm` from primary evidence
     (Store.rebuild_arms_from_evidence -- derive, never rename);
  3. stamps kv `theme_rev` so a second run is a no-op.

A field id that appears in no cache KEEPS ITS CURRENT THEME. Re-running infer_theme on
a bare id would discard the description and dataset name it reads, dumping most such
rows into `other` -- a second wrong migration on top of the first.

Stop autoloop first, and back the database up online (`cp` would split the WAL):

    sqlite3 data/machine.db ".backup data/machine.db.bak-$(date +%F)"

Afterwards run `python3 -m alphamachine audit` and `python3 -m alphamachine report`,
then re-run this script -- it should report zero changes.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine import audit
from alphamachine.catalog import infer_theme
from alphamachine.store import Store

THEME_REV = "2026-09-24-anchored-macro-plus-fundamental"


def load_field_records(cache_dir: str) -> tuple[dict[str, dict], dict[str, int]]:
    """field id -> the fullest record we hold for it, plus where they came from.

    Both shapes matter. `fields_{REGION}_{UNIV}_d{D}.json` is written only after a
    whole catalog run finishes, while `parts_*/` accumulates per dataset as it goes --
    so an interrupted run leaves parts and no combined file, and reading only the
    combined file would silently see nothing.
    """
    records: dict[str, dict] = {}
    sources: dict[str, int] = {}
    paths = sorted(glob.glob(os.path.join(cache_dir, "fields_*.json")))
    paths += sorted(glob.glob(os.path.join(cache_dir, "parts_*", "*.json")))
    for p in paths:
        try:
            with open(p) as fh:
                got = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"  ! unreadable, skipped: {p} ({e})", file=sys.stderr)
            continue
        if not isinstance(got, list):
            continue
        n = 0
        for f in got:
            fid = f.get("id")
            if not fid:
                continue
            # Prefer the record carrying the most text: infer_theme reads the
            # description and the dataset name, and a thinner duplicate from
            # another region would classify worse.
            prev = records.get(fid)
            if prev is None or _text_len(f) > _text_len(prev):
                records[fid] = f
            n += 1
        sources[p] = n
    return records, sources


def _text_len(f: dict) -> int:
    return len(str(f.get("description") or "")) + \
        len(str((f.get("dataset") or {}).get("name") or ""))


def plan(store: Store, records: dict[str, dict]) -> dict:
    conn = store.conn()
    rows = conn.execute(
        "SELECT id, field, theme, template, stage, parent_id FROM candidates").fetchall()
    moves: collections.Counter = collections.Counter()
    per_field: dict[str, tuple[str, str]] = {}
    unresolved: collections.Counter = collections.Counter()
    changed_ids: list[tuple[str, str]] = []
    for r in rows:
        fid, old = r["field"], r["theme"]
        if not fid:
            continue
        rec = records.get(fid)
        if rec is None:
            unresolved[fid] += 1
            continue
        new = infer_theme(rec)
        per_field[fid] = (old, new)
        if new != old:
            moves[(old, new)] += 1
            changed_ids.append((r["id"], new))
    arms_before = {r["arm"]: dict(r) for r in conn.execute("SELECT * FROM arms")}
    return {"candidate_rows": len(rows), "moves": moves, "per_field": per_field,
            "unresolved": unresolved, "changed_ids": changed_ids,
            "arms_before": arms_before}


def report(p: dict, sources: dict[str, int], records: dict[str, dict]) -> None:
    print(f"\ncaches: {len(records)} distinct field id(s) from {len(sources)} file(s)")
    for path, n in list(sources.items())[:8]:
        print(f"    {n:>7}  {path}")
    if len(sources) > 8:
        print(f"    ... and {len(sources) - 8} more")

    field_moves = collections.Counter(
        (o, n) for o, n in p["per_field"].values() if o != n)
    print(f"\nfields: {len(p['per_field'])} resolved, "
          f"{sum(1 for o, n in p['per_field'].values() if o != n)} change theme")
    for (o, n), c in field_moves.most_common():
        print(f"    {o or '(null)':<16} -> {n:<16} {c:>6} field(s)")

    print(f"\ncandidates: {p['candidate_rows']} row(s), "
          f"{sum(p['moves'].values())} change theme, "
          f"{sum(p['unresolved'].values())} unresolved (kept as-is)")
    for (o, n), c in p["moves"].most_common(30):
        print(f"    {o or '(null)':<16} -> {n:<16} {c:>7} row(s)")

    if p["unresolved"]:
        print(f"\nunresolved field ids: {len(p['unresolved'])} distinct")
        for fid, c in p["unresolved"].most_common(20):
            print(f"    {fid:<40} {c:>6} row(s)")

    # Which arms the evidence is riding on, and what becomes of each.
    moved_themes = {o for o, n in p["per_field"].values() if o != n}
    print(f"\narms: {len(p['arms_before'])} row(s) today; "
          f"{sum(1 for a in p['arms_before'] if a.split('::')[0] in moved_themes)} "
          f"sit on a theme that some field leaves")
    tot = collections.Counter()
    for arm, row in p["arms_before"].items():
        if arm.split("::")[0] not in moved_themes:
            continue
        for k in ("trials", "scan_trials", "gate_pass", "corr_trials",
                  "corr_pass", "deep_trials", "submitted"):
            tot[k] += row.get(k) or 0
    if tot:
        print("    evidence on arms whose theme moves: "
              + ", ".join(f"{k}={v}" for k, v in tot.items()))
    print("\n    (counters are REBUILT from candidates/sims/gate_history/deep_checks,"
          "\n     not renamed -- merges and splits both reduce to a GROUP BY)")


def apply(store: Store, p: dict) -> dict:
    conn = store.conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cid, new in p["changed_ids"]:
            conn.execute("UPDATE candidates SET theme=? WHERE id=?", (new, cid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    stats = store.rebuild_arms_from_evidence()
    # A child inherits its parent's theme verbatim (refine.py), so a per-field
    # update keeps a lineage coherent by construction. Assert it anyway: if it
    # ever stops holding, the bandit is being taught under two names for one idea.
    bad = conn.execute(
        "SELECT COUNT(*) FROM candidates c JOIN candidates pa ON pa.id = c.parent_id"
        " WHERE c.theme IS NOT pa.theme").fetchone()[0]
    store.put("theme_rev", THEME_REV)
    store.put(f"migrate_themes_{THEME_REV}", {
        "at": time.time(), "rows_changed": len(p["changed_ids"]), **stats})
    stats["lineage_theme_mismatches"] = bad
    stats["rows_changed"] = len(p["changed_ids"])
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser("migrate_themes")
    ap.add_argument("--db", default="data/machine.db")
    ap.add_argument("--cache-dir", default="data/cache")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if this theme revision is already stamped")
    ap.add_argument("--json", help="write the full plan to this path")
    a = ap.parse_args(argv)

    records, sources = load_field_records(a.cache_dir)
    if not records:
        print(f"\nno field records under {a.cache_dir} -- nothing can be resolved.\n"
              f"Run this where the catalog caches live (the machine that runs the "
              f"pipeline), not on a fresh checkout.\n", file=sys.stderr)
        return 2

    if not a.apply:
        # Read-only path takes a read-only connection, so it is safe mid-write.
        conn = audit.open_ro(a.db)
        try:
            store = _RoStore(conn)
            p = plan(store, records)
        finally:
            conn.close()
        report(p, sources, records)
        if a.json:
            with open(a.json, "w") as fh:
                json.dump({"moves": {f"{o}->{n}": c for (o, n), c in p["moves"].items()},
                           "unresolved": dict(p["unresolved"]),
                           "rows_changed": len(p["changed_ids"])}, fh, indent=1)
        print(f"\nDRY RUN -- nothing written. Re-run with --apply to commit.\n")
        return 0

    store = Store(a.db)
    if store.get("theme_rev") == THEME_REV and not a.force:
        print(f"\ntheme_rev is already {THEME_REV} -- nothing to do (--force to re-run).\n")
        return 0
    p = plan(store, records)
    report(p, sources, records)
    stats = apply(store, p)
    print("\napplied: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if stats.get("lineage_theme_mismatches"):
        print("  ! a child's theme differs from its parent's -- investigate before "
              "trusting the bandit table", file=sys.stderr)
    print("\nNow run: python3 -m alphamachine audit\n")
    return 0


class _RoStore:
    """Just enough Store surface for plan() to run against a read-only connection."""

    def __init__(self, conn):
        self._c = conn

    def conn(self):
        return self._c


if __name__ == "__main__":
    raise SystemExit(main())
