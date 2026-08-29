#!/usr/bin/env bash
# Overnight health snapshot, appended to data/logs/overnight.md.
#
# Read-only with ONE exception: it restarts autoloop.py if the process has died
# outright. It never touches submission, and never tries to solve a biometric
# block -- only a human can do that, so it just records the wait.
cd "$(dirname "$0")/.." || exit 1
OUT=data/logs/overnight.md
{
  echo
  echo "### $(date '+%m-%d %H:%M')"

  if pgrep -f "scripts/autoloop.py" >/dev/null; then
    stage=$(pgrep -af "python3 -m alphamachine" | grep -v pgrep \
            | sed 's/.*alphamachine //' | awk '{print $3}' | head -1)
    echo "- autoloop: RUNNING (stage: ${stage:-between stages})"
  else
    echo "- autoloop: **DEAD** -- restarting"
    nohup python3 scripts/autoloop.py --scan-budget 300 --refine-top 25 \
          --select-n 10 --submit-n 4 >> data/logs/autoloop.log 2>&1 &
    echo "- restarted as PID $!"
  fi

  # A biometric block parks the loop until morning; say so plainly rather than
  # letting a silent gap look like progress. Scoped to the CURRENT run only --
  # an auth block from a previous, already-restarted run is history, not status.
  cur=$(awk '/autoloop starting/ {n=NR} END {print n+0}' data/logs/autoloop.log)
  last_auth=$(tail -n "+${cur:-1}" data/logs/autoloop.log \
              | grep "NEEDS_AUTH\|RESUMED" | tail -1)
  case "$last_auth" in
    *NEEDS_AUTH*) echo "- **BLOCKED on face check**: $last_auth" ;;
    *RESUMED*)    echo "- recovered from an auth block: $last_auth" ;;
    *)            echo "- auth: fine, no block this run" ;;
  esac

  hits=$(grep -c "CLEARED_DEEP_GATE" data/logs/autoloop.log 2>/dev/null)
  echo "- CLEARED_DEEP_GATE so far: ${hits:-0}"

  echo '```'
  python3 -m alphamachine report --corr 2>&1 | tail -6
  python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/machine.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
pair = ("ratio_fields","resid_vs_field","gated_by_field","divergence","confirmation")
q = "SELECT COUNT(*) n FROM sims s JOIN candidates c ON c.id=s.candidate_id WHERE s.status='OK'"
tot = c.execute(q).fetchone()["n"]
two = c.execute(q + " AND c.field2 IS NOT NULL AND c.field2!=''").fetchone()["n"]
surv = c.execute(q + " AND c.field2!='' AND abs(s.sharpe)>=0.65").fetchone()["n"]
print(f"sims OK {tot} | two-field {two} | two-field past scan bar {surv}")
rows = c.execute(
    "SELECT c.template, c.field, c.field2, s.sharpe, s.fitness, s.turnover"
    " FROM sims s JOIN candidates c ON c.id=s.candidate_id"
    " WHERE s.status='OK' AND c.field2 IS NOT NULL AND c.field2!=''"
    " ORDER BY abs(s.sharpe) DESC LIMIT 5").fetchall()
for r in rows:
    print(f"  {r['sharpe']:>5.2f} fit={r['fitness'] or 0:>4.2f} "
          f"turn={r['turnover'] or 0:>5.3f}  {r['template']}: "
          f"{r['field']} x {r['field2']}")
PY
  echo '```'
} >> "$OUT" 2>&1
