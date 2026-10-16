#!/usr/bin/env python3
"""
每日快照存档 —— 为将来的收益验证攒数据。

═══════════════════════════════════════════════════════════════
  为什么必须有这个
═══════════════════════════════════════════════════════════════
选股结果**没法立刻回测收益**，因为新浪行情中心接口只能返回
**当前**的 PE/PB/市值 —— 历史估值拿不到（东财被封，其他源也没这数据）。

所以想验证「双低价值选出来的股票 20 天后涨了没有」，只有一条路：
**从今天开始，每天存一份快照。** 攒够一段时间就能回放：

    第 T 日选出 Top N  →  第 T+5 / T+20 日的价格  →  算收益

这不是"可选优化"，是**唯一可行**的做法。所以第一版就带上。

═══════════════════════════════════════════════════════════════
  存什么、存在哪
═══════════════════════════════════════════════════════════════
存**全市场快照的原始评估字段**（不含名称等冗余）到 JSONL：
  quant/state/snapshots/YYYY-MM-DD.jsonl    每行一只股票

⚠️ quant/state/ 已被 .gitignore 排除 —— 这些是本地数据，不入库。
   一份全市场快照约 1.5 MB，一年 250 个交易日约 375 MB。
   所以提供一个 --prune 选项清理旧文件。

═══════════════════════════════════════════════════════════════
  怎么用
═══════════════════════════════════════════════════════════════
  python quant/snapshot_archive.py --save        # 存今天的
  python quant/snapshot_archive.py --list        # 看存了哪些
  python quant/snapshot_archive.py --eval dual_low --days 20
                                                 # 评估：历史某个选股跑得怎样
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

STATE = HERE / "state"
SNAP_DIR = STATE / "snapshots"

# 只存这些字段。为什么不存全部：
#   · 名称、买卖五档这些对回测没用，白占空间
#   · 市值/PE/PB/换手/成交额才是选股依据，必须留
KEEP = ("symbol", "name", "price", "prev_close", "change_pct",
        "amount", "turnover", "pe", "pb", "mktcap")


def today_str() -> str:
    """用本地日期。A 股按自然日归档，不需要时区处理。"""
    return datetime.now().strftime("%Y-%m-%d")


def save_snapshot(market: list | None = None, verbose: bool = True) -> dict:
    """
    存一份快照。已存在则不覆盖（一天只存第一次 —— 尾盘数据更准，
    但本项目不做择时调度，所以以第一次为准并记录时间）。
    """
    from screener import fetch_market

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    day = today_str()
    path = SNAP_DIR / f"{day}.jsonl"
    if path.exists():
        return {"ok": False, "reason": "already-exists", "path": str(path),
                "date": day}

    t0 = time.time()
    if market is None:
        market = fetch_market(verbose=False)
    rows = []
    for s in market:
        d = s.to_dict()
        rows.append({k: d.get(k) for k in KEEP})

    # 原子写：先写 tmp 再 replace，避免中途失败留下半个文件
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)
    el = int((time.time() - t0) * 1000)
    if verbose:
        size_kb = path.stat().st_size / 1024
        print(f"  已存 {len(rows)} 只 → {path.name}  "
              f"({size_kb:.0f} KB, {el}ms)")
    return {"ok": True, "path": str(path), "date": day,
            "count": len(rows), "ms": el}


def list_snapshots() -> list[dict]:
    if not SNAP_DIR.exists():
        return []
    out = []
    for p in sorted(SNAP_DIR.glob("*.jsonl")):
        try:
            n = sum(1 for _ in p.open(encoding="utf8"))
        except OSError:
            n = 0
        out.append({"date": p.stem, "file": p.name,
                    "count": n, "kb": round(p.stat().st_size / 1024, 1)})
    return out


def load_snapshot(day: str) -> dict[str, dict]:
    """读某天的快照，返回 {symbol: row}。"""
    p = SNAP_DIR / f"{day}.jsonl"
    if not p.exists():
        return {}
    out = {}
    with p.open(encoding="utf8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("symbol"):
                out[r["symbol"]] = r
    return out


def prune(keep_days: int = 400, verbose: bool = True) -> int:
    """清理超过 keep_days 天的旧快照。返回删了几个。"""
    if not SNAP_DIR.exists():
        return 0
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    n = 0
    for p in sorted(SNAP_DIR.glob("*.jsonl")):
        if p.stem < cutoff:
            p.unlink()
            n += 1
            if verbose:
                print(f"  删除 {p.name}")
    return n


# ══════════════════════════════════════════════════════════════
#  收益评估
# ══════════════════════════════════════════════════════════════
def evaluate(strategy_key: str, days: int = 20, top: int = 10,
             verbose: bool = True) -> dict:
    """
    回看：如果 N 天前按这个策略选股，到今天收益如何。

    ⚠️ 需要至少两个日期的快照（选股那天 + 今天用于取现价）。
       只有一天数据时无法评估 —— 会明确说明，不编造结果。
    """
    from screener import STRATEGIES, HardFilters, apply_filters, score_cross_section, Snapshot

    snaps = list_snapshots()
    if len(snaps) < 2:
        return {
            "ok": False,
            "reason": "not-enough-data",
            "have": len(snaps),
            "need": 2,
            "note": ("只有 %d 天的快照，没法评估。"
                     "收益验证需要至少两天的数据 —— 每天跑一次 "
                     "`python quant/snapshot_archive.py --save` 攒起来。"
                     % len(snaps)),
        }

    st = STRATEGIES.get(strategy_key)
    if st is None:
        return {"ok": False, "reason": "unknown-strategy",
                "note": f"未知策略 {strategy_key}"}

    # 找 >= days 天前最近的那份快照作为「选股日」
    target = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    past = [s for s in snaps if s["date"] <= target]
    if not past:
        return {
            "ok": False, "reason": "no-past-snapshot",
            "note": (f"没有 {target} 或更早的快照（最早的存于 "
                     f"{snaps[0]['date']}）。要评估 {days} 天收益，"
                     f"得有 {days} 天前的数据。"),
        }
    base_day = past[-1]["date"]
    latest_day = snaps[-1]["date"]

    base = load_snapshot(base_day)
    now = load_snapshot(latest_day)
    if not base or not now:
        return {"ok": False, "reason": "empty-snapshot",
                "note": f"快照为空（{base_day} / {latest_day}）"}

    # 重建 Snapshot 对象以复用策略逻辑
    def rebuild(rows: dict[str, dict]) -> list[Snapshot]:
        out = []
        for r in rows.values():
            try:
                out.append(Snapshot(
                    symbol=r["symbol"], code=r["symbol"][2:],
                    name=r.get("name") or r["symbol"],
                    price=r.get("price"), prev_close=r.get("prev_close"),
                    change_pct=r.get("change_pct"), open=None, high=None,
                    low=None, volume=None, amount=r.get("amount"),
                    pe=r.get("pe"), pb=r.get("pb"),
                    mktcap=r.get("mktcap"), float_cap=None,
                    turnover=r.get("turnover"),
                    is_st=("ST" in (r.get("name") or "").upper()),
                    is_suspended=(not r.get("amount") or r["amount"] <= 0),
                ))
            except Exception:                        # noqa: BLE001
                continue
        return out

    cands = rebuild(base)
    kept, _ = apply_filters(cands, st.filters)
    ranked = score_cross_section(
        kept, st.weights, max_per_sector=st.max_per_sector, top_n=top)
    picks = ranked[:top]

    rows_out = []
    rets = []
    for x in picks:
        sym = x["snapshot"].symbol
        p0 = x["snapshot"].price
        p1 = (now.get(sym) or {}).get("price")
        if not p0 or not p1:
            continue
        ret = (p1 / p0 - 1) * 100
        rets.append(ret)
        rows_out.append({
            "symbol": sym, "name": x["snapshot"].name,
            "sector": x["sector"], "score": x["score"],
            "priceAt": p0, "priceNow": p1, "ret": round(ret, 2),
        })

    if not rets:
        return {"ok": False, "reason": "no-overlap",
                "note": "选出的股票在今天的价格里找不到，无法算收益"}

    avg = sum(rets) / len(rets)
    win = sum(1 for r in rets if r > 0)
    return {
        "ok": True,
        "strategy": st.key, "label": st.label,
        "baseDay": base_day, "latestDay": latest_day,
        "days": days, "top": top,
        "count": len(rets),
        "avgReturn": round(avg, 2),
        "medianReturn": round(sorted(rets)[len(rets) // 2], 2),
        "winRate": round(win / len(rets) * 100, 1),
        "best": round(max(rets), 2),
        "worst": round(min(rets), 2),
        "results": rows_out,
        "note": ("⚠️ 这只反映已存档区间内的表现，样本天数少时噪声很大。"
                 "别把它当策略有效性的证据。"),
    }


# ══════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日快照存档 + 收益评估")
    ap.add_argument("--save", action="store_true", help="存今天的快照")
    ap.add_argument("--list", action="store_true", help="列出已有快照")
    ap.add_argument("--eval", metavar="STRATEGY", help="评估某策略的历史表现")
    ap.add_argument("--days", type=int, default=20, help="评估回看天数")
    ap.add_argument("--top", type=int, default=10, help="取前 N 只")
    ap.add_argument("--prune", type=int, metavar="KEEP_DAYS",
                    help="清理超过 N 天的旧快照")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.prune:
        n = prune(a.prune, verbose=not a.json)
        print(json.dumps({"pruned": n}, ensure_ascii=False) if a.json
              else f"  清理了 {n} 个旧快照（保留 {a.prune} 天）")
        return 0

    if a.list:
        snaps = list_snapshots()
        if a.json:
            print(json.dumps(snaps, ensure_ascii=False))
        else:
            if not snaps:
                print("  还没有任何快照。跑 `--save` 存一份。")
            else:
                print(f"  共 {len(snaps)} 天的快照：")
                for s in snaps:
                    print(f"    {s['date']}  {s['count']:5} 只  {s['kb']:8.1f} KB")
                total = sum(s["kb"] for s in snaps)
                print(f"  合计 {total/1024:.1f} MB")
        return 0

    if a.eval:
        r = evaluate(a.eval, days=a.days, top=a.top)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif not r.get("ok"):
            print(f"  无法评估：{r.get('note')}")
        else:
            print("=" * 74)
            print(f"  {r['label']} · {r['baseDay']} → {r['latestDay']}"
                  f"（{r['days']} 天前选出，取前 {r['top']} 只）")
            print("=" * 74)
            print(f"  平均收益 {r['avgReturn']:+.2f}%   中位 "
                  f"{r['medianReturn']:+.2f}%   胜率 {r['winRate']:.0f}%")
            print(f"  最好 {r['best']:+.2f}%   最差 {r['worst']:+.2f}%")
            print()
            print(f"  {'代码':<10}{'名称':<10}{'行业':<10}{'当时':>9}{'现在':>9}{'收益':>9}")
            for x in sorted(r["results"], key=lambda y: -y["ret"]):
                print(f"  {x['symbol']:<10}{x['name'][:9]:<10}"
                      f"{(x['sector'] or '-'):<10}{x['priceAt']:>9.2f}"
                      f"{x['priceNow']:>9.2f}{x['ret']:>8.2f}%")
            print()
            print(f"  {r['note']}")
        return 0

    if a.save:
        try:
            r = save_snapshot(verbose=not a.json)
        except Exception as e:                       # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False))
            return 1
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif not r.get("ok"):
            print(f"  今天已经存过了（{r['date']}），不重复存。")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
