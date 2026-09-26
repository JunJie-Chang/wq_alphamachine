"""Thin, defensive HTTP client for the WorldQuant BRAIN API.

Design notes
------------
* BRAIN's session is a JWT cookie set by POST /authentication. It expires; we
  transparently re-authenticate exactly once on a 401 and replay the request.
* Simulation is submit-then-poll. While a simulation is running the progress
  endpoint returns a `Retry-After` header instead of a terminal payload.
* A 201 from /submit means "request accepted", NOT "alpha is live". You must
  re-read the alpha and confirm status == ACTIVE.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Iterable

import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urljoin

from .config import API_BASE

log = logging.getLogger("alphamachine.client")


class BrainError(RuntimeError):
    pass


class AuthError(BrainError):
    pass


class BiometricRequired(AuthError):
    """BRAIN periodically demands a browser-based persona check."""


class SimulationFailed(BrainError):
    pass


def is_persona_challenge(headers) -> bool:
    """Is this 401's WWW-Authenticate a persona (biometric) challenge?

    BRAIN answers 401 for two different reasons that must not be confused: a
    persona check (WWW-Authenticate carries "persona") and plain bad credentials
    (WWW-Authenticate is just the auth scheme, e.g. "Bearer", no Location header).
    Treating the second as the first prints an EMPTY link -- confirmed live
    2026-09-23, when a stale WQ_PASSWORD answered 401 INVALID_CREDENTIALS with
    WWW-Authenticate: Bearer and no Location, and the un-fixed script waited on a
    sentinel for a URL that was never going to appear.
    """
    return "persona" in (headers.get("WWW-Authenticate", "") or "").lower()


class DailyLimitReached(SimulationFailed):
    """The platform's daily simulation cap (DAILY_SIMULATION_LIMIT_EXCEEDED) is spent.

    Nothing about the candidate: it resets with the day. The Executor stops dispatching
    for the rest of the run and leaves everything queued.
    """


# Substrings that mark an error message as INFRASTRUCTURE, not a verdict on the
# candidate: the slots were full, the daily cap was spent, the API rate-limited us, the
# session was dead, the network blipped. Such a message must never be stored as a
# candidate's permanent result. Found 2026-09-21/22: ~25,000 candidates -- 21,074
# CONCURRENT_SIMULATION_LIMIT_EXCEEDED, 3,914 from a dead session (authenticate() hit
# its 5/min limit from 13 threads at once), ~570 network blips, 402 DAILY_SIMULATION_
# LIMIT_EXCEEDED -- were recorded as ERROR for good and counted as failed bandit trials.
# The Executor decides by exception TYPE where it can; this list classifies messages
# that only exist as stored text (scripts/repair_capacity_errors.py) or arrive wrapped.
TRANSIENT_ERROR_MARKERS = (
    "CONCURRENT_SIMULATION_LIMIT_EXCEEDED",
    "DAILY_SIMULATION_LIMIT_EXCEEDED",
    "rate limited [",              # authenticate(): 429 on POST /authentication (5/min)
    "API rate limit exceeded",
    "auth failed [",               # incl. the captcha it demands after repeated attempts
    "BRAIN requires a biometric",  # the session died
    "simulate [401]",              # ... and re-authenticating inside request() gave up
    "HTTPSConnectionPool",
    "Connection aborted",
    "RemoteDisconnected",
    "Read timed out",
    "Connection reset",
    "NameResolutionError",
    "timeout after",               # poll deadline: the job may still finish server-side
)


def is_transient_text(msg: str) -> bool:
    """Is this stored/wrapped error message an infrastructure failure?"""
    return any(m in (msg or "") for m in TRANSIENT_ERROR_MARKERS)


class SlotsBusy(SimulationFailed):
    """Every simulation slot on the account stayed busy for the whole wait.

    NOT a verdict on the candidate: nothing was simulated. It subclasses
    SimulationFailed so a caller that does not know about it still fails safe, but the
    Executor catches it first and defers the bundle instead of recording an ERROR.
    """


class SimulationCancelled(SimulationFailed):
    """BRAIN cancelled this job: it was never simulated, so it is no verdict.

    Seen live 2026-09-26: when one child of a multi-sim bundle fails at run time,
    BRAIN marks the parent ERROR and every sibling not yet run CANCELLED. Before
    this existed poll() read {"status": "CANCELLED"} as "not finished yet" and
    waited out its whole timeout per child -- 9 siblings x 300s stalled a scan
    for 44 minutes -- then deferred them with the in-flight marker kept, so every
    later reclaim() re-polled a job that had ended hours before.
    """


class BrainClient:
    """One instance per thread. `requests.Session` cookie jars are not
    thread-safe under concurrent use, and BRAIN auth lives in a cookie."""

    def __init__(self, email: str, password: str, api: str = API_BASE,
                 timeout: float = 60.0, cookie_path: str = "data/cookies.json"):
        if not email or not password:
            raise AuthError("WQ_EMAIL / WQ_PASSWORD are not set")
        self._email = email
        self._password = password
        self.api = api.rstrip("/")
        self.timeout = timeout
        self.cookie_path = cookie_path
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "alphamachine/1.0"})
        self._lock = threading.Lock()
        self._authed = False
        self._expires_at: float | None = None
        self._load_cookies()

    # -------------------------------------------------------- cookie jar
    # BRAIN's persona (biometric) check is bound to the SESSION that raised
    # the inquiry, not to the account: a fresh session is re-challenged every
    # time. So once a session is verified, persist its jar and reuse it --
    # otherwise every process restart demands another face scan.
    def _load_cookies(self) -> None:
        try:
            with open(self.cookie_path) as fh:
                blob = json.load(fh)
            jar = blob.get("cookies", blob) if isinstance(blob, dict) else blob
            self.s.cookies.update(requests.utils.cookiejar_from_dict(jar))
            if isinstance(blob, dict):
                self._expires_at = blob.get("expires_at")
            if self._expires_at is None:
                # A jar written by the persona flow carries no expiry field.
                # The JWT itself always does, so read it rather than leaving
                # the session looking immortal -- refresh_if_stale() is a no-op
                # when it cannot tell how long is left.
                self._expires_at = self._jwt_expiry()
            log.debug("loaded %d cookies from %s", len(jar), self.cookie_path)
        except (OSError, ValueError):
            pass

    def _save_cookies(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cookie_path) or ".", exist_ok=True)
            with open(self.cookie_path, "w") as fh:
                json.dump({"cookies": requests.utils.dict_from_cookiejar(self.s.cookies),
                           "expires_at": self._expires_at}, fh)
            os.chmod(self.cookie_path, 0o600)
        except OSError as e:
            log.warning("could not persist cookies: %s", e)

    # ------------------------------------------ biometric quota: NOT counted here
    # There used to be a client-side budget in this spot (20 of the platform's 25 a
    # day, kept in data/biometric_attempts.json). It was added after an early retry
    # loop burned the day's quota in minutes, and it later caused more downtime than
    # it prevented: a session that expires every ~4 h ran into OUR number before the
    # platform's. The platform exposes no count anywhere (checked 2026-09-21:
    # /users/self is profile only, and the only rate-limit headers on
    # /authentication are per-minute), so the counter was a guess.
    #
    # What still protects the account: a 429 BIOMETRICS_THROTTLED from the platform
    # itself raises immediately and is never retried (see request()), and
    # REAUTH_COOLDOWN stops one process re-POSTing /authentication in a loop.

    # The session's end is not a thing to guess: it is the `exp` claim of the JWT in
    # the cookie, already read into _expires_at. This used to be the cookie FILE's
    # mtime plus a hardcoded 4 h, a stand-in for a value sitting in the cookie.
    # POST /authentication is limited to 5/min and every 401-persona answer mints a
    # new inquiry, yet each executor thread owns its own BrainClient -- so when the
    # session died, 13 threads each re-authenticated at once: 3,760 "rate limited" and
    # 100 "captcha required" answers in minutes. One failed attempt now pauses every
    # client in the process (class-level), so the other threads raise immediately
    # instead of posting again.
    AUTH_PAUSE_SECONDS = 300.0
    _auth_blocked_until = 0.0
    _auth_block_reason = ""

    @classmethod
    def _pause_auth(cls, reason: str) -> None:
        cls._auth_blocked_until = time.time() + cls.AUTH_PAUSE_SECONDS
        cls._auth_block_reason = reason

    SESSION_MARGIN = 60.0          # seconds: do not trust a session in its last minute
    FALLBACK_TTL_HOURS = 4.0       # only when no expiry can be read at all

    def session_fresh(self) -> bool:
        """Trust the persisted cookie without spending a request on it."""
        exp = self._expires_at or self._jwt_expiry()
        if exp is not None:
            return time.time() < exp - self.SESSION_MARGIN
        try:
            age = time.time() - os.path.getmtime(self.cookie_path)
        except OSError:
            return False
        return age < self.FALLBACK_TTL_HOURS * 3600.0

    def session_alive(self) -> bool:
        """Is the loaded cookie still a live BRAIN session?"""
        try:
            r = self.s.get(f"{self.api}/users/self", timeout=self.timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ---------------------------------------------------- session lifetime
    # BRAIN sessions are short-lived (the auth response carries token.expiry;
    # commonly 14400s = 4h). This matters more than it looks: an overnight scan
    # that lets the session lapse will start failing mid-flight, and the 401
    # recovery path re-POSTs /authentication -- which can trigger a persona
    # challenge that no unattended process can answer. So we refresh EARLY,
    # between simulations, while the current session is still valid.
    def _record_expiry(self, body: Any) -> None:
        exp = None
        if isinstance(body, dict):
            tok = body.get("token")
            if isinstance(tok, dict) and tok.get("expiry") is not None:
                try:
                    exp = time.time() + float(tok["expiry"])
                except (TypeError, ValueError):
                    exp = None
        if exp is None:
            exp = self._jwt_expiry()
        self._expires_at = exp
        if exp:
            log.info("session valid for %.1f h", (exp - time.time()) / 3600.0)
        self._save_cookies()

    def _jwt_expiry(self) -> float | None:
        """Read the `exp` claim straight out of the session JWT, if present."""
        for c in self.s.cookies:
            val = c.value or ""
            parts = val.split(".")
            if len(parts) != 3:
                continue
            try:
                pad = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(pad))
            except Exception:
                continue
            if isinstance(claims, dict) and claims.get("exp"):
                try:
                    return float(claims["exp"])
                except (TypeError, ValueError):
                    pass
        return None

    def seconds_left(self) -> float | None:
        if not self._expires_at:
            return None
        return self._expires_at - time.time()

    # Minimum gap between two real /authentication POSTs from this process.
    # Every POST can mint a persona inquiry, and the platform allows only 25
    # biometric requests a day.
    REAUTH_COOLDOWN = 300.0
    _last_reauth = 0.0

    def refresh_if_stale(self, margin: float = 900.0) -> bool:
        """Re-authenticate proactively if under `margin` seconds remain.

        Call this BETWEEN units of work, never mid-poll. Returns True if the
        session was renewed. Raises BiometricRequired if BRAIN wants a face
        check, so the caller can stop cleanly rather than burn the queue.

        The subtlety this guards against: N worker threads each hold their own
        client, and they all cross the expiry boundary at the same moment. The
        naive version -- clear cookies, then authenticate -- makes a POST
        unavoidable and does it N times per bundle. Against a 25/day biometric
        quota that empties the account in minutes. So: take a cross-process
        lock, re-read the jar from disk (a sibling may have already refreshed
        it), and only POST if that genuinely fails.
        """
        left = self.seconds_left()
        if left is None or left > margin:
            return False
        log.info("session expires in %.0fs -- renewing", left)
        return self._renew()

    def _renew(self) -> bool:
        # The lock is an optimisation, not a correctness requirement: the
        # cooldown below still bounds logins if we cannot take it. So never let
        # an unwritable directory turn a renewal into a crash.
        fh = None
        try:
            lock_path = os.path.join(os.path.dirname(self.cookie_path) or ".",
                                     ".auth.lock")
            os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
            fh = open(lock_path, "a+")
        except OSError as e:
            log.debug("no auth lock (%s) -- relying on the cooldown", e)
        try:
            if fh is not None:
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass                               # best effort off-Linux
            # Someone else may have refreshed while we waited for the lock.
            self.s.cookies.clear()
            self._load_cookies()
            if len(self.s.cookies) and self.session_alive():
                self._authed = True
                log.info("session already renewed by another worker")
                return True
            now = time.time()
            if now - BrainClient._last_reauth < self.REAUTH_COOLDOWN:
                log.warning("re-auth attempted %.0fs ago -- refusing to POST "
                            "/authentication again this soon (biometric quota)",
                            now - BrainClient._last_reauth)
                return False
            BrainClient._last_reauth = now
            self._authed = False
            self.authenticate()
            return True
        finally:
            if fh is not None:
                fh.close()

    # ---------------------------------------------------------------- auth
    def authenticate(self) -> dict[str, Any]:
        # A persisted cookie beats re-authenticating: re-POSTing /authentication
        # on an already-verified session restarts the persona flow and gets you
        # challenged again for no reason.
        if len(self.s.cookies) and (self.session_fresh() or self.session_alive()):
            self._authed = True
            if self._expires_at is None:
                self._expires_at = self._jwt_expiry()
            log.info("reusing persisted session for %s", self._email)
            return {"reused": True}
        # Every call to this endpoint can mint a persona inquiry, so it is only
        # reached once the session is really gone -- and once one attempt has failed,
        # no other client in this process may try again for AUTH_PAUSE_SECONDS.
        if time.time() < BrainClient._auth_blocked_until:
            raise BiometricRequired(
                f"authentication paused for {BrainClient._auth_blocked_until - time.time():.0f}s "
                f"after a failed attempt ({BrainClient._auth_block_reason}); not POSTing "
                f"/authentication again. Run scripts/auth_wait.py.")
        r = self.s.post(f"{self.api}/authentication",
                        auth=HTTPBasicAuth(self._email, self._password),
                        timeout=self.timeout)
        if r.status_code == 201:
            self._authed = True
            body = _json(r)
            self._record_expiry(body)
            log.info("authenticated as %s", self._email)
            return body
        if r.status_code == 401:
            if is_persona_challenge(r.headers):
                self._pause_auth("persona check required")
                loc = r.headers.get("Location", "")
                url = urljoin(r.url, loc) if loc else ""
                raise BiometricRequired(
                    "BRAIN requires a biometric/persona check. Run "
                    "scripts/auth_wait.py, complete the printed URL in a browser, "
                    "then `touch data/persona_done`. Do NOT re-POST "
                    "/authentication afterwards: the finalise call IS the "
                    f"authentication, and re-authing mints a new inquiry. [{url}]"
                )
            raise AuthError(f"bad credentials: {r.text[:300]}")
        if r.status_code == 429:
            # authenticate() posts directly, so it does not pass through the
            # 429 handling in request(). Classify it here or the caller gets a
            # generic auth error and no idea that retrying makes it worse.
            detail = ""
            try:
                detail = (r.json() or {}).get("detail", "")
            except ValueError:
                pass
            self._pause_auth("429 on /authentication")
            if "BIOMETRIC" in detail.upper():
                raise BiometricRequired(
                    "BIOMETRICS_THROTTLED -- the daily biometric quota (25/day) "
                    "is spent. No Retry-After is published; the counter is "
                    "daily, so retry tomorrow. Retrying now only extends it.")
            raise AuthError(f"rate limited [{r.status_code}]: {r.text[:200]}")
        self._pause_auth(f"HTTP {r.status_code}")
        raise AuthError(f"auth failed [{r.status_code}]: {r.text[:300]}")

    def _ensure_auth(self) -> None:
        if not self._authed:
            with self._lock:
                if not self._authed:
                    self.authenticate()

    # ------------------------------------------------------------- request
    def _respect_rate_limit(self, r: requests.Response, elapsed: float = 0.0) -> None:
        """Self-throttle from the server's own budget headers.

        BRAIN advertises its remaining quota on every response
        (`RateLimit-Remaining` / `RateLimit-Reset`, and the per-minute variants).
        Reading those and pausing when the budget runs low is strictly better
        than discovering the limit by getting 429'd: a 429 on a poll can cost an
        in-flight simulation, and repeated 429s on some endpoints escalate to a
        much longer lockout.
        """
        rem = r.headers.get("RateLimit-Remaining") or r.headers.get(
            "X-RateLimit-Remaining-Minute")
        if rem is None:
            return
        try:
            remaining = int(rem)
        except ValueError:
            return
        if remaining > 1:
            return
        try:
            reset = float(r.headers.get("RateLimit-Reset") or 2.0)
        except ValueError:
            reset = 2.0
        # The reset window starts when the request was SENT, not when we got
        # the reply. On a 1-req/sec endpoint a 2-second round trip has already
        # outlived its own window, so sleeping the full reset on top of it is
        # pure waste -- it was costing ~1.5s on every single catalogue page.
        wait = min(reset, 60.0) - elapsed
        if wait <= 0:
            return
        log.debug("rate budget spent (%s left) -- pausing %.2fs", remaining, wait)
        time.sleep(wait)

    def request(self, method: str, path: str, *, retries: int = 4,
                **kw) -> requests.Response:
        self._ensure_auth()
        url = path if path.startswith("http") else f"{self.api}{path}"
        kw.setdefault("timeout", self.timeout)
        backoff = 2.0
        for attempt in range(retries + 1):
            _t0 = time.time()
            r = self.s.request(method, url, **kw)
            self._respect_rate_limit(r, time.time() - _t0)
            if r.status_code == 401 and attempt < retries:
                log.info("401 -> re-authenticating")
                with self._lock:
                    self._authed = False
                self._ensure_auth()
                continue
            if r.status_code == 429:
                detail = ""
                try:
                    detail = (r.json() or {}).get("detail", "")
                except ValueError:
                    pass
                if "DAILY_SIMULATION_LIMIT" in detail.upper():
                    return r          # the caller stops for the day (see simulate())
                if "CONCURRENT_SIMULATION_LIMIT" in detail.upper():
                    # Capacity, not a rate limit: backing off 2/4/8/16/32 s and then
                    # giving up (what the branch below does) is the wrong shape. The
                    # caller waits for a slot to free (see simulate()).
                    return r
                if "BIOMETRIC" in detail.upper():
                    # A separate, much longer counter that exposes no reset
                    # time. Retrying only digs the hole deeper -- surface it.
                    raise BiometricRequired(
                        "BIOMETRICS_THROTTLED -- too many biometric attempts. "
                        "Wait it out (hours, possibly until tomorrow); do not retry.")
                wait = float(r.headers.get("Retry-After",
                                           r.headers.get("RateLimit-Reset", backoff)))
                log.warning("rate limited, sleeping %.1fs", wait)
                time.sleep(min(wait, 300.0))
                backoff = min(backoff * 2, 300.0)
                continue
            if r.status_code >= 500 and attempt < retries:
                log.warning("server %s, backoff %.1fs", r.status_code, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            return r
        return r

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw) -> requests.Response:
        return self.request("PATCH", path, **kw)

    # ------------------------------------------------------------ catalog
    def operators(self) -> list[dict]:
        return _json(self.get("/operators")) or []

    # BRAIN caps page size at 50; asking for more returns 400, it does not
    # silently clamp. Verified against /data-fields on 2026-08-28.
    MAX_PAGE = 50

    def data_sets(self, *, region="USA", delay=1, universe="TOP3000",
                  instrument_type="EQUITY", limit=MAX_PAGE) -> list[dict]:
        return self._paged("/data-sets", {
            "instrumentType": instrument_type, "region": region,
            "delay": delay, "universe": universe, "limit": limit})

    def data_fields(self, *, region="USA", delay=1, universe="TOP3000",
                    dataset_id: str | None = None, search: str | None = None,
                    instrument_type="EQUITY", limit=MAX_PAGE) -> list[dict]:
        params: dict[str, Any] = {
            "instrumentType": instrument_type, "region": region,
            "delay": delay, "universe": universe, "limit": limit}
        if dataset_id:
            params["dataset.id"] = dataset_id
        if search:
            params["search"] = search
        return self._paged("/data-fields", params)

    def _paged(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = int(params.get("limit", 50))
        while True:
            p = dict(params, offset=offset)
            r = self.get(path, params=p)
            if r.status_code != 200:
                log.warning("%s [%s]: %s", path, r.status_code, r.text[:200])
                break
            body = _json(r) or {}
            results = body.get("results", [])
            out.extend(results)
            count = body.get("count")
            if not results:
                break
            # Advance by what the server ACTUALLY returned. Servers are free to
            # cap `limit` below what you asked for; advancing by the requested
            # limit then skips every record in the gap -- silently, and worst on
            # the biggest datasets, which are exactly the ones worth mining.
            offset += len(results)
            if count is not None and offset >= int(count):
                break
            if len(out) > 200000:                 # runaway guard
                log.warning("%s: stopping at %d records", path, len(out))
                break
        return out

    # --------------------------------------------------------- simulation
    # POST /simulations answers 429 CONCURRENT_SIMULATION_LIMIT_EXCEEDED when every slot
    # on the account is busy -- including slots used by another session or machine. It
    # used to be handled as a rate limit (5 backoffs, then give up), after which the
    # Executor recorded the whole bundle as ERROR. Found 2026-09-21: 21,074 of the
    # database's 22,470 errors (94%, since 2026-08-30) were exactly this -- candidates
    # that were never simulated, marked failed for good, and counted as failed trials
    # against their bandit arms. Wait for a slot instead; only if none frees up for
    # SLOT_WAIT_SECONDS raise SlotsBusy, which the Executor DEFERS (no record written).
    SLOT_WAIT_SECONDS = 1200.0
    SLOT_POLL_SECONDS = 20.0

    def simulate(self, payload: dict | list[dict]) -> str:
        """Submit one alpha (dict) or a multi-simulation bundle (list).

        Multi-simulation accepts up to 10 children per bundle and is gated on
        account tier. Returns the progress URL from the Location header.
        """
        deadline = time.time() + self.SLOT_WAIT_SECONDS
        announced = 0.0
        while True:
            r = self.post("/simulations", json=payload)
            if r.status_code == 429 and "CONCURRENT_SIMULATION_LIMIT" in (r.text or "").upper():
                now = time.time()
                if now >= deadline:
                    raise SlotsBusy(f"no simulation slot free for {self.SLOT_WAIT_SECONDS:.0f}s "
                                    f"(CONCURRENT_SIMULATION_LIMIT_EXCEEDED); nothing was simulated")
                if now - announced >= 60.0:
                    log.info("all simulation slots busy; waiting for one to free up")
                    announced = now
                time.sleep(self.SLOT_POLL_SECONDS)
                continue
            break
        if r.status_code == 429 and "DAILY_SIMULATION_LIMIT" in (r.text or "").upper():
            raise DailyLimitReached("DAILY_SIMULATION_LIMIT_EXCEEDED: the daily simulation "
                                    "cap is spent; nothing was simulated")
        if r.status_code in (200, 201):
            loc = r.headers.get("Location")
            if not loc:
                raise SimulationFailed(f"no Location header: {r.text[:300]}")
            return loc
        raise SimulationFailed(f"simulate [{r.status_code}]: {r.text[:500]}")

    # BRAIN stops serving a progress URL some time after the job ends. A 404 body
    # ({"detail": "Not found."}) has no children / status / alpha, so poll used to
    # read it as "finished, payload not built yet" and spin until the deadline --
    # and Executor.reclaim, seeing "timeout" in the error, kept the in-flight
    # marker as "still running". Found 2026-09-20: 28 candidates from a run 261 h
    # earlier were never re-simulated and cost ~4.5 min of dead polling on every
    # start. Consecutive 404s, not one, so a URL queried the instant after the
    # POST that briefly is not there yet is still waited for.
    POLL_404_LIMIT = 5

    def poll(self, progress_url: str, *, interval: float = 5.0,
             timeout: float = 900.0) -> dict:
        """Block until the simulation resolves. Honours Retry-After."""
        deadline = time.time() + timeout
        not_found = 0
        while time.time() < deadline:
            r = self.get(progress_url)
            if r.status_code == 404:
                not_found += 1
                if not_found >= self.POLL_404_LIMIT:
                    # Deliberately NOT worded as a timeout: reclaim() keeps the
                    # marker for those, and this job is gone, not slow.
                    raise SimulationFailed(
                        f"progress URL not found (HTTP 404 x{not_found}); the job "
                        f"expired or was deleted: {progress_url}")
                time.sleep(interval)
                continue
            not_found = 0
            # BRAIN sends Retry-After on every progress read and sets it to "0"
            # when the job is finished. Treating any Retry-After as "still
            # running" hangs forever; treating its absence as "done" reads a
            # half-built payload. Only a POSITIVE value means keep waiting --
            # and obeying the server's own number is also what keeps us inside
            # the request-rate budget without a hand-tuned poll interval.
            retry_after = r.headers.get("Retry-After", "0")
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 0.0
            if wait > 0:
                time.sleep(min(wait, 60.0))
                continue
            body = _json(r) or {}
            status = body.get("status")
            # A multi-sim parent reports ERROR when ANY child failed. Raising
            # here would attribute one generic message to all ten candidates
            # and discard the per-child reason -- which is the only thing that
            # actually tells you WHY (e.g. a parse error in one expression).
            # Hand the body back so the caller can read each child separately.
            if body.get("children"):
                return body
            if status == "CANCELLED":
                raise SimulationCancelled(f"cancelled by BRAIN, never simulated: {progress_url}")
            if status in ("ERROR", "FAIL"):
                raise SimulationFailed(body.get("message") or str(body)[:300])
            if body.get("alpha"):
                return body
            # Finished-looking response with no payload yet: brief pause.
            time.sleep(interval)
        raise SimulationFailed(f"timeout after {timeout}s: {progress_url}")

    def cancel_simulation(self, progress_url: str) -> bool:
        """Release a slot we are no longer waiting on.

        On shutdown this is better than orphaning the job: DELETE hands the
        slot straight back instead of leaving it occupied until BRAIN finishes
        work whose result nobody will read.
        """
        try:
            r = self.request("DELETE", progress_url, retries=0)
            return r.status_code in (200, 202, 204)
        except BrainError:
            return False

    def child_result(self, child_id: str, *, interval: float = 2.0,
                     timeout: float = 300.0) -> dict:
        """Resolve one child of a multi-simulation bundle.

        The parent going quiet does not guarantee every child is written yet,
        so this polls on the child's own Retry-After exactly like the parent.
        A bare GET here silently returns a payload with no alpha id, which the
        caller can only report as an error -- after the slot was already paid.
        """
        return self.poll(f"{self.api}/simulations/{child_id}",
                         interval=interval, timeout=timeout)

    # -------------------------------------------------------------- alpha
    def alpha(self, alpha_id: str) -> dict:
        return _json(self.get(f"/alphas/{alpha_id}")) or {}

    def alpha_pnl(self, alpha_id: str) -> dict:
        return self._recordset(f"/alphas/{alpha_id}/recordsets/pnl")

    def alpha_yearly(self, alpha_id: str) -> dict:
        return self._recordset(f"/alphas/{alpha_id}/recordsets/yearly-stats")

    def _recordset(self, path: str, tries: int = 3, delay: float = 2.0,
                   present=lambda out: bool(out.get("records"))) -> dict:
        """Fetch a recordset, tolerating BRAIN computing it lazily.

        These endpoints behave like /check: the first read after an alpha is
        created can 204, or answer 200 with an empty `records` list, and only
        fill in a moment later. A bare GET therefore reports "this alpha has no
        PnL" for an alpha that has plenty -- which upstream reads as a real
        absence and silently drops the candidate.

        _await_record already handles 204/Retry-After. The extra retry here is
        for the 200-with-nothing-yet case it cannot see, bounded tightly so a
        genuinely empty result costs a few seconds, not the full timeout.
        `present` lets a caller whose payload is NOT shaped like
        `{"records": [...]}` (alpha_check's `{"is": {"checks": [...]}}`) supply
        its own "did this actually arrive" test.
        """
        out: dict = {}
        for i in range(tries):
            out = self._await_record(path) or {}
            if present(out):
                return out
            if i + 1 < tries:
                time.sleep(delay)
        return out

    def alpha_check(self, alpha_id: str) -> dict:
        """Server-side submission check -- the authoritative gate, including
        PROD_CORRELATION which you cannot compute locally.

        Same lazy-computation race as alpha_pnl/alpha_yearly, missed the first
        time this class was hardened against it: a 200 with an empty `checks`
        list reads exactly like "nothing failed" to a naive caller, when it
        really means BRAIN has not finished computing yet. Confirmed live --
        one alpha returned `checks: []` immediately, then 20 real entries
        (including a PROD_CORRELATION FAIL) two minutes later on retry.
        """
        def _has_checks(out: dict) -> bool:
            return bool((out.get("is") or {}).get("checks") or out.get("checks"))
        return self._recordset(f"/alphas/{alpha_id}/check", tries=5, delay=3.0,
                               present=_has_checks)

    def self_correlation(self, alpha_id: str) -> dict:
        return self._await_record(f"/alphas/{alpha_id}/correlations/self")

    def prod_correlation(self, alpha_id: str) -> dict:
        return self._await_record(f"/alphas/{alpha_id}/correlations/prod")

    def _await_record(self, path: str, *, timeout: float = 300.0) -> dict:
        """Several BRAIN endpoints compute lazily and 204/Retry-After first."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.get(path)
            ra = r.headers.get("Retry-After")
            if ra:
                time.sleep(max(float(ra), 1.0))
                continue
            if r.status_code == 204:
                time.sleep(3.0)
                continue
            return _json(r) or {}
        raise BrainError(f"timed out waiting for {path}")

    def patch_alpha(self, alpha_id: str, **props) -> dict:
        return _json(self.patch(f"/alphas/{alpha_id}", json=props)) or {}

    def my_alphas(self, *, status: str | None = "ACTIVE", limit: int = 100,
                  order: str = "-dateCreated") -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "order": order}
        if status:
            params["status"] = status
        return self._paged("/users/self/alphas", params)

    def submit(self, alpha_id: str) -> dict:
        r = self.post(f"/alphas/{alpha_id}/submit")
        if r.status_code not in (200, 201, 202):
            raise BrainError(f"submit [{r.status_code}]: {r.text[:400]}")
        # 201 only means accepted. Poll the submit resource for the verdict.
        try:
            return self._await_record(f"/alphas/{alpha_id}/submit")
        except BrainError:
            return _json(r) or {}


def _json(r: requests.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return None
