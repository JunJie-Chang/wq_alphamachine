"""Live-database consistency audit for the accumulated data/machine.db.

scripts/smoke_test.py proves the WRITE PATH is correct against a fresh
tempfile db, driven the way the test drives it. It says nothing about whether
the actual accumulated database -- written by real runs, crashes, migrations,
and interrupted processes over its whole lifetime -- currently satisfies the
invariants that write path assumes. SQLite foreign keys are OFF (Store.conn(),
store.py) and the schema has no FOREIGN KEY/CHECK constraints, so every
cross-table invariant here is enforced purely by caller discipline -- exactly
the kind of thing that erodes silently. This module is the compensating
control: read the live db, recompute each invariant from first principles,
and report where it has drifted.

Every check takes a plain read-only sqlite3.Connection (never a Store --
Store() runs migrations and takes a write connection, which this must never
do while autoloop.py is writing) and returns a list of Finding.

Born from two real bugs found only because a human happened to ask about one
specific candidate: a poisoned deep_checks cache entry (ok=1, empty checks,
from a lazy-computation race) that read as a clean pass for a full
deep_check_ttl_hours, and a stale-PASS bug in gate_passing_now() that let an
old passing snapshot outrank a later, correct failure. Checks 2 and 3 below
are those two bugs' exact shapes, checked against the live table instead of a
synthetic fixture.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from .config import RunConfig, SimSettings
from .store import GATE_PASSING_NOW_SQL, cid
from . import templates as T


@dataclass
class Finding:
    check: str                          # stable id, e.g. "candidate_id_mismatch"
    severity: str                       # "critical" | "warning" | "info"
    table: str
    summary: str
    detail: dict = field(default_factory=dict)


def open_ro(path: str = "data/machine.db") -> sqlite3.Connection:
    """Read-only connection, never through Store.

    Store() runs migrations and takes a write-capable WAL connection; this
    must be safe to call at any time, including mid-write by autoloop.py.
    scripts/_status.py and scripts/night_check.sh already open data/machine.db
    this exact way while autoloop runs -- WAL's "readers never block writers"
    guarantee is already relied on in production, not just asserted here.
    """
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


NEEDS_FIELD2 = {t.name for t in T.LIBRARY if t.needs_field2}


def check_gate_history_ok0_with_deep(conn: sqlite3.Connection) -> list[Finding]:
    """Deep verdicts are only ever supposed to be computed once the metric
    gate has already passed -- three independent call sites (cmd_refine,
    cmd_select, cmd_gatelog) each re-implement that rule rather than sharing
    it, so a future fourth call site could get it wrong."""
    out = []
    for r in conn.execute(
            "SELECT id, candidate_id, expr, ts FROM gate_history "
            "WHERE ok=0 AND deep_ok IS NOT NULL"):
        out.append(Finding(
            "gate_history_deep_computed_after_metric_fail", "critical",
            "gate_history",
            "a deep verdict was recorded for a candidate that failed the "
            "metric gate -- deep checks should only ever run after ok=1",
            {"id": r["id"], "candidate_id": r["candidate_id"],
             "expr": (r["expr"] or "")[:80]}))
    return out


def check_deep_checks_poisoned(conn: sqlite3.Connection) -> list[Finding]:
    """The exact shape of the lazy-computation race: BRAIN can answer 200
    with an empty `checks` list before it has actually finished computing.
    An authentic PASS always carries dozens of entries (LOW_SHARPE,
    LOW_FITNESS, ...), so ok=1 with an empty list is never legitimate --
    submit.py's deep_verdict() now refuses to write or trust this shape, but
    older rows written before that fix (or a future caller that reintroduces
    the bug) would sit here undetected without this check."""
    out = []
    for r in conn.execute("SELECT alpha_id, checks, fetched FROM deep_checks "
                          "WHERE ok=1"):
        try:
            checks = json.loads(r["checks"] or "[]")
        except (ValueError, TypeError):
            checks = None
        if not checks:
            out.append(Finding(
                "deep_checks_poisoned", "critical", "deep_checks",
                "a cached deep-check row is marked ok=1 but its checks "
                "payload is empty or unparseable -- a real BRAIN response "
                "is never empty for a computed alpha",
                {"alpha_id": r["alpha_id"], "fetched": r["fetched"]}))
    return out


def check_gate_passing_now_consistency(conn: sqlite3.Connection
                                       ) -> list[Finding]:
    """Independently recomputes 'the true latest gate_history row per expr,
    then judge pass/fail' in plain Python, tie-broken by id (SQLite's
    insertion-order proxy) when two rows share a timestamp -- a case the live
    query's MAX(ts) join cannot distinguish -- then diffs that ground truth
    against what GATE_PASSING_NOW_SQL actually reports. This is deliberately
    NOT just re-running that same SQL and trusting it: if the query is ever
    reverted to its pre-fix form (or a differently-buggy variant is added
    elsewhere), the diff against this independent recomputation still fires."""
    rows = conn.execute(
        "SELECT id, ts, expr, ok, deep_ok FROM gate_history").fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        cur = latest.get(r["expr"])
        if cur is None or (r["ts"], r["id"]) > (cur["ts"], cur["id"]):
            latest[r["expr"]] = r
    truth_passing = {e for e, r in latest.items()
                     if r["ok"] == 1 and r["deep_ok"] == 1}
    reported = {r["expr"] for r in
                conn.execute(GATE_PASSING_NOW_SQL, (10**9,))}

    out = []
    for e in sorted(reported - truth_passing):
        out.append(Finding(
            "gate_passing_now_stale_pass", "critical", "gate_history",
            "gate_passing_now() reports this expression submittable, but "
            "its true latest row (by ts, tie-broken by id) is not a clean "
            "ok=1/deep_ok=1 pass -- the exact shape of a real regression "
            "found and fixed once already",
            {"expr": e[:80]}))
    for e in sorted(truth_passing - reported):
        out.append(Finding(
            "gate_passing_now_missing_true_pass", "warning", "gate_history",
            "this expression's true latest row is a clean pass but "
            "gate_passing_now() did not report it -- check for a ts "
            "collision with another row for the same expr",
            {"expr": e[:80]}))
    return out


def check_arms_bounds(conn: sqlite3.Connection) -> list[Finding]:
    """Nothing in bump_arm (a plain += UPDATE) enforces pass <= trials for
    any stage, or that the cross-stage `trials` counter stays >= every
    individual stage's trial count."""
    out = []
    for r in conn.execute("SELECT * FROM arms"):
        bad = (r["scan_pass"] < 0 or r["scan_pass"] > r["scan_trials"] or
               r["gate_pass"] < 0 or r["gate_pass"] > r["refine_trials"] or
               r["deep_pass"] < 0 or r["deep_pass"] > r["deep_trials"] or
               r["submitted"] < 0 or r["submitted"] > r["trials"] or
               r["trials"] < r["scan_trials"] or
               r["trials"] < r["refine_trials"] or
               r["trials"] < r["deep_trials"])
        if bad:
            out.append(Finding(
                "arms_bounds_violation", "warning", "arms",
                "an arm's pass count exceeds its trial count for some "
                "stage, or the cross-stage trials total is smaller than a "
                "per-stage count",
                {"arm": r["arm"],
                 "scan": f"{r['scan_pass']}/{r['scan_trials']}",
                 "gate": f"{r['gate_pass']}/{r['refine_trials']}",
                 "deep": f"{r['deep_pass']}/{r['deep_trials']}",
                 "submitted": r["submitted"], "trials": r["trials"]}))
    return out


