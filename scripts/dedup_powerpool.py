"""Collapse a Power Pool screening report into a set of genuinely DIFFERENT ideas.

A full `check_powerpool.py --since ...` scan returns tens of thousands of
eligible alphas, and almost all of them are the same few ideas written many
times: the same field under a different window, decay, neutralization or
truncation. Power Pool correlation must be < 0.5 against the pool AND against
the Power Pool alphas already submitted, so submitting twins wastes the daily
slots -- the second twin fails the check the first one just made stricter.

    python3 scripts/dedup_powerpool.py pp.json -o pp_dedup.json
    python3 scripts/dedup_powerpool.py pp.json --max-per-field 2 --max-per-dataset 3

Input is `check_powerpool.py --json` output. Output is the SAME record format,
so it feeds `check_powerpool.py --from-file ... --deep` and `tag_powerpool.py`
directly. No network, no quota.

Before any of that, DECAYED alphas are dropped (`--allow-decayed` to keep them):
a record whose recency evidence (`recency.ok`, from BRAIN's own LOW_2Y_SHARPE or
IS_LADDER_SHARPE value against its own limit) is False. Without this the best
representative of an idea is whichever variant had the best WHOLE-PERIOD Sharpe,
which is exactly the one most likely to have earned it years ago: on the real
2026-09-20 pull the three highest-Sharpe survivors had a trailing Sharpe of 0.00.
Unknown recency (BRAIN sent neither check) is kept, not dropped.

Four stages, each strictly narrower than the last:

  1. SIGNATURE   (region, delay, operator set, field set) -> keep the best
                 Sharpe. Window / decay / neutralization variants of one idea
                 are not different ideas.
  2. FIELD CAP   an underlying field may appear in at most --max-per-field kept
                 alphas (default 1). Two alphas on the same field are the most
                 likely twins even when their operators differ. Ubiquitous
                 plumbing fields (`returns`, `cap`, `assets`, ... -- see
                 COMMON_FIELDS) are exempt: they are denominators, not ideas.
  3. DATASET CAP optional (--max-per-dataset). Datasets are the real diversity
                 unit -- fields inside one dataset share a source and a latent
                 factor. Needs the field catalog cache (`catalog` command);
                 fields it does not know are never capped by this stage.

  4. SHAPE CAP  at most --max-per-shape kept alphas per (region, delay, operator
                 set) -- the transform SHAPE. Stages 1-3 compare fields; this one
                 compares constructions. It exists because one template applied
                 to many fields is not many ideas: on the real 2026-09-20 pull,
                 89 of the 157 alphas that survived stages 1-3 had the identical
                 operator set {group_rank, ts_regression} -- the residual of
                 `returns` on some field, which is mostly `returns` again, i.e.
                 one reversal factor written 89 times. Transform shape drives
                 correlation far more than field identity does.

Stages 2-4 are greedy in descending Sharpe, so the best representative of
each idea is the one that survives.

`--against ACTIVE.json` (from `check_powerpool.py --status ACTIVE --json`) makes
the caps count what is ALREADY submitted: a candidate on a field or dataset an
active alpha holds is dropped, and an exact signature match is dropped outright.
Without it, dedup only compares candidates with each other, and a candidate can
still be a twin of something in the book -- which is what fails
POWER_POOL_CORRELATION at the deep stage, after the API budget is spent.

The kept list is then ORDERED to interleave pyramids (region x delay x dataset
category), because `--deep` is the expensive step and is normally run on a
`--top N` slice: an order by Sharpe alone would spend the whole slice on
whichever pyramid the pipeline happened to generate most candidates for. The
Dynamic Pyramid Multiplier rewards under-used pyramids and Genius counts
distinct completed ones, so an order that spreads them is the useful one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.catalog import DENOMINATORS
from alphamachine.templates import PV_TOKENS

# Fields that show up as plumbing in thousands of unrelated alphas -- a price
# series, or the size denominator of a yield. They are not "the idea", so they
# never count toward the field / dataset cap: without this, the single best
# alpha that happens to divide by `cap` would lock out every other cap-scaled
# alpha, each of which has a different numerator and is a different idea.
COMMON_FIELDS = frozenset(PV_TOKENS) | frozenset(DENOMINATORS)

DEFAULT_CATALOG = "data/cache/fields_USA_TOP3000_d1.json"
NO_PYRAMID = "(unknown)"


def signature(r: dict) -> tuple:
    return (r.get("region"), r.get("delay"),
            tuple(sorted(r.get("operators") or [])),
            tuple(sorted(r.get("fields") or [])))


def load_field_datasets(path: str) -> dict[str, str]:
    """field id -> dataset id, from the catalog cache. Empty if unavailable."""
    try:
        with open(path) as fh:
            fields = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {f["id"]: (f.get("dataset") or {}).get("id")
            for f in fields if f.get("id") and (f.get("dataset") or {}).get("id")}


def _sharpe(r: dict) -> float:
    return r.get("sharpe") or 0.0


def pyramid_key(r: dict) -> str:
    names = sorted(r.get("pyramids") or [])
    return "+".join(names) if names else NO_PYRAMID


def by_signature(pool: list[dict]) -> list[dict]:
    """Best Sharpe per signature; records how many variants each one absorbed."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in pool:
        groups[signature(r)].append(r)
    out = []
    for members in groups.values():
        best = max(members, key=_sharpe)
        out.append({**best, "dedup_group_size": len(members)})
    return sorted(out, key=lambda r: -_sharpe(r))


