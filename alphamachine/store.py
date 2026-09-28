"""SQLite persistence. One writer process, WAL mode, idempotent by expr hash.

The database IS the machine's memory. Every simulation ever run is recorded so
that (a) restarts never re-burn a slot on work already done, and (b) the bandit
in bandit.py can learn which (theme, template) arms actually convert.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

log = logging.getLogger("alphamachine.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id            TEXT PRIMARY KEY,      -- sha1(expr + settings key)
    expr          TEXT NOT NULL,
    settings      TEXT NOT NULL,         -- json
    theme         TEXT,
    template      TEXT,
    field         TEXT,
    field2        TEXT,                  -- two-field templates only
    stage         TEXT DEFAULT 'scan',   -- scan | refine | gated
    parent_id     TEXT,
    created       REAL
);
CREATE INDEX IF NOT EXISTS idx_cand_stage ON candidates(stage);
-- refine's "not laddered yet" test is NOT EXISTS (... WHERE k.parent_id=c.id):
-- without this it scanned every candidate per row, ~5 minutes at 82k rows.
CREATE INDEX IF NOT EXISTS idx_cand_parent ON candidates(parent_id);

CREATE TABLE IF NOT EXISTS sims (
    candidate_id  TEXT PRIMARY KEY,
    alpha_id      TEXT,
    status        TEXT,                  -- OK | ERROR | TIMEOUT
    error         TEXT,
    sharpe        REAL, fitness REAL, turnover REAL, returns REAL,
    drawdown      REAL, margin REAL,
    longCount     INTEGER, shortCount INTEGER,
    subuniverse_sharpe REAL,
    -- Recency, pulled out of the payload we already store. Every other metric
    -- here is a whole-period aggregate, so without these a signal that died in
    -- 2018 still scores on its 2015 performance.
    two_year_sharpe REAL,                -- BRAIN's LOW_2Y_SHARPE value (~44%)
    test_sharpe     REAL,                -- trailing testPeriod Sharpe (~100%)
    -- Payout, from MATCHES_PYRAMID (present on 100% of OK sims). The
    -- multiplier is the MINIMUM over every pyramid the expression matches --
    -- measured on 9,414 multi-pyramid alphas, never the max, never the mean --
    -- so touching an extra dataset category can only hold the payout down.
    pyramid_multiplier REAL,
    pyramid_names      TEXT,             -- json list, e.g. ["USA/D1/PV", ...]
    pyramid_effective  INTEGER,          -- pyramids this alpha counts toward
    -- The OTHER recency check -- BRAIN sends exactly one of this or
    -- two_year_sharpe per alpha, and this is the larger bucket. Same
    -- {value, limit} shape; is_ladder_limit is per-alpha (scales with years
    -- of history), same reason two_year_sharpe's limit is read, not assumed.
    is_ladder_sharpe   REAL,
    is_ladder_limit    REAL,
    -- Worst (actual / subuniverse_floor) across a champion's own rung-5
    -- children -- real re-simulation against a tighter universe, not BRAIN's
    -- formula estimate. NULL until refine.finalize() computes it (only
    -- meaningful on a 'gated' row that has rung-5 children on disk).
    portability_ratio  REAL,
    checks        TEXT,                  -- json list
    raw           TEXT,                  -- json
    finished      REAL
);
CREATE INDEX IF NOT EXISTS idx_sims_sharpe ON sims(sharpe);
CREATE INDEX IF NOT EXISTS idx_sims_alpha  ON sims(alpha_id);

-- Simulations dispatched to BRAIN but not yet resolved locally.
-- BRAIN charges the slot the moment you POST /simulations, and finishes the
-- job whether or not we are still listening. If the process dies mid-poll and
-- we have not recorded the progress URL, that slot is simply burnt: on restart
-- the candidate still looks pending and we pay for it a second time.
CREATE TABLE IF NOT EXISTS inflight (
    candidate_id  TEXT PRIMARY KEY,
    progress_url  TEXT NOT NULL,
    child_index   INTEGER DEFAULT 0,   -- position within a multi-sim bundle
    dispatched    REAL
);

-- Unused since 2026-09-28: correlation is read from BRAIN, never recomputed
-- from PnL. Kept so existing databases need no migration.
CREATE TABLE IF NOT EXISTS pnl (
    alpha_id      TEXT PRIMARY KEY,
    dates         TEXT,                  -- json list of ISO dates
    vals          TEXT,                  -- json list of cumulative pnl
    fetched       REAL
);

-- The SHAPE of an alpha's correlation against BRAIN's production pool, from
-- /correlations/prod. Separate from deep_checks on purpose: save_deep() binds
-- a positional VALUES (?,?,?,?,?,?) that a column count change breaks
-- silently, and audit.check_deep_checks_poisoned reads that table with
-- assumptions about its shape.
--
-- Why the shape and not just the max: PROD_CORRELATION scores the MAXIMUM
-- against ~2.5M production alphas, and the expected maximum of that many
-- draws is mean + sd*z(N) with z fixed near 4.95. So mean and sd are the only
-- two things an alpha can actually change. Recording just the max (which the
-- /check payload already gives) tells us we failed; recording the profile
-- tells us whether anything we changed moved the distribution.
CREATE TABLE IF NOT EXISTS prod_corr_hist (
    alpha_id      TEXT PRIMARY KEY,
    hist          TEXT,                  -- json recordset, verbatim
    max_corr      REAL,
    min_corr      REAL,
    mean          REAL,
    sd            REAL,
    expected_max  REAL,                  -- mean + sd*z(n_alphas)
    n_alphas      INTEGER,
    fetched       REAL
);

CREATE TABLE IF NOT EXISTS submissions (
    alpha_id      TEXT PRIMARY KEY,
    submitted_at  REAL,
    verdict       TEXT,
    final_status  TEXT,
    detail        TEXT
);

-- One row per (theme::template) arm, holding the bandit's evidence.
--
-- The counters are split BY FUNNEL STAGE rather than pooled. A scan simulation
-- and a refine champion are different events with different conversion rates,
-- and pooling them means an arm that receives more scan budget accumulates
-- "failures" at a gate it was never presented to -- i.e. the bandit penalises
-- an arm for having been explored. Each stage therefore keeps its own
-- denominator, and bandit.Arm.sample() multiplies the three rates.
--
-- corr_* vs deep_*: the deep counters record TRUE end-to-end conversion (did
-- deep_verdict pass everything), which is what `report` should show but is a
-- terrible learning signal -- it is 0 for every arm, because ~97% of
-- candidates fail SOMETHING and the bit collapses "this transform shape is
-- crowded" (attributable to the arm) with "this field's history decayed"
-- (a property of the field, not the shape). corr_* isolates the half the arm
-- actually controls: did PROD_CORRELATION come in under its limit. sample()
-- learns on corr_*; deep_* stays for reporting.
CREATE TABLE IF NOT EXISTS arms (
    arm           TEXT PRIMARY KEY,      -- "theme::template"
    trials        INTEGER DEFAULT 0,     -- reporting total across all stages
    scan_trials   INTEGER DEFAULT 0,
    scan_pass     INTEGER DEFAULT 0,
    refine_trials INTEGER DEFAULT 0,
    gate_pass     INTEGER DEFAULT 0,
    corr_trials   INTEGER DEFAULT 0,
    corr_pass     INTEGER DEFAULT 0,
    deep_trials   INTEGER DEFAULT 0,
    deep_pass     INTEGER DEFAULT 0,
    submitted     INTEGER DEFAULT 0,
    sum_sharpe    REAL DEFAULT 0.0,
    updated       REAL
);

-- Cache for the two verdicts that a simulation record cannot answer:
-- yearly consistency and PROD_CORRELATION. Both need extra API calls.
--
-- THIS CACHE IS A LEARNING SIGNAL, NEVER A SUBMISSION DECISION. `yearly` is IS
-- data and never changes once computed, so it is kept indefinitely. `checks`
-- holds /check output whose PROD_CORRELATION drifts with your book and with
-- BRAIN's production pool, so it expires on a TTL -- and Submitter.server_check()
-- deliberately does not read this table at all: submit always re-checks live.
CREATE TABLE IF NOT EXISTS deep_checks (
    alpha_id      TEXT PRIMARY KEY,
    yearly        TEXT,                  -- json recordset (immutable)
    checks        TEXT,                  -- json list from /check (TTL'd)
    ok            INTEGER,
    reasons       TEXT,                  -- json list
    fetched       REAL
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

-- Fields BRAIN has told us do not exist in a scope ("unknown variable",
-- "Invalid data field"). The catalog cache keeps listing them -- 137 such
-- errors in 3 days (2026-09-26), the same few fields over and over -- and each
-- one takes its whole bundle down: BRAIN cancels the unrun siblings.
CREATE TABLE IF NOT EXISTS dead_fields (
    region TEXT, universe TEXT, delay INTEGER, field TEXT,
    error TEXT, first_seen REAL,
    PRIMARY KEY (region, universe, delay, field)
);

-- Which bandit trials have already been counted, so a verdict teaches exactly
-- once no matter how many times it is recomputed.
--
-- `select` and `gatelog --passing` both run a full deep_verdict() -- real API
-- calls against yearly-stats and /check -- and used to discard the answer,
-- because teaching from a command someone runs repeatedly would inflate the
-- arm. Silence was the wrong fix for that: 21 deep checks were run and 1
-- reached the bandit, so four arms whose only deep result was a failure still
-- ranked near the top of the allocation. Idempotency is the right fix.
--
-- `key` is the LINEAGE ROOT, not the alpha: on real data those 21 alphas
-- collapse to 6 roots, and a root is the independent sample -- one scan
-- candidate, one ladder, one verdict. Keying on alpha_id would count a single
-- idea four times over its rung variants and re-ladders.
CREATE TABLE IF NOT EXISTS bandit_taught (
    stage         TEXT NOT NULL,         -- 'gate' | 'deep'
    key           TEXT NOT NULL,         -- lineage root candidate id
    arm           TEXT,
    ts            REAL,
    PRIMARY KEY (stage, key)
);

-- Independent, append-only trail of "did this candidate clear the gate"
-- decisions. Neither `candidates.stage='gated'` (just "refine's final answer
-- for this lineage") nor a dry-run `submit` (prints and writes nothing) leaves
-- a durable record on its own, and quality_verdict() is always evaluated
-- against whatever Gates are live *right now* -- so a threshold change (e.g.
-- tightening Sharpe) silently invalidates old verdicts with nothing left to
-- show it ever passed. This table snapshots the verdict AND the gate values
-- that produced it, every time one is computed, so "was this submit-grade,
-- and under what bar" stays answerable after the bar moves.
CREATE TABLE IF NOT EXISTS gate_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    candidate_id  TEXT,
    alpha_id      TEXT,
    expr          TEXT,
    sharpe        REAL, fitness REAL, turnover REAL,
    ok            INTEGER,             -- 1 = cleared gates at ts, 0 = did not
    reasons       TEXT,                -- json list, empty when ok
    gate_sharpe   REAL, gate_fitness REAL, gate_turnover_max REAL,
    gate_subuniverse_ratio REAL, gate_max_drawdown REAL,
    gate_max_self_corr REAL, gate_max_prod_corr REAL,
    -- The deep verdict (yearly consistency + PROD_CORRELATION), which the
    -- metric gate above cannot see. NULL = never checked, which is NOT the
    -- same as passing: it is why gate_passing_now() requires deep_ok=1 rather
    -- than merely "not failed".
    deep_ok       INTEGER,
    deep_reasons  TEXT                 -- json list
);
CREATE INDEX IF NOT EXISTS idx_gate_hist_cand ON gate_history(candidate_id);
CREATE INDEX IF NOT EXISTS idx_gate_hist_ts ON gate_history(ts);
"""


