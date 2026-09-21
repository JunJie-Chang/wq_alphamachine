#!/usr/bin/env bash
# 一覽：現在在跑什麼、進度、上一階段的結果。
#   scripts/status.sh       看一次
#   scripts/status.sh -w    每 20 秒刷新（Ctrl-C 離開）
cd "$(dirname "$0")/.." || exit 1

show() {
  echo
  echo "════════════ alphamachine ════════════"
  python3 scripts/_status.py
  echo
  echo "  log 檔  : data/logs/"
  echo
}

if [ "$1" = "-w" ]; then
  while true; do clear; show; sleep 20; done
else
  show
fi
