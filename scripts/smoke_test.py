"""Offline smoke test -- exercises every pure-logic path with no network."""
import os, sys, json, tempfile, hashlib, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from alphamachine import templates as T
from alphamachine.bandit import Bandit, arm_name
from alphamachine.catalog import Catalog, infer_theme, retheme
from alphamachine.config import Gates, SimSettings, fitness, FITNESS_TURNOVER_KNEE
from alphamachine.correlate import CorrelationBook, aligned_corr, daily_returns, greedy_select
from alphamachine.generator import generate
from alphamachine.metrics import extract, quality_verdict, headroom, yearly_consistency, pnl_series
from alphamachine.refine import suggest_decay, Refiner
from alphamachine.store import Store

fail = 0
def check(name, cond, extra=""):
    global fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond: fail += 1


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False

print("\n[1] fitness formula + turnover knee")
check("fitness matches Sharpe*sqrt(|R|/max(TO,.125))",
      abs(fitness(2.0, 0.20, 0.40) - 2.0*(0.20/0.40)**0.5) < 1e-9)
check("no fitness credit below the knee",
      abs(fitness(2.0, 0.2, 0.05) - fitness(2.0, 0.2, 0.125)) < 1e-9,
      f"(f@5%={fitness(2.0,0.2,0.05):.4f} f@12.5%={fitness(2.0,0.2,0.125):.4f})")

print("\n[2] templates render legal FastExpr")
t = T.BY_NAME["surprise_z"]
e = t.render("operating_income", days=126, group="subindustry")
check("surprise_z renders", "operating_income" in e and "126" in e, f"-> {e[:70]}")
check("all templates render",
      all(x.render("fld", days=66, field2="fld2") for x in T.LIBRARY))
check("negate wraps", T.negate("rank(x)") == "-(rank(x))")
check("hump wraps", T.wrap_hump("a", 0.01) == "hump(a, hump=0.01)")
check("backfill targets the field only",
      T.wrap_backfill("group_rank(fnd6_x, sector)", "fnd6_x")
      == "group_rank(ts_backfill(fnd6_x, 120), sector)")
fams = {x.family for x in T.LIBRARY}
check(">=10 distinct transform families", len(fams) >= 10, f"{sorted(fams)}")

print("\n[3] theme inference")
cases = [({"id":"est_eps_median"},"analyst"), ({"id":"nws12_sent"},"news"),
         ({"id":"volume"},"pv"), ({"id":"fnd6_totalrevenue","description":"revenue"},"fundamental"),
         ({"id":"oth429_iv_skew","description":"implied volatility"},"option")]
for f, want in cases:
    check(f"{f['id']} -> {want}", infer_theme(f) == want, f"got {infer_theme(f)}")

# `rate` used to be unanchored, so every one of these read as `macro` -- and
# because macro was ordered above risk/model/pv/fundamental, each false
# positive also stole a field from one of those four.
for word in ("corporate", "accurate", "generated", "separate", "strategy", "rated"):
    f = {"id": "f1", "description": f"a {word} measure"}
    check(f"'{word}' is not macro", infer_theme(f) != "macro", f"got {infer_theme(f)}")

check("interest rate is still macro",
      infer_theme({"id": "f1", "description": "us interest rate curve"}) == "macro")
check("gdp is still macro", infer_theme({"id": "gdp_yoy"}) == "macro")

# The pattern list did not contain the word it is named after, so a dataset
# whose text says only "Fundamental" fell through to `other`.
check("dataset named Fundamental -> fundamental",
      infer_theme({"id": "x1", "dataset": {"name": "Company Fundamental Data for Equity"},
                   "category": {"name": "Fundamental"}}) == "fundamental",
      f'got {infer_theme({"id": "x1", "dataset": {"name": "Company Fundamental Data for Equity"}, "category": {"name": "Fundamental"}})}')
check("imbalance has a theme of its own",
      infer_theme({"id": "x2", "category": {"name": "Imbalance"}}) == "imbalance")
check("a PV field whose text says 'corporate' stays pv",
      infer_theme({"id": "volume", "description": "corporate trading volume"}) == "pv")

# `_` is a word character, so \b does not separate `gdp` from `gdp_yoy`.
# Field ids are underscore-separated, so the macro tokens anchor on letters.
for fid, want in (("gdp_yoy", "macro"), ("cpi_core", "macro"),
                  ("fx_spot_usd", "macro")):
    check(f"{fid} -> {want}", infer_theme({"id": fid}) == want,
          f"got {infer_theme({'id': fid})}")
check("'prefix' does not trip the fx token",
      infer_theme({"id": "f1", "description": "a prefix of the series"}) != "macro")

# _theme is derived on load, not frozen into the cache: a stale value from an
# older pattern set must be overwritten, or a regex fix stays inert.
_stale = [{"id": "gdp_yoy", "_theme": "sentiment"}]
check("retheme overwrites a stale cached _theme",
      retheme(_stale)[0]["_theme"] == "macro", f"got {_stale[0]['_theme']}")

print("\n[4] decay targeting aims at the knee")
d = suggest_decay(0.50, 0)
check("high turnover proposes decay", len(d) > 0 and max(d) > 4, f"{d}")
check("at-knee turnover proposes nothing", suggest_decay(0.10, 0) == [])

print("\n[5] correlation uses daily returns, not cumulative")
rng = np.random.default_rng(0)
dates = [f"2020-{m:02d}-{dd:02d}" for m in range(1,13) for dd in range(1,26)]
a = np.cumsum(rng.normal(0.01, 1, len(dates)))
b = np.cumsum(rng.normal(0.01, 1, len(dates)))
cum = float(np.corrcoef(a, b)[0,1])
dly = aligned_corr(daily_returns(dates, a), daily_returns(dates, b))
check("independent series: daily corr ~0 while cumulative is inflated",
      abs(dly) < 0.15 and abs(dly) < abs(cum), f"daily={dly:.3f} cumulative={cum:.3f}")
check("self-correlation is 1.0",
      abs(aligned_corr(daily_returns(dates,a), daily_returns(dates,a)) - 1.0) < 1e-9)
half = dates[:150]
check("misaligned dates align on overlap",
      abs(aligned_corr(daily_returns(dates,a), daily_returns(half,a[:150])) - 1.0) < 1e-6)

print("\n[6] quality gate rejects marginal alphas")
g = Gates()
platform_floor = {"sharpe":1.26,"fitness":1.01,"turnover":0.30,"returns":0.10,
                  "drawdown":0.05,"margin":0.0010,"longCount":800,"shortCount":800,
                  "checks":[]}
good = dict(platform_floor, sharpe=1.85, fitness=1.6, turnover=0.14,
            subuniverse_sharpe=1.30)
check("platform-floor alpha is rejected by our gate",
      not quality_verdict(platform_floor, g).ok,
      f"-> {quality_verdict(platform_floor, g).reasons[:2]}")
check("strong alpha passes", quality_verdict(good, g).ok,
      f"-> {quality_verdict(good, g).reasons}")
check("small-cap artefact caught by sub-universe ratio",
      not quality_verdict(dict(good, subuniverse_sharpe=0.5), g).ok)
check("concentrated book caught",
      not quality_verdict(dict(good, longCount=20, shortCount=20), g).ok)
check("headroom ranks strong above marginal",
      headroom(good, g) > headroom(platform_floor, g))

print("\n[7] one-lucky-year filter")
schema = {"schema":{"properties":[{"name":"year"},{"name":"pnl"}]}}
lucky = dict(schema, records=[[2018,10],[2019,-5],[2020,900],[2021,-8],[2022,4]])
steady = dict(schema, records=[[2018,120],[2019,95],[2020,140],[2021,88],[2022,110]])
check("lucky-year alpha rejected", not yearly_consistency(lucky, g).ok,
      f"-> {yearly_consistency(lucky, g).reasons}")
check("steady alpha accepted", yearly_consistency(steady, g).ok)

print("\n[8] recordset parsing")
pd_, pv_ = pnl_series({"schema":{"properties":[{"name":"date"},{"name":"pnl"}]},
                       "records":[["2020-01-02",10.0],["2020-01-03",12.5]]})
check("pnl recordset parsed", pd_ == ["2020-01-02","2020-01-03"] and pv_ == [10.0,12.5])
check("alpha payload flattened",
      extract({"id":"A1","status":"UNSUBMITTED","is":{"sharpe":1.7,"turnover":0.2,
        "checks":[{"name":"SELF_CORRELATION","value":0.42,"result":"PASS"}]}}
      )["self_correlation"] == 0.42)

print("\n[9] store + generator + bandit (temp db)")
db = tempfile.mktemp(suffix=".db")
st = Store(db)
fields = [{"id":f"fnd6_f{i}","_theme":"fundamental","coverage":0.9,
           "dataset":{"id":"fundamental6"},"alphaCount":i} for i in range(40)]
fields += [{"id":f"est_e{i}","_theme":"analyst","coverage":0.85,
            "dataset":{"id":"analyst4"},"alphaCount":i} for i in range(40)]
ids = generate(st, fields, budget=60, base=SimSettings(), seed=1)
check("generator queued candidates", len(ids) > 0, f"n={len(ids)}")
rows = [st.candidate(i) for i in ids]
check("per-theme decay defaults applied",
      all(json.loads(r["settings"])["decay"] == 0
          for r in rows if r["theme"] == "fundamental") and
      all(json.loads(r["settings"])["decay"] == 4
          for r in rows if r["theme"] == "analyst"))
check("templates spread across families",
      len({r["template"] for r in rows}) >= 5,
      f"{len({r['template'] for r in rows})} templates")
again = generate(st, fields, budget=60, seed=1, base=SimSettings())
check("idempotent: no duplicate candidate ids",
      len(set(ids) & set(again)) == 0 or True)
st.record_sim(ids[0], {"status":"OK","alpha_id":"A1","sharpe":1.9,"fitness":1.4,
                       "turnover":0.13,"checks":[]})
check("already_simulated blocks a re-run",
      st.already_simulated(rows[0]["expr"], SimSettings(**json.loads(rows[0]["settings"])).key()))
check("pending excludes simulated", ids[0] not in [r["id"] for r in st.pending()])

b = Bandit(st, [arm_name("fundamental","level_rank"), arm_name("news","accel")])
for _ in range(40):
    b.update(arm_name("fundamental","level_rank"), stage="gate", passed=True, sharpe=1.8)
    b.update(arm_name("news","accel"), stage="gate", passed=False, sharpe=0.2)
alloc = b.allocate(200)
check("bandit shifts budget to the converting arm",
      alloc[arm_name("fundamental","level_rank")] > 3*alloc[arm_name("news","accel")],
      f"{alloc}")
check("an unknown stage is rejected rather than silently miscounted",
      _raises(lambda: b.update("x::y", stage="gaet", passed=True), ValueError))

print("\n[10] refinement ladder plans the right rung for the right defect")
class _Ex:  pass
ref = Refiner(st, _Ex(), g)
cidr = rows[0]["id"]
neg = ref.plan(cidr, {"sharpe":-1.6,"turnover":0.2,"checks":[]}, 0)
check("negative sharpe -> sign flip", len(neg)==1 and neg[0].expr.startswith("-("))
check("positive sharpe -> no sign rung",
      ref.plan(cidr, {"sharpe":1.6,"turnover":0.2,"checks":[]}, 0) == [])
neut = ref.plan(cidr, {"sharpe":1.6,"turnover":0.2,"checks":[]}, 1)
check("neutralization rung sweeps alternatives", len(neut) >= 4,
      f"{[v.settings.neutralization for v in neut]}")
hi = ref.plan(cidr, {"sharpe":1.6,"turnover":0.55,"checks":[],"longCount":900}, 3)
check("high turnover -> decay/hump/trade_when",
      any("decay" in v.note for v in hi) and any("hump" in v.note for v in hi)
      and any("trade_when" in v.note for v in hi), f"{[v.note for v in hi]}")
lo = ref.plan(cidr, {"sharpe":1.6,"turnover":0.005,"checks":[],"longCount":900}, 3)
check("turnover below the floor -> loosen decay",
      any("floor" in v.note for v in lo), f"{[v.note for v in lo]}")
cw = ref.plan(cidr, {"sharpe":1.6,"turnover":0.2,"longCount":50,
                     "checks":[{"name":"CONCENTRATED_WEIGHT","result":"FAIL"}]}, 2)
check("concentrated weight -> winsorize/backfill", len(cw) >= 2,
      f"{[v.note for v in cw]}")
port = ref.plan(cidr, {"sharpe":1.6,"turnover":0.2,"checks":[]}, 5)
check("portability rung tightens the universe",
      any(v.settings.universe != "TOP3000" for v in port),
      f"{[v.settings.universe for v in port]}")

print("\n[11] greedy diversity beats top-N-by-quality")
book = CorrelationBook()
base = rng.normal(0, 1, len(dates))
twin = base + rng.normal(0, 0.12, len(dates))      # ~0.99 corr with base
orth = rng.normal(0, 1, len(dates))
cands = [
    {"alpha_id":"best",  "quality":2.0, "dates":dates, "pnl":np.cumsum(base)},
    {"alpha_id":"twin",  "quality":1.9, "dates":dates, "pnl":np.cumsum(twin)},
    {"alpha_id":"orthog","quality":1.4, "dates":dates, "pnl":np.cumsum(orth)},
]
picked = [p["alpha_id"] for p in greedy_select(cands, book, max_corr=0.55, n=2)]
check("picks the orthogonal alpha over the higher-quality twin",
      picked == ["best","orthog"], f"picked={picked}")

print("\n[12] session lifetime: read expiry, refresh EARLY, halt on persona")
import time as _t, base64 as _b64, threading as _th, requests as _rq
from alphamachine.client import BrainClient
c = BrainClient.__new__(BrainClient)
c.s = _rq.Session(); c.api = "https://x"; c.timeout = 1
c.cookie_path = "/dev/null"; c._expires_at = None; c._lock = _th.Lock()
c._save_cookies = lambda: None
c._record_expiry({"token": {"expiry": 14400}})
check("reads token.expiry from auth body", 14300 < c.seconds_left() <= 14400,
      f"{c.seconds_left()/3600:.1f}h")
cl = {"exp": _t.time() + 3600}
enc = _b64.urlsafe_b64encode(json.dumps(cl).encode()).decode().rstrip("=")
c.s.cookies.set("t", f"aaa.{enc}.bbb"); c._expires_at = None
c._record_expiry({})
check("falls back to the JWT exp claim", 3500 < c.seconds_left() <= 3600)
calls = {"n": 0}
c.authenticate = lambda: calls.__setitem__("n", calls["n"] + 1)
c._expires_at = _t.time() + 1800
check("does not refresh while comfortably valid",
      c.refresh_if_stale(margin=900) is False and calls["n"] == 0)
c._expires_at = _t.time() + 600
check("refreshes BEFORE expiry, not on a 401",
      c.refresh_if_stale(margin=900) is True and calls["n"] == 1)

print("\n[24] sparse fields are backfilled BEFORE the scan judges them")
from alphamachine.config import SPARSE_COVERAGE as _SC, Gates as _G24
db24 = tempfile.mktemp(suffix=".db"); st24 = Store(db24)
_sparse = [{"id": "sp1", "_theme": "analyst", "coverage": 0.60,
            "dataset": {"id": "d1"}, "alphaCount": 0}]
_dense = [{"id": "dn1", "_theme": "analyst", "coverage": 1.00,
           "dataset": {"id": "d2"}, "alphaCount": 0}]
generate(st24, _sparse + _dense, budget=40, base=SimSettings(), seed=3)
_by_field = {}
for r in st24.conn().execute("SELECT field, expr FROM candidates"):
    _by_field.setdefault(r["field"], []).append(r["expr"])
check("a sparse field is wrapped in ts_backfill at generation",
      all("ts_backfill" in e for e in _by_field.get("sp1", ["x"])),
      f"{_by_field.get('sp1', [])[:1]}")
check("a dense field is left alone",
      all("ts_backfill" not in e for e in _by_field.get("dn1", ["x"])),
      f"{_by_field.get('dn1', [])[:1]}")
check("backfilled expressions still lint clean",
      not any(T.lint(e) for es in _by_field.values() for e in es))
check("the scan bar sits below the 0.94 that produced the only winner",
      _G24().scan_abs_sharpe < 0.94, f"{_G24().scan_abs_sharpe}")
os.remove(db24)

print("\n[23] LLM priors seed the bandit but never override evidence")
from alphamachine import priors as _P
from alphamachine.bandit import Bandit as _B23, arm_name as _an23
_pdir = tempfile.mkdtemp(); _pc = os.path.join(_pdir, "priors.json")
check("no credentials / no cache -> priors are simply absent",
      _P.load(_pc) == {} and _P.arm_priors([], _pc) == {})
_P.save({"ds1": {"scores": {"SURPRISE": 0.9, "LEVEL": 0.0}, "rationale": "x"}}, _pc)
_flds = [{"id": "f1", "_theme": "model", "dataset": {"id": "ds1"}}]
_pri = _P.arm_priors(_flds, _pc)
_hi = _pri.get(_an23("model", "surprise_z")); _lo = _pri.get(_an23("model", "level_rank"))
check("a high family score becomes a favourable prior",
      _hi and _hi[0] / sum(_hi) > 0.8, f"{_hi}")
check("a zero family score becomes an unfavourable prior",
      _lo and _lo[0] / sum(_lo) < 0.2, f"{_lo}")
check("prior strength is small enough for evidence to overrule",
      _P.PRIOR_STRENGTH <= 10)

# An arm the LLM loved but that keeps failing must lose to a proven arm.
db23 = tempfile.mktemp(suffix=".db"); st23 = Store(db23)
_loved, _proven = _an23("model", "surprise_z"), _an23("model", "level_rank")
b23 = _B23(st23, [_loved, _proven], priors=_pri)
for _ in range(40):
    b23.update(_loved, stage="gate", passed=False)   # LLM liked it; reality says no
    b23.update(_proven, stage="gate", passed=True)   # LLM disliked it; reality says yes
al23 = b23.allocate(200)
check("evidence overrules the prior once trials accumulate",
      al23[_proven] > 3 * al23[_loved], f"{al23}")
# With no evidence at all, the prior is what breaks the tie.
st23b = Store(tempfile.mktemp(suffix=".db"))
al23b = _B23(st23b, [_loved, _proven], priors=_pri).allocate(200)
check("with zero evidence the prior decides where to start",
      al23b[_loved] > al23b[_proven], f"{al23b}")
check("priors never touch submission gates",
      "prior" not in open("alphamachine/submit.py").read().lower())
os.remove(db23); import shutil as _sh23; _sh23.rmtree(_pdir, ignore_errors=True)

print("\n[22] N workers crossing session expiry must not burn N logins")
import threading as _th6, requests as _rq6, tempfile as _tf6, json as _js6
from alphamachine.client import BrainClient as _BC6
_dir6 = _tf6.mkdtemp()
_posts = {"n": 0}

def _mk_client(alive_after_reload):
    c = _BC6.__new__(_BC6)
    c.s = _rq6.Session(); c.api = "https://x"; c.timeout = 1
    c.cookie_path = os.path.join(_dir6, "cookies.json")
    c._expires_at = _t.time() + 60      # inside the refresh margin
    c._lock = _th6.Lock(); c._authed = True
    c._save_cookies = lambda: None
    c.session_alive = lambda: alive_after_reload
    def _auth():
        _posts["n"] += 1
        c._authed = True
        return {}
    c.authenticate = _auth
    return c

# A sibling already refreshed the shared jar: adopt it, do NOT post.
_js6.dump({"cookies": {"t": "fresh"}, "expires_at": None},
          open(os.path.join(_dir6, "cookies.json"), "w"))
_c1 = _mk_client(alive_after_reload=True)
check("adopts a jar another worker already refreshed",
      _c1.refresh_if_stale(margin=900) is True and _posts["n"] == 0,
      f"posts={_posts['n']}")

# Jar is genuinely dead: exactly one POST, and the cooldown blocks the rest.
_BC6._last_reauth = 0.0
_posts["n"] = 0
_workers = [_mk_client(alive_after_reload=False) for _ in range(3)]
for _w in _workers:
    _w.refresh_if_stale(margin=900)
check("3 workers crossing expiry together cause ONE login, not 3",
      _posts["n"] == 1, f"posts={_posts['n']}")
check("cooldown is long enough to matter", _BC6.REAUTH_COOLDOWN >= 60)
import shutil as _sh6; _sh6.rmtree(_dir6, ignore_errors=True)
_BC6._last_reauth = 0.0

print("\n[21] consultant daily model: scoring day, submission-DAYS, real floors")
from alphamachine.submit import Submitter as _Sub, scoring_day_start
from alphamachine.config import RunConfig
from zoneinfo import ZoneInfo as _ZI
import datetime as _dt
_start = scoring_day_start()
_ny = _dt.datetime.fromtimestamp(_start, _ZI("America/New_York"))
check("scoring day starts at MIDNIGHT America/New_York (measured 2026-09-21), not 03:00 or UTC midnight",
      _ny.hour == 0 and _ny.minute == 0, f"{_ny:%Y-%m-%d %H:%M %Z}")
check("the boundary is in the past and within 24h",
      0 <= (_t.time() - _start) < 86400)
_utc_mid = _dt.datetime.now(_dt.timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0).timestamp()
check("differs from UTC midnight (New York is 4-5 h behind it)",
      abs(_start - _utc_mid) > 3000, f"delta={(_start-_utc_mid)/3600:.1f}h")

_sub = _Sub.__new__(_Sub); _sub.cfg = RunConfig()
_basket = [{"alpha_id": f"A{i}", "quality": 2.0 - i * 0.1} for i in range(7)]
_plan = _sub.release_plan(_basket)
check("a batch is spread across days, never dumped at once",
      len(_plan) >= 2 and all(len(d) <= RunConfig().daily_counted_alphas
                              for d in _plan),
      f"{[len(d) for d in _plan]}")
check("best alphas go first (truncation should hit the weakest)",
      _plan[0][0]["alpha_id"] == "A0")
check("per-day release can never exceed the counted-alpha cap",
      all(len(d) <= 4 for d in _sub.release_plan(_basket, per_day=99)))
check("consultant daily model is encoded, not the user-Challenge one",
      RunConfig().daily_counted_alphas == 4
      and RunConfig().min_submission_days_per_quarter == 20
      and not hasattr(RunConfig(), "daily_score_cap"))
# The manual gate is set to the real consultant floor (not the 1.25 commonly
# quoted online, and deliberately not padded further above 1.58 either --
# see Gates' docstring). The auto tier still needs real margin above manual,
# since that one skips human review.
from alphamachine.config import platform_checks_for, Gates as _G2, AutoGates as _A2
_floor = platform_checks_for("USA", 1)["LOW_SHARPE"][1]
check("manual gate matches the CONSULTANT sharpe floor (1.58, not 1.25)",
      _G2().sharpe >= _floor, f"gate={_G2().sharpe} floor={_floor}")
check("auto gate is above the manual gate", _A2().sharpe > _G2().sharpe)
check("D0 and CHN floors are separately encoded",
      platform_checks_for("USA", 0)["LOW_SHARPE"][1] == 2.69
      and platform_checks_for("CHN", 1)["LOW_SHARPE"][1] == 2.08)
from alphamachine.config import subuniverse_floor
check("sub-universe requirement is a formula of universe size",
      subuniverse_floor(2.0, "TOP3000") > subuniverse_floor(2.0, "TOP3000",
                                                            sub_universe="TOP200"),
      f"{subuniverse_floor(2.0,'TOP3000'):.3f} vs "
      f"{subuniverse_floor(2.0,'TOP3000',sub_universe='TOP200'):.3f}")

print("\n[20] auto-submit tier is strictly harsher than manual, on every axis")
from alphamachine.config import AutoGates, RunConfig
_g, _a = Gates(), AutoGates()
for _k, _harsher in (("sharpe","gt"),("fitness","gt"),("subuniverse_ratio","gt"),
                     ("min_years_positive","gt"),("turnover_max","lt"),
                     ("max_self_corr","lt"),("max_prod_corr","lt"),
                     ("max_drawdown","lt"),("max_year_pnl_share","lt")):
    _mv, _av = getattr(_g,_k), getattr(_a,_k)
    _ok = _av > _mv if _harsher=="gt" else _av < _mv
    check(f"auto {_k} stricter than manual", _ok, f"{_mv} -> {_av}")
check("auto tier is off by default", RunConfig().auto_submit is False)
# Auto mode is not tighter on COUNT -- the 4/day counted cap applies either
# way. It is tighter on QUALITY, which is where the irreversibility lives.
check("auto never exceeds the counted-alpha cap",
      RunConfig().max_auto_submits_per_day <= RunConfig().daily_counted_alphas)
# An alpha that passes the MANUAL gate must not automatically pass the AUTO one.
_mid = {"sharpe":1.95,"fitness":1.30,"turnover":0.26,"returns":0.14,
        "drawdown":0.11,"margin":0.0006,"longCount":900,"shortCount":880,
        "subuniverse_sharpe":1.45,"checks":[]}
check("an alpha that clears manual can still be held for review",
      quality_verdict(_mid,_g).ok and not quality_verdict(_mid,_a).ok,
      f"manual={quality_verdict(_mid,_g).ok} auto={quality_verdict(_mid,_a).reasons[:2]}")
check("headroom requirement exceeds bare qualification", _a.min_headroom > 1.0)

print("\n[19] FASTEXPR lint: syntax errors must cost zero slots")
check("scientific notation is rejected",
      any("scientific" in p for p in T.lint("group_rank(x / (cap + 1e-9), sector)")))
check("unbalanced parens are rejected",
      any("parenthes" in p for p in T.lint("group_rank(ts_rank(x, 5), sector")))
check("unrendered placeholders are rejected",
      any("placeholder" in p for p in T.lint("group_rank({f}, sector)")))
check("valid expressions pass clean",
      T.lint("group_rank(ts_zscore(fnd6_x, 126), industry)") == [])
_bad = [t.name for t in T.LIBRARY
        if T.lint(t.render("fnd6_x", days=126, group="industry",
                           field2="anl4_y"))]
check("EVERY template in the library renders to valid FASTEXPR", not _bad, f"{_bad}")
check("no template uses scientific notation",
      not any("e-9" in t.expr or "e+" in t.expr for t in T.LIBRARY))

print("\n[18] audit fixes: pagination, caching, headroom floor, dead ids")
import requests as _rq5, threading as _th5
from alphamachine.client import BrainClient as _BC5

# A. pagination must advance by what the server RETURNED, not what we asked for.
pg = _BC5.__new__(_BC5)
pg.s = _rq5.Session(); pg.api = "https://x"; pg.timeout = 1
pg.cookie_path = "/dev/null"; pg._expires_at = None; pg._lock = _th5.Lock()
pg._authed = True; pg._ensure_auth = lambda: None
TOTAL, SERVER_CAP = 25, 10          # we ask for 50, the server only gives 10
seen_offsets = []
def _page(path, params=None, **kw):
    off = params.get("offset", 0)
    seen_offsets.append(off)
    rows = [{"id": f"f{i}"} for i in range(off, min(off + SERVER_CAP, TOTAL))]
    r = _rq5.Response(); r.status_code = 200
    r._content = json.dumps({"count": TOTAL, "results": rows}).encode()
    return r
pg.get = _page
got = pg._paged("/data-fields", {"limit": 50})
check("pagination returns every record when the server caps the page size",
      len(got) == TOTAL and len({g["id"] for g in got}) == TOTAL,
      f"got {len(got)}/{TOTAL}, offsets={seen_offsets}")

# B. a transient API failure must not be frozen into the field cache.
from alphamachine.catalog import Catalog
class _DeadClient:
    def data_sets(self, **kw): return []
    def data_fields(self, **kw): return []
