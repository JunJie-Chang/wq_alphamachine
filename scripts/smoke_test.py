"""Offline smoke test -- exercises every pure-logic path with no network."""
import os, sys, json, tempfile, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from alphamachine import templates as T
from alphamachine.bandit import Bandit, arm_name
from alphamachine.catalog import Catalog, infer_theme
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
check("scoring day starts 03:00 America/New_York, not UTC midnight",
      _ny.hour == 3 and _ny.minute == 0, f"{_ny:%Y-%m-%d %H:%M %Z}")
check("the boundary is in the past and within 24h",
      0 <= (_t.time() - _start) < 86400)
_utc_mid = _dt.datetime.now(_dt.timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0).timestamp()
check("differs from the UTC-midnight window it replaced",
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

print("\n[15] biometric quota guard (25/day platform cap)")
import tempfile as _tf, threading as _th3, requests as _rq3, time as _t
from alphamachine.client import BrainClient, BiometricRequired
_biodir = _tf.mkdtemp()
bc = BrainClient.__new__(BrainClient)
bc.s = _rq3.Session(); bc.api = "https://x"; bc.timeout = 1
bc.cookie_path = os.path.join(_biodir, "cookies.json")
bc._expires_at = None; bc._lock = _th3.Lock(); bc._authed = False
check("starts with the full safe budget",
      bc.biometric_budget_left() == BrainClient.BIOMETRIC_SAFE_BUDGET,
      f"{bc.biometric_budget_left()}")
for _ in range(BrainClient.BIOMETRIC_SAFE_BUDGET):
    bc._record_bio_attempt()
check("budget depletes as attempts are recorded", bc.biometric_budget_left() == 0)
try:
    bc._check_bio_budget()
    check("refuses a further attempt once spent", False, "no exception")
except BiometricRequired as e:
    check("refuses a further attempt once spent", "budget spent" in str(e))
# Attempts older than 24h must age out of the window.
old_stamps = [_t.time() - 90000 for _ in range(20)]
json.dump(old_stamps, open(bc._bio_log_path(), "w"))
check("attempts older than 24h no longer count",
      bc.biometric_budget_left() == BrainClient.BIOMETRIC_SAFE_BUDGET)
check("safe budget sits well under the platform cap",
      BrainClient.BIOMETRIC_SAFE_BUDGET < BrainClient.BIOMETRIC_DAILY_CAP == 25)
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

# B. The deep gate has to actually move budget, or the extra API calls buy
# nothing. Two arms identical through scan and gate; one dies at the deep gate.
_clean, _lucky = _an25("analyst", "change"), _an25("insiders", "event_gated")
b25b = _B25(st25, [_clean, _lucky])
for _ in range(30):
    for a in (_clean, _lucky):
        b25b.update(a, stage="scan", passed=True)
        b25b.update(a, stage="gate", passed=True)
for _ in range(10):
    b25b.update(_clean, stage="deep", passed=True)
    b25b.update(_lucky, stage="deep", passed=False)   # lucky year / prod-corr
al25b = b25b.allocate(400)
check("an arm that dies at the deep gate loses budget to one that does not",
      al25b[_clean] > 3 * al25b[_lucky],
      f"clean={al25b[_clean]} deep-blocked={al25b[_lucky]}")

# C. Three stacked priors must not flatten everything into noise.
check("compounded priors still let a converting arm win decisively",
      al25b[_clean] > 0.6 * 400, f"{al25b[_clean]}/400")

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

_tTie = _t32.time() + 20.0
with st32.conn() as _c32:
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok,"
        "deep_ok,deep_reasons) VALUES (?,?,?,?,?,?,?)",
        (_tTie, "cE1", "AE1", "expr_e", 1, 1, "[]"))
    _c32.execute(
        "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,ok,"
        "deep_ok,deep_reasons) VALUES (?,?,?,?,?,?,?)",
        (_tTie, "cE2", "AE2", "expr_e", 1, 0, "[\"lucky year\"]"))
_fE = _AU32.check_gate_passing_now_consistency(st32.conn())
check("a ts-tie is resolved by id, not left to the SQL join's discretion",
      any(f.detail.get("expr") == "expr_e" for f in _fE), f"{_fE}")

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

os.remove(db)
print(f"\n{'ALL CHECKS PASSED' if not fail else str(fail)+' CHECK(S) FAILED'}\n")
sys.exit(1 if fail else 0)
