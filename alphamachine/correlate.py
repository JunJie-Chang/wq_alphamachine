"""Correlation and diversity selection.

Two hard-won rules encoded here:

1. Correlate DAILY RETURNS, never cumulative PnL. Every cumulative equity
   curve trends upward, so two independent alphas will show 0.9+ correlation
   on cumulative series. This single mistake is the most common reason a
   locally-vetted basket gets rejected at SELF_CORRELATION.

2. Overlap the date ranges before correlating. Alphas simulated over different
   windows produce different-length vectors, and naive truncation silently
   compares 2015 against 2019.

Selection is greedy max-marginal-value rather than "top N by Sharpe". BRAIN
rewards baskets of strong AND uncorrelated alphas, so the fourth-best alpha
that is orthogonal to your book is worth more than the second-best alpha that
is a twin of something you already submitted.
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

import numpy as np

log = logging.getLogger("alphamachine.correlate")


def daily_returns(dates: Sequence[str], cum_pnl: Sequence[float]
                  ) -> tuple[list[str], np.ndarray]:
    """Convert a cumulative PnL curve to daily PnL changes."""
    v = np.asarray(cum_pnl, dtype=float)
    if v.size < 2:
        return list(dates), np.zeros(0)
    return list(dates[1:]), np.diff(v)


# BRAIN measures alpha-vs-alpha correlation (SELF_CORRELATION, and the Power Pool
# correlation) on the LAST FOUR YEARS of daily PnL changes, not the whole
# history. Established 2026-09-20 by reproducing BRAIN's own reported numbers to
# four decimals -- 0.3434 / 0.3365 / 0.1637 / 0.7162 -- from PnL series fetched
# for the same alphas; the full 10-year window read 0.03-0.05 LOW on every one of
# them, which is the unsafe direction for a "is this too correlated" gate.
BRAIN_CORR_YEARS = 4


def aligned_corr(a: tuple[list[str], np.ndarray],
                 b: tuple[list[str], np.ndarray], *,
                 years: int | None = None) -> float:
    """Pearson correlation of two daily-return series on their shared dates.

    `years` keeps only the last N calendar years of the overlap (a window that
    starts on Jan 1 of `last_year - N + 1`); pass BRAIN_CORR_YEARS to match what
    the platform reports. Default None = the whole overlap, as before.
    """
    da, va = a
    db, vb = b
    if va.size == 0 or vb.size == 0:
        return 0.0
    idx = {d: i for i, d in enumerate(da)}
    common = [(idx[d], j) for j, d in enumerate(db) if d in idx]
    if years is not None and common:
        cutoff = f"{int(max(db[j] for _, j in common)[:4]) - years + 1}-01-01"
        common = [(i, j) for i, j in common if db[j] >= cutoff]
    if len(common) < 60:                       # < ~3 months of overlap
        return 0.0
    ia = np.array([i for i, _ in common])
    ib = np.array([j for _, j in common])
    x, y = va[ia], vb[ib]
    if x.std() < 1e-12 or y.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


class CorrelationBook:
    """Holds the daily-return series of alphas you already own."""

    def __init__(self):
        self.series: dict[str, tuple[list[str], np.ndarray]] = {}

    def add(self, alpha_id: str, dates: Sequence[str], cum_pnl: Sequence[float]) -> None:
        self.series[alpha_id] = daily_returns(dates, cum_pnl)

    def load_from_store(self, store, alpha_ids: Iterable[str] | None = None) -> int:
        data = store.all_pnl()
        for aid, (d, v) in data.items():
            if alpha_ids is None or aid in set(alpha_ids):
                self.add(aid, d, v)
        return len(self.series)

    def max_corr(self, dates: Sequence[str], cum_pnl: Sequence[float]
                 ) -> tuple[float, str | None]:
        cand = daily_returns(dates, cum_pnl)
        worst, who = 0.0, None
        for aid, s in self.series.items():
            c = abs(aligned_corr(cand, s))
            if c > worst:
                worst, who = c, aid
        return worst, who


def fetch_book(client, store, *, status: str = "ACTIVE", limit: int = 200
               ) -> CorrelationBook:
    """Pull your existing ACTIVE alphas' PnL once and cache it locally."""
    book = CorrelationBook()
    from .metrics import pnl_series
    for a in client.my_alphas(status=status, limit=limit):
        aid = a.get("id")
        if not aid:
            continue
        cached = store.load_pnl(aid)
        if cached is None:
            try:
                d, v = pnl_series(client.alpha_pnl(aid))
            except Exception as e:
                log.warning("pnl fetch failed for %s: %s", aid, e)
                continue
            if not d:
                continue
            store.save_pnl(aid, d, v)
        else:
            d, v = cached
        book.add(aid, d, v)
    log.info("correlation book: %d existing alphas", len(book.series))
    return book


def greedy_select(candidates: list[dict], book: CorrelationBook,
                  *, max_corr: float = 0.55, n: int = 10) -> list[dict]:
    """Greedy max-diversity selection.

    `candidates` must each carry: alpha_id, quality (float), dates, pnl.
    Selection value = quality * (1 - max_corr_against_everything_kept_so_far),
    so a merely-good orthogonal alpha outranks a great redundant one.
    """
    pool = sorted(candidates, key=lambda c: -c.get("quality", 0.0))
    kept: list[dict] = []
    working = CorrelationBook()
    working.series = dict(book.series)

    while pool and len(kept) < n:
        best, best_val, best_c = None, -1e9, 0.0
        for c in pool:
            corr, _who = working.max_corr(c["dates"], c["pnl"])
            if corr >= max_corr:
                continue
            val = c.get("quality", 0.0) * (1.0 - corr)
            if val > best_val:
                best, best_val, best_c = c, val, corr
        if best is None:
            break
        best["max_corr"] = best_c
        best["marginal_value"] = best_val
        kept.append(best)
        working.add(best["alpha_id"], best["dates"], best["pnl"])
        pool = [c for c in pool if c is not best]
    return kept


def corr_matrix(series: dict[str, tuple[list[str], np.ndarray]]) -> tuple[list[str], np.ndarray]:
    ids = sorted(series)
    m = np.eye(len(ids))
    for i, a in enumerate(ids):
        for j in range(i + 1, len(ids)):
            c = aligned_corr(series[a], series[ids[j]])
            m[i, j] = m[j, i] = c
    return ids, m