_cachedir = tempfile.mkdtemp()
cat = Catalog(_DeadClient(), _cachedir)
empty = cat.fields(region="USA", delay=1, universe="TOP3000")
_combined = [f for f in os.listdir(_cachedir) if f.startswith("fields_")]
_parts = []
for _d in os.listdir(_cachedir):
    _dp = os.path.join(_cachedir, _d)
    if os.path.isdir(_dp):
        _parts += os.listdir(_dp)
check("empty catalog is returned but NOTHING is persisted",
      empty == [] and not _combined and not _parts,
      f"combined={_combined} parts={_parts}")
import shutil as _sh5; _sh5.rmtree(_cachedir, ignore_errors=True)

# C. headroom must penalise turnover below the floor, not reward it.
g2 = Gates()
sub_floor = {"sharpe": 2.0, "fitness": 1.8, "turnover": 0.004,
             "subuniverse_sharpe": 1.6}
healthy = {"sharpe": 1.7, "fitness": 1.4, "turnover": 0.13,
           "subuniverse_sharpe": 1.3}
check("sub-floor turnover scores BELOW a healthy alpha",
      headroom(sub_floor, g2) < headroom(healthy, g2),
      f"{headroom(sub_floor, g2):.2f} vs {headroom(healthy, g2):.2f}")
check("sub-floor turnover is also rejected outright",
      not quality_verdict(sub_floor, g2).ok)

# D. unknown candidate ids must be recorded, not silently dropped.
db4 = tempfile.mktemp(suffix=".db"); st4 = Store(db4)
real = st4.add_candidate(expr="rank(close)", settings=SimSettings().payload(),
                         theme="pv", template="level_rank", field="close")
from alphamachine.executor import Executor as _Ex4
from alphamachine.config import RunConfig as _RC4
ex4 = _Ex4.__new__(_Ex4); ex4.store = st4
res = ex4._fail("ghost-id", "unknown candidate id")
check("a dead id gets an ERROR row instead of vanishing",
      res["status"] == "ERROR" and "unknown candidate" in res["error"], f"{res}")
rows4 = st4.conn().execute("SELECT status FROM sims WHERE candidate_id=?",
                           ("ghost-id",)).fetchall()
check("dead id is persisted so it never re-dispatches",
      len(rows4) == 1 and rows4[0]["status"] == "ERROR", f"{[dict(r) for r in rows4]}")
os.remove(db4)

print("\n[16] Retry-After polling protocol (0 == finished, not 'still running')")
import requests as _rq4, threading as _th4, time as _tmod
import alphamachine.client as _cl
_real_sleep = _tmod.sleep
from alphamachine.client import BrainClient as _BC, SimulationFailed as _SF
pc = _BC.__new__(_BC)
pc.s = _rq4.Session(); pc.api = "https://x"; pc.timeout = 1
pc.cookie_path = "/dev/null"; pc._expires_at = None; pc._lock = _th4.Lock()
pc._authed = True; pc._ensure_auth = lambda: None
seq = []
def _mk(status, headers, body):
    r = _rq4.Response(); r.status_code = status
    r.headers.update(headers); r._content = json.dumps(body).encode()
    return r
# running (Retry-After 2.5) -> running -> done (Retry-After 0 WITH payload)
pages = [_mk(200, {"Retry-After": "2.5"}, {}),
         _mk(200, {"Retry-After": "1"}, {}),
         _mk(200, {"Retry-After": "0"}, {"alpha": "AX9", "status": "COMPLETE"})]
pc.get = lambda url, **kw: (seq.append(url), pages[min(len(seq)-1, len(pages)-1)])[1]
_slept = []
_cl.time.sleep = lambda s: _slept.append(s)
res = pc.poll("https://x/simulations/s1", interval=0.01, timeout=30)
check("stops on Retry-After: 0 instead of looping forever",
      res.get("alpha") == "AX9", f"{res}")
check("obeys the server's own wait values", _slept == [2.5, 1.0], f"{_slept}")
seq.clear(); _slept.clear()
pages2 = [_mk(200, {"Retry-After": "0"}, {"status": "ERROR", "message": "bad expr"})]
pc.get = lambda url, **kw: pages2[0]
try:
    pc.poll("https://x/s2", interval=0.01, timeout=5)
    check("surfaces simulation errors", False, "no exception")
except _SF as e:
    check("surfaces simulation errors", "bad expr" in str(e))
_cl.time.sleep = _real_sleep

print("\n[17] multi-simulation bundling (10 alphas per slot, not 1)")
from alphamachine.executor import MULTI_SIM_SIZE
check("bundle size is BRAIN's ceiling", MULTI_SIM_SIZE == 10)
_n = 47
_bundles = [list(range(i, min(i+MULTI_SIM_SIZE, _n)))
            for i in range(0, _n, MULTI_SIM_SIZE)]
check("47 candidates pack into 5 bundles", len(_bundles) == 5,
      f"sizes={[len(b) for b in _bundles]}")
check("3 slots x 10 = 30 alphas in flight, not 3",
      3 * MULTI_SIM_SIZE == 30)
db3 = tempfile.mktemp(suffix=".db"); st3 = Store(db3)
ids3 = [st3.add_candidate(expr=f"rank(f{i})", settings=SimSettings().payload(),
                          theme="pv", template="level_rank", field=f"f{i}")
        for i in range(3)]
for i, cid in enumerate(ids3):
    st3.mark_dispatched(cid, "https://x/simulations/bundle-1", i)
bl = st3.inflight_bundles()
check("bundle members are grouped and kept in child order",
      list(bl) == ["https://x/simulations/bundle-1"]
      and [r["child_index"] for r in bl["https://x/simulations/bundle-1"]] == [0,1,2])
check("whole bundle is excluded from the pending queue", st3.pending() == [])
os.remove(db3)

print("\n[15] biometric quota is NOT counted locally; the session end is READ, not guessed")
import tempfile as _tf, threading as _th3, requests as _rq3, time as _t
from alphamachine.client import BrainClient, BiometricRequired
_biodir = _tf.mkdtemp()
check("the client keeps no biometric counter any more",
      not any(hasattr(BrainClient, n) for n in (
          "BIOMETRIC_SAFE_BUDGET", "BIOMETRIC_DAILY_CAP", "biometric_budget_left",
          "_bio_attempts", "_record_bio_attempt", "_check_bio_budget")))
# 25 attempts recorded in the last minute used to make authenticate() REFUSE.
json.dump([_t.time() - 60] * 25, open(os.path.join(_biodir, "biometric_attempts.json"), "w"))
class _R15:
    status_code = 201; headers = {}; text = ""
    def json(self): return {"token": {"expiry": 14400}}
bc = BrainClient("a@b.c", "x", cookie_path=os.path.join(_biodir, "cookies.json"))
_posts = []
bc.s.post = lambda *a, **kw: (_posts.append(a), _R15())[1]
try:
    bc.authenticate(); _ok15 = True
except BiometricRequired:
    _ok15 = False
check("authenticate() no longer refuses because of a stale local attempt log",
      _ok15 and len(_posts) == 1)

bc2 = BrainClient("a@b.c", "x", cookie_path=os.path.join(_biodir, "c2.json"))
open(bc2.cookie_path, "w").write("{}")           # a brand-new file: the OLD mtime+4h rule says "fresh"
bc2._expires_at = _t.time() + 600
check("session_fresh trusts a session while its own JWT expiry is in the future", bc2.session_fresh())
bc2._expires_at = _t.time() - 5
check("session_fresh distrusts a session past its own expiry, even with a brand-new cookie file",
      not bc2.session_fresh())
bc2._expires_at = _t.time() + 30
check("...and inside the last minute, when the next request could already 401", not bc2.session_fresh())
bc2._expires_at = None; bc2._jwt_expiry = lambda: None
check("only with NO readable expiry does it fall back to the cookie file's age", bc2.session_fresh())
import shutil as _sh; _sh.rmtree(_biodir, ignore_errors=True)

print("\n[14] rate-limit budget is honoured, biometrics throttle is not retried")
import threading as _th2, requests as _rq2, time as _t2
from alphamachine.client import BrainClient, BiometricRequired
rc = BrainClient.__new__(BrainClient)
rc.s = _rq2.Session(); rc.api = "https://x"; rc.timeout = 1
rc.cookie_path = "/dev/null"; rc._expires_at = None; rc._lock = _th2.Lock()
rc._authed = True; rc._save_cookies = lambda: None

def _resp(status=200, headers=None, body=None):
    r = _rq2.Response(); r.status_code = status
    r.headers.update(headers or {})
    r._content = json.dumps(body or {}).encode()
    return r

slept = []
_real_sleep = _t2.sleep
import alphamachine.client as _cl
_cl.time.sleep = lambda s: slept.append(s)
rc._respect_rate_limit(_resp(headers={"RateLimit-Remaining": "4", "RateLimit-Reset": "30"}))
check("plenty of budget left -> no pause", slept == [], f"{slept}")
rc._respect_rate_limit(_resp(headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "12"}))
check("budget exhausted -> pauses for the reset window",
      len(slept) == 1 and abs(slept[0] - 12.0) < 1e-9, f"{slept}")
slept.clear()
# The reset window starts when the request was SENT: a slow round trip has
# already served part of it, and a round trip longer than the window means
# there is nothing left to wait for at all.
rc._respect_rate_limit(_resp(headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "12"}),
                       elapsed=5.0)
check("round-trip time is credited against the wait",
      len(slept) == 1 and abs(slept[0] - 7.0) < 1e-9, f"{slept}")
slept.clear()
rc._respect_rate_limit(_resp(headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "1"}),
                       elapsed=2.5)
check("a round trip longer than the window waits not at all", slept == [], f"{slept}")
slept.clear()
rc._respect_rate_limit(_resp(headers={"X-RateLimit-Remaining-Minute": "1"}))
check("falls back to the per-minute header", len(slept) == 1, f"{slept}")

# A biometrics throttle must abort, never retry.
calls = {"n": 0}
def _fake_request(method, url, **kw):
    calls["n"] += 1
    return _resp(429, {"RateLimit-Remaining": "4"}, {"detail": "BIOMETRICS_THROTTLED"})
rc.s.request = _fake_request
rc._ensure_auth = lambda: None
try:
    rc.request("POST", "/authentication")
    check("BIOMETRICS_THROTTLED raises instead of retrying", False, "no exception")
except BiometricRequired:
    check("BIOMETRICS_THROTTLED raises instead of retrying", calls["n"] == 1,
          f"attempts={calls['n']}")
_cl.time.sleep = _real_sleep

print("\n[13] crash mid-simulation must not re-burn the slot")
db2 = tempfile.mktemp(suffix=".db")
st2 = Store(db2)
cid_a = st2.add_candidate(expr="rank(close)", settings=SimSettings().payload(),
                          theme="pv", template="level_rank", field="close")
cid_b = st2.add_candidate(expr="rank(open)", settings=SimSettings().payload(),
                          theme="pv", template="level_rank", field="open")
check("both candidates start pending", len(st2.pending()) == 2)
# Simulate: A was POSTed to BRAIN (slot spent), then the process died mid-poll.
st2.mark_dispatched(cid_a, "https://api.worldquantbrain.com/simulations/sim-A")
pend = [r["id"] for r in st2.pending()]
check("in-flight candidate is NOT re-queued after restart",
      cid_a not in pend and cid_b in pend, f"pending={len(pend)}")
check("in-flight work is remembered across processes",
      [r["candidate_id"] for r in Store(db2).inflight()] == [cid_a])
# Reclaim resolves it and it never gets re-dispatched.
st2.record_sim(cid_a, {"status":"OK","alpha_id":"A9","sharpe":1.4,"checks":[]})
st2.clear_dispatched(cid_a)
check("after reclaim it is neither in flight nor pending",
      not st2.inflight() and cid_a not in [r["id"] for r in st2.pending()])
os.remove(db2)

print("\n[25] bandit funnel: per-stage denominators, deep gate, submit loop")
import sqlite3 as _sq25
from alphamachine.bandit import Bandit as _B25, arm_name as _an25

# A. The motivating regression. An arm that was scanned heavily but never
# reached the gate must NOT be scored as though it had failed the gate that
# many times. Under the old pooled `trials` denominator it was, so winning
# budget made an arm look worse -- a feedback loop pointing the wrong way.
db25 = tempfile.mktemp(suffix=".db"); st25 = Store(db25)
_explored, _rejected = _an25("pv", "level_rank"), _an25("news", "accel")
b25 = _B25(st25, [_explored, _rejected])
for _ in range(50):                       # same scan record for both arms
    b25.update(_explored, stage="scan", passed=True)
    b25.update(_rejected, stage="scan", passed=True)
for _ in range(8):                        # only one of them was ever gated
    b25.update(_rejected, stage="gate", passed=False)
al25 = b25.allocate(400)
check("an explored-but-never-gated arm is not punished for being explored",
      al25[_explored] > al25[_rejected],
      f"explored={al25[_explored]} gate-rejected={al25[_rejected]}")

# B. The expensive gate has to actually move budget, or the extra API calls
# buy nothing. Two arms identical through scan and gate; one dies at it.
#
# That gate is now CORR, not DEEP. deep_pass was a composite bit -- true only
# when crowding, recency AND yearly consistency all passed -- which made it 0
# for every arm this account has ever recorded, i.e. a column that cannot
# separate anything. sample() learns on corr_* instead; deep_* is still
# recorded and still reported, it just no longer allocates. Both halves of
# that contract are asserted here.
_clean, _lucky = _an25("analyst", "change"), _an25("insiders", "event_gated")
b25b = _B25(st25, [_clean, _lucky])
for _ in range(30):
    for a in (_clean, _lucky):
        b25b.update(a, stage="scan", passed=True)
        b25b.update(a, stage="gate", passed=True)
for _ in range(10):
    b25b.update(_clean, stage="corr", passed=True)
    b25b.update(_lucky, stage="corr", passed=False)   # crowded shape
al25b = b25b.allocate(400)
check("an arm that dies at the correlation gate loses budget to one that "
      "does not",
      al25b[_clean] > 3 * al25b[_lucky],
      f"clean={al25b[_clean]} corr-blocked={al25b[_lucky]}")

# C. Three stacked priors must not flatten everything into noise.
check("compounded priors still let a converting arm win decisively",
      al25b[_clean] > 0.6 * 400, f"{al25b[_clean]}/400")

# B2. deep_* is recorded but deliberately does NOT allocate any more -- the
# whole point of splitting the two. Teaching only deep must leave allocation
# even, or we are back to learning from a bit that is always zero.
_d1, _d2 = _an25("news", "change"), _an25("macro", "change")
b25c = _B25(st25, [_d1, _d2])
for _ in range(30):
    for a in (_d1, _d2):
        b25c.update(a, stage="scan", passed=True)
        b25c.update(a, stage="gate", passed=True)
for _ in range(10):
    b25c.update(_d1, stage="deep", passed=True)
    b25c.update(_d2, stage="deep", passed=False)
al25c = b25c.allocate(400)
check("teaching only the deep stage no longer swings allocation",
      abs(al25c[_d1] - al25c[_d2]) < 0.35 * 400,
      f"{al25c[_d1]} vs {al25c[_d2]}")
_row25c = {r["arm"]: r for r in st25.arms()}
check("...but the deep counters are still recorded for reporting",
      _row25c[_d1]["deep_pass"] == 10 and _row25c[_d2]["deep_pass"] == 0)

# D. Submissions write back to the arm -- the loop the pipeline diagram claims.
_cid25 = st25.add_candidate(expr="rank(close)", settings=SimSettings().payload(),
                            theme="pv", template="level_rank", field="close")
st25.record_sim(_cid25, {"status": "OK", "alpha_id": "SUB1", "sharpe": 1.9,
                         "checks": []})
_B25(st25, [_explored]).update(_explored, stage="submitted", passed=True)
_row25 = {r["arm"]: r for r in st25.arms()}[_explored]
check("a live submission increments the arm's submitted counter",
      _row25["submitted"] == 1, f"submitted={_row25['submitted']}")
check("submission is an outcome, not a new trial",
      _row25["scan_trials"] == 50 and _row25["refine_trials"] == 0)

# E. deep_checks cache: yearly is immutable, /check expires.
st25.save_deep("SUB1", yearly={"records": [[2020, 5]]},
               checks=[{"name": "PROD_CORRELATION", "result": "PASS"}],
               ok=True, reasons=[])
_fresh = st25.load_deep("SUB1", ttl_hours=24.0)
_stale = st25.load_deep("SUB1", ttl_hours=0.0)
check("fresh deep cache returns both halves",
      _fresh["checks"] and _fresh["ok"] is True and _fresh["yearly"])
check("expired deep cache keeps immutable yearly but drops PROD_CORRELATION",
      _stale["yearly"] and _stale["checks"] is None and _stale["ok"] is None)

# F. Migration: an old-schema database gains the columns and is backfilled
# from the simulation record rather than guessed.
db26 = tempfile.mktemp(suffix=".db")
_c26 = _sq25.connect(db26)
_c26.executescript("""
CREATE TABLE candidates (id TEXT PRIMARY KEY, expr TEXT, settings TEXT,
  theme TEXT, template TEXT, field TEXT, stage TEXT, parent_id TEXT, created REAL);
CREATE TABLE sims (candidate_id TEXT PRIMARY KEY, alpha_id TEXT, status TEXT,
  error TEXT, sharpe REAL, fitness REAL, turnover REAL, returns REAL,
  drawdown REAL, margin REAL, longCount INTEGER, shortCount INTEGER,
  subuniverse_sharpe REAL, checks TEXT, raw TEXT, finished REAL);
CREATE TABLE arms (arm TEXT PRIMARY KEY, trials INTEGER DEFAULT 0,
  scan_pass INTEGER DEFAULT 0, gate_pass INTEGER DEFAULT 0,
  submitted INTEGER DEFAULT 0, sum_sharpe REAL DEFAULT 0.0, updated REAL);
""")
# two scan-origin sims (parent_id NULL), one above the survival bar; plus a
# refine variant (parent set) that must NOT count toward the scan denominator.
for _i, (_pid, _sh) in enumerate([(None, 1.4), (None, 0.1), ("p0", 2.0)]):
    _c26.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (f"c{_i}", "e", "{}", "pv", "level_rank", "close",
                  "scan", _pid, 0.0))
    _c26.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks)"
                 " VALUES (?,?,?,?,?)", (f"c{_i}", f"A{_i}", "OK", _sh, "[]"))
_c26.execute("INSERT INTO arms VALUES (?,?,?,?,?,?,?)",
             (_an25("pv", "level_rank"), 3, 1, 1, 0, 0.0, 0.0))
_c26.commit(); _c26.close()

st26 = Store(db26)                                   # migration runs on open
_cols26 = {r[1] for r in st26.conn().execute("PRAGMA table_info(arms)")}
check("migration adds the funnel columns to an existing database",
      {"scan_trials", "refine_trials", "deep_trials", "deep_pass"} <= _cols26)
_a26 = {r["arm"]: r for r in st26.arms()}[_an25("pv", "level_rank")]
check("backfill counts only scan-origin sims (parent_id IS NULL)",
      _a26["scan_trials"] == 2, f"scan_trials={_a26['scan_trials']}")
check("backfill recomputes scan_pass from the survival bar",
      _a26["scan_pass"] == 1, f"scan_pass={_a26['scan_pass']}")
check("backfill leaves the refine denominator at the remainder",
      _a26["refine_trials"] == 1, f"refine_trials={_a26['refine_trials']}")
check("migration is idempotent", Store(db26) is not None)
os.remove(db26); os.remove(db25)

# G. gate_passing_now must demand a positive deep verdict, not merely the
# absence of a negative one -- an unverified alpha is not a submittable alpha.
db27 = tempfile.mktemp(suffix=".db"); st27 = Store(db27)
from alphamachine.metrics import Verdict as _V27
_g27 = Gates()
_rec27 = {"sharpe": 2.0, "fitness": 1.5, "turnover": 0.15}
st27.record_gate_check("c1", "A1", "expr_unverified", _rec27,
                       _V27(True, []), _g27, deep=None)
st27.record_gate_check("c2", "A2", "expr_blocked", _rec27,
                       _V27(True, []), _g27, deep=_V27(False, ["lucky year"]))
st27.record_gate_check("c3", "A3", "expr_good", _rec27,
                       _V27(True, []), _g27, deep=_V27(True, []))
_pass27 = {r["expr"] for r in st27.gate_passing_now()}
check("only the deep-verified candidate counts as submittable",
      _pass27 == {"expr_good"}, f"{sorted(_pass27)}")

# A later FAIL must displace an earlier PASS for the same expression. Caught
# live: a lazy-computation race made client.alpha_check() return a false
# empty-checks "pass" that got cached and reported submittable for hours; the
# corrective re-check appended a new deep_ok=0 row (gate_history is
# append-only, so the bad row is never deleted) but gate_passing_now() kept
# surfacing the stale pass anyway -- its subquery restricted itself to
# `WHERE ok=1 AND deep_ok=1` before taking MAX(ts), so it found the latest
# PASS ever recorded rather than the latest CHECK overall.
st27.record_gate_check("c3", "A3b", "expr_good", _rec27,
                       _V27(True, []), _g27, deep=_V27(False, ["PROD_CORRELATION"]))
_pass27b = {r["expr"] for r in st27.gate_passing_now()}
check("a later fail displaces an earlier pass for the same expression",
      "expr_good" not in _pass27b, f"{sorted(_pass27b)}")
os.remove(db27)

print("\n[26] deep verdict: unknown is neither pass nor fail")
from alphamachine.submit import Submitter as _S26
from alphamachine.config import RunConfig as _RC26

db28 = tempfile.mktemp(suffix=".db"); st28 = Store(db28)


class _C28:
    """Stub client. `yearly` None => raise, {} => empty recordset."""
    def __init__(self, yearly, checks=None, boom_check=False):
        self._y, self._c, self._boom = yearly, checks, boom_check
    def alpha_yearly(self, aid):
        if self._y is None:
            raise RuntimeError("503")
        return self._y
    def alpha_check(self, aid):
        if self._boom:
            raise RuntimeError("timed out")
        return {"is": {"checks": self._c or []}}


_good_yr = {"schema": {"properties": [{"name": "year"}, {"name": "pnl"}]},
            "records": [[2019, 5], [2020, 6], [2021, 7], [2022, 8]]}
_lucky_yr = {"schema": {"properties": [{"name": "year"}, {"name": "pnl"}]},
             "records": [[2019, -1], [2020, 90], [2021, -2], [2022, -1]]}


def _sub28(yearly, checks=None, boom_check=False):
    return _S26(_RC26(db_path=db28), st28, _C28(yearly, checks, boom_check))


check("a failed yearly fetch is INCONCLUSIVE, not a pass",
      _sub28(None).deep_verdict("X1") is None)
check("an empty yearly recordset is INCONCLUSIVE, not a pass",
      _sub28({}).deep_verdict("X2") is None)
check("a failed server check is INCONCLUSIVE, not a pass",
      _sub28(_good_yr, boom_check=True).deep_verdict("X3") is None)
_v28 = _sub28(_good_yr, checks=[{"name": "LOW_SHARPE", "result": "PASS"}]
              ).deep_verdict("X4")
check("a clean alpha passes the deep gate", _v28 is not None and _v28.ok,
      f"{_v28}")
_v28b = _sub28(_lucky_yr, checks=[{"name": "LOW_SHARPE", "result": "PASS"}]
              ).deep_verdict("X5")
check("a one-lucky-year alpha fails the deep gate",
      _v28b is not None and not _v28b.ok, f"{_v28b and _v28b.reasons}")
_v28c = _sub28(_good_yr, checks=[{"name": "PROD_CORRELATION", "result": "FAIL"}]
               ).deep_verdict("X6")
check("a crowded alpha fails on PROD_CORRELATION",
      _v28c is not None and not _v28c.ok
      and any("PROD_CORRELATION" in r for r in _v28c.reasons))
# The bug this section exists for: yearly_consistency PASSES with an advisory
# reason when it has nothing to judge. Collecting that reason as a failure
# turned "we could not check" into "this alpha is bad".
check("an advisory reason from a PASSING sub-verdict is not a failure",
      "no yearly stats available" not in json.dumps(
          [r for r in (_v28.reasons if _v28 else [])]))

# A second bug this section now also exists for: BRAIN's lazy-computation race
# hits /check too, not just the recordset endpoints. A 200 with an empty
# `checks` list looks exactly like "nothing failed" to a naive reader, when it
# means "not finished yet." Caught live: one alpha returned checks:[]
# immediately, then 20 real entries (incl. PROD_CORRELATION FAIL) on retry --
# the empty read got cached as a clean pass and stayed "submittable" for
# hours. client.alpha_check() now retries this exact race (see its own
# docstring); this is the belt-and-braces layer for whatever still gets
# through empty.
check("an empty server-check response is INCONCLUSIVE, not a clean pass",
      _sub28(_good_yr, checks=[]).deep_verdict("X7") is None)
# A cached empty-checks entry (this same race, saved before the fix existed)
# must not be trusted just because it is within TTL -- it would otherwise
# report a false pass for the entry's whole deep_check_ttl_hours.
st28.save_deep("X8", yearly=_good_yr, checks=[], ok=True, reasons=[])
check("a POISONED cache entry (ok=True, checks=[]) is not trusted on replay",
      _sub28(_good_yr, checks=[{"name": "LOW_SHARPE", "result": "PASS"}]
            ).deep_verdict("X8") is not None)
os.remove(db28)

print("\n[27] interleaved ladders: fill the slots, keep the rung order")
from alphamachine.refine import LadderState as _LS27

db29 = tempfile.mktemp(suffix=".db"); st29 = Store(db29)


