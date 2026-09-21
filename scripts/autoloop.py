"""Drive the pipeline (scan -> refine -> gatelog -> select -> submit) forever.

Each stage is run exactly as it has been run by hand all session: a fresh
`python3 -m alphamachine <stage> ...` subprocess. This script only sequences
and watches those processes -- it reimplements none of their logic, so a stage
behaves identically whether launched from here or from a terminal.

INVARIANT, enforced only in this file: submit is invoked WITHOUT --live. No
code path here constructs that flag under any condition. Automating the loop
is not a reason to relax the one boundary that has held all session --
submission stays a dry-run report, never an action.

Two failure signatures neither `cli.py` nor `executor.py` turn into a clean
stop:

  * `_setup()` calls `client.authenticate()` at the start of every command; a
    dead session raises BiometricRequired, uncaught anywhere in cli.py, so the
    process exits non-zero with a traceback.
  * `Executor.run()` catches BiometricRequired PER BUNDLE internally, sets its
    own halt flag, and returns HALTED records without re-raising -- so the
    command exits 0 having done less work than asked, logging one line:
    "HALTED: BRAIN requires a biometric check".

Either one means the same thing: the session is dead and only a human can
revive it. This script does not try to solve that itself -- it cannot complete
a face scan. It logs one clear marker and polls a FREE endpoint (GET
/users/self, no biometric quota touched -- see BrainClient.session_alive() and
scripts/auth_wait.py's own comment) until the session a Claude session
refreshes on its behalf becomes valid, then retries the SAME stage. Retrying
costs nothing extra: scan dedupes via already_simulated, and refine replays a
part-finished ladder from stored results (verified live this session at 1
round / 2 new simulations after a real crash mid-refine).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import signal
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from alphamachine.cli import _load_env
from alphamachine.client import BrainClient

POLL_SECONDS = 30
NEEDS_AUTH_MARKERS = ("BiometricRequired", "HALTED")

# A third outcome the original two-way split had no room for: the network went
# away. Seen live -- DNS stopped resolving api.worldquantbrain.com mid-`select`
# ("Temporary failure in name resolution"), the stage exited non-zero with no
# auth marker, and the loop classified a transient blip as a code bug and shut
# itself down for good. Nothing was broken and nothing needed a human; it just
# needed to wait.
#
# These are matched against the child's combined output, so they must be
# specific enough not to swallow a genuine bug. Every one names a transport
# failure that cannot be produced by this codebase's own logic.
TRANSIENT_MARKERS = (
    "Temporary failure in name resolution",
    "NameResolutionError",
    "Failed to resolve",
    "Connection aborted",
    "ConnectionResetError",
    "Max retries exceeded",
    "Read timed out",
    "ReadTimeoutError",
    "Network is unreachable",
)
TRANSIENT_BACKOFF = 60.0     # one minute, then just try the stage again
TRANSIENT_MAX_RETRIES = 20   # ~20 min of outage before giving up for real

# The currently-running stage subprocess, if any -- so a signal handler can
# kill it. `subprocess.run()` blocks the parent until the child exits, so a
# plain `kill` on the parent leaves that child orphaned and still hitting the
# API. This bit within minutes of first running this script: killing an
# earlier buggy instance left its `scan` subprocess running, and the next
# clean launch started a second one against the same account concurrently.
_current_proc: subprocess.Popen | None = None


def _handle_signal(signum, _frame) -> None:
    if _current_proc is not None and _current_proc.poll() is None:
        log(f"received signal {signum}: terminating the running stage too")
        _current_proc.terminate()
    raise SystemExit(1)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def log(line: str) -> None:
    """Stdout only. The launcher redirects stdout to LOG_PATH (`>> data/logs/
    autoloop.log`, the same convention every other stage's nohup run uses this
    session) -- writing to the file here too would double every line."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def session_alive() -> bool:
    """Free check: a plain GET on the cookies currently on disk.

    Reloaded fresh every call -- a Claude session finalising a persona check
    overwrites data/cookies.json out from under this process, and this is how
    that update is noticed without either process telling the other directly.
    """
    try:
        with open("data/cookies.json") as fh:
            blob = json.load(fh)
        jar = blob.get("cookies", blob) if isinstance(blob, dict) else blob
        s = requests.Session()
        s.cookies.update(requests.utils.cookiejar_from_dict(jar))
        r = s.get("https://api.worldquantbrain.com/users/self", timeout=30)
        return r.status_code == 200
    except (OSError, ValueError, requests.RequestException):
        return False


