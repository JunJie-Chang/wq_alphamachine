"""Central configuration: platform constants, gates, and search grids.

Everything the machine treats as a "rule of the game" lives here so it can be
audited and tuned in one place. Values marked (verify) come from community
reverse-engineering of the BRAIN API and should be checked against your own
account tier before you trust them in production.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any

API_BASE = "https://api.worldquantbrain.com"

# --------------------------------------------------------------------------
# Scoring model
# --------------------------------------------------------------------------
# Fitness = Sharpe * sqrt(|Returns| / max(Turnover, 0.125))
#
# The max() clamp is the single most exploitable fact on the platform: below
# 12.5% turnover you earn ZERO additional fitness from trading less. So the
# decay/hump ladder should drive turnover *down to* ~0.125 and then stop --
# past that point you are only destroying returns. Call this the fitness knee.
FITNESS_TURNOVER_KNEE = 0.125


def fitness(sharpe: float, returns: float, turnover: float) -> float:
    """Reproduce BRAIN's fitness locally (used for offline ranking/what-ifs)."""
    if sharpe is None or returns is None or turnover is None:
        return 0.0
    return sharpe * ((abs(returns) / max(turnover, FITNESS_TURNOVER_KNEE)) ** 0.5)


# --------------------------------------------------------------------------
# Submission checks (IS checks returned in alpha["is"]["checks"])
# --------------------------------------------------------------------------
# Name -> (comparison, limit). "min" = value must be >= limit, "max" = <= limit.
# Limits are region/universe dependent; these are the USA/TOP3000 defaults.
# CONSULTANT thresholds (docs/BRAIN_RULES.md). These are NOT the same as the
# user-level WorldQuant Challenge numbers that circulate online: the widely
# quoted "Sharpe 1.25" is the USER bar. A consultant D1 alpha needs 1.58, so a
# gate built on 1.25 cheerfully forwards alphas the platform will reject --
# spending a submission slot to be told no.
CONSULTANT_CHECKS: dict[str, dict[str, tuple[str, float]]] = {
    "D1": {
        "LOW_SHARPE": ("min", 1.58),
        "LOW_FITNESS": ("min", 1.0),
        "LOW_TURNOVER": ("min", 0.01),
        "HIGH_TURNOVER": ("max", 0.70),
        "MAX_STOCK_WEIGHT": ("max", 0.10),
        "SELF_CORRELATION": ("max", 0.70),
        "PROD_CORRELATION": ("max", 0.70),
    },
    "D0": {
        "LOW_SHARPE": ("min", 2.69),
        "LOW_FITNESS": ("min", 1.5),
        "LOW_TURNOVER": ("min", 0.01),
        "HIGH_TURNOVER": ("max", 0.70),
        "MAX_STOCK_WEIGHT": ("max", 0.10),
        "SELF_CORRELATION": ("max", 0.70),
        "PROD_CORRELATION": ("max", 0.70),
    },
}
# CHN runs its own, much harder ladder.
CHN_CHECKS = {
    "D1": {"LOW_SHARPE": ("min", 2.08), "LOW_RETURNS": ("min", 0.08),
           "LOW_FITNESS": ("min", 1.0)},
    "D0": {"LOW_SHARPE": ("min", 3.50), "LOW_RETURNS": ("min", 0.12),
           "LOW_FITNESS": ("min", 1.5)},
}
PLATFORM_CHECKS: dict[str, tuple[str, float]] = CONSULTANT_CHECKS["D1"]


def platform_checks_for(region: str = "USA", delay: int = 1) -> dict:
    key = "D0" if int(delay) == 0 else "D1"
    if str(region).upper() == "CHN":
        return {**CONSULTANT_CHECKS[key], **CHN_CHECKS[key]}
    return CONSULTANT_CHECKS[key]


# IS Ladder Sharpe thresholds by years of history. Multiply PASS thresholds
# (not FAIL_THRESHOLD) by 0.85 when turnover < 30%.
IS_LADDER: dict[str, dict[int, float]] = {
    "D1": {0: 1.59, 2: 2.38, 3: 2.38, 4: 2.38, 5: 2.38,
           6: 2.22, 7: 2.06, 8: 1.90, 9: 1.74, 10: 1.59},
    "D0": {0: 2.69, 2: 3.96, 3: 3.96, 4: 3.96, 5: 3.96,
           6: 3.64, 7: 3.33, 8: 3.17, 9: 2.85, 10: 2.69},
}

