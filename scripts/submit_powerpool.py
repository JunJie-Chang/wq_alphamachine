"""Submit specific alpha IDs as Pure Power Pool alphas, for real.

Deliberately does NOT go through Submitter.submit() -- that path applies
this pipeline's own private quality_verdict (Consultant D1 Regular-alpha
thresholds: fitness>=1.0, 2Y sharpe>=1.58, margin>=4bps...), which a Pure
Power Pool alpha is explicitly exempt from
(docs/WQ_BRAIN_報酬機制完整說明.md §3.2). Confirmed live 2026-09-19: once an
alpha carries PowerPoolSelected + a compliant description, BRAIN itself
reclassifies LOW_FITNESS/PROD_CORRELATION/LOW_2Y_SHARPE from FAIL to
WARNING -- so the only gate that should block a Pure PP submission is
BRAIN's own live check (platform_verdict / a fresh alpha_check() call), not
this pipeline's stricter private bar.

    python3 scripts/submit_powerpool.py PICK1 PICK2 ... BACKUP1 BACKUP2 ... [--need K] [--live]

Give the ids IN ORDER, picks then backups (pick_powerpool.py prints the line). It
goes down the list until K are live (default: every slot left today): each gets
BRAIN's live /check, which sees everything submitted a moment earlier, so a
candidate that turns out to be a twin of one just sent -- the one thing BRAIN
cannot report in advance -- is skipped and the next takes its slot. A POST that
BRAIN refuses (4xx) is skipped the same way. It stops without sending more if
BRAIN has not decided a submission within the wait, rather than risk a slot twice.

Without --live this is a dry run (fetches + server-checks, never POSTs
/submit). --live actually submits and polls for status == ACTIVE (a 201 is
"accepted for processing", not "live" -- submit.py's own lesson).

Today's count is read from the PLATFORM (your ACTIVE alphas' dateSubmitted), so
alphas sent from the web UI are counted. There is no local counter to keep in sync
and no --used-today; BRAIN's own REGULAR_SUBMISSION check, read inside the live
/check right before each submit, remains the final authority.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient, BrainError
from alphamachine.config import RunConfig
from alphamachine.metrics import extract, failed_checks, platform_verdict
from alphamachine.store import Store
from alphamachine.submit import (await_submission, count_submitted_today,
                                 refused_submission, submission_verdict,
                                 submit_in_order, sync_active_submissions)


def submit_as_power_pool(client: BrainClient, store: Store, alpha_id: str,
                          *, dry_run: bool) -> dict:
    alpha = client.alpha(alpha_id)
    rec = extract(alpha)
    report: dict = {"alpha_id": alpha_id, "sharpe": rec["sharpe"],
                    "tags": alpha.get("tags"),
                    "has_description": bool((alpha.get("regular") or {}).get("description"))}

    pv = platform_verdict(rec)
    report["platform"] = {"ok": pv.ok, "reasons": pv.reasons}
    if not pv.ok:
        report["ok"] = False
        report["submitted"] = False
        return report

    # Live, uncached -- the stored `is.checks` can be stale; POWER_POOL_CORRELATION
    # and the others must be re-verified against BRAIN's current state right
    # before submitting, same principle as submit.py's server_check().
    body = client.alpha_check(alpha_id)
    checks = (body.get("is") or {}).get("checks") or body.get("checks") or []
    fails = failed_checks(checks)
    report["server_check"] = {"ok": not fails, "fails": fails}
    if fails:
        report["ok"] = False
        report["submitted"] = False
        return report

    report["ok"] = True
    if dry_run:
        report["submitted"] = False
        report["would_submit"] = True
        report["note"] = "dry run -- would have submitted"
        return report

    try:
        client.submit(alpha_id)
    except BrainError as e:
        return refused_submission(report, e)
    final = await_submission(client, alpha_id)
    status = final.get("status")
    verdict = submission_verdict(status)
    report["submitted"] = status == "ACTIVE"
    report["final_status"] = status
    report["verdict"] = verdict
    report["dateSubmitted"] = final.get("dateSubmitted")
    if status != "ACTIVE":
        report["reason"] = failed_checks(
            (final.get("is") or {}).get("checks") or []) or "unknown"
    store.record_submission(alpha_id, verdict, status or "UNKNOWN", report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("alpha_ids", nargs="+", help="picks then backups, in order")
    ap.add_argument("--need", type=int, default=None,
                    help="how many to get live (default: every slot left today)")
    ap.add_argument("--live", action="store_true",
                     help="actually POST /submit (default: dry run)")
    args = ap.parse_args()

    _load_env()
    cfg = RunConfig()
    if not cfg.email or not cfg.password:
        print("WQ_EMAIL / WQ_PASSWORD not set.", file=sys.stderr)
        return 2

    client = BrainClient(cfg.email, cfg.password)
    try:
        client.authenticate()
    except (BiometricRequired, AuthError) as e:
        print(f"authentication failed: {e}", file=sys.stderr)
        return 1

    store = Store(cfg.db_path)
    try:
        active = client.my_alphas(status="ACTIVE", limit=100)
    except Exception as e:
        print(f"cannot read your ACTIVE alphas from the platform ({e}); not "
              f"submitting on a guess about today's count.", file=sys.stderr)
        return 1
    used = count_submitted_today(active)
    remaining = max(0, cfg.max_submits_per_day - used)
    want = remaining if args.need is None else min(args.need, remaining)
    print(f"submitted today (platform): {used}/{cfg.max_submits_per_day}  "
          f"-> {remaining} left; aiming for {want} from {len(args.alpha_ids)} "
          f"candidate(s) in order", file=sys.stderr)
    try:                                   # bookkeeping only; nothing depends on it
        sync_active_submissions(client, store, rows=active)
    except Exception as e:
        print(f"note: could not refresh the local ledger ({e})", file=sys.stderr)

    def left() -> int:                     # re-read before every live submit
        rows = client.my_alphas(status="ACTIVE", limit=100)
        return cfg.max_submits_per_day - count_submitted_today(rows)

    res = submit_in_order(
        args.alpha_ids, want,
        lambda aid: submit_as_power_pool(client, store, aid, dry_run=not args.live),
        budget_left=left if args.live else None)
    for report in res["sent"] + res["skipped"] + ([res["pending"]] if res["pending"] else []):
        print(f"\n===== {report['alpha_id']} =====")
        print(json.dumps(report, indent=2, default=str))

    def why(r):
        return (r.get("server_check", {}).get("fails") or r.get("reason")
                or r.get("platform", {}).get("reasons") or "?")
    print(f"\n{'submitted' if args.live else 'would submit'}: "
          f"{[r['alpha_id'] for r in res['sent']] or 'none'}")
    for r in res["skipped"]:
        print(f"skipped {r['alpha_id']}: {why(r)}")
    if res["pending"]:
        print(f"STOPPED: BRAIN has not decided {res['pending']['alpha_id']} yet -- check "
              f"it on the platform before sending more")
    if res["unused"]:
        print(f"not tried: {res['unused']}")
    if len(res["sent"]) < want and not res["pending"]:
        print(f"only {len(res['sent'])} of {want} -- the list ran out; pick more backups")
    return 0

if __name__ == "__main__":
    sys.exit(main())
