"""Write PowerPoolSelected tag + an Idea/Rationale description onto verified
Power Pool candidates, using BRAIN's own field/operator definitions to ground
the write-up instead of boilerplate.

Input: a --json report from check_powerpool.py --deep (only alphas with
eligible=True and verified=True are touched). Never overwrites an alpha that
already has a tag or description -- those are left untouched and reported.

    python3 scripts/tag_powerpool.py /tmp/pp_full.json [--dry-run] [--limit N]
    python3 scripts/tag_powerpool.py report.json --descriptions hand_written.json

`--descriptions` maps alpha_id -> a description you wrote yourself, used instead
of the template. The template's Idea sentence is "when <field> changes, price
follows", which is wrong for any alpha that is not a field-change signal (a
residual-of-returns reversal, a rank spread, ...) -- and a Power Pool
description that misstates the mechanism is worse than a short one. Every
description, template or hand-written, is validated before anything is PATCHed:
all three section headers present and at least 100 characters (the two checks
BRAIN itself runs as POWER_POOL_DESCRIPTION_FORMAT / _LENGTH).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import AuthError, BiometricRequired, BrainClient

# Per-field economic-channel phrasing (Traditional Chinese), grounded in what
# each field actually measures -- not a generic template. Extend this dict if
# a new field shows up in a future candidate batch.
FIELD_CHANNEL = {
    "analyst_embedding_component_25": "分析師報告中隱含語意特徵的變化,可能反映分析師論述基調的轉變",
    "analyst_embedding_component_37": "分析師報告中隱含語意特徵的變化,可能反映分析師論述基調的轉變",
    "analyst_sentiment_score_5": "分析師整體看法(情緒)的轉變",
    "asset_utilization_score_2": "公司資產運用效率的變化",
    "atm_call_option_delta_quartile_3": "選擇權市場對該股方向性風險看法的轉變",
    "atm_call_volatility_120d_short_term": "選擇權市場對該股未來波動的預期變化",
    "change_log_weighted_count_2": "ETF 等被動資金調整持股的變化,反映被動資金流向",
    "executed_short_position_share_sum": "空頭部位變化,反映市場看空/看多力道的轉變",
    "inst6_num_of_institutional_holders": "機構投資人持股家數的變化,反映機構關注度轉變",
    "mdl250_hc_eq_mktcap": "公司市值規模的變化",
    "search_interest_1y_corporate_name": "投資人對公司名稱的搜尋熱度變化,反映一般投資人關注度",
    "search_interest_84d_equity_symbol": "投資人對股票代號的搜尋熱度變化,反映短期關注度",
}

# Priority order to pick ONE "temporal dynamic" phrase when several ts_*
# operators are present in one alpha.
TS_PHRASE = [
    ("ts_zscore", "以標準差為單位衡量的異常程度出現偏離"),
    ("ts_av_diff", "相對其自身過去一段時間的平均值出現偏離"),
    ("ts_delta", "近期出現明顯變化(動能)"),
    ("ts_decay_linear", "近期加權平均趨勢出現轉變"),
    ("ts_mean", "近期平均水準出現轉變"),
    ("days_from_last_change", "距離上次數值變動的天數出現異常"),
]

CROSS_PHRASE = [
    ("group_rank", "在同類股(subindustry/industry)內做相對排名,分離出相對而非全類股共通的訊號"),
    ("group_neutralize", "去除同類股平均水準的影響,只保留相對差異"),
    ("group_scale", "在同類股內做標準化,使不同類股間可比較"),
    ("rank", "在全市場做橫截面排名,降低極端值影響"),
    ("quantile", "轉換為分位數並套用統計分布,壓縮極端值影響"),
]


REQUIRED_HEADERS = ("Idea:", "Rationale for data used:", "Rationale for operators used:")
MIN_DESCRIPTION_CHARS = 100


def validate_description(desc: str) -> list[str]:
    """Problems that would make BRAIN's own description checks warn. Empty = ok."""
    problems = [f"missing section header {h!r}" for h in REQUIRED_HEADERS if h not in desc]
    if len(desc) < MIN_DESCRIPTION_CHARS:
        problems.append(f"{len(desc)} chars < {MIN_DESCRIPTION_CHARS}")
    order = [desc.find(h) for h in REQUIRED_HEADERS]
    if all(i >= 0 for i in order) and order != sorted(order):
        problems.append("sections out of order (Idea, data, operators)")
    return problems


def select_candidates(data: list[dict], include_unverified: bool = False) -> list[dict]:
    """Records to tag. Default: eligible AND verified (every server gate resolved).

    `include_unverified` also admits eligible records whose ONLY unresolved gate is
    POWER_POOL_CORRELATION. BRAIN computes that lazily and, after the pool changes
    or a burst of /check calls, can leave it PENDING for hours; tagging is
    reversible and does not consume a submission slot, so it is allowed -- but the
    correlation must then have been measured another way (corr_powerpool.py).
    Any other pending or failed gate still excludes the record.
    """
    out = []
    for r in data:
        if not r.get("eligible"):
            continue
        if r.get("verified"):
            out.append(r)
        elif include_unverified and set(r.get("pending_gates") or []) <= {"POWER_POOL_CORRELATION"}:
            out.append(r)
    return out


