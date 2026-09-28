"""Turning a raw BRAIN alpha payload into a decision.

Two layers of judgement:
  1. `platform_verdict` -- will BRAIN accept this? (mirrors is.checks)
  2. `quality_verdict`  -- SHOULD we submit it? (our stricter gates)

Layer 2 is where alpha quality actually comes from. The platform floor is a
floor, not a target: an alpha that clears Sharpe 1.25 in sample is roughly a
coin flip out of sample, and a book full of coin flips scores badly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from typing import Any

from .config import (PYRAMID_MIN_ALPHAS, Gates, PLATFORM_CHECKS, fitness,
                     subuniverse_floor)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = dc_field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def extract(alpha: dict) -> dict:
    """Flatten the /alphas/{id} payload into the fields we score on."""
    is_ = alpha.get("is") or {}
    checks = is_.get("checks") or []
    out = {
        "alpha_id": alpha.get("id"),
        "sharpe": _f(is_.get("sharpe")),
        "fitness": _f(is_.get("fitness")),
        "turnover": _f(is_.get("turnover")),
        "returns": _f(is_.get("returns")),
        "drawdown": _f(is_.get("drawdown")),
        "margin": _f(is_.get("margin")),
        "longCount": is_.get("longCount"),
        "shortCount": is_.get("shortCount"),
        "checks": checks,
        "status": alpha.get("status"),
        "grade": alpha.get("grade"),
        "raw": alpha,
    }
    out["subuniverse_sharpe"] = check_value(checks, "LOW_SUB_UNIVERSE_SHARPE")
    out["self_correlation"] = check_value(checks, "SELF_CORRELATION")
    out["prod_correlation"] = check_value(checks, "PROD_CORRELATION")
    # Recency. Nothing else in the pipeline knew an alpha could be dead for
    # five years and still look good: every other field here is a whole-period
    # aggregate, so a signal that earned Sharpe 9 in 2016 and nothing since
    # scores like a working one.
    #
    # two_year_sharpe is BRAIN's OWN number, present on ~44% of simulations
    # (it sends IS_LADDER_SHARPE to the rest) and identical to the value
    # /check later reports -- only the `result` string flips WARNING -> FAIL,
    # which is exactly how a 0.48 slipped through unnoticed. Because it is
    # BRAIN's, it can block.
    out["two_year_sharpe"] = check_value(checks, "LOW_2Y_SHARPE")
    # IS_LADDER_SHARPE is the OTHER recency check -- BRAIN sends exactly one of
    # this or LOW_2Y_SHARPE per alpha, never both (fewer years of history get
    # the ladder; enough years get the flat 2Y bar). It carries the same
    # {value, limit} shape, just with a "year" field alongside, and it is the
    # LARGER of the two buckets in this database. Config.py has filed it in
    # BOOLEAN_CHECKS since day one, which nothing ever reads -- the number was
    # sitting there the whole time, same as LOW_2Y_SHARPE was.
    out["is_ladder_sharpe"] = check_value(checks, "IS_LADDER_SHARPE")
    out["is_ladder_limit"] = check_limit(checks, "IS_LADDER_SHARPE")
    # test_sharpe is the trailing-testPeriod (P1Y6M) Sharpe BRAIN reports
    # alongside the full period. Nearly universal coverage, but a shorter
    # window than the 2Y check and only ~88% agreement with it at the 1.58
    # threshold -- good enough to RANK on, not to reject on.
    out["test_sharpe"] = _f((alpha.get("test") or {}).get("sharpe"))
    # Payout. MATCHES_PYRAMID carries the Dynamic Pyramid Multiplier that
    # scales the base payment's Quality Factor, and it is present on 100% of
    # simulations -- same shape as the recency gap above: BRAIN has been
    # sending it since day one and nothing read it.
    pyr = check_dict(checks, "MATCHES_PYRAMID")
    out["pyramid_multiplier"] = _f((pyr or {}).get("multiplier"))
    out["pyramid_names"] = [p.get("name") for p in (pyr or {}).get("pyramids") or []
                            if p.get("name")] or None
    out["pyramid_effective"] = (pyr or {}).get("effective")
    return out


def check_value(checks: list[dict], name: str) -> float | None:
    for c in checks or []:
        if c.get("name") == name:
            return _f(c.get("value"))
    return None


def check_dict(checks: list[dict], name: str) -> dict | None:
    """The whole check entry, for checks that carry more than value/limit.

    MATCHES_PYRAMID is the reason this exists: it reports `multiplier`,
    `effective` and a `pyramids` list rather than a bare `value`, so
    check_value() reads None off it and silently loses the payout number.
    """
    for c in checks or []:
        if c.get("name") == name:
            return c
    return None


def check_limit(checks: list[dict], name: str) -> float | None:
    """The threshold BRAIN itself applied, straight from the checks payload.

    Worth preferring over any local formula wherever it exists: it is the
    authoritative number, it is already per-alpha (LOW_SUB_UNIVERSE_SHARPE
    scales with the alpha's own Sharpe), and every simulation record we hold
    carries it.
    """
    for c in checks or []:
        if c.get("name") == name:
            return _f(c.get("limit"))
    return None


def check_result(checks: list[dict], name: str) -> str | None:
    for c in checks or []:
        if c.get("name") == name:
            return c.get("result")
    return None


def failed_checks(checks: list[dict]) -> list[str]:
    return [c.get("name", "?") for c in checks or []
            if c.get("result") in ("FAIL", "ERROR")]


def platform_verdict(rec: dict) -> Verdict:
    """Mirror BRAIN's own gate, using is.checks when present and falling back
    to the documented USA/TOP3000 limits when it is not."""
    checks = rec.get("checks") or []
    if checks:
        bad = failed_checks(checks)
        return Verdict(not bad, [f"platform FAIL: {c}" for c in bad])
    reasons = []
    fallback = {
        "LOW_SHARPE": abs(rec.get("sharpe") or 0),
        "LOW_FITNESS": abs(rec.get("fitness") or 0),
        "LOW_TURNOVER": rec.get("turnover") or 0,
        "HIGH_TURNOVER": rec.get("turnover") or 0,
    }
    for name, val in fallback.items():
        how, lim = PLATFORM_CHECKS[name]
        if how == "min" and val < lim:
            reasons.append(f"{name}: {val:.3f} < {lim}")
        if how == "max" and val > lim:
            reasons.append(f"{name}: {val:.3f} > {lim}")
    return Verdict(not reasons, reasons)


def quality_verdict(rec: dict, g: Gates, universe: str = "TOP3000",
                    region: str = "USA") -> Verdict:
    """Our gate: would BRAIN accept this, and is it worth our slot?

    Stricter than the platform where we have a documented reason (Sharpe,
    Fitness, drawdown, margin) -- but NOT stricter on the submission ceilings
    themselves. Being stricter there does not raise quality, it just hides
    alphas BRAIN would have taken; see the Gates docstring for what that cost.

    `universe`/`region` are only consulted for the sub-universe fallback, when
    a record has no BRAIN-reported limit to read.
    """
    r: list[str] = []
    sh = abs(rec.get("sharpe") or 0.0)
    fit = abs(rec.get("fitness") or 0.0)
    to = rec.get("turnover") or 0.0
    dd = abs(rec.get("drawdown") or 0.0)
    mg = rec.get("margin")

    if sh < g.sharpe:
        r.append(f"sharpe {sh:.2f} < {g.sharpe}")
    if fit < g.fitness:
        r.append(f"fitness {fit:.2f} < {g.fitness}")
    if to < g.turnover_min:
        r.append(f"turnover {to:.3f} < {g.turnover_min}")
    if to > g.turnover_max:
        r.append(f"turnover {to:.3f} > {g.turnover_max}")
    if dd > g.max_drawdown:
        r.append(f"drawdown {dd:.3f} > {g.max_drawdown}")

    # A signal that only works on the full universe is usually a small-cap /
    # illiquidity artefact. But the bar is BRAIN's, not a flat ratio of our
    # own: LOW_SUB_UNIVERSE_SHARPE is 0.75*sqrt(sub_size/size)*sharpe
    # (BRAIN_RULES.md:27), roughly 0.433x on USA/TOP3000 -- so the old
    # `sub/sharpe < 0.70` test rejected alphas BRAIN passes, by a wide margin.
    # Prefer the limit BRAIN reported; fall back to the formula only for a
    # record that predates the check.
    sub = rec.get("subuniverse_sharpe")
    if sub is not None:
        floor = check_limit(rec.get("checks"), "LOW_SUB_UNIVERSE_SHARPE")
        if floor is None:
            floor = subuniverse_floor(sh, universe, region)
        if sub < floor:
            r.append(f"sub-universe sharpe {sub:.2f} < {floor:.2f}")

    # Recency, on BRAIN's own number. Judged on the VALUE, never on the
    # `result` string: at simulation time these come back WARNING, and it is
    # the value that predicts the FAIL /check will return later -- a 0.48
    # reported as WARNING is exactly how a dead alpha reached the deep gate
    # looking submittable. Absent means BRAIN sent IS_LADDER_SHARPE for this
    # alpha instead, which is unknown, not fine, so absence never blocks.
    two_y = rec.get("two_year_sharpe")
    if two_y is None:
        two_y = check_value(rec.get("checks"), "LOW_2Y_SHARPE")
    if two_y is not None:
        # The limit is NOT always 1.58. Observed live: /check returned
        # limit 2.02 for five alphas while every simulation-time payload said
        # 1.58, so a 1.88 that our gate waved through was rejected by BRAIN.
        # Same lesson as LOW_SUB_UNIVERSE_SHARPE above -- prefer the limit
        # BRAIN reported for THIS alpha, and keep the Gates value only as the
        # fallback for a record that carries no limit.
        floor2y = check_limit(rec.get("checks"), "LOW_2Y_SHARPE")
        if floor2y is None:
            floor2y = g.min_two_year_sharpe
        if abs(two_y) < floor2y:
            r.append(f"2y sharpe {two_y:.2f} < {floor2y}")

    # The OTHER recency check -- BRAIN sends this OR two_year_sharpe, never
    # both, and this is the larger bucket. Same discipline: judge the value,
    # prefer BRAIN's own per-alpha limit, deep_verdict already treats a FAIL
    # here as a hard rejection via failed_checks() -- this just moves that
    # same block to the free local stage instead of paying for a /check first.
    ladder = rec.get("is_ladder_sharpe")
    if ladder is None:
        ladder = check_value(rec.get("checks"), "IS_LADDER_SHARPE")
    if ladder is not None:
        floor_ladder = rec.get("is_ladder_limit")
        if floor_ladder is None:
            floor_ladder = check_limit(rec.get("checks"), "IS_LADDER_SHARPE")
        if floor_ladder is not None and abs(ladder) < floor_ladder:
            r.append(f"ladder sharpe {ladder:.2f} < {floor_ladder}")

    # Real re-simulation on a tighter universe, from rung 5 -- see Gates
    # docstring for the case that motivated this (a formula-estimated
    # sub-universe check PASSED at 1.33 while the real number came back 0.68,
    # then 0.17). Absent means this candidate has no rung-5 children yet
    # (still mid-ladder, or too few universe tiers for its region) -- unknown,
    # not fine, so absence never blocks, same as every other check here.
    port = rec.get("portability_ratio")
    if port is not None and port < g.min_portability_ratio:
        r.append(f"portability ratio {port:.2f} < {g.min_portability_ratio} "
                 f"(collapses on a tighter universe)")

    if mg is not None:
        bps = mg * 10000.0 if abs(mg) < 1.0 else mg
        if bps < g.min_margin_bps:
            r.append(f"margin {bps:.1f}bps < {g.min_margin_bps}")

    sc = rec.get("self_correlation")
    if sc is not None and sc > g.max_self_corr:
        r.append(f"self-corr {sc:.2f} > {g.max_self_corr}")
    pc = rec.get("prod_correlation")
    if pc is not None and pc > g.max_prod_corr:
        r.append(f"prod-corr {pc:.2f} > {g.max_prod_corr}")

    lc, sc_ = rec.get("longCount") or 0, rec.get("shortCount") or 0
    if lc and sc_:
        bal = min(lc, sc_) / max(lc, sc_)
        if bal < 0.5:
            r.append(f"long/short count imbalance {bal:.2f}")
    if (lc + sc_) and (lc + sc_) < 100:
        r.append(f"only {lc + sc_} positions -- too concentrated to be robust")

    return Verdict(not r, r)


def yearly_consistency(yearly: dict, g: Gates) -> Verdict:
    """Reject alphas carried by one lucky year.

    A Sharpe-2 alpha that made all its money in 2020 is a Sharpe-0 alpha with a
    story attached. This is the cheapest overfitting filter available.
    """
    rows = _records(yearly)
    if not rows:
        return Verdict(True, ["no yearly stats available"])
    pnls = []
    for row in rows:
        v = row.get("pnl", row.get("returns"))
        if v is not None:
            pnls.append(float(v))
    if len(pnls) < 3:
        return Verdict(True, ["fewer than 3 years of stats"])
    pos = sum(1 for p in pnls if p > 0) / len(pnls)
    total = sum(abs(p) for p in pnls) or 1.0
    top_share = max(abs(p) for p in pnls) / total
    r = []
    if pos < g.min_years_positive:
        r.append(f"only {pos:.0%} of years positive")
    if top_share > g.max_year_pnl_share:
        r.append(f"single year is {top_share:.0%} of |PnL|")
    return Verdict(not r, r)


def _records(recordset: dict) -> list[dict]:
    """BRAIN recordsets arrive as {schema:{properties:[{name}...]}, records:[[...]]}"""
    if not recordset:
        return []
    props = ((recordset.get("schema") or {}).get("properties")) or []
    names = [p.get("name") for p in props]
    recs = recordset.get("records") or []
    if names and recs and isinstance(recs[0], (list, tuple)):
        return [dict(zip(names, row)) for row in recs]
    return [r for r in recs if isinstance(r, dict)]


def pnl_series(pnl_recordset: dict) -> tuple[list[str], list[float]]:
    rows = _records(pnl_recordset)
    dates, vals = [], []
    for row in rows:
        d = row.get("date") or row.get("Date")
        v = row.get("pnl", row.get("Pnl", row.get("value")))
        if d is None or v is None:
            continue
        dates.append(str(d))
        vals.append(float(v))
    return dates, vals


# Expected maximum of N iid standard-normal draws. PROD_CORRELATION scores an
# alpha on the MAXIMUM correlation against every alpha in BRAIN's production
# pool, so with a pool this large the max is dominated by extreme-value
# statistics rather than by how novel the idea is: at the ~2.48M pool observed
# in 2026-08, z is about 4.95, so a profile centred at 0.11 with sd 0.16
# "expects" a max near 0.92 no matter what the alpha does. mean and sd are
# therefore the only two levers, which is why they are worth recording.
def expected_max_z(n: int) -> float:
    if n < 2:
        return 0.0
    ln = math.log(n)
    a = math.sqrt(2.0 * ln)
    return a - (math.log(ln) + math.log(4.0 * math.pi)) / (2.0 * a)


def corr_profile(payload: dict) -> dict | None:
    """mean/sd/max/min/n from a /correlations/prod histogram.

    The payload is a recordset of [lo, hi, count] bins spanning the whole
    production pool. Mean and sd come from bin midpoints -- coarse, but the
    bins are 0.1 wide against an sd near 0.16, and it is the SHAPE that
    matters here, not the third decimal.

    Returns None when there are no records, matching deep_verdict's
    inconclusive-is-not-a-verdict discipline: these endpoints are lazily
    computed and _await_record (unlike _recordset) does not retry a 200 with
    an empty body, so "no records" means "we did not get an answer", never
    "this alpha has no correlations".
    """
    rows = _records(payload or {})
    bins = []
    for r in rows:
        lo, hi, cnt = _f(r.get("min")), _f(r.get("max")), r.get("alphas")
        if lo is None or hi is None or cnt is None:
            continue
        bins.append(((lo + hi) / 2.0, int(cnt)))
    n = sum(c for _, c in bins)
    if not bins or n <= 0:
        return None
    mean = sum(m * c for m, c in bins) / n
    var = sum(((m - mean) ** 2) * c for m, c in bins) / n
    sd = math.sqrt(max(var, 0.0))
    return {"n_alphas": n, "mean": mean, "sd": sd,
            # BRAIN reports the true max/min alongside the histogram; prefer
            # those over a bin-edge estimate when present.
            "max_corr": _f((payload or {}).get("max")),
            "min_corr": _f((payload or {}).get("min")),
            "expected_max": mean + sd * expected_max_z(n)}


def headroom(rec: dict, g: Gates) -> float:
    """How much margin does this alpha have over our gates? Used for ranking.

    Returns the minimum normalised slack across axes -- a "weakest link" score,
    because an alpha is only as submittable as its worst metric.
    """
    sh = abs(rec.get("sharpe") or 0.0)
    fit = abs(rec.get("fitness") or 0.0)
    to = rec.get("turnover") or 1.0
    # Scored against the PREFERRED turnover, not the submission ceiling. The
    # ceiling decides what is allowed; this decides what the refinement ladder
    # climbs toward, and Value Factor rewards staying near 30% (BRAIN_RULES.md
    # :77) plus a PASS-threshold discount below it (line 44). Scoring against
    # 0.70 here would flatten that gradient and let the ladder drift up.
    slacks = [sh / max(g.sharpe, 1e-9), fit / max(g.fitness, 1e-9),
              g.turnover_ideal_max / max(to, 1e-9)]
    # Turnover has a FLOOR as well as a ceiling. Scoring only the ceiling makes
    # a 0.4% -turnover alpha look outstanding when it is actually unsubmittable
    # (LOW_TURNOVER), and the refinement ladder will happily climb toward it --
    # decay makes turnover fall, so the gradient points straight off the cliff.
    slacks.append(to / max(g.turnover_min, 1e-9))
    sub = rec.get("subuniverse_sharpe")
    if sub is not None and sh > 0:
        slacks.append((sub / sh) / max(g.subuniverse_ratio, 1e-9))
    # Recency. Without this the ladder actively PREFERS decayed signals: every
    # other term above is a whole-period aggregate, so an alpha whose
    # 2015-2016 block inflates its full-period Sharpe by ~40% outscores a
    # steady one, and refine._score (= headroom - 0.25*fails) is the only
    # objective the ladder has. Measured before this line existed: a
    # Sharpe-2.39 alpha with a 0.48 two-year Sharpe scored 0.96 and beat a
    # steady Sharpe-1.75/2Y-1.8 alpha at 1.05's expense.
    #
    # Prefer BRAIN's exact 2Y number; then IS_LADDER_SHARPE (the other check
    # BRAIN sends instead, on the larger share of alphas -- a record carries
    # one or the other, never both); fall back to the trailing-testPeriod
    # Sharpe, present on nearly every simulation but a shorter window that
    # agrees with the 2Y check only ~88% of the time at the threshold. That is
    # why the fallback ranks here and does NOT gate in quality_verdict: good
    # enough to steer, not to veto -- unlike the first two, which DO gate
    # there, because both are BRAIN's own numbers.
    recent, recent_floor = rec.get("two_year_sharpe"), g.min_two_year_sharpe
    if recent is None:
        recent = rec.get("is_ladder_sharpe")
        recent_floor = rec.get("is_ladder_limit") or g.min_two_year_sharpe
    if recent is None:
        recent = rec.get("test_sharpe")
    if recent is not None:
        slacks.append(abs(recent) / max(recent_floor, 1e-9))
    # Real re-simulation on a tighter universe (rung 5), scored against BRAIN's
    # own sub-universe formula -- see Gates.min_portability_ratio for the case
    # that motivated this: a formula ESTIMATE passed at 1.33 while the real
    # number came back 0.68, then 0.17. Already a normalised ratio (1.0 = at
    # the floor), so no rescaling needed, same as every other slack here.
    port = rec.get("portability_ratio")
    if port is not None:
        slacks.append(port / max(g.min_portability_ratio, 1e-9))
    return min(slacks)


def completed_pyramids(tally: list[dict], min_alphas: int = PYRAMID_MIN_ALPHAS
                       ) -> list[str]:
    """Pyramids BRAIN counts as formulated, from client.pyramid_tally() rows."""
    return [r["name"] for r in tally or [] if (r.get("alphas") or 0) >= min_alphas]


def tally_disagreement(tallies: list[list[dict]],
                       min_alphas: int = PYRAMID_MIN_ALPHAS) -> str | None:
    """None when every read of BRAIN's pyramid tally agreed; else a line naming
    each answer, so a report never silently picks one of BRAIN's two stories."""
    if len(tallies) < 2:
        return None
    alts = [f"{sum(r['alphas'] for r in t)} alphas / "
            f"{len(completed_pyramids(t, min_alphas))} formulated" for t in tallies]
    return ("BRAIN's pyramid tally disagreed between reads (" + " vs ".join(alts)
            + "); showing the most frequent -- re-read later")


def stale_note(measured_at: float | None, now: float | None = None) -> str:
    """When BRAIN measured a value that drifts as new alphas join the book
    (SELF / POWER_POOL / PROD correlation). The value is shown as BRAIN gave it,
    never recomputed locally; this says how old it is so nobody mistakes it for
    a live reading. The live /check right before a submit is what decides.
    """
    if not measured_at:
        return "BRAIN, time unknown -- may have drifted"
    import time as _t
    age = max(0.0, (now if now is not None else _t.time()) - measured_at)
    if age < 3600:
        when = f"{int(age // 60)}m ago"
    elif age < 86400:
        when = f"{age / 3600:.0f}h ago"
    else:
        when = f"{age / 86400:.0f}d ago"
    return (f"BRAIN, {when}" if age < 3600
            else f"BRAIN, {when} -- may have drifted as alphas joined")


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