def _field_keys(r: dict) -> list[tuple]:
    return [(r.get("region"), f) for f in r.get("fields") or []
            if f not in COMMON_FIELDS]


def cap_fields(pool: list[dict], max_per_field: int,
               existing: list[dict] | None = None) -> list[dict]:
    """Greedy in Sharpe order; a field may back at most `max_per_field` alphas.

    Keyed on (region, field): the same field name in another region is a
    different data series. COMMON_FIELDS are not counted. `existing` (alphas
    already submitted) start the count, so they use up their fields' quota.
    """
    used: Counter = Counter()
    for r in existing or []:
        used.update(_field_keys(r))
    kept = []
    for r in sorted(pool, key=lambda r: -_sharpe(r)):
        keys = _field_keys(r)
        if any(used[k] >= max_per_field for k in keys):
            continue
        used.update(keys)
        kept.append(r)
    return kept


def _datasets(r: dict, field_datasets: dict[str, str]) -> set[str]:
    return {field_datasets[f] for f in r.get("fields") or []
            if f in field_datasets and f not in COMMON_FIELDS}


def cap_datasets(pool: list[dict], field_datasets: dict[str, str],
                 max_per_dataset: int,
                 existing: list[dict] | None = None) -> list[dict]:
    """Greedy in Sharpe order; a dataset may back at most `max_per_dataset`."""
    used: Counter = Counter()
    for r in existing or []:
        used.update((r.get("region"), d) for d in _datasets(r, field_datasets))
    kept = []
    for r in sorted(pool, key=lambda r: -_sharpe(r)):
        ds = _datasets(r, field_datasets)
        keys = [(r.get("region"), d) for d in ds]
        if any(used[k] >= max_per_dataset for k in keys):
            continue
        used.update(keys)
        kept.append({**r, "datasets": sorted(ds)})
    return kept


def _shape(r: dict) -> tuple:
    return (r.get("region"), r.get("delay"), tuple(sorted(r.get("operators") or [])))


def cap_shapes(pool: list[dict], max_per_shape: int,
               existing: list[dict] | None = None) -> list[dict]:
    """Greedy in Sharpe order; one operator set backs at most `max_per_shape`."""
    used: Counter = Counter(_shape(r) for r in existing or [])
    kept = []
    for r in sorted(pool, key=lambda r: -_sharpe(r)):
        k = _shape(r)
        if used[k] >= max_per_shape:
            continue
        used[k] += 1
        kept.append(r)
    return kept