def check_bandit_taught_vs_deep_trials(conn: sqlite3.Connection
                                       ) -> list[Finding]:
    """One bandit_taught(stage='deep') claim should bump arms.deep_trials
    exactly once (cli._teach_deep pairs claim_bandit_trial with bandit.update
    unconditionally) -- store._rebuild_deep_trials's own migration logic
    relies on this exact equality. Deliberately NOT extended to scan/refine:
    _backfill_arm_stages documents those as an approximate floor for
    pre-migration history, so a mismatch there is expected, not a bug."""
    counts = {r["arm"]: r["n"] for r in conn.execute(
        "SELECT arm, COUNT(*) n FROM bandit_taught WHERE stage='deep' "
        "GROUP BY arm")}
    out = []
    for r in conn.execute("SELECT arm, deep_trials FROM arms"):
        n = counts.get(r["arm"], 0)
        if n != r["deep_trials"]:
            out.append(Finding(
                "deep_trials_bandit_taught_mismatch", "warning", "arms",
                "bandit_taught(stage=deep) row count for this arm does not "
                "equal arms.deep_trials; each claim should bump deep_trials "
                "exactly once",
                {"arm": r["arm"], "bandit_taught_deep_n": n,
                 "arms_deep_trials": r["deep_trials"]}))
    return out