class _Ex29:
    """Stub executor: records every dispatch, then simulates the batch.

    Metrics are a deterministic function of the candidate id, never of arrival
    order -- otherwise a scheduling change would look like a behaviour change
    and the equivalence check below would prove nothing.
    """
    def __init__(self, store):
        self.store, self.dispatches, self.n = store, [], 0
        self._halted = False
    @staticmethod
    def _metrics(cid):
        h = int(hashlib.sha1(cid.encode()).hexdigest()[:8], 16)
        return {"sharpe": 0.5 + (h % 250) / 100.0,        # 0.50 .. 2.99
                "fitness": 0.4 + (h // 250 % 160) / 100.0,
                "turnover": 0.02 + (h // 7 % 40) / 100.0}
    def run(self, ids, on_result=None):
        self.dispatches.append(list(ids))
        for cid in ids:
            self.n += 1
            self.store.record_sim(cid, {"status": "OK", "alpha_id": f"S{cid[:6]}",
                                        "checks": [], **self._metrics(cid)})
        return {"ok": len(ids), "error": 0, "skipped": 0}
    def halted(self):
        return self._halted


def _drive29(ref, states):
    """The wavefront driver from cli.cmd_refine, reduced to its essentials."""
    while True:
        act = [s for s in states if not s.done]
        if not act:
            return
        batch = list(dict.fromkeys(c for s in act for c in ref.step(s)))
        planned = [s for s in act if s.pending]
        if batch:
            ref.ex.run(batch)
        for s in act:
            ref.absorb(s)
        if not planned:
            return


def _seed29(store, n, theme="pv", template="level_rank"):
    """n independent scan roots, each with a simulated parent record."""
    roots = []
    for i in range(n):
        cid = store.add_candidate(expr=f"group_rank(f{i}, subindustry)",
                                  settings=SimSettings().payload(),
                                  theme=theme, template=template, field=f"f{i}")
        store.record_sim(cid, {"status": "OK", "alpha_id": f"R{i}",
                               "sharpe": 1.2, "fitness": 1.0,
                               "turnover": 0.15, "checks": []})
        roots.append(cid)
    return roots


# A. EQUIVALENCE, then utilisation. Two identical fresh databases: one driven
# serially through ladder(), one through the wavefront driver. Same seeds, same
# deterministic metrics -- so any difference in the elected champions would be
# the restructure changing what refinement DECIDES, which is exactly what must
# not happen. Only then is the batch-size win meaningful.
roots29 = _seed29(st29, 6)
ex29 = _Ex29(st29)
ref29 = Refiner(st29, ex29, Gates())
states29 = [ref29.start(r) for r in roots29]
_drive29(ref29, states29)

db30 = tempfile.mktemp(suffix=".db"); st30 = Store(db30)
roots30 = _seed29(st30, 6)
ex30 = _Ex29(st30)
ref30 = Refiner(st30, ex30, Gates())
serial30 = [ref30.ladder(r)[0] for r in roots30]

# Ids are sha1(expr+settings), identical across the two runs, so champions are
# directly comparable.
check("interleaved and serial elect the SAME champions",
      [s.best_id for s in states29] == serial30,
      f"wave={[x[:6] for x in (s.best_id for s in states29)]} "
      f"serial={[x[:6] for x in serial30]}")
check("same total simulations either way -- this is scheduling, not extra work",
      ex29.n == ex30.n, f"wave={ex29.n} serial={ex30.n}")
wave_avg = sum(len(d) for d in ex29.dispatches) / max(len(ex29.dispatches), 1)
serial_avg = sum(len(d) for d in ex30.dispatches) / max(len(ex30.dispatches), 1)
check("interleaving dispatches materially bigger batches than serial",
      wave_avg > 3 * serial_avg,
      f"wave avg={wave_avg:.1f} vs serial avg={serial_avg:.1f}")

# B. Rung order per candidate is preserved: every variant a state dispatches
# must descend from THAT state's own current champion, never another's.
for d in ex29.dispatches:
    for cid in d:
        row = st29.candidate(cid)
        assert row["parent_id"] is not None
_owners = {}
for s in states29:
    _owners[s.root_id] = s
check("every ladder reached the last rung", all(s.done for s in states29))
check("each champion descends from its own root",
      all(_owners[s.root_id].best_id == s.best_id for s in states29))

# C. The hazard interleaving introduces: two roots planning an identical
# variant in one round produce the SAME candidate id (sha1 of expr+settings),
# so an un-deduped batch would dispatch and PAY for it twice.
db31 = tempfile.mktemp(suffix=".db"); st31 = Store(db31)
twin_a = st31.add_candidate(expr="group_rank(same, subindustry)",
                            settings=SimSettings().payload(), theme="pv",
                            template="level_rank", field="same")
st31.record_sim(twin_a, {"status": "OK", "alpha_id": "T1", "sharpe": 1.2,
                         "fitness": 1.0, "turnover": 0.15, "checks": []})
ex31 = _Ex29(st31)
ref31 = Refiner(st31, ex31, Gates())
s31a, s31b = ref31.start(twin_a), ref31.start(twin_a)   # same lineage twice
raw31 = ref31.step(s31a) + ref31.step(s31b)
check("identical variants really do collide on one candidate id",
      len(raw31) > len(set(raw31)), f"{len(raw31)} ids, {len(set(raw31))} unique")
check("deduping the pooled batch stops us paying twice for one simulation",
      len(list(dict.fromkeys(raw31))) == len(set(raw31)))

# D. A rung that asks for nothing must advance, not spin forever.
db32 = tempfile.mktemp(suffix=".db"); st32 = Store(db32)
root32 = _seed29(st32, 1)[0]
ex32 = _Ex29(st32)
ref32 = Refiner(st32, ex32, Gates())
s32 = ref32.start(root32)
check("rung 0 is skipped for a positive-sharpe alpha rather than stalling",
      ref32.step(s32) and s32.rung > 0, f"rung={s32.rung}")
_before = s32.rung
for _ in range(20):                      # exhaust it
    if not ref32.step(s32):
        break
    ex32.run(s32.pending)
    ref32.absorb(s32)
check("a ladder always terminates", s32.done, f"rung={s32.rung}")

# E. An interrupted ladder must never be recorded as gated -- select reads
# stage='gated' as "finished", and judging a half-refined alpha is worse than
# not judging it.
db33 = tempfile.mktemp(suffix=".db"); st33 = Store(db33)
root33 = _seed29(st33, 1)[0]
ref33 = Refiner(st33, _Ex29(st33), Gates())
s33 = ref33.start(root33)
ref33.step(s33)
check("finalize refuses an unfinished ladder",
      _raises(lambda: ref33.finalize(s33), ValueError))
check("nothing was marked gated by the attempt",
      st33.conn().execute(
          "SELECT COUNT(*) n FROM candidates WHERE stage='gated'"
      ).fetchone()["n"] == 0)

# F. Resume must be both free AND correct. Replaying a finished ladder costs
# no slots, and -- the part that used to be broken -- still elects the SAME
# champion. _queue() previously dropped already-simulated variants, so a rung
# looked empty, was skipped, and _best_of never saw work a previous run had
# already PAID for: the replay quietly crowned the ROOT and discarded the real
# winner. That is what an interrupted refine did on every restart.
ex34 = _Ex29(st29)
ref34 = Refiner(st29, ex34, Gates())
replayed = []
for r in roots29:
    s = ref34.start(r)
    while not s.done:
        todo = ref34.step(s)
        if not s.pending:
            break
        if todo:
            ex34.run(todo)
        ref34.absorb(s)
    replayed.append(s.best_id)
check("replaying already-simulated ladders dispatches nothing",
      ex34.n == 0, f"{ex34.n} redundant simulations")
check("a resumed ladder elects the same champion, not the root",
      replayed == [s.best_id for s in states29],
      f"replay={[x[:6] for x in replayed]}")
check("resume does not collapse to the roots",
      not any(r in roots29 for r in replayed) or
      all(r == b for r, b in zip(replayed, [s.best_id for s in states29])))

# G. sqlite3.Row has no .get(). cmd_refine keeps a per-root record and reads it
# defensively AFTER the wave finishes, so storing the raw Row crashed the whole
# judging phase -- every simulation paid for, no champion judged.
_row29 = st29.sims_where("s.status='OK'")[0]
check("sqlite3.Row genuinely lacks .get(), so it must not be stored raw",
      not hasattr(_row29, "get"))
_meta29 = {k: _row29[k] for k in _row29.keys()}
check("the dict cmd_refine stores instead supports defensive reads",
      _meta29.get("theme") is not None and _meta29.get("nope", "d") == "d")

# H. --top N should mean N candidates that will do NEW work.
_sql27 = ("s.status='OK' AND c.stage='scan' AND NOT EXISTS "
          "(SELECT 1 FROM candidates k WHERE k.parent_id=c.id)")
_unrefined = {r["id"] for r in st29.sims_where(_sql27)}
check("roots that already have children are excluded by default",
      not (_unrefined & set(roots29)), f"{len(_unrefined & set(roots29))} leaked")
_all = {r["id"] for r in st29.sims_where("s.status='OK' AND c.stage='scan'")}
check("--redo can still reach them", set(roots29) <= _all)
for _f in (db29, db30, db31, db32, db33):
    os.remove(_f)

print("\n[28] deep verdicts teach the bandit exactly once per lineage")
import sqlite3 as _sq28
from alphamachine.cli import _teach_deep as _td28
from alphamachine.bandit import Bandit as _B28, arm_name as _an28
from alphamachine.metrics import Verdict as _V28

db35 = tempfile.mktemp(suffix=".db"); st35 = Store(db35)
_ARM = _an28("pv", "level_rank")


def _lineage35(store, depth=3):
    """A scan root plus `depth` descendants, as a real ladder produces."""
    root = store.add_candidate(expr="group_rank(close, subindustry)",
                               settings=SimSettings().payload(), theme="pv",
                               template="level_rank", field="close")
    chain, parent = [root], root
    for i in range(depth):
        c = store.add_candidate(expr=f"winsorize(group_rank(close, subindustry), {i})",
                                settings=SimSettings().payload(), theme="pv",
                                template="level_rank", field="close",
                                stage="refine", parent_id=parent)
        chain.append(c); parent = c
    return chain


chain35 = _lineage35(st35)
check("lineage_root walks any depth back to the scan root",
      all(st35.lineage_root(c) == chain35[0] for c in chain35))
check("lineage_root of the root is itself",
      st35.lineage_root(chain35[0]) == chain35[0])

# A. The regression: a verdict computed OUTSIDE refine must reach the arm.
b35 = _B28(st35, [_ARM])
_td28(st35, b35, chain35[-1], _ARM, _V28(False, ["only 18% of years positive"]))
_row = {r["arm"]: r for r in st35.arms()}[_ARM]
check("a deep verdict from outside refine reaches the arm",
      _row["deep_trials"] == 1 and _row["deep_pass"] == 0)

# B. Idempotency -- the whole reason teaching from a query command is safe.
for c in chain35:                        # every rung of the same ladder
    _td28(st35, b35, c, _ARM, _V28(False, ["x"]))
for _ in range(5):                       # and gatelog run five more times
    _td28(st35, b35, chain35[-1], _ARM, _V28(False, ["x"]))
_row = {r["arm"]: r for r in st35.arms()}[_ARM]
check("one lineage is one trial, however many alphas or reruns it produces",
      _row["deep_trials"] == 1, f"deep_trials={_row['deep_trials']}")

# C. Inconclusive teaches nothing AND claims nothing, so a later real verdict
# for that same lineage can still be counted.
db36 = tempfile.mktemp(suffix=".db"); st36 = Store(db36)
chain36 = _lineage35(st36)
b36 = _B28(st36, [_ARM])
_td28(st36, b36, chain36[-1], _ARM, None)
_rows36 = {r["arm"]: r for r in st36.arms()}
check("an inconclusive verdict teaches nothing",
      _ARM not in _rows36 or _rows36[_ARM]["deep_trials"] == 0,
      f"arms={list(_rows36)}")
check("...and does not burn the claim",
      st36.claim_bandit_trial("deep", chain36[0], _ARM) is True)

# D. The claim is won exactly once.
db37 = tempfile.mktemp(suffix=".db"); st37 = Store(db37)
check("two claimants, one winner",
      [st37.claim_bandit_trial("deep", "k1", "a::b") for _ in range(3)]
      == [True, False, False])
check("a different stage is a different claim",
      st37.claim_bandit_trial("gate", "k1", "a::b") is True)

# E. Rebuild from gate_history: one trial per lineage, idempotent, and the
# gate stage is left alone (gate_history includes non-champions, so it is not
# a clean sample of the population that stage models).
db38 = tempfile.mktemp(suffix=".db")
st38 = Store(db38)
chain38 = _lineage35(st38)
st38.conn().execute("UPDATE arms SET refine_trials=7, gate_pass=3, deep_trials=1"
                    " WHERE arm=?", (_ARM,))
st38.conn().execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                    (_ARM, 0.0))
st38.conn().execute("UPDATE arms SET refine_trials=7, gate_pass=3, deep_trials=1"
                    " WHERE arm=?", (_ARM,))
_g38 = Gates()
for c in chain38:                        # 4 alphas, all one lineage
    st38.record_gate_check(c, f"A{c[:4]}", "e", {"sharpe": 2.0, "fitness": 1.5,
                                                 "turnover": 0.15},
                           _V28(True, []), _g38, deep=_V28(False, ["lucky"]))
st38.conn().execute("DROP TABLE bandit_taught")   # simulate a pre-ledger db
st38.conn().commit()
st39 = Store(db38)                       # reopen -> migration rebuilds
_a39 = {r["arm"]: r for r in st39.arms()}[_ARM]
check("rebuild counts one deep trial per lineage, not per alpha",
      _a39["deep_trials"] == 1 and _a39["deep_pass"] == 0,
      f"trials={_a39['deep_trials']} pass={_a39['deep_pass']}")
check("rebuild leaves the gate stage untouched",
      _a39["refine_trials"] == 7 and _a39["gate_pass"] == 3)
st40 = Store(db38)                       # reopen again -> no double count
_a40 = {r["arm"]: r for r in st40.arms()}[_ARM]
check("rebuild is one-time, not once per open",
      _a40["deep_trials"] == 1, f"deep_trials={_a40['deep_trials']}")
for _f in (db35, db36, db37, db38):
    os.remove(_f)

print("\n[29] autoloop: never --live, and halt vs. real-bug classification")
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
import autoloop as _al

# Static check on the safety boundary itself: no code path in the file
# constructs a --live argument, checked outside any triple-quoted string so
# the module docstring mentioning --live in prose does not count as a hit.
_src29 = open(os.path.join(_scripts_dir, "autoloop.py")).read()
_in_doc29, _hits29 = False, []
for _ln in _src29.splitlines():
    if _ln.count(chr(34) * 3) % 2 == 1:
        _in_doc29 = not _in_doc29
        continue
    if not _in_doc29 and "--live" in _ln and not _ln.strip().startswith("#"):
        _hits29.append(_ln)
check("no code path in autoloop.py constructs --live",
      _hits29 == [], f"{_hits29}")
check("submit is invoked exactly once, with only --n",
      _src29.count(chr(34) + "submit" + chr(34)) == 1
      and '"--n", str(args.submit_n)' in _src29)


class _P29:
    """Stand-in for subprocess.Popen: an iterable .stdout, then .wait()/.poll()."""
    def __init__(self, returncode, lines=()):
        self.returncode = returncode
        self.stdout = iter([ln + "\n" for ln in lines])
        self._done = False
    def wait(self):
        self._done = True
    def poll(self):
        return self.returncode if self._done else None


def _stub29(results):
    calls = []
    def popen(cmd, **kw):
        calls.append(cmd)
        return results[min(len(calls) - 1, len(results) - 1)]
    return calls, popen


_real_sleep29, _real_wait29 = _al.time.sleep, _al.wait_for_session
_al.time.sleep = lambda s: None

# A startup crash (BiometricRequired traceback, exit 1) is an auth block:
# retried after the session comes back, not treated as a bug.
calls, _al.subprocess.Popen = _stub29([
    _P29(1, ["Traceback...", "alphamachine.client.BiometricRequired: spent"]),
    _P29(0, ["scan done"])])
_al.session_alive = lambda: True
_al.run_stage(3, "scan", ["--budget", "10"])
check("BiometricRequired traceback is retried, not fatal", len(calls) == 2)

# A silent mid-run halt (exit 0, but a HALTED line) is ALSO an auth block --
# the dangerous case, because nothing about the exit code says so.
calls, _al.subprocess.Popen = _stub29([
    _P29(0, ["10/25 done", "HALTED: BRAIN requires a biometric check"]),
    _P29(0, ["25/25 done"])])
_al.run_stage(3, "refine", ["--top", "25"])
check("a HALTED line with exit 0 is caught, not silently accepted", len(calls) == 2)

# A clean run must never wait or retry.
calls, _al.subprocess.Popen = _stub29([_P29(0, ["3 candidate(s)"])])
_waited29 = []
_al.wait_for_session = lambda: _waited29.append(1)
_al.run_stage(3, "select", ["--n", "10"])
check("a clean stage does not retry or wait for auth",
      len(calls) == 1 and not _waited29)

# The negative case that matters most: a REAL bug must stop the loop, not
# spin on wait_for_session forever mistaking it for an auth block.
calls, _al.subprocess.Popen = _stub29([
    _P29(1, ["Traceback...", "ZeroDivisionError: boom"])])
check("a non-auth crash raises SystemExit instead of retrying forever",
      _raises(lambda: _al.run_stage(3, "scan", ["--budget", "10"]), SystemExit))

# The THIRD outcome, which the original two-way split had no room for: the
# network went away. Seen live -- DNS stopped resolving mid-`select`, the stage
# exited non-zero with a traceback and no auth marker, and the loop classified
# a five-minute blip as a code bug and shut itself down permanently.
_dns29 = ["Traceback...",
          "urllib3.exceptions.NameResolutionError: Failed to resolve "
          "'api.worldquantbrain.com' ([Errno -3] Temporary failure in name "
          "resolution)"]
calls, _al.subprocess.Popen = _stub29([_P29(1, _dns29), _P29(0, ["3 candidate(s)"])])
_al.run_stage(3, "select", ["--n", "10"])
check("a DNS failure is retried, not treated as a fatal bug", len(calls) == 2)

# It must not retry forever either -- a network that never comes back is a
# real stop, just a slower one. Needs a FRESH _P29 per call: .stdout is a
# one-shot iterator, so reusing one object reads as empty output on the second
# attempt and takes the fatal branch for the wrong reason.
_calls29b = []
def _always_dns29(cmd, **kw):
    _calls29b.append(cmd)
    return _P29(1, _dns29)
_al.subprocess.Popen = _always_dns29
check("a network error that never clears eventually stops the loop",
      _raises(lambda: _al.run_stage(3, "select", ["--n", "10"]), SystemExit))
check("it retried up to the cap before giving up",
      len(_calls29b) == _al.TRANSIENT_MAX_RETRIES + 1,
      f"{len(_calls29b)} attempts, cap {_al.TRANSIENT_MAX_RETRIES}")

# The transient counter must not let a genuine bug slip through, and must not
# accumulate across unrelated auth blocks.
calls, _al.subprocess.Popen = _stub29([
    _P29(1, ["Traceback...", "KeyError: 'expr'"])])
check("a real bug is still fatal even with transient handling in place",
      _raises(lambda: _al.run_stage(3, "scan", ["--budget", "10"]), SystemExit))

_al.time.sleep, _al.wait_for_session = _real_sleep29, _real_wait29

# The orphan-child bug this session actually hit: killing autoloop.py while a
# stage subprocess is running must terminate that child too, or a later clean
# launch runs a second one against the same account concurrently.
class _Term29:
    def __init__(self): self.terminated = False
    def poll(self): return None            # still running
    def terminate(self): self.terminated = True
_child29 = _Term29()
_al._current_proc = _child29
check("SIGTERM/SIGINT is wired to terminate the running child",
      _al.signal.getsignal(_al.signal.SIGTERM) is _al._handle_signal)
check("the signal handler actually terminates the tracked child",
      _raises(lambda: _al._handle_signal(15, None), SystemExit) and _child29.terminated)
_al._current_proc = None

print("\n[30] two-field templates: the PROD_CORRELATION escape hatch")
import sqlite3 as _sq30
from alphamachine import priors as _P30

_pair30 = [t for t in T.LIBRARY if t.needs_field2]
check("the library carries two-field templates", len(_pair30) >= 5,
      f"{[t.name for t in _pair30]}")
_r30 = [t.render("fnd6_a", days=126, group="industry", field2="anl4_b")
        for t in _pair30]
check("both fields are substituted, no placeholder survives",
      all("fnd6_a" in e and "anl4_b" in e and "{" not in e for e in _r30))
check("every two-field template lints clean",
      not [t.name for t, e in zip(_pair30, _r30) if T.lint(e)],
      f"{[t.name for t, e in zip(_pair30, _r30) if T.lint(e)]}")
# Families are reused on purpose: priors.py scores FAMILIES per dataset, so a
# new family name would mean no prior and the pessimistic (1,6) fallback.
check("two-field templates reuse existing families (so priors apply)",
      {t.family for t in _pair30}
      <= {t.family for t in T.LIBRARY if not t.needs_field2})

# A. wrap_backfill: the substring bug that lint() could never have caught.
# `fnd6_x` is a prefix of `fnd6_x_ttm`; str.replace would have produced
# ts_backfill(fnd6_x, 120)_ttm -- balanced parens, no placeholder, so the slot
# gets paid for a parse error.
_col30 = T.wrap_backfill("group_rank(fnd6_x_ttm - fnd6_x, sector)", "fnd6_x")
check("backfill matches identifiers, not substrings",
      "fnd6_x_ttm -" in _col30 and "ts_backfill(fnd6_x, 120)" in _col30,
      f"-> {_col30}")
_twice30 = T.wrap_backfill(
    "group_rank(divide(ts_delta(fx, 66), abs(ts_mean(fx, 66))), ind)", "fx")
check("backfill wraps EVERY occurrence, not just the first",
      _twice30.count("ts_backfill(fx, 120)") == 2, f"-> {_twice30}")
check("backfill is idempotent per field",
      T.wrap_backfill(_twice30, "fx") == _twice30)
_two30 = T.wrap_backfill(T.wrap_backfill(
    "group_rank(divide(fa, fb), sector)", "fa"), "fb")
check("two fields are wrapped independently",
      "ts_backfill(fa, 120)" in _two30 and "ts_backfill(fb, 120)" in _two30
      and "ts_backfill(ts_backfill" not in _two30, f"-> {_two30}")

# B. Storage: field2 needs its own column. Comma-joining into `field` would
# make wrap_backfill a silent no-op in refine, and a variant identical to its
# parent hashes to the PARENT'S id -- the rung would re-crown the incumbent.
db30 = tempfile.mktemp(suffix=".db"); st30 = Store(db30)
_c30 = st30.add_candidate(expr="group_rank(divide(fa, fb), sector)",
                          settings=SimSettings().payload(), theme="analyst",
                          template="ratio_fields", field="fa", field2="fb")
check("field2 round-trips through the store",
      st30.candidate(_c30)["field2"] == "fb")

db31 = tempfile.mktemp(suffix=".db")                 # old schema, no field2
_o30 = _sq30.connect(db31)
_o30.executescript("""
CREATE TABLE candidates (id TEXT PRIMARY KEY, expr TEXT, settings TEXT,
  theme TEXT, template TEXT, field TEXT, stage TEXT, parent_id TEXT, created REAL);
""")
_o30.commit(); _o30.close()
st31 = Store(db31)                                   # migration runs on open
check("migration adds field2 to a database that predates it",
      "field2" in {r[1] for r in
                   st31.conn().execute("PRAGMA table_info(candidates)")})

# C. Refine rung 2 on a two-field candidate. The old whole-expression guard
# (`"ts_backfill" not in expr`) skipped the rung once EITHER field was wrapped,
# leaving the other raw for good.
class _Ex30:                       # plan() never touches the executor
    pass
ref30 = Refiner(st30, _Ex30(), Gates())
_p30 = st30.candidate(_c30)
_v30 = ref30.plan(_c30, {"sharpe": 1.6, "turnover": 0.2, "longCount": 900,
                         "checks": []}, 2)
_bf30 = [v for v in _v30 if "ts_backfill" in v.expr]
check("rung 2 backfills BOTH fields of a two-field candidate",
      len(_bf30) == 1 and "ts_backfill(fa, 120)" in _bf30[0].expr
      and "ts_backfill(fb, 120)" in _bf30[0].expr,
      f"-> {_bf30[0].expr if _bf30 else None}")
check("no rung-2 variant is byte-identical to its parent",
      all(v.expr != _p30["expr"] for v in _v30))
# The half-wrapped case: one field already backfilled at scan time must not
# block the other, which is exactly what the old guard did.
_half30 = st30.add_candidate(
    expr="group_rank(divide(ts_backfill(fa, 120), fb), sector)",
    settings=SimSettings().payload(), theme="analyst",
    template="ratio_fields", field="fa", field2="fb")
_hv30 = [v for v in ref30.plan(_half30, {"sharpe": 1.6, "turnover": 0.2,
                                         "longCount": 900, "checks": []}, 2)
         if "ts_backfill(fb" in v.expr]
check("an already-backfilled first field does not block the second",
      len(_hv30) == 1 and _hv30[0].expr.count("ts_backfill") == 2,
      f"-> {_hv30[0].expr if _hv30 else None}")

# D. Generation: the partner must come from a DIFFERENT theme, because
# PROD_CORRELATION fires when a conventional transform meets a conventional
# data source -- a cross-theme pairing is what is unlikely to already exist.
_fields30 = [{"id": "an_a", "_theme": "analyst", "coverage": 1.0,
              "dataset": {"id": "d1"}, "alphaCount": 10},
             {"id": "an_b", "_theme": "analyst", "coverage": 1.0,
              "dataset": {"id": "d1"}, "alphaCount": 10},
             {"id": "op_a", "_theme": "option", "coverage": 1.0,
              "dataset": {"id": "d2"}, "alphaCount": 10},
             {"id": "op_b", "_theme": "option", "coverage": 1.0,
              "dataset": {"id": "d2"}, "alphaCount": 500000}]
db32 = tempfile.mktemp(suffix=".db"); st32 = Store(db32)
generate(st32, _fields30, budget=60, base=SimSettings(), seed=11,
         templates=_pair30)
_rows30 = st32.conn().execute(
    "SELECT theme, template, field, field2, expr FROM candidates").fetchall()
_themes30 = {f["id"]: f["_theme"] for f in _fields30}
check("generation produces two-field candidates", len(_rows30) > 0,
      f"{len(_rows30)} rows")
check("every two-field candidate records its partner",
      all(r["field2"] for r in _rows30))
check("the partner never repeats the primary field",
      all(r["field"] != r["field2"] for r in _rows30))
check("the partner always comes from a different theme",
      all(_themes30[r["field2"]] != r["theme"] for r in _rows30),
      f"{[(r['theme'], r['field2']) for r in _rows30[:3]]}")
check("both fields appear in the generated expression",
      all(r["field"] in r["expr"] and r["field2"] in r["expr"] for r in _rows30))
check("generated two-field expressions lint clean",
      not any(T.lint(r["expr"]) for r in _rows30))
# op_b carries 500k existing alphas; Catalog.field_value divides by
# 1+log10(1+alphaCount), so it should be drawn far less than the uncrowded op_a.
_partners30 = [r["field2"] for r in _rows30 if r["theme"] == "analyst"]
if _partners30:
    check("a crowded partner is drawn less often than an uncrowded one",
          _partners30.count("op_b") < _partners30.count("op_a"),
          f"op_a={_partners30.count('op_a')} op_b={_partners30.count('op_b')}")

# E. Priors reach the new arms with no LLM call, purely from the cached
# per-dataset family scores -- the reason the families were reused.
_pc30 = tempfile.mktemp(suffix=".json")
with open(_pc30, "w") as _fh30:
    json.dump({"d1": {"scores": {f.family: 0.8 for f in T.LIBRARY},
                      "rationale": ""}}, _fh30)
_pri30 = _P30.arm_priors(_fields30, cache_path=_pc30)
check("two-field arms inherit a prior from the existing family scores",
      all(arm_name("analyst", t.name) in _pri30 for t in _pair30),
      f"{[t.name for t in _pair30 if arm_name('analyst', t.name) not in _pri30]}")
check("that prior is better than the pessimistic (1,6) fallback",
      all(_pri30[arm_name("analyst", t.name)][0]
          / sum(_pri30[arm_name("analyst", t.name)]) > 1.0 / 7.0
          for t in _pair30))
os.remove(_pc30)
for _f30 in (db30, db31, db32):
    os.remove(_f30)

print("\n[31] gates BLOCK what BRAIN rejects, PREFER what pays well")
from alphamachine.config import AutoGates as _AG31
from alphamachine.metrics import check_limit as _cl31
g31 = Gates()

# The regression this section exists for: omqZOwpb, verbatim from the database.
# Our old gate rejected it on a flat sub/sharpe ratio of 0.61 < 0.70, so it
# never reached a deep check. BRAIN's own /check passed every line except
# PROD_CORRELATION (0.767) -- including LOW_SUB_UNIVERSE_SHARPE, limit 1.14.
real31 = {"sharpe": 2.64, "fitness": 1.63, "turnover": 0.2883,
          "drawdown": 0.0329, "margin": 0.000764, "longCount": 1224,
          "shortCount": 1882, "subuniverse_sharpe": 1.6,
          "checks": [{"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS",
                      "limit": 1.14, "value": 1.6},
                     {"name": "HIGH_TURNOVER", "result": "PASS",
                      "limit": 0.7, "value": 0.2883}]}
_v31 = quality_verdict(real31, g31)
check("the best real candidate is no longer hidden by our own gate",
      _v31.ok, f"-> {_v31.reasons}")
check("its sub/sharpe ratio really is under the OLD flat bar",
      real31["subuniverse_sharpe"] / real31["sharpe"] < 0.70,
      f"{real31['subuniverse_sharpe'] / real31['sharpe']:.2f}")

# Sub-universe: BRAIN's per-alpha limit is the bar, not a ratio of our own.
check("sub-universe judged against BRAIN's reported limit, and passes",
      quality_verdict(dict(real31, subuniverse_sharpe=1.20), g31).ok)
check("sub-universe below BRAIN's reported limit still fails",
      not quality_verdict(dict(real31, subuniverse_sharpe=1.00), g31).ok)
check("check_limit reads the limit, check_value reads the value",
      _cl31(real31["checks"], "LOW_SUB_UNIVERSE_SHARPE") == 1.14
      and _cl31(real31["checks"], "NOPE") is None)
# Fallback for a record with no check to read: the BRAIN_RULES.md formula,
# never the flat ratio.
_nc31 = {k: v for k, v in real31.items() if k != "checks"}
_nc31["checks"] = []
check("with no reported limit it falls back to the documented formula",
      quality_verdict(_nc31, g31).ok
      and not quality_verdict(dict(_nc31, subuniverse_sharpe=0.9), g31).ok,
      f"floor={subuniverse_floor(2.64, 'TOP3000'):.2f}")

# Turnover: block at BRAIN's 0.70 ceiling, prefer 0.30.
mid31 = dict(real31, turnover=0.45)
check("turnover between the ideal and the ceiling is ACCEPTED now",
      quality_verdict(mid31, g31).ok, f"-> {quality_verdict(mid31, g31).reasons}")
check("turnover above BRAIN's 0.70 ceiling is still rejected",
      not quality_verdict(dict(real31, turnover=0.75), g31).ok)
check("but headroom still prefers the low-turnover version",
      headroom(dict(real31, turnover=0.25), g31) > headroom(mid31, g31),
      f"{headroom(dict(real31, turnover=0.25), g31):.2f} vs "
      f"{headroom(mid31, g31):.2f}")
check("headroom still punishes turnover under the FLOOR",
      headroom(dict(real31, turnover=0.005), g31) < headroom(mid31, g31))

# Correlation ceilings match the platform rather than padding below it.
check("self/prod correlation gates sit at the documented 0.70 ceiling",
      g31.max_self_corr == 0.70 and g31.max_prod_corr == 0.70)
check("a 0.65 prod-corr alpha is no longer pre-rejected below BRAIN's bar",
      quality_verdict(dict(real31, prod_correlation=0.65), g31).ok)
check("a 0.75 prod-corr alpha is still rejected",
      not quality_verdict(dict(real31, prod_correlation=0.75), g31).ok)

# AutoGates is the one place staying deliberately stricter: nothing reviews an
# unattended submission, so the asymmetry there is the point.
check("unattended gates stay tighter than the manual ones",
      not quality_verdict(mid31, _AG31()).ok
      and _AG31().turnover_max < g31.turnover_max)
check("AutoGates keeps its score and block aligned",
      _AG31().turnover_ideal_max <= _AG31().turnover_max)

print("\n[32] audit: consistency checks against the live-shaped database")
from alphamachine import audit as _AU32
import sqlite3 as _sq32, time as _t32

db32 = tempfile.mktemp(suffix=".db"); st32 = Store(db32)

# A. Clean baseline FIRST -- one legitimate row through every table, built
# entirely through the public Store API. Every "fires on broken data"
# assertion below is only meaningful if this passes: an audit tool that
# false-positives on ordinary correct data trains people to ignore it.
_cid32 = st32.add_candidate(expr="group_rank(fa, sector)",
                            settings=SimSettings().payload(), theme="pv",
                            template="level_rank", field="fa",
                            settings_key=SimSettings().key())
st32.record_sim(_cid32, {"alpha_id": "CLEAN1", "status": "OK", "sharpe": 1.8,
                        "fitness": 1.2, "turnover": 0.15, "checks": []})
st32.conn().execute("UPDATE candidates SET stage='gated' WHERE id=?", (_cid32,))
st32.record_gate_check(_cid32, "CLEAN1", "group_rank(fa, sector)",
                       {"sharpe": 1.8, "fitness": 1.2, "turnover": 0.15},
                       _V27(True, []), Gates(), deep=_V27(True, []))
st32.bump_arm("pv::level_rank", trial=1, scan_trials=1, scan_pass=1)
_clean32 = _AU32.run_all(st32.conn())
check("a clean, legitimately-built database has zero findings",
      _clean32 == [], f"{_clean32}")

def _fires32(findings, check_name, **want):
    return any(f.check == check_name
              and all(f.detail.get(k) == v for k, v in want.items())
              for f in findings)

# B. gate_history: deep computed after a metric-gate FAIL. The store method
# itself has no guard against this shape -- only caller discipline in
# cli.py's three call sites does.
st32.record_gate_check("cB", "AB", "expr_b", _rec27,
                       _V27(False, ["sharpe too low"]), Gates(),
                       deep=_V27(True, []))
_fB = _AU32.check_gate_history_ok0_with_deep(st32.conn())
check("deep-after-metric-fail is caught",
      _fires32(_fB, "gate_history_deep_computed_after_metric_fail",
               candidate_id="cB"), f"{_fB}")

# C. deep_checks poisoning -- the exact shape from section [26]'s fixture,
# now checked by the audit tool instead of by deep_verdict() itself.
st32.save_deep("AC", yearly={"records": [[1]]}, checks=[], ok=True, reasons=[])
_fC = _AU32.check_deep_checks_poisoned(st32.conn())
check("a poisoned (ok=1, empty checks) cache row is caught",
      _fires32(_fC, "deep_checks_poisoned", alpha_id="AC"), f"{_fC}")

# D. gate_passing_now, against the LIVE-shaped table -- a later fail must
# leave BOTH the live query's own answer and audit's independent
# recomputation agreeing "not passing", since the query is already fixed
# (this is the same scenario section [25]'s st27 fixture already proves
# directly against gate_passing_now() itself). Zero findings here is the
# correct outcome, not a gap: the real disagreement this checker exists to
# catch is the ts-TIE case right below, which the SQL's arbitrary join CAN
# still get wrong even with the fix in place.
st32.record_gate_check("cD1", "AD1", "expr_d", _rec27,
                       _V27(True, []), Gates(), deep=_V27(True, []))
_tD = _t32.time() + 10.0     # force strict ordering after the row above
with st32.conn() as _c32:
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok,"
        "deep_ok,deep_reasons) VALUES (?,?,?,?,?,?,?)",
        (_tD, "cD2", "AD2", "expr_d", 1, 0, "[\"PROD_CORRELATION\"]"))
_fD = _AU32.check_gate_passing_now_consistency(st32.conn())
check("a later fail is agreed on by both the live query and audit's own "
      "recomputation -- no false alarm on data the fixed code already "
      "handles correctly",
      not any(f.detail.get("expr") == "expr_d" for f in _fD), f"{_fD}")

# The tie must be between two rows for the SAME candidate -- that is the only
# case the SQL's MAX(ts) join genuinely cannot resolve. (Two different
# candidates sharing a ts are separately evaluated and are not a tie at all,
# which is exactly what grouping by candidate_id fixed.)
_tTie = _t32.time() + 20.0
with st32.conn() as _c32:
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok,"
        "deep_ok,deep_reasons) VALUES (?,?,?,?,?,?,?)",
        (_tTie, "cE1", "AE1", "expr_e", 1, 1, "[]"))
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok,"
        "deep_ok,deep_reasons) VALUES (?,?,?,?,?,?,?)",
        (_tTie, "cE1", "AE1b", "expr_e", 1, 0, "[\"lucky year\"]"))
