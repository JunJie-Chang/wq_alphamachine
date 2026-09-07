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
THEME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("analyst",       r"\b(est|analyst|rec|target|eps_est|consensus|rev(ision)?)\b|^anl|^est"),
    ("news",          r"\bnews\b|^nws"),
    ("sentiment",     r"sentiment|social|buzz|twitter|reddit|scl"),
    ("option",        r"option|implied|iv_|\bskew\b|^opt"),
    ("shortinterest", r"short_interest|shortint|\bsi_\b|^si"),
    ("institutions",  r"institution|holder|ownership|13f|fund_flow"),
    ("insiders",      r"insider"),
    ("macro",         r"macro|gdp|cpi|rate|fx|commodity|oil_"),
    ("risk",          r"risk|beta|factor_|barra|specrisk"),
    ("model",         r"model|score|prediction|ml_"),
    ("pv",            r"^(open|close|high|low|volume|vwap|returns|adv|cap)$|price|volume|^pv"),
    ("fundamental",   r"income|revenue|asset|equity|debt|cash|margin|earning|sales|"
                      r"capex|inventory|liabilit|dividend|book|ebit|fcf|expense|tax"),
)

# Size denominators used by the YIELD templates -- these must exist in-region.
DENOMINATORS = ("cap", "assets", "revenue", "equity", "sales")


def infer_theme(field: dict) -> str:
    hay = " ".join(str(x).lower() for x in (
        field.get("id", ""), field.get("description", ""),
        (field.get("dataset") or {}).get("name", ""),
        (field.get("category") or {}).get("name", ""),
        (field.get("subcategory") or {}).get("name", ""),
    ))
    for theme, pat in THEME_PATTERNS:
        if re.search(pat, hay):
            return theme
    return "other"


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
                return json.load(fh)
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
                f.setdefault("dataset", {"id": dsid, "name": ds.get("name")})
                f["_theme"] = infer_theme(f)
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
        return out

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
