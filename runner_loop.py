"""常驻链外壳：只负责「准点触发」和「接力」，不含业务逻辑。

对应 meter_monitor 的 main.py，业务从「每分钟采集」换成「每天 14:50 记一次账」。

为什么要有它
------------
cron 是 best-effort：会晚几十分钟甚至几小时，也会整次丢弃。
但只要有一个进程在 14:50 之前就已经活着、并且它自己数到了 14:50，
准点就不再依赖 cron。所以：

    cron（兜底拉起）→ 本循环活着 → 自己数到 14:50 → 子进程跑 paper_trade.py
                     → 跑满 5h45m → exit(0)
                     → workflow 最后一步用 PAT dispatch 自己 → 下一轮

两个触发时刻
------------
    14:50           主跑：正式盘（窗口内才真记账）+ 测试盘（每天一次）
    09:00 ~ 09:29   次月 1~3 号的盘前补跑：正式盘

两者都是「到点之后，只要今天还没跑过就补跑」，所以：
  - 接力换班的几十秒恰好落在 14:50，下一轮 job 会自己补上，不会漏；
  - 链断了几小时才被拉起，只要还在同一天，起来立刻补跑。

可靠性约定
----------
  1. 子进程隔离：paper_trade.py 无论跳过、报错还是崩溃，都带不走本循环。
  2. 推送失败只告警，绝不中断循环 —— 否则链会断。
  3. 「今天跑过没」记在 chain_state.json 并提交，接力换了 job 也不会重复触发。
  4. 只有 exit code 0 才算「今天做完了」；选股失败（exit 2）不记账，
     留给下一轮 job 重试（11-30 那次因此最多能重试到当天深夜）。

用法
----
    python runner_loop.py                     # 正常运行（5h45m 后退出交棒）
    python runner_loop.py --max-run 180        # 短跑验证链路
    python runner_loop.py --run-now            # 立刻跑一次主触发，不等 14:50
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "chain_state.json"

MAIN_AT = "14:50"                 # 主触发时刻（北京时间 HH:MM）
PREOPEN_FROM, PREOPEN_TO = "09:00", "09:30"
PREOPEN_MAX_DAY = 3               # 只有每月 1~3 号做盘前补跑

MAX_RUN_TIME = 20700              # 5h45m；平台单 job 硬上限 6h，留 15 分钟收尾
SLEEP_ALIGN = 60                  # 每分钟醒一次，对齐整分

DATA_DIRS = ("paper", "paper_test")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:                 # noqa: BLE001
    pass


# --------------------------------------------------------------------------- 工具
def now() -> dt.datetime:
    return dt.datetime.now()


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def printf(msg: str) -> None:
    """带时间戳的即时输出，方便在 Actions 页面上看进度。"""
    print(f"[{stamp()}] {msg}", flush=True)


def check_tz() -> None:
    """时区必须真的生效，否则 14:50 会被当成 14:50 UTC，整天错 8 小时。"""
    off = dt.datetime.now().astimezone().utcoffset()
    if off != dt.timedelta(hours=8):
        printf(f"::error::TZ 不是 +08:00（当前 {off}），目标时刻会算错 8 小时")
        sys.exit(1)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:         # noqa: BLE001
            pass
    return {}


def save_state(st: dict) -> None:
    st["updated"] = stamp()
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def sleep_to_next_minute() -> None:
    """睡到下一个整分，不累积漂移。"""
    time.sleep(max(1.0, SLEEP_ALIGN - (time.time() % SLEEP_ALIGN)))


# --------------------------------------------------------------------------- git
def _token() -> str:
    return os.environ.get("GH_PUSH_TOKEN") or os.environ.get("PAT_TOKEN") or ""


def _redact(text: str) -> str:
    tok = _token()
    return text.replace(tok, "***") if tok else text


def _authed_url() -> str | None:
    tok = _token()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        return None
    return f"https://x-access-token:{tok}@github.com/{repo}.git"


def _git(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=180)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:      # noqa: BLE001
        return 1, "", str(exc)


def commit_data(verbose: bool = True) -> bool:
    """把数据目录和 chain_state.json 提交并推回仓库。返回是否真的推送成功。

    推送失败只告警不抛异常 —— 链断了比丢一次提交严重得多。
    """
    if not (ROOT / ".git").exists():
        if verbose:
            printf("[commit] 跳过：不在 git 仓库里")
        return False

    if _git(["config", "user.name"])[0] != 0:
        _git(["config", "user.name", "paper-bot"])
    if _git(["config", "user.email"])[0] != 0:
        _git(["config", "user.email", "paper-bot@users.noreply.github.com"])

    targets = [n for n in (*DATA_DIRS, STATE_FILE.name) if (ROOT / n).exists()]
    if not targets:
        if verbose:
            printf("[commit] 跳过：没有可提交的目录")
        return False

    code, _, err = _git(["add", "-A", "--", *targets])
    if code != 0:
        printf(f"[commit] git add 失败: {_redact(err)[:200]}")
        return False
    if _git(["diff", "--cached", "--quiet"])[0] == 0:
        if verbose:
            printf("[commit] 跳过：无变化")
        return False

    msg = f"paper: {stamp()} 自动提交"
    code, _, err = _git(["commit", "-q", "-m", msg])
    if code != 0:
        printf(f"[commit] git commit 失败: {_redact(err)[:200]}")
        return False

    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    authed = _authed_url()
    remote = authed or "origin"

    for attempt in range(1, 4):
        _git(["pull", "--rebase", "--autostash", remote, branch])
        code, _, err = _git(["push", remote, f"HEAD:{branch}"])
        if code == 0:
            printf(f"[commit] 已推送 -> {branch}（第 {attempt} 次尝试）")
            return True
        printf(f"[commit] 推送失败（第 {attempt} 次）: {_redact(err)[:200]}")
        time.sleep(2)

    printf("[commit] 推送多次失败，数据先留在 runner 上；本轮继续跑，链不能断")
    return False


# --------------------------------------------------------------------------- 业务
def _paper_args(root: str, extra: list[str]) -> list[str]:
    env = os.environ
    return [
        sys.executable, "-u", str(ROOT / "paper_trade.py"),
        "--root", root,
        "--n", *env.get("PORTFOLIOS", "5 10").split(),
        "--cand", env.get("CAND", "20"),
        "--capital", env.get("CAPITAL", "200000"),
        "--window", env.get("WINDOW", "20"),
        "--cut", env.get("CUT", "2"),
        *extra,
    ]


def run_paper(root: str, extra: list[str] | None = None) -> int:
    """在子进程里跑一次 paper_trade.py，返回退出码。永不抛异常。"""
    cmd = _paper_args(root, extra or [])
    printf(f"→ 运行 paper_trade.py --root {root} {' '.join(extra or [])}".rstrip())
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, timeout=900)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        printf(f"  [!] {root} 超时（>15 分钟），当作失败")
        code = 2
    except Exception as exc:      # noqa: BLE001
        printf(f"  [!] {root} 启动失败: {exc}")
        code = 2
    printf(f"← paper_trade.py --root {root} 退出码 {code}，耗时 {time.time() - t0:.1f}s")
    return code


def main_trigger(why: str) -> bool:
    """主触发：正式盘（不带 --force，让脚本自己的闸门决定）+ 测试盘 + 每日估值。"""
    printf(f"===== 主触发（{why}）=====")
    code_paper = run_paper("paper")
    code_test = run_paper("paper_test",
                          ["--force", "--skip-if-ran-today", "--skip-if-recorded"])
    # 每日估值：把今天补进净值曲线（只估值、不成交）。
    # 必须排在真调仓之后 —— 那天已经有记录了，这两步会自己跳过。
    run_paper("paper", ["--mark-only"])
    run_paper("paper_test", ["--mark-only"])
    # 选股失败（2）不算做完，留给下一轮 job 重试；估值失败不影响这个判断
    return code_paper == 0 and code_test == 0


def preopen_trigger(why: str) -> bool:
    """盘前补跑：只补正式盘。必须赶在 09:30 开盘前跑完。"""
    printf(f"===== 盘前补跑（{why}）=====")
    code = run_paper("paper")
    return code == 0


# --------------------------------------------------------------------------- 主循环
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-run", type=int, default=MAX_RUN_TIME,
                    help=f"本轮最长运行秒数，默认 {MAX_RUN_TIME}（5h45m）")
    ap.add_argument("--run-now", action="store_true",
                    help="启动时立刻跑一次主触发，不等 14:50")
    args = ap.parse_args()

    check_tz()
    t0 = time.time()
    st = load_state()

    printf("=" * 72)
    printf("常驻链启动")
    printf(f"  主触发时刻       = {MAIN_AT}")
    printf(f"  盘前补跑窗口     = {PREOPEN_FROM}~{PREOPEN_TO}，仅每月 1~{PREOPEN_MAX_DAY} 号")
    printf(f"  本轮最长运行     = {args.max_run}s（{args.max_run / 3600:.2f}h）")
    printf(f"  chain_state      = {json.dumps(st, ensure_ascii=False)}")
    printf("=" * 72)

    if args.run_now:
        printf("[--run-now] 立刻执行一次主触发")
        ok = main_trigger("手动 run-now")
        if ok:
            st["last_main"] = now().date().isoformat()
            save_state(st)
        commit_data()

    while True:
        cur = now()
        today = cur.date().isoformat()
        hhmm = cur.strftime("%H:%M")

        # ---- 1) 主触发：到点且今天还没跑过（故意用 >=，到点后的任何时刻都能补跑）
        if hhmm >= MAIN_AT and st.get("last_main") != today:
            printf(f"到达主触发时刻（{hhmm} >= {MAIN_AT}），今天 {today} 还没跑")
            ok = main_trigger(f"{hhmm} 主触发")
            if ok:
                st["last_main"] = today
                save_state(st)
            else:
                printf("[!] 主触发未成功，不记 last_main，下一轮 job 会重试")
            # 数据和 chain_state 合并成一条 commit
            commit_data()

        # ---- 2) 盘前补跑：次月 1~3 号的 09:00~09:29
        elif (PREOPEN_FROM <= hhmm < PREOPEN_TO
              and cur.day <= PREOPEN_MAX_DAY
              and st.get("last_preopen") != today):
            printf(f"到达盘前补跑窗口（{hhmm}，每月 {cur.day} 号）")
            ok = preopen_trigger(f"{hhmm} 盘前")
            if ok:
                st["last_preopen"] = today
                save_state(st)
            else:
                printf("[!] 盘前补跑未成功，不记 last_preopen，稍后同窗口内还会再试")
            commit_data()

        # ---- 3) 到点交棒：主动 exit(0)，让 workflow 去 dispatch 下一轮
        elapsed = time.time() - t0
        if elapsed > args.max_run:
            printf(f"已运行 {elapsed:.0f}s，主动退出交棒")
            # 这里刻意不写 chain_state：没有任何状态变化就不该产生提交，
            # 否则每 5h45m 都会多一条只有时间戳的噪音 commit。
            commit_data()
            return 0

        sleep_to_next_minute()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        printf("收到中断信号，先提交再退出")
        commit_data()
        sys.exit(130)
