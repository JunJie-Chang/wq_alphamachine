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
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from requests.auth import HTTPBasicAuth

from alphamachine.cli import _load_env

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

    from alphamachine.client import BrainClient
    probe = BrainClient(email, pw)
    left = probe.biometric_budget_left()
    print(f"biometric budget: {left} of {BrainClient.BIOMETRIC_SAFE_BUDGET} "
          f"remaining in the last 24h (platform cap {BrainClient.BIOMETRIC_DAILY_CAP}/day)")
    if left < 2:
        print("Not enough budget for a clean attempt (needs 2). Wait for it to free up.")
        return 1

    if os.path.exists(SENTINEL):
        os.remove(SENTINEL)

    # --- quota unit 1 -------------------------------------------------------
    probe._record_bio_attempt()
    r = s.post(f"{API}/authentication", auth=HTTPBasicAuth(email, pw), timeout=60)
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

    loc = r.headers.get("Location", "")
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

    # --- quota unit 2 -------------------------------------------------------
    probe._record_bio_attempt()
    fin = s.post(url, timeout=60)
    who = s.get(f"{API}/users/self", timeout=60)      # free: not a biometric call
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
