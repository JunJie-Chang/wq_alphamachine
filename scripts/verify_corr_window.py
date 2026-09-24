"""Re-test the claim that BRAIN measures SELF_CORRELATION over the last four years.

    python3 scripts/verify_corr_window.py                 # offline, from stored data
    python3 scripts/verify_corr_window.py --windows 2,3,4,5

`correlate.BRAIN_CORR_YEARS = 4` is NOT from any WorldQuant document. docs/ says only
"PnL 序列相關 < 0.7" and names no window at all. The constant came from commit b8580e3
(2026-09-20), which fitted it against eleven numbers BRAIN had reported -- matching to
four decimals eleven times, and failing to explain a twelfth (a SELF_CORRELATION of
0.2271 against a computed 0.1791). Its own files call it "a reproduction of the
platform's number, not an independent estimate", and corr_powerpool.py carries a
SAFETY margin of 0.10 because of that.

That evidence is good but it is ours, and it is a year of platform drift old. The main
pipeline has never used the window -- CorrelationBook.max_corr correlates the whole
overlap, which correlate.py's own comment says reads 0.03-0.05 LOW, the unsafe
direction for a "is this too correlated" gate. So before propagating the window, re-run
the test on current data.

WHAT THIS DOES, and why it needs no network and no quota: `/check` payloads are already
stored in `deep_checks`, and daily PnL is already stored in `pnl`. For every alpha
holding both, it recomputes the maximum correlation against the rest of the book at
each candidate window and compares that with the SELF_CORRELATION the platform itself
reported. The original alpha ids were never recorded in the commit, so this does not
replay those eleven pairs -- it re-runs the same experiment on whatever the book holds
now, which is the stronger test anyway.

DECISION RULE, stated before the numbers are seen:
  * propagate the window into CorrelationBook only if 4 years reproduces the platform
    to within a median absolute error clearly below the other windows tested;
  * if no window separates itself, leave correlate.py alone -- the 0.55 selection
    threshold already sits 0.15 under the platform's 0.70;
  * either way, do NOT apply the window to PROD_CORRELATION. Every reproduced number
    was SELF or POWER_POOL. PROD compares against alphas whose PnL cannot be fetched,
    so nothing here is evidence about it.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine import audit
from alphamachine.correlate import BRAIN_CORR_YEARS, aligned_corr, daily_returns
from alphamachine.metrics import check_value


def reported_self_corr(conn) -> dict[str, float]:
    """alpha_id -> the SELF_CORRELATION BRAIN reported for it, from stored /check."""
    out: dict[str, float] = {}
    for r in conn.execute(
            "SELECT alpha_id, checks FROM deep_checks WHERE checks IS NOT NULL"):
        try:
            checks = json.loads(r["checks"] or "[]")
        except (ValueError, TypeError):
            continue
        v = check_value(checks, "SELF_CORRELATION")
        if v is not None:
            out[r["alpha_id"]] = float(v)
    return out


def load_series(conn) -> dict[str, tuple[list[str], object]]:
    out = {}
    for r in conn.execute("SELECT alpha_id, dates, vals FROM pnl"):
        try:
            d, v = json.loads(r["dates"]), json.loads(r["vals"])
        except (ValueError, TypeError):
            continue
        if d and v:
            out[r["alpha_id"]] = daily_returns(d, v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser("verify_corr_window")
    ap.add_argument("--db", default="data/machine.db")
    ap.add_argument("--windows", default="2,3,4,5,6",
                    help="comma-separated year windows to test; 'all' is added")
    ap.add_argument("--json", help="write the per-alpha table here")
    a = ap.parse_args(argv)

    conn = audit.open_ro(a.db)
    try:
        reported = reported_self_corr(conn)
        series = load_series(conn)
    finally:
        conn.close()

    usable = [aid for aid in reported if aid in series]
    print(f"\n{len(reported)} alpha(s) with a reported SELF_CORRELATION, "
          f"{len(series)} with stored PnL, {len(usable)} with both")
    if len(usable) < 2:
        print("\nNot enough overlap to reproduce anything. SELF_CORRELATION is the max\n"
              "against the REST of the book, so this needs several alphas that have\n"
              "both a stored /check and a stored PnL series. Run this on the machine\n"
              "holding the accumulated database.\n", file=sys.stderr)
        return 2

    windows: list[int | None] = [int(w) for w in a.windows.split(",") if w.strip()]
    windows.append(None)                      # the whole overlap, today's behaviour

    rows = []
    for aid in usable:
        # SELF_CORRELATION is the maximum against everything else you own, so the
        # comparison has to be a max over the book, not a single pair.
        rec: dict = {"alpha_id": aid, "reported": reported[aid]}
        for w in windows:
            best = 0.0
            for other, s in series.items():
                if other == aid:
                    continue
                c = abs(aligned_corr(series[aid], s, years=w))
                best = max(best, c)
            rec[str(w)] = best
            rec[f"err_{w}"] = abs(best - reported[aid])
        rows.append(rec)

    print(f"\nper-window absolute error against BRAIN's own number "
          f"(n={len(rows)}):\n")
    print(f"    {'window':<10} {'median':>9} {'mean':>9} {'max':>9} "
          f"{'<=0.001':>8} {'<=0.01':>8}")
    summary = {}
    for w in windows:
        errs = [r[f"err_{w}"] for r in rows]
        label = f"{w} years" if w else "whole"
        summary[label] = {
            "median": statistics.median(errs),
            "mean": sum(errs) / len(errs),
            "max": max(errs),
            "within_0001": sum(1 for e in errs if e <= 0.001),
            "within_001": sum(1 for e in errs if e <= 0.01)}
        s = summary[label]
        print(f"    {label:<10} {s['median']:>9.4f} {s['mean']:>9.4f} "
              f"{s['max']:>9.4f} {s['within_0001']:>8} {s['within_001']:>8}")

    best_label = min(summary, key=lambda k: summary[k]["median"])
    four = summary.get(f"{BRAIN_CORR_YEARS} years")
    whole = summary.get("whole")
    print(f"\n  lowest median error: {best_label}")
    if four and whole:
        print(f"  {BRAIN_CORR_YEARS}-year median {four['median']:.4f} vs "
              f"whole-history {whole['median']:.4f} "
              f"({'better' if four['median'] < whole['median'] else 'NOT better'})")
    print(f"\n  decision rule: propagate into CorrelationBook only if "
          f"{BRAIN_CORR_YEARS} years is clearly\n"
          f"  the lowest. Never apply any window to PROD_CORRELATION -- no number\n"
          f"  behind this constant was ever a PROD one.\n")

    worst = sorted(rows, key=lambda r: -r[f"err_{BRAIN_CORR_YEARS}"])[:10]
    print(f"  worst {len(worst)} at {BRAIN_CORR_YEARS} years "
          f"(reported vs computed):")
    for r in worst:
        print(f"    {r['alpha_id']:<14} reported {r['reported']:.4f}  "
              f"computed {r[str(BRAIN_CORR_YEARS)]:.4f}  "
              f"err {r[f'err_{BRAIN_CORR_YEARS}']:.4f}")
    print()

    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"summary": summary, "rows": rows}, fh, indent=1)
        print(f"  wrote {a.json}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