def cid(expr: str, settings_key: str) -> str:
    return hashlib.sha1(f"{expr}||{settings_key}".encode()).hexdigest()[:20]


# The single source of truth for "what is submittable right now" -- see
# Store.gate_passing_now()'s own docstring for the reasoning. Pulled out to
# module scope so alphamachine.audit can execute this exact string against
# its own read-only connection and diff the result against an independently
# recomputed answer, instead of trusting the query to check itself.
#
# The subquery finds each expr's single latest row FIRST, filtering for pass
# only in the outer WHERE -- so a later FAIL correctly displaces an earlier
# PASS. Restricting the subquery itself to `WHERE ok=1 AND deep_ok=1` (the bug
# this replaced) instead finds the latest row THAT HAPPENED TO PASS, ignoring
# anything more recent: an expr that passed once and was superseded by a real
# failure kept reporting the stale pass forever, exactly contradicting the
# docstring's "submit-grade AT THE TIME of their most recent check." Caught
# live: a lazy-computation race (client.alpha_check returning 200 with an
# empty checks list) produced a false deep_ok=1 snapshot; the corrective
# re-check that followed recorded deep_ok=0 as a NEW row, per gate_history's
# own append-only design -- but the old query kept surfacing the stale pass
# because it never looked past deep_ok=1 rows to notice a fail had been
# recorded since. DO NOT move the ok=1/deep_ok=1 filter into the subquery --
# that is precisely this bug.
#
# Grouped by candidate_id, NOT by expr. Expression-grouping was right while
# the ladder varied one neutralization; it became wrong the moment the search
# widened to nine, because the SAME expression under a different
# neutralization is a genuinely different alpha -- different settings,
# different candidate_id, different alpha_id, different PnL, separately
# submittable. Up to 26 distinct candidates now share a single expr.
#
# Caught live: WjPj1R0x (FAST neutralization) passed every gate and every
# BRAIN check -- ok=1, deep_ok=1, PROD_CORRELATION 0.557, 2Y Sharpe 1.93 --
# and was invisible here, because six OTHER neutralizations of the same
# expression were recorded failing at the identical timestamp and the
# MAX(ts) join picked one of those instead. candidate_id is the true identity
# of a submittable alpha; deduping to it still collapses the same candidate
# re-checked over time, which is all the expr grouping was ever meant to do.
GATE_PASSING_NOW_SQL = (
    "SELECT g.* FROM gate_history g JOIN "
    "(SELECT candidate_id, MAX(ts) mts FROM gate_history "
    " GROUP BY candidate_id) m "
    "ON m.candidate_id=g.candidate_id AND m.mts=g.ts "
    "WHERE g.ok=1 AND g.deep_ok=1 "
    "ORDER BY g.sharpe DESC LIMIT ?"
)


