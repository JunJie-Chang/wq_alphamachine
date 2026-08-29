"""Command line interface.

    python -m alphamachine catalog
    python -m alphamachine scan   --budget 300
    python -m alphamachine refine --top 20
    python -m alphamachine select --n 10
    python -m alphamachine submit --n 3          # dry run by default
    python -m alphamachine submit --n 3 --live
    python -m alphamachine report
    python -m alphamachine run    --budget 300 --top 20 --n 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from .bandit import Bandit, arm_name
from .catalog import Catalog
from .client import BrainClient
from .config import AutoGates, RunConfig, SimSettings, Gates
from .priors import MODEL as MODEL_NAME
from .correlate import CorrelationBook, fetch_book, greedy_select
from .executor import Executor
from .generator import generate
from . import priors as P
from .metrics import Verdict, headroom, pnl_series, quality_verdict
from .refine import Refiner
from .store import Store
from .submit import Submitter


def _load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _setup(args) -> tuple[RunConfig, Store, BrainClient]:
    _load_env()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S")
    cfg = RunConfig(db_path=args.db, concurrency=args.concurrency)
    if args.max_submits is not None:
        cfg.max_submits_per_day = args.max_submits
    store = Store(cfg.db_path)
    client = BrainClient(cfg.email, cfg.password)
    client.authenticate()
    return cfg, store, client


def _base_settings(args) -> SimSettings:
    return SimSettings(region=args.region, universe=args.universe,
                       delay=args.delay)


# ----------------------------------------------------------------- commands
def cmd_catalog(args):
    cfg, store, client = _setup(args)
    cat = Catalog(client, cfg.cache_dir)
    fields = cat.fields(region=args.region, delay=args.delay,
                        universe=args.universe, refresh=args.refresh)
    usable = cat.usable(fields)
    themes = cat.by_theme(usable)
    print(f"\n{len(fields)} fields total, {len(usable)} usable "
          f"({args.region}/{args.universe}/delay={args.delay})\n")
    for t, fs in sorted(themes.items(), key=lambda kv: -len(kv[1])):
        print(f"  {t:<16} {len(fs):>6}  "
              f"{len(cat.by_dataset(fs)):>4} datasets")
    print()


def cmd_scan(args):
    cfg, store, client = _setup(args)
    cat = Catalog(client, cfg.cache_dir)
    fields = cat.usable(cat.fields(region=args.region, delay=args.delay,
                                   universe=args.universe))
    fields = cat.stratified_sample(fields, per_dataset=args.per_dataset)
    print(f"sampling from {len(fields)} fields across "
          f"{len(cat.by_dataset(fields))} datasets")

    ids = generate(store, fields, budget=args.budget,
                   base=_base_settings(args), seed=args.seed)
    if not ids:
        print("nothing new to scan (everything already simulated)")
        return

    ex = Executor(cfg, store)
    rc = ex.reclaim()          # harvest anything a previous crash left in flight
    if rc["reclaimed"] or rc["still_running"]:
        print(f"reclaimed {rc['reclaimed']} in-flight sims "
              f"({rc['still_running']} still running, {rc['lost']} lost)")
    bandit = Bandit(store, [], seed=args.seed)
    g = cfg.gates

    def on_result(cid, rec):
        row = store.candidate(cid)
        arm = arm_name(row["theme"], row["template"])
        sh = abs(rec.get("sharpe") or 0.0)
        passed = rec.get("status") == "OK" and sh >= g.scan_abs_sharpe
        bandit.update(arm, stage="scan", passed=passed, sharpe=sh)

    stats = ex.run(ids, on_result=on_result)
    survivors = store.sims_where(
        "s.status='OK' AND abs(s.sharpe) >= ? AND c.stage='scan'",
        (g.scan_abs_sharpe,))
    print(f"\nscan done: {stats}  survivors(|sharpe|>={g.scan_abs_sharpe}): "
          f"{len(survivors)}")


def cmd_refine(args):
    cfg, store, client = _setup(args)
    g = cfg.gates
    rows = store.sims_where(
        "s.status='OK' AND abs(s.sharpe) >= ? AND c.stage='scan' "
        "ORDER BY abs(s.sharpe) DESC LIMIT ?", (g.scan_abs_sharpe, args.top))
    if not rows:
        print("no scan survivors to refine -- run `scan` first")
        return
    ex = Executor(cfg, store)
    rc = ex.reclaim()
    if rc["reclaimed"]:
        print(f"reclaimed {rc['reclaimed']} in-flight sims from a previous run")
    print(f"refining {len(rows)} survivors")
    ref = Refiner(store, ex, g)
    bandit = Bandit(store, [], seed=args.seed)
    sub = Submitter(cfg, store, client)
    deep_budget = cfg.max_deep_checks_per_refine if cfg.deep_check_in_refine else 0

    for i, r in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {r['template']}/{r['theme']} on {r['field']}"
              f"  sharpe={r['sharpe']:.2f}")
        best_id, best = ref.ladder(r["id"])
        qv = quality_verdict(best, g)
        print(f"    -> sharpe={best.get('sharpe', 0):.2f} "
              f"fitness={best.get('fitness', 0):.2f} "
              f"turnover={best.get('turnover', 0):.3f} "
              f"| gate: {'PASS' if qv.ok else '; '.join(qv.reasons[:2])}")
        arm = arm_name(r["theme"], r["template"])
        bandit.update(arm, stage="gate", passed=qv.ok,
                      sharpe=abs(best.get("sharpe") or 0))

        # The deep gate is only informative for champions that got past the
        # metric gate -- below it the reward is already 0, so paying for the
        # extra calls would buy nothing.
        dv = None
        aid = best.get("alpha_id")
        if qv.ok and aid and deep_budget > 0:
            deep_budget -= 1
            dv = sub.deep_verdict(aid)
            if dv is None:
                # Inconclusive: a source did not answer. Teaching the bandit
                # here would charge the arm for our failed fetch.
                print("       deep: UNVERIFIED (could not fetch)")
            else:
                print(f"       deep: "
                      f"{'PASS' if dv.ok else '; '.join(dv.reasons[:2])}")
                bandit.update(arm, stage="deep", passed=dv.ok)
        store.record_gate_check(best_id, aid, r["expr"], best, qv, g, deep=dv)


def cmd_select(args):
    cfg, store, client = _setup(args)
    g = cfg.gates
    book = fetch_book(client, store)
    rows = store.sims_where(
        "s.status='OK' AND c.stage='gated' ORDER BY abs(s.sharpe) DESC LIMIT 200")
    sub = Submitter(cfg, store, client)
    cands = []
    qualified = 0            # cleared quality_verdict, regardless of PnL
    blocked_deep = []         # cleared the metrics but not the deep gate
    no_pnl = []               # qualified but PnL fetch came back empty
    for r in rows:
        rec = {k: r[k] for k in r.keys()}
        rec["checks"] = json.loads(r["checks"] or "[]")
        qv = quality_verdict(rec, g)
        aid = r["alpha_id"]
        # Same deep gate submit will apply. Selecting on metrics alone is how
        # a basket of three "passing" alphas got blocked three ways at submit.
        dv = sub.deep_verdict(aid) if (qv.ok and aid) else None
        store.record_gate_check(r["id"], aid, r["expr"], rec, qv, g, deep=dv)
        if not qv.ok:
            continue
        qualified += 1
        if dv is None:
            # Never select what we could not verify.
            blocked_deep.append(f"{aid} (UNVERIFIED -- deep check unavailable)")
            continue
        if not dv.ok:
            blocked_deep.append(f"{aid} ({dv.reasons[0] if dv.reasons else '?'})")
            continue
        cached = store.load_pnl(aid)
        if cached is None:
            d, v = pnl_series(client.alpha_pnl(aid))
            if not d:
                # A gate-passing alpha with no PnL is very likely a transient
                # API hiccup (records not yet available right after a fresh
                # login), not a real absence -- don't let it silently look
                # identical to "nothing passed the gate".
                no_pnl.append(aid)
                continue
            store.save_pnl(aid, d, v)
        else:
            d, v = cached
        cands.append({"alpha_id": aid, "quality": headroom(rec, g),
                      "dates": d, "pnl": v, "expr": r["expr"],
                      "sharpe": r["sharpe"], "fitness": r["fitness"],
                      "turnover": r["turnover"]})
    print(f"{qualified} metric-gate passing candidate(s); "
          f"{len(cands)} submittable with usable PnL; selecting {args.n}")
    if blocked_deep:
        print(f"  {len(blocked_deep)} blocked by the deep gate "
              f"(yearly consistency / PROD_CORRELATION):")
        for b in blocked_deep[:5]:
            print(f"      {b}")
    if no_pnl:
        print(f"  NOTE: {len(no_pnl)} cleared the gate but returned no PnL "
              f"data ({', '.join(no_pnl)}) -- likely transient, rerun select "
              f"to retry rather than assuming they are unsubmittable")
    picked = greedy_select(cands, book, max_corr=g.max_self_corr, n=args.n)
    store.put("selected_basket", [p["alpha_id"] for p in picked])
    print(f"\n{'alpha':<14}{'sharpe':>8}{'fitness':>9}{'turn':>7}"
          f"{'maxcorr':>9}  expression")
    for p in picked:
        print(f"{p['alpha_id']:<14}{p['sharpe']:>8.2f}{p['fitness']:>9.2f}"
              f"{p['turnover']:>7.3f}{p['max_corr']:>9.2f}  {p['expr'][:70]}")


def cmd_submit(args):
    cfg, store, client = _setup(args)
    basket = store.get("selected_basket", None)
    if not basket:
        # An empty basket means select ran and found nothing submittable --
        # a different situation from never having run it, and telling someone
        # to "run select" when they just did is how they end up rerunning it.
        print("select has not been run yet -- run `select` first" if basket is None
              else "select found nothing submittable -- nothing to submit. "
                   "Run `gatelog --passing` to see what is blocking candidates.")
        return
    book = fetch_book(client, store)
    sub = Submitter(cfg, store, client)

    if getattr(args, "auto", False):
        cfg.auto_submit = True
        res = sub.auto_submit_batch(basket[:args.n], book=book, live=args.live)
        ag = cfg.auto_gates
        print(f"\nauto tier: sharpe>={ag.sharpe} fitness>={ag.fitness} "
              f"turnover<={ag.turnover_max} self-corr<{ag.max_self_corr} "
              f"headroom>={ag.min_headroom}  (cap {cfg.max_auto_submits_per_day}/day)")
        for a in res["submitted"]:
            m = a["metrics"]
            print(f"\n  {'SUBMITTED' if args.live else 'WOULD SUBMIT'} {a['alpha_id']}"
                  f"  sharpe={m.get('sharpe')} fitness={m.get('fitness')} "
                  f"headroom={a['headroom']:.2f}")
        for a in res["queued_for_review"]:
            print(f"\n  REVIEW  {a['alpha_id']}  headroom={a['headroom']:.2f}")
            for r in a["reasons"][:3]:
                print(f"          {r}")
        for a in res["rejected"]:
            print(f"\n  REJECT  {a['alpha_id']}: {a['reasons'][0] if a['reasons'] else ''}")
        print(f"\n{len(res['submitted'])} auto, {len(res['queued_for_review'])} "
              f"need your eyes, {len(res['rejected'])} rejected")
        return

    for aid in basket[:args.n]:
        d = sub.submit(aid, dry_run=not args.live, book=book)
        tag = ("SUBMITTED" if d.get("submitted") else
               "WOULD SUBMIT" if d.get("ok") and not args.live else "BLOCKED")
        print(f"\n{aid}: {tag}")
        for k in ("platform", "quality", "consistency"):
            v = d.get(k)
            if v and not v["ok"]:
                print(f"   {k}: {'; '.join(v['reasons'][:3])}")
        if d.get("local_max_corr"):
            lc = d["local_max_corr"]
            print(f"   local max corr {lc['value']:.2f} vs {lc['against']}")
        if d.get("server_check", {}).get("fails"):
            print(f"   server check FAIL: {d['server_check']['fails']}")
        if d.get("final_status") and d["final_status"] != "ACTIVE":
            print(f"   final status {d['final_status']} -- NOT live: {d.get('reason')}")


def cmd_priors(args):
    """Classify each dataset with an LLM to seed the bandit."""
    cfg, store, client = _setup(args)
    cat = Catalog(client, cfg.cache_dir)
    fields = cat.usable(cat.fields(region=args.region, delay=args.delay,
                                   universe=args.universe))
    n_ds = len(cat.by_dataset(fields))
    if args.limit:
        n_ds = min(n_ds, args.limit)

    # Cost model: the taxonomy system prompt is identical across requests, so
    # it is cached; only the per-dataset field sample is fresh input.
    model = args.model or MODEL_NAME
    cache = args.cache or P.CACHE
    rates = {"claude-opus-5": (5.0, 25.0), "claude-fable-5": (10.0, 50.0),
             "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}
    rin, rout = rates.get(model, (5.0, 25.0))
    sys_tok, user_tok, out_tok = 900, 1600, 300
    batch = 0.5 if not args.no_batch else 1.0
    inp = (sys_tok * 0.1 + user_tok) * n_ds        # cached prefix at ~10%
    cost = (inp / 1e6 * rin + out_tok * n_ds / 1e6 * rout) * batch
    print(f"\n{n_ds} datasets x {model}"
          f"{' (Batch API, 50% off)' if batch < 1 else ''}")
    print(f"  ~{inp/1e6:.2f}M input + {out_tok*n_ds/1e6:.2f}M output")
    print(f"  estimated cost: ${cost:.2f}\n")
    if args.estimate:
        return
    have = P.load(cache)
    if have:
        print(f"already cached: {len(have)} datasets (these are skipped)")
    res = P.build(fields, limit=args.limit, use_batch=not args.no_batch,
                  cache_path=cache, model=model)
    if not res:
        print("no priors produced -- check credentials (see docs/README.md)")
        return
    pri = P.arm_priors(fields, cache)
    print(f"\npriors for {len(res)} datasets -> {len(pri)} arms\n")
    top = sorted(pri.items(), key=lambda kv: -kv[1][0] / (kv[1][0] + kv[1][1]))
    print(f"  {'arm':<36}{'prior':>7}")
    for arm, (a, b) in top[:15]:
        print(f"  {arm:<36}{a/(a+b):>7.2f}")


def cmd_report(args):
    cfg, store, client = _setup(args) if args.online else (
        RunConfig(db_path=args.db), Store(args.db), None)
    c = store.conn()
    n_cand = c.execute("SELECT COUNT(*) n FROM candidates").fetchone()["n"]
    n_sim = c.execute("SELECT COUNT(*) n FROM sims WHERE status='OK'").fetchone()["n"]
    n_err = c.execute("SELECT COUNT(*) n FROM sims WHERE status!='OK'").fetchone()["n"]
    n_sub = c.execute("SELECT COUNT(*) n FROM submissions "
                      "WHERE final_status='ACTIVE'").fetchone()["n"]
    n_fly = c.execute("SELECT COUNT(*) n FROM inflight").fetchone()["n"]
    print(f"\ncandidates {n_cand}   simulated {n_sim}   errors {n_err}   "
          f"in-flight {n_fly}   live submissions {n_sub}")
    if n_fly:
        print(f"  {n_fly} simulation(s) dispatched but unresolved -- the next "
              f"scan/refine run will reclaim them rather than re-pay for them")
    print("\narm                                trials   scan%   gate%   deep%"
          "   avg|sharpe|")
    b = Bandit(store, [r["arm"] for r in store.arms()])
    for name, trials, scan_r, gate_r, deep_r, avg in b.report()[:30]:
        print(f"  {name:<34}{trials:>6}{scan_r * 100:>7.1f}"
              f"{gate_r * 100:>8.1f}{deep_r * 100:>8.1f}{avg:>13.2f}")
    print()


def cmd_gatelog(args):
    """Answer "what can I actually submit right now", recomputed on the spot.

    `--passing` is a LIVE recheck, not a cached-flag lookup. Every OK
    simulation is re-run through quality_verdict() against whatever Gates are
    in config.py right now -- that is what keeps the answer honest when the
    thresholds move (see the 1.50 -> 1.80 -> 1.58 Sharpe history that prompted
    this table). Metric-passers then get the deep verdict, because clearing
    sharpe/fitness/turnover is NOT the same as being submittable: on the first
    run that shipped this table, all three "passing" candidates were blocked by
    submit -- two on one lucky year, one on PROD_CORRELATION.

    The deep verdict needs the network, so it reuses the deep_checks cache and
    only fetches what is missing or stale. `--offline` skips it entirely and
    reports those candidates as UNVERIFIED rather than quietly calling them
    submittable.
    """
    store = Store(args.db)
    g = Gates()
    if args.passing:
        sub, unverified = None, 0
        if not args.offline:
            cfg, store, client = _setup(args)
            g = cfg.gates
            sub = Submitter(cfg, store, client)
        for r in store.sims_where("s.status='OK'"):
            rec = {k: r[k] for k in r.keys()}
            rec["checks"] = json.loads(r["checks"] or "[]")
            qv = quality_verdict(rec, g)
            dv = None
            if qv.ok and r["alpha_id"]:
                if sub is not None:
                    dv = sub.deep_verdict(r["alpha_id"])
                else:
                    cached = store.load_deep(r["alpha_id"])
                    if cached and cached["ok"] is not None:
                        dv = Verdict(cached["ok"], cached["reasons"])
                if dv is None:
                    unverified += 1
            store.record_gate_check(r["id"], r["alpha_id"], r["expr"], rec,
                                    qv, g, deep=dv)
        rows = store.gate_passing_now(limit=args.limit)
        print(f"\n{len(rows)} candidate(s) submit-grade RIGHT NOW "
              f"(sharpe>={g.sharpe} fitness>={g.fitness} "
              f"turnover<={g.turnover_max}, plus yearly consistency and "
              f"PROD_CORRELATION -- freshly rechecked, not a cached flag)\n")
        if unverified:
            how = ("Rerun without --offline to check them."
                   if args.offline else
                   "BRAIN did not answer for them; rerun to retry.")
            print(f"  NOTE: {len(rows)} confirmed submittable, but {unverified} "
                  f"more cleared the metric gate with NO deep verdict -- "
                  f"unverified, not passing. {how}\n")
    else:
        rows = store.gate_history(limit=args.limit)
        print(f"\nlast {len(rows)} gate check(s) (most recent first)\n")
    print(f"{'when':<17}{'ok':<6}{'sharpe':>7}{'fitness':>9}{'turn':>7}"
          f"{'gate>=':>8}  reasons / expr")
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        ok = "PASS" if r["ok"] else "fail"
        tail = (r["expr"][:40] if r["ok"] else
               "; ".join(json.loads(r["reasons"] or "[]")[:2]))
        print(f"{when:<17}{ok:<6}{(r['sharpe'] or 0):>7.2f}"
              f"{(r['fitness'] or 0):>9.2f}{(r['turnover'] or 0):>7.3f}"
              f"{(r['gate_sharpe'] or 0):>8.2f}  {tail}")
    print()


def main(argv=None):
    p = argparse.ArgumentParser("alphamachine")
    p.add_argument("--db", default="data/machine.db")
    p.add_argument("--concurrency", type=int, default=3,
                   help="MUST match your account's simulation slot count")
    p.add_argument("--region", default="USA")
    p.add_argument("--universe", default="TOP3000")
    p.add_argument("--delay", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-submits", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("catalog"); s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("scan")
    s.add_argument("--budget", type=int, default=200)
    s.add_argument("--per-dataset", type=int, default=8)
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("refine"); s.add_argument("--top", type=int, default=20)
    s.set_defaults(fn=cmd_refine)

    s = sub.add_parser("select"); s.add_argument("--n", type=int, default=10)
    s.set_defaults(fn=cmd_select)

    s = sub.add_parser("submit")
    s.add_argument("--n", type=int, default=3)
    s.add_argument("--live", action="store_true",
                   help="actually submit; without this it is a dry run")
    s.add_argument("--auto", action="store_true",
                   help="unattended mode: send ONLY alphas clearing the "
                        "stricter auto tier; queue everything borderline")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("priors")
    s.add_argument("--limit", type=int, default=None,
                   help="classify only the first N datasets (for a trial run)")
    s.add_argument("--no-batch", action="store_true",
                   help="use synchronous calls instead of the Batch API")
    s.add_argument("--estimate", action="store_true",
                   help="print the cost estimate and exit without calling")
    s.add_argument("--model", default=None, help="override the classifier model")
    s.add_argument("--cache", default=None, help="write priors to this file")
    s.set_defaults(fn=cmd_priors)

    s = sub.add_parser("report")
    s.add_argument("--online", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("gatelog",
                       help="independent history of gate pass/fail decisions "
                            "(survives Gates threshold changes)")
    s.add_argument("--passing", action="store_true",
                   help="only candidates that are submittable right now")
    s.add_argument("--offline", action="store_true",
                   help="skip the deep verdict (no network); candidates "
                        "needing one are reported as unverified, not passing")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_gatelog)

    s = sub.add_parser("run")
    s.add_argument("--budget", type=int, default=200)
    s.add_argument("--per-dataset", type=int, default=8)
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--n", type=int, default=5)
    s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    return args.fn(args)


def cmd_run(args):
    """Full loop: scan -> refine -> select -> submit(dry by default)."""
    cmd_scan(args)
    cmd_refine(args)
    cmd_select(args)
    cmd_submit(args)
    cmd_report(argparse.Namespace(**{**vars(args), "online": False}))


if __name__ == "__main__":
    sys.exit(main())
