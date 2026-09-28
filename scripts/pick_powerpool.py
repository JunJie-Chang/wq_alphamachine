"""Choose the next Power Pool alphas to submit, plus backups, on BRAIN's numbers alone.

    python3 scripts/pick_powerpool.py --from-file pp_dedup.json --pick 4 --backups 4
    python3 scripts/pick_powerpool.py --from-file pp_dedup.json --top 40 --json

For each candidate (file order, first --top), one live GET /alphas/{id}/check. Kept:
nothing blocking (check_powerpool.blocking_fails), POWER_POOL_CORRELATION and
SELF_CORRELATION both PASS, recent performance not decayed. Ranked by Sharpe. The
first --pick are the picks, the next --backups the backups -- hand the whole list,
in order, to submit_powerpool.py, which fills the day's slots from it.

Why backups and not a smarter pick: this script used to recompute correlation from
PnL and count pyramid progress from MATCHES_PYRAMID. Both were wrong when it
mattered -- a candidate it passed drew a POWER_POOL_CORRELATION WARNING from
BRAIN, and its pyramid count disagreed with BRAIN's own tally (how BRAIN assigns
an alpha among the pyramids it matches is not published). So every number here
is BRAIN's, with the time it was read; the tally is read several times, because
the endpoint has returned two different answers seconds apart. The one thing BRAIN cannot report is whether
two unsubmitted candidates are twins of each other; that shows only once one is
live, and submit_powerpool.py's live /check before each submit catches it. A
backup takes the slot of a pick it rejects.

A POWER_POOL_CORRELATION WARNING means BRAIN did not grant the 10%-Sharpe
exception, and the submit would be refused, so WARNING is excluded, not waved
through. The values drift as alphas join the book: each is shown with its age.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient
from alphamachine.metrics import (check_result, check_value, completed_pyramids,
                                  stale_note, tally_disagreement)

from check_powerpool import blocking_fails, pyramid_names, recency

CORR_CHECKS = ("POWER_POOL_CORRELATION", "SELF_CORRELATION")


def judge(rec: dict, checks: list[dict], checked_at: float) -> dict:
    """Pure: one candidate's standing from BRAIN's live /check payload."""
    pairs = [(c.get("name"), c.get("result")) for c in checks]
    why = [f"{n} FAIL" for n in blocking_fails(pairs)]
    for name in CORR_CHECKS:
        res = check_result(checks, name)
        if res != "PASS":
            why.append(f"{name} {res or 'missing'}")
    rc = recency(checks)
    if rc.get("ok") is False:
        why.append("recent performance decayed")
    sharpe = rec.get("sharpe") or 0.0
    if sharpe < 1.0:
        why.append(f"Sharpe {sharpe} < 1.0")
    return {"alpha_id": rec["alpha_id"], "sharpe": sharpe,
            "pp_corr": check_value(checks, "POWER_POOL_CORRELATION"),
            "self_corr": check_value(checks, "SELF_CORRELATION"),
            "checked_at": checked_at, "pyramids": pyramid_names(checks),
            "ok": not why, "why": why,
            # BRAIN has not computed a correlation yet: unknown, not rejected.
            "pending": bool(why) and all(w.endswith(" PENDING") for w in why)}


def order(judged: list[dict], pick: int, backups: int) -> tuple[list[dict], list[dict]]:
    """Pure: (picks, backups) among the ok ones, best Sharpe first."""
    ok = sorted((j for j in judged if j["ok"]), key=lambda j: -j["sharpe"])
    return ok[:pick], ok[pick:pick + backups]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-file", required=True, help="dedup_powerpool.py output")
    ap.add_argument("--top", type=int, default=40,
                    help="live-check at most this many (one API call each)")
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--pick", type=int, default=4)
    ap.add_argument("--backups", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with open(args.from_file) as fh:
        recs = [r for r in json.load(fh) if r["alpha_id"] not in set(args.exclude)]
    recs = recs[:args.top]
    if not recs:
        print("no candidates", file=sys.stderr)
        return 2

    _load_env()
    client = BrainClient(os.environ.get("WQ_EMAIL", ""), os.environ.get("WQ_PASSWORD", ""))
    try:
        client.authenticate()
    except (BiometricRequired, AuthError) as e:
        print(f"authentication failed: {e}", file=sys.stderr)
        return 1

    tally_at = time.time()
    tallies = client.pyramid_tallies(reads=5)
    tally = tallies[0]
    active = {a["id"] for a in client.my_alphas(status="ACTIVE", limit=100)}
    judged = []
    for r in recs:
        if r["alpha_id"] in active:
            continue                                    # already submitted
        try:
            body = client.alpha_check(r["alpha_id"])
        except Exception as e:
            judged.append({"alpha_id": r["alpha_id"], "sharpe": r.get("sharpe") or 0.0,
                           "ok": False, "why": [f"check unavailable ({e})"]})
            continue
        checks = (body.get("is") or {}).get("checks") or body.get("checks") or []
        judged.append(judge({"alpha_id": r["alpha_id"], "sharpe": r.get("sharpe")},
                            checks, time.time()))
    picks, backups = order(judged, args.pick, args.backups)
    for p in picks + backups:
        al = client.alpha(p["alpha_id"])
        p["tagged_and_described"] = ("PowerPoolSelected" in (al.get("tags") or [])
                                     and bool((al.get("regular") or {}).get("description")))

    if args.json:
        print(json.dumps({"pyramid_tally": tally, "tally_read_at": tally_at,
                          "tally_disagreement": tally_disagreement(tallies),
                          "picks": picks, "backups": backups,
                          "excluded": [j for j in judged if not j["ok"]]}, indent=1))
        return 0

    done = completed_pyramids(tally)
    print(f"pyramids (BRAIN's tally, {stale_note(tally_at)}): {len(done)} formulated")
    if tally_disagreement(tallies):
        print(f"   NOTE: {tally_disagreement(tallies)}")
    for t in tally:
        print(f"   {t['alphas']:>2}  {t['name']}{'   (formulated)' if t['name'] in done else ''}")

    def show(i, p):
        print(f"  {i}. {p['alpha_id']}  sharpe={p['sharpe']:.2f}  "
              f"PP corr={p['pp_corr']}  SELF corr={p['self_corr']}  "
              f"[{stale_note(p['checked_at'])}]  "
              f"{'TAGGED+described' if p['tagged_and_described'] else 'NEEDS tag + description'}")
        print(f"       matches {p['pyramids']} (which of these BRAIN credits is its call)")
    print(f"\npicks ({len(picks)} of {args.pick}):")
    for i, p in enumerate(picks, 1):
        show(i, p)
    print(f"\nbackups ({len(backups)} of {args.backups}), in order:")
    for i, p in enumerate(backups, len(picks) + 1):
        show(i, p)
    waiting = [j for j in judged if j.get("pending")]
    if waiting:
        print(f"\nBRAIN still computing ({len(waiting)}) -- unknown, not rejected; re-run "
              f"later (a burst of /check calls, or a spent daily cap, can hold these for hours):")
        print("   " + " ".join(j["alpha_id"] for j in waiting))
    bad = [j for j in judged if not j["ok"] and not j.get("pending")]
    if bad:
        print(f"\nexcluded {len(bad)}:")
        for j in bad[:15]:
            print(f"   {j['alpha_id']}  {'; '.join(j['why'][:3])}")
    print("\nnot checked: correlation between these candidates (BRAIN reports it only "
          "once one is live).\nsubmit them in this order; submit_powerpool.py re-checks "
          "each live and moves to the next on a refusal:")
    print("  python3 scripts/submit_powerpool.py "
          + " ".join(p["alpha_id"] for p in picks + backups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
