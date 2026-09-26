"""Concurrent simulation dispatcher, built around multi-simulation bundles.

Throughput arithmetic
---------------------
A slot holds one *request*, not one alpha. BRAIN accepts a JSON **list** of up
to 10 payloads on POST /simulations, and that whole bundle occupies a single
slot. So a 3-slot account running singles has 3 alphas in flight; the same
account bundling by 10 has 30. That 10x is free -- it is the difference between
scanning 300 candidates in an evening and scanning 300 overnight.

Rules of engagement
-------------------
* Concurrency == your real slot count. Above it you only build a server-side
  queue and lose the ability to react to results.
* Poll on the server's own `Retry-After`. It sends a positive value while the
  job runs and `0` when it is done; obeying it is both correct and the reason
  we never need a hand-tuned poll interval that could breach the rate budget.
* Each thread gets its own BrainClient (cookie jars are not thread-safe).
* Results land in SQLite as they arrive, never batched at the end.
* The progress URL is persisted BEFORE the first poll: BRAIN charges the slot
  the instant POST returns, so a crash mid-poll must be recoverable.
* On shutdown, unfinished bundles are DELETEd to hand the slots straight back.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import requests

from .client import (AuthError, BiometricRequired, BrainClient, BrainError,
                     DailyLimitReached, SimulationCancelled, SimulationFailed,
                     SlotsBusy, is_transient_text)
from .config import RunConfig
from .metrics import extract

log = logging.getLogger("alphamachine.executor")

MULTI_SIM_SIZE = 10          # BRAIN's per-bundle ceiling


class Executor:
    def __init__(self, cfg: RunConfig, store, multi: bool = True):
        self.cfg = cfg
        self.store = store
        self.multi = multi
        self._tls = threading.local()
        self._lock = threading.Lock()
        # Tripped when BRAIN demands a persona check mid-run. An unattended
        # process cannot answer a face scan, so the only sane response is to
        # stop dispatching and leave the queue intact -- NOT to keep going and
        # record hundreds of bogus ERROR rows that permanently block retries.
        self._halt = threading.Event()
        # The platform's daily simulation cap is spent: nothing more can run today.
        # Distinct from _halt (a dead session) so autoloop, which reads the word
        # "HALTED" as "wait for a human to re-authenticate", is not sent to wait.
        self._daily = threading.Event()
        self._open_bundles: set[str] = set()

    def daily_limit_reached(self) -> bool:
        """Did the platform's daily simulation cap stop the last run?"""
        return self._daily.is_set()

    def halted(self) -> bool:
        """Did BRAIN demand a persona check during the last run?

        A caller that dispatches in several rounds needs this: `run()` reports
        the halt for its own batch, but only the caller can decide to stop
        queueing further work rather than march the whole plan into a wall.
        """
        return self._halt.is_set()

    def client(self) -> BrainClient:
        c = getattr(self._tls, "client", None)
        if c is None:
            c = BrainClient(self.cfg.email, self.cfg.password)
            c.authenticate()
            self._tls.client = c
        return c

    # ------------------------------------------------------------------ run
    def run(self, candidate_ids: list[str],
            on_result: Callable[[str, dict], None] | None = None,
            _retry_cancelled: bool = True) -> dict:
        stats = {"ok": 0, "error": 0, "skipped": 0, "deferred": 0, "cancelled": 0}
        cancelled: list[str] = []
        if not candidate_ids:
            return stats
        self._halt.clear()

        size = MULTI_SIM_SIZE if self.multi else 1
        bundles = [candidate_ids[i:i + size]
                   for i in range(0, len(candidate_ids), size)]
        log.info("%d candidates in %d bundle(s) of up to %d, %d slot(s)",
                 len(candidate_ids), len(bundles), size, self.cfg.concurrency)

        t0 = time.time()
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
                futures = {pool.submit(self._bundle, b): b for b in bundles}
                for fut in as_completed(futures):
                    batch = futures[fut]
                    try:
                        recs = fut.result()
                    except Exception as e:
                        log.warning("bundle crashed: %s", e)
                        if isinstance(e, AuthError):
                            self._halt.set()              # the session is dead
                            stats["skipped"] += len(batch)
                            continue
                        if isinstance(e, requests.RequestException) or is_transient_text(str(e)):
                            stats["deferred"] += len(batch)   # network / rate limit: no verdict
                            continue
                        for cid in batch:
                            self.store.record_sim(
                                cid, {"status": "ERROR", "error": str(e)[:400]})
                            self.store.clear_dispatched(cid)
                        stats["error"] += len(batch)
                        continue
                    for cid, rec in recs.items():
                        st = rec.get("status")
                        if st == "HALTED":
                            stats["skipped"] += 1
                            continue
                        if st == "DEFERRED":            # never simulated: no verdict, no trial
                            stats["deferred"] += 1
                            continue
                        if st == "CANCELLED":           # a sibling failed; never simulated
                            stats["cancelled"] += 1
                            cancelled.append(cid)
                            continue
                        stats["ok" if st == "OK" else "error"] += 1
                        if on_result:
                            try:
                                on_result(cid, rec)
                            except Exception as e:
                                log.warning("on_result failed for %s: %s", cid, e)
                    done += len(batch)
                    if self._halt.is_set() and not stats.get("_warned"):
                        stats["_warned"] = True
                        log.error("HALTED: BRAIN requires a biometric check. "
                                  "Remaining candidates stay queued -- run "
                                  "scripts/auth_wait.py, then re-run this command.")
                    rate = done / max(time.time() - t0, 1e-9) * 3600
                    log.info("%d/%d done (%.0f alphas/hr) ok=%d err=%d",
                             done, len(candidate_ids), rate,
                             stats["ok"], stats["error"])
        finally:
            self._release_open_bundles()
        if self._daily.is_set():
            log.error("DAILY_SIMULATION_LIMIT_EXCEEDED: the platform's daily cap is spent. "
                      "Remaining candidates stay queued; try again after the daily reset.")
        if cancelled:
            # They were only collateral of a failing sibling, so one more bundle of
            # just them should run clean. Once: a second cancellation stays pending
            # (its marker is already cleared) rather than looping.
            if (_retry_cancelled and not self._halt.is_set()
                    and not self._daily.is_set()):
                log.warning("%d candidate(s) cancelled by BRAIN because a sibling in "
                            "their bundle failed -- re-dispatching them once",
                            len(cancelled))
                again = self.run(cancelled, on_result, _retry_cancelled=False)
                stats["cancelled"] = again["cancelled"]
                for k in ("ok", "error", "skipped", "deferred"):
                    stats[k] += again[k]
            else:
                log.warning("%d candidate(s) cancelled by BRAIN; left pending",
                            len(cancelled))
        return stats

    # -------------------------------------------------------------- bundle
    def _bundle(self, cids: list[str]) -> dict[str, dict]:
        """Simulate one bundle of candidates. Returns {candidate_id: record}."""
        if self._halt.is_set():
            return {cid: {"status": "HALTED"} for cid in cids}
        if self._daily.is_set():
            return {cid: {"status": "DEFERRED"} for cid in cids}

        rows = [self.store.candidate(cid) for cid in cids]
        live = [(cid, r) for cid, r in zip(cids, rows) if r is not None]
        # Record the misses now. Dropping them from the result dict leaves them
        # pending forever, so every subsequent run re-dispatches the same dead
        # ids and the queue never drains.
        missing = {}
        for cid, r in zip(cids, rows):
            if r is None:
                missing[cid] = self._fail(cid, "unknown candidate id")
        if not live:
            return missing

        try:
            c = self.client()
            # Refresh between units of work, while the session is still valid.
            # Waiting for a 401 mid-poll means losing an in-flight bundle.
            c.refresh_if_stale()
        except AuthError:                                   # BiometricRequired is one
            self._halt.set()
            return {**missing, **{cid: {"status": "HALTED"} for cid, _ in live}}

        payloads = [{"type": "REGULAR",
                     "settings": json.loads(r["settings"]),
                     "regular": r["expr"]} for _cid, r in live]
        single = len(payloads) == 1

        try:
            loc = c.simulate(payloads[0] if single else payloads)
        except AuthError:
            self._halt.set()
            return {cid: {"status": "HALTED"} for cid in cids}
        except DailyLimitReached as e:
            self._daily.set()                               # stop dispatching for the day
            log.warning("deferring %d candidate(s): %s", len(live), e)
            return {**missing, **{cid: {"status": "DEFERRED"} for cid, _ in live}}
        except requests.RequestException as e:
            log.warning("deferring %d candidate(s): network error before dispatch: %s", len(live), e)
            return {**missing, **{cid: {"status": "DEFERRED"} for cid, _ in live}}
        except SlotsBusy as e:
            # Nothing was simulated, so there is nothing to record: writing an ERROR
            # here is what poisoned 21k candidates. They stay pending for the next run.
            log.warning("deferring %d candidate(s): %s", len(live), e)
            return {**missing, **{cid: {"status": "DEFERRED"} for cid, _ in live}}
        except SimulationFailed as e:
            out = dict(missing)
            for cid, _r in live:
                rec = {"status": "ERROR", "error": str(e)[:400]}
                self.store.record_sim(cid, rec)
                out[cid] = rec
            return out

        # The slot is spent the instant simulate() returns. Persist the mapping
        # before polling so a crash here is recoverable rather than re-paid.
        for idx, (cid, _r) in enumerate(live):
            self.store.mark_dispatched(cid, loc, idx)
        with self._lock:
            self._open_bundles.add(loc)

        try:
            body = c.poll(loc, interval=self.cfg.poll_interval,
                          timeout=self.cfg.sim_timeout)
        except AuthError:
            self._halt.set()             # dispatched: the in-flight marker stays, reclaim() finds it
            return {cid: {"status": "HALTED"} for cid in cids}
        except SimulationCancelled:
            # The whole job was cancelled (a single sim, or a bundle with no
            # children listed). Same as a cancelled child: no verdict, marker gone.
            for cid, _r in live:
                self.store.clear_dispatched(cid)
            with self._lock:
                self._open_bundles.discard(loc)
            return {**missing, **{cid: {"status": "CANCELLED"} for cid, _ in live}}
        except SimulationFailed as e:
            if is_transient_text(str(e)):
                # e.g. the poll deadline: the job may still finish server-side, and it is
                # NOT a verdict on the candidate. Leave the in-flight marker (reclaim()
                # harvests or clears it next run) and keep the bundle in _open_bundles so
                # the end of the run hands its slot back.
                log.warning("deferring %d candidate(s): %s", len(live), e)
                return {**missing, **{cid: {"status": "DEFERRED"} for cid, _ in live}}
            out = dict(missing)
            for cid, _r in live:
                rec = {"status": "ERROR", "error": str(e)[:400]}
                self.store.record_sim(cid, rec)
                self.store.clear_dispatched(cid)
                out[cid] = rec
            with self._lock:
                self._open_bundles.discard(loc)
            return out

        with self._lock:
            self._open_bundles.discard(loc)
        return {**missing, **self._harvest(c, body, live)}

    def _harvest(self, c: BrainClient, body: dict,
                 live: list[tuple[str, object]],
                 deadline: float | None = None) -> dict[str, dict]:
        """Turn a resolved progress payload into per-candidate records.

        `children` come back in payload order, which is the only thing tying a
        child result to the expression that produced it.

        `deadline`: children are fetched ONE AT A TIME (`child_result`'s own
        timeout is 300s), so a single bundle with several backend-degraded
        children can itself run well past any budget the caller enforces only
        between bundles -- confirmed live 2026-09-24, one 7-child bundle alone
        cost 20+ minutes this way. Checked before EACH child (not just once per
        bundle): a child not yet started when the deadline passes is left
        completely alone (no entry in the returned dict, so its marker and any
        earlier candidates' results in this same bundle are untouched) rather
        than skipped after being counted as a failure.
        """
        out: dict[str, dict] = {}
        children = body.get("children")
        if children:
            for idx, (cid, _r) in enumerate(live):
                if deadline is not None and time.time() >= deadline:
                    break
                if idx >= len(children):
                    out[cid] = self._fail(cid, "bundle returned fewer children "
                                               "than payloads")
                    continue
                try:
                    child = c.child_result(str(children[idx]).rsplit("/", 1)[-1])
                except SimulationCancelled:
                    # Never simulated: no record, and the marker goes -- the job is
                    # over, so keeping it would have reclaim() re-poll it forever.
                    self.store.clear_dispatched(cid)
                    out[cid] = {"status": "CANCELLED"}
                    continue
                except SimulationFailed as e:
                    if is_transient_text(str(e)):
                        out[cid] = {"status": "DEFERRED"}   # marker stays: the sim is paid for
                        continue
                    # This is the useful message: the child's own reason, e.g.
                    # a FASTEXPR parse error naming the offending character.
                    out[cid] = self._fail(cid, str(e))
                    continue
                except AuthError:
                    self._halt.set()                        # session died mid-harvest
                    out[cid] = {"status": "HALTED"}         # marker stays: reclaim() retries
                    continue
                except BrainError as e:
                    if is_transient_text(str(e)):
                        out[cid] = {"status": "DEFERRED"}
                        continue
                    out[cid] = self._fail(cid, f"child fetch failed: {e}")
                    continue
                if (child.get("status") in ("ERROR", "FAIL")
                        or child.get("message")) and not _alpha_id(child):
                    out[cid] = self._fail(
                        cid, child.get("message") or f"child failed: {child.get('status')}")
                    continue
                alpha_id = _alpha_id(child)
                out[cid] = (self._record(c, cid, alpha_id) if alpha_id
                            else self._fail(cid, f"no alpha id: {str(child)[:160]}"))
        else:
            cid = live[0][0]
            alpha_id = _alpha_id(body)
            out[cid] = (self._record(c, cid, alpha_id) if alpha_id
                        else self._fail(cid, f"no alpha id: {str(body)[:160]}"))
        return out

    def _record(self, c: BrainClient, cid: str, alpha_id: str) -> dict:
        rec = extract(c.alpha(alpha_id))
        rec["status"] = "OK"
        rec["alpha_id"] = alpha_id
        self.store.record_sim(cid, rec)
        self.store.clear_dispatched(cid)
        return rec

    def _fail(self, cid: str, msg: str) -> dict:
        rec = {"status": "ERROR", "error": msg[:400]}
        self.store.record_sim(cid, rec)
        self.store.clear_dispatched(cid)
        return rec

    def _release_open_bundles(self) -> None:
        """Hand unfinished slots back rather than orphaning them."""
        with self._lock:
            urls = list(self._open_bundles)
            self._open_bundles.clear()
        if not urls:
            return
        # Only ever use a client this thread ALREADY has. Calling client() here
        # could authenticate from scratch, and doing that inside a shutdown
        # path can spend a biometric quota unit just to tidy up.
        c = getattr(self._tls, "client", None)
        if c is None:
            log.info("%d bundle(s) left running -- no live session to cancel "
                     "them with; they will be reclaimed on the next run", len(urls))
            return
        for u in urls:
            if c.cancel_simulation(u):
                log.info("released slot: %s", u)

    # ---------------------------------------------------------- reclaiming
    def _reclaim_bundle(self, url: str, rows: list, deadline: float | None) -> dict:
        """One bundle's worth of `reclaim()`, callable from a worker thread."""
        out = {"reclaimed": 0, "still_running": 0, "lost": 0, "deferred_by_budget": 0}
        if deadline is not None and time.time() >= deadline:
            out["deferred_by_budget"] += len(rows)
            return out
        live = []
        for r in rows:
            cand = self.store.candidate(r["candidate_id"])
            if cand is not None:
                live.append((r["candidate_id"], cand))
        if not live:
            for r in rows:
                self.store.clear_dispatched(r["candidate_id"])
            return out
        try:
            c = self.client()
        except AuthError:
            # Confirmed live 2026-09-24: with several worker threads each
            # lazily authenticating for the first time, one hits a real
            # persona challenge and the rest see the process-wide auth pause
            # and raise here too. A dead session is not evidence the job is
            # lost -- keep the marker for the next reclaim(), same as _bundle().
            self._halt.set()
            out["still_running"] += len(live)
            return out
        try:
            body = c.poll(url, interval=self.cfg.poll_interval, timeout=90.0)
        except AuthError:
            self._halt.set()
            out["still_running"] += len(live)
            return out
        except SimulationFailed as e:
            if "timeout" in str(e).lower():
                out["still_running"] += len(live)   # keep the marker
                return out
            for cid, _ in live:
                self.store.clear_dispatched(cid)
            out["lost"] += len(live)
            return out
        except BrainError:
            for cid, _ in live:
                self.store.clear_dispatched(cid)
            out["lost"] += len(live)
            return out
        recs = self._harvest(c, body, live, deadline=deadline)
        out["deferred_by_budget"] += len(live) - len(recs)
        for rec in recs.values():
            status = rec.get("status")
            if status == "DEFERRED":
                out["still_running"] += 1
            else:
                out["reclaimed" if status == "OK" else "lost"] += 1
        return out

    def reclaim(self, budget_seconds: float | None = None) -> dict:
        """Harvest simulations BRAIN ran while we were not listening.

        A crash mid-poll leaves the slot already paid for; BRAIN finishes the
        job regardless. Polling the stored progress URL turns that sunk cost
        back into a result instead of paying for the same expression twice.

        Bundles are polled on the SAME thread pool `run()` uses for dispatch
        (`cfg.concurrency` workers), not one at a time. A single-threaded loop
        here means a backlog of N bundles each costing up to the ~90s poll
        timeout takes N * 90s of WALL CLOCK no matter how many simulation
        slots the account actually has -- confirmed live 2026-09-24: 83
        leftover bundles, mostly genuinely still running, drained at roughly
        one every 90s and the budget below was too small to ever reach most
        of them. Concurrency turns that into ceil(N / concurrency) * 90s.

        `budget_seconds` caps total WALL-CLOCK time, not bundle count: each bundle
        can itself cost up to ~90s (its own poll) plus, per child, up to
        `child_result`'s 300s timeout if BRAIN's backend is slow to answer that
        specific record. With no cap, a handful of backend-degraded bundles left
        over from an interrupted run can block dispatch of thousands of unrelated,
        perfectly healthy pending candidates for over an hour -- confirmed live
        2026-09-24. A bundle not yet STARTED when the budget runs out is left
        completely alone (its marker untouched, tried again on the next reclaim);
        one already being polled is allowed to finish rather than cut off mid-poll,
        so the budget is a floor on responsiveness, not a hard deadline. Under
        concurrency this is enforced per-worker: a bundle a free worker has not
        yet picked up when the deadline passes is deferred without ever polling.
        """
        bundles = self.store.inflight_bundles()
        out = {"reclaimed": 0, "still_running": 0, "lost": 0, "deferred_by_budget": 0}
        if not bundles:
            return out
        log.info("reclaiming %d in-flight bundle(s) from a previous run",
                 len(bundles))
        deadline = None if budget_seconds is None else time.time() + budget_seconds
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            futures = {pool.submit(self._reclaim_bundle, url, rows, deadline): rows
                       for url, rows in bundles.items()}
            for fut in as_completed(futures):
                try:
                    counts = fut.result()
                except Exception as e:
                    # _reclaim_bundle already turns every anticipated failure (auth,
                    # transient, genuine) into a return value, never a raise -- this
                    # is a safety net against a bug in ours, same spirit as run()'s
                    # crash path. Leave the marker; the next reclaim() retries it.
                    log.warning("reclaim: bundle crashed: %s", e)
                    out["deferred_by_budget"] += len(futures[fut])
                    continue
                for k, v in counts.items():
                    out[k] += v
        if out["deferred_by_budget"]:
            log.info("reclaim: time budget spent -- %d candidate(s) across the "
                     "remaining bundle(s) left untouched, retried next time",
                     out["deferred_by_budget"])
        log.info("reclaim: %d recovered, %d still running, %d unrecoverable",
                 out["reclaimed"], out["still_running"], out["lost"])
        return out


def _alpha_id(body: dict) -> str | None:
    a = (body or {}).get("alpha")
    if isinstance(a, str):
        return a.rsplit("/", 1)[-1]
    if isinstance(a, dict):
        return a.get("id")
    return None
