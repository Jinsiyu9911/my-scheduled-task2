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

from pick_live import select_candidates   # noqa: E402

# --------------------------------------------------------------------------- 费率
FEES = {
    "commission_rate": 0.00025,      # 佣金 万 2.5，双边
    "min_commission": 5.0,           # 单笔最低 5 元
    "stamp_duty_rate": 0.0005,       # 印花税 万 5，仅卖出
    "transfer_fee_rate": 0.00001,    # 过户费 十万分之一，双边
}
INIT_CAPITAL = 200_000.0
LOT = 100


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
    """粗判：今天是否本月最后一个交易日。

    只做「今天 = 本月最后一个工作日」的判断；若该日恰逢节假日，需要 --force 手动跑，
    或者由下一交易日的前一次运行补上（脚本有幂等保护，同一个月不会记两次）。
    """
    return d.weekday() < 5 and d == last_weekday_of_month(d)


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


def append_csv(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    header = not p.exists()
    df.to_csv(p, mode="a", header=header, index=False, encoding="utf-8-sig")


def upsert_block(p: Path, rows: list[dict]) -> None:
    """按日期整块 upsert：同一天重复运行（测试）时整块覆盖，不产生重复行。"""
    if not rows:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).astype(str)
    if p.exists():
        old = pd.read_csv(p, dtype=str)
        old = old[old["date"] != str(rows[0]["date"])]
        df = pd.concat([old[df.columns], df], ignore_index=True)
    df.to_csv(p, index=False, encoding="utf-8-sig")


