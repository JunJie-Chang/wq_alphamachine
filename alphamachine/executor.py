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

from .client import (BiometricRequired, BrainClient, BrainError,
                     SimulationFailed)
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
        self._open_bundles: set[str] = set()

    def client(self) -> BrainClient:
        c = getattr(self._tls, "client", None)
        if c is None:
            c = BrainClient(self.cfg.email, self.cfg.password)
            c.authenticate()
            self._tls.client = c
        return c

    # ------------------------------------------------------------------ run
    def run(self, candidate_ids: list[str],
            on_result: Callable[[str, dict], None] | None = None) -> dict:
        stats = {"ok": 0, "error": 0, "skipped": 0}
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
        return stats

    # -------------------------------------------------------------- bundle
    def _bundle(self, cids: list[str]) -> dict[str, dict]:
        """Simulate one bundle of candidates. Returns {candidate_id: record}."""
        if self._halt.is_set():
            return {cid: {"status": "HALTED"} for cid in cids}

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
        except BiometricRequired:
            self._halt.set()
            return {**missing, **{cid: {"status": "HALTED"} for cid, _ in live}}

        payloads = [{"type": "REGULAR",
                     "settings": json.loads(r["settings"]),
                     "regular": r["expr"]} for _cid, r in live]
        single = len(payloads) == 1

        try:
            loc = c.simulate(payloads[0] if single else payloads)
        except BiometricRequired:
            self._halt.set()
            return {cid: {"status": "HALTED"} for cid in cids}
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
        except BiometricRequired:
            self._halt.set()
            return {cid: {"status": "HALTED"} for cid in cids}
        except SimulationFailed as e:
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
                 live: list[tuple[str, object]]) -> dict[str, dict]:
        """Turn a resolved progress payload into per-candidate records.

        `children` come back in payload order, which is the only thing tying a
        child result to the expression that produced it.
        """
        out: dict[str, dict] = {}
        children = body.get("children")
        if children:
            for idx, (cid, _r) in enumerate(live):
                if idx >= len(children):
                    out[cid] = self._fail(cid, "bundle returned fewer children "
                                               "than payloads")
                    continue
                try:
                    child = c.child_result(str(children[idx]).rsplit("/", 1)[-1])
                except SimulationFailed as e:
                    # This is the useful message: the child's own reason, e.g.
                    # a FASTEXPR parse error naming the offending character.
                    out[cid] = self._fail(cid, str(e))
                    continue
                except BrainError as e:
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
    def reclaim(self) -> dict:
        """Harvest simulations BRAIN ran while we were not listening.

        A crash mid-poll leaves the slot already paid for; BRAIN finishes the
        job regardless. Polling the stored progress URL turns that sunk cost
        back into a result instead of paying for the same expression twice.
        """
        bundles = self.store.inflight_bundles()
        out = {"reclaimed": 0, "still_running": 0, "lost": 0}
        if not bundles:
            return out
        log.info("reclaiming %d in-flight bundle(s) from a previous run",
                 len(bundles))
        c = self.client()
        for url, rows in bundles.items():
            live = []
            for r in rows:
                cand = self.store.candidate(r["candidate_id"])
                if cand is not None:
                    live.append((r["candidate_id"], cand))
            if not live:
                for r in rows:
                    self.store.clear_dispatched(r["candidate_id"])
                continue
            try:
                body = c.poll(url, interval=self.cfg.poll_interval, timeout=90.0)
            except SimulationFailed as e:
                if "timeout" in str(e).lower():
                    out["still_running"] += len(live)   # keep the marker
                    continue
                for cid, _ in live:
                    self.store.clear_dispatched(cid)
                out["lost"] += len(live)
                continue
            except BrainError:
                for cid, _ in live:
                    self.store.clear_dispatched(cid)
                out["lost"] += len(live)
                continue
            recs = self._harvest(c, body, live)
            for rec in recs.values():
                out["reclaimed" if rec.get("status") == "OK" else "lost"] += 1
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
