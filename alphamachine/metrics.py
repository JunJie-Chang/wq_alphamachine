"""Turning a raw BRAIN alpha payload into a decision.

Two layers of judgement:
  1. `platform_verdict` -- will BRAIN accept this? (mirrors is.checks)
  2. `quality_verdict`  -- SHOULD we submit it? (our stricter gates)

Layer 2 is where alpha quality actually comes from. The platform floor is a
floor, not a target: an alpha that clears Sharpe 1.25 in sample is roughly a
coin flip out of sample, and a book full of coin flips scores badly.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from .config import Gates, PLATFORM_CHECKS, fitness, subuniverse_floor


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
    return out


def check_value(checks: list[dict], name: str) -> float | None:
    for c in checks or []:
        if c.get("name") == name:
            return _f(c.get("value"))
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
    return min(slacks)


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