# Sub-universe requirement is a FORMULA, not a constant:
#   TOPXXX:  sub_sharpe >= 0.75 * sqrt(sub_size / universe_size) * sharpe
NON_TOP_SUBUNIVERSE_RATIO = {
    "ASI:MINVOL1M": 0.295,
    "USA:ILLIQUID_MINVOL1M": 0.41,
    "EUR:ILLIQUID_MINVOL1M": 0.355,
}

UNIVERSE_SIZE = {
    "TOP3000": 3000, "TOP2500": 2500, "TOP2000": 2000, "TOP1600": 1600,
    "TOP1200": 1200, "TOP1000": 1000, "TOP800": 800, "TOP600": 600,
    "TOP500": 500, "TOP400": 400, "TOP200": 200, "TOPSP500": 500,
    "TOPDIV3000": 3000, "TOP2000U": 2000,
}


def subuniverse_floor(sharpe: float, universe: str, region: str = "USA",
                      sub_universe: str | None = None) -> float:
    """Minimum sub-universe Sharpe this alpha must retain."""
    key = f"{region.upper()}:{universe.upper()}"
    if key in NON_TOP_SUBUNIVERSE_RATIO:
        return NON_TOP_SUBUNIVERSE_RATIO[key] * abs(sharpe)
    size = UNIVERSE_SIZE.get(universe.upper(), 3000)
    sub = UNIVERSE_SIZE.get((sub_universe or "").upper(), max(size // 3, 200))
    return 0.75 * ((sub / size) ** 0.5) * abs(sharpe)
# Checks with no numeric limit -- pass/fail only.
BOOLEAN_CHECKS = ("CONCENTRATED_WEIGHT", "UNITS", "MATCHES_COMPETITION", "IS_LADDER_SHARPE")


@dataclass
class Gates:
    """Our gates -- set to the actual consultant floor, not a padded guess.

    docs/BRAIN_RULES.md (the authoritative source, per the user's own official
    documents) puts the real D1 consultant floor at Sharpe > 1.58, Fitness >
    1.0 -- NOT the 1.25 commonly quoted online, and also not an arbitrarily
    inflated number on top of 1.58. Pushing the gate to e.g. 1.80 "for margin"
    mostly just throws away real candidates (1.7x-Sharpe alphas that are
    already above the true floor) without a documented reason to justify it.

    BLOCK vs PREFER -- the distinction this class got wrong for a long time
    ------------------------------------------------------------------------
    BRAIN_RULES.md carries two different kinds of number and they were being
    applied as if they were one:

      * SUBMISSION requirements (line 17-20) -- what BRAIN actually rejects.
        Turnover 1%-70%, self/prod correlation < 0.7.
      * VALUE FACTOR guidance (line 76-77) -- "官方提高建議", advice on what
        EARNS well once submitted: turnover < 30%, retain 70% of Sharpe in the
        sub/super universe, margin > 4bps.

    The second set was wired in as a hard rejection, so a preference about
    payout was silently deciding what was allowed to exist. Measured cost, on
    the database as it stood: of 100 distinct ideas clearing Sharpe 1.58 and
    Fitness 1.0, only 19 survived -- turnover alone hid 39 and the sub-universe
    rule hid another 34. Among the hidden was the best candidate the pipeline
    has ever produced (Sharpe 2.64 / Fitness 1.63), whose real server check
    came back PASS on every line except PROD_CORRELATION at 0.767.

    So: the `*_max` / `*_min` fields below are BLOCKS and mirror what BRAIN
    rejects. The `*_ideal_*` fields are PREFERENCES -- they steer `headroom()`,
    and through it the refinement ladder, toward the Value-Factor sweet spot
    without discarding anything BRAIN would have accepted. Widening the block
    costs no extra API budget: deep checks are capped per run and spent in
    score order, so a bigger eligible pool only improves what the cap buys.
    """
    # Survival bar for the coarse scan. Kept below where you might set it by
    # instinct: in the first real run the ONLY candidate that went on to clear
    # the gate scanned at 0.94 -- a sparse field whose signal only appeared
    # once ts_backfill was applied in refinement. A 1.0 bar would have culled
    # the single winner, so the scan must stay generous about sparse data.
    scan_abs_sharpe: float = 0.65
    sharpe: float = 1.58                 # CONSULTANT D1 floor (BRAIN_RULES.md)
    fitness: float = 1.00                # consultant D1 floor (BRAIN_RULES.md)
    turnover_min: float = 0.01
    turnover_max: float = 0.70           # BRAIN_RULES.md:17 submission ceiling
    # PREFERENCE, not a block: Value Factor guidance (BRAIN_RULES.md:77) is
    # turnover < 30%, and low turnover also earns a discount on the PASS
    # threshold (line 44). headroom() scores against this, so the refinement
    # ladder still climbs toward it -- it just no longer deletes the rest.
    turnover_ideal_max: float = 0.30
    # PREFERENCE. The BLOCK for sub-universe Sharpe is BRAIN's own per-alpha
    # limit, which every simulation record carries verbatim in its checks
    # payload (`LOW_SUB_UNIVERSE_SHARPE`, e.g. limit 1.14 against Sharpe 2.64
    # -- that is 0.433x, the BRAIN_RULES.md:27 formula, NOT a flat 0.70). See
    # metrics.quality_verdict; subuniverse_floor() below is the fallback when
    # a record predates the check.
    subuniverse_ratio: float = 0.70
    max_drawdown: float = 0.12
    min_margin_bps: float = 4.0          # official guidance: margin > 4 bps
    max_self_corr: float = 0.70          # BRAIN_RULES.md:19 platform ceiling
    max_prod_corr: float = 0.70          # BRAIN_RULES.md:20 platform ceiling
    min_years_positive: float = 0.70     # fraction of IS years with +PnL
    max_year_pnl_share: float = 0.50     # no single year carries the alpha
    # BLOCK, and BRAIN's own number: LOW_2Y_SHARPE carries limit 1.58 in every
    # simulation payload that reports it. Every other threshold here is a
    # whole-period aggregate, which is how three candidates that had cleared
    # PROD_CORRELATION for the first time ever still turned out to be dead --
    # Sharpe 2.39 over 2014-2023, but 0.48 over the trailing two years,
    # essentially all of it earned in 2015-2016. Absence of the value means
    # BRAIN sent IS_LADDER_SHARPE instead, NOT that the alpha is fine, so
    # quality_verdict only applies this when the number is actually present.
    min_two_year_sharpe: float = 1.58
    # BLOCK, and a real re-simulation rather than a formula estimate. Rung 5
    # (refine.py) already re-tests every champion on 1-2 tighter universes as
    # a falsification check; this is the ratio of that REAL Sharpe to
    # subuniverse_floor()'s prediction for the same universe. 1.0 means the
    # champion barely clears the same bar BRAIN's own LOW_SUB_UNIVERSE_SHARPE
    # check approximates -- and that check can pass on a formula estimate
    # while the true number still collapses: observed live, an alpha reported
    # 1.33 against a limit of 0.90 (comfortable PASS) while its real TOP1000
    # resimulation came back 0.68 against the same formula's 0.90 floor
    # (ratio 0.76) and TOP500 came back 0.17 against a 0.64 floor (ratio
    # 0.27) -- a liquidity artefact BRAIN's own check would not have caught.
    min_portability_ratio: float = 1.0


# --------------------------------------------------------------------------
# Simulation settings
# --------------------------------------------------------------------------
REGIONS = ("USA", "GLB", "EUR", "ASI", "CHN", "JPN", "KOR", "TWN", "HKG", "AMR")

# refine's portability rung tries unis[1:3]. Every entry must be a universe BRAIN
# accepts for that region: CHN/TOP3000 and ASI/ILLIQUID_MINVOL1M were listed and
# are not ("Universe TOP3000 is not available for instrument type EQUITY and
# region CHN", 2026-09-26) -- each one 400'd its whole bundle.
UNIVERSES: dict[str, tuple[str, ...]] = {
    "USA": ("TOP3000", "TOP1000", "TOP500", "TOP200", "TOPSP500"),
    "GLB": ("TOPDIV3000", "TOP3000", "MINVOL1M"),
    "EUR": ("TOP2500", "TOP1200", "TOP800", "TOP400"),
    "ASI": ("MINVOL1M",),
    "CHN": ("TOP2000U",),
    "JPN": ("TOP1600", "TOP1200"),
    "KOR": ("TOP600",),
    "TWN": ("TOP500",),
    "HKG": ("TOP800",),
    "AMR": ("TOP600",),
}

NEUTRALIZATIONS = (
    "NONE", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY",
    "COUNTRY", "STATISTICAL", "CROWDING", "FAST", "SLOW", "SLOW_AND_FAST",
)


@dataclass
class SimSettings:
    """One point in BRAIN's simulation-settings space."""
    instrumentType: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    pasteurization: str = "ON"
    unitHandling: str = "VERIFY"
    nanHandling: str = "ON"
    maxTrade: str = "OFF"
    # NOT a holdout: measured over 2,992 stored payloads, is.pnl == train.pnl +
    # test.pnl to within 0.5%, while is.pnl differs from train.pnl by 16%. The
    # IS metrics every gate reads INCLUDE this window. What testPeriod actually
    # buys is a separate `test` block in the response covering the trailing
    # period -- which is where metrics.extract() gets test_sharpe, the recency
    # proxy. (SimSettings.key() omits testPeriod, so changing it would collide
    # candidate ids with existing rows rather than producing new ones.)
    testPeriod: str = "P1Y6M"
    language: str = "FASTEXPR"
    visualization: bool = False

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    def replace(self, **kw) -> "SimSettings":
        d = asdict(self)
        d.update(kw)
        return SimSettings(**d)

    def key(self) -> str:
        return (f"{self.region}/{self.universe}/d{self.delay}/dc{self.decay}/"
                f"{self.neutralization}/t{self.truncation}")


# Stage-2 coarse scan uses ONE setting per candidate. Cheap, comparable,
# and good enough to tell signal from noise.
SCAN_SETTINGS = SimSettings()

# Stage-3 refinement ladder. Ordered by (expected Sharpe gain) / (sims spent).
# Neutralization first: it is the biggest single lever and costs ~9 sims.
#
# Widened from 5 to 9 after probing what BRAIN actually accepts for
# EQUITY/USA/TOP3000/d1 (scratch probe, 2026-08-31). NEUTRALIZATIONS above has
# listed 11 values since day one and is read by nothing -- the ladder only ever
# tried these 5, so STATISTICAL, CROWDING, FAST and SLOW had never been
# simulated once. On the probe expression they were three of the four best:
#
#   STATISTICAL  3.48    SLOW  3.11    CROWDING 3.04 (fitness 1.22, the best)
#   SLOW_AND_FAST 2.98  <- the best the old tuple could reach
#   SUBINDUSTRY  2.60    FAST  2.57    MARKET 2.56   SECTOR 2.47  INDUSTRY 2.44
#
# Rejected by BRAIN for this region/instrument, so deliberately NOT here:
#   COUNTRY -- "not available for instrument type EQUITY and region USA"
#   RAM     -- '"RAM" is not a valid choice' (it appears in docs/ for Power
#              Pools, but is not a value this API accepts)
# NONE is accepted but excluded: it removes no factor exposure at all, which is
# the opposite of the mechanism being targeted here (a positive mean
# correlation against the production pool is a factor-exposure signature), and
# it scored 0.88 against 2.44-3.48 for every other option on the same
# expression. Add it back only with a reason.
#
# Cost: rung 1 goes from 4 variants per ladder to 8, so ~100 extra simulations
# per `refine --top 25` run -- roughly 10 minutes at observed throughput.
REFINE_NEUTRALIZATION = ("INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET",
                         "SLOW_AND_FAST", "STATISTICAL", "CROWDING",
                         "FAST", "SLOW")
REFINE_DECAY = (0, 4, 8, 16, 32, 64)
REFINE_TRUNCATION = (0.01, 0.04, 0.08, 0.15)

# --------------------------------------------------------------------------
# Payout: the Dynamic Pyramid Multiplier
# --------------------------------------------------------------------------
# A pyramid is region x delay x dataset category (e.g. USA/D1/ANALYST). BRAIN
# publishes a per-pyramid multiplier in the range 1.0-2.0, driven by how little
# that pyramid's data is used platform-wide, and applies it to the base
# payment's Quality Factor. Every simulation carries the alpha's own value in
# the MATCHES_PYRAMID check -- on 100% of OK sims -- so this costs no API call.
#
# Two measured facts shape how it is used:
#
#   * The reported multiplier is the MINIMUM over every pyramid the expression
#     matches (9,414 of 9,414 multi-pyramid alphas; never the max or mean). So
#     writing `returns`/`cap`/`volume`/`adv20` into an expression pulls in
#     USA/D1/PV at 1.1 and holds the whole alpha there. The budget-weighted
#     average across 24,277 simulations is 1.186 against 1.4 available.
#   * It is NOT keyed by our `theme` tag, which agrees with BRAIN's actual
#     pyramid category on only 25% of simulations. It is a property of the
#     expression's datasets, which is why it is read per candidate rather than
#     folded into a bandit arm.
#
# This is a PREFER, never a BLOCK: it is payout, not a submission requirement.
# The weight is deliberately smaller than refine._score's 0.25-per-failed-check
# penalty, so payout can break a tie between two feasible variants and can
# never buy a broken one.
PYRAMID_SCORE_WEIGHT = 0.15
# A pyramid counts as "formulated" toward Genius tier at this many submitted
# alphas, as BRAIN counts them (client.pyramid_tally) -- never a local sum of
# MATCHES_PYRAMID, whose assignment rule BRAIN does not publish. Expert needs
# 10 formulated pyramids, Master 30, Grand Master 60.
# Tier sets the biometric re-auth interval (Gold 4h -> GM 24h), which is an
# operational cost, not only a payout one.
PYRAMID_MIN_ALPHAS = 3
GENIUS_PYRAMIDS = {"EXPERT": 10, "MASTER": 30, "GRAND_MASTER": 60}


def pyramid_tilt(multiplier: float | None, weight: float = PYRAMID_SCORE_WEIGHT
                 ) -> float:
    """Payout preference as a signed offset, 0.0 when unknown.

    Unknown is neutral here, unlike a gate: a missing multiplier must not
    penalise a candidate whose payload predates the column.
    """
    if multiplier is None:
        return 0.0
    return weight * (float(multiplier) - 1.0)


# --------------------------------------------------------------------------
# Which neutralization actually survives PROD_CORRELATION
# --------------------------------------------------------------------------
# REFINE_NEUTRALIZATION above fixed WHICH neutralizations get tried at rung 1
# (5 -> 9). This fixes a different bug: which one WINS the ladder's champion
# election. refine._score has no visibility into PROD_CORRELATION at rung-1
# time -- it is only measured later, on whichever champion survives to
# `select` -- so the ladder keeps electing classic-neutralization champions on
# raw Sharpe, and the limited deep-check budget is spent confirming what the
# accumulated history already says. Measured on 632 deep-checked alphas:
#
#   STATISTICAL  12/18 (66.7%)   SLOW  6/6 (100%)
#   MARKET        0/258 (0%)     INDUSTRY 0/124 (0%)   SECTOR 0/85 (0%)
#   SUBINDUSTRY   0/94 (0%)
#
# 89% of deep-check volume goes to the four neutralizations with a measured 0%
# survival rate. This matches an independently-derived mechanism already in
# this file's history (STATISTICAL/SLOW/CROWDING produce near-zero or negative
# mean correlation with roughly a third the spread of the classic five -- see
# REFINE_NEUTRALIZATION's comment) -- but the winning theme::template combos
# so far were only ever MEASURED under STATISTICAL/SLOW, never under a classic
# neutralization on the same idea, so some of this gap could still be which
# ideas got explored rather than neutralization alone. Hence a bounded,
# self-correcting PREFERENCE, not a hard rule.
NEUTRALIZATION_SURVIVAL_PRIOR = (1.0, 3.0)   # Beta(1,3), mean 0.25
# Bounded below refine._score's 0.25-per-failed-check penalty, same reasoning
# as PYRAMID_SCORE_WEIGHT: must be able to break a near-tie between two rung-1
# variants, must never be mistaken for a passing check.
NEUTRALIZATION_SURVIVAL_WEIGHT = 0.20


def neutralization_tilt(neutralization: str | None,
                        survival: dict[str, tuple[int, int]] | None,
                        weight: float = NEUTRALIZATION_SURVIVAL_WEIGHT) -> float:
    """Posterior mean PROD_CORRELATION survival probability, scaled.

    0.0 when the neutralization or the survival table is unknown -- absence
    must never read as a penalty, only as "no preference yet".
    """
    if not neutralization or not survival:
        return 0.0
    wins, trials = survival.get(neutralization, (0, 0))
    a, b = NEUTRALIZATION_SURVIVAL_PRIOR
    prob = (a + wins) / (a + b + trials)
    return weight * prob


# Coverage below this makes a field sparse enough that the raw series is
# mostly gaps: ts_delta reads NaN, ranks concentrate on the few names that
# have values, and the signal looks like noise until it is backfilled. 38% of
# usable USA fields sit under 0.95, so this is the common case, not the edge.
SPARSE_COVERAGE = 0.95
BACKFILL_DAYS = 120

# Per-data-type defaults. Fundamental data changes quarterly, so decaying it
# is pointless; technical data is noisy, so it needs heavy smoothing.
# Seeded from THIS account's realised conversion, not from generic advice:
# of 1,100 simulations, MARKET took 57% of the budget and produced 2 of 13
# ACTIVE alphas, while INDUSTRY took 28% and produced 9. So INDUSTRY is the
# default here and SUBINDUSTRY is what the refinement ladder tries next.
TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fundamental": {"decay": 0, "neutralization": "INDUSTRY"},
    "analyst":     {"decay": 4, "neutralization": "INDUSTRY"},
    "news":        {"decay": 8, "neutralization": "INDUSTRY"},
    "sentiment":   {"decay": 8, "neutralization": "INDUSTRY"},
    "option":      {"decay": 4, "neutralization": "INDUSTRY"},
    "institutions":{"decay": 4, "neutralization": "INDUSTRY"},
    "insiders":    {"decay": 8, "neutralization": "INDUSTRY"},
    "shortinterest":{"decay": 4, "neutralization": "INDUSTRY"},
    "model":       {"decay": 4, "neutralization": "INDUSTRY"},
    "macro":       {"decay": 16, "neutralization": "MARKET"},
    "pv":          {"decay": 20, "neutralization": "INDUSTRY"},
    "risk":        {"decay": 8, "neutralization": "INDUSTRY"},
    # Order imbalance is microstructure, i.e. the same kind of fast, noisy
    # series as price-volume, so it starts where `pv` does. No realised
    # conversion of its own yet -- the bandit will correct this if it is wrong.
    "imbalance":   {"decay": 20, "neutralization": "INDUSTRY"},
}
# `other` is deliberately absent. default_settings_for() falls through to the
# base SimSettings (decay 0 / SUBINDUSTRY) for any theme not listed, and giving
# `other` an entry here would change its settings key, hence every candidate id
# built from it -- re-paying for simulations already on file. The residual
# bucket is also much smaller now that `fundamental` matches its own name.


@dataclass
class AutoGates(Gates):
    """The bar for submitting WITHOUT a human looking first.

    Deliberately a tier above the manual gates. The asymmetry is the whole
    point: a bad simulation wastes a slot, a bad submission occupies book space
    and drags the score, and cannot be withdrawn. So automation is reserved for
    the cases that are not close -- anything borderline is queued for review
    rather than sent.
    """
    sharpe: float = 2.10              # manual 1.58, consultant floor 1.58
    fitness: float = 1.50             # manual 1.00, consultant floor 1.00
    # These stay well inside the submission ceilings ON PURPOSE. Unlike the
    # manual gates -- where being stricter than BRAIN silently destroyed
    # candidates -- here the asymmetry is the point, because nothing downstream
    # reviews the decision.
    turnover_max: float = 0.22        # manual 0.70 (ceiling), ideal 0.30
    turnover_ideal_max: float = 0.22  # keep score and block aligned here
    max_self_corr: float = 0.40       # manual 0.70, platform 0.70
    max_prod_corr: float = 0.45       # manual 0.70, platform 0.70
    subuniverse_ratio: float = 0.80   # manual 0.70 (official guidance)
    max_drawdown: float = 0.10        # manual 0.12
    min_years_positive: float = 0.80  # manual 0.70
    max_year_pnl_share: float = 0.40  # manual 0.50
    # Extra conditions that only apply to unattended submission.
    min_headroom: float = 1.25        # must clear EVERY gate by 25%
    require_all_checks_pass: bool = True


@dataclass
class RunConfig:
    email: str = field(default_factory=lambda: os.environ.get("WQ_EMAIL", ""))
    password: str = field(default_factory=lambda: os.environ.get("WQ_PASSWORD", ""))
    db_path: str = "data/machine.db"
    cache_dir: str = "data/cache"
    # Concurrency == your account's simulation-slot count. Setting it above
    # your real slot count just builds a server-side queue -- it does not make
    # you faster, and it costs you the ability to react to results as they land.
    # Account slot count, as reported by the account owner: 13 (was 3 until
    # 2026-09-21). The platform exposes no number for it -- /users/self does not
    # carry one -- so this is configuration, not something the code can read.
    concurrency: int = 13
    poll_interval: float = 5.0
    # How long a worker polls ITS OWN bundle before deferring it. Giving up does
    # not free the slot -- BRAIN keeps running the job -- it only frees the worker
    # to dispatch more, which then waits for a slot its own abandoned bundles hold.
    # At 900s that is what happened on GLB/TOP3000 (2026-09-25): a 10-alpha GLB
    # bundle ran past 15 minutes, and 50 of 200 then 110 of 150 refine variants
    # were deferred unsimulated. GLB ran 100-870 alphas/hr against 1,500-2,000
    # for EUR/CHN the same day.
    sim_timeout: float = 2400.0
    # reclaim() runs before EVERY dispatch, on the same `concurrency`-worker thread
    # pool as run() (fixed 2026-09-24 -- it was sequential, one bundle at a time,
    # which meant a backlog of N leftover bundles cost N * ~90s of wall clock no
    # matter the account's real slot count; confirmed live the same day with 83
    # bundles draining at roughly one per 90s). Each bundle can still itself cost
    # up to child_result()'s own 300s timeout per child if BRAIN's backend is slow
    # to answer. A bundle no free worker has picked up by this deadline is left
    # untouched (marker kept, tried again next run); one ALREADY being polled is
    # allowed to finish rather than cut off mid-way.
    reclaim_budget_seconds: float = 300.0
    max_submits_per_day: int = 4
    gates: Gates = field(default_factory=Gates)
    # Unattended submission is off unless explicitly enabled, and capped
    # separately and more tightly than manual submission.
    auto_submit: bool = False
    auto_gates: AutoGates = field(default_factory=AutoGates)
    # Consultant base payment counts only the FIRST 4 ALPHAS (+1 SuperAlpha)
    # of each day -- the widely quoted "2,000 points/day" ceiling belongs to
    # the user-level Challenge, not to this programme.
    #
    # The binding constraint is not daily volume but SUBMITTING DAYS: fewer
    # than 20 submission days in a rolling 3 months forces Value Factor to its
    # minimum, and quarterly payment requires >=20 distinct days. So the policy
    # is "submit every day, 1-4 per day", never "bank them and dump a batch".
    max_auto_submits_per_day: int = 4
    daily_counted_alphas: int = 4
    min_submission_days_per_quarter: int = 20
    hold_surplus: bool = True
    # Deep gate (yearly consistency + PROD_CORRELATION) during refinement.
    # A simulation record reports both correlation checks as PENDING, so
    # without this the bandit's reward cannot see the two gates that in
    # practice block most alphas which clear every local metric.
    #
    # Run only on champions that already passed quality_verdict, which keeps
    # it cheap; /check is lazily computed server-side and polls on
    # Retry-After, so the per-run cap bounds the slow path.
    deep_check_in_refine: bool = True
    deep_check_ttl_hours: float = 24.0
    max_deep_checks_per_refine: int = 10
