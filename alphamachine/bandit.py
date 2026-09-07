"""Thompson sampling over (theme x template) arms.

Why a bandit and not a grid
---------------------------
Simulation slots are the binding constraint -- a few thousand sims a day at
best. A uniform grid spends the same budget on `news x acceleration` (which
converts at ~1%) as on `analyst x surprise` (which may convert at ~15%). The
bandit reallocates budget toward what is actually converting *for your account,
in your region, against your existing book* -- which is not knowable in advance
and drifts as your book fills up and self-correlation bites.

Reward is deliberately the SUBMITTABLE rate, not Sharpe. Optimising mean Sharpe
pushes the machine toward crowded factors that score well in sample and then
die at the correlation gate.

"Submittable" means the whole funnel, not one metric check:

    scan  --  |sharpe| cleared the coarse survival bar
    gate  --  quality_verdict: sharpe/fitness/turnover/sub-universe/margin
    deep  --  yearly consistency and PROD_CORRELATION

The last stage matters more than its cost suggests. A simulation record reports
SELF_CORRELATION and PROD_CORRELATION as PENDING with no value, so the metric
gate is structurally blind to them, and it never sees a one-lucky-year equity
curve at all. Both need extra API calls, and both routinely block alphas that
sailed through the first two stages -- so an arm that keeps producing them is
converting far worse than the gate rate implies.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class Arm:
    name: str
    trials: int = 0
    scan_trials: int = 0
    scan_pass: int = 0
    refine_trials: int = 0
    gate_pass: int = 0
    corr_trials: int = 0
    corr_pass: int = 0
    deep_trials: int = 0
    deep_pass: int = 0
    submitted: int = 0
    sum_sharpe: float = 0.0

    def sample(self, rng: random.Random, prior_a: float = 1.0,
               prior_b: float = 6.0) -> float:
        """Thompson draw of the whole funnel conversion rate.

            P(scan_pass) x P(gate_pass | scan_pass) x P(deep_pass | gate_pass)

        Each stage keeps its own denominator. Pooling them -- which is what the
        single `trials` counter used to do -- charges an arm for scan
        simulations that never reached the gate, so the more budget an arm won
        the worse it scored. That is a feedback loop pointing the wrong way.

        Drawing each stage independently and multiplying is the standard
        funnel approximation: the product of the draws is a sample of the
        product of the rates, which is the quantity worth maximising (alphas
        submitted per simulation spent).

        Prior strength is matched to how much data each stage sees. Stage 1 has
        hundreds of observations and takes a uniform prior. The pessimistic
        (1, 6) belongs on stage 2, where a single lucky hit on a fresh arm
        could otherwise hijack the budget. Stage 3 is sparser still but gets
        only a mild (1, 3): stacking three pessimistic priors would crush every
        arm toward zero and degrade the allocation into noise.
        """
        s1 = rng.betavariate(1.0 + self.scan_pass,
                             1.0 + max(0, self.scan_trials - self.scan_pass))
        s2 = rng.betavariate(prior_a + self.gate_pass,
                             prior_b + max(0, self.refine_trials - self.gate_pass))
        # Stage 3 learns on corr_*, not deep_*. deep_pass is 0 for EVERY arm
        # on this account's whole history -- it is a composite bit that only
        # fires when yearly consistency, recency and crowding all pass at once,
        # so it carries no information to separate arms and the posterior is
        # just the prior. corr_* isolates the part an arm actually controls
        # (did PROD_CORRELATION clear its limit), measured on 152 independent
        # lineages instead of a bit that is always zero. deep_* is still
        # recorded and still reported -- it just no longer drives allocation.
        s3 = rng.betavariate(1.0 + self.corr_pass,
                             3.0 + max(0, self.corr_trials - self.corr_pass))
        return s1 * s2 * s3

    @property
    def rate(self) -> float:
        """Gate conversion among alphas that actually reached the gate."""
        return self.gate_pass / self.refine_trials if self.refine_trials else 0.0

    @property
    def scan_rate(self) -> float:
        return self.scan_pass / self.scan_trials if self.scan_trials else 0.0

    @property
    def deep_rate(self) -> float:
        return self.deep_pass / self.deep_trials if self.deep_trials else 0.0

    @property
    def corr_rate(self) -> float:
        """PROD_CORRELATION survival -- the stage sample() actually learns on."""
        return self.corr_pass / self.corr_trials if self.corr_trials else 0.0


class Bandit:
    def __init__(self, store, arms: list[str], seed: int = 0,
                 priors: dict[str, tuple[float, float]] | None = None):
        self.store = store
        self.rng = random.Random(seed or None)
        # Optional LLM-derived pseudo-counts, keyed by arm. They shift where
        # sampling STARTS; they never override evidence, because they are added
        # to the same Beta posterior that real trials update.
        self.priors = priors or {}
        rows = {r["arm"]: r for r in store.arms()}
        self.arms = {}
        for name in arms:
            r = rows.get(name)
            self.arms[name] = Arm(
                name,
                trials=_col(r, "trials"),
                scan_trials=_col(r, "scan_trials"),
                scan_pass=_col(r, "scan_pass"),
                refine_trials=_col(r, "refine_trials"),
                gate_pass=_col(r, "gate_pass"),
                corr_trials=_col(r, "corr_trials"),
                corr_pass=_col(r, "corr_pass"),
                deep_trials=_col(r, "deep_trials"),
                deep_pass=_col(r, "deep_pass"),
                submitted=_col(r, "submitted"),
                sum_sharpe=_col(r, "sum_sharpe", 0.0),
            )

    def allocate(self, budget: int) -> dict[str, int]:
        """Split `budget` simulations across arms by Thompson sampling."""
        alloc: dict[str, int] = {a: 0 for a in self.arms}
        if not self.arms:
            return alloc
        for _ in range(budget):
            pick = max(self.arms.values(), key=lambda a: self._sample(a))
            alloc[pick.name] += 1
        return alloc

    def _sample(self, arm: Arm) -> float:
        pa, pb = self.priors.get(arm.name, (1.0, 6.0))
        # Keep the pessimistic floor: an LLM's enthusiasm must not, on its own,
        # look like a proven arm.
        return arm.sample(self.rng, prior_a=max(pa, 1.0), prior_b=max(pb, 1.0))

    # Which counters each funnel stage advances. Naming the stage explicitly
    # beats the old pair of booleans: a caller can no longer record a gate
    # result without also declaring which population the trial belongs to,
    # which is how the denominators got conflated in the first place.
    STAGES = {
        "scan":      ("scan_trials", "scan_pass"),
        "gate":      ("refine_trials", "gate_pass"),
        # "corr" is what sample() learns on; "deep" is true end-to-end
        # conversion, kept for reporting. A caller records both, separately.
        "corr":      ("corr_trials", "corr_pass"),
        "deep":      ("deep_trials", "deep_pass"),
        "submitted": (None, "submitted"),
    }

    def update(self, arm: str, *, stage: str, passed: bool = False,
               sharpe: float = 0.0) -> None:
        try:
            trial_col, pass_col = self.STAGES[stage]
        except KeyError:
            raise ValueError(f"unknown bandit stage {stage!r}; "
                             f"expected one of {sorted(self.STAGES)}") from None
        kw: dict[str, int] = {pass_col: int(bool(passed))}
        if trial_col:
            kw[trial_col] = 1
        # `trials` stays the cross-stage total, for reporting only. A
        # submission is an outcome of an existing trial, not a new one.
        self.store.bump_arm(arm, trial=1 if trial_col else 0,
                            sharpe=abs(sharpe), **kw)
        a = self.arms.get(arm)
        if a:
            if trial_col:
                a.trials += 1
                setattr(a, trial_col, getattr(a, trial_col) + 1)
            setattr(a, pass_col, getattr(a, pass_col) + int(bool(passed)))
            a.sum_sharpe += abs(sharpe)

    def report(self) -> list[tuple[str, int, float, float, float, float]]:
        """(arm, trials, scan%, gate%, corr%, avg|sharpe|), worst-stage first.

        Sorted by the composite conversion so the table answers "where is the
        budget converting", and the per-stage columns show WHERE an arm dies
        rather than collapsing three very different failures into one number.

        The last rate is corr%, not deep% -- deep% is 0 for every arm ever
        recorded here, so a column of zeroes told the reader nothing. corr%
        (PROD_CORRELATION survival) is the stage sample() learns on and the
        one that actually varies.
        """
        rows = []
        for a in self.arms.values():
            rows.append((a.name, a.trials, a.scan_rate, a.rate, a.corr_rate,
                         a.sum_sharpe / a.trials if a.trials else 0.0))
        return sorted(rows, key=lambda r: (-(r[2] * r[3]), -r[3], -r[1]))


def arm_name(theme: str, template: str) -> str:
    return f"{theme}::{template}"


def _col(row, name: str, default=0):
    """Read a column that an older database may predate.

    Store._migrate() adds the funnel columns on open, so this is belt-and-
    braces for a row object built before that ran (notably in tests).
    """
    if row is None:
        return default
    try:
        v = row[name]
    except (IndexError, KeyError):
        return default
    return default if v is None else v
