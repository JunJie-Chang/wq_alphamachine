"""The submission gate -- the last and most expensive decision in the machine.

Submission slots are limited and irreversible in practice: a submitted alpha
occupies book space and drags your score if it underperforms. So the gate is
belt-and-braces:

  local quality gate -> yearly consistency -> local correlation vs your book
  -> BRAIN's own /check (the only source of PROD_CORRELATION) -> submit
  -> RE-READ THE ALPHA and confirm status == ACTIVE.

That last step is not paranoia. POST /submit returning 201 means "accepted for
processing", not "live". Alphas routinely come back UNSUBMITTED after a 201
because a server-side correlation check rejected them.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .bandit import Bandit, arm_name
from .config import Gates, RunConfig
from .correlate import CorrelationBook, daily_returns
from .metrics import (Verdict, extract, failed_checks, headroom,
                      platform_verdict, pnl_series, quality_verdict,
                      yearly_consistency)

log = logging.getLogger("alphamachine.submit")


def _records_present(recordset: dict) -> bool:
    """Did BRAIN actually return rows, as opposed to an empty placeholder?"""
    return bool((recordset or {}).get("records"))


class Submitter:
    def __init__(self, cfg: RunConfig, store, client):
        self.cfg = cfg
        self.store = store
        self.c = client
        self.g: Gates = cfg.gates

    # ------------------------------------------------------------- vetting
    def vet(self, alpha_id: str, book: CorrelationBook | None = None) -> dict:
        """Full pre-submission dossier for one alpha. No side effects."""
        alpha = self.c.alpha(alpha_id)
        rec = extract(alpha)
        report: dict = {"alpha_id": alpha_id, "metrics": {
            k: rec.get(k) for k in ("sharpe", "fitness", "turnover", "returns",
                                    "drawdown", "margin", "longCount",
                                    "shortCount", "subuniverse_sharpe")}}

        pv = platform_verdict(rec)
        qv = quality_verdict(rec, self.g)
        report["platform"] = {"ok": pv.ok, "reasons": pv.reasons}
        report["quality"] = {"ok": qv.ok, "reasons": qv.reasons}
        report["headroom"] = headroom(rec, self.g)

        # Live, uncached: a submission decision must never rest on a stored
        # PROD_CORRELATION, which drifts with the book and the production pool.
        # server_check() runs separately in submit(), so it is skipped here.
        #
        # Inconclusive does not block a submission: BRAIN's own /check is the
        # authoritative gate and runs regardless, so a missing yearly recordset
        # is reported and stepped over, exactly as it was before.
        yc = self.deep_verdict(alpha_id, use_cache=False, with_server_check=False)
        report["consistency"] = ({"ok": True, "reasons": ["yearly stats unavailable"]}
                                 if yc is None else
                                 {"ok": yc.ok, "reasons": yc.reasons})

        # Local correlation against our own book (cheap, catches most twins).
        if book is not None:
            cached = self.store.load_pnl(alpha_id)
            if cached is None:
                d, v = pnl_series(self.c.alpha_pnl(alpha_id))
                if d:
                    self.store.save_pnl(alpha_id, d, v)
            else:
                d, v = cached
            if d:
                corr, who = book.max_corr(d, v)
                report["local_max_corr"] = {"value": corr, "against": who,
                                            "ok": corr < self.g.max_self_corr}

        report["ok"] = all(
            report[k]["ok"] for k in ("platform", "quality", "consistency")
        ) and report.get("local_max_corr", {"ok": True})["ok"]
        return report

    # -------------------------------------------------------- deep verdict
    def deep_verdict(self, alpha_id: str, *, use_cache: bool = True,
                     with_server_check: bool = True,
                     ttl_hours: float | None = None) -> Verdict | None:
        """The two judgements a simulation record cannot make on its own.

        Yearly consistency (is this alpha carried by one lucky year?) and
        PROD_CORRELATION (is this hypothesis already crowded in BRAIN's
        production pool?). Both cost an extra API call, and both routinely
        block alphas that cleared every local metric gate.

        One definition, several callers: `vet()` runs it live before a
        submission, while refine/select/gatelog run it cached to decide what is
        really submittable and to teach the bandit. Duplicating this logic is
        how the two would drift apart.

        Returns None for INCONCLUSIVE -- a source we asked for did not answer.
        That is deliberately distinct from both pass and fail: an unverified
        alpha must not be reported as submittable, but neither should a failed
        fetch be blamed on the alpha or taught to the bandit as a real loss.
        """
        ttl = self.cfg.deep_check_ttl_hours if ttl_hours is None else ttl_hours
        cached = self.store.load_deep(alpha_id, ttl) if use_cache else None
        reasons: list[str] = []

        yearly = (cached or {}).get("yearly") or None
        if not yearly:
            try:
                yearly = self.c.alpha_yearly(alpha_id)
            except Exception as e:
                log.info("deep verdict inconclusive for %s: yearly stats "
                         "unavailable (%s)", alpha_id, e)
                return None
        yv = yearly_consistency(yearly, self.g)
        if not yv.ok:
            reasons += yv.reasons
        elif not _records_present(yearly):
            # yearly_consistency PASSES when it has nothing to judge, returning
            # an advisory reason rather than a failure. Reading that as a
            # verdict would either wave through an unchecked alpha or, if the
            # reason were counted, blame the alpha for a missing fetch.
            log.info("deep verdict inconclusive for %s: empty yearly recordset",
                     alpha_id)
            return None

        # An empty list is never trusted, cached or fresh: `or None` collapses
        # `[]` to the same "need a real look" state as never having cached it.
        # A poisoned cache entry (the empty-checks race below, saved before
        # this was in place) would otherwise read as fresh for a full
        # deep_check_ttl_hours and keep reporting a false PASS the whole time.
        checks = (cached or {}).get("checks") or None
        if with_server_check and checks is None:
            try:
                checks = self.server_check(alpha_id)["checks"]
            except Exception as e:
                log.info("deep verdict inconclusive for %s: server check "
                         "unavailable (%s)", alpha_id, e)
                return None
            if not checks:
                # client.alpha_check() already retries this exact race
                # (5 tries over 15s); if it is STILL empty here, an authentic
                # response is never this: a real PASS carries dozens of
                # entries (LOW_SHARPE, LOW_FITNESS, ...). Report inconclusive
                # rather than reading "nothing failed" out of nothing checked.
                log.info("deep verdict inconclusive for %s: server check "
                         "came back empty", alpha_id)
                return None
        reasons += [f"server check FAIL: {n}" for n in failed_checks(checks or [])]

        v = Verdict(not reasons, reasons)
        if use_cache:
            self.store.save_deep(alpha_id, yearly=yearly, checks=checks,
                                 ok=v.ok, reasons=v.reasons)
        return v

    def server_check(self, alpha_id: str) -> dict:
        """BRAIN's authoritative check -- the only way to see PROD_CORRELATION.

        Worth running only AFTER the local gates pass: it is slow and it is the
        step most likely to be rate-limited.
        """
        body = self.c.alpha_check(alpha_id)
        checks = ((body.get("is") or {}).get("checks")
                  or body.get("checks") or [])
        fails = failed_checks(checks)
        return {"checks": checks, "fails": fails, "ok": not fails}

    # ------------------------------------------------------------- submit
    def submit(self, alpha_id: str, *, dry_run: bool = True,
               book: CorrelationBook | None = None) -> dict:
        if self._daily_budget_spent():
            return {"alpha_id": alpha_id, "skipped": "daily submit budget spent"}

        dossier = self.vet(alpha_id, book=book)
        if not dossier["ok"]:
            dossier["submitted"] = False
            return dossier

        sc = self.server_check(alpha_id)
        dossier["server_check"] = sc
        if not sc["ok"]:
            dossier["ok"] = False
            dossier["submitted"] = False
            return dossier

        if dry_run:
            dossier["submitted"] = False
            dossier["note"] = "dry run -- would have submitted"
            return dossier

        resp = self.c.submit(alpha_id)
        time.sleep(5.0)
        final = self.c.alpha(alpha_id)
        status = final.get("status")
        dossier["submitted"] = status == "ACTIVE"
        dossier["final_status"] = status
        dossier["response"] = resp
        if status != "ACTIVE":
            # 201 != live. Surface the reason rather than reporting success.
            dossier["reason"] = failed_checks(
                (final.get("is") or {}).get("checks") or []) or "unknown"
        self.store.record_submission(
            alpha_id, "ACCEPTED" if dossier["submitted"] else "REJECTED",
            status or "UNKNOWN", dossier)
        self._teach_bandit(alpha_id, dossier["submitted"])
        return dossier

    def _teach_bandit(self, alpha_id: str, submitted: bool) -> None:
        """Write the real outcome back to the arm that produced this alpha.

        This is the last hop of the loop the pipeline diagram describes
        ("submit -> write outcome back -> bandit learns -> loop"). Without it
        the `submitted` counter stays zero forever and the strongest available
        evidence -- an alpha that actually went live -- teaches nothing.
        """
        try:
            rows = self.store.sims_where("s.alpha_id=?", (alpha_id,))
            if not rows:
                return
            r = rows[0]
            arm = arm_name(r["theme"], r["template"])
            Bandit(self.store, [arm]).update(
                arm, stage="submitted", passed=submitted)
        except Exception as e:                 # never fail a submission on this
            log.warning("could not record submission against its arm: %s", e)

    # -------------------------------------------------------- release plan
    def release_plan(self, ranked: list[dict], *, per_day: int | None = None
                     ) -> list[list[dict]]:
        """Spread a ranked basket across submission DAYS instead of dumping it.

        Two separate rules make batching costly for a consultant:

        * only the first `daily_counted_alphas` (4) alphas of a day count
          toward base payment -- a fifth is a permanent book slot bought for
          nothing;
        * far more importantly, fewer than 20 SUBMISSION DAYS in a rolling
          three months forces Value Factor to its minimum, and quarterly
          payment eligibility requires >=20 distinct days outright.

        So days are the scarce resource, not alphas. Ten alphas released one
        a day beat ten released at once, even though the alpha count is
        identical -- and that is why `per_day` defaults well below the cap
        when inventory is thin enough to stretch.

        Best-first within each day: if anything gets truncated it should be
        the weakest alpha, not the strongest.
        """
        n = per_day or self.cfg.max_auto_submits_per_day
        n = max(1, min(n, self.cfg.daily_counted_alphas))
        ordered = sorted(ranked, key=lambda a: -a.get("quality",
                                                      a.get("headroom", 0.0)))
        return [ordered[i:i + n] for i in range(0, len(ordered), n)]

    def submission_days_this_quarter(self) -> int:
        """Distinct days with at least one live submission, this quarter.

        This is the number that gates quarterly payment (>=20) and keeps
        Value Factor off its floor -- it matters more than total alpha count.
        """
        import datetime as _dt
        now = _dt.datetime.now(timezone.utc)
        q_start = _dt.datetime(now.year, 3 * ((now.month - 1) // 3) + 1, 1,
                               tzinfo=timezone.utc).timestamp()
        rows = self.store.conn().execute(
            "SELECT submitted_at FROM submissions WHERE submitted_at>=? "
            "AND final_status='ACTIVE'", (q_start,)).fetchall()
        days = {_dt.datetime.fromtimestamp(r["submitted_at"],
                                           timezone.utc).date() for r in rows}
        return len(days)

    def days_shortfall(self) -> int:
        """How many more submission days this quarter still needs."""
        return max(0, self.cfg.min_submission_days_per_quarter
                   - self.submission_days_this_quarter())

    def submitted_today(self) -> int:
        return self.store.submits_since(scoring_day_start())

    def remaining_today(self) -> int:
        if not self.cfg.hold_surplus:
            return self.cfg.max_auto_submits_per_day
        return max(0, self.cfg.max_auto_submits_per_day - self.submitted_today())

    # ------------------------------------------------------- auto submission
    def auto_eligible(self, alpha_id: str, book: CorrelationBook | None = None
                      ) -> tuple[bool, dict]:
        """Is this alpha unambiguous enough to send with nobody watching?

        Every manual gate must pass at the STRICTER auto thresholds, every
        platform check must pass outright, and the alpha must clear each gate
        with a margin (headroom) rather than scraping past it. Anything that
        merely qualifies is exactly the case a human should see.
        """
        ag = self.cfg.auto_gates
        alpha = self.c.alpha(alpha_id)
        rec = extract(alpha)
        reasons: list[str] = []

        pv = platform_verdict(rec)
        if not pv.ok:
            reasons += pv.reasons
        qv = quality_verdict(rec, ag)
        if not qv.ok:
            reasons += [f"auto-tier: {r}" for r in qv.reasons]

        h = headroom(rec, ag)
        if h < ag.min_headroom:
            reasons.append(f"headroom {h:.2f} < {ag.min_headroom} "
                           f"(clears the bar but not by enough to be automatic)")

        if ag.require_all_checks_pass:
            unresolved = [c.get("name") for c in (rec.get("checks") or [])
                          if c.get("result") not in ("PASS", "WARNING", None)]
            if unresolved:
                reasons.append(f"unresolved checks: {unresolved}")

        try:
            yc = yearly_consistency(self.c.alpha_yearly(alpha_id), ag)
            if not yc.ok:
                reasons += [f"auto-tier: {r}" for r in yc.reasons]
        except Exception as e:
            reasons.append(f"yearly stats unavailable ({e}) -- refusing to "
                           f"auto-submit without them")

        if book is not None:
            cached = self.store.load_pnl(alpha_id)
            if cached is None:
                d, v = pnl_series(self.c.alpha_pnl(alpha_id))
                if d:
                    self.store.save_pnl(alpha_id, d, v)
            else:
                d, v = cached
            if not d:
                reasons.append("no PnL available -- cannot verify correlation")
            else:
                corr, who = book.max_corr(d, v)
                if corr > ag.max_self_corr:
                    reasons.append(f"auto-tier: corr {corr:.2f} vs {who} "
                                   f"> {ag.max_self_corr}")
        else:
            reasons.append("no correlation book loaded -- refusing to "
                           "auto-submit blind")

        return (not reasons), {"alpha_id": alpha_id, "headroom": h,
                               "reasons": reasons, "metrics": {
                                   k: rec.get(k) for k in
                                   ("sharpe", "fitness", "turnover", "drawdown")}}

    def auto_submit_batch(self, alpha_ids: list[str],
                          book: CorrelationBook | None = None,
                          *, live: bool = False) -> dict:
        """Submit only the unambiguous ones; hand everything else back."""
        if not self.cfg.auto_submit:
            return {"enabled": False, "submitted": [], "queued_for_review": [],
                     "note": "auto_submit is off (RunConfig.auto_submit=False)"}
        out = {"enabled": True, "submitted": [], "queued_for_review": [],
               "rejected": []}
        budget = self.remaining_today()
        out["held_for_tomorrow"] = []
        sent = 0
        for aid in alpha_ids:
            if sent >= budget:
                # Not a rejection: the daily 2,000-point ceiling is already
                # accounted for, so this alpha is worth strictly more tomorrow.
                out["held_for_tomorrow"].append(
                    {"alpha_id": aid,
                     "reasons": [f"daily score cap: {self.submitted_today() + sent}"
                                 f"/{self.cfg.max_auto_submits_per_day} already "
                                 f"submitted this scoring day"]})
                continue
            ok, dossier = self.auto_eligible(aid, book=book)
            if not ok:
                # "Not automatic" is not "not good" -- borderline alphas go to
                # the human queue, they are not thrown away.
                (out["queued_for_review"] if dossier["headroom"] > 1.0
                 else out["rejected"]).append(dossier)
                continue
            sc = self.server_check(aid)
            if not sc["ok"]:
                dossier["reasons"] = [f"server check: {sc['fails']}"]
                out["queued_for_review"].append(dossier)
                continue
            res = self.submit(aid, dry_run=not live, book=book)
            if res.get("submitted") or (not live and res.get("ok")):
                out["submitted"].append({**dossier, "live": live})
                sent += 1
            else:
                dossier["reasons"] = ["submit call did not confirm ACTIVE"]
                out["queued_for_review"].append(dossier)
        return out

    def _daily_budget_spent(self) -> bool:
        used = self.store.submits_since(scoring_day_start())
        if used >= self.cfg.max_submits_per_day:
            log.info("daily submit budget reached (%d since 3am ET)", used)
            return True
        return False


def scoring_day_start() -> float:
    """Epoch of the current BRAIN scoring day, which begins 03:00 America/New_York.

    Not UTC midnight. Getting this wrong splits one scoring day across two
    local windows and merges parts of two scoring days into one -- so the
    machine would think it had budget left when the cap was already spent, and
    every surplus submission past the daily 2,000-point ceiling is a permanent
    book slot bought for zero score. New York, not UTC, because the boundary
    follows US daylight saving.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:                              # no tzdata: approximate EST
        tz = timezone(timedelta(hours=-5))
    now = datetime.now(tz)
    start = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return start.timestamp()