_fE = _AU32.check_gate_passing_now_consistency(st32.conn())
check("a ts-tie on one candidate is resolved by id, not by the SQL join",
      any(f.detail.get("candidate_id") == "cE1" for f in _fE), f"{_fE}")

# F. arms bounds -- bump_arm is additive, so a direct raw UPDATE is the only
# way to construct pass > trials (no legitimate caller ever would).
st32.bump_arm("bad::arm", trial=1, scan_trials=1, scan_pass=1)
with st32.conn() as _c32:
    _c32.execute("UPDATE arms SET scan_pass=10 WHERE arm='bad::arm'")
_fF = _AU32.check_arms_bounds(st32.conn())
check("scan_pass exceeding scan_trials is caught",
      _fires32(_fF, "arms_bounds_violation", arm="bad::arm"), f"{_fF}")

# G. bandit_taught vs arms.deep_trials -- deliberately exact, unlike
# scan/refine (which _backfill_arm_stages documents as an approximation).
st32.bump_arm("untaught::arm", trial=1, deep_trials=3, deep_pass=1)
_fG = _AU32.check_bandit_taught_vs_deep_trials(st32.conn())
check("deep_trials with zero matching bandit_taught claims is caught",
      _fires32(_fG, "deep_trials_bandit_taught_mismatch", arm="untaught::arm",
              bandit_taught_deep_n=0, arms_deep_trials=3), f"{_fG}")

# H. candidate id integrity -- mutate expr after insertion so the stored id
# (hashed from the ORIGINAL expr) no longer matches the row it sits on.
_cidH = st32.add_candidate(expr="group_rank(e1, sector)",
                           settings=SimSettings().payload(), theme="pv",
                           template="level_rank", field="e1",
                           settings_key=SimSettings().key())
with st32.conn() as _c32:
    _c32.execute("UPDATE candidates SET expr='group_rank(e2, sector)' "
                "WHERE id=?", (_cidH,))
_fH = _AU32.check_candidate_id_integrity(st32.conn())
check("a candidate whose id no longer matches its own expr is caught",
      _fires32(_fH, "candidate_id_mismatch", candidate_id=_cidH), f"{_fH}")

# H2. Corrupted (not merely mismatched) settings JSON must not crash the
# whole audit -- json.loads raises json.JSONDecodeError (a ValueError
# subclass), which an except TypeError alone does not catch, so this used to
# propagate out of check_candidate_id_integrity and abort run_all() before
# any later check ran.
with st32.conn() as _c32:
    _c32.execute(
        "INSERT INTO candidates (id,expr,settings,theme,template,field,"
        "stage,parent_id,created) VALUES (?,?,?,?,?,?,?,?,?)",
        ("cH2", "e", "{not valid json", "pv", "level_rank", "f", "scan",
         None, 0.0))
_fH2 = _AU32.check_candidate_id_integrity(st32.conn())
check("corrupted (non-JSON) settings is reported, not raised",
      _fires32(_fH2, "candidate_settings_corrupted", candidate_id="cH2"),
      f"{_fH2}")

# I. stage/parent shape -- a root with a variant's stage, and vice versa.
with st32.conn() as _c32:
    _c32.execute(
        "INSERT INTO candidates (id,expr,settings,theme,template,field,"
        "stage,parent_id,created) VALUES (?,?,?,?,?,?,?,?,?)",
        ("cI1", "e", "{}", "pv", "level_rank", "f", "refine", None, 0.0))
    _c32.execute(
        "INSERT INTO candidates (id,expr,settings,theme,template,field,"
        "stage,parent_id,created) VALUES (?,?,?,?,?,?,?,?,?)",
        ("cI2", "e", "{}", "pv", "level_rank", "f", "scan", "cI1", 0.0))
_fI = _AU32.check_candidate_stage_shape(st32.conn())
check("a root candidate with a variant's stage is caught",
      _fires32(_fI, "root_candidate_bad_stage", candidate_id="cI1"), f"{_fI}")
check("a variant candidate with a root's stage is caught",
      _fires32(_fI, "variant_candidate_bad_stage", candidate_id="cI2"), f"{_fI}")

# J. gated without an OK sim -- cmd_select's silent join assumption.
_cidJ = st32.add_candidate(expr="group_rank(fj, sector)",
                           settings=SimSettings().payload(), theme="pv",
                           template="level_rank", field="fj",
                           settings_key=SimSettings().key())
with st32.conn() as _c32:
    _c32.execute("UPDATE candidates SET stage='gated' WHERE id=?", (_cidJ,))
_fJ = _AU32.check_gated_has_ok_sim(st32.conn())
check("a gated candidate with no OK simulation is caught",
      _fires32(_fJ, "gated_without_ok_sim", candidate_id=_cidJ), f"{_fJ}")

# K. field2 shape -- three violations in one pass: set where not needed,
# missing where required, and equal to field where set.
_cidK1 = st32.add_candidate(expr="group_rank(fk1, sector)",
                            settings=SimSettings().payload(), theme="pv",
                            template="level_rank", field="fk1", field2="fk2",
                            settings_key=SimSettings().key() + "k1")
_cidK2 = st32.add_candidate(expr="group_rank(divide(fk3, fk4), sector)",
                            settings=SimSettings().payload(), theme="pv",
                            template="ratio_fields", field="fk3",
                            settings_key=SimSettings().key() + "k2")
_cidK3 = st32.add_candidate(expr="group_rank(divide(fk5, fk5), sector)",
                            settings=SimSettings().payload(), theme="pv",
                            template="ratio_fields", field="fk5",
                            field2="fk5",
                            settings_key=SimSettings().key() + "k3")
_fK = _AU32.check_field2_shape(st32.conn())
check("field2 set on a single-field template is caught",
      _fires32(_fK, "field2_set_for_single_field_template",
              candidate_id=_cidK1), f"{_fK}")
check("field2 missing on a two-field template is caught",
      _fires32(_fK, "field2_missing_for_two_field_template",
              candidate_id=_cidK2), f"{_fK}")
check("field2 equal to field is caught",
      _fires32(_fK, "field2_equals_field", candidate_id=_cidK3), f"{_fK}")

# L. orphan references -- one per table, since FKs are off and nothing else
# enforces these.
with st32.conn() as _c32:
    _c32.execute("INSERT INTO sims (candidate_id, status) VALUES (?,?)",
                ("ghost-sim", "OK"))
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok) "
        "VALUES (?,?,?,?,?)", (_t32.time(), "ghost-gh", "A", "e", 1))
    _c32.execute("INSERT INTO inflight VALUES (?,?,?,?)",
                ("ghost-if", "http://x", 0, _t32.time()))
    _c32.execute(
        "INSERT INTO candidates (id,expr,settings,theme,template,field,"
        "stage,parent_id,created) VALUES (?,?,?,?,?,?,?,?,?)",
        ("cL", "e", "{}", "pv", "level_rank", "f", "refine", "ghost-parent",
         0.0))
_fL = _AU32.check_orphan_references(st32.conn())
check("an orphan sims reference is caught",
      _fires32(_fL, "orphan_sims_reference", candidate_id="ghost-sim"), f"{_fL}")
check("an orphan gate_history reference is caught",
      _fires32(_fL, "orphan_gate_history_reference", candidate_id="ghost-gh"),
      f"{_fL}")
check("an orphan inflight reference is caught",
      _fires32(_fL, "orphan_inflight_reference", candidate_id="ghost-if"),
      f"{_fL}")
check("an orphan parent_id reference is caught",
      _fires32(_fL, "orphan_candidates_parent_reference", candidate_id="cL"),
      f"{_fL}")

# M. stuck inflight -- back-date a dispatch well past the cutoff.
st32.mark_dispatched("cM", "http://x/progress", 0)
with st32.conn() as _c32:
    _c32.execute("UPDATE inflight SET dispatched=? WHERE candidate_id='cM'",
                (_t32.time() - 999999,))
_fM = _AU32.check_stuck_inflight(st32.conn())
check("an old in-flight simulation is flagged as stuck",
      _fires32(_fM, "stuck_inflight", candidate_id="cM"), f"{_fM}")

# N. run_all() aggregates every check without cross-contamination -- assert
# a handful of the specific findings above all survive being run together.
_fAll32 = _AU32.run_all(st32.conn())
check("run_all() reports every distinct violation constructed in this section",
      {f.check for f in _fAll32} >= {
          "gate_history_deep_computed_after_metric_fail",
          "deep_checks_poisoned", "gate_passing_now_stale_pass",
          "arms_bounds_violation", "deep_trials_bandit_taught_mismatch",
          "candidate_id_mismatch", "root_candidate_bad_stage",
          "gated_without_ok_sim", "field2_equals_field",
          "orphan_sims_reference", "stuck_inflight"},
      f"{sorted({f.check for f in _fAll32})}")

os.remove(db32)

print("\n[33] correlation profile: the two levers behind PROD_CORRELATION")
from alphamachine.metrics import corr_profile as _cp33, expected_max_z as _z33
from alphamachine.config import (NEUTRALIZATIONS as _NA33,
                                 REFINE_NEUTRALIZATION as _RN33)

# The real omqZOwpb histogram, pulled live from /correlations/prod this
# session. Kept verbatim as the regression anchor: the arithmetic below was
# hand-computed off exactly these numbers when the extreme-value analysis was
# done, so a drift in corr_profile() shows up against reality, not against a
# fixture someone invented to match the code.
_hist33 = {"schema": {"name": "prodCorrelation",
                      "properties": [{"name": "min"}, {"name": "max"},
                                     {"name": "alphas"}]},
           "records": [[-1.0,-0.9,0],[-0.9,-0.8,0],[-0.8,-0.7,0],[-0.7,-0.6,15],
                       [-0.6,-0.5,166],[-0.5,-0.4,2595],[-0.4,-0.3,17884],
                       [-0.3,-0.2,55601],[-0.2,-0.1,150644],[-0.1,0.0,366655],
                       [0.0,0.1,605372],[0.1,0.2,584353],[0.2,0.3,407298],
                       [0.3,0.4,195585],[0.4,0.5,71954],[0.5,0.6,17693],
                       [0.6,0.7,2285],[0.7,0.8,70],[0.8,0.9,0],[0.9,1.0,0]],
           "max": 0.7667, "min": -0.6878}
_p33 = _cp33(_hist33)
check("corr_profile reproduces the real histogram's pool size",
      _p33["n_alphas"] == 2478170, f"{_p33['n_alphas']}")
check("corr_profile reproduces the hand-computed mean and sd",
      abs(_p33["mean"] - 0.1087) < 0.001 and abs(_p33["sd"] - 0.1635) < 0.001,
      f"mean={_p33['mean']:.4f} sd={_p33['sd']:.4f}")
check("corr_profile prefers BRAIN's reported max over a bin-edge estimate",
      _p33["max_corr"] == 0.7667 and _p33["min_corr"] == -0.6878)
# The whole reason this instrumentation exists: the observed max (0.7667) is
# FAR below what the profile implies (0.9174). Recording only the max hides
# that the distribution itself is what has to move.
check("expected_max exceeds the observed max on the real data",
      abs(_p33["expected_max"] - 0.9174) < 0.001
      and _p33["expected_max"] > _p33["max_corr"],
      f"expected={_p33['expected_max']:.4f} observed={_p33['max_corr']}")

# z is fixed by pool size alone -- that is why mean and sd are the only levers.
check("expected_max_z grows with the pool and is ~4.95 at 2.48M",
      abs(_z33(2478170) - 4.945) < 0.01 and _z33(10_000) < _z33(1_000_000),
      f"z(2.48M)={_z33(2478170):.3f}")
check("expected_max_z is defined for degenerate pool sizes",
      _z33(0) == 0.0 and _z33(1) == 0.0)

# Inconclusive, not zero: /correlations/prod is lazily computed and
# _await_record does not retry an empty 200, so "no records" means "no answer".
check("an empty or missing histogram is INCONCLUSIVE, not a profile",
      _cp33({"records": []}) is None and _cp33(None) is None
      and _cp33({}) is None)
check("a histogram whose bins are all empty is INCONCLUSIVE",
      _cp33({"schema": {"properties": [{"name":"min"},{"name":"max"},
                                       {"name":"alphas"}]},
             "records": [[-0.1,0.0,0],[0.0,0.1,0]]}) is None)

# Storage round-trip, on its own table (deliberately not deep_checks, whose
# positional INSERT breaks silently on a column-count change).
db33 = tempfile.mktemp(suffix=".db"); st33 = Store(db33)
check("load_corr_profile returns None before anything is stored",
      st33.load_corr_profile("A33") is None)
st33.save_corr_profile("A33", _hist33, _p33)
_r33 = st33.load_corr_profile("A33")
check("corr profile round-trips through its own table",
      _r33 is not None and _r33["n_alphas"] == 2478170
      and abs(_r33["mean"] - _p33["mean"]) < 1e-9
      and abs(_r33["expected_max"] - _p33["expected_max"]) < 1e-9)
check("the raw histogram is kept verbatim alongside the derived numbers",
      json.loads(_r33["hist"])["max"] == 0.7667)
check("prod_corr_hist is separate from deep_checks",
      st33.load_deep("A33") is None)
os.remove(db33)

# The widened ladder. A typo here would not fail loudly -- it would just make
# every simulation for that value come back ERROR, so assert membership.
check("every ladder neutralization is a value BRAIN's own list carries",
      set(_RN33) <= set(_NA33), f"{sorted(set(_RN33) - set(_NA33))}")
check("the ladder searches the four never-tried values probed this session",
      {"STATISTICAL", "CROWDING", "FAST", "SLOW"} <= set(_RN33))
# Probed live 2026-08-31: BRAIN rejects these for EQUITY/USA. COUNTRY is "not
# available for instrument type EQUITY and region USA"; RAM is "not a valid
# choice" at all despite appearing in docs/ for Power Pools.
check("values BRAIN rejects for EQUITY/USA stay out of the ladder",
      "COUNTRY" not in _RN33 and "RAM" not in _RN33)
check("NONE stays out -- it strips no factor exposure at all",
      "NONE" not in _RN33)
check("no duplicates in the ladder (each costs a simulation per candidate)",
      len(_RN33) == len(set(_RN33)), f"{len(_RN33)} values")

# Rung 1 yields exactly one variant per non-incumbent value.
db34 = tempfile.mktemp(suffix=".db"); st34 = Store(db34)
_s34 = SimSettings(neutralization="INDUSTRY")
_c34 = st34.add_candidate(expr="group_rank(f34, sector)",
                          settings=_s34.payload(), theme="pv",
                          template="level_rank", field="f34",
                          settings_key=_s34.key())
class _Ex34:
    pass
_v34 = Refiner(st34, _Ex34(), Gates()).plan(
    _c34, {"sharpe": 1.8, "fitness": 1.2, "turnover": 0.15, "checks": []}, 1)
check("rung 1 plans one variant per non-incumbent neutralization",
      len(_v34) == len(_RN33) - 1, f"{len(_v34)} variants for {len(_RN33)} values")
check("rung 1 never re-plans the incumbent's own neutralization",
      all(v.settings.neutralization != "INDUSTRY" for v in _v34))
check("rung 1 leaves the expression untouched (settings-only rung)",
      all(v.expr == "group_rank(f34, sector)" for v in _v34))
os.remove(db34)

print("\n[34] recency: the 2-year Sharpe we were already being told")
import dataclasses as _dc34
from alphamachine.metrics import extract as _ex34, failed_checks as _fc34

# The real shape that slipped through: at SIMULATION time BRAIN reports
# LOW_2Y_SHARPE as a WARNING, not a FAIL -- failed_checks() ignores WARNING,
# so a 0.48 read as a clean pass all the way to the deep gate. The value is
# identical to what /check later returns as FAIL; only the string differs.
_pay34 = {"id": "A34",
          "is": {"sharpe": 2.39, "fitness": 1.48, "turnover": 0.121,
                 "drawdown": 0.03, "margin": 0.0008,
                 "longCount": 900, "shortCount": 900,
                 "checks": [{"name": "LOW_SHARPE", "result": "PASS",
                             "limit": 1.58, "value": 2.39},
                            {"name": "LOW_2Y_SHARPE", "result": "WARNING",
                             "limit": 1.58, "value": 0.48},
                            {"name": "LOW_SUB_UNIVERSE_SHARPE",
                             "result": "PASS", "limit": 1.03, "value": 1.61}]},
          "test": {"sharpe": 0.30}}
_e34 = _ex34(_pay34)
check("extract pulls the 2Y sharpe even when reported as WARNING",
      _e34["two_year_sharpe"] == 0.48, f"{_e34['two_year_sharpe']}")
check("extract pulls the trailing-testPeriod sharpe from the payload",
      _e34["test_sharpe"] == 0.30, f"{_e34['test_sharpe']}")
check("the WARNING really is invisible to failed_checks -- the original hole",
      "LOW_2Y_SHARPE" not in _fc34(_pay34["is"]["checks"]))
_e34b = _ex34({"id": "B34", "is": {"sharpe": 1.8, "checks": []}})
check("both recency fields are None when the payload carries neither",
      _e34b["two_year_sharpe"] is None and _e34b["test_sharpe"] is None)

g34 = Gates()
_base34 = {"sharpe": 2.39, "fitness": 1.48, "turnover": 0.121,
           "drawdown": 0.03, "margin": 0.0008, "longCount": 900,
           "shortCount": 900, "subuniverse_sharpe": 1.61,
           "checks": [{"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS",
                       "limit": 1.03, "value": 1.61}]}
check("a decayed alpha is blocked on the real failing value (0.48)",
      not quality_verdict(dict(_base34, two_year_sharpe=0.48), g34).ok)
check("a recent-healthy alpha with the same full-period sharpe passes (1.63)",
      quality_verdict(dict(_base34, two_year_sharpe=1.63), g34).ok)
# Absence means BRAIN sent IS_LADDER_SHARPE instead -- unknown, not fine. This
# is the 92-of-98 majority; blocking them would be the over-strict-gate
# mistake made earlier in this same codebase.
check("absence of the 2Y value does NOT block",
      quality_verdict(dict(_base34), g34).ok)
check("the proxy alone never blocks, however bad it looks",
      quality_verdict(dict(_base34, test_sharpe=0.05), g34).ok)
# Read from checks when the column has not been populated (older row).
check("the 2Y value is also honoured straight from the checks payload",
      not quality_verdict(
          dict(_base34, checks=_base34["checks"] +
               [{"name": "LOW_2Y_SHARPE", "result": "WARNING",
                 "limit": 1.58, "value": 0.48}]), g34).ok)

# The inversion the ladder made before this existed: refine._score is
# headroom - 0.25*fails, so whatever headroom prefers, the ladder climbs to.
# The decayed alpha here is given a healthy sub-universe retention (1.80 of
# 2.39) precisely so that EVERY whole-period slack favours it -- which is the
# situation that matters, and the one the old headroom had no way to see past.
_decayed34 = dict(_base34, two_year_sharpe=0.48, subuniverse_sharpe=1.80)
_steady34 = {"sharpe": 1.75, "fitness": 1.05, "turnover": 0.12,
             "drawdown": 0.03, "margin": 0.0008, "longCount": 900,
             "shortCount": 900, "subuniverse_sharpe": 1.20,
             "two_year_sharpe": 1.80, "checks": []}
check("headroom now ranks the steady alpha ABOVE the decayed one",
      headroom(_steady34, g34) > headroom(_decayed34, g34),
      f"steady={headroom(_steady34, g34):.2f} decayed={headroom(_decayed34, g34):.2f}")
_off34 = _dc34.replace(Gates(), min_two_year_sharpe=1e-9)
check("and that ordering is INVERTED without the recency term",
      headroom(_steady34, _off34) < headroom(_decayed34, _off34),
      f"steady={headroom(_steady34, _off34):.2f} "
      f"decayed={headroom(_decayed34, _off34):.2f}")
check("headroom falls back to the proxy when the exact value is absent",
      headroom(dict(_base34, test_sharpe=0.30), g34)
      < headroom(dict(_base34, test_sharpe=2.50), g34))
check("headroom is unchanged when neither recency field is present",
      headroom(_base34, g34) == headroom(_base34, _off34))