def interleave_pyramids(pool: list[dict]) -> list[dict]:
    """Order so any prefix is spread across pyramids, best Sharpe first within
    each. Repeatedly takes from the pyramid with the fewest picks so far."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(pool, key=lambda r: -_sharpe(r)):
        buckets[pyramid_key(r)].append(r)
    picked: Counter = Counter()
    out = []
    while any(buckets.values()):
        live = [k for k, v in buckets.items() if v]
        k = min(live, key=lambda k: (picked[k], -_sharpe(buckets[k][0])))
        out.append(buckets[k].pop(0))
        picked[k] += 1
    return out


def is_decayed(r: dict) -> bool:
    return (r.get("recency") or {}).get("ok") is False


def recency_from_db(records: list[dict], db_path: str) -> int:
    """Fill `recency` on old reports (made before it existed) from the local db.

    The db keeps BRAIN's 2Y / IS-ladder values from each simulation. The 2Y limit
    is not stored, so 1.58 is assumed for it. Returns how many were filled.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    n = 0
    for r in records:
        if r.get("recency"):
            continue
        row = conn.execute("SELECT two_year_sharpe, is_ladder_sharpe, is_ladder_limit "
                           "FROM sims WHERE alpha_id=?", (r["alpha_id"],)).fetchone()
        if not row:
            continue
        y2, lad, lim = row
        if y2 is not None:
            r["recency"] = {"kind": "2Y", "value": y2, "limit": 1.58, "ok": y2 >= 1.58}
        elif lad is not None and lim is not None:
            r["recency"] = {"kind": "LADDER", "value": lad, "limit": lim, "ok": lad >= lim}
        else:
            continue
        n += 1
    conn.close()
    return n


