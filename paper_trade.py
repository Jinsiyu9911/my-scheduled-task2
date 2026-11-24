"""模拟盘：按真实费率自动成交并记账（n=5 / n=10 两个盘）。

费率（单边万 2.5 佣金 + 印花税 + 过户费，忽略滑点与冲击成本）
    佣金      0.025%   双边，单笔最低 5 元
    印花税    0.05%    仅卖出
    过户费    0.001%   双边
  -> 一次来回约 0.102%

流程
----
    1. 跑选股规则，拿到顺延后的候选 20 只（防止个别买不到要用下一名补）
    2. 对每个盘：目标 = 前 n 只；卖出不在目标的，买入新进入的
    3. 成交价 = 14:50 实时价（脚本运行时刻的现价）
    4. 记账：equity / holdings / trades 三个 CSV + 一段简短日志

产物
----
    paper/<root>/
      config.json            费率与初始资金
      state.json             幂等：本月是否已记账
      n5/{equity,holdings,trades}.csv  + state.json
      n10/{...}
      picks/<日期>.csv       当期 20 只候选
      logs/<日期>.log

用法
----
    .venv\\Scripts\\python.exe paper_trade.py --force          # 本地测试，忽略日期
    .venv\\Scripts\\python.exe paper_trade.py                  # 正式：仅月末最后交易日
    .venv\\Scripts\\python.exe paper_trade.py --root paper_test --force
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:                          # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pick_live import select_candidates, snapshot_tencent   # noqa: E402

# --------------------------------------------------------------------------- 费率
FEES = {
    "commission_rate": 0.00025,      # 佣金 万 2.5，双边
    "min_commission": 5.0,           # 单笔最低 5 元
    "stamp_duty_rate": 0.0005,       # 印花税 万 5，仅卖出
    "transfer_fee_rate": 0.00001,    # 过户费 十万分之一，双边
}
INIT_CAPITAL = 200_000.0
LOT = 100

# holdings.csv 的列（整表覆盖，只留当前持仓）
HOLD_COLS = ["date", "portfolio", "code", "name", "shares", "cost_price",
             "last_price", "market_value", "weight", "pnl_pct", "price_src"]


def calc_fee(amount: float, side: str, fees: dict) -> tuple[float, float, float, float]:
    """返回 (佣金, 印花税, 过户费, 合计)。"""
    comm = max(amount * fees["commission_rate"], fees["min_commission"])
    stamp = amount * fees["stamp_duty_rate"] if side == "SELL" else 0.0
    tran = amount * fees["transfer_fee_rate"]
    return round(comm, 2), round(stamp, 2), round(tran, 2), round(comm + stamp + tran, 2)


# --------------------------------------------------------------------------- 日期
def last_weekday_of_month(d: dt.date) -> dt.date:
    last = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    while last.weekday() >= 5:
        last -= dt.timedelta(days=1)
    return last


def is_month_end_trading_day(d: dt.date) -> bool:
    """粗判：d 是否它所在月份的最后一个交易日。

    只做「本月最后一个工作日」的判断，不查节假日。真正的闸门在 main() 里
    用**行情自带的数据日**去调它，所以「月末那次被延迟到次月 1~3 号盘前」的运行
    依然能算出 data_date = 上月最后交易日，从而把上个月的账补上。
    """
    return d.weekday() < 5 and d == last_weekday_of_month(d)


# 月末窗口：每月 25 号 ~ 次月 3 号。
#   本月最后一个交易日最早可能落在 26 号（2 月 28 日是周日时），25 号开窗留一天余量；
#   次月 1~3 号盘前行情还停在上月最后交易日，属于合法的补跑窗口。
# 窗口只决定「值不值得去拉行情」，真正的判定由 data_date 决定。
WINDOW_TAIL_DAYS = 25
WINDOW_HEAD_DAYS = 3


def in_month_end_window(d: dt.date) -> bool:
    return d.day >= WINDOW_TAIL_DAYS or d.day <= WINDOW_HEAD_DAYS


def prev_nav(p: Path, date: str, default: float) -> float:
    """读 date 之前最近一条净值记录；没有历史时退化为初始资金。

    注意 CSV 是日期降序（新的在上面），所以不能取「最后一行」。
    这里先过滤再按日期升序排，取最后一条 —— 对字符串列最稳
    （Series.idxmax() 对 object dtype 会直接抛 TypeError）。
    """
    if not p.exists():
        return default
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
        df["date"] = df["date"].astype(str)
        df = df[df["date"] < str(date)]
        if len(df):
            return float(df.sort_values("date", kind="stable")["nav"].iloc[-1])
    except Exception as exc:               # noqa: BLE001
        print(f"  [!] 读取上期净值失败（{type(exc).__name__}），退回初始资金：{exc}")
    return default


# --------------------------------------------------------------------------- 工具
def load_json(p: Path, default: dict) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:                  # noqa: BLE001
            return default
    return default


def save_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _sorted_desc(df: pd.DataFrame) -> pd.DataFrame:
    """按日期降序；同一天内也倒序（后发生的在上面）。

    同一块内用 reversed() 先翻一次，再用 kind="stable" 排序 —— 这样：
      - 同一天里最后执行的那笔排最上面；
      - 其他日期的行保持文件里原有的顺序，不会被反复重写翻来翻去。
    """
    if "date" not in df.columns or not len(df):
        return df
    return df.sort_values("date", ascending=False, kind="stable").reset_index(drop=True)


def upsert_block(p: Path, rows: list[dict]) -> None:
    """按日期整块 upsert：同一天重复运行（测试/补跑）时整块覆盖，不产生重复行。

    equity / trades 走这里，保证「同一天最多一份记录」。
    整块在写入前先翻转，于是同一天内也是「后发生的在上面」（trade 日志符合券商习惯）。
    列不一致时取并集（旧文件缺新列时补空，反之亦然），避免 schema 演进时 KeyError。
    """
    if not rows:
        return                                 # 空块不动文件：同日复跑无成交＝维持原记录
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(list(reversed(rows))).astype(str)
    if p.exists():
        old = pd.read_csv(p, dtype=str)
        old = old[old["date"] != str(rows[0]["date"])]
        cols = list(dict.fromkeys([*old.columns, *df.columns]))
        df = pd.concat([old.reindex(columns=cols), df.reindex(columns=cols)],
                       ignore_index=True)
    _sorted_desc(df).to_csv(p, index=False, encoding="utf-8-sig")


def snapshot_file(p: Path, rows: list[dict], cols: list[str]) -> None:
    """整表重写：只留当前快照，不留历史（holdings.csv 用）。

    与 upsert_block 的区别是彻底覆盖，所以文件里永远只有「当前持仓」这几行。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=cols).astype(str) if rows else pd.DataFrame(columns=cols)
    df.to_csv(p, index=False, encoding="utf-8-sig")


