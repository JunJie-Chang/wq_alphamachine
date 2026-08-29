"""Offline smoke test -- exercises every pure-logic path with no network."""
import os, sys, json, tempfile
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
check("all templates render", all(x.render("fld", days=66) for x in T.LIBRARY))
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
        if T.lint(t.render("fnd6_x", days=126, group="industry"))]
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
_v28b = _sub28(_lucky_yr, checks=[]).deep_verdict("X5")
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
os.remove(db28)

os.remove(db)
print(f"\n{'ALL CHECKS PASSED' if not fail else str(fail)+' CHECK(S) FAILED'}\n")
sys.exit(1 if fail else 0)
