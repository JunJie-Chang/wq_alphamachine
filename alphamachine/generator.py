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
            expr = tmpl.render(fid, days=days, group=group)
            # Backfill sparse fields AT SCAN TIME, not as a refinement option.
            # The first real run made the case: analyst_count_fy1_forecast
            # scanned at 0.94 and only reached 1.73 once backfilled, because
            # the gaps -- not the hypothesis -- were what looked like noise.
            # Discovering that in refinement means every sparse field must
            # first survive a scan that systematically understates it.
            cov = f.get("coverage")
            if cov is not None and float(cov) < SPARSE_COVERAGE:
                expr = T.wrap_backfill(expr, fid, BACKFILL_DAYS)
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
                template=tmpl.name, field=fid, stage="scan", settings_key=key)
            queued.append(cid)
            made += 1
    log.info("queued %d scan candidates across %d arms",
             len(queued), sum(1 for v in alloc.values() if v))
    return queued
