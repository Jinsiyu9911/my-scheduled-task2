"""月末最后一个交易日 14:50 运行的实盘选股脚本（全实时行情）。

用法
----
    .venv\\Scripts\\python.exe pick_live.py
    .venv\\Scripts\\python.exe pick_live.py --n 5 10 20 --hold holdings.txt

规则（与回测完全一致）
--------------------
    1. 池子 = 沪深主板（60/00）∩ 实际控制人为央企或地方国企
    2. 剔除当日停牌 / 无报价 / 退市整理期
    3. 按总市值升序
    4. 剔除当日已涨停（买不到）
    5. 剔除「过去 20 个交易日涨停次数 >= 2」
    6. 顺延补足，输出 n=5/10/20

数据来源
--------
    行情    腾讯 qt.gtimg.cn（快照）+ web.ifzq.gtimg.cn（日线）
            失败时自动切东方财富 push2 / push2his
    股权    refdata/soe_owners.csv（静态快照，随仓库分发）
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

try:                                       # Windows 控制台默认 GBK
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:                          # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
OWNER_CSV = ROOT / "refdata" / "soe_owners.csv"
OWNER_PARQUET = ROOT / "data" / "research" / "owner_final.parquet"
OUT_DIR = ROOT / "out"
CACHE_DIR = OUT_DIR / "cache"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TX_H = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
EM_H = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

EM_HOSTS = ["push2his.eastmoney.com", "13.push2his.eastmoney.com",
            "82.push2his.eastmoney.com", "1.push2his.eastmoney.com"]


# --------------------------------------------------------------------------- HTTP
def _get_text(url: str, params: dict | None = None, headers: dict | None = None,
              encoding: str | None = None, tries: int = 5) -> str | None:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            txt = r.text
            if txt:
                return txt
        except Exception:                  # noqa: BLE001
            pass
        time.sleep(min(1.2 * (i + 1), 6.0))
    return None


def _get_json(url: str, params: dict | None = None, headers: dict | None = None,
              tries: int = 5):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:                  # noqa: BLE001
            pass
        time.sleep(min(1.2 * (i + 1), 6.0))
    return None


# --------------------------------------------------------------------------- 工具
def is_main_board(code: str) -> bool:
    """只要沪深主板：60xxxx / 00xxxx。"""
    return len(code) == 6 and (code.startswith("60") or code.startswith("00"))


def tx_prefix(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def load_owners() -> tuple[set[str], dict[str, str]]:
    """返回 (央国企代码集合, 代码->性质)。优先 refdata CSV，其次本地 parquet。"""
    if OWNER_CSV.exists():
        o = pd.read_csv(OWNER_CSV, dtype={"code": str})
    elif OWNER_PARQUET.exists():
        o = pd.read_parquet(OWNER_PARQUET)
    else:
        raise FileNotFoundError(f"缺少股权名单：{OWNER_CSV} 或 {OWNER_PARQUET}")
    o["code"] = o["code"].astype(str).str.zfill(6)
    kind = dict(zip(o["code"], o["owner_type"]))
    soe = set(o.loc[o["owner_type"].isin(("央企", "地方国企")), "code"])
    return soe, kind


# --------------------------------------------------------------------------- 快照
def snapshot_tencent(codes: list[str], batch: int = 60, quiet: bool = True) -> pd.DataFrame:
    """腾讯批量行情。字段：1名称 2代码 3现价 4昨收 45总市值(亿) 47涨停价 48跌停价"""
    rows: list[dict] = []
    groups = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    for gi, grp in enumerate(groups, 1):
        q = ",".join(tx_prefix(c) for c in grp)
        txt = _get_text("https://qt.gtimg.cn/q=" + q, headers=TX_H, encoding="gbk",
                        tries=3)
        if not txt:
            continue
        for seg in txt.split(";"):
            seg = seg.strip()
            if "=" not in seg:
                continue
            body = seg.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 49:
                continue
            rows.append({
                "code": str(f[2]).zfill(6),
                "name": f[1],
                "price": _f(f[3]),
                "prev_close": _f(f[4]),
                "mktcap": _f(f[45]) * 1e8,        # 亿元 -> 元
                "limit_up": _f(f[47]),
                "limit_down": _f(f[48]),
            })
        if not quiet and gi % 5 == 0:
            print(f"    ... 快照 {gi}/{len(groups)} 批", flush=True)
        time.sleep(0.05)
    return pd.DataFrame(rows)


def snapshot_eastmoney() -> pd.DataFrame:
    """东财全市场快照（备份）。"""
    rows: list[dict] = []
    for page in range(1, 45):
        j = _get_json("https://push2.eastmoney.com/api/qt/clist/get", {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f20", "fs": "m:1+t:2,m:0+t:6",
            "fields": "f2,f3,f12,f14,f18,f20",
        }, EM_H, tries=2)
        d = ((j or {}).get("data") or {}) if isinstance(j, dict) else {}
        diff = d.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        if len(rows) >= int(d.get("total") or 0):
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f18": "prev_close",
        "f20": "mktcap"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    for c in ("price", "prev_close", "mktcap"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["limit_up"] = (df["prev_close"] * 1.10).round(2)
    lu5 = (df["prev_close"] * 1.05).round(2)
    df.loc[(df["price"] - lu5).abs() < 0.006, "limit_up"] = lu5
    df["limit_down"] = (df["prev_close"] * 0.90).round(2)
    return df


# --------------------------------------------------------------------------- 日线
def closes_tencent(code: str, lmt: int = 30) -> pd.Series | None:
    """腾讯日线。注意 web.ifzq 子域常返回 501，ifzq.gtimg.cn 更稳。"""
    sym = tx_prefix(code)
    for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
        j = _get_json(f"https://{host}/appstock/app/fqkline/get",
                      {"param": f"{sym},day,,,{lmt},"}, TX_H, tries=2)
        d = ((j or {}).get("data") or {}).get(sym) or {} if isinstance(j, dict) else {}
        bars = d.get("day") or d.get("qfqday") or []
        if not bars:
            continue
        out = {}
        for b in bars:
            try:
                out[b[0]] = float(b[2])    # [date, open, close, high, low, vol]
            except (IndexError, ValueError):
                continue
        if out:
            return pd.Series(out).sort_index()
    return None


def closes_sina(code: str, lmt: int = 30) -> pd.Series | None:
    """新浪日线（第三兜底）。"""
    j = _get_json(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        {"symbol": tx_prefix(code), "scale": "240", "ma": "no", "datalen": str(lmt)},
        {"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}, tries=2)
    if not isinstance(j, list) or not j:
        return None
    out = {}
    for b in j:
        try:
            out[b["day"]] = float(b["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return pd.Series(out).sort_index() if out else None


def closes_eastmoney(code: str, lmt: int = 30) -> pd.Series | None:
    secid = ("1." if code.startswith("6") else "0.") + code
    for scheme in ("http", "https"):
        for h in EM_HOSTS:
            j = _get_json(f"{scheme}://{h}/api/qt/stock/kline/get", {
                "secid": secid, "fields1": "f1,f2,f3", "fields2": "f51,f53",
                "klt": 101, "fqt": 0, "end": "20500101", "lmt": lmt,
            }, EM_H, tries=1)
            kl = ((j or {}).get("data") or {}).get("klines") or [] \
                if isinstance(j, dict) else []
            if kl:
                out = {}
                for line in kl:
                    p = line.split(",")
                    if len(p) >= 2:
                        try:
                            out[p[0]] = float(p[1])
                        except ValueError:
                            pass
                if out:
                    return pd.Series(out).sort_index()
    return None


def closes_of(code: str, lmt: int = 30) -> pd.Series | None:
    """三源兜底：腾讯 ifzq -> 新浪 -> 东财。"""
    s = closes_tencent(code, lmt)
    if s is not None and len(s) >= 5:
        return s
    s = closes_sina(code, lmt)
    if s is not None and len(s) >= 5:
        return s
    return closes_eastmoney(code, lmt)


# --------------------------------------------------------------------------- 判定
def count_limit_up(closes: pd.Series, window: int = 20) -> tuple[int, int]:
    """(涨停次数, 窗口内可用交易日数)。主板 10% / ST 5%，按收盘价精确判定。"""
    if closes is None or len(closes) < 2:
        return 0, 0
    v = closes.values
    start = max(1, len(v) - window)
    n = used = 0
    for i in range(start, len(v)):
        used += 1
        pc = v[i - 1]
        if pc <= 0:
            continue
        p10, p05 = round(pc * 1.10 + 1e-9, 2), round(pc * 1.05 + 1e-9, 2)
        if abs(v[i] - p10) < 0.006 or abs(v[i] - p05) < 0.006:
            n += 1
    return n, used


# --------------------------------------------------------------------------- 核心
def select_candidates(cand: int = 80, window: int = 20, cut: int = 2,
                      offline: bool = False, quiet: bool = True) -> dict:
    """跑完整套规则，返回候选名单与统计。供 pick_live / paper_trade 共用。"""
    soe, kind = load_owners()
    universe = sorted(c for c in soe if is_main_board(c))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    snap_cache = sorted(CACHE_DIR.glob("snap_*.parquet"))
    if offline and snap_cache:
        snap = pd.read_parquet(snap_cache[-1])
    else:
        snap = snapshot_tencent(universe, quiet=quiet)
        if snap.empty:
            snap = snapshot_eastmoney()
            snap = snap[snap["code"].isin(soe)] if not snap.empty else snap
        if snap.empty:
            raise RuntimeError("两个数据源都失败（东财/腾讯偶发 502 或限流），"
                               "等 30~60 秒重跑")
        snap.to_parquet(CACHE_DIR / f"snap_{dt.date.today().isoformat()}.parquet",
                        index=False)

    snap = snap[snap["code"].isin(soe) & snap["code"].map(is_main_board)]
    n_all = len(snap)
    snap = snap[snap["price"].notna() & (snap["price"] > 0)
                & snap["prev_close"].notna() & (snap["prev_close"] > 0)
                & snap["mktcap"].notna() & (snap["mktcap"] > 0)]
    n_valid = len(snap)

    name_s = snap["name"].fillna("").astype(str)
    dead = (snap["limit_up"].isna() | (snap["limit_up"] <= 0)
            | name_s.str.contains("退"))
    snap = snap[~dead]
    n_delisted = n_valid - len(snap)

    snap = snap.copy()
    snap["is_lu"] = (snap["price"] - snap["limit_up"]).abs() < 0.006
    n_lu = int(snap["is_lu"].sum())
    snap = snap[~snap["is_lu"]]

    snap = snap.sort_values("mktcap").reset_index(drop=True)
    cand_df = snap.head(max(cand, 25)).copy()

    with ThreadPoolExecutor(max_workers=8) as pool:
        series = list(pool.map(closes_of, cand_df["code"].tolist()))
    stats = [count_limit_up(s, window) for s in series]
    cand_df["nlu"] = [x[0] for x in stats]
    cand_df["bars"] = [x[1] for x in stats]
    cand_df["rank"] = range(1, len(cand_df) + 1)

    # 日线大面积失败时，涨停次数会低估 -> 必须中止，否则会买进本该剔除的股票
    n_ok = int((cand_df["bars"] >= int(window * 0.8)).sum())
    if len(cand_df) and n_ok / len(cand_df) < 0.7:
        raise RuntimeError(
            f"日线拉取失败过多：{len(cand_df) - n_ok}/{len(cand_df)} 只拿不到足够K线。"
            "三源（腾讯/新浪/东财）可能同时被限流，等 1~2 分钟重跑。")

    dropped = cand_df[cand_df["nlu"] >= cut].copy()
    kept = cand_df[cand_df["nlu"] < cut].reset_index(drop=True)
    kept["rank"] = range(1, len(kept) + 1)

    data_date = None
    for s in series:
        if s is not None and len(s):
            d = s.index[-1]
            data_date = d if data_date is None else max(data_date, d)

    return {
        "kept": kept,
        "dropped": dropped,
        "kind": kind,
        "stats": {
            "n_all": n_all, "n_valid": n_valid, "n_delisted": n_delisted,
            "n_limitup": n_lu, "n_dropped_rule": len(dropped),
            "data_date": data_date, "universe": len(universe),
            "bars_short": int((cand_df["bars"] < int(window * 0.8)).sum()),
        },
    }


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[5, 10, 20])
    ap.add_argument("--cand", type=int, default=80)
    ap.add_argument("--hold", type=str, default="")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--cut", type=int, default=2)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    today = dt.date.today().isoformat()
    print("=" * 100)
    print(f"实时选股   {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 100)

    try:
        res = select_candidates(args.cand, args.window, args.cut,
                                args.offline, quiet=False)
    except RuntimeError as exc:
        print(f"  [X] {exc}")
        return 2

    st, kind = res["stats"], res["kind"]
    kept, dropped = res["kept"], res["dropped"]
    print(f"  央国企主板代码：{st['universe']} 只")
    print(f"  快照有效：{st['n_valid']} 只（原始 {st['n_all']}）")
    print(f"  剔除退市/退市整理期：{st['n_delisted']} 只")
    print(f"  剔除当日涨停：{st['n_limitup']} 只（买不到）")
    print(f"  按总市值升序取前 {len(kept) + len(dropped)} 只算涨停次数")
    print(f"  剔除近{args.window}日涨停 >= {args.cut} 次：{st['n_dropped_rule']} 只")
    if st["n_dropped_rule"]:
        print("    " + "  ".join(f"{r.code}({int(r.nlu)}次)"
                                 for r in dropped.head(12).itertuples()))
    if st["bars_short"]:
        print(f"  [!] {st['bars_short']} 只日线不足，涨停次数可能低估")
    if st["data_date"] and st["data_date"] != today:
        print(f"  [!] 最新数据日 {st['data_date']}，与今天 {today} 不同")
    print("  [!] 脚本不判断节假日，请自行确认今天是本月最后一个交易日")

    for n in args.n:
        sel = kept.head(n)
        if len(sel) < n:
            print(f"  [X] n={n} 只有 {len(sel)} 只可用，请调大 --cand")
            continue
        print()
        print("=" * 100)
        print(f"  【n={n}】  合计市值 {sel['mktcap'].sum()/1e8:.1f} 亿   "
              f"中位 {sel['mktcap'].median()/1e8:.1f} 亿")
        print("=" * 100)
        print(f"  {'#':>3} {'代码':<8}{'名称':<11}{'现价':>8}{'总市值(亿)':>11}"
              f"{'涨跌':>8}{'近20日涨停':>10}{'昨收':>8}{'买入限价':>9}{'性质':>8}")
        for i, r in enumerate(sel.itertuples(), 1):
            print(f"  {i:>3} {r.code:<8}{str(r.name):<11}{r.price:>8.2f}"
                  f"{r.mktcap/1e8:>11.1f}{(r.price/r.prev_close-1)*100:>7.2f}%"
                  f"{int(r.nlu):>10}{r.prev_close:>8.2f}{r.limit_up:>9.2f}"
                  f"{kind.get(r.code, ''):>8}")
        nxt = kept.iloc[n:n + 5]
        if len(nxt):
            print("  候补：" + "  ".join(
                f"{r.code} {r.name} {r.mktcap/1e8:.1f}亿" for r in nxt.itertuples()))

    hp = Path(args.hold) if args.hold else None
    if hp and hp.exists():
        holds: list[str] = []
        for ln in hp.read_text(encoding="utf-8-sig").splitlines():
            ln = ln.strip().lstrip("\ufeff")
            if not ln or ln.startswith("#"):
                continue
            tok = ln.split()[0]
            if tok.isdigit():
                c = tok.zfill(6)
                if len(c) == 6 and c not in holds:
                    holds.append(c)
        print(f"\n  读入持仓 {len(holds)} 只：{', '.join(holds)}")
        nm = dict(zip(kept["code"], kept["name"]))
        print()
        print("=" * 100)
        print(f"  持仓对比（当前 {len(holds)} 只）")
        print("=" * 100)
        for n in args.n:
            sel = kept.head(n)
            scodes = set(sel["code"])
            sell = [c for c in holds if c not in scodes]
            buy = [c for c in sel["code"] if c not in holds]
            print(f"  n={n}:  保留 {len([c for c in holds if c in scodes])}"
                  f"  卖出 {len(sell)}  买入 {len(buy)}")
            if sell:
                ld = dict(zip(kept["code"], kept["limit_down"]))
                print("    卖出（挂跌停价）： " + "  ".join(
                    f"{c} {nm.get(c,'')}@{ld.get(c,0):.2f}" for c in sell))
            if buy:
                bp = dict(zip(sel["code"], sel["limit_up"]))
                print("    买入（挂涨停价）： " + "  ".join(
                    f"{c} {nm.get(c,'')}@{bp.get(c,0):.2f}" for c in buy))

    print()
    print("=" * 100)
    print("  下单规则")
    print("=" * 100)
    print("    买入：挂涨停价（见上表「买入限价」）")
    print("    卖出：挂跌停价 = round(昨收 x 0.90, 2)，ST 股 = round(昨收 x 0.95, 2)")
    print("    买不到：次日再挂，最多 3~5 天；仍买不到 -> 留现金，不做替代")

    OUT_DIR.mkdir(exist_ok=True)
    fp = OUT_DIR / f"picks_{st['data_date'] or today}.csv"
    out = kept.head(max(args.n)).copy()
    out["mktcap_亿"] = (out["mktcap"] / 1e8).round(2)
    out["owner"] = out["code"].map(kind)
    out.drop(columns=["mktcap", "is_lu"], errors="ignore").to_csv(
        fp, index=False, encoding="utf-8-sig")
    print(f"\n  已保存：{fp}")
    print(f"  总用时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
