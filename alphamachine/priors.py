"""LLM-generated priors over (theme x transform) arms.

Why this exists
---------------
The catalogue carries a `description` for every one of its 85,679 fields, and
the machine currently reduces that to a regex theme tag -- reading a manual as
if it were a barcode. Meanwhile the bandit learns which transform suits which
data by burning simulations, one arm at a time.

Some of what it learns that way is knowable by reading. `techindi_model` is
"Deep Learning Technical Indicator"; `ml_factor_proj` is "Machine Learning
Factor Projection". Those fields are *another model's output* -- closer to a
prediction than to raw data. Ranking such a score (the LEVEL family) is what
everyone who touches the dataset does first, so it is the most crowded thing
you can do with it. The value is in asking when the model CHANGES ITS MIND
(SURPRISE, CHANGE, ACCELERATION). That judgement costs one read and hundreds
of simulations to discover empirically.

So: an LLM reads the field descriptions once, per dataset, and emits a prior.
The bandit still decides -- the prior only shifts where it starts looking.

Deliberate limits
-----------------
* This NEVER touches submission. Irreversible decisions stay on hard numbers.
* Priors are pseudo-counts, capped, so evidence overwhelms them quickly.
* Absent credentials the whole module no-ops and the bandit runs uninformed.
* Results are cached to disk; the classification runs once per catalogue.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

from . import templates as T

log = logging.getLogger("alphamachine.priors")

# Sonnet by default: this is a bulk read-and-judge pass over a few hundred
# dataset descriptions, and it runs once. Override per run with --model.
MODEL = "claude-sonnet-5"
CACHE = "data/cache/arm_priors.json"

# How much a prior is allowed to be worth, in units of "simulations already
# run". Small on purpose: a plausible-sounding story is worth a couple of
# trials, not a hundred. Real conversions must be able to overrule it fast.
PRIOR_STRENGTH = 6.0

_FAMILIES = sorted({t.family for t in T.LIBRARY})

SYSTEM = f"""You advise a quantitative alpha-research pipeline that mines the \
WorldQuant BRAIN platform. Your job is narrow: given the description of one \
dataset and a sample of its fields, judge which SIGNAL TRANSFORMS are likely \
to extract predictive, low-correlation alpha from that data.

The transform families are:

LEVEL         rank the raw value cross-sectionally ("who is high right now")
CHANGE        first difference over a window ("who is improving")
SURPRISE      deviation from the field's OWN recent history (self-referential z)
ACCELERATION  second difference ("the rate of improvement is itself changing")
STABILITY     value scaled by its own dispersion ("how reliable is this reading")
RELATIVE      value minus its peer-group value (pure idiosyncratic component)
YIELD         value normalised by a size denominator (per dollar of cap/assets)
RESIDUAL      the part of the field a benchmark regression does NOT explain
CONSISTENCY   trailing co-movement of the field with returns
EVENT         trade only when a condition fires (used to suppress turnover)

Judgement principles:

1. CROWDING. The obvious transform for a dataset is the one every researcher \
tries first, so its alpha is already arbitraged and it will fail correlation \
checks. Penalise the obvious reading.
2. PROCESSED SIGNALS. When fields are already a model's output (ML scores, \
NLP sentiment, factor projections, embeddings), the level has been mined. \
What is under-used is the CHANGE in that output -- when the model revises.
3. UPDATE FREQUENCY. Quarterly accounting data cannot support acceleration or \
short-window change; it has too few distinct observations. Daily or event-driven \
data can.
4. SPARSITY. Sparse or event-driven fields suit EVENT and SURPRISE; they make \
STABILITY and CONSISTENCY meaningless because the window is mostly empty.
5. DENOMINATORS. YIELD only makes sense for flow or stock quantities that scale \
with company size. It is meaningless for scores, ratios, probabilities and ranks.

Score each family for THIS dataset on a scale from 0.0 to 1.0 inclusive -- \
never below 0.0, never above 1.0. Most families should score low; a flat \
distribution is not a judgement. Be willing to give 0.0.
Keep `rationale` under 400 characters.
Reply with the requested JSON only."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scores", "rationale"],
    "properties": {
        # NOTE: structured-output schemas reject `minimum`/`maximum` on number
        # and `maxLength` on string ("properties maximum, minimum are not
        # supported"). Ranges are stated in the prompt and clamped on parse
        # instead of being declared here.
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "required": _FAMILIES,
            "properties": {f: {"type": "number"} for f in _FAMILIES},
        },
        "rationale": {"type": "string"},
    },
}


def _client():
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed -- priors disabled")
        return None
    try:
        return anthropic.Anthropic()
    except Exception as e:
        log.warning("no Anthropic credentials (%s) -- priors disabled", e)
        return None


def _dataset_prompt(ds_id: str, ds_name: str, fields: list[dict],
                    sample: int = 25) -> str:
    picked = fields[:sample]
    lines = [f"Dataset id: {ds_id}", f"Dataset name: {ds_name}",
             f"Field count: {len(fields)}", "", "Sample fields:"]
    for f in picked:
        desc = str(f.get("description") or "").strip()[:150]
        cov = f.get("coverage")
        lines.append(f"  {f.get('id')}  (coverage {cov})  {desc}")
    return "\n".join(lines)


