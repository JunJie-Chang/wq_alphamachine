"""Status snapshot for scripts/status.sh.

Kept as its own file rather than inline in the shell script: the SQL would
otherwise have to survive two layers of heredoc quoting, and f-strings cannot
carry backslash escapes inside their expressions.

Read-only. The one number that lives on the platform -- what is submitted -- is read
from the platform when a session is alive (one GET, capped at a few seconds); with no
live session it falls back to the local ledger and says so.
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# Stages of `python3 -m alphamachine [global options] <stage> ...`. The old shell
# one-liner took the first word after "alphamachine", which is a global option
# (`--concurrency`), so it reported "執行中: --concurrency" for every stage.
STAGES = ("catalog", "priors", "scan", "refine", "select", "submit",
          "report", "gatelog", "audit", "run")


def stage_from_args(args: str):
    """The subcommand of an `alphamachine` command line, or None."""
    toks = args.split()
    try:
        i = toks.index("alphamachine")
    except ValueError:
        return None
    for t in toks[i + 1:]:
        if t in STAGES:
            return t
    return None


def running_processes(ps_text: str) -> dict:
    """{'autoloop': (pid, seconds) | None, 'stage': (name, pid, seconds) | None}
    from `ps -eo pid,etimes,args` output. Anchored on the command STARTING with
    python3, so a shell whose command line merely contains the words is ignored."""
    out = {"autoloop": None, "stage": None}
    for line in ps_text.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(python3?\s.*)$", line)
        if not m:
            continue
        pid, secs, args = int(m.group(1)), int(m.group(2)), m.group(3)
        if re.match(r"python3?\s+(\S+/)?scripts/autoloop\.py", args):
            out["autoloop"] = (pid, secs)
        elif re.match(r"python3?\s+-m\s+alphamachine\b", args):
            st = stage_from_args(args)
            if st:
                out["stage"] = (st, pid, secs)
    return out


def hms(secs: int) -> str:
    h, r = divmod(int(secs), 3600)
    return f"{h}h{r // 60:02d}m" if h else f"{r // 60}m{r % 60:02d}s"


def blockers(sharpe, fitness, turnover, g) -> list:
    """Why a `gated` row would NOT pass today's gates -- the old 最佳候選 list
    ranked by |Sharpe| alone and showed a turnover of 1.299 as if it were a
    candidate."""
    out = []
    if abs(sharpe or 0) < g.sharpe:
        out.append(f"sharpe<{g.sharpe}")
    if (fitness or 0) < g.fitness:
        out.append(f"fitness<{g.fitness}")
    t = turnover or 0
    if t > g.turnover_max:
        out.append(f"turnover>{g.turnover_max}")
    if t < g.turnover_min:
        out.append(f"turnover<{g.turnover_min}")
    return out


Q_SIMS = "SELECT COUNT(*) FROM sims"
Q_OK = "SELECT COUNT(*) FROM sims WHERE status='OK'"
Q_ERR = "SELECT COUNT(*) FROM sims WHERE status!='OK'"
Q_FLY = "SELECT COUNT(*) FROM inflight"
Q_STAGE = "SELECT COUNT(*) FROM candidates WHERE stage=?"
Q_SUB = "SELECT COUNT(*) FROM submissions WHERE final_status='ACTIVE'"
Q_SUB_PP = ("SELECT COUNT(*) FROM submissions WHERE final_status='ACTIVE' "
            "AND json_valid(detail) AND json_extract(detail,'$.power_pool')=1")
Q_BEST = ("SELECT c.theme,c.template,s.sharpe,s.fitness,s.turnover "
          "FROM sims s JOIN candidates c ON c.id=s.candidate_id "
          "WHERE c.stage='gated' AND s.status='OK' "
          "ORDER BY abs(s.sharpe) DESC LIMIT 3")


def platform_snapshot(timeout: float = 8.0):
    """(total ACTIVE, Power Pool, submitted this scoring day) read live from BRAIN,
    or None when there is no live session. Never authenticates: it must not be
    able to start a persona challenge just because someone ran `status`."""
    try:
        from alphamachine.cli import _load_env
        from alphamachine.client import BrainClient
        from alphamachine.submit import count_submitted_today
        _load_env()
        c = BrainClient(os.environ.get("WQ_EMAIL", ""), os.environ.get("WQ_PASSWORD", ""))
        c.timeout = timeout
        if not c.session_alive():
            return None
        rows = c.my_alphas(status="ACTIVE", limit=100)
        pp = sum(1 for a in rows if "Power Pool Alpha" in
                 [x.get("name") if isinstance(x, dict) else x for x in a.get("classifications") or []]
                 or "PowerPoolSelected" in (a.get("tags") or []))
        return len(rows), pp, count_submitted_today(rows)
    except Exception:
        return None


def main() -> None:
    os.chdir(_ROOT)
    sys.path.insert(0, _ROOT)

    # --- what is running -----------------------------------------------
    try:
        ps = subprocess.run(["ps", "-eo", "pid,etimes,args"], capture_output=True,
                            text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        ps = ""
    run = running_processes(ps)
    al = run["autoloop"]
    print(f"  autoloop: {'執行中 (pid %d, 已跑 %s)' % (al[0], hms(al[1])) if al else '(無)'}")
    st = run["stage"]
    print(f"  執行中  : {'%s (pid %d, 已跑 %s)' % (st[0], st[1], hms(st[2])) if st else '(無)'}")

    # --- catalog ----------------------------------------------------------
    # The old line read a catalog.log from 2026-08-28 and divided by a hardcoded
    # 299. Count what is actually cached instead.
    parts = glob.glob("data/cache/parts_USA_TOP3000_d1/*.json")
    try:
        f = sum(int(x) for x in re.findall(r"(\d+) fields", open("data/logs/catalog.log").read()))
    except OSError:
        f = 0
    if parts:
        print(f"  catalog : {len(parts)} 個資料集已快取" + (f", {f:,} 欄位" if f else ""))

    # --- priors -------------------------------------------------------------
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

    # --- database -------------------------------------------------------------
    if os.path.exists("data/machine.db"):
        try:
            from alphamachine.config import Gates, RunConfig
            from alphamachine.submit import scoring_day_start
            c = sqlite3.connect("file:data/machine.db?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            one = lambda q, a=(): c.execute(q, a).fetchone()[0]
            print(f"  模擬    : {one(Q_SIMS)} 次  (成功 {one(Q_OK)}, "
                  f"錯誤 {one(Q_ERR)}, 未回收 {one(Q_FLY)})")
            print(f"  候選    : scan {one(Q_STAGE, ('scan',))}, "
                  f"refine {one(Q_STAGE, ('refine',))}, "
                  f"gated {one(Q_STAGE, ('gated',))}")

            cap = RunConfig().max_submits_per_day
            snap = platform_snapshot()
            if snap:
                total, pp, today = snap
                print(f"  已提交  : {total}  (Power Pool {pp}, 其他 {total - pp})   "
                      f"今日計分日 {today}/{cap}   [平台即時]")
            else:
                total = one(Q_SUB)
                try:
                    pp = one(Q_SUB_PP)
                except sqlite3.Error:                   # SQLite without JSON1
                    pp = None
                today = c.execute("SELECT COUNT(*) FROM submissions WHERE final_status='ACTIVE' "
                                  "AND submitted_at>=?", (scoring_day_start(),)).fetchone()[0]
                split = f"  (Power Pool {pp}, 其他 {total - pp})" if pp is not None else ""
                row = c.execute("SELECT v FROM kv WHERE k='submissions_synced'").fetchone()
                when = (time.strftime('%m-%d %H:%M', time.localtime(json.loads(row["v"])["at"]))
                        if row else "從未同步")
                print(f"  已提交  : {total}{split}   今日計分日 {today}/{cap}   "
                      f"[本機紀錄,可能落後;無有效 session;上次同步 {when}]")

            rows = c.execute(Q_BEST).fetchall()
            if rows:
                g = Gates()
                print("  gated 階段 |Sharpe| 前三 (標 ✗ 的今天過不了閘):")
                for r in rows:
                    bad = blockers(r["sharpe"], r["fitness"], r["turnover"], g)
                    print(f"      sharpe={r['sharpe'] or 0:5.2f}  fit={r['fitness'] or 0:5.2f}"
                          f"  turn={r['turnover'] or 0:5.3f}   {r['theme']}/{r['template']}"
                          + (f"   ✗ {', '.join(bad)}" if bad else ""))
        except sqlite3.Error as e:
            print(f"  (db 讀取失敗: {e})")


if __name__ == "__main__":
    main()