def build_description(fields: list[str], operators: list[str],
                       field_descs: dict[str, str], op_descs: dict[str, dict]) -> str:
    field_lines = [f"- {f}: {field_descs.get(f, '(說明未取得)')}" for f in fields]
    op_lines = [f"- {op_descs.get(o, {}).get('definition', o)}: "
                f"{op_descs.get(o, {}).get('description', '')}" for o in operators]

    channel = " / ".join(dict.fromkeys(FIELD_CHANNEL.get(f, f) for f in fields))
    ts_phrase = next((p for op, p in TS_PHRASE if op in operators), "數值出現變化")
    cross_phrase = next((p for op, p in CROSS_PHRASE if op in operators), None)

    idea = (f"當「{channel}」的相關欄位{ts_phrase}時,可能反映尚未完全反映在價格上的資訊,"
            f"進而領先價格朝同方向修正。")
    if cross_phrase:
        idea += f" 訊號經過處理:{cross_phrase}。"

    return (
        f"Idea: {idea}\n\n"
        f"Rationale for data used:\n" + "\n".join(field_lines) + "\n\n"
        f"Rationale for operators used:\n" + "\n".join(op_lines)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="path to a check_powerpool.py --deep --json output file")
    ap.add_argument("--dry-run", action="store_true",
                     help="build and print descriptions but do not PATCH anything")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-unverified", action="store_true",
                    help="also tag eligible records whose only unresolved gate is "
                         "POWER_POOL_CORRELATION (measure it with corr_powerpool.py)")
    ap.add_argument("--descriptions", default=None,
                    help="JSON {alpha_id: description}; used instead of the "
                         "template for those alphas")
    args = ap.parse_args()

    custom: dict[str, str] = {}
    if args.descriptions:
        custom = json.load(open(args.descriptions))
        bad = {a: validate_description(d) for a, d in custom.items()
               if validate_description(d)}
        if bad:
            print(f"refusing: hand-written description(s) fail validation: {bad}",
                  file=sys.stderr)
            return 2

    _load_env()
    email = os.environ.get("WQ_EMAIL", "")
    password = os.environ.get("WQ_PASSWORD", "")
    if not email or not password:
        print("WQ_EMAIL / WQ_PASSWORD not set.", file=sys.stderr)
        return 2

    client = BrainClient(email, password)
    try:
        client.authenticate()
    except (BiometricRequired, AuthError) as e:
        print(f"authentication failed: {e}", file=sys.stderr)
        return 1

    from check_powerpool import rejudge
    data = rejudge(json.load(open(args.report)))
    candidates = select_candidates(data, args.include_unverified)
    unverified = [r["alpha_id"] for r in candidates if not r.get("verified")]
    if unverified:
        print(f"NOTE: {len(unverified)} candidate(s) have POWER_POOL_CORRELATION "
              f"still PENDING at BRAIN: {unverified}", file=sys.stderr)
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"{len(candidates)} verified candidate(s) to process.\n", file=sys.stderr)

    print("fetching operator definitions ...", file=sys.stderr)
    op_descs = {o["name"]: o for o in client.operators() if o.get("name")}

    unique_fields = sorted({f for r in candidates for f in r["fields"]})
    missing = [f for f in unique_fields if f not in FIELD_CHANNEL]
    if missing:
        print(f"WARNING: no channel phrasing for fields {missing} -- "
              f"their description will read less specific.", file=sys.stderr)

    print("fetching field definitions ...", file=sys.stderr)
    field_descs: dict[str, str] = {}
    for fid in unique_fields:
        try:
            results = client.data_fields(search=fid, region="USA", delay=1, universe="TOP3000")
        except Exception as e:
            print(f"  field lookup failed for {fid}: {e}", file=sys.stderr)
            results = []
        match = next((f for f in results if f.get("id") == fid),
                     results[0] if results else None)
        field_descs[fid] = (match or {}).get("description", "")

    written, skipped, errors = [], [], []
    for i, r in enumerate(candidates):
        aid = r["alpha_id"]
        try:
            alpha = client.alpha(aid)
        except Exception as e:
            errors.append((aid, str(e)))
            continue
        existing_tags = alpha.get("tags") or []
        existing_desc = (alpha.get("regular") or {}).get("description")
        if existing_tags or existing_desc:
            skipped.append(aid)
            print(f"  [{i+1}/{len(candidates)}] {aid}: SKIP (already has "
                  f"tags={existing_tags} or a description)", file=sys.stderr)
            continue

        desc = custom.get(aid) or build_description(
            r["fields"], r["operators"], field_descs, op_descs)
        problems = validate_description(desc)
        if problems:
            errors.append((aid, f"description fails validation: {problems}"))
            print(f"  [{i+1}/{len(candidates)}] {aid}: NOT WRITTEN, {problems}",
                  file=sys.stderr)
            continue
        if args.dry_run:
            print(f"\n===== {aid} (dry run) =====\n{desc}\n", file=sys.stderr)
            written.append(aid)
            continue

        try:
            client.patch_alpha(aid, tags=["PowerPoolSelected"], regular={"description": desc})
        except Exception as e:
            errors.append((aid, str(e)))
            print(f"  [{i+1}/{len(candidates)}] {aid}: ERROR {e}", file=sys.stderr)
            continue
        written.append(aid)
        print(f"  [{i+1}/{len(candidates)}] {aid}: tagged + described", file=sys.stderr)

    print(f"\n=== done: {len(written)} written, {len(skipped)} skipped "
          f"(already had tags/description), {len(errors)} error(s) ===")
    if errors:
        for aid, msg in errors:
            print(f"  ERROR {aid}: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
