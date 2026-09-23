"""Persona (biometric) authentication -- ONE request per attempt.

BRAIN allows 25 biometric requests per day. That number makes polling the
wrong shape entirely: a loop that POSTs the finalise endpoint every 10s burns
the whole day's quota in four minutes and earns a BIOMETRICS_THROTTLED lockout.
Worse, every POST /authentication that returns 401-persona mints a NEW inquiry,
so a "retry until it works" loop spends two units per iteration.

So this script spends exactly two units:

    1 unit   POST /authentication      -> raises one inquiry, prints its URL
    <you complete it in the browser, then: touch data/persona_done>
    1 unit   POST <persona URL>        -> finalises, exactly once

Waiting is done on a local sentinel file, not on the network. The verification
read (GET /users/self) is a normal API call and does not touch the quota.

Nothing here keeps a running count of the day's units -- the platform exposes none,
and a client-side guess caused more downtime than it prevented -- so the design
(never poll, wait on a file, finalise once) is what keeps the usage low.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.auth import HTTPBasicAuth

from alphamachine.cli import _load_env
from alphamachine.client import is_persona_challenge

API = "https://api.worldquantbrain.com"
COOKIE_FILE = "data/cookies.json"
SENTINEL = "data/persona_done"
URL_FILE = "data/persona_url.txt"
WAIT_MINUTES = 30


def save(session, expires_at=None) -> None:
    os.makedirs("data", exist_ok=True)
    with open(COOKIE_FILE, "w") as fh:
        json.dump({"cookies": requests.utils.dict_from_cookiejar(session.cookies),
                   "expires_at": expires_at}, fh)
    os.chmod(COOKIE_FILE, 0o600)


# Every response header from both persona POSTs, appended to a log. This existed to
# settle whether the finalise call costs a biometric unit -- a question that only
# mattered while the client kept its own count of the day's units (it no longer does,
# 2026-09-21: the platform exposes no such number, and the counter was a guess that
# caused more downtime than it prevented). The log is kept because it costs nothing
# and shows exactly what the platform does send back.
HEADER_LOG = "data/logs/biometric_headers.log"


# Never write these to disk: an /authentication response carries the session
# JWT in Set-Cookie, and this log exists to be read and pasted around.
_REDACT = ("set-cookie", "authorization", "www-authenticate", "cookie")


def _dump_headers(label: str, r) -> None:
    try:
        os.makedirs(os.path.dirname(HEADER_LOG), exist_ok=True)
        with open(HEADER_LOG, "a") as fh:
            fh.write(f"\n=== {label} @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"status={r.status_code} ===\n")
            for k, v in sorted(r.headers.items()):
                if k.lower() in _REDACT:
                    fh.write(f"  {k}: <redacted>\n")
                    continue
                fh.write(f"  {k}: {v}\n")
    except OSError:
        pass                                    # never fail auth over logging
    interesting = {k: v for k, v in r.headers.items()
                   if "ratelimit" in k.lower() or "retry" in k.lower()
                   or "quota" in k.lower() or "biometric" in k.lower()}
    if interesting:
        print(f"  [{label}] {interesting}")


def main() -> int:
    _load_env()
    email, pw = os.environ.get("WQ_EMAIL"), os.environ.get("WQ_PASSWORD")
    s = requests.Session()
    s.headers.update({"User-Agent": "alphamachine/1.0"})

    # Free check first: a persisted session costs no quota at all.
    try:
        with open(COOKIE_FILE) as fh:
            blob = json.load(fh)
        jar = blob.get("cookies", blob) if isinstance(blob, dict) else blob
        s.cookies.update(requests.utils.cookiejar_from_dict(jar))
        if s.get(f"{API}/users/self", timeout=30).status_code == 200:
            print("AUTH OK -- persisted session still live. No quota spent.")
            return 0
    except (OSError, ValueError):
        pass
    s.cookies.clear()

    if os.path.exists(SENTINEL):
        os.remove(SENTINEL)

    # --- step 1: raise the inquiry ------------------------------------------
    r = s.post(f"{API}/authentication", auth=HTTPBasicAuth(email, pw), timeout=60)
    _dump_headers("POST /authentication (mints the inquiry)", r)
    if r.status_code == 201:
        save(s)
        print("AUTH OK (no persona needed); cookies saved.")
        return 0
    if r.status_code == 429:
        print(f"THROTTLED: {r.text[:120]}  -- wait, do not retry.")
        return 1
    if r.status_code != 401:
        print(f"unexpected [{r.status_code}]: {r.text[:200]}")
        return 1

    # A 401 here is not necessarily a persona challenge -- BRAIN also answers 401
    # INVALID_CREDENTIALS with no Location header (a wrong/rotated WQ_PASSWORD).
    # That used to fall through to the Location-extraction below, printing an EMPTY
    # link: the sentinel would then wait on nothing, and finalise() would POST to "".
    if not is_persona_challenge(r.headers):
        print(f"AUTH FAILED (not a persona challenge): [{r.status_code}] {r.text[:200]}\n"
              f"WWW-Authenticate: {r.headers.get('WWW-Authenticate', '')!r}\n"
              f"WQ_EMAIL / WQ_PASSWORD in .env are being rejected by BRAIN -- check "
              f"they still match your account (a password change on the site would "
              f"do this) and fix .env before retrying. Not retrying automatically: "
              f"repeated failed attempts risk BRAIN flagging or locking the account.")
        return 1

    loc = r.headers.get("Location", "")
    if not loc:
        print(f"persona challenge with no Location header: {dict(r.headers)}")
        return 1
    url = f"{API}{loc}" if loc.startswith("/") else loc
    os.makedirs("data", exist_ok=True)
    open(URL_FILE, "w").write(url + "\n")

    print(f"\n  1. Open this in a browser and complete the check:\n\n     {url}\n")
    print(f"  2. Then run:  touch {SENTINEL}\n")
    print(f"  Waiting up to {WAIT_MINUTES} min. No network polling -- "
          f"watching the sentinel file only.\n", flush=True)

    deadline = time.time() + WAIT_MINUTES * 60
    while time.time() < deadline:
        if os.path.exists(SENTINEL):
            break
        time.sleep(3)
    else:
        print("TIMEOUT -- sentinel never appeared. No extra quota was spent.")
        return 1

    # --- step 2: finalise it, exactly once ------------------------------------
    fin = s.post(url, timeout=60)
    _dump_headers("POST persona finalise", fin)
    who = s.get(f"{API}/users/self", timeout=60)      # free: not a biometric call
    _dump_headers("GET /users/self (known free -- control)", who)
    print(f"finalise={fin.status_code} users/self={who.status_code}")
    if who.status_code == 200:
        save(s)
        u = who.json() or {}
        print(f"\nAUTH OK -- verified as {u.get('id')}. Session persisted to "
              f"{COOKIE_FILE} and reused from now on.")
        os.remove(SENTINEL)
        return 0

    print("\nFinalise did not take. Do NOT rerun immediately -- each attempt "
          "costs quota. Confirm the browser check completed, then try once more.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
