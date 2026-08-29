#!/usr/bin/env bash
# Catalog progress at a glance. Usage: scripts/progress.sh [-w]
cd "$(dirname "$0")/.." || exit 1
LOG=data/logs/catalog.log
PARTS=data/cache/parts_USA_TOP3000_d1

show() {
  local n f start now
  n=$(grep -cE '\[ *[0-9]+/' "$LOG" 2>/dev/null || echo 0)
  f=$(grep -oE '[0-9]+ fields' "$LOG" 2>/dev/null | awk '{s+=$1} END {print s+0}')
  start=$(head -1 "$LOG" 2>/dev/null | grep -oE '^[0-9:]+')
  now=$(date +%H:%M:%S)
  if pgrep -f "alphamachine .*catalo""g" >/dev/null; then st="RUNNING"; else st="STOPPED"; fi
  python3 - "$n" "$f" "$start" "$now" "$st" <<'PY'
import datetime as dt, sys
n, f, start, now, st = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
bar = int(n / 299 * 40)
print(f"\n  [{'#'*bar}{'.'*(40-bar)}]  {n}/299 datasets   {f:,} fields   {st}")
if start and n:
    el = (dt.datetime.strptime(now, '%H:%M:%S') - dt.datetime.strptime(start, '%H:%M:%S')).total_seconds()
    if el > 0:
        print(f"  elapsed {el/60:.0f}m   eta {(299-n)*el/n/60:.0f}m   {n/el*60:.1f} datasets/min")
PY
  echo
  tail -3 "$LOG" 2>/dev/null | sed 's/^/  /'
  echo
}

if [ "$1" = "-w" ]; then
  while true; do clear; show; sleep 20; done
else
  show
fi
