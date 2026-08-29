"""Candidate generation: bandit-allocated, transform-first, dedup-aware."""
from __future__ import annotations

import logging
import random
from typing import Iterable

from . import templates as T
from . import priors as P
from .bandit import Bandit, arm_name
from .catalog import Catalog
from .config import BACKFILL_DAYS, SPARSE_COVERAGE, SimSettings, TYPE_DEFAULTS

log = logging.getLogger("alphamachine.generator")


def default_settings_for(theme: str, base: SimSettings) -> SimSettings:
    """Give each data type a sane starting decay/neutralization so the coarse
    scan is not systematically unfair to slow-moving data."""
    d = TYPE_DEFAULTS.get(theme, {})
    return base.replace(**d) if d else base


def _partner_pool(fields: list[dict], theme: str
                  ) -> tuple[list[dict], list[float]]:
    """Candidate {f2} fields for a primary field of `theme`, plus draw weights.

    Two choices, both aimed squarely at PROD_CORRELATION -- the binding
    constraint, and by a wide margin: of the 16 one-field ideas whose value we
    have measured, NONE came in under the 0.70 limit (min 0.778, median 0.844).

    * CROSS-THEME. That check reports the MAXIMUM correlation against any one
      alpha already live in production, so it fires when someone else has
      written the same idea. Every rejection so far pairs a conventional
      transform with a conventional data source; a pairing that spans two
      themes is structurally unlikely to already sit in that pool.
    * WEIGHTED BY Catalog.field_value, which already carries
      1/(1+log10(1+alphaCount)) -- and alphaCount is the platform's OWN count
      of how many alphas use that field, i.e. a direct crowding measure.
      Weighting by it prefers under-mined partners without banning anything,
      the same principle the bandit runs on: shift the odds, never blacklist.

    Computed once per theme by the caller, not once per candidate.
    """
    pool = [x for x in fields if x.get("_theme") != theme and x.get("id")]
    return pool, [max(Catalog.field_value(x), 1e-6) for x in pool]


def generate(store, fields: list[dict], *, budget: int,
             base: SimSettings, seed: int = 0,
             templates: Iterable[T.Template] = T.LIBRARY) -> list[str]:
    """Produce up to `budget` fresh scan candidates and persist them.

    Returns the candidate ids actually queued (already-simulated combinations
    are silently skipped, so the caller may get fewer than `budget`).
    """
    rng = random.Random(seed or None)
    tmpls = list(templates)
    by_theme = Catalog.by_theme(fields)
    exhausted = set(store.get("exhausted_fields", []))

    arms = [arm_name(theme, t.name) for theme in by_theme for t in tmpls]
    pri = P.arm_priors(fields)
    if pri:
        log.info("using LLM priors for %d/%d arms", len(pri), len(arms))
    bandit = Bandit(store, arms, seed=seed, priors=pri)
    alloc = bandit.allocate(budget)

    queued: list[str] = []
    partners: dict[str, tuple[list[dict], list[float]]] = {}
    for arm, n in sorted(alloc.items(), key=lambda kv: -kv[1]):
        if n <= 0:
            continue
        theme, tname = arm.split("::", 1)
        tmpl = T.BY_NAME.get(tname)
        pool = by_theme.get(theme) or []
        if not tmpl or not pool:
            continue
        settings = default_settings_for(theme, base)
        # Under-mined fields first; already-used fields last but not banned.
        pool = sorted(pool, key=lambda f: (f.get("id") in exhausted,
                                           -Catalog.field_value(f)))
        head = pool[:max(n * 6, 30)]
        rng.shuffle(head)

        made = 0
        for f in head:
            if made >= n:
                break
            fid = f.get("id")
            if not fid:
                continue
            days = rng.choice(tmpl.windows or (0,))
            group = rng.choice(("subindustry", "industry"))
            f2, f2id = None, ""
            if tmpl.needs_field2:
                if theme not in partners:
                    partners[theme] = _partner_pool(fields, theme)
                pool2, w2 = partners[theme]
                if not pool2:
                    continue          # nothing cross-theme to pair with
                f2 = rng.choices(pool2, weights=w2, k=1)[0]
                f2id = f2["id"]
                if f2id == fid:
                    continue
            expr = tmpl.render(fid, days=days, group=group, field2=f2id)
            # Backfill sparse fields AT SCAN TIME, not as a refinement option.
            # The first real run made the case: analyst_count_fy1_forecast
            # scanned at 0.94 and only reached 1.73 once backfilled, because
            # the gaps -- not the hypothesis -- were what looked like noise.
            # Discovering that in refinement means every sparse field must
            # first survive a scan that systematically understates it.
            # Each field is judged on its OWN coverage: pairing a dense field
            # with a sparse one must not leave the sparse half raw.
            for fd, i in ((f, fid), (f2, f2id)):
                if not i:
                    continue
                cov = fd.get("coverage")
                if cov is not None and float(cov) < SPARSE_COVERAGE:
                    expr = T.wrap_backfill(expr, i, BACKFILL_DAYS)
            problems = T.lint(expr)
            if problems:
                # Never spend a slot to discover a syntax error.
                log.warning("skipping %s on %s: %s", tmpl.name, fid, problems[0])
                continue
            key = settings.key()
            if store.already_simulated(expr, key):
                continue
            cid = store.add_candidate(
                expr=expr, settings=settings.payload(), theme=theme,
                template=tmpl.name, field=fid, field2=f2id, stage="scan",
                settings_key=key)
            queued.append(cid)
            made += 1
    log.info("queued %d scan candidates across %d arms",
             len(queued), sum(1 for v in alloc.values() if v))
    return queued
