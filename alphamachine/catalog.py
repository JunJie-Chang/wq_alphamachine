"""Data-field discovery, theme mapping, and field-exhaustion tracking.

BRAIN exposes tens of thousands of fields. Iterating them uniformly is the
classic beginner mistake: 80% of the field space is near-duplicate accounting
lines that all express the same latent factor, and mining them produces a book
that fails SELF_CORRELATION on submission after you have already burnt the slots.

This module does three things instead:
  1. groups fields by DATASET, because datasets are the real diversity unit;
  2. tags each field with a THEME (the economic channel it proxies);
  3. remembers which fields already back an ACTIVE alpha, so the machine keeps
     walking into unexplored territory rather than re-mining its own book.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, Iterable

log = logging.getLogger("alphamachine.catalog")

# Theme inference from dataset category / field id. Order matters: first hit wins.
#
# Two rules learned the hard way, both visible in `macro` below:
#   * anchor every token that is also a common English substring. Bare `rate`
#     matched inside corporate / accurate / generated / separate / strategy,
#     and `rate` on its own is not a macro concept anyway -- `interest rate` is.
#   * order broad patterns AFTER narrow ones. `macro` used to sit above
#     risk/model/pv/fundamental, so each of its false positives was also a
#     field stolen from one of those four.
# Measured on the 293-dataset USA/TOP3000 cache before the fix: of 31 Price
# Volume datasets 14 were read as macro, and of 22 Fundamental datasets only 2
# reached `fundamental` -- the rest split between macro and `other`, because
# this pattern list did not contain the word "fundamental" itself.
THEME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("analyst",       r"\b(est|analyst|rec|target|eps_est|consensus|rev(ision)?)\b|^anl|^est"),
    ("news",          r"\bnews\b|^nws"),
    ("sentiment",     r"sentiment|social|buzz|twitter|reddit|scl"),
    ("option",        r"option|implied|iv_|\bskew\b|^opt"),
    ("shortinterest", r"short_interest|shortint|\bsi_\b|^si"),
    ("institutions",  r"institution|holder|ownership|13f|fund_flow"),
    ("insiders",      r"insider"),
    ("imbalance",     r"imbalance"),
    ("risk",          r"risk|beta|factor_|barra|specrisk"),
    ("model",         r"model|score|prediction|ml_"),
    ("pv",            r"^(open|close|high|low|volume|vwap|returns|adv|cap)$|price|volume|^pv"),
    ("fundamental",   r"\bfundamental|income|revenue|asset|equity|debt|cash|margin|"
                      r"earning|sales|capex|inventory|liabilit|dividend|book|ebit|"
                      r"fcf|expense|tax"),
    # `\b` is the wrong boundary for field ids: `_` is a word character, so
    # `\bgdp\b` does not match `gdp_yoy`. These anchor on LETTERS instead, which
    # treats the underscore as the separator it actually is.
    ("macro",         r"(?<![a-z])(?:gdp|cpi|fx)(?![a-z])"
                      r"|(?<![a-z])(?:macro|commodit|treasur|inflation)"
                      r"|interest rate|foreign exchange|oil price|yield curve"),
)

_THEME_RE: tuple[tuple[str, Any], ...] = tuple(
    (theme, re.compile(pat)) for theme, pat in THEME_PATTERNS)

# Size denominators used by the YIELD templates -- these must exist in-region.
# GLB, GBR, DEU, IND and CHN/d0 carry cap but none of the other four (checked
# against each catalog 2026-09-26); generate() skips a template that names a
# field its scope does not have.
DENOMINATORS = ("cap", "assets", "revenue", "equity", "sales")

_DEAD_FIELD_RE = re.compile(r'unknown variable "([A-Za-z0-9_]+)"'
                            r"|Invalid data field ([A-Za-z0-9_]+)")


def dead_field_in(error: str) -> str | None:
    """The field BRAIN says does not exist, from its error message, if any."""
    m = _DEAD_FIELD_RE.search(error or "")
    return (m.group(1) or m.group(2)) if m else None


def identifiers(expr: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr or ""))


def infer_theme(field: dict) -> str:
    hay = " ".join(str(x).lower() for x in (
        field.get("id", ""), field.get("description", ""),
        (field.get("dataset") or {}).get("name", ""),
        (field.get("category") or {}).get("name", ""),
        (field.get("subcategory") or {}).get("name", ""),
    ))
    for theme, rx in _THEME_RE:
        if rx.search(hay):
            return theme
    return "other"


def retheme(fields: list[dict]) -> list[dict]:
    """Stamp `_theme` on every record, overwriting whatever was there.

    `_theme` is DERIVED, never authoritative. It used to be written once in
    the fetch branch and then frozen into the per-dataset cache, which made a
    pattern fix inert: the top-level `fields_*.json` is skipped under
    `--refresh`, but the `parts_*/` files are resumed regardless of it, and the
    resume branch never re-stamped. Fixing a regex therefore changed nothing
    until ~4,000 requests at 1 req/s had been paid again. Re-deriving on every
    load costs one regex sweep and makes the cache self-healing.
    """
    for f in fields:
        f["_theme"] = infer_theme(f)
    return fields


class Catalog:
    def __init__(self, client, cache_dir: str = "data/cache"):
        self.c = client
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------- fetching
    def operators(self, *, refresh=False) -> list[dict]:
        """BRAIN's own operator catalogue, cached like fields.

        Never fetched once in this project's history -- client.operators()
        existed with no call site -- which is how templates.py came to use ~20
        of the 85 operators the platform offers, with the Logical and Vector
        categories untouched entirely. Cheap (one request, no quota), and the
        `level` field on each record is the account-tier gate, so this is also
        how to tell a typo from an operator this tier simply cannot use.
        """
        path = os.path.join(self.cache_dir, "operators.json")
        if os.path.exists(path) and not refresh:
            with open(path) as fh:
                return json.load(fh)
        ops = self.c.operators()
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(ops, fh)
        log.info("cached %d operators", len(ops))
        return ops

    def fields(self, *, region="USA", delay=1, universe="TOP3000",
               refresh=False) -> list[dict]:
        path = os.path.join(
            self.cache_dir, f"fields_{region}_{universe}_d{delay}.json")
        if os.path.exists(path) and not refresh:
            with open(path) as fh:
                return retheme(json.load(fh))
        datasets = self.c.data_sets(region=region, delay=delay, universe=universe)
        log.info("%d datasets in %s/%s d%d", len(datasets), region, universe, delay)
        # Per-dataset cache. A full USA/TOP3000 sweep is ~4,000 requests against
        # a 1-req/sec endpoint, i.e. over an hour; persisting only at the end
        # means any interruption throws all of it away. Each dataset is written
        # as it lands, so a restart resumes instead of starting over.
        part_dir = os.path.join(self.cache_dir,
                                f"parts_{region}_{universe}_d{delay}")
        os.makedirs(part_dir, exist_ok=True)
        out: list[dict] = []
        resumed = 0
        for i, ds in enumerate(datasets, 1):
            dsid = ds.get("id") or f"unknown{i}"
            part = os.path.join(part_dir, f"{dsid}.json")
            if os.path.exists(part):
                with open(part) as fh:
                    got = json.load(fh)
                out.extend(got)
                resumed += 1
                continue
            got = self.c.data_fields(region=region, delay=delay, universe=universe,
                                     dataset_id=dsid)
            for f in got:
                # The dataset name is real enrichment -- the field payload does
                # not carry it and infer_theme reads it, so it must be
                # persisted. `_theme` itself is not: see retheme().
                f.setdefault("dataset", {"id": dsid, "name": ds.get("name")})
            with open(part, "w") as fh:
                json.dump(got, fh)
            out.extend(got)
            log.info("  [%3d/%3d] %-28s %5d fields", i, len(datasets), dsid, len(got))
        if resumed:
            log.info("resumed %d dataset(s) from cache", resumed)
        if not out:
            # Never cache an empty catalog. A transient API failure would
            # otherwise be frozen to disk and reused forever, and every later
            # run would quietly generate zero candidates while looking healthy.
            log.warning("no fields returned for %s/%s d%d -- not caching; "
                        "check permissions for this region/universe",
                        region, universe, delay)
            return out
        with open(path, "w") as fh:
            json.dump(out, fh)
        return retheme(out)

    # ------------------------------------------------------------ selection
    @staticmethod
    def usable(fields: Iterable[dict], *, min_coverage: float = 0.60,
               matrix_only: bool = True) -> list[dict]:
        """Drop fields that cannot support a robust cross-sectional signal.

        Low coverage is the leading cause of CONCENTRATED_WEIGHT: if a field
        exists for 200 of 3000 names, `rank()` hands those 200 the whole book.
        """
        out = []
        for f in fields:
            cov = f.get("coverage")
            if cov is not None and float(cov) < min_coverage:
                continue
            if matrix_only and f.get("type") not in (None, "MATRIX"):
                continue
            if int(f.get("alphaCount") or 0) > 200000:
                # Heavily mined field: high prior probability of SELF_CORRELATION.
                continue
            out.append(f)
        return out

    @staticmethod
    def by_theme(fields: Iterable[dict]) -> dict[str, list[dict]]:
        d: dict[str, list[dict]] = defaultdict(list)
        for f in fields:
            d[f.get("_theme") or infer_theme(f)].append(f)
        return dict(d)

    @staticmethod
    def by_dataset(fields: Iterable[dict]) -> dict[str, list[dict]]:
        d: dict[str, list[dict]] = defaultdict(list)
        for f in fields:
            d[(f.get("dataset") or {}).get("id", "?")].append(f)
        return dict(d)

    # ------------------------------------------------------- field scoring
    # BRAIN publishes `pyramidMultiplier` per field: a direct, official
    # statement of how much extra credit an alpha built on that field earns.
    # It is not a proxy or an inference -- it is the platform telling you where
    # it wants coverage. Combined with `alphaCount` (how mined the field
    # already is) it gives a marginal-value estimate before spending any slot.
    @staticmethod
    def field_value(f: dict) -> float:
        mult = float(f.get("pyramidMultiplier") or 1.0)
        mined = int(f.get("alphaCount") or 0)
        cov = float(f.get("coverage") or 0.0)
        # Crowding hurts on a log scale: the step from 0 to 100 existing alphas
        # matters far more than 100,000 to 200,000 -- by then it is saturated
        # either way and PROD_CORRELATION is the binding constraint.
        import math
        crowd = 1.0 / (1.0 + math.log10(1.0 + mined))
        return mult * crowd * min(cov / 0.6, 1.0)

    @staticmethod
    def rank_by_value(fields, top: int | None = None) -> list[dict]:
        out = sorted(fields, key=Catalog.field_value, reverse=True)
        return out[:top] if top else out

    @staticmethod
    def stratified_sample(fields: Iterable[dict], per_dataset: int = 8,
                          rng=None) -> list[dict]:
        """Take a few fields from EVERY dataset rather than many from one.

        Breadth across datasets is what buys decorrelation; depth within one
        dataset buys you a cluster of alphas that will cannibalise each other
        at the SELF_CORRELATION gate.
        """
        import random
        rng = rng or random.Random(7)
        out = []
        for ds, fs in Catalog.by_dataset(fields).items():
            # Rank by marginal value (pyramid multiplier vs how mined it is),
            # not by raw crowding: a heavily-mined field at 1.4x can still beat
            # an untouched one at 1.0x.
            fs = sorted(fs, key=Catalog.field_value, reverse=True)
            head = fs[:per_dataset * 3]
            rng.shuffle(head)
            out.extend(head[:per_dataset])
        return out

    # ----------------------------------------------------------- exhaustion
    def exhausted_fields(self, store) -> set[str]:
        """Fields already backing an ACTIVE alpha -- deprioritise, don't ban."""
        used = store.get("exhausted_fields", [])
        return set(used)

    def mark_exhausted(self, store, field_id: str) -> None:
        cur = set(store.get("exhausted_fields", []))
        cur.add(field_id)
        store.put("exhausted_fields", sorted(cur))