def build(fields: Iterable[dict], *, limit: int | None = None,
          use_batch: bool = True, cache_path: str = CACHE,
          model: str = MODEL) -> dict:
    """Classify each dataset and cache {dataset_id: {family: score}}.

    Uses the Batch API (half price, and this is not latency-sensitive) with the
    taxonomy cached as a stable prompt prefix across every request.
    """
    from .catalog import Catalog
    by_ds = Catalog.by_dataset(list(fields))
    if limit:
        by_ds = dict(list(by_ds.items())[:limit])

    existing = load(cache_path)
    todo = {k: v for k, v in by_ds.items() if k not in existing}
    if not todo:
        log.info("priors already cached for all %d datasets", len(by_ds))
        return existing

    client = _client()
    if client is None:
        return existing

    # The taxonomy is identical across every request, so it is the cache prefix.
    system = [{"type": "text", "text": SYSTEM,
               "cache_control": {"type": "ephemeral"}}]
    common = {
        "model": model,
        # 1200 truncated ~17% of responses mid-JSON (stop_reason=max_tokens):
        # the model reasons before emitting, and that reasoning shares the
        # budget. The payload itself is ~250 tokens, so the headroom is for
        # thinking, not for output.
        "max_tokens": 8000,
        "system": system,
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }

    log.info("classifying %d datasets with %s (batch=%s)",
             len(todo), model, use_batch)
    out = dict(existing)

    if use_batch:
        from anthropic.types.messages.batch_create_params import Request
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        reqs = []
        for ds, fs in todo.items():
            name = (fs[0].get("dataset") or {}).get("name", ds)
            reqs.append(Request(
                custom_id=_safe_id(ds),
                params=MessageCreateParamsNonStreaming(
                    **common,
                    messages=[{"role": "user",
                               "content": _dataset_prompt(ds, name, fs)}]),
            ))
        batch = client.messages.batches.create(requests=reqs)
        log.info("batch %s submitted (%d requests)", batch.id, len(reqs))
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            log.info("  batch %s ...", b.processing_status)
            time.sleep(20)
        id_map = {_safe_id(ds): ds for ds in todo}
        failures: dict[str, int] = {}
        for res in client.messages.batches.results(batch.id):
            ds = id_map.get(res.custom_id)
            if ds is None:
                continue
            if res.result.type != "succeeded":
                # Never swallow this. A silent zero looks exactly like "no
                # credentials", and the actual reason (a rejected schema, a
                # rate limit) is only visible here.
                reason = _reason(res.result)
                failures[reason] = failures.get(reason, 0) + 1
                continue
            parsed = _parse(res.result.message)
            if parsed:
                out[ds] = parsed
            else:
                # Name the actual cause. "unparseable" alone sent me to the
                # batch API by hand to discover it was simple truncation.
                stop = getattr(res.result.message, "stop_reason", "?")
                why = ("response truncated (stop_reason=max_tokens) -- raise "
                       "max_tokens" if stop == "max_tokens"
                       else f"unparseable response (stop_reason={stop})")
                failures[why] = failures.get(why, 0) + 1
        for reason, n in sorted(failures.items(), key=lambda kv: -kv[1]):
            log.error("%d/%d requests failed: %s", n, len(reqs), reason)
    else:
        for ds, fs in todo.items():
            name = (fs[0].get("dataset") or {}).get("name", ds)
            try:
                msg = client.messages.create(
                    **common,
                    messages=[{"role": "user",
                               "content": _dataset_prompt(ds, name, fs)}])
            except Exception as e:
                log.error("classify %s failed: %s", ds, str(e)[:300])
                continue
            parsed = _parse(msg)
            if parsed:
                out[ds] = parsed

    save(out, cache_path)
    log.info("priors cached for %d datasets -> %s", len(out), cache_path)
    return out


def _reason(result) -> str:
    """Pull a human-readable cause out of a failed batch result."""
    err = getattr(result, "error", None)
    inner = getattr(err, "error", None)
    msg = getattr(inner, "message", None) or getattr(err, "message", None)
    return str(msg or getattr(result, "type", "unknown"))[:200]


def _safe_id(ds: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in ds)[:60]


def _parse(message) -> dict | None:
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        try:
            body = json.loads(block.text)
        except (ValueError, AttributeError):
            continue
        scores = body.get("scores")
        if isinstance(scores, dict):
            return {"scores": {k: min(1.0, max(0.0, float(v)))
                               for k, v in scores.items() if k in _FAMILIES},
                    "rationale": str(body.get("rationale", ""))[:400]}
    return None


def load(cache_path: str = CACHE) -> dict:
    try:
        with open(cache_path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data: dict, cache_path: str = CACHE) -> None:
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(data, fh, indent=1)


def arm_priors(fields: Iterable[dict], cache_path: str = CACHE
               ) -> dict[str, tuple[float, float]]:
    """Turn dataset scores into Beta pseudo-counts per (theme, template) arm.

    A dataset's score for a family is spread over every template in that
    family, then averaged across the datasets that carry the theme. The result
    is (alpha, beta) pseudo-counts the bandit adds to its posterior -- so a
    prior of 0.9 is worth a handful of imaginary successes, never a verdict.
    """
    from .bandit import arm_name
    from .catalog import Catalog
    scores = load(cache_path)
    if not scores:
        return {}
    fields = list(fields)
    acc: dict[str, list[float]] = {}
    for ds, fs in Catalog.by_dataset(fields).items():
        entry = scores.get(ds)
        if not entry:
            continue
        fam_scores = entry.get("scores", {})
        themes = {f.get("_theme") for f in fs if f.get("_theme")}
        for theme in themes:
            for t in T.LIBRARY:
                s = fam_scores.get(t.family)
                if s is None:
                    continue
                acc.setdefault(arm_name(theme, t.name), []).append(float(s))
    out = {}
    for arm, vals in acc.items():
        m = sum(vals) / len(vals)
        out[arm] = (PRIOR_STRENGTH * m, PRIOR_STRENGTH * (1.0 - m))
    return out
