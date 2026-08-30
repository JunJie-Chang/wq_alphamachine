"""Status snapshot for scripts/status.sh.

Kept as its own file rather than inline in the shell script: the SQL would
otherwise have to survive two layers of heredoc quoting, and f-strings cannot
carry backslash escapes inside their expressions.
"""
import json
import os
import re
import sqlite3
import sys
import time

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# --- catalog -----------------------------------------------------------
try:
    log = open("data/logs/catalog.log").read()
    n = len(re.findall(r"\[ *\d+/", log))
    f = sum(int(x) for x in re.findall(r"(\d+) fields", log))
    print(f"  catalog : {n}/299 資料集, {f:,} 欄位")
except OSError:
    pass

# --- priors ------------------------------------------------------------
try:
    p = json.load(open("data/cache/arm_priors.json"))
    print(f"  priors  : {len(p)} 個資料集已分類")
except (OSError, ValueError):
    pass
try:
    plog = open("data/logs/priors_full.log").read()
    last = plog.rstrip().splitlines()[-1]
    if "in_progress" in last or "submitted" in last:
        print(f"            (batch 進行中，已輪詢 {plog.count('in_progress')} 次)")
except (OSError, IndexError):
    pass

# --- database ----------------------------------------------------------
Q_SIMS = "SELECT COUNT(*) FROM sims"
Q_OK = "SELECT COUNT(*) FROM sims WHERE status='OK'"
Q_ERR = "SELECT COUNT(*) FROM sims WHERE status!='OK'"
Q_FLY = "SELECT COUNT(*) FROM inflight"
Q_STAGE = "SELECT COUNT(*) FROM candidates WHERE stage=?"
Q_SUB = "SELECT COUNT(*) FROM submissions WHERE final_status='ACTIVE'"
Q_BEST = ("SELECT c.theme,c.template,s.sharpe,s.fitness,s.turnover "
          "FROM sims s JOIN candidates c ON c.id=s.candidate_id "
          "WHERE c.stage='gated' AND s.status='OK' "
          "ORDER BY abs(s.sharpe) DESC LIMIT 3")

if os.path.exists("data/machine.db"):
    try:
        c = sqlite3.connect("file:data/machine.db?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        one = lambda q, a=(): c.execute(q, a).fetchone()[0]
        print(f"  模擬    : {one(Q_SIMS)} 次  (成功 {one(Q_OK)}, "
              f"錯誤 {one(Q_ERR)}, 未回收 {one(Q_FLY)})")
        print(f"  候選    : scan {one(Q_STAGE, ('scan',))}, "
              f"refine {one(Q_STAGE, ('refine',))}, "
              f"gated {one(Q_STAGE, ('gated',))}")
        print(f"  已提交  : {one(Q_SUB)}")
        rows = c.execute(Q_BEST).fetchall()
        if rows:
            print("  最佳候選:")
            for r in rows:
                print(f"      sharpe={r['sharpe'] or 0:5.2f}  fit={r['fitness'] or 0:5.2f}"
                      f"  turn={r['turnover'] or 0:5.3f}   {r['theme']}/{r['template']}")
    except sqlite3.Error as e:
        print(f"  (db 讀取失敗: {e})")

# --- biometric quota ---------------------------------------------------
try:
    n = len([t for t in json.load(open("data/biometric_attempts.json"))
             if t > time.time() - 86400])
    from alphamachine.client import BrainClient
    print(f"  人臉額度: 24h 內用了 {n}/{BrainClient.BIOMETRIC_SAFE_BUDGET}"
          f"  (平台上限 {BrainClient.BIOMETRIC_DAILY_CAP})")
except (OSError, ValueError):
    pass
