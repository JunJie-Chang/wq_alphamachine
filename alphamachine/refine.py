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
                     UNIVERSES, pyramid_tilt, neutralization_tilt,
                     subuniverse_floor)
from .metrics import check_result, failed_checks, headroom

log = logging.getLogger("alphamachine.refine")


@dataclass
class Variant:
    expr: str
    settings: SimSettings
    note: str


@dataclass
class LadderState:
    """One candidate's position on the ladder, so many can be driven at once.

    The rung dependency is real -- rung N+1 is planned from rung N's champion,
    which is what makes this coordinate ascent rather than a sweep. But that
    dependency is WITHIN a lineage. Different candidates are independent, and
    keeping the position in a value rather than on the call stack is what lets
    a driver interleave them and dispatch their variants together.
    """
    root_id: str
    best_id: str
    best: dict
    rung: int = 0
    pending: list[str] = None            # ids dispatched for the current rung
    done: bool = False

    def __post_init__(self):
        if self.pending is None:
            self.pending = []


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
        # Computed ONCE per refine invocation, not per candidate -- this is an
        # aggregate read over the whole deep_checks history, not a per-record
        # lookup, so it belongs beside the Bandit's own once-per-run load in
        # cli.cmd_refine rather than inside the hot per-variant scoring path.
        self.neut_survival = store.neutralization_survival()

    # ------------------------------------------------------------------ API
    MAX_RUNGS = 6

    def start(self, candidate_id: str) -> LadderState | None:
        """Open a ladder. None when the root has no usable simulation."""
        best = self._rec(candidate_id)
        if not best:
            return None
        return LadderState(root_id=candidate_id, best_id=candidate_id, best=best)

    def step(self, st: LadderState) -> list[str]:
        """Queue the current rung; return only the ids that still need a slot.

        `st.pending` holds EVERY variant id for the rung, because absorb() must
        judge the whole rung -- a resumed ladder whose variants were simulated
        by an earlier run still has to elect the same champion. The returned
        list is just the work outstanding: everything on a fresh run, often
        nothing on a replay.

        Rungs that ask for nothing at all (`plan()` returns [] -- rung 0 on a
        positive-Sharpe alpha, say) are skipped here, so a state can never
        re-plan the same barren rung forever and never reach `done`.
        """
        st.pending = []
        while not st.done and st.rung < self.MAX_RUNGS:
            variants = self.plan(st.best_id, st.best, st.rung)
            ids = self._queue(st.best_id, variants) if variants else []
            if ids:
                st.pending = ids
                return self._needs_sim(ids)
            st.rung += 1
        st.done = True
        return []

    def absorb(self, st: LadderState) -> None:
        """Fold the dispatched rung's results in and advance one rung.

        A candidate whose bundle was HALTED has no new record to read;
        `_best_of` simply keeps the incumbent, so the rung still advances and
        the driver is expected to stop the wave rather than retry here.
        """
        if not st.pending:
            return
        ids, st.pending = st.pending, []
        cand_id, cand = self._best_of([st.best_id, *ids])
        gain = (_score(cand, self.g, self.neut_survival)
               - _score(st.best, self.g, self.neut_survival))
        log.info("rung %d: %d variants, best score %+.3f (%s)",
                 st.rung, len(ids), gain, cand.get("alpha_id"))
        if cand_id != st.best_id and gain > 0:
            st.best_id, st.best = cand_id, cand
        st.rung += 1
        if st.rung >= self.MAX_RUNGS:
            st.done = True

    def finalize(self, st: LadderState) -> tuple[str, dict]:
        """Mark the champion. ONLY for a state that reached the last rung.

        An interrupted ladder must never be recorded as gated: `select` reads
        stage='gated' as "this lineage is finished", and a half-refined alpha
        labelled that way would be judged on settings it had not finished
        exploring.
        """
        if not st.done:
            raise ValueError(f"refusing to finalize an unfinished ladder "
                             f"({st.root_id} stopped at rung {st.rung})")
        c = self.store.conn()
        c.execute("UPDATE candidates SET stage='gated' WHERE id=?", (st.best_id,))
        port = self._portability_ratio(st.best_id, st.best)
        if port is not None:
            c.execute("UPDATE sims SET portability_ratio=? WHERE candidate_id=?",
                      (port, st.best_id))
            st.best["portability_ratio"] = port
        c.commit()
        return st.best_id, st.best

    def _portability_ratio(self, champion_id: str, champion: dict
                           ) -> float | None:
        """Worst (actual / subuniverse_floor) across the champion's own rung-5
        children -- see config.Gates.min_portability_ratio for why this exists
        and the real numbers that motivated it.

        Rung 5's variants are queued with parent_id = the rung-4 champion, and
        a smaller universe essentially never scores higher in absorb(), so
        st.best_id cannot change during rung 5 -- the finalized champion's own
        id IS the parent_id its rung-5 children carry. None when there are no
        rung-5 children (a region with too few universe tiers, or a barren
        rung that queued nothing): unknown, not a penalty, same discipline as
        every other field here.
        """
        try:
            csettings = json.loads(champion.get("settings") or "{}")
        except (ValueError, TypeError):
            return None
        cuniv = csettings.get("universe")
        region = csettings.get("region", "USA")
        csharpe = champion.get("sharpe")
        if not cuniv or csharpe is None:
            return None
        children = self.store.conn().execute(
            "SELECT ch.settings, s.sharpe FROM candidates ch "
            "JOIN sims s ON s.candidate_id=ch.id "
            "WHERE ch.parent_id=? AND s.status='OK'", (champion_id,)).fetchall()
        worst = None
        for child in children:
            try:
                universe = json.loads(child["settings"]).get("universe")
            except (ValueError, TypeError):
                continue
            if not universe or universe == cuniv or child["sharpe"] is None:
                continue
            floor = subuniverse_floor(csharpe, cuniv, region, universe)
            if floor <= 0:
                continue
            ratio = child["sharpe"] / floor
            if worst is None or ratio < worst:
                worst = ratio
        return worst

    def ladder(self, candidate_id: str, max_rungs: int | None = None
               ) -> tuple[str, dict]:
        """Run one candidate's ladder to completion, serially.

        The single-candidate reference path. `cmd_refine` drives many states at
        once instead (see cli.cmd_refine), but the semantics here are what that
        driver must reproduce.
        """
        st = self.start(candidate_id)
        if st is None:
            return candidate_id, {}
        limit = self.MAX_RUNGS if max_rungs is None else max_rungs
        while not st.done and st.rung < limit:
            todo = self.step(st)
            if not st.pending:
                break                      # ladder exhausted
            if todo:
                self.ex.run(todo)
            self.absorb(st)                # judge the rung either way
        st.done = True
        return self.finalize(st)

    # ---------------------------------------------------------------- rungs
    def plan(self, candidate_id: str, rec: dict, rung: int) -> list[Variant]:
        row = self.store.candidate(candidate_id)
        if row is None:
            return []
        expr = row["expr"]
        s = SimSettings(**json.loads(row["settings"]))
        field = row["field"] or ""
        field2 = row["field2"] or ""
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
            # Only reachable for dense fields now -- the generator backfills
            # sparse ones before they are ever scanned. Folded over BOTH fields
            # because the old whole-expression `"ts_backfill" not in expr` test
            # skipped the rung entirely once either field had been wrapped,
            # leaving the other raw; wrap_backfill carries its own per-field
            # guard. The `bf != expr` test is load-bearing: a variant identical
            # to its parent hashes to the PARENT'S id (sha1(expr+settings)), so
            # the rung would silently re-crown the incumbent as its own child.
            bf = expr
            for fd in (field, field2):
                if fd:
                    bf = T.wrap_backfill(bf, fd)
            if bf != expr:
                v.append(Variant(bf, s, "ts_backfill"))
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
                # The gate's CONDITION is chosen to avoid dragging the alpha
                # into the price-volume pyramid when it is not there already;
                # see templates.turnover_gate. Substitution, not addition --
                # this rung still emits exactly one trade_when variant.
                gated = T.turnover_gate(expr, field)
                v.append(Variant(gated, s, "trade_when "
                                 + ("volume>adv20" if T.touches_pv(expr)
                                    or not field else "own-zscore")))
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
        """Register every legal variant and return ALL their ids.

        Including ones already simulated on an earlier run. Dropping those here
        is what used to break resumption: the rung then looked empty, was
        skipped entirely, and `_best_of` never saw variants a previous run had
        already PAID for -- so a re-run quietly elected the root as champion and
        threw the real winner away. Ids are `sha1(expr + settings)`, so a replay
        reproduces them exactly; deciding which still need a slot is the
        caller's job, not this one's.
        """
        out = []
        parent = self.store.candidate(parent_id)
        for v in variants:
            if T.lint(v.expr):
                log.warning("refine variant rejected by lint: %s", T.lint(v.expr)[0])
                continue
            out.append(self.store.add_candidate(
                expr=v.expr, settings=v.settings.payload(),
                theme=parent["theme"], template=parent["template"],
                field=parent["field"], field2=parent["field2"],
                stage="refine", parent_id=parent_id,
                settings_key=v.settings.key()))
        return out

    def _needs_sim(self, cids: list[str]) -> list[str]:
        """Which of these have no simulation record yet."""
        return [c for c in cids if not self._rec(c)]

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
            sc = _score(rec, self.g, self.neut_survival)
            if sc > best_s:
                best_id, best, best_s = i, rec, sc
        return best_id, best


