"""How correlated is each Power Pool candidate with what you have ALREADY submitted?

    python3 scripts/corr_powerpool.py --from-file pp_dedup.json --top 20
    python3 scripts/corr_powerpool.py ID1 ID2 ID3 --exclude ID9
    python3 scripts/corr_powerpool.py --from-file pp_dedup_wide.json --top 200 --pick 4

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

`--pick N` chooses the next batch to submit, not just a ranking. Greedy: take the best
remaining candidate, add it to the book, re-measure the rest against the grown book
(twins of what was just picked drop out), repeat. "Best" is Sharpe plus a tilt for
PROGRESS TOWARD COMPLETING A PYRAMID -- region x delay x dataset category. A pyramid
counts as formulated toward Genius tier at PYRAMID_MIN_ALPHAS (3) alphas, so an alpha in
one that is already full adds nothing beyond its own quality, while one that takes a
pyramid from k to k+1 earns `config.PYRAMID_PICK_WEIGHT` * (k+1)/3 Sharpe points -- the
most for the one that crosses the line (the Dynamic Pyramid Multiplier also pays more for
under-used pyramids). Hard floors apply first and are never traded off: Sharpe >= 1.0, recent
performance not decayed, and the correlation margins above.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient
from alphamachine.config import PYRAMID_MIN_ALPHAS, PYRAMID_PICK_WEIGHT
from alphamachine.correlate import BRAIN_CORR_YEARS, aligned_corr, daily_returns
from alphamachine.metrics import extract, pnl_series

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


def pick_batch(cands: list[dict], series: dict, pp_pool: dict, reg_pool: dict,
               pyramid_counts, n: int, *, pp_limit: float = PP_LIMIT - SAFETY,
               reg_limit: float = SELF_LIMIT - SAFETY,
               weight: float = PYRAMID_PICK_WEIGHT,
               min_alphas: int = PYRAMID_MIN_ALPHAS) -> list[dict]:
    """Pure: choose up to `n` alphas, one at a time, each added to the book before the
    next is judged.

    cands: {"alpha_id", "sharpe", "pyramids", "recency"} records (dedup output).
    series: alpha_id -> (dates, daily returns). pp_pool / reg_pool: the book's series.
    pyramid_counts: alphas already submitted per pyramid name.
    """
    pp_pool, reg_pool = dict(pp_pool), dict(reg_pool)
    counts = Counter(pyramid_counts)
    left = {c["alpha_id"]: c for c in cands
            if c["alpha_id"] not in pp_pool and c["alpha_id"] not in reg_pool}
    picks: list[dict] = []
    while len(picks) < n and left:
        best = None
        for aid, c in left.items():
            sharpe = c.get("sharpe") or 0.0
            if sharpe < 1.0 or (c.get("recency") or {}).get("ok") is False:
                continue
            pc, pw = worst(series[aid], pp_pool)
            rc, rw = worst(series[aid], reg_pool)
            pc = 0.0 if pc != pc else pc            # empty pool: nothing to correlate with
            rc = 0.0 if rc != rc else rc
            if pc > pp_limit or rc > reg_limit:
                continue
            names = list(c.get("pyramids") or [])
            advances = [p for p in names if counts[p] < min_alphas]
            # progress toward the threshold, not a flat credit per pyramid touched
            score = sharpe + weight * sum((counts[p] + 1) / min_alphas for p in advances)
            if best is None or score > best["score"]:
                best = {"alpha_id": aid, "sharpe": sharpe, "score": score,
                        "pp_corr": pc, "pp_worst": pw, "self_corr": rc, "self_worst": rw,
                        "pyramids": names, "advances": advances,
                        "counts_before": {p: counts[p] for p in names}}
        if best is None:
            break
        picks.append(best)
        aid = best["alpha_id"]
        pp_pool[aid] = series[aid]                  # Power Pool alphas correlate against each other
        counts.update(best["pyramids"])
        left.pop(aid)
    return picks


def pyramid_coverage(counts, min_alphas: int = PYRAMID_MIN_ALPHAS) -> int:
    return sum(1 for v in counts.values() if v >= min_alphas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("alpha_ids", nargs="*")
    ap.add_argument("--from-file", default=None,
                    help="dedup_powerpool.py output; candidates are taken in FILE order")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="ids to leave out (already handled)")
    ap.add_argument("--pick", type=int, default=None, metavar="N",
                    help="choose the next N to submit: greedy, pyramid-aware (needs --from-file)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.pick and not args.from_file:
        print("--pick needs --from-file (it reads Sharpe, pyramids and recency from "
              "the dedup records)", file=sys.stderr)
        return 2

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
    if args.pick:
        return _pick(client, cache, book, pp_pool, reg_pool, ids, args)
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


def _pick(client, cache, book, pp_pool, reg_pool, ids, args) -> int:
    with open(args.from_file) as fh:
        recs = {r["alpha_id"]: r for r in json.load(fh)}
    cands = [recs[a] for a in ids if a in recs]
    counts: Counter = Counter()
    for b in book:                                   # every submitted alpha counts toward Genius
        counts.update(extract(client.alpha(b["id"])).get("pyramid_names") or [])
    series = {c["alpha_id"]: series_for(client, c["alpha_id"], cache) for c in cands}
    save_cache(cache)
    picks = pick_batch(cands, series, pp_pool, reg_pool, counts, args.pick)
    after = Counter(counts)
    for p in picks:
        after.update(p["pyramids"])
    tagged = {}
    for p in picks:
        al = client.alpha(p["alpha_id"])
        tagged[p["alpha_id"]] = ("PowerPoolSelected" in (al.get("tags") or [])
                                 and bool((al.get("regular") or {}).get("description")))
    if args.json:
        print(json.dumps({"picks": [{**p, "tagged_and_described": tagged[p["alpha_id"]]} for p in picks],
                          "pyramid_counts_before": counts, "pyramid_counts_after": after}, indent=1))
        return 0
    print(f"\npyramids in the ACTIVE book (>= {PYRAMID_MIN_ALPHAS} = formulated):")
    for name, v in counts.most_common():
        print(f"   {v:>2}  {name}{'   (full)' if v >= PYRAMID_MIN_ALPHAS else ''}")
    print(f"\nnext {len(picks)} of {args.pick} asked, greedy, each added to the book before the next:")
    for i, p in enumerate(picks, 1):
        adv = ", ".join(f"{q} {p['counts_before'][q]}->{p['counts_before'][q] + 1}"
                        + (" COMPLETES it" if p['counts_before'][q] + 1 >= PYRAMID_MIN_ALPHAS else "")
                        for q in p["advances"]) or "none (all full)"
        print(f"  {i}. {p['alpha_id']}  sharpe={p['sharpe']:.2f}  PP corr={p['pp_corr']:.3f} ({p['pp_worst']})  "
              f"vs regular={p['self_corr']:.3f}  {'TAGGED+described' if tagged[p['alpha_id']] else 'NEEDS tag + description'}")
        print(f"       pyramids {p['pyramids']}   advances: {adv}")
    print(f"\nformulated pyramids: {pyramid_coverage(counts)} -> {pyramid_coverage(after)}")
    if len(picks) < args.pick:
        print(f"only {len(picks)} candidate(s) cleared every floor; the rest failed Sharpe / "
              f"recency / the correlation margins against the growing book.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