class Store:
    def __init__(self, path: str = "data/machine.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._local = threading.local()
        # Which tables existed BEFORE the schema ran. SCHEMA is
        # CREATE TABLE IF NOT EXISTS, so afterwards everything looks present and
        # a migration keyed on a missing TABLE (rather than a missing column)
        # would never fire.
        with self.conn() as c:
            before = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            c.executescript(SCHEMA)
        self._migrate(pre_existing=before)

    # ------------------------------------------------------------- migration
    # SCHEMA uses CREATE TABLE IF NOT EXISTS, so an existing database never
    # picks up a new column. Adding one therefore needs an explicit, idempotent
    # ALTER pass -- run on every open, cheap when there is nothing to do.
    MIGRATIONS: dict[str, list[tuple[str, str]]] = {
        "arms": [("scan_trials", "INTEGER DEFAULT 0"),
                 ("refine_trials", "INTEGER DEFAULT 0"),
                 ("deep_trials", "INTEGER DEFAULT 0"),
                 ("deep_pass", "INTEGER DEFAULT 0"),
                 ("corr_trials", "INTEGER DEFAULT 0"),
                 ("corr_pass", "INTEGER DEFAULT 0")],
        "gate_history": [("deep_ok", "INTEGER"),
                         ("deep_reasons", "TEXT")],
        "candidates": [("field2", "TEXT")],
        "sims": [("two_year_sharpe", "REAL"), ("test_sharpe", "REAL"),
                 ("pyramid_multiplier", "REAL"), ("pyramid_names", "TEXT"),
                 ("pyramid_effective", "INTEGER"),
                 ("is_ladder_sharpe", "REAL"), ("is_ladder_limit", "REAL"),
                 ("portability_ratio", "REAL")],
    }

    def _migrate(self, pre_existing: set[str] | None = None) -> None:
        c = self.conn()
        added: set[str] = set()
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue                       # table absent -- SCHEMA owns it
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    added.add(f"{table}.{name}")
        c.commit()
        if "arms.scan_trials" in added:
            self._backfill_arm_stages()
        if "arms.corr_trials" in added:
            self._backfill_corr_arms()
        if "sims.two_year_sharpe" in added:
            self._backfill_recency()
        if "sims.pyramid_multiplier" in added:
            self._backfill_pyramid()
        if "sims.is_ladder_sharpe" in added:
            self._backfill_is_ladder()
        if "sims.portability_ratio" in added:
            self._backfill_portability()
        # A database that already had gate_history but not the ledger has deep
        # verdicts on file that were never counted -- rebuild them once.
        pre = pre_existing or set()
        if "gate_history" in pre and "bandit_taught" not in pre:
            self._rebuild_deep_trials()

    def _backfill_recency(self) -> None:
        """Populate the two recency columns from payloads already on disk.

        Both numbers have been arriving with every simulation since day one --
        LOW_2Y_SHARPE inside `checks`, and the trailing-testPeriod Sharpe
        inside `raw.test` -- and both were dropped on the floor because
        extract() never read them. So the whole accumulated history is usable
        immediately rather than only simulations from here on, which matters:
        the alphas this was built to catch are already in the table.

        Exact, not inferred: every value is copied out of BRAIN's own stored
        response. Rows whose payload carries neither are left NULL, which
        downstream treats as "unknown" and never as "fine".
        """
        c = self.conn()
        rows = c.execute(
            "SELECT candidate_id, checks, raw FROM sims "
            "WHERE two_year_sharpe IS NULL AND test_sharpe IS NULL").fetchall()
        n2 = nt = 0
        for r in rows:
            two = test = None
            try:
                for ch in json.loads(r["checks"] or "[]"):
                    if ch.get("name") == "LOW_2Y_SHARPE":
                        two = ch.get("value")
                        break
            except (ValueError, TypeError, AttributeError):
                pass
            try:
                test = ((json.loads(r["raw"] or "{}") or {}).get("test")
                        or {}).get("sharpe")
            except (ValueError, TypeError, AttributeError):
                pass
            if two is None and test is None:
                continue
            n2 += two is not None
            nt += test is not None
            c.execute("UPDATE sims SET two_year_sharpe=?, test_sharpe=? "
                      "WHERE candidate_id=?", (two, test, r["candidate_id"]))
        c.commit()
        log.info("recency backfill: %d rows scanned, %d two_year_sharpe, "
                 "%d test_sharpe", len(rows), n2, nt)

    def _backfill_pyramid(self) -> None:
        """Populate the three payout columns from payloads already on disk.

        MATCHES_PYRAMID is present on 100% of OK simulations and has been since
        day one, so the whole accumulated history becomes usable at once --
        which is the point: the budget-weighted average multiplier over that
        history is the baseline the scoring tilt is measured against.

        Exact, not inferred. `multiplier` is copied as BRAIN reports it (the
        MINIMUM over the matched pyramids), and a row whose payload carries no
        MATCHES_PYRAMID is left NULL rather than defaulted to 1.0 -- a missing
        multiplier is unknown, not neutral.
        """
        c = self.conn()
        rows = c.execute(
            "SELECT candidate_id, checks FROM sims "
            "WHERE pyramid_multiplier IS NULL AND pyramid_names IS NULL "
            "AND checks IS NOT NULL").fetchall()
        n = 0
        for r in rows:
            try:
                pyr = next((ch for ch in json.loads(r["checks"] or "[]")
                            if ch.get("name") == "MATCHES_PYRAMID"), None)
            except (ValueError, TypeError, AttributeError):
                continue
            if not pyr:
                continue
            names = [p.get("name") for p in (pyr.get("pyramids") or [])
                     if p.get("name")]
            c.execute("UPDATE sims SET pyramid_multiplier=?, pyramid_names=?, "
                      "pyramid_effective=? WHERE candidate_id=?",
                      (pyr.get("multiplier"),
                       json.dumps(names) if names else None,
                       pyr.get("effective"), r["candidate_id"]))
            n += 1
        c.commit()
        log.info("pyramid backfill: %d rows scanned, %d populated", len(rows), n)

    def _backfill_is_ladder(self) -> None:
        """Populate the ladder-recency columns from payloads already on disk.

        Same rationale and shape as _backfill_recency(): IS_LADDER_SHARPE has
        been arriving in `checks` since day one and nothing read it. A row
        carries this OR two_year_sharpe, never both, so this is the other half
        of that same recency picture -- and per the live database, the larger
        half.
        """
        c = self.conn()
        rows = c.execute(
            "SELECT candidate_id, checks FROM sims "
            "WHERE is_ladder_sharpe IS NULL AND is_ladder_limit IS NULL "
            "AND checks IS NOT NULL").fetchall()
        n = 0
        for r in rows:
            try:
                ch = next((x for x in json.loads(r["checks"] or "[]")
                          if x.get("name") == "IS_LADDER_SHARPE"), None)
            except (ValueError, TypeError, AttributeError):
                continue
            if not ch or ch.get("value") is None:
                continue
            c.execute("UPDATE sims SET is_ladder_sharpe=?, is_ladder_limit=? "
                      "WHERE candidate_id=?",
                      (ch.get("value"), ch.get("limit"), r["candidate_id"]))
            n += 1
        c.commit()
        log.info("IS_LADDER_SHARPE backfill: %d rows scanned, %d populated",
                 len(rows), n)

    def _backfill_portability(self) -> None:
        """Score every already-'gated' champion against its own rung-5 result.

        Rung 5 (refine.py) re-simulates a champion on 1-2 tighter universes
        purely as a falsification test -- and until this existed, nothing ever
        read the result back onto the champion it was testing. This applies
        config.subuniverse_floor(), the SAME formula BRAIN's own
        LOW_SUB_UNIVERSE_SHARPE check approximates, to the REAL re-simulated
        Sharpe instead of an estimate: a formula estimate can pass (observed
        live: 1.33 vs a limit of 0.90) while the true tighter-universe Sharpe
        still collapses (0.68, then 0.17) -- exactly the case that motivated
        this column. One import from config.py, a deliberate, narrow exception
        to this module staying formula-free: the alternative was duplicating
        subuniverse_floor's constants here, which is worse.
        """
        from .config import subuniverse_floor
        c = self.conn()
        champions = c.execute(
            "SELECT c.id, c.settings, s.sharpe FROM candidates c "
            "JOIN sims s ON s.candidate_id=c.id "
            "WHERE c.stage='gated' AND s.portability_ratio IS NULL "
            "AND s.status='OK'").fetchall()
        n = 0
        for champ in champions:
            try:
                csettings = json.loads(champ["settings"])
            except (ValueError, TypeError):
                continue
            cuniv, region = csettings.get("universe"), csettings.get("region", "USA")
            csharpe = champ["sharpe"]
            if not cuniv or csharpe is None:
                continue
            children = c.execute(
                "SELECT ch.settings, s.sharpe FROM candidates ch "
                "JOIN sims s ON s.candidate_id=ch.id "
                "WHERE ch.parent_id=? AND s.status='OK'", (champ["id"],)).fetchall()
            worst = None
            for child in children:
                try:
                    universe = json.loads(child["settings"]).get("universe")
                except (ValueError, TypeError):
                    continue
                if not universe or universe == cuniv or child["sharpe"] is None:
                    continue
                floor = subuniverse_floor(csharpe, cuniv, region, universe)
                if floor <= 0:
                    continue
                ratio = child["sharpe"] / floor
                if worst is None or ratio < worst:
                    worst = ratio
            if worst is not None:
                c.execute("UPDATE sims SET portability_ratio=? WHERE candidate_id=?",
                          (worst, champ["id"]))
                n += 1
        c.commit()
        log.info("portability backfill: %d champion(s) scanned, %d scored",
                 len(champions), n)

    def _rebuild_deep_trials(self) -> None:
        """Recount the deep stage from gate_history, one trial per lineage.

        gate_history is the authoritative log: record_gate_check(..., deep=)
        runs at every site that computes a deep verdict. Before the ledger
        existed only `refine` taught the bandit, so verdicts computed by
        `select` and `gatelog` -- which cost real API calls -- were discarded.

        REBUILD rather than increment: whatever `refine` did manage to teach is
        already in these counters, and adding the full history on top would
        double-count it.

        The gate stage is deliberately NOT rebuilt. gate_history holds a
        quality_verdict for every simulation `gatelog` walks, including
        non-champions, so it is not a sample of the "lineages that finished a
        ladder" population that stage models. Gate only becomes idempotent from
        here on.
        """
        c = self.conn()
        rows = c.execute(
            "SELECT g.candidate_id, g.deep_ok, cn.theme, cn.template"
            "  FROM gate_history g JOIN candidates cn ON cn.id = g.candidate_id"
            " WHERE g.deep_ok IS NOT NULL ORDER BY g.ts").fetchall()
        by_root: dict[str, tuple[str, int]] = {}
        for r in rows:
            root = self.lineage_root(r["candidate_id"])
            arm = f"{r['theme']}::{r['template']}"
            # Last verdict wins: a re-check reflects the current book.
            by_root[root] = (arm, int(bool(r["deep_ok"])))
        c.execute("UPDATE arms SET deep_trials=0, deep_pass=0")
        c.execute("DELETE FROM bandit_taught WHERE stage='deep'")
        for root, (arm, ok) in by_root.items():
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (arm, time.time()))
            c.execute("UPDATE arms SET deep_trials=deep_trials+1,"
                      " deep_pass=deep_pass+? WHERE arm=?", (ok, arm))
            c.execute("INSERT OR IGNORE INTO bandit_taught VALUES (?,?,?,?)",
                      ("deep", root, arm, time.time()))
        c.commit()
        if by_root:
            log.info("rebuilt deep-stage evidence from gate_history: "
                     "%d lineage(s)", len(by_root))

    def _backfill_corr_arms(self) -> None:
        """Seed corr_trials/corr_pass from the /check results already on disk.

        DEDUPED BY LINEAGE ROOT, and that is the whole point. A ladder produces
        several sibling variants of one idea (rung 2/4/5 tweaks), all of which
        keep essentially the same PROD_CORRELATION -- counting them separately
        turns one win into five and one loss into five. That inflation is not
        hypothetical: reading the raw rows made a single winning lineage look
        like "8/10 for this template" when the honest number was 1/1.

        Matches the per-lineage discipline claim_bandit_trial() already applies
        to live teaching, so the backfilled history and anything taught from
        here on are counted the same way.
        """
        c = self.conn()
        rows = c.execute(
            "SELECT cd.id cid, cd.theme, cd.template, d.checks "
            "FROM deep_checks d "
            "JOIN sims s ON s.alpha_id=d.alpha_id "
            "JOIN candidates cd ON cd.id=s.candidate_id "
            "WHERE d.checks IS NOT NULL").fetchall()
        # A lineage counts ONCE, and counts as a pass if ANY of its measured
        # variants cleared the limit. Two of them genuinely disagree -- one
        # lineage has siblings at 0.641 and 0.883 -- so the rule matters and
        # "whichever row SQL happened to return first" is not a rule. Best-of
        # is the honest measure of what an ARM can produce: the ladder's job is
        # to search variants and keep the best, so if any variant cleared
        # crowding, that arm did produce a crowding-clearing alpha. (Whether
        # champion SELECTION then picks it is a separate problem, measured
        # separately -- _score has no visibility into correlation at all.)
        best: dict[tuple[str, str], bool] = {}
        for r in rows:
            if not r["theme"] or not r["template"]:
                continue
            try:
                checks = json.loads(r["checks"] or "[]")
            except (ValueError, TypeError):
                continue
            entry = next((x for x in checks
                          if x.get("name") == "PROD_CORRELATION"), None)
            value = (entry or {}).get("value")
            if value is None:
                continue
            limit = (entry or {}).get("limit") or 0.70
            key = (f"{r['theme']}::{r['template']}", self.lineage_root(r["cid"]))
            best[key] = best.get(key, False) or float(value) < float(limit)
        seen = set(best)
        tally: dict[str, list[int]] = {}
        for (arm, _root), won in best.items():
            t = tally.setdefault(arm, [0, 0])
            t[0] += 1
            t[1] += int(won)
        for arm, (trials, passes) in tally.items():
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (arm, time.time()))
            c.execute("UPDATE arms SET corr_trials=?, corr_pass=? WHERE arm=?",
                      (trials, passes, arm))
        c.commit()
        log.info("corr backfill: %d rows -> %d distinct (arm, lineage) pairs "
                 "across %d arms, %d passes",
                 len(rows), len(seen), len(tally),
                 sum(v[1] for v in tally.values()))

    def _backfill_arm_stages(self) -> None:
        """Split the old pooled `trials` counter into per-stage denominators.

        Only reachable once, immediately after the columns are created.

        `parent_id IS NULL` identifies a scan-generated candidate exactly:
        generator.py always inserts with no parent, and Refiner._queue always
        sets one. So the scan stage can be recomputed from the simulation
        record rather than guessed. The refine stage cannot -- the old schema
        never distinguished it -- so it takes the remainder, floored at the
        gate passes we know happened. That approximation applies to
        pre-migration history only; gate_history records the stages exactly
        from here on.
        """
        from .config import Gates
        floor = Gates().scan_abs_sharpe
        c = self.conn()
        rows = c.execute(
            "SELECT c.theme, c.template,"
            "       COUNT(*) n,"
            "       SUM(CASE WHEN s.status='OK' AND ABS(s.sharpe) >= ?"
            "                THEN 1 ELSE 0 END) passed"
            "  FROM sims s JOIN candidates c ON c.id = s.candidate_id"
            " WHERE c.parent_id IS NULL"
            " GROUP BY c.theme, c.template", (floor,)).fetchall()
        for r in rows:
            arm = f"{r['theme']}::{r['template']}"
            c.execute(
                "UPDATE arms SET scan_trials=?, scan_pass=?,"
                " refine_trials=MAX(trials - ?, gate_pass) WHERE arm=?",
                (r["n"], r["passed"] or 0, r["n"], arm))
        # Arms with no scan-origin sims at all still need a sane denominator.
        c.execute("UPDATE arms SET refine_trials=MAX(trials, gate_pass) "
                  "WHERE scan_trials=0 AND refine_trials=0")
        c.commit()

    def rebuild_arms_from_evidence(self) -> dict:
        """Recompute every arm counter from the rows that justify it.

        Written for the case where a field's inferred theme changes, which
        renames its arm -- `arm` is the text `theme::template` and nothing else.
        Renaming the rows in place cannot work: two old arms can collapse into
        one (sum the counters) while a third splits across two, and a split
        cannot be apportioned because the arm row does not record which
        candidate contributed which trial. Deriving instead of renaming makes
        both cases the same GROUP BY, and drops stranded arms for free.

        Reads `candidates.theme`, so update that FIRST.

        Not one transaction: the two reused backfills commit for themselves.
        What holds instead is idempotency -- every counter is an absolute value
        recomputed from primary evidence, so an interrupted run is repaired by
        running it again, not by unwinding it.
        """
        from .config import Gates
        floor = Gates().scan_abs_sharpe
        c = self.conn()
        before = c.execute("SELECT COUNT(*) FROM arms").fetchone()[0]
        c.execute("DELETE FROM arms")

        # Scan stage: parent_id IS NULL identifies a scan-generated candidate
        # exactly (generator never sets a parent, Refiner._queue always does).
        for r in c.execute(
                "SELECT c.theme, c.template, COUNT(*) n,"
                "       SUM(CASE WHEN s.status='OK' AND ABS(s.sharpe) >= ?"
                "                THEN 1 ELSE 0 END) passed,"
                "       SUM(CASE WHEN s.status='OK' THEN ABS(s.sharpe) ELSE 0 END) ss"
                "  FROM sims s JOIN candidates c ON c.id = s.candidate_id"
                " WHERE c.parent_id IS NULL AND c.theme IS NOT NULL"
                " GROUP BY c.theme, c.template", (floor,)).fetchall():
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (f"{r['theme']}::{r['template']}", time.time()))
            c.execute("UPDATE arms SET scan_trials=?, scan_pass=?, sum_sharpe=?"
                      " WHERE arm=?",
                      (r["n"], r["passed"] or 0, r["ss"] or 0.0,
                       f"{r['theme']}::{r['template']}"))

        # Gate stage, per lineage root, last verdict wins -- the same rule
        # _rebuild_deep_trials applies, because a re-check reflects the book as
        # it stands now rather than as it stood the first time.
        by_root: dict[str, tuple[str, int]] = {}
        for r in c.execute(
                "SELECT g.candidate_id, g.ok, cn.theme, cn.template"
                "  FROM gate_history g JOIN candidates cn ON cn.id = g.candidate_id"
                " WHERE g.ok IS NOT NULL ORDER BY g.ts").fetchall():
            by_root[self.lineage_root(r["candidate_id"])] = (
                f"{r['theme']}::{r['template']}", int(bool(r["ok"])))
        for root, (arm, ok) in by_root.items():
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (arm, time.time()))
            c.execute("UPDATE arms SET refine_trials=refine_trials+1,"
                      " gate_pass=gate_pass+? WHERE arm=?", (ok, arm))
            c.execute("UPDATE bandit_taught SET arm=? WHERE stage='gate' AND key=?",
                      (arm, root))

        for r in c.execute(
                "SELECT cd.theme, cd.template, COUNT(*) n FROM submissions sb"
                "  JOIN sims s ON s.alpha_id = sb.alpha_id"
                "  JOIN candidates cd ON cd.id = s.candidate_id"
                " WHERE cd.theme IS NOT NULL GROUP BY cd.theme, cd.template").fetchall():
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (f"{r['theme']}::{r['template']}", time.time()))
            c.execute("UPDATE arms SET submitted=? WHERE arm=?",
                      (r["n"], f"{r['theme']}::{r['template']}"))
        c.commit()

        # Both read candidates.theme at run time and write absolute values, so
        # they emit the NEW arm names unchanged. _backfill_arm_stages is
        # deliberately not reused: its `MAX(trials - ?, gate_pass)` arithmetic
        # assumes the legacy pooled `trials` column this method recomputes.
        self._backfill_corr_arms()
        self._rebuild_deep_trials()

        # `trials` is the cross-stage total, for reporting only (Bandit.update
        # bumps it alongside each stage; a submission is an outcome of an
        # existing trial, not a new one).
        c.execute("UPDATE arms SET trials = scan_trials + refine_trials"
                  " + corr_trials + deep_trials")
        c.commit()
        after = c.execute("SELECT COUNT(*) FROM arms").fetchone()[0]
        log.info("rebuilt arms from evidence: %d -> %d row(s)", before, after)
        return {"arms_before": before, "arms_after": after,
                "gate_lineages": len(by_root)}

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    # ---------------------------------------------------------- candidates
    def add_candidate(self, *, expr: str, settings: dict, theme: str = "",
                      template: str = "", field: str = "", stage: str = "scan",
                      parent_id: str | None = None, settings_key: str = "",
                      field2: str = "") -> str:
        key = settings_key or json.dumps(settings, sort_keys=True)
        _id = cid(expr, key)
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO candidates"
                "(id,expr,settings,theme,template,field,field2,stage,parent_id,"
                "created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_id, expr, json.dumps(settings), theme, template, field,
                 field2, stage, parent_id, time.time()))
        return _id

    def pending(self, stage: str = "scan", limit: int = 1000) -> list[sqlite3.Row]:
        """Candidates with no simulation record yet -- the work queue.

        Excludes anything currently in flight: those slots are already paid for
        and will be reclaimed rather than re-dispatched.
        """
        return self.conn().execute(
            "SELECT c.* FROM candidates c LEFT JOIN sims s ON s.candidate_id=c.id"
            " LEFT JOIN inflight f ON f.candidate_id=c.id"
            " WHERE c.stage=? AND s.candidate_id IS NULL AND f.candidate_id IS NULL"
            " ORDER BY c.created LIMIT ?", (stage, limit)).fetchall()

    def candidate(self, _id: str) -> sqlite3.Row | None:
        return self.conn().execute(
            "SELECT * FROM candidates WHERE id=?", (_id,)).fetchone()

    def already_simulated(self, expr: str, settings_key: str) -> bool:
        _id = cid(expr, settings_key)
        return self.conn().execute(
            "SELECT 1 FROM sims WHERE candidate_id=?", (_id,)).fetchone() is not None

    # ----------------------------------------------------------- inflight
    def mark_dispatched(self, candidate_id: str, progress_url: str,
                        child_index: int = 0) -> None:
        """Record the progress URL BEFORE polling, so a crash is recoverable.

        `child_index` is the candidate's position inside a multi-simulation
        bundle: BRAIN returns `children` in payload order, so the index is the
        only thing that maps a child result back to the expression that made it.
        """
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO inflight VALUES (?,?,?,?)",
                      (candidate_id, progress_url, child_index, time.time()))

    def inflight_bundles(self) -> dict[str, list[sqlite3.Row]]:
        """In-flight work grouped by bundle URL, each list in child order."""
        out: dict[str, list[sqlite3.Row]] = {}
        for r in self.inflight():
            out.setdefault(r["progress_url"], []).append(r)
        for url in out:
            out[url].sort(key=lambda r: r["child_index"])
        return out

    def clear_dispatched(self, candidate_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM inflight WHERE candidate_id=?", (candidate_id,))

    def inflight(self) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT * FROM inflight ORDER BY dispatched").fetchall()

    # --------------------------------------------------------------- sims
    def record_sim(self, candidate_id: str, rec: dict) -> None:
        cols = ("alpha_id status error sharpe fitness turnover returns drawdown "
                "margin longCount shortCount subuniverse_sharpe "
                "two_year_sharpe test_sharpe "
                "pyramid_multiplier pyramid_names pyramid_effective "
                "is_ladder_sharpe is_ladder_limit").split()
        # pyramid_names arrives from extract() as a list; every other column is
        # a scalar sqlite can bind directly.
        vals = [json.dumps(rec[k]) if k == "pyramid_names" and rec.get(k) is not None
                else rec.get(k) for k in cols]
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO sims (candidate_id," + ",".join(cols) +
                ",checks,raw,finished) VALUES (" + ",".join(["?"] * (len(cols) + 4)) + ")",
                [candidate_id, *vals, json.dumps(rec.get("checks") or []),
                 json.dumps(rec.get("raw") or {}), time.time()])

    def sims_where(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn().execute(
            "SELECT c.*, s.* FROM sims s JOIN candidates c ON c.id=s.candidate_id "
            "WHERE " + sql, tuple(args)).fetchall()

    # ------------------------------------------------- prod correlation shape
    def save_corr_profile(self, alpha_id: str, hist: Any, profile: dict) -> None:
        """Named columns, not positional: deep_checks' positional INSERT is
        exactly the fragility this table was split out to avoid."""
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO prod_corr_hist"
                "(alpha_id,hist,max_corr,min_corr,mean,sd,expected_max,"
                "n_alphas,fetched) VALUES (?,?,?,?,?,?,?,?,?)",
                (alpha_id, json.dumps(hist or {}), profile.get("max_corr"),
                 profile.get("min_corr"), profile.get("mean"),
                 profile.get("sd"), profile.get("expected_max"),
                 profile.get("n_alphas"), time.time()))

    def load_corr_profile(self, alpha_id: str) -> sqlite3.Row | None:
        return self.conn().execute(
            "SELECT * FROM prod_corr_hist WHERE alpha_id=?",
            (alpha_id,)).fetchone()

    # -------------------------------------------------------- submissions
    def record_submission(self, alpha_id: str, verdict: str,
                          final_status: str, detail: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO submissions VALUES (?,?,?,?,?)",
                      (alpha_id, time.time(), verdict, final_status,
                       json.dumps(detail)))

    def mark_submission_active(self, alpha_id: str) -> bool:
        """The platform lists this alpha ACTIVE: correct a row that says otherwise.
        Verdict and status only -- the dossier the pipeline recorded is kept."""
        with self.conn() as c:
            return c.execute(
                "UPDATE submissions SET verdict='ACCEPTED', final_status='ACTIVE' "
                "WHERE alpha_id=? AND final_status IS NOT 'ACTIVE'", (alpha_id,)).rowcount > 0

    def import_submission(self, alpha_id: str, submitted_at: float,
                          detail: dict) -> bool:
        """Record a submission made OUTSIDE this pipeline (the web UI, another
        machine). INSERT OR IGNORE, never REPLACE: a row this pipeline wrote itself
        carries the verdict and dossier it acted on, and must not be overwritten by
        a thinner platform-derived one. True if a row was added."""
        with self.conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO submissions VALUES (?,?,?,?,?)",
                            (alpha_id, submitted_at, "SYNCED", "ACTIVE",
                             json.dumps(detail)))
            return cur.rowcount == 1

    # --------------------------------------------------------------- arms
    def bump_arm(self, arm: str, *, trial=0, scan_trials=0, scan_pass=0,
                 refine_trials=0, gate_pass=0, deep_trials=0, deep_pass=0,
                 corr_trials=0, corr_pass=0,
                 submitted=0, sharpe=0.0) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (arm, time.time()))
            c.execute(
                "UPDATE arms SET trials=trials+?,"
                " scan_trials=scan_trials+?, scan_pass=scan_pass+?,"
                " refine_trials=refine_trials+?, gate_pass=gate_pass+?,"
                " deep_trials=deep_trials+?, deep_pass=deep_pass+?,"
                " corr_trials=corr_trials+?, corr_pass=corr_pass+?,"
                " submitted=submitted+?, sum_sharpe=sum_sharpe+?,"
                " updated=? WHERE arm=?",
                (trial, scan_trials, scan_pass, refine_trials, gate_pass,
                 deep_trials, deep_pass, corr_trials, corr_pass,
                 submitted, sharpe, time.time(), arm))

    def arms(self) -> list[sqlite3.Row]:
        return self.conn().execute("SELECT * FROM arms").fetchall()

    def neutralization_survival(self) -> dict[str, tuple[int, int]]:
        """(wins, measured) per neutralization, from checks already on disk.

        A "win" is a stored /check PROD_CORRELATION value under 0.70. Joins
        deep_checks -> sims (alpha_id) -> candidates (settings) -- the same
        shape cli._prod_corr_table already reads for reporting, just grouped
        by neutralization instead of left flat. No new table: deep_checks and
        candidates.settings have carried everything this needs since day one.
        """
        out: dict[str, tuple[int, int]] = {}
        rows = self.conn().execute(
            "SELECT cd.settings settings, d.checks checks FROM deep_checks d "
            "JOIN sims s ON s.alpha_id=d.alpha_id "
            "JOIN candidates cd ON cd.id=s.candidate_id").fetchall()
        for r in rows:
            try:
                pc = next((c.get("value") for c in json.loads(r["checks"] or "[]")
                          if c.get("name") == "PROD_CORRELATION"), None)
            except (ValueError, TypeError, AttributeError):
                continue
            if pc is None:
                continue
            try:
                neut = json.loads(r["settings"]).get("neutralization")
            except (ValueError, TypeError, AttributeError):
                continue
            if not neut:
                continue
            wins, trials = out.get(neut, (0, 0))
            out[neut] = (wins + (1 if pc < 0.70 else 0), trials + 1)
        return out

    # ------------------------------------------------------- bandit ledger
    def lineage_root(self, candidate_id: str) -> str:
        """Walk a candidate back to the scan candidate its ladder started from.

        The lineage is the bandit's unit of evidence: one scan candidate, one
        ladder, one verdict. An alpha id is the wrong key -- a single idea
        appears once per rung variant and once per re-ladder, so on real data 21
        deep-checked alphas were only 6 independent lineages.
        """
        seen: set[str] = set()
        cur = candidate_id
        while cur and cur not in seen:
            seen.add(cur)                      # also breaks a cyclic parent
            row = self.candidate(cur)
            if row is None or not row["parent_id"]:
                return cur
            cur = row["parent_id"]
        return cur

    def incomplete_lineage_roots(self) -> set[str]:
        """Roots of every ladder that has a refine variant with no simulation record
        and nothing in flight -- a rung whose champion was elected without it.

        Such variants exist when a record was removed (e.g. the capacity-error repair,
        scripts/repair_capacity_errors.py) or a run died between registering and
        dispatching. `refine --redo-incomplete` re-ladders exactly these.
        """
        rows = self.conn().execute(
            "SELECT c.id FROM candidates c"
            " LEFT JOIN sims s ON s.candidate_id=c.id"
            " LEFT JOIN inflight f ON f.candidate_id=c.id"
            " WHERE c.parent_id IS NOT NULL AND s.candidate_id IS NULL"
            "   AND f.candidate_id IS NULL").fetchall()
        return {self.lineage_root(r["id"]) for r in rows}

    def claim_bandit_trial(self, stage: str, key: str, arm: str = "") -> bool:
        """Claim the right to count this trial. True only for the first caller.

        Lets every site that computes a verdict teach the bandit without any of
        them having to know whether another already did. `INSERT OR IGNORE` on
        the primary key makes the claim atomic, so concurrent processes cannot
        both win it.
        """
        if not key:
            return False
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO bandit_taught(stage,key,arm,ts) "
                "VALUES (?,?,?,?)", (stage, key, arm, time.time()))
            return cur.rowcount > 0

    # --------------------------------------------------------- deep_checks
    def save_deep(self, alpha_id: str, *, yearly: Any = None,
                  checks: Any = None, ok: bool = False,
                  reasons: Iterable[str] = ()) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO deep_checks VALUES (?,?,?,?,?,?)",
                      (alpha_id, json.dumps(yearly or {}),
                       json.dumps(checks or []), int(bool(ok)),
                       json.dumps(list(reasons)), time.time()))

    def load_deep(self, alpha_id: str, ttl_hours: float = 24.0) -> dict | None:
        """Cached deep verdict, or None when absent.

        `yearly` is returned even past the TTL -- in-sample yearly stats do not
        change once BRAIN has computed them. `checks` is dropped on expiry
        because PROD_CORRELATION moves with your book and the production pool,
        so a stale value must never masquerade as current evidence.
        """
        row = self.conn().execute(
            "SELECT * FROM deep_checks WHERE alpha_id=?", (alpha_id,)).fetchone()
        if not row:
            return None
        fresh = (time.time() - (row["fetched"] or 0.0)) < ttl_hours * 3600.0
        return {"alpha_id": alpha_id,
                "yearly": json.loads(row["yearly"] or "{}"),
                "checks": json.loads(row["checks"] or "[]") if fresh else None,
                "ok": bool(row["ok"]) if fresh else None,
                "reasons": json.loads(row["reasons"] or "[]"),
                "fresh": fresh, "fetched": row["fetched"]}

    # -------------------------------------------------------- gate_history
    def record_gate_check(self, candidate_id: str, alpha_id: str | None,
                          expr: str, rec: dict, verdict, gates,
                          deep=None) -> None:
        """Snapshot one quality_verdict() outcome, pass or fail.

        Called every time refine or select computes a verdict, so there is an
        independent trail even for candidates that never make it into a
        submitted (or even dry-run) basket.

        `deep` is the optional deeper Verdict (yearly consistency +
        PROD_CORRELATION). Left None when it was not computed -- which is
        recorded as "unknown", never as a pass.
        """
        with self.conn() as c:
            c.execute(
                "INSERT INTO gate_history (ts,candidate_id,alpha_id,expr,"
                "sharpe,fitness,turnover,ok,reasons,gate_sharpe,gate_fitness,"
                "gate_turnover_max,gate_subuniverse_ratio,gate_max_drawdown,"
                "gate_max_self_corr,gate_max_prod_corr,deep_ok,deep_reasons)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), candidate_id, alpha_id, expr,
                 rec.get("sharpe"), rec.get("fitness"), rec.get("turnover"),
                 1 if verdict.ok else 0, json.dumps(verdict.reasons),
                 gates.sharpe, gates.fitness, gates.turnover_max,
                 gates.subuniverse_ratio, gates.max_drawdown,
                 gates.max_self_corr, gates.max_prod_corr,
                 None if deep is None else int(bool(deep.ok)),
                 None if deep is None else json.dumps(deep.reasons)))

    def gate_history(self, candidate_id: str | None = None,
                     limit: int = 200) -> list[sqlite3.Row]:
        if candidate_id:
            return self.conn().execute(
                "SELECT * FROM gate_history WHERE candidate_id=? "
                "ORDER BY ts DESC LIMIT ?", (candidate_id, limit)).fetchall()
        return self.conn().execute(
            "SELECT * FROM gate_history ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()

    def gate_passing_now(self, limit: int = 200) -> list[sqlite3.Row]:
        """Most recent snapshot per DISTINCT EXPRESSION, restricted to ones
        that were submit-grade AT THE TIME of their most recent check.

        Grouped by `expr`, not `candidate_id`: refine's coordinate ascent
        re-simulates the current champion at rungs where nothing improved
        ("best score +0.000"), which mints a fresh candidate_id and alpha_id
        for what is, economically, the same idea simulated again. Grouping by
        candidate_id would show that as N submittable alphas instead of one.

        This method itself is a plain read -- it reports "was submit-grade
        when last checked", not a live re-evaluation. `cli.cmd_gatelog
        --passing` is what makes the answer trustworthy: it re-runs
        quality_verdict() against the CURRENT Gates for every recorded
        simulation and logs a fresh snapshot immediately before calling this,
        so by the time these rows are read they are current, not stale.

        Requires deep_ok=1, not merely "not failed": a NULL means the deep
        verdict was never computed, and an unverified alpha must not be
        reported as submittable -- that was exactly the failure mode where
        three candidates looked ready and submit blocked all three.

        The query itself lives in GATE_PASSING_NOW_SQL, a module constant
        rather than an inline string, so alphamachine.audit can re-run this
        exact same single source of truth against its own read-only
        connection and diff it against an independent recomputation --
        without duplicating the SQL or instantiating a second Store.
        """
        return self.conn().execute(GATE_PASSING_NOW_SQL, (limit,)).fetchall()

    # --------------------------------------------------------- dead fields
    def add_dead_field(self, region: str, universe: str, delay: int,
                       field: str, error: str = "") -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO dead_fields VALUES (?,?,?,?,?,?)",
                      (region, universe, int(delay), field, error[:200], time.time()))

    def dead_fields(self, region: str, universe: str, delay: int) -> set[str]:
        return {r["field"] for r in self.conn().execute(
            "SELECT field FROM dead_fields WHERE region=? AND universe=? AND delay=?",
            (region, universe, int(delay)))}

    # ----------------------------------------------------------------- kv
    def get(self, k: str, default=None):
        row = self.conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, k: str, v: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