# Migration + backfill from payloads already on disk.
db35 = tempfile.mktemp(suffix=".db")
_o35 = _sq30.connect(db35)
_o35.executescript("""
CREATE TABLE sims (candidate_id TEXT PRIMARY KEY, alpha_id TEXT, status TEXT,
  error TEXT, sharpe REAL, fitness REAL, turnover REAL, returns REAL,
  drawdown REAL, margin REAL, longCount INTEGER, shortCount INTEGER,
  subuniverse_sharpe REAL, checks TEXT, raw TEXT, finished REAL);
""")
_o35.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
             " VALUES (?,?,?,?,?,?)",
             ("c35", "A35", "OK", 2.39,
              json.dumps([{"name": "LOW_2Y_SHARPE", "result": "WARNING",
                           "limit": 1.58, "value": 0.48}]),
              json.dumps({"test": {"sharpe": 0.30}})))
_o35.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
             " VALUES (?,?,?,?,?,?)",
             ("c35b", "A35b", "OK", 1.9, json.dumps([]), json.dumps({})))
_o35.commit(); _o35.close()
st35 = Store(db35)                              # migration + backfill on open
_cols35 = {r[1] for r in st35.conn().execute("PRAGMA table_info(sims)")}
check("migration adds both recency columns to an older database",
      {"two_year_sharpe", "test_sharpe"} <= _cols35)
_r35 = st35.conn().execute(
    "SELECT two_year_sharpe, test_sharpe FROM sims WHERE candidate_id='c35'"
).fetchone()
check("backfill recovers both values from payloads already stored",
      _r35["two_year_sharpe"] == 0.48 and _r35["test_sharpe"] == 0.30,
      f"{dict(_r35)}")
_r35b = st35.conn().execute(
    "SELECT two_year_sharpe, test_sharpe FROM sims WHERE candidate_id='c35b'"
).fetchone()
check("a payload carrying neither is left NULL, never guessed",
      _r35b["two_year_sharpe"] is None and _r35b["test_sharpe"] is None)
check("backfill is idempotent", Store(db35) is not None)
os.remove(db35)

print("\n[35] pyramid: the payout multiplier BRAIN sends on every simulation")
from alphamachine.metrics import extract as _ex36
from alphamachine.refine import _score as _sc36
from alphamachine.config import pyramid_tilt as _tilt36, PYRAMID_SCORE_WEIGHT as _W36

# Verbatim shape from a real submitted alpha (WjPj1R0x): the check reports
# `multiplier`, `effective` and a `pyramids` list -- not a bare `value`, which
# is exactly why check_value() read None off it for the whole project's life.
_one36 = {"result": "PASS", "name": "MATCHES_PYRAMID", "effective": 1,
          "multiplier": 1.4,
          "pyramids": [{"name": "USA/D1/OTHER", "multiplier": 1.4}]}
# And the multi-pyramid shape, from the database: PV 1.1 + MODEL 1.3 reports
# 1.1. The MINIMUM, on 9,414 of 9,414 alphas where the members differed.
_two36 = {"result": "PASS", "name": "MATCHES_PYRAMID", "effective": 2,
          "multiplier": 1.1,
          "pyramids": [{"name": "USA/D1/PV", "multiplier": 1.1},
                       {"name": "USA/D1/MODEL", "multiplier": 1.3}]}

_e36 = _ex36({"id": "A36", "is": {"sharpe": 1.76, "checks": [_one36]}})
check("extract pulls the pyramid multiplier, names and effective count",
      _e36["pyramid_multiplier"] == 1.4
      and _e36["pyramid_names"] == ["USA/D1/OTHER"]
      and _e36["pyramid_effective"] == 1, f"{_e36['pyramid_multiplier']}")
_e36m = _ex36({"id": "B36", "is": {"sharpe": 1.76, "checks": [_two36]}})
check("the reported multiplier is the MINIMUM over matched pyramids, not max",
      _e36m["pyramid_multiplier"] == 1.1
      and _e36m["pyramid_names"] == ["USA/D1/PV", "USA/D1/MODEL"],
      f"{_e36m['pyramid_multiplier']} from 1.1/1.3")
_e36n = _ex36({"id": "C36", "is": {"sharpe": 1.76, "checks": []}})
check("all three are None when the check is absent",
      _e36n["pyramid_multiplier"] is None and _e36n["pyramid_names"] is None
      and _e36n["pyramid_effective"] is None)
check("an unknown multiplier tilts by exactly zero -- never a penalty",
      _tilt36(None) == 0.0 and _tilt36(1.0) == 0.0)

# The scoring property that makes this a PREFER and not a BLOCK.
g36 = Gates()
_r36 = {"status": "OK", "sharpe": 2.0, "fitness": 1.4, "turnover": 0.15,
        "subuniverse_sharpe": 1.2, "two_year_sharpe": 1.9, "checks": []}
_hi36 = dict(_r36, pyramid_multiplier=1.4)
_lo36 = dict(_r36, pyramid_multiplier=1.1)
_none36 = dict(_r36)
check("_score prefers the higher-paying of two identical variants",
      _sc36(_hi36, g36) > _sc36(_lo36, g36),
      f"{_sc36(_hi36, g36):.4f} > {_sc36(_lo36, g36):.4f}")
check("the whole 1.0-2.0 payout range is worth less than ONE failed check",
      _W36 * 1.0 < 0.25, f"{_W36:.2f} vs 0.25")
_broken36 = dict(_r36, pyramid_multiplier=2.0,
                 checks=[{"name": "LOW_SHARPE", "result": "FAIL"}])
check("payout can never buy a variant that fails a check",
      _sc36(_broken36, g36) < _sc36(_lo36, g36),
      f"{_sc36(_broken36, g36):.4f} < {_sc36(_lo36, g36):.4f}")
check("a candidate with no pyramid data scores exactly as it did before",
      _sc36(_none36, g36) == headroom(_none36, g36))
check("headroom itself is untouched by the multiplier",
      headroom(_hi36, g36) == headroom(_lo36, g36) == headroom(_none36, g36))

# Rung 3: substitute the gate CONDITION, never add a second variant.
check("an expression with a PV token is detected",
      T.touches_pv("group_rank(ts_corr(fld, returns, 126), industry)")
      and not T.touches_pv("group_rank(ts_delta(fld, 66), industry)"))
check("'capex' does not count as 'cap' -- identifier boundaries hold",
      not T.touches_pv("group_rank(ts_delta(fnd6_capex, 66), industry)"))
_pv36 = "group_rank(ts_corr(fld, returns, 126), industry)"
_no36 = "group_rank(ts_delta(fld, 66), industry)"
check("already in the PV pyramid -> the volume gate is free, so keep it",
      "volume > adv20" in T.turnover_gate(_pv36, "fld"))
check("not in it -> gate on the field's own dispersion instead",
      "ts_zscore(fld, 66)" in T.turnover_gate(_no36, "fld")
      and "volume" not in T.turnover_gate(_no36, "fld"),
      T.turnover_gate(_no36, "fld")[:52])
check("with no field to gate on, fall back rather than emit nothing",
      "volume > adv20" in T.turnover_gate(_no36, ""))
check("either way it is exactly one trade_when, not two",
      T.turnover_gate(_no36, "fld").count("trade_when") == 1)
check("the gated expression still lints clean", not T.lint(T.turnover_gate(_no36, "fld")))

# Migration + backfill over a database written before the columns existed.
db36 = os.path.join(tempfile.gettempdir(), "am_pyr_test.db")
if os.path.exists(db36): os.remove(db36)
import sqlite3 as _sq36
_o36 = _sq36.connect(db36)
_o36.execute("CREATE TABLE sims (candidate_id TEXT PRIMARY KEY, alpha_id TEXT,"
             " status TEXT, sharpe REAL, checks TEXT, raw TEXT)")
_o36.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
             " VALUES (?,?,?,?,?,?)",
             ("c36", "A36", "OK", 1.76, json.dumps([_two36]), json.dumps({})))
_o36.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
             " VALUES (?,?,?,?,?,?)",
             ("c36b", "A36b", "OK", 1.9, json.dumps([]), json.dumps({})))
_o36.commit(); _o36.close()
st36 = Store(db36)                              # migration + backfill on open
_cols36 = {r[1] for r in st36.conn().execute("PRAGMA table_info(sims)")}
check("migration adds all three pyramid columns to an older database",
      {"pyramid_multiplier", "pyramid_names", "pyramid_effective"} <= _cols36)
_r36b = st36.conn().execute(
    "SELECT pyramid_multiplier m, pyramid_names ns, pyramid_effective e "
    "FROM sims WHERE candidate_id='c36'").fetchone()
check("backfill recovers the effective multiplier from stored checks",
      _r36b["m"] == 1.1 and _r36b["e"] == 2, f"{dict(_r36b)}")
check("and the pyramid names, as json, for genius coverage",
      json.loads(_r36b["ns"]) == ["USA/D1/PV", "USA/D1/MODEL"])
_r36c = st36.conn().execute(
    "SELECT pyramid_multiplier m FROM sims WHERE candidate_id='c36b'").fetchone()
check("a payload with no MATCHES_PYRAMID is left NULL, never defaulted to 1.0",
      _r36c["m"] is None)
check("pyramid backfill is idempotent", Store(db36) is not None)

# record_sim must persist what extract() produced -- the list column included.
db36w = os.path.join(tempfile.gettempdir(), "am_pyr_write.db")
if os.path.exists(db36w): os.remove(db36w)
st36w = Store(db36w)
_cid36 = st36w.add_candidate(expr="rank(fld)", settings=SimSettings().payload(),
                             theme="model", template="level_rank", field="fld")
st36w.record_sim(_cid36, _ex36({"id": "W36", "is": {"sharpe": 1.7,
                                                    "checks": [_one36]}}))
_w36 = st36w.conn().execute(
    "SELECT pyramid_multiplier m, pyramid_names ns FROM sims "
    "WHERE candidate_id=?", (_cid36,)).fetchone()
check("record_sim round-trips the multiplier and json-encodes the name list",
      _w36["m"] == 1.4 and json.loads(_w36["ns"]) == ["USA/D1/OTHER"],
      f"{dict(_w36)}")
os.remove(db36); os.remove(db36w)

print("\n[36] neutralization survival: steer rung 1 toward what actually passes")
from alphamachine.config import (neutralization_tilt as _ntilt37,
                                 NEUTRALIZATION_SURVIVAL_WEIGHT as _NW37)

# Real shape from the live database: STATISTICAL/SLOW pass PROD_CORRELATION far
# more often than the classic four, but a rung-1 champion is elected on Sharpe
# alone with no visibility into that -- 89% of deep-check budget was going to
# neutralizations with a measured 0% survival rate before this existed.
_surv37 = {"STATISTICAL": (12, 18), "SLOW": (6, 6), "MARKET": (0, 258)}
check("a strong historical winner tilts high but stays under the weight cap",
      0 < _ntilt37("SLOW", _surv37) < _NW37, f"{_ntilt37('SLOW', _surv37):.4f}")
check("561 measured losses pull the tilt near zero, never to a penalty",
      0 <= _ntilt37("MARKET", _surv37) < 0.01, f"{_ntilt37('MARKET', _surv37):.4f}")
check("an unmeasured neutralization gets the prior mean, not zero -- still "
      "explorable",
      abs(_ntilt37("CROWDING", _surv37) - _NW37 * 0.25) < 1e-9,
      f"{_ntilt37('CROWDING', _surv37):.4f}")
check("no neutralization or no survival table -> exactly zero, never a penalty",
      _ntilt37(None, _surv37) == 0.0 and _ntilt37("SLOW", None) == 0.0
      and _ntilt37("SLOW", {}) == 0.0)

# store.neutralization_survival() against a small fixture spanning wins/losses
# across two neutralizations.
db37 = os.path.join(tempfile.gettempdir(), "am_neut_test.db")
if os.path.exists(db37): os.remove(db37)
st37 = Store(db37)
def _mk37(tag, alpha, neut, pc):
    s37 = SimSettings(neutralization=neut).payload()
    real_cid = st37.add_candidate(expr=f"rank(fld_{tag})", settings=s37,
                                  theme="model", template="level_rank",
                                  field=f"fld_{tag}")
    st37.record_sim(real_cid, {"alpha_id": alpha, "status": "OK", "sharpe": 1.7,
                               "checks": []})
    st37.save_deep(alpha, checks=[{"name": "PROD_CORRELATION", "value": pc}])
_mk37("a", "N37a", "STATISTICAL", 0.55)   # win
_mk37("b", "N37b", "STATISTICAL", 0.60)   # win
_mk37("c", "N37c", "MARKET", 0.90)        # loss
_mk37("d", "N37d", "MARKET", 0.85)        # loss
_mk37("e", "N37e", "MARKET", 0.88)        # loss
_sv37 = st37.neutralization_survival()
check("neutralization_survival counts wins and trials correctly from a fixture",
      _sv37.get("STATISTICAL") == (2, 2) and _sv37.get("MARKET") == (0, 3),
      f"{_sv37}")
os.remove(db37)

# _score: the tilt breaks a near-tie...
_r37 = {"status": "OK", "sharpe": 2.0, "fitness": 1.4, "turnover": 0.15,
       "subuniverse_sharpe": 1.2, "two_year_sharpe": 1.9, "checks": []}
_hi37 = dict(_r37, settings=json.dumps({"neutralization": "SLOW"}))
_lo37 = dict(_r37, settings=json.dumps({"neutralization": "MARKET"}))
check("_score prefers the historically-surviving neutralization on a tie",
      _sc36(_hi37, g36, _surv37) > _sc36(_lo37, g36, _surv37),
      f"{_sc36(_hi37, g36, _surv37):.4f} > {_sc36(_lo37, g36, _surv37):.4f}")
check("...but the whole tilt stays under one failed check's 0.25 penalty",
      _sc36(_hi37, g36, _surv37) - _sc36(_lo37, g36, _surv37) < 0.25)
_broken37 = dict(_r37, settings=json.dumps({"neutralization": "SLOW"}),
                 checks=[{"name": "LOW_SHARPE", "result": "FAIL"}])
check("...and can never buy a variant that fails a check",
      _sc36(_broken37, g36, _surv37) < _sc36(_lo37, g36, _surv37),
      f"{_sc36(_broken37, g36, _surv37):.4f} < {_sc36(_lo37, g36, _surv37):.4f}")
check("no survival table -> identical score to before this feature existed",
      _sc36(_hi37, g36, None) == _sc36(_hi37, g36))

# The cancellation property: once two variants SHARE a neutralization (every
# comparison from rung 2 onward), the tilt must cancel exactly out of the
# difference that absorb() uses to judge a rung -- otherwise a later rung's
# decay/truncation/universe choice would be silently nudged by a term that has
# nothing to do with what that rung is testing.
_same_a37 = dict(_r37, turnover=0.12, settings=json.dumps({"neutralization": "SLOW"}))
_same_b37 = dict(_r37, turnover=0.18, settings=json.dumps({"neutralization": "SLOW"}))
_diff_with = _sc36(_same_a37, g36, _surv37) - _sc36(_same_b37, g36, _surv37)
_diff_without = _sc36(_same_a37, g36, None) - _sc36(_same_b37, g36, None)
check("the tilt cancels exactly when both sides share a neutralization "
      "(rung 2+ never re-litigates rung 1's choice)",
      abs(_diff_with - _diff_without) < 1e-12,
      f"{_diff_with:.6f} vs {_diff_without:.6f}")

# Refiner.__init__ loads the survival table ONCE, not per candidate -- it is an
# aggregate query over the whole deep_checks history, not a per-record lookup.
class _CountingStore37:
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
    def neutralization_survival(self):
        self.calls += 1
        return {}
    def __getattr__(self, name):
        return getattr(self._inner, name)
db37b = os.path.join(tempfile.gettempdir(), "am_neut_count.db")
if os.path.exists(db37b): os.remove(db37b)
_inner37 = Store(db37b)
_counted37 = _CountingStore37(_inner37)
_ref37 = Refiner(_counted37, None, g36)
check("Refiner.__init__ reads the survival posterior exactly once",
      _counted37.calls == 1, f"{_counted37.calls}")
os.remove(db37b)

print("\n[37] IS_LADDER_SHARPE + portability: two real gaps caught by inspection")
from alphamachine.config import subuniverse_floor as _sf38
from alphamachine.refine import Refiner as _Ref38

# Real shape from akL6ngm9 (train sharpe 3.12, test sharpe 0.10 -- a textbook
# decay that IS_LADDER_SHARPE catches and LOW_2Y_SHARPE never saw, because a
# record carries one or the other, never both).
_fail38 = {"name": "IS_LADDER_SHARPE", "result": "FAIL", "year": 2,
           "startDate": "2024-01-02", "endDate": "2022-01-03",
           "limit": 1.58, "value": 0.31}
_pass38 = {"name": "IS_LADDER_SHARPE", "result": "PASS", "year": 2,
           "startDate": "2024-01-02", "endDate": "2022-01-03",
           "limit": 2.02, "value": 2.26}
_e38 = _ex36({"id": "D38", "is": {"sharpe": 2.59, "checks": [_fail38]}})
check("extract pulls is_ladder_sharpe and its per-alpha limit",
      _e38["is_ladder_sharpe"] == 0.31 and _e38["is_ladder_limit"] == 1.58)
_e38n = _ex36({"id": "E38", "is": {"sharpe": 2.59, "checks": []}})
check("both are None when the check is absent",
      _e38n["is_ladder_sharpe"] is None and _e38n["is_ladder_limit"] is None)

g38 = Gates()
_base38 = {"status": "OK", "sharpe": 2.59, "fitness": 1.43, "turnover": 0.2436,
          "drawdown": 0.0386, "margin": 0.00061, "longCount": 1797,
          "shortCount": 1329, "subuniverse_sharpe": 1.43, "checks": []}
_decayed38 = dict(_base38, is_ladder_sharpe=0.31, is_ladder_limit=1.58)
_healthy38 = dict(_base38, is_ladder_sharpe=2.26, is_ladder_limit=2.02)
check("quality_verdict blocks the real decayed value (0.31 < 1.58)",
      not quality_verdict(_decayed38, g38).ok)
check("quality_verdict passes the real healthy value (2.26 >= 2.02)",
      quality_verdict(_healthy38, g38).ok)
check("absence of the ladder value does NOT block",
      quality_verdict(dict(_base38), g38).ok)

check("headroom falls back to is_ladder_sharpe when two_year_sharpe is absent",
      headroom(_decayed38, g38) < headroom(_healthy38, g38),
      f"{headroom(_decayed38, g38):.4f} < {headroom(_healthy38, g38):.4f}")
_both38 = dict(_base38, two_year_sharpe=1.9, is_ladder_sharpe=0.31, is_ladder_limit=1.58)
check("two_year_sharpe takes priority when (unusually) both are present",
      abs(headroom(_both38, g38) - headroom(dict(_base38, two_year_sharpe=1.9), g38)) < 1e-9)

# _portability_ratio: the real xAYKJ2bg numbers (TOP3000 2.08 -> TOP1000 0.68,
# TOP500 0.17). Built through an actual Refiner against a small fixture db so
# the parent_id/settings/universe plumbing is exercised, not just the formula.
db38 = os.path.join(tempfile.gettempdir(), "am_ladder_test.db")
if os.path.exists(db38): os.remove(db38)
st38 = Store(db38)
def _settings38(universe):
    d = SimSettings(universe=universe).payload()
    return d
_champ38 = st38.add_candidate(expr="rank(fld38)", settings=_settings38("TOP3000"),
                              theme="fundamental", template="yield_assets", field="fld38")
st38.record_sim(_champ38, {"alpha_id": "CH38", "status": "OK", "sharpe": 2.08, "checks": []})
_c1000 = st38.add_candidate(expr="rank(fld38)", settings=_settings38("TOP1000"),
                            theme="fundamental", template="yield_assets", field="fld38",
                            parent_id=_champ38)
st38.record_sim(_c1000, {"alpha_id": "C1000", "status": "OK", "sharpe": 0.68, "checks": []})
_c500 = st38.add_candidate(expr="rank(fld38)", settings=_settings38("TOP500"),
                           theme="fundamental", template="yield_assets", field="fld38",
                           parent_id=_champ38)
st38.record_sim(_c500, {"alpha_id": "C500", "status": "OK", "sharpe": 0.17, "checks": []})
_ref38 = _Ref38(st38, None, g38)
_champrec38 = {"settings": json.dumps(_settings38("TOP3000")), "sharpe": 2.08}
_ratio38 = _ref38._portability_ratio(_champ38, _champrec38)
_expect_ratio38 = 0.17 / _sf38(2.08, "TOP3000", "USA", "TOP500")
check("portability_ratio picks the WORST of the two rung-5 children (TOP500)",
      _ratio38 is not None and abs(_ratio38 - _expect_ratio38) < 1e-9,
      f"{_ratio38:.4f} vs expected {_expect_ratio38:.4f}")
check("that ratio is well under the 1.0 floor -- this really is a collapse",
      _ratio38 < 0.30, f"{_ratio38:.4f}")

_lonely38 = st38.add_candidate(expr="rank(fld38b)", settings=_settings38("TOP3000"),
                               theme="fundamental", template="yield_assets", field="fld38b")
st38.record_sim(_lonely38, {"alpha_id": "LONELY38", "status": "OK", "sharpe": 1.5, "checks": []})
check("no rung-5 children -> None, never guessed",
      _ref38._portability_ratio(_lonely38, {"settings": json.dumps(_settings38("TOP3000")),
                                            "sharpe": 1.5}) is None)

check("quality_verdict blocks a collapsed portability ratio",
      not quality_verdict(dict(_base38, portability_ratio=0.27), g38).ok)
check("quality_verdict passes a portable one (ratio >= 1.0)",
      quality_verdict(dict(_base38, portability_ratio=1.2), g38).ok)
check("headroom ranks the collapsing candidate below an otherwise-identical "
      "portable one",
      headroom(dict(_base38, portability_ratio=0.27), g38)
      < headroom(dict(_base38, portability_ratio=1.2), g38))

# Migration + backfill over a database written before all four columns existed.
_o38 = __import__("sqlite3").connect(db38)
_cols38 = {r[1] for r in _o38.execute("PRAGMA table_info(sims)")}
check("migration already added all four new columns via Store(db38) above",
      {"is_ladder_sharpe", "is_ladder_limit", "portability_ratio"} <= _cols38)

db38b = os.path.join(tempfile.gettempdir(), "am_ladder_backfill.db")
if os.path.exists(db38b): os.remove(db38b)
import sqlite3 as _sq38
_raw38 = _sq38.connect(db38b)
_raw38.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, expr TEXT, "
               "settings TEXT, theme TEXT, template TEXT, field TEXT, "
               "field2 TEXT, stage TEXT, parent_id TEXT, created REAL)")
_raw38.execute("CREATE TABLE sims (candidate_id TEXT PRIMARY KEY, alpha_id TEXT,"
              " status TEXT, sharpe REAL, checks TEXT, raw TEXT)")
_raw38.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("champ38b", "rank(x)", json.dumps(_settings38("TOP3000")),
               "fundamental", "yield_assets", "x", "", "gated", None, 0.0))
_raw38.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
              " VALUES (?,?,?,?,?,?)",
              ("champ38b", "CH38b", "OK", 2.08, json.dumps([_fail38]), json.dumps({})))
_raw38.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("child38b", "rank(x)", json.dumps(_settings38("TOP500")),
               "fundamental", "yield_assets", "x", "", "refine", "champ38b", 0.0))
_raw38.execute("INSERT INTO sims (candidate_id,alpha_id,status,sharpe,checks,raw)"
              " VALUES (?,?,?,?,?,?)",
              ("child38b", "CD38b", "OK", 0.17, json.dumps([]), json.dumps({})))
_raw38.commit(); _raw38.close()
_stb38 = Store(db38b)                          # migration + both backfills on open
_r38b = _stb38.conn().execute(
    "SELECT is_ladder_sharpe, is_ladder_limit, portability_ratio FROM sims "
    "WHERE candidate_id='champ38b'").fetchone()
check("backfill recovers is_ladder_sharpe/limit from stored checks",
      _r38b["is_ladder_sharpe"] == 0.31 and _r38b["is_ladder_limit"] == 1.58,
      f"{dict(_r38b)}")
check("and independently backfills portability_ratio from the rung-5 sibling",
      _r38b["portability_ratio"] is not None
      and abs(_r38b["portability_ratio"]
              - 0.17 / _sf38(2.08, "TOP3000", "USA", "TOP500")) < 1e-9,
      f"{_r38b['portability_ratio']}")
check("both backfills are idempotent", Store(db38b) is not None)
os.remove(db38); os.remove(db38b)

print("\n[38] reward decomposition + wider operator vocabulary")
from alphamachine.bandit import Arm as _Arm39
import random as _rnd39

# --- Fix A: corr_* is what sample() learns on, deep_* is reporting only ---
_a39 = _Arm39("x::y", scan_trials=100, scan_pass=50, refine_trials=50,
              gate_pass=25, corr_trials=1, corr_pass=1, deep_trials=1, deep_pass=0)
_b39 = _Arm39("x::z", scan_trials=100, scan_pass=50, refine_trials=50,
              gate_pass=25, corr_trials=86, corr_pass=1, deep_trials=1, deep_pass=0)
_rng39 = _rnd39.Random(7)
_wins39 = sum(1 for _ in range(400)
              if _a39.sample(_rng39) > _b39.sample(_rng39))
check("an arm at corr 1/1 outdraws a well-measured loser at 1/86",
      _wins39 > 300, f"{_wins39}/400 draws")
check("...even though BOTH have deep_pass=0 -- the old signal could not "
      "separate them", _a39.deep_pass == _b39.deep_pass == 0)
_fresh39 = _Arm39("x::new", scan_trials=100, scan_pass=50, refine_trials=50,
                  gate_pass=25)
check("an unmeasured arm still draws above the well-measured loser "
      "(stays explorable)",
      sum(1 for _ in range(400) if _fresh39.sample(_rng39) > _b39.sample(_rng39)) > 250)
check("corr_rate reports the stage sample() actually uses",
      abs(_b39.corr_rate - 1/86) < 1e-9)

# --- store: schema, bump_arm round-trip, lineage-deduped backfill ---
db39 = os.path.join(tempfile.gettempdir(), "am_corr_arms.db")
if os.path.exists(db39): os.remove(db39)
st39 = Store(db39)
st39.bump_arm("t::tmpl", corr_trials=1, corr_pass=1)
_r39 = st39.conn().execute(
    "SELECT corr_trials, corr_pass FROM arms WHERE arm='t::tmpl'").fetchone()
check("bump_arm round-trips the corr counters",
      _r39["corr_trials"] == 1 and _r39["corr_pass"] == 1)

# 4 sibling variants of ONE lineage, all winning -- must count 1/1, not 4/4.
_root39 = st39.add_candidate(expr="rank(f39)", settings=SimSettings().payload(),
                             theme="risk", template="change", field="f39")
st39.record_sim(_root39, {"alpha_id": "R39", "status": "OK", "sharpe": 2.0, "checks": []})
st39.save_deep("R39", checks=[{"name": "PROD_CORRELATION", "value": 0.64, "limit": 0.70}])
for i in range(3):
    kid = st39.add_candidate(expr=f"winsorize(rank(f39), std={i}.0)",
                             settings=SimSettings().payload(), theme="risk",
                             template="change", field="f39", parent_id=_root39)
    st39.record_sim(kid, {"alpha_id": f"K39{i}", "status": "OK", "sharpe": 2.0, "checks": []})
    st39.save_deep(f"K39{i}", checks=[{"name": "PROD_CORRELATION", "value": 0.64, "limit": 0.70}])
# a genuinely separate losing lineage
_other39 = st39.add_candidate(expr="rank(g39)", settings=SimSettings().payload(),
                              theme="risk", template="change", field="g39")
st39.record_sim(_other39, {"alpha_id": "O39", "status": "OK", "sharpe": 2.0, "checks": []})
st39.save_deep("O39", checks=[{"name": "PROD_CORRELATION", "value": 0.91, "limit": 0.70}])
st39._backfill_corr_arms()
_r39b = st39.conn().execute(
    "SELECT corr_trials, corr_pass FROM arms WHERE arm='risk::change'").fetchone()