def wait_for_session() -> None:
    log("NEEDS_AUTH: waiting for a valid session "
        "(a Claude session should be fetching a persona link for you)")
    while not session_alive():
        time.sleep(POLL_SECONDS)
    log("RESUMED: session valid again")


def run_stage(concurrency: int, name: str, args: list[str]) -> None:
    """Run one CLI stage; retry indefinitely across auth blocks.

    Streams the child's output line by line (rather than waiting for it to
    exit) so a HALTED or CLEARED_DEEP_GATE line reaches the log -- and the
    Monitor watching it -- as it happens, not after a multi-hour scan finally
    finishes. stdout and stderr merge into one stream: a HALTED line and a
    BiometricRequired traceback are both diagnostic text here, not output this
    script needs to tell apart by channel.
    """
    global _current_proc
    transient_tries = 0
    while True:
        cmd = ["python3", "-m", "alphamachine",
               "--concurrency", str(concurrency), name, *args]
        log(f"[{name}] {' '.join(args)}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        _current_proc = proc
        out_lines: list[str] = []
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            out_lines.append(line)
            if line.strip():
                log(f"  [{name}] {line}")
        proc.wait()
        _current_proc = None
        out = "\n".join(out_lines)
        needs_auth = (proc.returncode != 0
                     or any(m in out for m in NEEDS_AUTH_MARKERS))
        if not needs_auth:
            return
        if proc.returncode != 0 and not any(m in out for m in NEEDS_AUTH_MARKERS):
            # Order matters: check for a transient transport failure BEFORE
            # declaring a bug. The network dropping out looks identical to a
            # crash from the outside -- non-zero exit, a traceback, no auth
            # marker -- and treating one as the other killed a healthy loop
            # for 5 minutes of DNS trouble.
            if any(m in out for m in TRANSIENT_MARKERS):
                transient_tries += 1
                if transient_tries > TRANSIENT_MAX_RETRIES:
                    log(f"FATAL: [{name}] still failing on a network error "
                        f"after {transient_tries} retries -- stopping")
                    raise SystemExit(1)
                log(f"TRANSIENT: [{name}] network error "
                    f"({transient_tries}/{TRANSIENT_MAX_RETRIES}), retrying "
                    f"in {TRANSIENT_BACKOFF:.0f}s")
                time.sleep(TRANSIENT_BACKOFF)
                continue
            # A real bug, not an auth block -- surface it loudly and stop the
            # whole loop rather than spin forever on something a fresh
            # session cannot fix.
            log(f"FATAL: [{name}] exited {proc.returncode} for a reason "
                f"other than biometric auth -- stopping")
            raise SystemExit(1)
        transient_tries = 0          # a real auth block is not a network fault
        wait_for_session()
        # Loop back and retry the SAME stage rather than advancing --
        # partial work from the halted attempt is already saved.


def main() -> int:
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--concurrency", type=int, default=13)
    p.add_argument("--scan-budget", type=int, default=300)
    p.add_argument("--refine-top", type=int, default=25)
    p.add_argument("--select-n", type=int, default=10)
    p.add_argument("--submit-n", type=int, default=4)
    p.add_argument("--cycles", type=int, default=0,
                   help="stop after N cycles (0 = forever)")
    args = p.parse_args()

    _load_env()
    log(f"autoloop starting: scan --budget {args.scan_budget}, "
        f"refine --top {args.refine_top}, select --n {args.select_n}, "
        f"submit --n {args.submit_n} (dry run only)")

    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        cycle += 1
        log(f"===== cycle {cycle} =====")
        run_stage(args.concurrency, "scan", ["--budget", str(args.scan_budget)])
        run_stage(args.concurrency, "refine", ["--top", str(args.refine_top)])
        run_stage(args.concurrency, "gatelog", ["--passing", "--limit", "20"])
        run_stage(args.concurrency, "select", ["--n", str(args.select_n)])
        # NEVER --live. This is the only line in the file that invokes submit,
        # and it stays that way -- see the module docstring.
        run_stage(args.concurrency, "submit", ["--n", str(args.submit_n)])
        log(f"----- cycle {cycle} complete -----")
    return 0


if __name__ == "__main__":
    sys.exit(main())
