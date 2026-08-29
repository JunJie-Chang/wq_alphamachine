"""The transform taxonomy -- the machine's actual source of alpha diversity.

Thesis
------
Correlation between two alphas is driven far more by the *shape of the
transform* than by the identity of the field. Two level-signals on different
fundamental fields typically correlate 0.7-0.85 (they are all "cheap vs
expensive"). A level-signal and a change-signal on the SAME field typically
correlate 0.2-0.4.

So the search axis that matters is transform-first, field-second. Each family
below is a distinct economic hypothesis, not a parameter tweak:

  LEVEL         cross-sectional cheapness / quality ranking
  CHANGE        the derivative -- improvement, not level
  SURPRISE      deviation from the stock's OWN history (self-referential z)
  ACCELERATION  second derivative -- inflection
  STABILITY     signal quality: level scaled by its own dispersion
  RELATIVE      level minus peer-group level (pure idiosyncratic)
  YIELD         field normalised by a size denominator
  RESIDUAL      part of the field unexplained by a benchmark
  CONSISTENCY   trailing co-movement of field with returns
  EVENT         signal only traded when a condition fires (turnover killer)

The second axis: HOW MANY data sources
---------------------------------------
The taxonomy above took the transform axis seriously and the field-count axis
not at all -- every template above names exactly one field. That ceiling is now
measured, not suspected. PROD_CORRELATION is the MAXIMUM correlation against
any single alpha already live in BRAIN's production pool; the limit is 0.70.
Across the 16 distinct one-field ideas whose value we have paid to measure
(`report --corr`), the distribution was min 0.778, median 0.844, max 0.895 --
NOT ONE under the limit, not one close to it. That is not bad luck on a few
candidates, it is the shape being crowded, and no amount of Sharpe/turnover
tuning moves it.

So `needs_field2` templates pair TWO fields. Four of the five are minimal
generalisations of shapes already above (`returns` -> a chosen data field; one
field -> two), and every one reuses an existing family so that priors.py, which
scores families per dataset, gives them a prior with no extra LLM call.

Placeholders: {f} field, {f2} second field, {g} group, {d}/{d2} lookback days.

FASTEXPR notes, learned by having 20 simulations rejected at once:
  * scientific notation is a PARSE ERROR -- "Unexpected character 'e'".
    Write decimals out in full, or arrange not to need them;
  * division needs no epsilon guard. A zero denominator yields NaN and
    `nanHandling: ON` absorbs it -- which is why working alphas on the
    platform are written `rank(sales / cap)` with no guard at all;
  * prefer native operators to hand-rolled equivalents: `ts_zscore(x, d)`
    IS (x - ts_mean(x,d)) / ts_std_dev(x,d), and unlike the hand-rolled
    version it parses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

GROUPS = ("subindustry", "industry", "sector", "market")


@dataclass(frozen=True)
class Template:
    name: str
    family: str
    expr: str
    windows: tuple[int, ...] = (22, 66, 126, 252)
    needs_group: bool = False
    # Rough prior on turnover so we can pre-pick a sane decay before spending
    # a slot. "low" ~ quarterly data, "high" ~ daily price/volume.
    turnover_prior: str = "mid"
    hypothesis: str = ""
    # Does the expression name a SECOND data field? The generator has to draw a
    # partner before it can render one of these; see generator._partner_pool.
    needs_field2: bool = False

    def render(self, field: str, *, days: int = 126, group: str = "subindustry",
               days2: int | None = None, field2: str = "") -> str:
        return (self.expr
                .replace("{f2}", field2)
                .replace("{f}", field)
                .replace("{g}", group)
                .replace("{d2}", str(days2 or max(5, days // 5)))
                .replace("{d}", str(days)))


LIBRARY: tuple[Template, ...] = (
    # ---------------------------------------------------------------- LEVEL
    Template("level_rank", "LEVEL",
             "group_rank({f}, {g})", (0,), True, "low",
             "Cross-sectionally cheap/high-quality names outperform peers."),
    Template("level_tsrank", "LEVEL",
             "group_rank(ts_rank({f}, {d}), {g})", (126, 252), True, "low",
             "A name high in its OWN history, ranked against peers."),

    # --------------------------------------------------------------- CHANGE
    Template("change", "CHANGE",
             "group_rank(ts_delta({f}, {d}), {g})", (22, 66, 126), True, "mid",
             "Improvement is priced more slowly than level."),
    Template("change_pct", "CHANGE",
             "group_rank(divide(ts_delta({f}, {d}), abs(ts_mean({f}, {d}))), {g})",
             (22, 66, 126), True, "mid",
             "Scale-free improvement; comparable across large and small names."),

    # ------------------------------------------------------------- SURPRISE
    Template("surprise_z", "SURPRISE",
             "group_rank(ts_zscore({f}, {d}), {g})",
             (66, 126, 252), True, "mid",
             "Deviation from the stock's own norm -- an idiosyncratic shock."),
    Template("surprise_vs_short", "SURPRISE",
             "group_rank(ts_mean({f}, {d2}) - ts_mean({f}, {d}), {g})",
             (126, 252), True, "mid",
             "Fast average crossing slow average: regime shift in the data."),

    # --------------------------------------------------------- ACCELERATION
    Template("accel", "ACCELERATION",
             "group_rank(ts_delta({f}, {d2}) - ts_delta(ts_delay({f}, {d2}), {d2}), {g})",
             (66, 126), True, "mid",
             "Second derivative: the rate of improvement is itself improving."),

    # ------------------------------------------------------------ STABILITY
    Template("stability", "STABILITY",
             "group_rank(divide(ts_mean({f}, {d}), ts_std_dev({f}, {d})), {g})",
             (126, 252), True, "low",
             "Reliable signals deserve larger positions than noisy ones."),

    # ------------------------------------------------------------- RELATIVE
    Template("relative", "RELATIVE",
             "group_neutralize(rank({f}), {g})", (0,), True, "low",
             "Strip the sector bet; keep only the within-peer view."),
    Template("relative_change", "RELATIVE",
             "group_neutralize(rank(ts_delta({f}, {d})), {g})", (22, 66), True, "mid",
             "Idiosyncratic improvement, sector effects removed."),

    # ---------------------------------------------------------------- YIELD
    Template("yield_cap", "YIELD",
             "group_rank(divide({f}, cap), {g})", (0,), True, "low",
             "Classic yield form: fundamental flow per dollar of market value."),
    Template("yield_assets", "YIELD",
             "group_rank(divide({f}, assets), {g})", (0,), True, "low",
             "Efficiency form: output per unit of balance sheet."),

    # ------------------------------------------------------------- RESIDUAL
    Template("resid_vs_returns", "RESIDUAL",
             "group_rank(-ts_regression(returns, {f}, {d}, rettype=0), {g})",
             (126, 252), True, "mid",
             "The part of the field the market has NOT already priced in."),

    # ---------------------------------------------------------- CONSISTENCY
    Template("consistency", "CONSISTENCY",
             "group_rank(ts_corr({f}, returns, {d}), {g})", (126, 252), True, "mid",
             "Names whose returns have been tracking this field keep doing so."),

    # ---------------------------------------------------------------- EVENT
    Template("event_gated", "EVENT",
             "trade_when(ts_delta({f}, 1) != 0, group_rank({f}, {g}), -1)",
             (0,), True, "low",
             "Only re-trade when the data actually updates -- kills turnover."),
    Template("event_volume", "EVENT",
             "trade_when(volume > adv20, group_rank(ts_rank({f}, {d}), {g}), -1)",
             (66, 126), True, "mid",
             "Take the position only when there is liquidity to take it in."),

    # ------------------------------------------------ TWO-FIELD (see docstring)
    # Families are reused deliberately: priors.py scores FAMILIES per dataset,
    # so an existing family means these arms inherit a real prior on day one
    # instead of falling back to the pessimistic (1, 6) that would starve them.
    Template("ratio_fields", "YIELD",
             "group_rank(divide({f}, {f2}), {g})", (0,), True, "low",
             "Field A per unit of field B -- an intensity neither states alone.",
             needs_field2=True),
    Template("resid_vs_field", "RESIDUAL",
             "group_rank(ts_regression({f}, {f2}, {d}, rettype=0), {g})",
             (126, 252), True, "mid",
             "The part of A that B does not explain. Everyone regresses against "
             "returns; almost nobody against a second dataset.",
             needs_field2=True),
    Template("gated_by_field", "EVENT",
             "trade_when(ts_zscore({f2}, {d}) > 1, group_rank({f}, {g}), -1)",
             (66, 126), True, "low",
             "A is only informative in the regime where B is unusually elevated.",
             needs_field2=True),
    Template("divergence", "SURPRISE",
             "group_rank(ts_rank({f}, {d}) - ts_rank({f2}, {d}), {g})",
             (126, 252), True, "mid",
             "A is high in its own history while B is not -- the two sources "
             "disagree, and disagreement is where the mispricing lives.",
             needs_field2=True),
    Template("confirmation", "CONSISTENCY",
             "group_rank(ts_corr({f}, {f2}, {d}), {g})", (126, 252), True, "mid",
             "A and B have been moving together -- mutual confirmation from "
             "two independent sources.",
             needs_field2=True),
)

BY_FAMILY: dict[str, list[Template]] = {}
for _t in LIBRARY:
    BY_FAMILY.setdefault(_t.family, []).append(_t)

BY_NAME: dict[str, Template] = {t.name: t for t in LIBRARY}


# --------------------------------------------------------------------------
# Robustness wrappers -- applied in the refinement ladder, not the scan.
# Each is a cheap, well-understood edit that targets one specific IS failure.
# --------------------------------------------------------------------------
def wrap_backfill(expr: str, field: str, days: int = 120) -> str:
    """Fixes sparse fields (CONCENTRATED_WEIGHT, low coverage, NaN drag).

    Wraps EVERY standalone occurrence of `field`, matched on identifier
    boundaries. The old `expr.replace(field, ..., 1)` was wrong twice over:

      * str.replace matches substrings, not identifiers. A field id that is a
        prefix of another token in the same expression (`fnd6_x` inside
        `fnd6_x_ttm`) had the replacement land INSIDE the neighbour --
        `ts_backfill(fnd6_x, 120)_ttm`. lint() cannot catch that: parens
        balance, no placeholder, no scientific notation. So the slot gets paid
        for a parse error, which is exactly what lint() exists to prevent.
        Two fields drawn from two different datasets make a collision far
        likelier than one field ever did.
      * count=1 backfilled only the FIRST occurrence, so a template naming its
        field twice (change_pct, stability) left the second one raw.

    Idempotent per field: if this field already sits inside a ts_backfill(...)
    the expression is returned untouched. That per-field guard is what lets a
    caller fold over two fields without the first wrap blocking the second --
    which a whole-expression `"ts_backfill" not in expr` test does not.
    """
    if not field:
        return expr
    esc = re.escape(field)
    if re.search(rf"ts_backfill\(\s*{esc}\s*,", expr):
        return expr
    # A callable replacement, not a string: re.sub would otherwise interpret
    # backslash escapes in the field name.
    return re.sub(rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])",
                  lambda _m: f"ts_backfill({field}, {days})", expr)


def wrap_winsorize(expr: str, std: float = 4.0) -> str:
    """Fixes fat tails -> CONCENTRATED_WEIGHT and drawdown spikes."""
    return f"winsorize({expr}, std={std})"


def wrap_zscore(expr: str) -> str:
    return f"zscore({expr})"


def wrap_hump(expr: str, threshold: float = 0.01) -> str:
    """Turnover killer that, unlike decay, does not lag the signal: it simply
    refuses to trade unless the target weight has moved materially."""
    return f"hump({expr}, hump={threshold})"


def wrap_trade_when(expr: str, condition: str = "volume > adv20") -> str:
    return f"trade_when({condition}, {expr}, -1)"


def wrap_group_neutralize(expr: str, group: str = "subindustry") -> str:
    return f"group_neutralize({expr}, {group})"


def negate(expr: str) -> str:
    """Sign is not a hypothesis -- a Sharpe of -1.6 is a 1.6 alpha you have
    not written down yet. Always test the flip before discarding."""
    return f"-({expr})"


# --------------------------------------------------------------------------
# Local lint. A syntax error should cost zero simulation slots: BRAIN charges
# the slot before it parses, so a malformed expression is billed exactly like
# a real backtest. One bad template once cost 20 slots in a single batch.
# --------------------------------------------------------------------------
_SCI_NOTATION = re.compile(r"\d+\.?\d*[eE][-+]?\d+")


def lint(expr: str) -> list[str]:
    """Return reasons this expression cannot be valid FASTEXPR."""
    problems = []
    if _SCI_NOTATION.search(expr):
        problems.append("scientific notation is a FASTEXPR parse error "
                        "(write the decimal out in full)")
    if expr.count("(") != expr.count(")"):
        problems.append(f"unbalanced parentheses "
                        f"({expr.count('(')} open, {expr.count(')')} close)")
    if "{" in expr or "}" in expr:
        problems.append("unrendered placeholder left in expression")
    if not expr.strip():
        problems.append("empty expression")
    return problems


def iter_variants(field: str, templates=LIBRARY, groups=("subindustry", "industry"),
                  field2: str = "") -> Iterator[tuple[Template, str, int, str]]:
    """Yield (template, expr, days, group) across the transform grid.

    Two-field templates are skipped unless a partner is supplied -- rendering
    one without a `field2` would leave `{f2}` in the expression for lint() to
    reject downstream.
    """
    for t in templates:
        if t.needs_field2 and not field2:
            continue
        for g in (groups if t.needs_group else (groups[0],)):
            for d in (t.windows or (0,)):
                yield t, t.render(field, days=d, group=g, field2=field2), d, g
