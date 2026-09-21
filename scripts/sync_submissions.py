"""Sync the platform's ACTIVE alphas into the local `submissions` table.

    python3 scripts/sync_submissions.py

This is a HISTORY LEDGER, nothing more. The table was written only by this
pipeline's own submit paths, so anything sent from the web UI was missing from it
(`status.sh` showed "submitted: 1" with 22 alphas ACTIVE). The daily cap and the
quarterly submission-day count no longer read it at all: they come straight from
the platform. What still reads it: the Genius-pyramid coverage report and the
offline fallback of `status.sh`.

Insert-only: it never overwrites a row this pipeline wrote and never deletes
anything. `submit` and `submit_powerpool.py` refresh it themselves. It does not
re-authenticate: with a dead session it stops and tells you.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphamachine.cli import _load_env
from alphamachine.client import BrainClient
from alphamachine.config import RunConfig
from alphamachine.store import Store
from alphamachine.submit import sync_active_submissions


def main() -> int:
    _load_env()
    cfg = RunConfig()
    if not cfg.email or not cfg.password:
        print("WQ_EMAIL / WQ_PASSWORD not set.", file=sys.stderr)
        return 2
    client = BrainClient(cfg.email, cfg.password)
    if not client.session_alive():
        print("No live BRAIN session. Run scripts/auth_wait.py first.", file=sys.stderr)
        return 1
    client.authenticate()                      # session is alive -> reuses it
    store = Store(cfg.db_path)
    r = sync_active_submissions(client, store)
    print(f"platform ACTIVE: {r['platform_active']}  (Power Pool {r['power_pool']}, "
          f"other {r['platform_active'] - r['power_pool']})")
    print(f"added to the local table: {r['added']}   already recorded: {r['already_recorded']}")
    print(f"submitted in the current scoring day (platform): {r['submitted_today']}/"
          f"{cfg.max_submits_per_day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