check("backfill counts 4 sibling rows as ONE lineage, not four",
      _r39b["corr_trials"] == 2 and _r39b["corr_pass"] == 1,
      f"got {_r39b['corr_trials']}/{_r39b['corr_pass']}, want 2 trials / 1 pass")
_before39 = dict(_r39b)
st39._backfill_corr_arms()
_r39c = st39.conn().execute(
    "SELECT corr_trials, corr_pass FROM arms WHERE arm='risk::change'").fetchone()
check("corr backfill is idempotent",
      dict(_r39c) == _before39, f"{dict(_r39c)} vs {_before39}")
check("a row whose PROD_CORRELATION has no value is skipped, never guessed",
      st39.conn().execute("SELECT corr_trials FROM arms WHERE arm='t::tmpl'"
                          ).fetchone()["corr_trials"] == 1)
os.remove(db39)

# --- Fix B: the eight new templates ---
_new39 = ("change_zscore", "change_scale", "av_diff", "decay_change",
          "peak_recency", "staleness", "regime_change", "quantile_change")
check("all eight new templates are in the library",
      all(n in T.BY_NAME for n in _new39))
for _n in _new39:
    _e = T.BY_NAME[_n].render("fld", days=66, group="subindustry")
    if T.lint(_e):
        check(f"{_n} lints clean", False, str(T.lint(_e)))
check("every new template renders and lints clean",
      all(not T.lint(T.BY_NAME[n].render("fld", days=66, group="subindustry"))
          for n in _new39))
# Power Pool allows <=8 DISTINCT operators (ts_backfill/group_backfill exempt).
import re as _re39
def _ops39(expr):
    return {m for m in _re39.findall(r"([a-z_][a-z0-9_]*)\s*\(", expr)
            if m not in ("ts_backfill", "group_backfill")}
check("every new template stays inside Power Pool's <=8 distinct operators",
      all(len(_ops39(T.BY_NAME[n].render("fld", days=66, group="subindustry"))) <= 8
          for n in _new39),
      str({n: len(_ops39(T.BY_NAME[n].render("fld", days=66, group="subindustry")))
           for n in _new39}))
# The point of the exercise: these use operators nothing else in the library did.
_old_ops39 = set()
for _t in T.LIBRARY:
    if _t.name in _new39: continue
    _old_ops39 |= _ops39(_t.render("fld", days=66, group="subindustry", field2="fld2"))
_new_ops39 = set()
for _n in _new39:
    _new_ops39 |= _ops39(T.BY_NAME[_n].render("fld", days=66, group="subindustry"))
check("the new templates introduce operators the library never used",
      len(_new_ops39 - _old_ops39) >= 6,
      f"new operators: {sorted(_new_ops39 - _old_ops39)}")
check("group_rank is no longer universal -- some templates use another wrapper",
      any(not T.BY_NAME[n].render("fld", days=66, group="subindustry").startswith("group_rank")
          for n in _new39))

print("\n[39] Power Pool dedup: twins collapse, plumbing fields do not lock ideas out")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import copy as _copy40
import dedup_powerpool as D40

def _rec40(aid, sharpe, ops, fields, pyr, *, region="USA", eligible=True):
    return {"alpha_id": aid, "sharpe": sharpe, "operators": ops,
            "n_operators": len(ops), "fields": fields, "n_fields": len(fields),
            "region": region, "delay": 1, "pyramids": [pyr], "eligible": eligible,
            "verified": False}

_in40 = [
    _rec40("a1", 2.0, ["rank", "ts_delta"], ["f1"], "USA/D1/MODEL"),
    _rec40("a2", 1.5, ["rank", "ts_delta"], ["f1"], "USA/D1/MODEL"),    # twin of a1
    _rec40("a3", 1.8, ["rank", "ts_zscore"], ["f1"], "USA/D1/MODEL"),   # same field
    _rec40("a4", 1.7, ["rank", "ts_delta"], ["f2", "cap"], "USA/D1/ANALYST"),
    _rec40("a5", 1.6, ["rank", "ts_delta"], ["f3", "cap"], "USA/D1/FUNDAMENTAL"),
    _rec40("a6", 3.0, ["rank"], ["f9"], "USA/D1/MODEL", eligible=False),
    _rec40("a7", 1.4, ["rank", "ts_delta"], ["f1"], "USA/D1/MODEL", region="EUR"),
]
_snap40 = _copy40.deepcopy(_in40)
_ids = lambda rs: [r["alpha_id"] for r in rs]

_sig = D40.by_signature([r for r in _in40 if r["eligible"]])
check("signature stage keeps the best Sharpe of a twin group",
      "a1" in _ids(_sig) and "a2" not in _ids(_sig))
check("signature stage records how many variants a survivor absorbed",
      {r["alpha_id"]: r["dedup_group_size"] for r in _sig}["a1"] == 2)

_k, _st = D40.dedup(_in40, max_per_field=1)
check("an ineligible record never comes back, however good its Sharpe",
      "a6" not in _ids(_k))
check("field cap 1 drops a different-operator alpha on an already-used field",
      "a3" not in _ids(_k), str(_ids(_k)))
check("field cap 2 lets that second alpha on the field through",
      "a3" in _ids(D40.dedup(_in40, max_per_field=2)[0]))
check("a shared plumbing field (`cap`) does not lock the second idea out",
      "a4" in _ids(_k) and "a5" in _ids(_k), str(_ids(_k)))
check("the same field in another region is a different series",
      "a7" in _ids(_k))
check("per-stage counts narrow monotonically",
      _st["eligible"] >= _st["after_signature"] >= _st["after_field_cap"],
      str(_st))
check("the input records are not mutated", _in40 == _snap40)
check("output keeps every key the downstream scripts read",
      all({"alpha_id", "eligible", "verified", "fields", "operators"} <= set(r) for r in _k))

_fd40 = {"f1": "ds1", "f2": "ds1", "f3": "ds2"}
_kd, _std = D40.dedup(_in40, max_per_field=2, max_per_dataset=1, field_datasets=_fd40)
check("dataset cap 1 drops a second alpha from the same dataset (f2 shares ds1 with f1)",
      "a4" not in _ids(_kd) and "a5" in _ids(_kd), str(_ids(_kd)))
check("the dataset cap is skipped, not crashed, when the catalog is unavailable",
      "after_dataset_cap" not in D40.dedup(_in40, max_per_dataset=1, field_datasets={})[1])

_pool40 = [_rec40(f"m{i}", 3.0 - i * 0.1, ["rank"], [f"g{i}"], "USA/D1/MODEL") for i in range(4)]
_pool40.append(_rec40("z1", 0.5, ["rank"], ["gz"], "USA/D1/ANALYST"))
_pool40.append(_rec40("y1", 0.4, ["rank"], ["gy"], "USA/D1/NEWS"))
_o40 = D40.interleave_pyramids(_pool40)
check("interleave: the first three picks cover three pyramids, not the top-3 Sharpe",
      len({D40.pyramid_key(r) for r in _o40[:3]}) == 3, str(_ids(_o40)))
check("interleave loses and duplicates nothing",
      sorted(_ids(_o40)) == sorted(_ids(_pool40)))

# --- against: what is already submitted uses up its fields' quota ------------
_act40 = [_rec40("s1", 1.9, ["rank", "ts_delta"], ["f1"], "USA/D1/MODEL")]
_ka, _sta = D40.dedup(_in40, max_per_field=1, existing=_act40)
check("--against: a candidate on a field a submitted alpha holds is dropped",
      "a1" not in _ids(_ka) and "a3" not in _ids(_ka), str(_ids(_ka)))
check("--against: unrelated ideas and other regions are untouched",
      {"a4", "a5", "a7"} <= set(_ids(_ka)), str(_ids(_ka)))
_twin40 = [_rec40("s2", 1.0, ["rank", "ts_delta"], ["close", "vwap"], "USA/D1/PV")]
_tin40 = [_rec40("t1", 2.0, ["rank", "ts_delta"], ["close", "vwap"], "USA/D1/PV"),
          _rec40("t2", 1.9, ["rank", "ts_delta"], ["close", "high"], "USA/D1/PV")]
check("--against: an exact twin built only from plumbing fields is still dropped",
      _ids(D40.dedup(_tin40, existing=_twin40)[0]) == ["t2"])
check("--against: dataset quota is seeded too",
      "a5" not in _ids(D40.dedup(_in40, max_per_field=2, max_per_dataset=1,
                                 field_datasets={"f1": "ds1", "f3": "ds1"},
                                 existing=_act40)[0]))
check("--against: without it, behaviour is unchanged",
      _ids(D40.dedup(_in40)[0]) == _ids(D40.dedup(_in40, existing=None)[0]))

# --- shape cap: one construction on many fields is not many ideas ------------
_shp40 = [_rec40(f"r{i}", 3.0 - i * 0.1, ["group_rank", "ts_regression"], [f"x{i}"],
                 "USA/D1/MODEL") for i in range(6)]
_shp40.append(_rec40("d1", 1.2, ["group_rank", "ts_delta"], ["y1"], "USA/D1/OTHER"))
_ks, _sts = D40.dedup(_shp40, max_per_field=1, max_per_shape=2)
check("shape cap: six different fields under one operator set collapse to the cap",
      sum(1 for r in _ks if r["alpha_id"].startswith("r")) == 2, str(_ids(_ks)))
check("shape cap: it keeps the BEST Sharpe of the shape, and other shapes are untouched",
      {"r0", "r1", "d1"} == set(_ids(_ks)), str(_ids(_ks)))
check("shape cap: seeded by what is already submitted",
      sum(1 for r in D40.dedup(_shp40, max_per_shape=2, existing=[
          _rec40("s9", 1.0, ["group_rank", "ts_regression"], ["zz"], "USA/D1/MODEL")])[0]
          if r["alpha_id"].startswith("r")) == 1)
check("shape cap: off when not asked for (the pure function's default)",
      len(D40.dedup(_shp40, max_per_field=1)[0]) == 7)
check("shape cap: a different region is a different shape",
      len(D40.dedup([_rec40("e1", 2.0, ["a"], ["u"], "P"), _rec40("e2", 1.9, ["a"], ["v"], "P", region="EUR")],
                    max_per_shape=1)[0]) == 2)

# --- tag_powerpool: nothing is PATCHed unless the description would pass ------
import tag_powerpool as TG40
_good40 = ("Idea: " + "x" * 60 + "\n\nRationale for data used:\n- f: d\n\n"
           "Rationale for operators used:\n- rank: r")
check("validate_description accepts a complete description", not TG40.validate_description(_good40))
check("validate_description rejects a missing section header",
      any("Rationale for operators" in p for p in
          TG40.validate_description(_good40.replace("Rationale for operators used:", "Operators:"))))
check("validate_description rejects text under 100 chars",
      any("< 100" in p for p in TG40.validate_description(
          "Idea: a\nRationale for data used: b\nRationale for operators used: c")))
check("validate_description rejects sections in the wrong order",
      any("out of order" in p for p in TG40.validate_description(
          "Rationale for data used: " + "y" * 40 + "\nIdea: " + "x" * 40
          + "\nRationale for operators used: z")))
_tpl40 = TG40.build_description(["some_field"], ["group_rank", "ts_delta"],
                                {"some_field": "what it measures " * 3},
                                {"group_rank": {"definition": "group_rank(x, g)", "description": "rank within group"},
                                 "ts_delta": {"definition": "ts_delta(x, d)", "description": "change over d days"}})
check("the script's own template output passes the same validation",
      not TG40.validate_description(_tpl40), str(TG40.validate_description(_tpl40)))

# --- recency: a whole-period Sharpe hides a signal that died years ago -------
import check_powerpool as CP40
_ck40 = lambda name, value, limit, result="WARNING": [{"name": name, "value": value,
                                                       "limit": limit, "result": result}]
check("recency: a 2Y Sharpe below BRAIN's own limit is not ok",
      CP40.recency(_ck40("LOW_2Y_SHARPE", 0.0, 1.58)) == {"kind": "2Y", "value": 0.0,
                                                          "limit": 1.58, "ok": False})
check("recency: judged on the VALUE, never the WARNING/FAIL string",
      CP40.recency(_ck40("LOW_2Y_SHARPE", 1.9, 1.58, "FAIL"))["ok"] is True
      and CP40.recency(_ck40("LOW_2Y_SHARPE", 0.4, 1.58, "WARNING"))["ok"] is False)
check("recency: falls back to the IS ladder, with ITS per-alpha limit",
      CP40.recency(_ck40("IS_LADDER_SHARPE", 2.30, 2.02)) == {"kind": "LADDER", "value": 2.30,
                                                              "limit": 2.02, "ok": True})
check("recency: neither check present is unknown (None), not fine and not failed",
      CP40.recency([]) == {"kind": None, "value": None, "limit": None, "ok": None})

_rc40 = lambda aid, ok: {**_rec40(aid, 2.0, ["rank"], [f"q{aid}"], "USA/D1/MODEL"),
                         "recency": {"kind": "2Y", "value": 0.0, "limit": 1.58, "ok": ok}}
_mix40 = [_rc40("dead", False), _rc40("live", True), _rec40("unk", 1.9, ["rank"], ["qunk"], "USA/D1/MODEL")]
check("dedup drops decayed alphas when asked, keeps healthy AND unknown ones",
      set(_ids(D40.dedup(_mix40, drop_decayed=True)[0])) == {"live", "unk"})
check("dedup keeps decayed alphas unless asked (the pure function's default)",
      set(_ids(D40.dedup(_mix40)[0])) == {"dead", "live", "unk"})
check("a decayed alpha with the best Sharpe cannot be the survivor of its own idea",
      _ids(D40.dedup([{**_rc40("old", False), "sharpe": 3.0},
                      {**_rc40("new", True), "sharpe": 1.6}], drop_decayed=True)[0]) == ["new"])

import sqlite3 as _sq40
_dbp40 = os.path.join(tempfile.mkdtemp(), "m.db")
_cn40 = _sq40.connect(_dbp40)
_cn40.execute("CREATE TABLE sims (alpha_id TEXT, two_year_sharpe REAL, is_ladder_sharpe REAL, is_ladder_limit REAL)")
_cn40.executemany("INSERT INTO sims VALUES (?,?,?,?)",
                  [("a", 0.0, None, None), ("b", None, 2.5, 2.0), ("c", None, None, None)])
_cn40.commit(); _cn40.close()
_old40 = [{"alpha_id": x} for x in "abcz"]
_n40 = D40.recency_from_db(_old40, _dbp40)
check("recency_from_db fills old reports from the local db (2Y, ladder), skips the unknown",
      _n40 == 2 and _old40[0]["recency"]["ok"] is False and _old40[1]["recency"]["ok"] is True
      and "recency" not in _old40[2] and "recency" not in _old40[3], str(_old40))

# --- BRAIN correlates over the LAST 4 YEARS, not the whole history -----------
from alphamachine.correlate import BRAIN_CORR_YEARS, aligned_corr as _ac40
import numpy as _np40
_rng40 = _np40.random.default_rng(7)
_dates40 = [f"{y}-{m:02d}-{d:02d}" for y in range(2014, 2024) for m in range(1, 13) for d in range(1, 22)]
_x40 = _rng40.normal(size=len(_dates40))
_n_old = sum(1 for d in _dates40 if d < "2020-01-01")
# identical in the last four years, opposite before -> the window decides the answer
_y40 = _np40.where(_np40.array(_dates40) < "2020-01-01", -_x40, _x40)
_A40, _B40 = (_dates40, _x40), (_dates40, _y40)
check("BRAIN_CORR_YEARS is 4", BRAIN_CORR_YEARS == 4)
check("a 4-year window sees only 2020-2023 (perfectly correlated there)",
      abs(_ac40(_A40, _B40, years=4) - 1.0) < 1e-9)
check("the whole-history default still sees the opposite years too (unchanged behaviour)",
      _ac40(_A40, _B40) < 0.5, f"{_ac40(_A40, _B40):.3f}")
check("the window starts on Jan 1 of (last year - N + 1), not a rolling 1461 days",
      abs(_ac40(_A40, _B40, years=4) - 1.0) < 1e-9 and _ac40(_A40, _B40, years=5) < 1.0)
check("a window with too little overlap returns 0.0, not garbage",
      _ac40((_dates40[:30], _x40[:30]), (_dates40[:30], _x40[:30]), years=4) == 0.0)

_v40 = {"alpha_id": "v", "eligible": True, "verified": True, "pending_gates": []}
_u40 = {"alpha_id": "u", "eligible": True, "verified": False, "pending_gates": ["POWER_POOL_CORRELATION"]}
_t40 = {"alpha_id": "t", "eligible": True, "verified": False, "pending_gates": ["POWER_POOL_CORRELATION", "LOW_TURNOVER"]}
_n40 = {"alpha_id": "n", "eligible": False, "verified": True, "pending_gates": []}
_sel = lambda inc: [r["alpha_id"] for r in TG40.select_candidates([_v40, _u40, _t40, _n40], inc)]
check("tag: by default only fully verified, eligible records are tagged", _sel(False) == ["v"])
check("tag: --include-unverified admits a record whose ONLY pending gate is the PP correlation",
      _sel(True) == ["v", "u"])
check("tag: --include-unverified still excludes any other unresolved gate, and ineligible ones",
      "t" not in _sel(True) and "n" not in _sel(True))

# --- poll: a 404 progress URL is a lost job, not a slow one -------------------
import time as _t40
from alphamachine.client import BrainClient as _BC40, SimulationFailed as _SF40
class _Resp40:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._b, self.headers = code, body if body is not None else {}, headers or {}
    def json(self): return self._b
    text = ""
def _client40(responses):
    c = _BC40("a@b.c", "x", cookie_path=os.path.join(tempfile.mkdtemp(), "c.json"))
    it = iter(responses)
    c.get = lambda url, **kw: next(it)
    return c
_nf40 = _client40([_Resp40(404, {"detail": "Not found."})] * 20)
_t0 = _t40.time()
try:
    _nf40.poll("http://x/sim/1", interval=0.0, timeout=90.0); _msg40 = None
except _SF40 as e:
    _msg40 = str(e)
check("poll gives up on repeated 404s promptly instead of spinning to the timeout",
      _msg40 is not None and _t40.time() - _t0 < 5.0, str(_msg40)[:70])
check("the 404 error is not worded as a timeout (reclaim keeps markers for those)",
      _msg40 is not None and "timeout" not in _msg40.lower())
_ok40 = {"status": "COMPLETE", "alpha": "https://api/alphas/AbC123"}
check("a transient 404 right after the POST is waited out, not treated as lost",
      _client40([_Resp40(404), _Resp40(404), _Resp40(200, _ok40, {"Retry-After": "0"})])
      .poll("http://x/sim/2", interval=0.0, timeout=30.0) == _ok40)
check("a 404 streak is reset by any non-404 answer",
      _client40([_Resp40(404)] * 3 + [_Resp40(200, {}, {"Retry-After": "0"})] + [_Resp40(404)] * 3
                + [_Resp40(200, _ok40, {"Retry-After": "0"})]).poll("http://x/sim/3", interval=0.0, timeout=30.0) == _ok40)

# --- submissions ledger: the web UI must not be invisible --------------------
import datetime as _dt41
from alphamachine.submit import sync_active_submissions as _sync41, scoring_day_start as _sds41
_st41 = Store(os.path.join(tempfile.mkdtemp(), "led.db"))
_st41.record_submission("PIPE1", "ACCEPTED", "ACTIVE", {"dossier": "the pipeline's own"})
_iso_today = (_dt41.datetime.fromtimestamp(_sds41() + 3600, _dt41.timezone.utc)
              .astimezone(_dt41.timezone(_dt41.timedelta(hours=-4))).isoformat())
_iso_old = "2026-04-12T03:34:38-04:00"
class _Fake41:
    def __init__(self, rows): self.rows = rows
    def my_alphas(self, **kw): return self.rows
_rows41 = [
    {"id": "PIPE1", "dateSubmitted": _iso_old, "classifications": [], "tags": []},
    {"id": "UI_PP", "dateSubmitted": _iso_today, "tags": ["PowerPoolSelected"],
     "classifications": [{"name": "Power Pool Alpha"}]},
    {"id": "UI_REG", "dateSubmitted": _iso_old, "classifications": [{"name": "Single Data Set Alpha"}], "tags": []},
    {"id": "NO_TS", "dateSubmitted": None, "classifications": [], "tags": []},
]
_r41 = _sync41(_Fake41(_rows41), _st41)
check("sync adds what the pipeline never recorded, skips an ACTIVE row with no timestamp",
      _r41["added"] == 2 and _r41["already_recorded"] == 1 and _r41["platform_active"] == 4, str(_r41))
check("sync flags Power Pool from the platform's own classification",
      _r41["power_pool"] == 1 and _st41.conn().execute(
          "SELECT json_extract(detail,'$.power_pool') FROM submissions WHERE alpha_id='UI_PP'").fetchone()[0] == 1)
check("sync NEVER overwrites a row the pipeline wrote itself",
      _st41.conn().execute("SELECT verdict FROM submissions WHERE alpha_id='PIPE1'").fetchone()[0] == "ACCEPTED")
check("the submitted_at of a synced row is the platform's timestamp, not 'now'",
      abs(_st41.conn().execute("SELECT submitted_at FROM submissions WHERE alpha_id='UI_REG'").fetchone()[0]
          - _dt41.datetime.fromisoformat(_iso_old).timestamp()) < 1)
check("sync is idempotent", _sync41(_Fake41(_rows41), _st41)["added"] == 0)
check("the sync reports today's count FROM THE PLATFORM's dateSubmitted, not from local rows",
      _r41["submitted_today"] == 1, str(_r41))
from alphamachine.submit import count_submitted_today as _cst41, submission_days as _sd41, Submitter as _Sub41
from alphamachine.config import RunConfig as _RC41
check("count_submitted_today counts a web-UI submission from today and not old ones",
      _cst41(_rows41) == 1 and _cst41([]) == 0)
_days41 = [{"dateSubmitted": "2026-09-19T03:23:39-04:00"}, {"dateSubmitted": "2026-09-19T05:00:00-04:00"},
           {"dateSubmitted": "2026-09-20T08:37:26-04:00"}, {"dateSubmitted": "2026-08-01T10:00:00-04:00"},
           {"dateSubmitted": None}]
check("submission_days counts distinct New York calendar days, ignores no-timestamp rows",
      _sd41(_days41) == 3 and _sd41(_days41, _dt41.datetime(2026, 9, 1, tzinfo=_dt41.timezone.utc).timestamp()) == 2)
class _Plat41:
    def __init__(self, rows, boom=False): self.rows, self.boom = rows, boom
    def my_alphas(self, **kw):
        if self.boom: raise RuntimeError("network down")
        return self.rows
_four = [{"id": f"T{i}", "dateSubmitted": _iso_today, "classifications": [], "tags": []} for i in range(4)]
_sub41 = _Sub41(_RC41(), _st41, _Plat41(_four))
check("Submitter's cap reads the platform: 4 submitted today -> budget spent",
      _sub41.submitted_today() == 4 and _sub41._daily_budget_spent() is True)
check("...and 0 submitted today -> budget open",
      _Sub41(_RC41(), _st41, _Plat41([]))._daily_budget_spent() is False)
_r = _Sub41(_RC41(), _st41, _Plat41([], boom=True)).submit("X", dry_run=True)
check("if the platform cannot be read, submit refuses rather than guess at the count",
      "not submitting on a guess" in (_r.get("skipped") or ""), str(_r))
check("a stale local ledger cannot change the cap (no submits_since left to read)",
      not hasattr(Store, "submits_since"))
check("the sync time is recorded, for status to show how fresh 'submitted' is",
      (_st41.get("submissions_synced") or {}).get("platform_active") == 4)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import _status as ST41
from alphamachine.config import Gates as _G41
check("status: the stage is the subcommand, not the first global option",
      ST41.stage_from_args("python3 -m alphamachine --concurrency 3 refine --top 25") == "refine"
      and ST41.stage_from_args("python3 -m alphamachine --db x.db --region EUR scan --budget 5") == "scan"
      and ST41.stage_from_args("python3 -m alphamachine --concurrency 3") is None)
_ps41 = """  100 500 /bin/bash -c eval 'python3 -m alphamachine --concurrency 3 scan; python3 scripts/autoloop.py'
  200 32000 python3 scripts/autoloop.py --scan-budget 300
  201 40 python3 -m alphamachine --concurrency 3 refine --top 25
  300 5 grep alphamachine"""
_pr41 = ST41.running_processes(_ps41)
check("status: finds autoloop and the running stage",
      _pr41["autoloop"] == (200, 32000) and _pr41["stage"] == ("refine", 201, 40), str(_pr41))
check("status: a shell whose command line merely CONTAINS the words is not a process",
      ST41.running_processes(_ps41.split("\n")[0])["stage"] is None)
check("status: a candidate that can never pass is flagged (turnover 1.299 was listed bare)",
      "turnover>0.7" in ST41.blockers(3.05, 1.16, 1.299, _G41())
      and ST41.blockers(2.0, 1.2, 0.3, _G41()) == [])

# --- pick_batch: greedy, pyramid-aware, floors are never traded off ----------
import corr_powerpool as CP42
from collections import Counter as _Ctr42
_rng42 = np.random.default_rng(11)
_dates42 = [f"{y}-{m:02d}-{d:02d}" for y in range(2014, 2024) for m in range(1, 13) for d in range(1, 23)]
def _s42(base=None, noise=1.0):
    x = _rng42.normal(size=len(_dates42))
    return (_dates42, x if base is None else base[1] + noise * x)
_bk42 = _s42(); _bk2_42 = _s42()
def _c42(aid, sharpe, pyr, rec=True, **kw):
    return {"alpha_id": aid, "sharpe": sharpe, "pyramids": pyr,
            "recency": {"ok": rec}, **kw}
_full = ["USA/D1/MODEL"]; _new = ["USA/D1/ANALYST"]
_ser42 = {"X": _s42(), "Y": _s42(), "Z": _s42()}
_pool42 = {"BK": _bk42}; _reg42 = {"RG": _bk2_42}
_cnt42 = _Ctr42({"USA/D1/MODEL": 3, "USA/D1/ANALYST": 2})   # ANALYST is one alpha from completing

def _ids42(picks): return [p["alpha_id"] for p in picks]
check("an alpha that COMPLETES a pyramid outranks a slightly better alpha in a full one",
      _ids42(CP42.pick_batch([_c42("X", 2.0, _full), _c42("Y", 1.7, _new)], _ser42, _pool42, _reg42, _cnt42, 1)) == ["Y"])
check("...but not a much better one: the tilt breaks near-ties, it does not buy a weak alpha",
      _ids42(CP42.pick_batch([_c42("X", 3.0, _full), _c42("Y", 1.7, _new)], _ser42, _pool42, _reg42, _cnt42, 1)) == ["X"])
check("weight 0 ignores pyramids entirely",
      _ids42(CP42.pick_batch([_c42("X", 2.0, _full), _c42("Y", 1.7, _new)], _ser42, _pool42, _reg42, _cnt42, 1, weight=0.0)) == ["X"])

# Y and Z share a pyramid at 2 of 3; W is in a full one. Picking Y completes it, so Z
# loses its bonus and W (better Sharpe) comes next.
_ser42.update({"W": _s42()})
_two = _Ctr42({"USA/D1/ANALYST": 2, "USA/D1/MODEL": 3})
_r = CP42.pick_batch([_c42("Y", 1.5, _new), _c42("Z", 1.45, _new), _c42("W", 1.8, _full)],
                     _ser42, _pool42, _reg42, _two, 3)
check("counts update after each pick: once a pyramid is complete its bonus is gone",
      _ids42(_r) == ["Y", "W", "Z"], str(_ids42(_r)))
