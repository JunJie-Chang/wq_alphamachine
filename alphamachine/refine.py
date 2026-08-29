"""The refinement ladder -- where alpha QUALITY is actually manufactured.

The coarse scan tells you a field/transform pair carries signal. It almost
never hands you a submittable alpha. Refinement is greedy coordinate ascent
over the settings and expression space, but *diagnosis-driven*: each rung is
chosen by reading which metric is failing, so a survivor costs ~15-25 sims
instead of the ~500 a full grid would.

Rung order is by (expected gain) / (sims spent):

  0  SIGN         1 sim   -- a -1.6 Sharpe is a 1.6 alpha written backwards
  1  NEUTRALIZE   5 sims  -- biggest single Sharpe lever on the platform
  2  ROBUSTNESS   3 sims  -- backfill/winsorize; fixes CONCENTRATED_WEIGHT
  3  TURNOVER     4 sims  -- decay / hump / trade_when, aimed at the 12.5% knee
  4  TRUNCATION   4 sims  -- drawdown and weight concentration
  5  PORTABILITY  3 sims  -- does it survive TOP1000 / a tighter universe?

Rung 5 is not an optimisation, it is a falsification test. An alpha that dies
on TOP1000 is a liquidity artefact and will fail LOW_SUB_UNIVERSE_SHARPE or,
worse, pass in sample and lose money live.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from . import templates as T
from .config import (FITNESS_TURNOVER_KNEE, Gates, REFINE_DECAY,
                     REFINE_NEUTRALIZATION, REFINE_TRUNCATION, SimSettings,
                     UNIVERSES)
from .metrics import check_result, failed_checks, headroom

log = logging.getLogger("alphamachine.refine")


@dataclass
class Variant:
    expr: str
    settings: SimSettings
    note: str


def suggest_decay(turnover: float, current_decay: int,
                  target: float = FITNESS_TURNOVER_KNEE) -> list[int]:
    """Aim decay at the fitness knee instead of sweeping blindly.

    Turnover falls roughly as 1/sqrt(effective averaging window), so to go from
    `turnover` to `target` you need about (turnover/target)^2 times the window.
    We propose that estimate plus neighbours, because the relationship is only
    approximate and the true curve is field-dependent.
    """
    if not turnover or turnover <= target:
        return []
    base = max(current_decay, 1)
    est = max(2, min(int(round(base * (turnover / target) ** 2)), 512))
    # Priority order matters: the targeted estimate must never be crowded out
    # by the generic ladder, and the rung has to leave room for hump and
    # trade_when, which are often better turnover levers than decay alone.
    ordered = [est, max(2, est // 2), min(512, est * 2)]
    for d in REFINE_DECAY:                    # one generic rung as a baseline
        if 0 < d <= est * 2 and d not in ordered:
            ordered.append(d)
            break
    seen, out = set(), []
    for d in ordered:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out[:3]


class Refiner:
    def __init__(self, store, executor, gates: Gates):
        self.store = store
        self.ex = executor
        self.g = gates

    # ------------------------------------------------------------------ API
    def ladder(self, candidate_id: str, max_rungs: int = 6) -> tuple[str, dict]:
        """Run the ladder on one survivor. Returns (best_candidate_id, record)."""
        best_id = candidate_id
        best = self._rec(best_id)
        if not best:
            return candidate_id, {}

        for rung in range(max_rungs):
            variants = self.plan(best_id, best, rung)
            if not variants:
                continue
            ids = self._queue(best_id, variants)
            if not ids:
                continue
            self.ex.run(ids)
            cand_id, cand = self._best_of([best_id, *ids])
            gain = _score(cand, self.g) - _score(best, self.g)
            log.info("rung %d: %d variants, best score %+.3f (%s)",
                     rung, len(ids), gain, cand.get("alpha_id"))
            if cand_id != best_id and gain > 0:
                best_id, best = cand_id, cand
        self.store.conn().execute(
            "UPDATE candidates SET stage='gated' WHERE id=?", (best_id,))
        self.store.conn().commit()
        return best_id, best

    # ---------------------------------------------------------------- rungs
    def plan(self, candidate_id: str, rec: dict, rung: int) -> list[Variant]:
        row = self.store.candidate(candidate_id)
        if row is None:
            return []
        expr = row["expr"]
        s = SimSettings(**json.loads(row["settings"]))
        field = row["field"] or ""
        sharpe = rec.get("sharpe") or 0.0
        to = rec.get("turnover") or 0.0
        checks = rec.get("checks") or []
        fails = set(failed_checks(checks))

        if rung == 0:  # SIGN
            return [Variant(T.negate(expr), s, "sign flip")] if sharpe < 0 else []

        if rung == 1:  # NEUTRALIZATION -- the big Sharpe lever
            return [Variant(expr, s.replace(neutralization=n), f"neut={n}")
                    for n in REFINE_NEUTRALIZATION if n != s.neutralization]

        if rung == 2:  # ROBUSTNESS
            v = []
            if field and "ts_backfill" not in expr:
                # Only reachable for dense fields now -- the generator
                # backfills sparse ones before they are ever scanned.
                v.append(Variant(T.wrap_backfill(expr, field), s, "ts_backfill"))
            if "winsorize" not in expr:
                v.append(Variant(T.wrap_winsorize(expr), s, "winsorize std=4"))
            if "CONCENTRATED_WEIGHT" in fails or (rec.get("longCount") or 0) < 200:
                v.append(Variant(T.wrap_zscore(T.wrap_winsorize(expr)), s,
                                 "winsorize+zscore"))
            return v

        if rung == 3:  # TURNOVER -> aim at the fitness knee
            v: list[Variant] = []
            for d in suggest_decay(to, s.decay):
                v.append(Variant(expr, s.replace(decay=d), f"decay={d}"))
            if to > FITNESS_TURNOVER_KNEE and "hump" not in expr:
                # hump beats decay when the signal is sharp but jittery: it
                # suppresses churn WITHOUT lagging the signal the way decay does.
                v.append(Variant(T.wrap_hump(expr, 0.005), s, "hump=0.005"))
                v.append(Variant(T.wrap_hump(expr, 0.01), s, "hump=0.01"))
            if to > 0.25 and "trade_when" not in expr:
                v.append(Variant(T.wrap_trade_when(expr), s, "trade_when volume>adv20"))
            if to < 0.02:
                # Guard the LOW_TURNOVER floor; below the knee there is no
                # fitness credit anyway, so trading a little more is free.
                v.append(Variant(expr, s.replace(decay=max(0, s.decay // 2)),
                                 "decay halved (turnover floor)"))
            return v[:6]

        if rung == 4:  # TRUNCATION
            return [Variant(expr, s.replace(truncation=t), f"trunc={t}")
                    for t in REFINE_TRUNCATION if abs(t - s.truncation) > 1e-9][:4]

        if rung == 5:  # PORTABILITY / falsification
            v = []
            unis = UNIVERSES.get(s.region, ())
            for u in unis[1:3]:                       # one and two steps tighter
                if u != s.universe:
                    v.append(Variant(expr, s.replace(universe=u), f"universe={u}"))
            if "LOW_SUB_UNIVERSE_SHARPE" in fails and "trade_when" not in expr:
                v.append(Variant(T.wrap_trade_when(expr, "volume > adv20"), s,
                                 "liquidity gate for sub-universe"))
            return v[:3]

        return []

    # ------------------------------------------------------------- plumbing
    def _queue(self, parent_id: str, variants: list[Variant]) -> list[str]:
        out = []
        parent = self.store.candidate(parent_id)
        for v in variants:
            if T.lint(v.expr):
                log.warning("refine variant rejected by lint: %s", T.lint(v.expr)[0])
                continue
            key = v.settings.key()
            if self.store.already_simulated(v.expr, key):
                continue
            out.append(self.store.add_candidate(
                expr=v.expr, settings=v.settings.payload(),
                theme=parent["theme"], template=parent["template"],
                field=parent["field"], stage="refine", parent_id=parent_id,
                settings_key=key))
        return out

    def _rec(self, candidate_id: str) -> dict:
        rows = self.store.sims_where("s.candidate_id=?", (candidate_id,))
        if not rows:
            return {}
        r = rows[0]
        return {k: r[k] for k in r.keys()} | {"checks": json.loads(r["checks"] or "[]")}

    def _best_of(self, ids: list[str]) -> tuple[str, dict]:
        best_id, best, best_s = ids[0], {}, -1e9
        for i in ids:
            rec = self._rec(i)
            if not rec or rec.get("status") != "OK":
                continue
            sc = _score(rec, self.g)
            if sc > best_s:
                best_id, best, best_s = i, rec, sc
        return best_id, best


def _score(rec: dict, g: Gates) -> float:
    """Rank refinement variants by headroom over our gates, not raw Sharpe.

    Raw Sharpe would happily pick a variant with 60% turnover that can never
    pass HIGH_TURNOVER. Headroom is a weakest-link score, so it pushes the
    ladder toward alphas that clear *every* gate with room to spare.
    """
    if not rec or rec.get("status") != "OK":
        return -1e9
    h = headroom(rec, g)
    fails = len(failed_checks(rec.get("checks") or []))
    return h - 0.25 * fails
