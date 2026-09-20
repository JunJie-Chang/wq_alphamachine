"""How correlated is each Power Pool candidate with what you have ALREADY submitted?

    python3 scripts/corr_powerpool.py --from-file pp_dedup.json --top 20
    python3 scripts/corr_powerpool.py ID1 ID2 ID3 --exclude ID9

The last gate before a submission slot is spent. `/check` cannot be that gate: it
is cached, so after the book changes it keeps showing the old
POWER_POOL_CORRELATION (a real case: it read 0.1691 while the submit attempt saw
0.7162) or falls back to PENDING. This script recomputes from PnL, live, against
the CURRENT book.

Method, and why it is trustworthy: Pearson correlation of daily PnL changes over
the LAST FOUR YEARS (`correlate.BRAIN_CORR_YEARS`). That window reproduces
BRAIN's own reported numbers to four decimals (SELF 0.3434, POWER_POOL 0.3365,
0.1637, 0.7162); the whole 10-year history reads 0.03-0.05 low. It is a
reproduction of the platform's number, not an independent estimate -- but
BRAIN's number for the CANDIDATE against the book, which is what gets checked at
submission.

Reports the max against the Power Pool alphas (limit 0.5) and against the regular
ones (SELF_CORRELATION, limit 0.7), and which alpha is the worst. PnL of an IS
simulation never changes, so it is cached in data/pnl_cache.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient
from alphamachine.correlate import BRAIN_CORR_YEARS, aligned_corr, daily_returns
from alphamachine.metrics import pnl_series

CACHE = "data/pnl_cache.json"
PP_LIMIT = 0.5
SELF_LIMIT = 0.7
# Stay this far under the limit: the number is a reproduction, not the platform's
# own call, and the pool moves between now and the submit.
SAFETY = 0.10


def is_power_pool(alpha: dict) -> bool:
    names = [c.get("name") if isinstance(c, dict) else c
             for c in alpha.get("classifications") or []]
    return "Power Pool Alpha" in names or "PowerPoolSelected" in (alpha.get("tags") or [])


def load_cache(path: str = CACHE) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict, path: str = CACHE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(cache, fh)


def series_for(client, alpha_id: str, cache: dict):
    if alpha_id not in cache:
        cache[alpha_id] = pnl_series(client.alpha_pnl(alpha_id))
    dates, vals = cache[alpha_id]
    return daily_returns(dates, vals)


def worst(cand_series, pool: dict[str, tuple]) -> tuple[float, str | None]:
    """(max correlation, which alpha) of a candidate against a pool."""
    best, who = float("-inf"), None
    for pid, s in pool.items():
        c = aligned_corr(cand_series, s, years=BRAIN_CORR_YEARS)
        if c > best:
            best, who = c, pid
    return (best, who) if who else (float("nan"), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("alpha_ids", nargs="*")
    ap.add_argument("--from-file", default=None,
                    help="dedup_powerpool.py output; candidates are taken in FILE order")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="ids to leave out (already handled)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ids = list(args.alpha_ids)
    if args.from_file:
        with open(args.from_file) as fh:
            ids += [r["alpha_id"] for r in json.load(fh)]
    ids = [a for a in dict.fromkeys(ids) if a not in set(args.exclude)][:max(args.top, len(args.alpha_ids))]
    if not ids:
        print("no candidates", file=sys.stderr)
        return 2

    _load_env()
    client = BrainClient(os.environ.get("WQ_EMAIL", ""), os.environ.get("WQ_PASSWORD", ""))
    try:
        client.authenticate()
    except (BiometricRequired, AuthError) as e:
        print(f"authentication failed: {e}", file=sys.stderr)
        return 1

    book = client.my_alphas(status="ACTIVE", limit=100)
    cache = load_cache()
    print(f"book: {len(book)} ACTIVE ({sum(is_power_pool(b) for b in book)} Power Pool); "
          f"{len(ids)} candidate(s); window = last {BRAIN_CORR_YEARS} years", file=sys.stderr)

    pp_pool, reg_pool = {}, {}
    for b in book:
        (pp_pool if is_power_pool(b) else reg_pool)[b["id"]] = series_for(client, b["id"], cache)
    rows = []
    for a in ids:
        if a in pp_pool or a in reg_pool:
            continue                                    # already submitted
        s = series_for(client, a, cache)
        pc, pw = worst(s, pp_pool)
        rc, rw = worst(s, reg_pool)
        rows.append({"alpha_id": a, "pp_corr": pc, "pp_worst": pw,
                     "self_corr": rc, "self_worst": rw,
                     "safe": pc <= PP_LIMIT - SAFETY and rc <= SELF_LIMIT - SAFETY})
    save_cache(cache)
    rows.sort(key=lambda r: r["pp_corr"])

    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    print(f"\n{'alpha':10} {'PP corr':>8} (limit {PP_LIMIT})  {'worst PP':10}  {'vs regular':>10} (limit {SELF_LIMIT})  {'worst reg':10}  verdict")
    for r in rows:
        v = "ok" if r["safe"] else ("TOO CLOSE" if r["pp_corr"] <= PP_LIMIT else "OVER")
        print(f"{r['alpha_id']:10} {r['pp_corr']:>8.4f}               {str(r['pp_worst']):10}  "
              f"{r['self_corr']:>10.4f}               {str(r['self_worst']):10}  {v}")
    print(f"\n'ok' = at least {SAFETY} under both limits (a reproduction of BRAIN's number, "
          f"not BRAIN's own call).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