# --------------------------------------------------------------------------- 单个盘
def run_portfolio(pf: str, n: int, kept: pd.DataFrame, date: str,
                  base: Path, fees: dict, log: list[str],
                  snap_all: pd.DataFrame | None = None) -> dict:
    d = base / pf
    d.mkdir(parents=True, exist_ok=True)
    st = load_json(d / "state.json",
                   {"cash": INIT_CAPITAL, "positions": {}, "initial": INIT_CAPITAL})

    # 先用全量快照建价目表（覆盖所有持仓，含已跌出候选名单的），再用候选名单覆盖
    price: dict[str, float] = {}
    name: dict[str, str] = {}
    if snap_all is not None and len(snap_all):
        price = {str(c): float(p) for c, p in zip(snap_all["code"], snap_all["price"])}
        name = {str(c): str(nm) for c, nm in zip(snap_all["code"], snap_all["name"])}
    price.update({r.code: float(r.price) for r in kept.itertuples()})
    name.update({r.code: str(r.name) for r in kept.itertuples()})

    # 目标：按市值升序取前 n 只「有有效报价」的（顺延机制）
    target, skipped = [], []
    for r in kept.itertuples():
        p = price.get(r.code, 0.0)
        if not (p > 0):
            skipped.append(r.code)
            continue
        target.append(r.code)
        if len(target) >= n:
            break

    pos = {c: dict(v) for c, v in st["positions"].items()}
    cash = float(st["cash"])
    # 上一期净值用于算「本期收益率」。绝不能像以前那样用「当日价重估 → 当日价重估」
    # 的差，那样相减之后只剩手续费，收益率永远是 0。
    nav_prev = prev_nav(d / "equity.csv", date, float(st.get("initial", INIT_CAPITAL)))

    sells = [c for c in pos if c not in target]
    buys_pre = [c for c in target if c not in pos]

    # 成交先攒在内存，跑完统一按日期整块写盘：
    # 中途异常退出不会留下半截成交，同日复跑也不会产生重复行。
    trades: list[dict] = []

    # --- 卖出 ---
    n_trades = 0
    for c in sells:
        p = price.get(c)
        if not p or p <= 0:
            log.append(f"  [{pf}] 卖出跳过 {c}（无报价：停牌或已退市）")
            continue
        sh = int(pos[c]["shares"])
        amt = sh * p
        comm, stamp, tran, fee = calc_fee(amt, "SELL", fees)
        cash += amt - fee
        trades.append({
            "date": date, "portfolio": pf, "code": c, "name": name.get(c, pos[c].get("name", "")),
            "side": "SELL", "price": round(p, 3), "shares": sh, "amount": round(amt, 2),
            "commission": comm, "stamp_duty": stamp, "transfer_fee": tran,
            "fee_total": fee, "cash_after": round(cash, 2), "note": "跌出目标",
        })
        log.append(f"  [{pf}] 卖 {c} {sh}股 @{p:.2f} 金额{amt:,.0f} 费用{fee:.2f}")
        pos.pop(c)
        n_trades += 1

    # --- 买入 ---
    if buys_pre:
        per = (cash + sum(pos[c]["shares"] * price.get(c, 0) for c in pos)) / n
        for c in buys_pre:
            p = price.get(c)
            if not p or p <= 0:
                log.append(f"  [{pf}] 买入跳过 {c}（无报价，顺延）")
                continue
            sh = int(per // (p * LOT)) * LOT
            if sh < LOT:
                log.append(f"  [{pf}] 买入跳过 {c}（不足 1 手，预留现金）")
                continue
            amt = sh * p
            comm, stamp, tran, fee = calc_fee(amt, "BUY", fees)
            # 以前只减一手就完事，减完不复查；现金极薄时仍可能透支成负现金。
            while sh >= LOT and amt + fee > cash:
                sh -= LOT
                amt = sh * p
                comm, stamp, tran, fee = calc_fee(amt, "BUY", fees)
            if sh < LOT:
                log.append(f"  [{pf}] 买入跳过 {c}（现金不足）")
                continue
            cash -= amt + fee
            pos[c] = {"shares": sh, "cost": round(p, 3), "name": name.get(c, "")}
            trades.append({
                "date": date, "portfolio": pf, "code": c, "name": name.get(c, ""),
                "side": "BUY", "price": round(p, 3), "shares": sh, "amount": round(amt, 2),
                "commission": comm, "stamp_duty": stamp, "transfer_fee": tran,
                "fee_total": fee, "cash_after": round(cash, 2), "note": "进入目标",
            })
            log.append(f"  [{pf}] 买 {c} {sh}股 @{p:.2f} 金额{amt:,.0f} 费用{fee:.2f}")
            n_trades += 1

    # --- 快照 ---
    # 关键：拿不到报价的持仓以前会被静默按 cost 估值，holdings.csv 里就出现
    # last_price == cost_price 的「平盘」假象，看上去和当天刚买入的行一模一样。
    # 现在多打一列 price_src 把这件事显式化，并在日志里点名。
    mv = 0.0
    ghosts = []
    marked: list[tuple[str, float, str]] = []
    for c in sorted(pos):
        p = price.get(c)
        if p is None or not (float(p) > 0):
            p, src = float(pos[c]["cost"]), "cost"      # 兜底估值，不是真实市值
            ghosts.append(c)
        else:
            p, src = float(p), "market"
        mv += pos[c]["shares"] * p
        marked.append((c, p, src))
    nav = cash + mv

    hrows = []
    for c, p, src in marked:
        cost = float(pos[c]["cost"])
        v = pos[c]["shares"] * p
        hrows.append({
            "date": date, "portfolio": pf, "code": c,
            "name": name.get(c, pos[c].get("name", "")),
            "shares": int(pos[c]["shares"]), "cost_price": cost,
            "last_price": round(p, 3),
            "market_value": round(v, 2),
            # weight 的分母是 NAV（含现金），所以各只相加会略小于 1，差额就是现金
            "weight": round(v / nav, 4) if nav else 0.0,
            "pnl_pct": round((p / cost - 1) * 100, 3) if cost else 0.0,
            "price_src": src,
        })
    if ghosts:
        log.append(f"  [{pf}] ! {len(ghosts)} 只无报价，按成本估值（市值不可信）："
                   + "  ".join(f"{c}@{pos[c]['cost']}" for c in ghosts))
    # holdings.csv 只保留当前持仓快照，不留历史（整表覆盖）
    snapshot_file(d / "holdings.csv", hrows, HOLD_COLS)
    fee_paid = sum(t["fee_total"] for t in trades)
    ret_pct = round((nav / nav_prev - 1) * 100, 3) if nav_prev else 0.0
    upsert_block(d / "equity.csv", [{
        "date": date, "cash": round(cash, 2), "market_value": round(mv, 2),
        "nav": round(nav, 2), "nav_index": round(nav / st["initial"], 6),
        "positions": len(pos), "trades": n_trades,
        "ret_pct": ret_pct,
        "fee_pct": round(fee_paid / nav * 100, 3) if nav else 0.0,
    }])
    upsert_block(d / "trades.csv", trades)

    st["cash"] = cash
    st["positions"] = pos
    save_json(d / "state.json", st)

    log.append(f"  [{pf}] NAV {nav:,.2f}（指数 {nav/st['initial']:.4f}） "
               f"本期 {ret_pct:+.2f}%  现金 {cash:,.0f} 持仓 {len(pos)} 只 "
               f"成交 {n_trades} 笔 费用 {fee_paid:.2f}")
    return {"nav": nav, "nav_index": nav / st["initial"], "positions": len(pos),
            "trades": n_trades, "skipped": skipped, "ret_pct": ret_pct,
            "ghosts": ghosts}


# --------------------------------------------------------------------------- 每日估值
def mark_portfolio(pf: str, base: Path, date: str, log: list[str],
                   price: dict[str, float], name: dict[str, str]) -> dict | None:
    """只按今日报价给持仓估值，追加一行净值 —— 不成交、不改 state。

    这是「每日估值」的核心：策略仍然只在月末调仓，但净值曲线每天都有点，
    否则一个月只有一个点，中间的回撤完全看不到。
    """
    d = base / pf
    st = load_json(d / "state.json", None)
    if not st or not st.get("positions"):
        log.append(f"  [{pf}] 估值跳过：还没有持仓（等月末建仓）")
        return None

    # 这个交易日已经记过就不要再覆盖：周末重复跑、或月末真调仓刚写过
    eqp = d / "equity.csv"
    if eqp.exists():
        try:
            old = pd.read_csv(eqp, encoding="utf-8-sig")
            if (old["date"].astype(str) == str(date)).any():
                log.append(f"  [{pf}] 估值跳过：{date} 已有记录")
                return None
        except Exception:                  # noqa: BLE001
            pass

    pos = st["positions"]
    cash = float(st["cash"])
    mv = 0.0
    marked: list[tuple[str, float, str]] = []
    ghosts = []
    for c in sorted(pos):
        p = price.get(c)
        if p is None or not (float(p) > 0):
            p, src = float(pos[c]["cost"]), "cost"
            ghosts.append(c)
        else:
            p, src = float(p), "market"
        mv += pos[c]["shares"] * p
        marked.append((c, p, src))
    nav = cash + mv
    nav_prev = prev_nav(eqp, date, float(st.get("initial", INIT_CAPITAL)))
    ret_pct = round((nav / nav_prev - 1) * 100, 3) if nav_prev else 0.0

    hrows = []
    for c, p, src in marked:
        cost = float(pos[c]["cost"])
        v = pos[c]["shares"] * p
        hrows.append({
            "date": date, "portfolio": pf, "code": c,
            "name": name.get(c, pos[c].get("name", "")),
            "shares": int(pos[c]["shares"]), "cost_price": cost,
            "last_price": round(p, 3),
            "market_value": round(v, 2),
            "weight": round(v / nav, 4) if nav else 0.0,
            "pnl_pct": round((p / cost - 1) * 100, 3) if cost else 0.0,
            "price_src": src,
        })
    snapshot_file(d / "holdings.csv", hrows, HOLD_COLS)
    upsert_block(eqp, [{
        "date": date, "cash": round(cash, 2), "market_value": round(mv, 2),
        "nav": round(nav, 2), "nav_index": round(nav / st["initial"], 6),
        "positions": len(pos), "trades": 0,
        "ret_pct": ret_pct,
        "fee_pct": 0.0,
    }])

    if ghosts:
        log.append(f"  [{pf}] ! {len(ghosts)} 只无报价，按成本估值："
                   + "  ".join(f"{c}@{pos[c]['cost']}" for c in ghosts))
    log.append(f"  [{pf}] NAV {nav:,.2f}（指数 {nav/st['initial']:.4f}） "
               f"本期 {ret_pct:+.2f}%  持仓 {len(pos)} 只  —— 仅估值，未成交")
    return {"nav": nav, "nav_index": nav / st["initial"], "positions": len(pos),
            "ret_pct": ret_pct}


# --------------------------------------------------------------------------- 每日估值入口
def quote_date(snap: pd.DataFrame, head: list[str]) -> str:
    """从行情自带的时间戳 f[30] 解析出「这笔报价属于哪个交易日」。"""
    if "quote_time" in snap.columns:
        vals = [str(x) for x in snap["quote_time"]
                if str(x).isdigit() and len(str(x)) >= 8]
        if vals:
            qt = max(vals)
            return f"{qt[0:4]}-{qt[4:6]}-{qt[6:8]}"
    head.append("  [!] 行情没有时间戳，按今天记账")
    return ""


def mark_only_main(base: Path, args, today: dt.date, head: list[str]) -> int:
    """每日估值：只给已有持仓按今日报价记一行净值。

    为什么要单独一条路径：
      - 策略只在月末调仓，所以那个入口一个月才放行一次；
      - 但净值曲线需要每天一个点，否则中间的回撤完全看不见。
    代价很小：只拉持仓那 5~10 只的报价（1 次请求），不拉全市场、不拉 K 线。
    """
    per: dict[str, dict] = {}
    codes: set[str] = set()
    for pf_n in args.n:
        st = load_json(base / f"n{pf_n}" / "state.json", None)
        if st and st.get("positions"):
            per[f"n{pf_n}"] = st
            codes.update(st["positions"])

    if not codes:
        print(f"[mark] {args.root} 还没有持仓（等月末建仓），跳过当日估值")
        return 0

    try:
        snap = snapshot_tencent(sorted(codes), quiet=True)
    except Exception as exc:               # noqa: BLE001
        print(f"  [X] 拉报价失败：{exc}")
        return 2
    if snap is None or snap.empty:
        print("  [X] 拉不到报价，跳过当日估值")
        return 2

    date = args.date or quote_date(snap, head)
    if not date:
        date = today.isoformat()
    try:
        if dt.date.fromisoformat(date).weekday() >= 5:
            print(f"[mark] {date} 是周末，跳过当日估值")
            return 0
    except ValueError:
        pass

    price = {str(c): float(p) for c, p in zip(snap["code"], snap["price"])}
    name = {str(c): str(n) for c, n in zip(snap["code"], snap["name"])}

    head[1] = f"记账日期 {date}"
    head.append(f"每日估值 | 报价日 {date} | 持仓报价 {len(price)} 只")

    log = list(head)
    for pf in per:
        mark_portfolio(pf, base, date, log, price, name)

    (base / "logs").mkdir(parents=True, exist_ok=True)
    lf = base / "logs" / f"{date}.log"
    if not lf.exists():
        # 只有当天没有别的日志时才写；月末真调仓那份日志不能被估值覆盖掉
        lf.write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    return 0


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="paper", help="数据目录（测试可用 paper_test）")
    ap.add_argument("--n", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--cand", type=int, default=20, help="候选数（顺延备用）")
    ap.add_argument("--capital", type=float, default=INIT_CAPITAL)
    ap.add_argument("--date", default="", help="覆盖记账日期 YYYY-MM-DD（测试用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略「是否月末最后交易日」和「本月已记账」两道检查")
    ap.add_argument("--skip-if-ran-today", action="store_true",
                    help="今天已经跑过就退出（给每周测试跑用，避免同日重复调仓）")
    ap.add_argument("--skip-if-recorded", action="store_true",
                    help="这个交易日已经记过账就退出（避免周末把周五那行的成交清零）")
    ap.add_argument("--mark-only", action="store_true",
                    help="只做每日估值：按今日报价给持仓记一行净值，不成交、不改 state")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--cut", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="只选股，不写模拟盘")
    args = ap.parse_args()

    base = ROOT / args.root
    today = dt.date.today()
    run_date = args.date or today.isoformat()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = [f"模拟盘运行 {stamp}", f"记账日期 {run_date}"]
    sm = load_json(base / "state.json", {})

    # ── 每日估值：绕过下面所有月份的闸门，只算净值不动仓
    if args.mark_only:
        return mark_only_main(base, args, today, head)

    # ── 闸门顺序很重要 ────────────────────────────────────────────────
    # 1) 同日复跑保护：每周测试跑 + 手动重跑同一天时不要二次调仓
    if args.skip_if_ran_today and not args.date:
        if str(sm.get("last_run", ""))[:10] == today.isoformat():
            print(f"[skip] 今天已跑过（上次 {sm.get('last_run')}），跳过重复测试跑")
            return 0

    # 2) 廉价闸门：只有落在「可能的月末」窗口内才值得去拉一遍全市场行情
    if not args.force and not args.date and not in_month_end_window(today):
        print(f"[skip] {today} 不在月末窗口（每月 {WINDOW_TAIL_DAYS} 号 ~ 次月 "
              f"{WINDOW_HEAD_DAYS} 号），退出")
        return 0

    # 2b) 窗口内的廉价幂等预判：判据只有「本月已记账」和「次月头几天且上月已记账」。
    #     这条捷径只用来省掉一次全市场行情拉取，绝不会挡住该记的账——
    #     真正的闸门仍是下面拿到 data_date 之后的那一道。
    if not args.force and not args.date and not args.dry_run:
        cur_key = today.strftime("%Y-%m")
        prev_key = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        done = sm.get("last_month")
        if done == cur_key or (today.day <= WINDOW_HEAD_DAYS and done == prev_key):
            print(f"[skip] {done} 已记账（上次 {sm.get('last_run')}），本月无需再跑")
            return 0

    print(f"  候选池 {args.cand} 只，组合 " + "/".join(f"n={x}" for x in args.n))

    # --- 选股 ---
    try:
        res = select_candidates(args.cand, args.window, args.cut, quiet=True)
    except RuntimeError as exc:
        print(f"  [X] 选股失败：{exc}")
        return 2
    kept, stt = res["kept"], res["stats"]
    if len(kept) < max(args.n):
        print(f"  [X] 可用候选只有 {len(kept)} 只，少于 max(n)={max(args.n)}")
        return 2
    # data_date 可能是 str / date / Timestamp（取决于行情源给的 index 类型），
    # 统一成 'YYYY-MM-DD' 字符串，后面所有比较和记账都按这个口径走。
    data_date = stt["data_date"]
    data_date = str(data_date)[:10] if data_date else run_date
    # 非交易时段/休市运行时，行情是上一个交易日的收盘价。
    # 记账日期必须跟着数据日走，否则净值序列会多出一条无效行（例如周六）。
    if not args.date and data_date != run_date:
        try:
            gap = (dt.date.fromisoformat(run_date) - dt.date.fromisoformat(data_date)).days
        except Exception:
            gap = 0
        print(f"  [!] 数据日 {data_date} != 今天 {run_date}（非交易时段或休市）")
        print(f"      记账日期改用数据日 {data_date}，避免净值序列出现无效行")
        if gap > 7:
            print(f"  [!] 数据日已过期 {gap} 天，确认行情源是否正常")
        run_date = data_date
        head[1] = f"记账日期 {run_date}"

    # 3) 正式闸门：用**行情自带的交易日**判断，而不是日历日。
    #    这样月末那次被延迟到次月 1~3 号盘前的运行，data_date 仍停在上月最后
    #    交易日，判定为真 -> 把上个月的账补上；而次月 1~3 号盘后运行时
    #    data_date 已是新月份 -> 判定为假 -> 正常跳过。
    if not args.force and not args.date:
        try:
            gate_day = dt.date.fromisoformat(run_date)
        except ValueError:
            gate_day = today
        if not is_month_end_trading_day(gate_day):
            print(f"[skip] 数据日 {run_date} 不是它所在月份的最后一个交易日，退出"
                  f"（用 --force 可强制运行）")
            return 0

    # 4) 幂等键必须用**定稿后**的 run_date。以前在 data_date 校正之前就取，
    #    跨月补跑会把 last_month 写成下个月，于是下个月的正式跑被自己的幂等挡死。
    month_key = run_date[:7]

    # 5) 这个交易日已经记过就退出。
    #    paper_test 每天 --force 跑，周末的 data_date 还是周五，
    #    不挡的话会把周五那行的 trades 覆盖成 0，成交记录就丢了。
    if args.skip_if_recorded and not args.date and not args.dry_run:
        if str(sm.get("data_date") or "") == str(run_date):
            print(f"[skip] 交易日 {run_date} 已经记过账，跳过（避免覆盖当日成交）")
            return 0

    if sm.get("last_month") == month_key and not args.dry_run and not args.force:
        print(f"[skip] {month_key} 已记账（上次 {sm.get('last_run')}），退出")
        return 0

    head.append(f"数据日 {data_date} | 快照有效 {stt['n_valid']} | 剔退市/停牌 "
                f"{stt['n_delisted']} | 剔涨停 {stt['n_limitup']} | "
                f"剔近{args.window}日涨停>={args.cut} {stt['n_dropped_rule']}")

    if args.dry_run:
        pk = kept.head(args.cand).copy()
        pk["mktcap_亿"] = (pk["mktcap"] / 1e8).round(2)
        print(pk[["rank", "code", "name", "price", "mktcap_亿", "nlu"]]
              .head(max(args.n)).to_string(index=False))
        return 0

    # --- 到这里才真的可能落盘：先建目录、初始化 config/state ---
    base.mkdir(parents=True, exist_ok=True)
    cfg_p = base / "config.json"
    cfg = load_json(cfg_p, {})
    if not cfg:
        cfg = {"fees": FEES, "initial_capital": args.capital,
               "portfolios": args.n, "created": today.isoformat()}
        save_json(cfg_p, cfg)
        for pf in args.n:
            save_json(base / f"n{pf}" / "state.json",
                      {"cash": args.capital, "positions": {}, "initial": args.capital})
    fees = {**FEES, **cfg.get("fees", {})}

    # --- 落盘候选 ---
    (base / "picks").mkdir(exist_ok=True)
    pk = kept.head(args.cand).copy()
    pk["mktcap_亿"] = (pk["mktcap"] / 1e8).round(2)
    pk["owner"] = pk["code"].map(res["kind"])
    pk.drop(columns=["mktcap", "is_lu"], errors="ignore").to_csv(
        base / "picks" / f"{run_date}.csv", index=False, encoding="utf-8-sig")

    # --- 跑各盘 ---
    log = list(head)
    summary = {}
    for pf_n in args.n:
        pf = f"n{pf_n}"
        log.append("")
        log.append(f"--- {pf} ---")
        summary[pf] = run_portfolio(pf, pf_n, kept, run_date, base, fees, log,
                                    snap_all=res.get("snap"))

    sm["last_month"] = month_key
    sm["last_run"] = stamp
    sm["data_date"] = data_date
    save_json(base / "state.json", sm)

    log.append("")
    for pf, v in summary.items():
        log.append(f"  {pf}: NAV {v['nav']:,.2f}  指数 {v['nav_index']:.4f}  "
                   f"持仓 {v['positions']}  本期 {v['ret_pct']:+.2f}%")
    (base / "logs").mkdir(exist_ok=True)
    (base / "logs" / f"{run_date}.log").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
