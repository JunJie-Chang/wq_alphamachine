"""Screen your simulated-but-not-yet-submitted alphas for Power Pool eligibility.

Power Pool tagging works on alphas you already have in hand from simulation:
you pick the ones that meet the criteria below, mark them PowerPoolSelected
(with an Idea+Rationale description) in Properties, then submit. So the pool
worth screening is UNSUBMITTED alphas -- not alphas already ACTIVE, which are
already past that decision.

    python3 scripts/check_powerpool.py [--status UNSUBMITTED] [--deep] [--top N] [--json]

Power Pool Alpha requirements (docs/Power_Pool_Alpha_規則.md), all required:

  * Sharpe ratio >= 1.0
  * <= 8 distinct operators (ts_backfill / group_backfill excluded)
  * <= 3 distinct data fields (group fields -- country, industry,
    subindustry, currency, market, sector, exchange -- excluded)
  * Power Pool correlation < 0.5 (or Sharpe >= 10% above the most
    correlated Power Pool alpha)
  * PASS on the turnover test and sub-universe test

BRAIN's own IS-check payload turns out to carry a check literally named
POWER_POOL_CORRELATION (confirmed live, 2026-09-19) alongside LOW_TURNOVER /
HIGH_TURNOVER (turnover test) and LOW_SUB_UNIVERSE_SHARPE (sub-universe
test). There is no check name in that payload that obviously maps to
"robust-universe test" -- every check the alpha actually carries is printed
so you can eyeball it.

The catch: for an alpha that has only been simulated, not yet checked or
submitted, BRAIN computes these lazily -- they sit at result=PENDING until
something calls GET /alphas/{id}/check on that specific alpha (this is what
submit.py's server_check() does right before a real submission). Without
--deep this script only screens Sharpe/operator-count/field-count, which is
everything answerable from the alpha's stored simulation record, and reports
whichever of the three server checks are still PENDING. With --deep it calls
alpha_check() on the (optionally --top N) surviving candidates to force BRAIN
to actually compute them -- one extra live API call per alpha, so keep --top
sane against 245+ candidates.

Operator/data-field counts are parsed heuristically from the alpha's fastexpr
source (matched against the live /operators list) and are usually right, but
are not a substitute for the counts BRAIN itself shows on the alpha page.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient
from alphamachine.metrics import check_result, check_value, extract

GROUP_FIELDS = {"country", "industry", "subindustry", "currency", "market",
                "sector", "exchange"}
EXCLUDED_OPERATORS = {"ts_backfill", "group_backfill"}
KEYWORDS = {"if", "else", "then", "and", "or", "not", "true", "false", "nan"}

# The IS checks that map directly onto Power Pool's own eligibility rules.
SERVER_GATES = ("LOW_TURNOVER", "HIGH_TURNOVER", "LOW_SUB_UNIVERSE_SHARPE",
                "POWER_POOL_CORRELATION")
# Confirmed live (2026-09-19): these two exist as WARNING, not FAIL, so they
# never block submission -- but per docs/Power_Pool_Alpha_規則.md #5, an
# alpha without a compliant 100+ char Idea/Rationale description in
# Properties gets no Power Pool payout even if tagged. Reported, not gated.
DESCRIPTION_CHECKS = ("POWER_POOL_DESCRIPTION_LENGTH", "POWER_POOL_DESCRIPTION_FORMAT")

IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
KWARG_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?<![=!<>])=(?!=)")


def _iso(dt) -> str:
    return dt.isoformat()


def fetch_all_since(client: BrainClient, status: str, since_iso: str) -> list[dict]:
    """Page through /users/self/alphas from `since_iso` to now.

    BRAIN refuses to paginate a single query past ~1,000 results ("Cannot
    display more than the first 1,000 alphas"), but a consultant since August
    can easily have tens of thousands of UNSUBMITTED sims -- confirmed live,
    /users/self/alphas?status=UNSUBMITTED reports count=10000 (itself capped;
    the real total is unknown and >= that). So instead of one query, this
    recursively bisects the time range on dateCreated until each leaf window
    reports a safely sub-1000 count, then pages each leaf in full.
    """
    import datetime as _dt
    import time as _time

    import requests as _requests

    def _with_retry(fn, tries: int = 4):
        for i in range(tries):
            try:
                return fn()
            except _requests.exceptions.RequestException as e:
                if i + 1 == tries:
                    raise
                wait = 3.0 * (2 ** i)
                print(f"  network hiccup ({e.__class__.__name__}), retrying "
                      f"in {wait:.0f}s ...", file=sys.stderr)
                _time.sleep(wait)

    def count(start: str, end: str | None) -> int:
        params: dict = {"status": status, "limit": 1, "dateCreated>": start}
        if end:
            params["dateCreated<"] = end
        r = _with_retry(lambda: client.get("/users/self/alphas", params=params))
        if r.status_code != 200:
            return 0
        return int((r.json() or {}).get("count") or 0)

    def fetch_window(start: str, end: str | None) -> list[dict]:
        params: dict = {"status": status, "order": "-dateCreated",
                        "dateCreated>": start, "limit": 100}
        if end:
            params["dateCreated<"] = end
        return _with_retry(lambda: client._paged("/users/self/alphas", params))

    out: list[dict] = []

    def recurse(start_dt, end_dt) -> None:
        start_s, end_s = _iso(start_dt), _iso(end_dt) if end_dt else None
        c = count(start_s, end_s)
        if c == 0:
            return
        if c <= 900:
            out.extend(fetch_window(start_s, end_s))
            return
        upper = end_dt or _dt.datetime.now(_dt.timezone.utc)
        if upper - start_dt < _dt.timedelta(seconds=1):
            # Can't bisect any further -- take what pagination will give us.
            out.extend(fetch_window(start_s, end_s))
            return
        mid_dt = start_dt + (upper - start_dt) / 2
        recurse(start_dt, mid_dt)
        recurse(mid_dt, end_dt)

    since_dt = _dt.datetime.fromisoformat(since_iso)
    recurse(since_dt, None)
    return out


def alpha_code(alpha: dict) -> str:
    reg = alpha.get("regular") or {}
    return reg.get("code") or alpha.get("code") or ""


def parse_expr(code: str, operator_names: set[str]) -> tuple[set[str], set[str]]:
    """Best-effort split of an expression into (operators used, fields used)."""
    calls = {m.group(1) for m in CALL_RE.finditer(code)}
    kwargs = {m.group(1) for m in KWARG_RE.finditer(code)}
    all_idents = set(IDENT_RE.findall(code))

    operators = {c for c in calls if c in operator_names} - EXCLUDED_OPERATORS
    fields = {
        i for i in all_idents
        if i not in calls
        and i not in kwargs
        and i.lower() not in KEYWORDS
        and i.lower() not in GROUP_FIELDS
        and i not in operator_names
        and not i[0].isdigit()
    }
    return operators, fields


def gate_status(checks: list[dict]) -> dict:
    return {name: check_result(checks, name) for name in SERVER_GATES}


def evaluate(alpha: dict, operator_names: set[str]) -> dict:
    rec = extract(alpha)
    checks = rec["checks"]
    code = alpha_code(alpha)
    operators, fields = parse_expr(code, operator_names)

    sharpe = rec["sharpe"]
    sharpe_ok = sharpe is not None and sharpe >= 1.0
    op_ok = len(operators) <= 8
    field_ok = len(fields) <= 3

    gates = gate_status(checks)
    pp_corr = check_value(checks, "POWER_POOL_CORRELATION")
    failed_gates = [k for k, v in gates.items() if v == "FAIL"]
    pending_gates = [k for k, v in gates.items() if v in (None, "PENDING")]
    description_needs_work = [name for name in DESCRIPTION_CHECKS
                              if check_result(checks, name) == "WARNING"]

    reasons = []
    if not sharpe_ok:
        reasons.append(f"Sharpe {sharpe} < 1.0")
    if not op_ok:
        reasons.append(f"{len(operators)} distinct operators > 8: {sorted(operators)}")
    if not field_ok:
        reasons.append(f"{len(fields)} distinct fields > 3: {sorted(fields)}")
    for g in failed_gates:
        reasons.append(f"{g} FAIL")

    settings = alpha.get("settings") or {}
    return {
        "alpha_id": rec["alpha_id"],
        "status": rec["status"],
        "sharpe": sharpe,
        "region": settings.get("region"),
        "delay": settings.get("delay"),
        "n_operators": len(operators),
        "operators": sorted(operators),
        "n_fields": len(fields),
        "fields": sorted(fields),
        "power_pool_correlation": pp_corr,
        "pyramids": pyramid_names(checks),
        "gates": gates,
        "pending_gates": pending_gates,
        "description_needs_work": description_needs_work,
        "all_checks": [(c.get("name"), c.get("result")) for c in checks],
        "eligible": not reasons,
        "verified": not pending_gates,
        "reasons": reasons,
    }


def pyramid_names(checks: list[dict]) -> list[str]:
    """Which region×delay×dataset-category Pyramid(s) this alpha counts toward.

    Matters for two mechanisms beyond raw eligibility (see issue #4): Genius
    level needs a COUNT of distinct completed pyramids (>=3 alphas each), not
    total alpha count, and the Dynamic Pyramid Multiplier (1.0x-2.0x on base
    payment) rewards submitting into LESS-used pyramids. Piling picks into
    whichever pyramid the pipeline happened to generate the most candidates
    for helps neither.
    """
    for c in checks or []:
        if c.get("name") == "MATCHES_PYRAMID":
            return [p.get("name") for p in c.get("pyramids") or [] if p.get("name")]
    return []


def deepen(client: BrainClient, r: dict) -> dict:
    """Force BRAIN to actually compute the pending gates for one candidate."""
    try:
        body = client.alpha_check(r["alpha_id"])
    except Exception as e:
        r["deep_error"] = str(e)
        return r
    checks = ((body.get("is") or {}).get("checks") or body.get("checks") or [])
    gates = gate_status(checks)
    r["gates"] = gates
    r["power_pool_correlation"] = check_value(checks, "POWER_POOL_CORRELATION")
    r["pyramids"] = pyramid_names(checks)
    r["pending_gates"] = [k for k, v in gates.items() if v in (None, "PENDING")]
    r["verified"] = not r["pending_gates"]
    r["description_needs_work"] = [name for name in DESCRIPTION_CHECKS
                                   if check_result(checks, name) == "WARNING"]
    failed_gates = [k for k, v in gates.items() if v == "FAIL"]
    r["reasons"] = [x for x in r["reasons"] if not x.endswith("FAIL")] + \
        [f"{g} FAIL" for g in failed_gates]
    r["eligible"] = not r["reasons"]
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="UNSUBMITTED",
                    help="alpha status to pull from /users/self/alphas "
                         "(default: UNSUBMITTED, i.e. simulated candidates "
                         "not yet submitted -- pass ACTIVE to instead audit "
                         "alphas already live)")
    ap.add_argument("--deep", action="store_true",
                     help="force BRAIN to compute pending server checks "
                          "(turnover/sub-universe/power-pool-correlation) "
                          "for surviving candidates -- one live call each")
    ap.add_argument("--top", type=int, default=None,
                     help="with --deep, only deepen the top N candidates "
                          "(ranked by Sharpe) instead of all of them")
    ap.add_argument("--delay", type=float, default=2.0,
                     help="seconds to sleep between --deep calls (default "
                          "2.0). BRAIN computes POWER_POOL_CORRELATION "
                          "asynchronously; firing requests back-to-back "
                          "enqueues faster than it can compute and every "
                          "candidate comes back PENDING regardless of alpha "
                          "quality -- confirmed live 2026-09-19. Pass 0 only "
                          "if BRAIN has already shown fast, resolved (non-"
                          "PENDING) results on a small --top sample first.")
    ap.add_argument("--since", default=None,
                     help="ISO 8601 datetime with timezone offset (e.g. "
                          "2026-08-01T00:00:00+00:00); when given, pages "
                          "through the FULL history from this date via "
                          "date-window bisection instead of one capped query")
    ap.add_argument("--from-file", default=None,
                     help="load already-evaluated records (this script's own "
                          "--json output, e.g. after local dedup) instead of "
                          "re-fetching + re-evaluating from BRAIN -- only "
                          "--deep/--top/--json make sense combined with this")
    ap.add_argument("--json", action="store_true", help="dump raw JSON instead of a table")
    args = ap.parse_args()

    if args.from_file:
        results = json.load(open(args.from_file))
        eligible = sorted((r for r in results if r["eligible"]),
                          key=lambda r: -(r["sharpe"] or 0))
        _load_env()
        email = os.environ.get("WQ_EMAIL", "")
        password = os.environ.get("WQ_PASSWORD", "")
        if not email or not password:
            print("WQ_EMAIL / WQ_PASSWORD not set.", file=sys.stderr)
            return 2
        client = BrainClient(email, password)
        try:
            client.authenticate()
        except BiometricRequired as e:
            print(f"BRAIN wants a browser persona check: {e}\nRun "
                  f"scripts/auth_wait.py first.", file=sys.stderr)
            return 1
        except AuthError as e:
            print(f"authentication failed: {e}", file=sys.stderr)
            return 1
        return _process_and_report(client, results, eligible, args)

    _load_env()
    email = os.environ.get("WQ_EMAIL", "")
    password = os.environ.get("WQ_PASSWORD", "")
    if not email or not password:
        print("WQ_EMAIL / WQ_PASSWORD not set. Copy .env.example to .env and "
              "fill them in first.", file=sys.stderr)
        return 2

    client = BrainClient(email, password)
    try:
        client.authenticate()
    except BiometricRequired as e:
        print(f"BRAIN wants a browser persona check before this can log in: {e}\n"
              f"Run `python3 scripts/auth_wait.py`, complete the check it prints, "
              f"then re-run this script.", file=sys.stderr)
        return 1
    except AuthError as e:
        print(f"authentication failed: {e}", file=sys.stderr)
        return 1

    print("fetching operator list ...", file=sys.stderr)
    operator_names = {o.get("name") for o in client.operators() if o.get("name")}

    if args.since:
        print(f"fetching alphas with status={args.status} since {args.since} "
              f"(bisecting past the 1,000-per-query cap) ...", file=sys.stderr)
        alphas = fetch_all_since(client, args.status, args.since)
    else:
        print(f"fetching alphas with status={args.status} ...", file=sys.stderr)
        alphas = client.my_alphas(status=args.status)
    print(f"{len(alphas)} alpha(s) found.\n", file=sys.stderr)

    results = [evaluate(a, operator_names) for a in alphas]
    eligible = sorted((r for r in results if r["eligible"]),
                      key=lambda r: -(r["sharpe"] or 0))
    return _process_and_report(client, results, eligible, args)


def _process_and_report(client: BrainClient, results: list[dict],
                        eligible: list[dict], args) -> int:
    if args.deep:
        pool = eligible[:args.top] if args.top else eligible
        print(f"deepening {len(pool)} candidate(s) with a live /check call each ...",
              file=sys.stderr)
        for i, r in enumerate(pool):
            deepen(client, r)
            print(f"  [{i+1}/{len(pool)}] {r['alpha_id']}: "
                  f"eligible={r['eligible']} pending={r['pending_gates']} "
                  f"pyramids={r.get('pyramids')}",
                  file=sys.stderr)
            if args.delay and i + 1 < len(pool):
                time.sleep(args.delay)
        eligible = sorted((r for r in results if r["eligible"]),
                          key=lambda r: -(r["sharpe"] or 0))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    verified = [r for r in eligible if r["verified"]]
    provisional = [r for r in eligible if not r["verified"]]

    print(f"=== Power Pool candidates: {len(eligible)} / {len(results)} "
          f"(numeric + server gates) ===")
    print(f"    fully verified (no PENDING server check): {len(verified)}")
    print(f"    provisional (some server check still PENDING -- run --deep "
          f"to resolve): {len(provisional)}\n")

    for r in verified:
        desc_flag = " [NEEDS DESCRIPTION]" if r["description_needs_work"] else ""
        print(f"  [OK]  {r['alpha_id']}  sharpe={r['sharpe']:.2f}  "
              f"ops={r['n_operators']}({','.join(r['operators'])})  "
              f"fields={r['n_fields']}({','.join(r['fields'])})  "
              f"pp_corr={r['power_pool_correlation']}  "
              f"pyramids={r.get('pyramids')}{desc_flag}")
    for r in provisional:
        print(f"  [??]  {r['alpha_id']}  sharpe={r['sharpe']:.2f}  "
              f"ops={r['n_operators']}({','.join(r['operators'])})  "
              f"fields={r['n_fields']}({','.join(r['fields'])})  "
              f"pending={r['pending_gates']}")

    print("\n=== Not eligible (Sharpe/operator/field/server-gate FAIL) ===\n")
    not_eligible = [r for r in results if not r["eligible"]]
    print(f"  {len(not_eligible)} alpha(s). Re-run with --json for full detail "
          f"per alpha.")

    print("\nNote: no check name in the live payload obviously maps to a "
          "'robust-universe test' -- every check name/result an alpha "
          "actually carries is in --json output under all_checks. Confirm "
          "on each alpha's page in BRAIN before tagging it PowerPoolSelected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