def dedup(results: list[dict], *, max_per_field: int = 1,
          max_per_dataset: int | None = None,
          field_datasets: dict[str, str] | None = None,
          existing: list[dict] | None = None,
          max_per_shape: int | None = None,
          drop_decayed: bool = False,
          interleave: bool = True) -> tuple[list[dict], dict]:
    """Pure function: (records) -> (kept records, per-stage counts).

    `existing` are alphas already submitted; see the module docstring.
    """
    eligible = [r for r in results if r.get("eligible")]
    stats = {"input": len(results), "eligible": len(eligible)}
    if drop_decayed:
        eligible = [r for r in eligible if not is_decayed(r)]
        stats["after_recency"] = len(eligible)

    pool = by_signature(eligible)
    stats["after_signature"] = len(pool)

    if existing:
        taken = {signature(r) for r in existing}
        pool = [r for r in pool if signature(r) not in taken]
        stats["after_against_twins"] = len(pool)

    pool = cap_fields(pool, max_per_field, existing)
    stats["after_field_cap"] = len(pool)

    if max_per_dataset is not None and field_datasets:
        pool = cap_datasets(pool, field_datasets, max_per_dataset, existing)
        stats["after_dataset_cap"] = len(pool)

    if max_per_shape is not None:
        pool = cap_shapes(pool, max_per_shape, existing)
        stats["after_shape_cap"] = len(pool)

    pool = interleave_pyramids(pool) if interleave else sorted(
        pool, key=lambda r: -_sharpe(r))
    return pool, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("report", help="check_powerpool.py --json output")
    ap.add_argument("-o", "--out", default=None,
                    help="write the kept records here (same format as the input)")
    ap.add_argument("--max-per-field", type=int, default=1,
                    help="max kept alphas per underlying field (default 1)")
    ap.add_argument("--max-per-dataset", type=int, default=None,
                    help="max kept alphas per dataset (default: no dataset cap)")
    ap.add_argument("--allow-decayed", action="store_true",
                    help="keep alphas whose recent Sharpe is below BRAIN's limit")
    ap.add_argument("--recency-db", default=None,
                    help="local machine.db, to fill recency on a report made "
                         "before that field existed")
    ap.add_argument("--max-per-shape", type=int, default=3,
                    help="max kept alphas per operator set, i.e. per transform "
                         "shape (default 3; a very large number disables it)")
    ap.add_argument("--against", default=None,
                    help="already-submitted alphas (check_powerpool.py --status "
                         "ACTIVE --json): they use up their fields' and "
                         "datasets' quota, and exact twins are dropped")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG,
                    help="field catalog cache, for the dataset cap")
    ap.add_argument("--no-interleave", action="store_true",
                    help="order by Sharpe only, instead of spreading pyramids")
    ap.add_argument("--top", type=int, default=15,
                    help="rows of the kept list to print (default 15)")
    args = ap.parse_args()

    with open(args.report) as fh:
        results = json.load(fh)

    field_datasets: dict[str, str] = {}
    if args.max_per_dataset is not None:
        field_datasets = load_field_datasets(args.catalog)
        if not field_datasets:
            print(f"WARNING: no usable catalog at {args.catalog}; the dataset "
                  f"cap is skipped. Run `python3 -m alphamachine catalog`.",
                  file=sys.stderr)

    if args.recency_db:
        filled = recency_from_db(results, args.recency_db)
        print(f"filled recency for {filled:,} record(s) from {args.recency_db}",
              file=sys.stderr)
    unknown = sum(1 for r in results if r.get("eligible") and not r.get("recency"))
    if unknown and not args.allow_decayed:
        print(f"note: {unknown:,} eligible record(s) carry no recency data and are "
              f"kept (re-pull, or pass --recency-db).", file=sys.stderr)

    existing = None
    if args.against:
        with open(args.against) as fh:
            existing = json.load(fh)

    kept, st = dedup(results, max_per_field=args.max_per_field,
                     max_per_dataset=args.max_per_dataset,
                     field_datasets=field_datasets, existing=existing,
                     max_per_shape=args.max_per_shape,
                     drop_decayed=not args.allow_decayed,
                     interleave=not args.no_interleave)

    print(f"records in report            {st['input']:>8,}")
    print(f"  eligible                   {st['eligible']:>8,}")
    if "after_recency" in st:
        print(f"  minus decayed (recent < limit) {st['after_recency']:>4,}")
    print(f"  distinct ideas (signature) {st['after_signature']:>8,}")
    if "after_against_twins" in st:
        print(f"  minus twins of {len(existing)} submitted   "
              f"{st['after_against_twins']:>8,}")
    print(f"  after field cap ({args.max_per_field}/field)   "
          f"{st['after_field_cap']:>8,}")
    if "after_dataset_cap" in st:
        print(f"  after dataset cap ({args.max_per_dataset}/ds)  "
              f"{st['after_dataset_cap']:>8,}")
    if "after_shape_cap" in st:
        print(f"  after shape cap ({args.max_per_shape}/opset)   "
              f"{st['after_shape_cap']:>8,}")
    print(f"  KEPT                       {len(kept):>8,}")

    verified = sum(1 for r in kept if r.get("verified"))
    print(f"\n  of which server-verified (no PENDING check): {verified}")

    pyr = Counter(pyramid_key(r) for r in kept)
    print(f"\npyramids across the kept list ({len(pyr)} distinct):")
    for name, n in pyr.most_common(12):
        print(f"  {n:>5}  {name}")

    print(f"\nfirst {min(args.top, len(kept))} in submission-scan order:")
    for r in kept[:args.top]:
        print(f"  {r['alpha_id']}  sharpe={_sharpe(r):.2f}  "
              f"ops={r['n_operators']}  fields={','.join(r['fields'])}  "
              f"variants={r.get('dedup_group_size', 1)}  pyr={pyramid_key(r)}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(kept, fh, indent=2)
        print(f"\nwrote {len(kept)} record(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
