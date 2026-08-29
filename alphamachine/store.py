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
    stage         TEXT DEFAULT 'scan',   -- scan | refine | gated
    parent_id     TEXT,
    created       REAL
);
CREATE INDEX IF NOT EXISTS idx_cand_stage ON candidates(stage);

CREATE TABLE IF NOT EXISTS sims (
    candidate_id  TEXT PRIMARY KEY,
    alpha_id      TEXT,
    status        TEXT,                  -- OK | ERROR | TIMEOUT
    error         TEXT,
    sharpe        REAL, fitness REAL, turnover REAL, returns REAL,
    drawdown      REAL, margin REAL,
    longCount     INTEGER, shortCount INTEGER,
    subuniverse_sharpe REAL,
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

CREATE TABLE IF NOT EXISTS pnl (
    alpha_id      TEXT PRIMARY KEY,
    dates         TEXT,                  -- json list of ISO dates
    vals          TEXT,                  -- json list of cumulative pnl
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
CREATE TABLE IF NOT EXISTS arms (
    arm           TEXT PRIMARY KEY,      -- "theme::template"
    trials        INTEGER DEFAULT 0,     -- reporting total across all stages
    scan_trials   INTEGER DEFAULT 0,
    scan_pass     INTEGER DEFAULT 0,
    refine_trials INTEGER DEFAULT 0,
    gate_pass     INTEGER DEFAULT 0,
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
                 ("deep_pass", "INTEGER DEFAULT 0")],
        "gate_history": [("deep_ok", "INTEGER"),
                         ("deep_reasons", "TEXT")],
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
        # A database that already had gate_history but not the ledger has deep
        # verdicts on file that were never counted -- rebuild them once.
        pre = pre_existing or set()
        if "gate_history" in pre and "bandit_taught" not in pre:
            self._rebuild_deep_trials()

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
                      parent_id: str | None = None, settings_key: str = "") -> str:
        key = settings_key or json.dumps(settings, sort_keys=True)
        _id = cid(expr, key)
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO candidates"
                "(id,expr,settings,theme,template,field,stage,parent_id,created)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (_id, expr, json.dumps(settings), theme, template, field,
                 stage, parent_id, time.time()))
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
                "margin longCount shortCount subuniverse_sharpe").split()
        vals = [rec.get(k) for k in cols]
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

    # ---------------------------------------------------------------- pnl
    def save_pnl(self, alpha_id: str, dates: list[str], values: list[float]) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO pnl VALUES (?,?,?,?)",
                      (alpha_id, json.dumps([str(d) for d in dates]),
                       json.dumps([float(v) for v in values]), time.time()))

    def load_pnl(self, alpha_id: str) -> tuple[list[str], list[float]] | None:
        row = self.conn().execute(
            "SELECT dates,vals FROM pnl WHERE alpha_id=?", (alpha_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["dates"]), json.loads(row["vals"])

    def all_pnl(self) -> dict[str, tuple[list[str], list[float]]]:
        out = {}
        for r in self.conn().execute("SELECT alpha_id,dates,vals FROM pnl"):
            out[r["alpha_id"]] = (json.loads(r["dates"]), json.loads(r["vals"]))
        return out

    # -------------------------------------------------------- submissions
    def record_submission(self, alpha_id: str, verdict: str,
                          final_status: str, detail: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO submissions VALUES (?,?,?,?,?)",
                      (alpha_id, time.time(), verdict, final_status,
                       json.dumps(detail)))

    def submits_since(self, epoch: float) -> int:
        return self.conn().execute(
            "SELECT COUNT(*) n FROM submissions WHERE submitted_at>=? "
            "AND final_status='ACTIVE'", (epoch,)).fetchone()["n"]

    # --------------------------------------------------------------- arms
    def bump_arm(self, arm: str, *, trial=0, scan_trials=0, scan_pass=0,
                 refine_trials=0, gate_pass=0, deep_trials=0, deep_pass=0,
                 submitted=0, sharpe=0.0) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO arms(arm,updated) VALUES (?,?)",
                      (arm, time.time()))
            c.execute(
                "UPDATE arms SET trials=trials+?,"
                " scan_trials=scan_trials+?, scan_pass=scan_pass+?,"
                " refine_trials=refine_trials+?, gate_pass=gate_pass+?,"
                " deep_trials=deep_trials+?, deep_pass=deep_pass+?,"
                " submitted=submitted+?, sum_sharpe=sum_sharpe+?,"
                " updated=? WHERE arm=?",
                (trial, scan_trials, scan_pass, refine_trials, gate_pass,
                 deep_trials, deep_pass, submitted, sharpe, time.time(), arm))

    def arms(self) -> list[sqlite3.Row]:
        return self.conn().execute("SELECT * FROM arms").fetchall()

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
                "fresh": fresh}

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
        """
        return self.conn().execute(
            "SELECT g.* FROM gate_history g JOIN "
            "(SELECT expr, MAX(ts) mts FROM gate_history "
            " WHERE ok=1 AND deep_ok=1 GROUP BY expr) m "
            "ON m.expr=g.expr AND m.mts=g.ts "
            "WHERE g.ok=1 AND g.deep_ok=1 "
            "ORDER BY g.sharpe DESC LIMIT ?", (limit,)).fetchall()

    # ----------------------------------------------------------------- kv
    def get(self, k: str, default=None):
        row = self.conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, k: str, v: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