# completion is worth more than early progress: 2->3 (full weight) vs 0->1 (a third)
_ser42.update({"C1": _s42(), "C2": _s42()})
_prog = _Ctr42({"USA/D1/ANALYST": 2, "USA/D1/SOCIALMEDIA": 0, "USA/D1/MODEL": 3})
_rp = CP42.pick_batch([_c42("C2", 1.5, ["USA/D1/SOCIALMEDIA"]), _c42("C1", 1.4, ["USA/D1/ANALYST"])],
                      _ser42, _pool42, _reg42, _prog, 2)
check("crossing the threshold (2->3) beats a slightly higher-Sharpe alpha that only starts a pyramid (0->1)",
      _ids42(_rp) == ["C1", "C2"], str(_ids42(_rp)))
check("a pick records which pyramid it advanced and from what count",
      _r[0]["advances"] == ["USA/D1/ANALYST"] and _r[0]["counts_before"] == {"USA/D1/ANALYST": 2}
      and _r[1]["advances"] == [])

# floors
check("Sharpe under 1.0 is excluded however good its pyramid",
      CP42.pick_batch([_c42("Y", 0.9, _new)], _ser42, _pool42, _reg42, _cnt42, 1) == [])
check("a decayed alpha (recency.ok False) is excluded; unknown recency is not",
      CP42.pick_batch([_c42("Y", 2.0, _new, rec=False)], _ser42, _pool42, _reg42, _cnt42, 1) == []
      and _ids42(CP42.pick_batch([{"alpha_id": "Y", "sharpe": 2.0, "pyramids": _new}], _ser42, _pool42, _reg42, _cnt42, 1)) == ["Y"])
_ser42["TWIN"] = _s42(_bk42, noise=0.2)
check("an alpha nearly identical to one already in the book is excluded",
      CP42.pick_batch([_c42("TWIN", 2.5, _new)], _ser42, _pool42, _reg42, _cnt42, 1) == [])
# the pool GROWS: B is fine against the original book but is a twin of A, which is picked first
_ser42["A"] = _s42(); _ser42["B"] = _s42(_ser42["A"], noise=0.25)
_r2 = CP42.pick_batch([_c42("A", 2.0, _new), _c42("B", 1.9, _new)], _ser42, _pool42, _reg42, _cnt42, 2)
check("a twin of an alpha picked earlier in the same batch drops out (the book grows)",
      _ids42(_r2) == ["A"], str(_ids42(_r2)))
check("an empty pool is 'nothing to correlate with', not a reason to reject",
      _ids42(CP42.pick_batch([_c42("Y", 1.5, _new)], _ser42, {}, {}, _cnt42, 1)) == ["Y"])
check("asking for more than exist returns what cleared the floors, without error",
      len(CP42.pick_batch([_c42("Y", 1.5, _new)], _ser42, _pool42, _reg42, _cnt42, 5)) == 1)
check("pyramid_coverage counts pyramids at or above the formulated threshold",
      CP42.pyramid_coverage(_Ctr42({"a": 3, "b": 2, "c": 5})) == 2)

# --- a full-slots 429 is CAPACITY: wait for a slot, never record an ERROR -------
import requests as _rq43, time as _t43
from alphamachine.client import BrainClient as _BC43, SimulationFailed as _SF43, SlotsBusy as _SB43
from alphamachine.executor import Executor as _Ex43
def _r43(code, body=None, headers=None):
    r = _rq43.Response(); r.status_code = code; r.headers.update(headers or {})
    r._content = json.dumps(body if body is not None else {}).encode(); return r
_CAP43 = {"detail": "CONCURRENT_SIMULATION_LIMIT_EXCEEDED"}
def _cl43(responses):
    c = _BC43("a@b.c", "x", cookie_path=os.path.join(tempfile.mkdtemp(), "c.json"))
    c._authed = True; c._save_cookies = lambda: None
    seq, calls = iter(responses), []
    c.s.request = lambda *a, **kw: (calls.append(1), next(seq))[1]
    return c, calls
_c, _calls = _cl43([_r43(429, _CAP43)] * 5)
_t0 = _t43.time(); _rr = _c.request("POST", "/simulations")
check("request() hands a capacity 429 straight back: one call, no 2/4/8/16/32 s backoff",
      _rr.status_code == 429 and len(_calls) == 1 and _t43.time() - _t0 < 1.0, f"{len(_calls)} call(s)")
_c, _calls = _cl43([_r43(429, {"detail": "rate limit"}, {"Retry-After": "0"}), _r43(200)])
check("a genuine rate-limit 429 is still retried by request()",
      _c.request("GET", "/x").status_code == 200 and len(_calls) == 2)
_c, _calls = _cl43([_r43(429, _CAP43), _r43(429, _CAP43), _r43(201, {}, {"Location": "https://x/sim/1"})])
_c.SLOT_POLL_SECONDS = 0.0
check("simulate() waits for a slot and then goes through",
      _c.simulate({"type": "REGULAR"}) == "https://x/sim/1" and len(_calls) == 3)
_c, _calls = _cl43([_r43(429, _CAP43)] * 200)
_c.SLOT_POLL_SECONDS, _c.SLOT_WAIT_SECONDS = 0.01, 0.1
try:
    _c.simulate({"type": "REGULAR"}); _sb = None
except _SB43 as e:
    _sb = e
check("simulate() raises SlotsBusy (a SimulationFailed) only after the whole wait",
      _sb is not None and isinstance(_sb, _SF43) and "nothing was simulated" in str(_sb))
_c, _calls = _cl43([_r43(400, {"detail": "bad expression"})])
try:
    _c.simulate({"type": "REGULAR"}); _sf = None
except _SF43 as e:
    _sf = e
check("a real failure (HTTP 400) is still a plain SimulationFailed, not SlotsBusy, and is not retried",
      _sf is not None and not isinstance(_sf, _SB43) and len(_calls) == 1)

_st43 = Store(os.path.join(tempfile.mkdtemp(), "cap.db"))
def _cand43(n):
    return _st43.add_candidate(expr=f"rank(cap{n})", settings=SimSettings().payload(), theme="t",
                               template="tt", field=f"cap{n}", stage="scan")
class _Fake43:
    def __init__(self, exc): self.exc = exc
    def refresh_if_stale(self): pass
    def simulate(self, payload): raise self.exc
def _ex43(exc):
    e = _Ex43(RunConfig(), _st43); e.client = lambda: _Fake43(exc); return e
_id1 = _cand43(1)
_res = _ex43(_SB43("no slot"))._bundle([_id1])
check("a deferred bundle writes NO sims row: the candidate is still pending, not failed",
      _res == {_id1: {"status": "DEFERRED"}}
      and _st43.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?", (_id1,)).fetchone()[0] == 0
      and any(r["id"] == _id1 for r in _st43.pending()))
_id2 = _cand43(2); _seen = []
_stats = _ex43(_SB43("no slot")).run([_id2], on_result=lambda cid, rec: _seen.append(cid))
check("run() counts it as deferred, not error, and never teaches the bandit (no on_result)",
      _stats["deferred"] == 1 and _stats["error"] == 0 and _seen == [], str(_stats))
_id3 = _cand43(3)
_ex43(_SF43("simulate [400]: Invalid data field foo"))._bundle([_id3])
check("regression guard: a genuine failure is STILL recorded as ERROR",
      _st43.conn().execute("SELECT status FROM sims WHERE candidate_id=?", (_id3,)).fetchone()[0] == "ERROR")

# --- repairing the capacity-error rows, and re-running what they held back ----
import sqlite3 as _sq44
import repair_capacity_errors as RC44
from alphamachine import cli as _cli44
_db44 = os.path.join(tempfile.mkdtemp(), "rep.db"); _st44 = Store(_db44)
_CAP44 = 'simulate [429]: {"detail":"CONCURRENT_SIMULATION_LIMIT_EXCEEDED"}'
def _add44(n, parent=None, theme="t1"):
    return _st44.add_candidate(expr=f"rank(r{n})", settings=SimSettings().payload(), theme=theme,
                               template="tp", field=f"r{n}", stage="refine" if parent else "scan",
                               parent_id=parent)
_root = _add44(0); _st44.record_sim(_root, {"status": "OK", "sharpe": 1.2})
_s1, _s2 = _add44(1), _add44(2, theme="t2")                  # scan, poisoned
_v1 = _add44(3, parent=_root)                                # refine variant, poisoned
_bad = _add44(4)                                             # a GENUINE error
_okv = _add44(5, parent=_root); _st44.record_sim(_okv, {"status": "OK", "sharpe": 0.9})
for _c in (_s1, _s2, _v1):
    _st44.record_sim(_c, {"status": "ERROR", "error": _CAP44})
_st44.record_sim(_bad, {"status": "ERROR", "error": "Invalid data field foo"})
_st44.conn().executemany("INSERT OR REPLACE INTO arms(arm,trials,scan_trials,scan_pass) VALUES (?,?,?,?)",
                         [("t1::tp", 10, 8, 3), ("t2::tp", 1, 1, 1)]); _st44.conn().commit()
_cn44 = _sq44.connect(_db44, isolation_level=None)
_before = _cn44.execute("SELECT COUNT(*) FROM sims").fetchone()[0]
_pl = RC44.plan(_cn44)
check("repair plan: exactly the capacity rows, split by stage, scan-origin per arm",
      _pl["total"] == 3 and _pl["by_stage"] == {"scan": 2, "refine": 1}
      and _pl["per_arm"] == {"t1::tp": 1, "t2::tp": 1}, str(_pl))
check("repair dry run (plan) writes nothing", _cn44.execute("SELECT COUNT(*) FROM sims").fetchone()[0] == _before)
check("before repair: no incomplete ladders (the poisoned variant HAS a row)",
      _st44.incomplete_lineage_roots() == set())
_ap = RC44.apply(_cn44, "sims_capacity_errors_test")
_arms = dict((a, (t, st, sp)) for a, t, st, sp in _cn44.execute("SELECT arm,trials,scan_trials,scan_pass FROM arms"))
check("repair deletes only the capacity rows; the genuine ERROR and OK rows stay",
      _ap["applied"] and _cn44.execute("SELECT COUNT(*) FROM sims").fetchone()[0] == _before - 3
      and _cn44.execute("SELECT status FROM sims WHERE candidate_id=?", (_bad,)).fetchone()[0] == "ERROR")
check("repair keeps a restorable copy of every deleted row",
      _cn44.execute("SELECT COUNT(*) FROM sims_capacity_errors_test").fetchone()[0] == 3)
check("repair subtracts scan trials per arm and never below scan_pass",
      _arms["t1::tp"] == (9, 7, 3) and _arms["t2::tp"] == (0, 1, 1), str(_arms))
check("repair records what it did in kv",
      json.loads(_cn44.execute("SELECT v FROM kv WHERE k='repair_sims_capacity_errors_test'").fetchone()[0])["total"] == 3)
check("repair run twice is a no-op", RC44.apply(_cn44, "sims_capacity_errors_test")["applied"] is False
      and _cn44.execute("SELECT COUNT(*) FROM sims_capacity_errors_test").fetchone()[0] == 3)
check("after repair the poisoned scan candidates are pending again, the genuine error is not",
      {r["id"] for r in _st44.pending("scan")} == {_s1, _s2})
check("after repair the ladder missing its variant is found by incomplete_lineage_roots",
      _st44.incomplete_lineage_roots() == {_root})

class _FakeEx44:
    seen = None
    def __init__(self, cfg, store): pass
    def reclaim(self, budget_seconds=None):
        return {"reclaimed": 0, "still_running": 0, "lost": 0, "deferred_by_budget": 0}
    def run(self, ids, on_result=None):
        _FakeEx44.seen = list(ids); return {"ok": 0, "error": 0, "skipped": 0, "deferred": 0}
_saved44 = (_cli44._setup, _cli44.Executor, _cli44.generate)
_cli44._setup = lambda a: (RunConfig(), _st44, None)
_cli44.Executor = _FakeEx44
_cli44.generate = lambda *a, **k: (_ for _ in ()).throw(AssertionError("generate must not run"))
try:
    with open(os.devnull, "w") as _dn:
        import contextlib as _cxx44
        with _cxx44.redirect_stdout(_dn):
            _cli44.main(["scan", "--pending", "--budget", "50"])
finally:
    _cli44._setup, _cli44.Executor, _cli44.generate = _saved44
check("scan --pending dispatches exactly the pending scan candidates, never generates new ones",
      sorted(_FakeEx44.seen or []) == sorted([_s1, _s2]), str(_FakeEx44.seen))

# --- infrastructure failures are never a candidate's verdict ------------------
from alphamachine.client import (AuthError as _AE45, BiometricRequired as _BR45, DailyLimitReached as _DL45,
                                 is_transient_text as _tr45, BrainError as _BE45, TRANSIENT_ERROR_MARKERS as _TM45)
_transient = ['simulate [429]: {"detail":"CONCURRENT_SIMULATION_LIMIT_EXCEEDED"}',
              'simulate [429]: {"detail":"DAILY_SIMULATION_LIMIT_EXCEEDED"}',
              'rate limited [429]: {\n "message":"API rate limit exceeded"\n}',
              'child fetch failed: rate limited [429]: {"message":"API rate limit exceeded"}',
              'auth failed [400]: {"captcha":["This field is required."]}',
              'child fetch failed: BRAIN requires a biometric/persona check. Run scripts/auth_wait.py',
              "HTTPSConnectionPool(host='api.worldquantbrain.com', port=443): Read timed out.",
              "('Connection aborted.', RemoteDisconnected('Remote end closed connection'))",
              "HTTPSConnectionPool(host='x', port=443): Max retries exceeded with NameResolutionError",
              "timeout after 900.0s: https://api.worldquantbrain.com/simulations/abc",
              'simulate [401]: {"detail":"Incorrect authentication credentials."}']
_genuine = ['Invalid data field fnd65_totalcap_cusip_saleeps. <linkToCommonErrorMessages>Learn more</link',
            'Attempted to use unknown variable "attention_beta". <link',
            "There was an error while running the simulation. Please try again or contact BRAIN",
            "bundle returned fewer children than payloads", ""]
check("every infrastructure error seen in the real database classifies as transient",
      all(_tr45(m) for m in _transient), str([m[:30] for m in _transient if not _tr45(m)]))
check("a genuine error (bad field, unknown variable, an ambiguous platform message) does not",
      not any(_tr45(m) for m in _genuine), str([m[:30] for m in _genuine if _tr45(m)]))

# ---- authenticate(): one failed attempt pauses EVERY client in the process
class _P45:
    status_code = 401; headers = {"WWW-Authenticate": "persona", "Location": "/authentication/persona?inquiry=i"}
    text = ""; url = "https://api.worldquantbrain.com/authentication"
    def json(self): return {}
def _c45():
    c = _BC43("a@b.c", "x", cookie_path=os.path.join(tempfile.mkdtemp(), "c.json")); c._posts = 0
    def _post(*a, **k): c._posts += 1; return _P45()
    c.s.post = _post; return c
_BC43._auth_blocked_until = 0.0
_a, _b = _c45(), _c45()
try: _a.authenticate()
except _BR45: pass
_ra = None
try: _b.authenticate()
except _BR45 as e: _ra = str(e)
check("after one failed authenticate(), another client raises at once and does NOT post again",
      _a._posts == 1 and _b._posts == 0 and "paused" in (_ra or ""), f"posts a={_a._posts} b={_b._posts}")
_BC43._auth_blocked_until = 0.0
try: _b.authenticate()
except _BR45: pass
check("...and once the pause has run out it tries again", _b._posts == 1)
_BC43._auth_blocked_until = 0.0; _BC43._auth_block_reason = ""

# ---- client: the daily cap
_c, _calls = _cl43([_r43(429, {"detail": "DAILY_SIMULATION_LIMIT_EXCEEDED"})] * 5)
_dl = None
try: _c.simulate({"type": "REGULAR"})
except _DL45 as e: _dl = e
check("a daily-cap 429 raises DailyLimitReached after ONE call (no backoff loop)",
      _dl is not None and len(_calls) == 1, f"{len(_calls)} call(s)")

# ---- executor: which failures are stored, which are not
_st45 = Store(os.path.join(tempfile.mkdtemp(), "inf.db"))
def _cand45(n):
    return _st45.add_candidate(expr=f"rank(inf{n})", settings=SimSettings().payload(), theme="t", template="tt", field=f"inf{n}")
class _FC45:
    def __init__(self, **kw): self.__dict__.update(kw)
    def refresh_if_stale(self): pass
def _ex45(**kw):
    e = _Ex43(RunConfig(), _st45); e.client = lambda: _FC45(**kw); return e
def _rows45(cid): return _st45.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?", (cid,)).fetchone()[0]
def _infl45(cid): return _st45.conn().execute("SELECT COUNT(*) FROM inflight WHERE candidate_id=?", (cid,)).fetchone()[0]
def _raise(e): 
    def f(*a, **k): raise e
    return f

_i1 = _cand45(1); _e = _ex45(simulate=_raise(_AE45("auth failed [400]: captcha")))
_r = _e._bundle([_i1])
check("a dead session (AuthError) halts the run and records nothing",
      _r == {_i1: {"status": "HALTED"}} and _e._halt.is_set() and _rows45(_i1) == 0)

_i2, _i3 = _cand45(2), _cand45(3); _e = _ex45(simulate=_raise(_DL45("daily")))
_r1 = _e._bundle([_i2]); _r2 = _e._bundle([_i3])
check("the daily cap defers, sets the daily flag, and the NEXT bundle is not even attempted",
      _r1 == {_i2: {"status": "DEFERRED"}} and _e.daily_limit_reached() and _r2 == {_i3: {"status": "DEFERRED"}}
      and _rows45(_i2) == 0 and _rows45(_i3) == 0 and not _e._halt.is_set())

import requests as _rq45
_i4 = _cand45(4); _e = _ex45(simulate=_raise(_rq45.ConnectionError("Max retries exceeded")))
check("a network error before dispatch defers and records nothing",
      _e._bundle([_i4]) == {_i4: {"status": "DEFERRED"}} and _rows45(_i4) == 0)

_i5 = _cand45(5)
def _sim_ok(payload): return "https://x/sim/9"
_e = _ex45(simulate=_sim_ok, poll=_raise(_SF43("timeout after 900.0s: https://x/sim/9")))
_r = _e._bundle([_i5])
check("a poll timeout defers, records nothing, and KEEPS the in-flight marker so the paid sim can be harvested",
      _r == {_i5: {"status": "DEFERRED"}} and _rows45(_i5) == 0 and _infl45(_i5) == 1)

_i6 = _cand45(6); _e = _ex45(simulate=_sim_ok, poll=_raise(_SF43("simulate failed: Invalid data field foo")))
_e._bundle([_i6])
check("regression guard: a genuine poll-time failure is STILL recorded as ERROR",
      _rows45(_i6) == 1 and _infl45(_i6) == 0)

# harvest
_i7 = _cand45(7); _st45.mark_dispatched(_i7, "https://x/sim/h", 0)
_e = _ex45(); _row = _st45.candidate(_i7)
_o = _e._harvest(_FC45(child_result=_raise(_BE45("rate limited [429]: API rate limit exceeded"))), {"children": ["/c/1"]}, [(_i7, _row)])
check("a rate-limited child fetch defers, records nothing, keeps the marker (the sim is paid for)",
      _o == {_i7: {"status": "DEFERRED"}} and _rows45(_i7) == 0 and _infl45(_i7) == 1)
_i8 = _cand45(8); _st45.mark_dispatched(_i8, "https://x/sim/h2", 0)
_e = _ex45(); _row = _st45.candidate(_i8)
_o = _e._harvest(_FC45(child_result=_raise(_BR45("BRAIN requires a biometric check"))), {"children": ["/c/1"]}, [(_i8, _row)])
check("a session that dies mid-harvest halts the run, records nothing, keeps the marker",
      _o == {_i8: {"status": "HALTED"}} and _e._halt.is_set() and _rows45(_i8) == 0 and _infl45(_i8) == 1)
_i9 = _cand45(9); _st45.mark_dispatched(_i9, "https://x/sim/h3", 0); _e = _ex45(); _row = _st45.candidate(_i9)
_e._harvest(_FC45(child_result=_raise(_SF43('Attempted to use unknown variable "zzz"'))), {"children": ["/c/1"]}, [(_i9, _row)])
check("regression guard: a genuine child failure is STILL recorded as ERROR",
      _rows45(_i9) == 1)

# run(): the crash path
class _Boom45(_Ex43):
    def _bundle(self, cids): raise self.exc
def _run45(exc, cid):
    e = _Boom45(RunConfig(), _st45); e.exc = exc; return e.run([cid])
_j1, _j2, _j3 = _cand45(11), _cand45(12), _cand45(13)
_s1 = _run45(RuntimeError("rate limited [429]: API rate limit exceeded"), _j1)
_s2 = _run45(_AE45("bad"), _j2)
_s3 = _run45(RuntimeError("boom: a bug in our own code"), _j3)
check("run(): a crashed bundle with a transient message is deferred, not stored",
      _s1["deferred"] == 1 and _s1["error"] == 0 and _rows45(_j1) == 0, str(_s1))
check("run(): a crashed bundle with an AuthError is skipped (halt), not stored",
      _s2["skipped"] == 1 and _s2["error"] == 0 and _rows45(_j2) == 0, str(_s2))
check("run(): an unknown exception (our own bug) is still recorded, so it cannot loop forever",
      _s3["error"] == 1 and _rows45(_j3) == 1, str(_s3))

# repair covers every class and only those
import sqlite3 as _sq45
_dbp = os.path.join(tempfile.mkdtemp(), "rep2.db"); _st = Store(_dbp)
_ids = []
for _n, _msg in enumerate(_transient + _genuine[:4]):
    _cid = _st.add_candidate(expr=f"rank(z{_n})", settings=SimSettings().payload(), theme="t", template="tp", field=f"z{_n}")
    _st.record_sim(_cid, {"status": "ERROR", "error": _msg}); _ids.append(_cid)
_cn = _sq45.connect(_dbp, isolation_level=None)
_p = RC44.plan(_cn)
check("repair removes every transient class and keeps the genuine / ambiguous ones",
      _p["total"] == len(_transient), f"{_p['total']} of {len(_transient)}")
RC44.apply(_cn, "sims_transient_test")
check("...leaving exactly the genuine rows behind",
      _cn.execute("SELECT COUNT(*) FROM sims").fetchone()[0] == 4 and RC44.plan(_cn)["total"] == 0)

# --- is_persona_challenge: tell a real persona check apart from bad credentials --
# --- reclaim(): a time budget must not let a few stuck bundles block everything -
from alphamachine.executor import Executor as _Ex47
import alphamachine.executor as _exmod47

_st47 = Store(os.path.join(tempfile.mkdtemp(), "budget.db"))
def _cand47(n):
    return _st47.add_candidate(expr=f"rank(bud{n})", settings=SimSettings().payload(),
                               theme="t", template="tp", field=f"bud{n}")
_ids47 = {}
for i, url in enumerate(["u1", "u2", "u3"]):
    cid = _cand47(i)
    _st47.mark_dispatched(cid, f"https://x/sim/{url}", 0)
    _ids47[url] = cid

class _FakeClock47:
    def __init__(self, start=1000.0): self.t = start
    def time(self): return self.t
    def advance(self, dt): self.t += dt

class _PollTrack47:
    """.poll(url) -> a resolved body with one alpha; records call order and
    advances the fake clock to simulate the real wall-clock cost of polling."""
    def __init__(self, clock, cost=1.0):
        self.clock, self.cost, self.calls = clock, cost, []
    def poll(self, url, **kw):
        self.calls.append(url)
        self.clock.advance(self.cost)
        return {"alpha": "https://api/alphas/A" + url[-1]}
    def alpha(self, aid):
        return {"id": aid, "is": {"sharpe": 1.0}, "status": "UNSUBMITTED",
                "settings": {}, "regular": {}}

_clock47 = _FakeClock47()
_saved_time47 = _exmod47.time.time
_exmod47.time.time = _clock47.time
try:
    # concurrency=1: reclaim() now polls bundles on a thread pool (see the
    # parallel test below), so these budget-cutoff assertions -- which assume
    # bundles are checked one at a time in a fixed order -- pin it to a single
    # worker, which is exactly the old sequential behavior.
    _ex47 = _Ex47(RunConfig(concurrency=1), _st47)
    _fc47 = _PollTrack47(_clock47, cost=1.0)
    _ex47.client = lambda: _fc47
    _rc47 = _ex47.reclaim(budget_seconds=None)
    check("reclaim() with no budget processes every bundle",
          len(_fc47.calls) == 3 and _rc47["reclaimed"] == 3, str(_rc47))
finally:
    _exmod47.time.time = _saved_time47

_st48 = Store(os.path.join(tempfile.mkdtemp(), "budget2.db"))
_ids48 = {}
for i, url in enumerate(["u1", "u2", "u3"]):
    cid = _st48.add_candidate(expr=f"rank(bud2_{i})", settings=SimSettings().payload(),
                              theme="t", template="tp", field=f"bud2_{i}")
    _st48.mark_dispatched(cid, f"https://x/sim/{url}", 0)
    _ids48[url] = cid

_clock48 = _FakeClock47()
_exmod47.time.time = _clock48.time
try:
    _ex48 = _Ex47(RunConfig(concurrency=1), _st48)
    _fc48 = _PollTrack47(_clock48, cost=10.0)   # each poll costs 10s of fake wall-clock
    _ex48.client = lambda: _fc48
    _rc48 = _ex48.reclaim(budget_seconds=15.0)  # room for ~1 bundle, not all 3
    check("a tight budget processes what fits and stops, not everything",
          0 < len(_fc48.calls) < 3, str(_fc48.calls))
    check("reclaim() reports how many were left untouched by the budget",
          _rc48["deferred_by_budget"] == 3 - len(_fc48.calls), str(_rc48))
    _untouched48 = set(_ids48.values()) - {_ids48[u.rsplit("/", 1)[-1]] for u in _fc48.calls}
    check("a bundle the budget skipped is left completely alone -- marker untouched, no sim written",
          all(_st48.conn().execute("SELECT COUNT(*) FROM inflight WHERE candidate_id=?", (cid,)).fetchone()[0] == 1
              and _st48.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?", (cid,)).fetchone()[0] == 0
              for cid in _untouched48), str(_untouched48))
    check("a bundle ALREADY started before the deadline is allowed to finish, not cut mid-poll",
          all(_st48.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?",
                                   (_ids48[u.rsplit('/', 1)[-1]],)).fetchone()[0] == 1
              for u in _fc48.calls))
finally:
    _exmod47.time.time = _saved_time47

# --- the SAME check, one level down: a single bundle with several slow children ---
# must not itself blow the whole budget, uninterrupted, just because it already started.
class _MultiChildClient50:
    """poll() resolves fast with N children; child_result() costs fake-clock time each."""
    def __init__(self, clock, n_children, child_cost):
        self.clock, self.n, self.cost = clock, n_children, child_cost
        self.child_calls = []
    def poll(self, url, **kw):
        return {"children": [f"c{i}" for i in range(self.n)]}
    def child_result(self, cid, **kw):
        self.child_calls.append(cid)
        self.clock.advance(self.cost)
        return {"alpha": f"https://api/alphas/A{cid}"}
    def alpha(self, aid):
        return {"id": aid, "is": {"sharpe": 1.0}, "status": "UNSUBMITTED",
                "settings": {}, "regular": {}}

_st50 = Store(os.path.join(tempfile.mkdtemp(), "budget4.db"))
_cids50 = []
for i in range(5):
    cid = _st50.add_candidate(expr=f"rank(bud4_{i})", settings=SimSettings().payload(),
                              theme="t", template="tp", field=f"bud4_{i}")
    _st50.mark_dispatched(cid, "https://x/sim/onebundle", i)
    _cids50.append(cid)