def check_candidate_id_integrity(conn: sqlite3.Connection) -> list[Finding]:
    """Both places that ever mint a candidate id (generator.py, refine.py)
    always pass settings_key=SimSettings(...).key(), never the json-dump
    fallback in add_candidate -- so recomputing cid(expr, key) from a row's
    own stored expr+settings should always reproduce its own id exactly."""
    out = []
    for r in conn.execute("SELECT id, expr, settings FROM candidates"):
        try:
            payload = json.loads(r["settings"] or "{}")
        except ValueError as e:
            # json.loads raises json.JSONDecodeError (a ValueError subclass)
            # on malformed JSON -- distinct from, and worse than, the
            # TypeError case below: this is actual column corruption, not a
            # config.py field rename.
            out.append(Finding(
                "candidate_settings_corrupted", "critical", "candidates",
                f"settings column is not valid JSON: {e}",
                {"candidate_id": r["id"]}))
            continue
        try:
            key = SimSettings(**payload).key()
        except TypeError as e:
            out.append(Finding(
                "candidate_settings_schema_drift", "info", "candidates",
                f"settings JSON has fields SimSettings(**kw) rejects: {e} "
                "-- likely a config.py field rename, not data corruption",
                {"candidate_id": r["id"]}))
            continue
        expected = cid(r["expr"], key)
        if expected != r["id"]:
            out.append(Finding(
                "candidate_id_mismatch", "critical", "candidates",
                "recomputing sha1(expr + SimSettings(**settings).key()) "
                "from this row's own columns does not reproduce its "
                "stored id",
                {"candidate_id": r["id"], "expected_id": expected,
                 "expr": (r["expr"] or "")[:80]}))
    return out


def check_candidate_stage_shape(conn: sqlite3.Connection) -> list[Finding]:
    """generator.py always inserts parent_id=NULL/stage='scan'; refine.py's
    _queue() always inserts a parent_id with stage='refine'. Refiner.finalize
    can promote either a scan-root or a refine variant directly to 'gated'."""
    out = []
    for r in conn.execute(
            "SELECT id, stage FROM candidates "
            "WHERE parent_id IS NULL AND stage NOT IN ('scan','gated')"):
        out.append(Finding(
            "root_candidate_bad_stage", "warning", "candidates",
            "a root candidate (no parent) has a stage other than "
            "'scan'/'gated'",
            {"candidate_id": r["id"], "stage": r["stage"]}))
    for r in conn.execute(
            "SELECT id, stage FROM candidates "
            "WHERE parent_id IS NOT NULL AND stage NOT IN ('refine','gated')"):
        out.append(Finding(
            "variant_candidate_bad_stage", "warning", "candidates",
            "a refine variant (has a parent) has a stage other than "
            "'refine'/'gated'",
            {"candidate_id": r["id"], "stage": r["stage"]}))
    return out


def check_gated_has_ok_sim(conn: sqlite3.Connection) -> list[Finding]:
    """cmd_select silently assumes every stage='gated' candidate has an OK
    simulation, via a plain join -- a gated candidate with no OK sim would
    just vanish from consideration with no error."""
    out = []
    for r in conn.execute(
            "SELECT c.id FROM candidates c WHERE c.stage='gated' "
            "AND NOT EXISTS (SELECT 1 FROM sims s "
            "WHERE s.candidate_id=c.id AND s.status='OK')"):
        out.append(Finding(
            "gated_without_ok_sim", "critical", "candidates",
            "candidate is marked stage='gated' but has no OK simulation "
            "record -- it will silently disappear from cmd_select",
            {"candidate_id": r["id"]}))
    return out


