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
    Where a number below IS backed by official guidance (turnover_max,
    subuniverse_ratio, min_margin_bps) that guidance is kept as-is.
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
    # Official guidance for Value Factor (the quarterly-payment driver) is
    # turnover under 30%, which is stricter than the 70% submission ceiling.
    turnover_max: float = 0.30
    # Official guidance: retain 70% of Sharpe in sub/super universe.
    subuniverse_ratio: float = 0.70
    max_drawdown: float = 0.12
    min_margin_bps: float = 4.0          # official guidance: margin > 4 bps
    max_self_corr: float = 0.55          # platform ceiling 0.70
    max_prod_corr: float = 0.60          # platform ceiling 0.70
    min_years_positive: float = 0.70     # fraction of IS years with +PnL
    max_year_pnl_share: float = 0.50     # no single year carries the alpha


# --------------------------------------------------------------------------
# Simulation settings
# --------------------------------------------------------------------------
REGIONS = ("USA", "GLB", "EUR", "ASI", "CHN", "JPN", "KOR", "TWN", "HKG", "AMR")

UNIVERSES: dict[str, tuple[str, ...]] = {
    "USA": ("TOP3000", "TOP1000", "TOP500", "TOP200", "TOPSP500"),
    "GLB": ("TOPDIV3000", "TOP3000", "MINVOL1M"),
    "EUR": ("TOP2500", "TOP1200", "TOP800", "TOP400"),
    "ASI": ("MINVOL1M", "ILLIQUID_MINVOL1M"),
    "CHN": ("TOP2000U", "TOP3000"),
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
    testPeriod: str = "P1Y6M"   # holdout window kept out of the IS metrics
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
# Neutralization first: it is the biggest single lever and costs ~5 sims.
REFINE_NEUTRALIZATION = ("INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET", "SLOW_AND_FAST")
REFINE_DECAY = (0, 4, 8, 16, 32, 64)
REFINE_TRUNCATION = (0.01, 0.04, 0.08, 0.15)

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
}


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
    turnover_max: float = 0.22        # manual 0.30, submission ceiling 0.70
    max_self_corr: float = 0.40       # manual 0.55, platform 0.70
    max_prod_corr: float = 0.45       # manual 0.60, platform 0.70
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
    # CONFIRMED for this account: 3 slots.
    concurrency: int = 3
    poll_interval: float = 5.0
    sim_timeout: float = 900.0
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
