"""
市场云图数据 —— 按行业分组的全市场快照，给 treemap 用。

════════════════════════════════════════════════════════════════
  为什么单独写一个模块而不是塞进 screener
════════════════════════════════════════════════════════════════
选股和云图用同一份快照（`screener.fetch_market`），但**用途不同**：

  · 选股要的是「过滤后的候选」，所以先剔 ST/停牌/低流动性
  · 云图要的是「全市场分布」，**必须保留亏损股和停牌股** ——
    少了它们，板块的涨跌和市值权重就是错的

所以这里只做一件轻量的事：拿快照 → 按申万一级行业分组 → 算板块加权涨跌，
不做任何筛选。筛选是选股层的事。

════════════════════════════════════════════════════════════════
  板块涨跌幅怎么算
════════════════════════════════════════════════════════════════
用**市值加权**，不是简单平均。

  简单平均：10 只小盘股各涨 5%，1 只万亿权重股跌 1% → 显示 +4.5%，假的
  市值加权：那 1 只权重大的决定板块方向 → 跟真实指数更接近

⚠️ 仍然不是官方板块指数：申万行业指数有自己的成分股权重和调整规则。
   这里是用「全市场快照 + 申万分类」现算的近似，用途是**看方向**，
   不是复现指数点位。

════════════════════════════════════════════════════════════════
  CLI
════════════════════════════════════════════════════════════════
  python heatmap.py --json                 输出 JSON（服务端用）
  python heatmap.py --json --top 40        每个行业最多 40 只（减小体积）
  python heatmap.py --json --min-cap 30    只保留市值 ≥30 亿的股票
  python heatmap.py --text                 终端里打印一个粗略的排行
  python heatmap.py --selftest             离线自检
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime

from screener import Snapshot, classify_sector, fetch_market, set_industry_map

# 涨跌色阶的上限：超过 ±10% 统一按满色画（ST 股可能 ±5%，北证 ±30%）
CLAMP_PCT = 10.0


def _offline() -> bool:
    return os.environ.get("QUANT_OFFLINE") == "1"


def _weighted_pct(stocks: list[Snapshot]) -> float | None:
    """
    市值加权涨跌。

    ⚠️ 权重用**流通市值**（float_cap）优先，没有才退到总市值。
       理由：涨跌幅是「可交易部分」的价格变化，用总市值加权会把
       大量不流通的股权算进去（比如国企大股东持股）。
    """
    num = den = 0.0
    for s in stocks:
        if s.change_pct is None:
            continue
        w = s.float_cap or s.mktcap or 0.0
        if w <= 0:
            continue
        num += s.change_pct * w
        den += w
    if den <= 0:
        # 全都没有市值数据（极端情况）：退化成简单平均
        vals = [s.change_pct for s in stocks if s.change_pct is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    return round(num / den, 2)


def build(market: list[Snapshot], *, top: int = 0, min_cap_yi: float = 0.0) -> dict:
    """
    把快照列表整理成 treemap 需要的结构。

    top      > 0 时每个行业最多保留市值最大的 N 只（控制 JSON 体积）
    min_cap_yi  只保留总市值 ≥ N 亿元的股票（0 = 不过滤）
    """
    groups: dict[str, list[Snapshot]] = {}
    for s in market:
        # 停牌股不参与涨跌统计，但保留在列表里（它的市值仍然占位）
        sector = classify_sector(s.name, s.symbol)
        groups.setdefault(sector, []).append(s)

    sectors = []
    advance = decline = flat = 0
    for name, stocks in groups.items():
        total_cap = sum((s.mktcap or 0.0) for s in stocks)
        chg = _weighted_pct(stocks)

        rows = []
        for s in stocks:
            if min_cap_yi > 0 and (s.mktcap or 0) / 1e4 < min_cap_yi:
                continue
            pct = s.change_pct
            if pct is not None:
                if pct > 0.001:
                    advance += 1
                elif pct < -0.001:
                    decline += 1
                else:
                    flat += 1
            rows.append({
                "symbol": s.symbol,
                "code": s.code,
                "name": s.name,
                "price": s.price,
                "chg": pct,
                "mktcapYi": round((s.mktcap or 0) / 1e4, 2),
                "floatCapYi": round((s.float_cap or 0) / 1e4, 2),
                "pe": s.pe,
                "turnover": s.turnover,
                "st": s.is_st,
                "suspended": s.is_suspended,
            })

        rows.sort(key=lambda r: r["mktcapYi"], reverse=True)
        if top > 0:
            rows = rows[:top]
        if not rows:
            continue
        sectors.append({
            "name": name,
            "chg": chg,
            "count": len(rows),
            "mktcapYi": round(total_cap / 1e4, 2),
            "stocks": rows,
        })

    # 板块按市值排 —— 大的排前面，treemap 布局更稳定（不会因为涨跌变动而重排）
    sectors.sort(key=lambda g: g["mktcapYi"], reverse=True)

    return {
        "asOf": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clampPct": CLAMP_PCT,
        "total": len(market),
        "shown": sum(g["count"] for g in sectors),
        "breadth": {"advance": advance, "decline": decline, "flat": flat},
        "sectors": sectors,
    }


def _selftest() -> int:
    """离线自检：不联网，用手造的 Snapshot 验证分组与加权逻辑。"""
    n = 0
    # 注入确定的行业映射 —— 不然 offline 时 classify_sector 会走名称兜底
    # 规则（「浦发银行」→「金融」），断言里的「银行」就永远匹配不上。
    set_industry_map({
        "sh600000": "银行", "sh600001": "银行",
        "sh600002": "食品饮料", "sh600003": "机械设备",
        "sh600004": "电力设备",
    })

    def make(sym, name, cap, pct, float_ratio=1.0):
        s = Snapshot(
            symbol=sym, code=sym[2:], name=name, price=10.0, prev_close=10.0,
            change_pct=pct, open=10.0, high=10.0, low=10.0, volume=1.0,
            amount=1.0, pe=10.0, pb=1.0, mktcap=cap, float_cap=cap * float_ratio,
            turnover=1.0,
        )
        return s

    # 1) 市值加权：一只大权重股应该主导板块涨跌
    bank = [make("sh600000", "浦发银行", 1_000_000, 1.0),
            make("sh600001", "小银行", 10_000, 9.0)]
    out = build(bank + [make("sh600002", "贵州茅台", 500_000, -2.0)])
    n += 1
    bank_g = next(g for g in out["sectors"] if g["name"] == "银行")
    assert bank_g["chg"] < 2.0, f"市值加权应被大权重股拉低，实际 {bank_g['chg']}"
    n += 1
    assert bank_g["count"] == 2, bank_g["count"]
    n += 1
    assert bank_g["stocks"][0]["name"] == "浦发银行", "应按市值降序"

    # 2) 涨跌家数统计
    n += 1
    assert out["breadth"]["advance"] == 2, out["breadth"]
    n += 1
    assert out["breadth"]["decline"] == 1, out["breadth"]

    # 3) min_cap 过滤
    small = build([make("sh600003", "迷你股", 1_000, 5.0)], min_cap_yi=30.0)
    n += 1
    assert small["sectors"] == [], "市值不足应被过滤掉，板块整个消失"

    # 4) top 限制
    many = [make(f"sz0000{i:02d}", f"测试{i}", 100_000 - i, 1.0) for i in range(10)]
    limited = build(many, top=3)
    n += 1
    assert sum(g["count"] for g in limited["sectors"]) == 3, "top 应限制总数"

    # 5) 停牌股（amount=0）不参与涨跌但仍在列表里
    halt = make("sh600004", "停牌股", 200_000, None)
    halt.is_suspended = True
    out2 = build([halt])
    n += 1
    assert out2["sectors"][0]["count"] == 1, "停牌股应保留在列表里"
    n += 1
    assert out2["breadth"]["advance"] == 0 and out2["breadth"]["decline"] == 0

    # 6) 可 JSON 序列化
    n += 1
    json.dumps(out, ensure_ascii=False)

    print(f"  {n} 通过 / 0 失败")
    return n


def _text_report(data: dict, limit: int = 20) -> None:
    b = data["breadth"]
    print(f"  全市场 {data['total']} 只｜涨 {b['advance']} 跌 {b['decline']} 平 {b['flat']}")
    print(f"  行业 {len(data['sectors'])} 个｜显示 {data['shown']} 只")
    print()
    print(f"  {'行业':<10}{'涨跌':>8}{'市值(亿)':>12}{'只数':>6}")
    for g in data["sectors"][:limit]:
        chg = f"{g['chg']:+.2f}%" if g["chg"] is not None else "—"
        print(f"  {g['name']:<10}{chg:>8}{g['mktcapYi']:>12.0f}{g['count']:>6}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name: str, default: str | None = None) -> str | None:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    if "--selftest" in args:
        return 0 if _selftest() else 1

    top = int(opt("--top", "0") or 0)
    min_cap = float(opt("--min-cap", "0") or 0)

    if _offline():
        print("  [offline] 行情不可用（QUANT_OFFLINE=1）", file=sys.stderr)
        if "--json" in args:
            print(json.dumps({"error": "offline", "sectors": []}, ensure_ascii=False))
        return 1

    try:
        market = fetch_market()
    except Exception as e:  # noqa: BLE001
        print(f"  拉取行情失败：{type(e).__name__}: {e}", file=sys.stderr)
        if "--json" in args:
            print(json.dumps({"error": f"{type(e).__name__}: {e}", "sectors": []},
                             ensure_ascii=False))
        return 1

    data = build(market, top=top, min_cap_yi=min_cap)

    if "--json" in args:
        print(json.dumps(data, ensure_ascii=False))
    else:
        _text_report(data, limit=int(opt("--limit", "25") or 25))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