def check_field2_shape(conn: sqlite3.Connection) -> list[Finding]:
    """generator.py only ever sets field2 for the five needs_field2=True
    templates, and rejects f2id == fid before it ever reaches add_candidate --
    this is the live-db regression guard for both rules."""
    out = []
    for r in conn.execute(
            "SELECT id, template, field, field2 FROM candidates"):
        has_f2 = bool(r["field2"])
        needs = r["template"] in NEEDS_FIELD2
        if has_f2 and not needs:
            out.append(Finding(
                "field2_set_for_single_field_template", "warning",
                "candidates",
                "field2 is populated for a template not in "
                "templates.LIBRARY's needs_field2 set",
                {"candidate_id": r["id"], "template": r["template"]}))
        if needs and not has_f2:
            out.append(Finding(
                "field2_missing_for_two_field_template", "warning",
                "candidates",
                "a two-field template has no field2 recorded",
                {"candidate_id": r["id"], "template": r["template"]}))
        if has_f2 and r["field"] == r["field2"]:
            out.append(Finding(
                "field2_equals_field", "critical", "candidates",
                "field2 equals field -- generator.py's own "
                "generation-time guard (f2id == fid) should have "
                "prevented this",
                {"candidate_id": r["id"], "field": r["field"]}))
    return out


def check_orphan_references(conn: sqlite3.Connection) -> list[Finding]:
    """SQLite foreign keys are off and the schema declares none -- this is
    the compensating control for every cross-table reference that would
    otherwise be enforced by a FOREIGN KEY constraint."""
    out = []
    for r in conn.execute(
            "SELECT candidate_id FROM sims "
            "WHERE candidate_id NOT IN (SELECT id FROM candidates)"):
        out.append(Finding("orphan_sims_reference", "warning", "sims",
                           "sims row references a candidate_id that does "
                           "not exist", {"candidate_id": r["candidate_id"]}))
    for r in conn.execute(
            "SELECT candidate_id FROM gate_history "
            "WHERE candidate_id NOT IN (SELECT id FROM candidates)"):
        out.append(Finding("orphan_gate_history_reference", "warning",
                           "gate_history",
                           "gate_history row references a candidate_id "
                           "that does not exist",
                           {"candidate_id": r["candidate_id"]}))
    for r in conn.execute(
            "SELECT candidate_id FROM inflight "
            "WHERE candidate_id NOT IN (SELECT id FROM candidates)"):
        out.append(Finding("orphan_inflight_reference", "warning",
                           "inflight",
                           "inflight row references a candidate_id that "
                           "does not exist",
                           {"candidate_id": r["candidate_id"]}))
    for r in conn.execute(
            "SELECT id, parent_id FROM candidates WHERE parent_id IS NOT "
            "NULL AND parent_id NOT IN (SELECT id FROM candidates)"):
        out.append(Finding("orphan_candidates_parent_reference", "warning",
                           "candidates",
                           "candidate's parent_id does not exist",
                           {"candidate_id": r["id"],
                            "missing_parent_id": r["parent_id"]}))
    return out


def check_stuck_inflight(conn: sqlite3.Connection, *,
                         sim_timeout: float | None = None,
                         stuck_multiple: float = 6.0) -> list[Finding]:
    """A generous multiple of sim_timeout (default 900s -> 90 minutes): wide
    enough that one slow poll cycle never false-positives, tight enough to
    surface a dead Executor.reclaim() well before 'forever'. A stuck row
    means a paid-for slot's result is being silently lost."""
    timeout = sim_timeout if sim_timeout is not None else RunConfig().sim_timeout
    cutoff = time.time() - timeout * stuck_multiple
    out = []
    for r in conn.execute(
            "SELECT candidate_id, dispatched FROM inflight "
            "WHERE dispatched < ?", (cutoff,)):
        age_h = (time.time() - r["dispatched"]) / 3600.0
        out.append(Finding(
            "stuck_inflight", "warning", "inflight",
            f"in-flight simulation dispatched {age_h:.1f}h ago with no "
            "result yet -- Executor.reclaim() may not be running",
            {"candidate_id": r["candidate_id"], "age_hours": round(age_h, 1)}))
    return out


CHECKS = [
    check_gate_history_ok0_with_deep,
    check_deep_checks_poisoned,
    check_gate_passing_now_consistency,
    check_arms_bounds,
    check_bandit_taught_vs_deep_trials,
    check_candidate_id_integrity,
    check_candidate_stage_shape,
    check_gated_has_ok_sim,
    check_field2_shape,
    check_orphan_references,
    check_stuck_inflight,
]


def run_all(conn: sqlite3.Connection, checks=None) -> list[Finding]:
    out: list[Finding] = []
    for fn in (checks or CHECKS):
        out.extend(fn(conn))
    return out