def _score(rec: dict, g: Gates,
          neut_survival: dict[str, tuple[int, int]] | None = None) -> float:
    """Rank refinement variants by headroom over our gates, not raw Sharpe.

    Raw Sharpe would happily pick a variant with 60% turnover that can never
    pass HIGH_TURNOVER. Headroom is a weakest-link score, so it pushes the
    ladder toward alphas that clear *every* gate with room to spare.
    """
    if not rec or rec.get("status") != "OK":
        return -1e9
    h = headroom(rec, g)
    fails = len(failed_checks(rec.get("checks") or []))
    # Payout tilt, and it belongs HERE rather than in headroom(): headroom is a
    # weakest-link feasibility score, and a 1.1x multiplier is a smaller prize,
    # not a near-failure. Folding it into that min() would let a low-paying but
    # perfectly submittable alpha look broken. As an additive term bounded by
    # 0.15 it cannot outweigh a single failed check (0.25), which is the
    # property that keeps this a preference.
    #
    # PROD_CORRELATION survival, by neutralization -- see config.py's
    # NEUTRALIZATION_SURVIVAL_WEIGHT docstring for the measured motivation.
    # Cancels to exactly zero once a lineage is past rung 1: rungs 2-5 never
    # change settings.neutralization, so cand and st.best in absorb()'s
    # difference always share the same value here and the tilt cancels in the
    # subtraction. It only has a real effect where different neutralizations
    # are actually being compared, i.e. rung 1 -- no rung-number special-case
    # needed, the cancellation falls out of the existing structure.
    try:
        neut = json.loads(rec.get("settings") or "{}").get("neutralization")
    except (ValueError, TypeError, AttributeError):
        neut = None
    return (h - 0.25 * fails
           + pyramid_tilt(rec.get("pyramid_multiplier"))
           + neutralization_tilt(neut, neut_survival))