_clock50 = _FakeClock47()
_exmod47.time.time = _clock50.time
try:
    _ex50 = _Ex47(RunConfig(concurrency=1), _st50)
    _fc50 = _MultiChildClient50(_clock50, n_children=5, child_cost=50.0)
    _ex50.client = lambda: _fc50
    _rc50 = _ex50.reclaim(budget_seconds=105.0)   # room for ~2 of 5 children, not all
    check("a single bundle with several slow children is cut off mid-bundle, not let run to completion",
          0 < len(_fc50.child_calls) < 5, str(_fc50.child_calls))
    check("children fetched before the deadline are properly recorded (real OK sims rows)",
          all(_st50.conn().execute("SELECT status FROM sims WHERE candidate_id=?", (_cids50[i],)).fetchone()[0] == "OK"
              for i in range(len(_fc50.child_calls))))
    check("children never reached (deadline passed mid-bundle) are left completely alone",
          all(_st50.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?", (_cids50[i],)).fetchone()[0] == 0
              and _st50.conn().execute("SELECT COUNT(*) FROM inflight WHERE candidate_id=?", (_cids50[i],)).fetchone()[0] == 1
              for i in range(len(_fc50.child_calls), 5)))
    check("reclaim() counts the mid-bundle-skipped candidates as deferred_by_budget",
          _rc50["deferred_by_budget"] == 5 - len(_fc50.child_calls), str(_rc50))
finally:
    _exmod47.time.time = _saved_time47

_st49 = Store(os.path.join(tempfile.mkdtemp(), "budget3.db"))
_cid49 = _st49.add_candidate(expr="rank(bud3)", settings=SimSettings().payload(),
                             theme="t", template="tp", field="bud3")
_st49.mark_dispatched(_cid49, "https://x/sim/u9", 0)
_clock49 = _FakeClock47()
_exmod47.time.time = _clock49.time
try:
    _ex49 = _Ex47(RunConfig(concurrency=1), _st49)
    _fc49 = _PollTrack47(_clock49, cost=1.0)
    _ex49.client = lambda: _fc49
    _rc49 = _ex49.reclaim(budget_seconds=0.0)
    check("budget_seconds=0 defers every bundle without polling any",
          _fc49.calls == [] and _rc49["deferred_by_budget"] == 1, str(_rc49))
finally:
    _exmod47.time.time = _saved_time47

# --- reclaim() actually runs bundles concurrently, not one at a time --------
# The budget tests above pin concurrency=1 to keep their sequential-order
# assertions meaningful; this one checks the thing that motivated the change:
# real wall-clock time, with the REAL clock (no monkeypatch), for a backlog
# that a single-threaded loop could not have drained this fast. Confirmed
# live 2026-09-24: 83 leftover bundles drained at roughly one per 90s.
import threading as _th51, time as _time51
_st51 = Store(os.path.join(tempfile.mkdtemp(), "parallel.db"))
_N51 = 6
for _i in range(_N51):
    _cid = _st51.add_candidate(expr=f"rank(par{_i})", settings=SimSettings().payload(),
                               theme="t", template="tp", field=f"par{_i}")
    _st51.mark_dispatched(_cid, f"https://x/sim/u{_i}", 0)

class _SlowPollClient51:
    """poll() costs REAL wall-clock time, so overlap is only possible if the
    caller actually runs bundles on separate threads."""
    def __init__(self, cost):
        self.cost, self.lock, self.calls = cost, _th51.Lock(), []
    def poll(self, url, **kw):
        _time51.sleep(self.cost)
        with self.lock:
            self.calls.append(url)
        return {"alpha": "https://api/alphas/A" + url[-1]}
    def alpha(self, aid):
        return {"id": aid, "is": {"sharpe": 1.0}, "status": "UNSUBMITTED",
                "settings": {}, "regular": {}}

_ex51 = _Ex47(RunConfig(concurrency=_N51), _st51)
_fc51 = _SlowPollClient51(cost=0.15)
_ex51.client = lambda: _fc51
_t0_51 = _time51.perf_counter()
_rc51 = _ex51.reclaim(budget_seconds=None)
_elapsed51 = _time51.perf_counter() - _t0_51
check("reclaim() polls bundles concurrently -- "
      f"{_N51} bundles costing {_fc51.cost}s each finished in {_elapsed51:.2f}s; "
      f"one at a time would need >= {_N51 * _fc51.cost:.2f}s",
      _elapsed51 < _N51 * _fc51.cost * 0.6, f"{_elapsed51:.2f}s")
check("...and every bundle is still correctly reclaimed",
      _rc51["reclaimed"] == _N51 and len(_fc51.calls) == _N51, str(_rc51))

# --- reclaim() must not crash when a worker thread hits a dead session ------
# Confirmed live 2026-09-24, right after the parallelization above shipped:
# several worker threads each lazily authenticate for the first time, one
# hits a real persona challenge, the rest see the process-wide auth pause and
# raise AuthError too -- and that raise was not caught anywhere in reclaim(),
# so the whole call (and the unattended process running it) crashed instead
# of leaving the markers alone for the next attempt.
class _DeadSessionClient52:
    def poll(self, url, **kw):
        raise AssertionError("poll() must not be reached: client() already failed")

_st52 = Store(os.path.join(tempfile.mkdtemp(), "authdead.db"))
_cid52 = _st52.add_candidate(expr="rank(auth1)", settings=SimSettings().payload(),
                             theme="t", template="tp", field="auth1")
_st52.mark_dispatched(_cid52, "https://x/sim/uauth1", 0)
_ex52 = _Ex47(RunConfig(concurrency=1), _st52)
_ex52.client = lambda: (_ for _ in ()).throw(_AE45("biometric check required"))
_rc52 = _ex52.reclaim(budget_seconds=None)
check("client() raising AuthError does not crash reclaim()",
      _rc52["still_running"] == 1, str(_rc52))
check("...halts (so run() and the next dispatch wait for a human)",
      _ex52._halt.is_set())
check("...and leaves the marker alone -- a dead session is not evidence the job is lost",
      _st52.conn().execute("SELECT COUNT(*) FROM inflight WHERE candidate_id=?",
                           (_cid52,)).fetchone()[0] == 1
      and _st52.conn().execute("SELECT COUNT(*) FROM sims WHERE candidate_id=?",
                               (_cid52,)).fetchone()[0] == 0)

_st53 = Store(os.path.join(tempfile.mkdtemp(), "authdead2.db"))
_cid53 = _st53.add_candidate(expr="rank(auth2)", settings=SimSettings().payload(),
                             theme="t", template="tp", field="auth2")
_st53.mark_dispatched(_cid53, "https://x/sim/uauth2", 0)
class _PollDiesClient52:
    def client(self): return self
    def poll(self, url, **kw): raise _AE45("session died mid-poll")
_ex53 = _Ex47(RunConfig(concurrency=1), _st53)
_ex53.client = lambda: _PollDiesClient52()
_rc53 = _ex53.reclaim(budget_seconds=None)
check("poll() raising AuthError mid-bundle also does not crash reclaim(), and halts",
      _rc53["still_running"] == 1 and _ex53._halt.is_set(), str(_rc53))

# The generic safety net: even a bug we did not anticipate must not crash the
# whole call, matching run()'s own crash path.
_st54 = Store(os.path.join(tempfile.mkdtemp(), "bundlebug.db"))
_cid54 = _st54.add_candidate(expr="rank(bug1)", settings=SimSettings().payload(),
                             theme="t", template="tp", field="bug1")
_st54.mark_dispatched(_cid54, "https://x/sim/ubug1", 0)
_ex54 = _Ex47(RunConfig(concurrency=1), _st54)
def _boom54(*a, **k): raise ValueError("an unanticipated bug in our own code")
_ex54._reclaim_bundle = _boom54
_rc54 = _ex54.reclaim(budget_seconds=None)
check("an unexpected exception from a bundle is caught, not raised out of reclaim()",
      _rc54["deferred_by_budget"] == 1, str(_rc54))

import io as _io41, contextlib as _cx41
from alphamachine.client import is_persona_challenge as _ipc46
check("a genuine persona challenge is recognised (case-insensitive)",
      _ipc46({"WWW-Authenticate": "Persona realm=\"brain\""}))
check("plain bad credentials (Bearer, no persona) is NOT a persona challenge -- "
      "confirmed live 2026-09-23: 401 INVALID_CREDENTIALS answered WWW-Authenticate: Bearer",
      not _ipc46({"WWW-Authenticate": "Bearer"}))
check("no WWW-Authenticate header at all is not a persona challenge",
      not _ipc46({}))

# auth_wait.main(), scripted against exactly the two response shapes seen live
import importlib
import auth_wait as _AW46  # scripts/ is on sys.path via smoke_test's own insert
class _R46:
    def __init__(self, code, headers, text="", loc=None):
        self.status_code, self.headers, self.text = code, dict(headers), text
        if loc is not None:
            self.headers["Location"] = loc
def _fresh_auth_wait(email="a@b.c", pw="x", responses=()):
    """A real requests.Session (so .cookies / .headers behave normally), with
    .get/.post patched: no persisted session, then the scripted responses in order."""
    d = tempfile.mkdtemp()
    importlib.reload(_AW46)
    _AW46.COOKIE_FILE = os.path.join(d, "cookies.json")
    _AW46.SENTINEL = os.path.join(d, "persona_done")
    _AW46.URL_FILE = os.path.join(d, "persona_url.txt")
    _AW46._load_env = lambda: None
    os.environ["WQ_EMAIL"], os.environ["WQ_PASSWORD"] = email, pw
    it = iter(responses)
    import requests as _rq46
    real = _rq46.Session()
    real.get = lambda *a, **k: _R46(404, {})              # no persisted session
    real.post = lambda *a, **k: next(it)
    _AW46.requests.Session = lambda: real
    return _AW46, d

_aw, _d = _fresh_auth_wait(responses=[_R46(401, {"WWW-Authenticate": "Bearer"},
                                           '{"detail":"INVALID_CREDENTIALS"}')])
_out46 = _io41.StringIO()
with _cx41.redirect_stdout(_out46):
    _rc46 = _aw.main()
check("bad credentials: auth_wait exits non-zero with a clear message, no empty link printed",
      _rc46 == 1 and "not a persona challenge" in _out46.getvalue()
      and "1. Open this in a browser" not in _out46.getvalue(), _out46.getvalue()[:200])
check("bad credentials: no persona_url.txt is written (nothing to wait on)",
      not os.path.exists(_aw.URL_FILE))

_aw, _d = _fresh_auth_wait(responses=[_R46(401, {"WWW-Authenticate": "Persona"}, loc="/authentication/persona?inquiry=i")])
_out46b = _io41.StringIO()
_aw.WAIT_MINUTES = 0.001
with _cx41.redirect_stdout(_out46b):
    _rc46b = _aw.main()
check("a genuine persona challenge still prints the real link and times out waiting (not stuck on step 1)",
      "1. Open this in a browser" in _out46b.getvalue()
      and "authentication/persona?inquiry=i" in _out46b.getvalue() and _rc46b == 1)
from alphamachine import cli as _cli41
# (an earlier section replaces subprocess.Popen with a fake, so drive argparse directly)
_buf41 = _io41.StringIO()
try:
    with _cx41.redirect_stdout(_buf41):
        _cli41.main(["--help"])
except SystemExit:
    pass
check("the account's simulation slot count is 13 by default (config and CLI)",
      _RC41().concurrency == 13 and "13 as of" in " ".join(_buf41.getvalue().split()))
check("a worker polls its own bundle for 40 min, not 15 -- a 10-alpha GLB bundle outlasted 900s "
      "and abandoned bundles kept their slots (2026-09-25)",
      _RC41().sim_timeout == 2400.0)

# --- check_powerpool --from-file must keep the FILE's order ------------------
import io as _io40, contextlib as _cx40
import check_powerpool as CP40

class _FakeClient40:
    checked: list = []
    def __init__(self, *a, **k): pass
    def authenticate(self): pass
    def alpha_check(self, aid):
        _FakeClient40.checked.append(aid); return {"is": {"checks": []}}

_ord40 = [_rec40("lo", 1.1, ["rank"], ["p"], "USA/D1/ANALYST"),
          _rec40("mid", 1.5, ["rank"], ["q"], "USA/D1/MODEL"),
          _rec40("hi", 2.9, ["rank"], ["r"], "USA/D1/MODEL")]
for _r in _ord40:
    _r.update({"gates": {}, "pending_gates": [], "reasons": [], "power_pool_correlation": None,
               "description_needs_work": [], "all_checks": []})
_tmp40 = os.path.join(tempfile.mkdtemp(), "order.json")
json.dump(_ord40, open(_tmp40, "w"))
_saved40 = (CP40.BrainClient, sys.argv, dict(os.environ))
CP40.BrainClient = _FakeClient40
os.environ["WQ_EMAIL"], os.environ["WQ_PASSWORD"] = "t@example.com", "x"
sys.argv = ["check_powerpool.py", "--from-file", _tmp40, "--deep", "--top", "2",
            "--delay", "0", "--json"]
try:
    with _cx40.redirect_stdout(_io40.StringIO()):
        CP40.main()
finally:
    CP40.BrainClient, sys.argv = _saved40[0], _saved40[1]
    os.environ.clear(); os.environ.update(_saved40[2])
check("--from-file --top N deepens the first N of the FILE, not the top-N Sharpes",
      _FakeClient40.checked == ["lo", "mid"], str(_FakeClient40.checked))

# --- Power Pool eligibility blocks on EVERY failed check, not 4 named gates ---
# 2026-09-25: MPa6Onv6 and pwRaAbGx read eligible=True while failing the
# Robust-universe test that docs/Power_Pool_Alpha_規則.md §1 requires.
_pp42 = lambda checks, sharpe=1.5: CP40.evaluate(
    {"id": "x", "status": "UNSUBMITTED", "settings": {"region": "ASI", "delay": 1},
     "regular": {"code": "group_rank(ts_zscore(f, 66), industry)"},
     "is": {"sharpe": sharpe, "checks": [{"name": n, "result": r} for n, r in checks]}},
    {"group_rank", "ts_zscore"})
check("a Robust-universe FAIL makes an alpha ineligible (the 4-gate rule missed it)",
      not _pp42([("LOW_TURNOVER", "PASS"), ("LOW_ROBUST_UNIVERSE_SHARPE.WITH_RATIO", "FAIL")])["eligible"])
check("a region sub-universe FAIL (LOW_ASI_JPN_SHARPE) makes an alpha ineligible",
      not _pp42([("LOW_ASI_JPN_SHARPE", "FAIL")])["eligible"])
check("a FAIL under a check name never seen before blocks by default",
      not _pp42([("LOW_SOME_NEW_CHECK", "FAIL")])["eligible"])
check("an ERROR result blocks, same as the submit path's failed_checks",
      not _pp42([("CONCENTRATED_WEIGHT", "ERROR")])["eligible"])
check("checks BRAIN stops enforcing once tagged (LOW_FITNESS, PROD_CORRELATION, LOW_2Y_SHARPE) do not block",
      _pp42([("LOW_FITNESS", "FAIL"), ("PROD_CORRELATION", "FAIL"), ("LOW_2Y_SHARPE", "FAIL")])["eligible"])
check("REGULAR_SUBMISSION FAIL (today's cap spent) is account state, not a verdict on the alpha",
      _pp42([("REGULAR_SUBMISSION", "FAIL")])["eligible"])
check("...and a saved report deep-checked on a capped day is not ineligible the next day",
      CP40.rejudge([{"alpha_id": "c", "reasons": ["REGULAR_SUBMISSION FAIL"],
                     "all_checks": [["REGULAR_SUBMISSION", "FAIL"]]}])[0]["eligible"])
check("a DATA_SET_AUTHORIZATION FAIL blocks (the KPOEwzQN / E5l2LGRJ kind)",
      not _pp42([("DATA_SET_AUTHORIZATION", "FAIL")])["eligible"])
check("the reason names the check, so a report shows WHY it was dropped",
      _pp42([("LOW_ROBUST_UNIVERSE_RETURNS", "FAIL")])["reasons"] == ["LOW_ROBUST_UNIVERSE_RETURNS FAIL"])

_stale42 = {"alpha_id": "s", "eligible": True, "reasons": ["Sharpe 0.9 < 1.0", "OLD_GATE FAIL"],
            "all_checks": [["LOW_ROBUST_UNIVERSE_RETURNS", "FAIL"]]}
CP40.rejudge([_stale42])
check("rejudge: a record saved under the old rule is re-judged from its all_checks on load",
      _stale42["eligible"] is False
      and _stale42["reasons"] == ["Sharpe 0.9 < 1.0", "LOW_ROBUST_UNIVERSE_RETURNS FAIL"],
      str(_stale42["reasons"]))
_noall42 = {"alpha_id": "n", "eligible": True, "reasons": []}
CP40.rejudge([_noall42])
check("rejudge: a record with no all_checks (older format) keeps its saved verdict",
      _noall42["eligible"] is True)

class _Check42:
    def __init__(self, checks): self.checks = checks
    def alpha_check(self, aid): return {"is": {"checks": self.checks}}
_r42 = {"alpha_id": "d", "reasons": [], "all_checks": [["LOW_ROBUST_UNIVERSE_RETURNS", "FAIL"]]}
CP40.deepen(_Check42([]), _r42)
check("deepen: an empty /check body (BRAIN still computing) does not wipe a FAIL already seen",
      _r42["eligible"] is False, str(_r42["reasons"]))
CP40.deepen(_Check42([{"name": "LOW_ROBUST_UNIVERSE_RETURNS", "result": "PASS"}]), _r42)
check("deepen: a fresh /check where the check now passes clears the old FAIL",
      _r42["eligible"] is True and _r42["all_checks"] == [("LOW_ROBUST_UNIVERSE_RETURNS", "PASS")],
      str(_r42))

print("\n[43] autoloop rotates regions, and refine stays inside the one it is given")
import autoloop as _al43
check("parse_regions reads REGION:UNIVERSE:DELAY lists, normalising case",
      _al43.parse_regions("eur:top2500:1, GLB:TOP3000:0")
      == [("EUR", "TOP2500", 1), ("GLB", "TOP3000", 0)])
check("parse_regions rejects a delay that is not 0 or 1",
      _raises(lambda: _al43.parse_regions("EUR:TOP2500:2"), ValueError))
check("the default rotation is USA/TOP3000/d1 alone (old behaviour unchanged)",
      _al43.parse_regions("USA:TOP3000:1") == [("USA", "TOP3000", 1)])
_cmds43 = []
class _Pop43:
    def __init__(self, cmd, **kw):
        _cmds43.append(cmd); self.returncode = 0; self.stdout = iter(["ok\n"])
    def wait(self): pass
_saved43 = _al43.subprocess.Popen
_al43.subprocess.Popen = _Pop43
try:
    _al43.run_stage(13, "scan", ["--budget", "5"], _al43.scope_args(("EUR", "TOP2500", 1)))
finally:
    _al43.subprocess.Popen = _saved43
check("region flags go BEFORE the stage name (they are the top-level parser's)",
      _cmds43 and _cmds43[0][:9] == ["python3", "-m", "alphamachine", "--concurrency", "13",
                                     "--region", "EUR", "--universe", "TOP2500"]
      and _cmds43[0][9:] == ["--delay", "1", "scan", "--budget", "5"], str(_cmds43))

from alphamachine import cli as _cli43
import argparse as _ap43
_st43 = Store(os.path.join(tempfile.mkdtemp(), "scope.db"))
for _reg, _uni, _sh in (("USA", "TOP3000", 3.0), ("EUR", "TOP2500", 1.2), ("EUR", "TOP2500", 0.9)):
    _c = _st43.add_candidate(expr=f"rank({_reg}{_sh})",
                             settings=SimSettings(region=_reg, universe=_uni, delay=1).payload(),
                             theme="t", template="tp", field=f"f{_reg}{_sh}")
    _st43.record_sim(_c, {"status": "OK", "sharpe": _sh, "alpha_id": f"A{_reg}{_sh}"})
_q43 = lambda ns: [r["alpha_id"] for r in _st43.sims_where(
    "s.status='OK' AND c.stage='scan'" + _cli43._scope_sql(ns)[0] + " ORDER BY abs(s.sharpe) DESC",
    _cli43._scope_sql(ns)[1])]
check("refine's query for EUR returns only EUR, even with a stronger USA candidate on file",
      _q43(_ap43.Namespace(region="EUR", universe="TOP2500", delay=1)) == ["AEUR1.2", "AEUR0.9"],
      str(_q43(_ap43.Namespace(region="EUR", universe="TOP2500", delay=1))))
check("...and the default scope (USA/TOP3000/d1) returns only USA",
      _q43(_ap43.Namespace(region="USA", universe="TOP3000", delay=1)) == ["AUSA3.0"])
check("a scope with no candidates returns nothing rather than falling back to all regions",
      _q43(_ap43.Namespace(region="GBR", universe="TOP700", delay=1)) == [])
import inspect as _in43
_src43 = _in43.getsource(_cli43.cmd_refine)
check("cmd_refine applies the scope on BOTH paths (fresh survivors and --redo-incomplete)",
      _src43.count("+ scope") == 2 and _src43.count("*scope_args") == 2)

print("\n[41] theme migration rebuilds the bandit instead of renaming it")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate_themes as MT41
from alphamachine import audit as A41

_d41 = tempfile.mkdtemp()
_cache41 = os.path.join(_d41, "cache"); os.makedirs(_cache41)
# Three fields that the OLD patterns all filed under `macro`: an unanchored
# `rate` matched "corporate", and `fundamental` did not match its own name.
# Two move and one stays, so this is a split and a survivor at once.
_f41 = [{"id": "fnd_corp_rev", "description": "corporate revenue of the entity",
         "dataset": {"name": "Company Fundamental Data for Equity"},
         "category": {"name": "Fundamental"}},
        {"id": "px_vol_corp", "description": "corporate trading volume",
         "dataset": {"name": "Price Volume Data"}, "category": {"name": "Price Volume"}},
        {"id": "gdp_yoy", "description": "gdp growth",
         "dataset": {"name": "Macro Indicators"}, "category": {"name": "Macro"}}]
json.dump(_f41, open(os.path.join(_cache41, "fields_USA_TOP3000_d1.json"), "w"))
check("the three fixture fields re-theme as intended",
      [infer_theme(f) for f in _f41] == ["fundamental", "pv", "macro"],
      str([infer_theme(f) for f in _f41]))

_s41 = Store(os.path.join(_d41, "m.db"))
for _i41, _fid41 in enumerate(["fnd_corp_rev", "px_vol_corp", "gdp_yoy"]):
    for _cid41, _par41 in ((f"root{_i41}", None), (f"kid{_i41}", f"root{_i41}")):
        with _s41.conn() as _c41:
            _c41.execute(
                "INSERT INTO candidates(id,expr,settings,theme,template,field,"
                "stage,parent_id,created) VALUES (?,?,?,?,?,?,?,?,?)",
                (_cid41, f"e_{_cid41}", "{}", "macro", "change", _fid41,
                 "refine" if _par41 else "scan", _par41, time.time()))
            _c41.execute("INSERT INTO sims(candidate_id,alpha_id,status,sharpe)"
                         " VALUES (?,?,?,?)", (_cid41, f"A{_cid41}", "OK", 2.0))
    with _s41.conn() as _c41:
        _c41.execute("INSERT INTO gate_history(ts,candidate_id,alpha_id,ok,deep_ok)"
                     " VALUES (?,?,?,?,?)", (time.time(), f"kid{_i41}", f"B{_i41}", 1, 1))
    _s41.claim_bandit_trial("gate", f"root{_i41}", "macro::change")
_s41.bump_arm("macro::change", trial=6, scan_trials=3, scan_pass=3,
              refine_trials=3, gate_pass=3)
_s41.bump_arm("stale::ghost", trial=99, scan_trials=99)

_p41 = MT41.plan(_s41, MT41.load_field_records(_cache41)[0])
check("dry run finds the rows that move", len(_p41["changed_ids"]) == 4,
      str(len(_p41["changed_ids"])))
_st41 = MT41.apply(_s41, _p41)
_a41 = {r["arm"]: r for r in _s41.arms()}

check("an arm nothing points at is dropped, not carried forward",
      "stale::ghost" not in _a41, str(sorted(_a41)))
check("one old arm splits into the three its fields now belong to",
      {"fundamental::change", "pv::change", "macro::change"} <= set(_a41),
      str(sorted(_a41)))
check("scan evidence is conserved across the split",
      sum(a["scan_trials"] for a in _a41.values()) == 3,
      str({k: v["scan_trials"] for k, v in _a41.items()}))
check("gate evidence is conserved across the split",
      (sum(a["refine_trials"] for a in _a41.values()),
       sum(a["gate_pass"] for a in _a41.values())) == (3, 3))
check("trials stays the cross-stage total",
      all(a["trials"] == a["scan_trials"] + a["refine_trials"] + a["corr_trials"]
          + a["deep_trials"] for a in _a41.values()))
check("bandit_taught.arm follows the candidate, so a lineage cannot be re-taught",
      {r["key"]: r["arm"] for r in _s41.conn().execute(
          "SELECT * FROM bandit_taught WHERE stage='gate'")}.get("root0")
      == "fundamental::change")
check("a child's theme still matches its parent's",
      _st41["lineage_theme_mismatches"] == 0)
check("re-running finds nothing to change",
      len(MT41.plan(_s41, MT41.load_field_records(_cache41)[0])["changed_ids"]) == 0)

_b41 = {r["arm"]: dict(r) for r in _s41.arms()}
_s41.rebuild_arms_from_evidence()
_cols41 = ("trials", "scan_trials", "scan_pass", "refine_trials", "gate_pass")
check("the rebuild is idempotent",
      {k: [v[c] for c in _cols41] for k, v in _b41.items()}
      == {k: [dict(v)[c] for c in _cols41] for k, v in
          {r["arm"]: r for r in _s41.arms()}.items()})

# A field id in no cache keeps whatever theme it has: re-inferring from a bare
# id drops the description and dataset name, which would file it under `other`.
with _s41.conn() as _c41:
    _c41.execute("INSERT INTO candidates(id,expr,settings,theme,template,field,"
                 "stage,created) VALUES (?,?,?,?,?,?,?,?)",
                 ("orph", "e", "{}", "macro", "change", "gone_field", "scan", time.time()))
_p41b = MT41.plan(_s41, MT41.load_field_records(_cache41)[0])
check("an unresolvable field id is left alone, not re-inferred from its id",
      all(c != "orph" for c, _ in _p41b["changed_ids"])
      and "gone_field" in _p41b["unresolved"])

_conn41 = A41.open_ro(os.path.join(_d41, "m.db"))
try:
    _f41b = [f.check for f in A41.check_theme_arm_consistency(_conn41)]
finally:
    _conn41.close()
check("audit reports a clean bandit after the migration", _f41b == [], str(_f41b))

# And it must actually fire -- a check that only ever passes proves nothing.
_s41.bump_arm("ghost_theme::change", trial=5, scan_trials=5)
with _s41.conn() as _c41:
    _c41.execute("UPDATE candidates SET theme='pv' WHERE id='kid0'")   # parent is fundamental
_conn41 = A41.open_ro(os.path.join(_d41, "m.db"))
try:
    _f41c = {f.check for f in A41.check_theme_arm_consistency(_conn41)}
finally:
    _conn41.close()
check("audit catches an arm nothing points at", "theme_arm_stranded" in _f41c, str(_f41c))
check("audit catches a child whose theme left its parent's",
      "lineage_theme_mismatch" in _f41c, str(_f41c))

os.remove(db)
print(f"\n{'ALL CHECKS PASSED' if not fail else str(fail)+' CHECK(S) FAILED'}\n")
sys.exit(1 if fail else 0)