# --------------------------------------------------------------------------- 单个盘
def run_portfolio(pf: str, n: int, kept: pd.DataFrame, date: str,
                  base: Path, fees: dict, log: list[str]) -> dict:
    d = base / pf
    d.mkdir(parents=True, exist_ok=True)
    st = load_json(d / "state.json",
                   {"cash": INIT_CAPITAL, "positions": {}, "initial": INIT_CAPITAL})

    price = {r.code: float(r.price) for r in kept.itertuples()}
    name = {r.code: str(r.name) for r in kept.itertuples()}
    lu = {r.code: float(r.limit_up) for r in kept.itertuples()}
    ld = {r.code: float(r.limit_down) for r in kept.itertuples()}

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
    nav_before = cash + sum(pos[c]["shares"] * price.get(c, pos[c]["cost"]) for c in pos)

    sells = [c for c in pos if c not in target]
    buys_pre = [c for c in target if c not in pos]

    # --- 卖出 ---
    n_trades = 0
    for c in sells:
        p = price.get(c)
        if not p or p <= 0:
            log.append(f"  [{pf}] 卖出跳过 {c}（无报价）")
            continue
        sh = int(pos[c]["shares"])
        amt = sh * p
        comm, stamp, tran, fee = calc_fee(amt, "SELL", fees)
        cash += amt - fee
        append_csv(d / "trades.csv", {
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
            if amt + fee > cash:
                sh -= LOT
                if sh < LOT:
                    log.append(f"  [{pf}] 买入跳过 {c}（现金不足）")
                    continue
                amt = sh * p
                comm, stamp, tran, fee = calc_fee(amt, "BUY", fees)
            cash -= amt + fee
            pos[c] = {"shares": sh, "cost": round(p, 3), "name": name.get(c, "")}
            append_csv(d / "trades.csv", {
                "date": date, "portfolio": pf, "code": c, "name": name.get(c, ""),
                "side": "BUY", "price": round(p, 3), "shares": sh, "amount": round(amt, 2),
                "commission": comm, "stamp_duty": stamp, "transfer_fee": tran,
                "fee_total": fee, "cash_after": round(cash, 2), "note": "进入目标",
            })
            log.append(f"  [{pf}] 买 {c} {sh}股 @{p:.2f} 金额{amt:,.0f} 费用{fee:.2f}")
            n_trades += 1

    # --- 快照 ---
    mv = sum(pos[c]["shares"] * price.get(c, pos[c]["cost"]) for c in pos)
    nav = cash + mv
    hrows = []
    for c in sorted(pos):
        p = price.get(c, pos[c]["cost"])
        hrows.append({
            "date": date, "portfolio": pf, "code": c,
            "name": name.get(c, pos[c].get("name", "")),
            "shares": int(pos[c]["shares"]), "cost_price": pos[c]["cost"],
            "last_price": round(float(p), 3),
            "market_value": round(pos[c]["shares"] * float(p), 2),
            "weight": round(pos[c]["shares"] * float(p) / nav, 4) if nav else 0.0,
        })
    upsert_block(d / "holdings.csv", hrows)
    upsert_block(d / "equity.csv", [{
        "date": date, "cash": round(cash, 2), "market_value": round(mv, 2),
        "nav": round(nav, 2), "nav_index": round(nav / st["initial"], 6),
        "positions": len(pos), "trades": n_trades,
        "ret_pct": round((nav / nav_before - 1) * 100, 3) if nav_before else 0.0,
    }])

    st["cash"] = cash
    st["positions"] = pos
    save_json(d / "state.json", st)

    log.append(f"  [{pf}] NAV {nav:,.2f}（指数 {nav/st['initial']:.4f}） "
               f"现金 {cash:,.0f} 持仓 {len(pos)} 只 成交 {n_trades} 笔")
    return {"nav": nav, "nav_index": nav / st["initial"], "positions": len(pos),
            "trades": n_trades, "skipped": skipped}


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="paper", help="数据目录（测试可用 paper_test）")
    ap.add_argument("--n", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--cand", type=int, default=20, help="候选数（顺延备用）")
    ap.add_argument("--capital", type=float, default=INIT_CAPITAL)
    ap.add_argument("--date", default="", help="覆盖记账日期 YYYY-MM-DD（测试用）")
    ap.add_argument("--force", action="store_true", help="忽略「是否月末最后交易日」检查")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--cut", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="只选股，不写模拟盘")
    args = ap.parse_args()

    base = ROOT / args.root
    base.mkdir(parents=True, exist_ok=True)
    cfg_p = base / "config.json"
    cfg = load_json(cfg_p, {})
    if not cfg:
        cfg = {"fees": FEES, "initial_capital": args.capital,
               "portfolios": args.n, "created": dt.date.today().isoformat()}
        save_json(cfg_p, cfg)
        for pf in args.n:
            save_json(base / f"n{pf}" / "state.json",
                      {"cash": args.capital, "positions": {}, "initial": args.capital})
    fees = {**FEES, **cfg.get("fees", {})}

    today = dt.date.today()
    run_date = args.date or today.isoformat()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = [f"模拟盘运行 {stamp}", f"记账日期 {run_date}"]

    # --- 日期闸门 ---
    if not args.force and not args.date:
        if not is_month_end_trading_day(today):
            print(f"[skip] {today} 不是本月最后一个工作日，退出（用 --force 可强制运行）")
            return 0
    sm = load_json(base / "state.json", {})
    month_key = run_date[:7]
    # --force 同时绕过「本月已记账」的幂等保护，方便反复测试
    if sm.get("last_month") == month_key and not args.dry_run and not args.force:
        print(f"[skip] {month_key} 已记账（上次 {sm.get('last_run')}），退出")
        return 0

    print(f"  候选池 {args.cand} 只，组合 " + "/".join(f"n={x}" for x in args.n))

    # --- 选股 ---
    try:
        res = select_candidates(args.cand, args.window, args.cut, quiet=True)
    except RuntimeError as exc:
        print(f"  [X] 选股失败：{exc}")
        return 2
    kept, st = res["kept"], res["stats"]
    if len(kept) < max(args.n):
        print(f"  [X] 可用候选只有 {len(kept)} 只，少于 max(n)={max(args.n)}")
        return 2
    data_date = st["data_date"] or run_date
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
    head.append(f"数据日 {data_date} | 快照有效 {st['n_valid']} | 剔退市 {st['n_delisted']}"
                f" | 剔涨停 {st['n_limitup']} | 剔近{args.window}日涨停>={args.cut} "
                f"{st['n_dropped_rule']}")

    # --- 落盘候选 ---
    (base / "picks").mkdir(exist_ok=True)
    pk = kept.head(args.cand).copy()
    pk["mktcap_亿"] = (pk["mktcap"] / 1e8).round(2)
    pk["owner"] = pk["code"].map(res["kind"])
    pk.drop(columns=["mktcap", "is_lu"], errors="ignore").to_csv(
        base / "picks" / f"{run_date}.csv", index=False, encoding="utf-8-sig")

    if args.dry_run:
        print(pk[["rank", "code", "name", "price", "mktcap_亿", "nlu"]]
              .head(max(args.n)).to_string(index=False))
        return 0

    # --- 跑各盘 ---
    log = list(head)
    summary = {}
    for pf_n in args.n:
        pf = f"n{pf_n}"
        log.append("")
        log.append(f"--- {pf} ---")
        summary[pf] = run_portfolio(pf, pf_n, kept, run_date, base, fees, log)

    sm["last_month"] = month_key
    sm["last_run"] = stamp
    sm["data_date"] = data_date
    save_json(base / "state.json", sm)

    log.append("")
    for pf, v in summary.items():
        log.append(f"  {pf}: NAV {v['nav']:,.2f}  指数 {v['nav_index']:.4f}  "
                   f"持仓 {v['positions']}")
    (base / "logs").mkdir(exist_ok=True)
    (base / "logs" / f"{run_date}.log").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
