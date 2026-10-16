#!/usr/bin/env python3
"""
量化数据管道 —— 一次性产出前端所需的完整 JSON。

把「取数 → 算指标 → 生成信号 → 回测 → 模拟盘 → 实时快照」
串成一条链，输出单个 JSON 给前端渲染。

设计要点：
  · 只输出**真实计算出来的数字**，不做任何美化或填充
  · 每个数字都带来源（source 字段），前端必须显示数据来源
  · 缺失的指标输出 null，前端显示「—」，不编造
  · 一次调用拿全，避免前端多次请求造成数据不同步

用法：
  python quant/pipeline.py --market ashare --symbol sh600519 --interval 1d --limit 300
  python quant/pipeline.py --market crypto --symbol BTCUSDT --interval 1h --limit 300
  python quant/pipeline.py --list          # 列出可用标的
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict
from typing import Any

import numpy as np

from datasource import MARKETS, Series, fetch, quote, SYMBOL_NAMES, DataSourceError
from indicators import compute, IndicatorSet
from strategy import STRATEGIES, run_all, SignalSet
from backtest import run_backtest, MARKET_COSTS, BacktestResult
from paper import PaperTrader, default_initial

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _clean(x: Any, digits: int = 6) -> float | None:
    """NaN/Inf -> None（JSON 不支持 NaN，前端显示「—」）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, digits)


def jsonable(obj: Any) -> Any:
    """
    递归把 numpy 类型转成原生 Python 类型。

    ⚠️ 为什么必须显式做这件事：
       json.dump 不认识 numpy 标量，会抛
       「Object of type bool is not JSON serializable」——
       因为 np.bool_ 不是内置 bool（np.int8 / np.float64 同理）。
       而 np.float64 是 float 的子类，所以它反而能过；
       np.bool_ 和 np.int8 **不能**过 —— 于是报错信息只提 bool，
       真正的坑是两个类型混在一起。实测在这里卡过一次。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return str(obj)


def _arr(values: np.ndarray, digits: int = 6) -> list:
    return [_clean(v, digits) for v in values]


# 前端要画的指标线（键 -> (显示名, 用途分组)）
CHART_INDICATORS = {
    "ema12":  ("EMA12", "main"),
    "ema26":  ("EMA26", "main"),
    "sma20":  ("SMA20", "main"),
    "bb_upper": ("布林上轨", "main"),
    "bb_mid":   ("布林中轨", "main"),
    "bb_lower": ("布林下轨", "main"),
    "macd_dif": ("DIF", "macd"),
    "macd_dea": ("DEA", "macd"),
    "macd_hist": ("MACD柱", "macd"),
    "rsi":     ("RSI", "rsi"),
    "kdj_k":   ("K", "kdj"),
    "kdj_d":   ("D", "kdj"),
    "atr":     ("ATR", "atr"),
    "adx":     ("ADX", "adx"),
    "plus_di": ("+DI", "adx"),
    "minus_di": ("-DI", "adx"),
    "volume_ma20": ("量MA20", "volume"),
}


def build(market: str, symbol: str, interval: str, limit: int = 300,
          strategy: str = "all", initial: float | None = None) -> dict:
    t_start = time.time()

    series: Series = fetch(market, symbol, interval, limit)
    ind: IndicatorSet = compute(series)

    sigs: dict[str, SignalSet] = (
        run_all(ind) if strategy == "all" else {strategy: run_all(ind)[strategy]}
    )

    init = initial if initial is not None else default_initial(market)

    # ---- 回测 ----
    backtests: dict[str, dict] = {}
    for key, sig in sigs.items():
        try:
            r: BacktestResult = run_backtest(series, sig, initial=init)
            backtests[key] = r.summary()
        except Exception as e:
            backtests[key] = {"error": f"{type(e).__name__}: {e}"}

    # ---- 模拟盘 ----
    papers: dict[str, dict] = {}
    equity_curves: dict[str, list] = {}
    for key, sig in sigs.items():
        try:
            pt = PaperTrader(series, sig, initial=init)
            pt.run_all()
            snap = pt.snapshot()
            # ⚠️ 不能用 snap.summary()：它会 pop 掉 events，
            #    而前端的「过程明细 · 逐笔」正是读 papers[*].events，
            #    结果那一栏永远显示"无数据"（实测踩过）。
            #    这里改为保留 events，只剔除体积大的 equity_curve 与 fills。
            papers[key] = {
                **asdict(snap),
                "equity_curve": [],
                "fills": [],
            }
            # 权益曲线只取稀疏点，避免 JSON 过大；最后一根必须保留
            curve = snap.equity_curve
            step = max(1, len(curve) // 240)
            equity_curves[key] = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
        except Exception as e:
            papers[key] = {"error": f"{type(e).__name__}: {e}"}

    # ---- K 线 ----
    bars = [{
        "i": i,
        "t": b.as_dict()["iso"],
        "ts": b.ts,
        "o": _clean(b.open, 4), "h": _clean(b.high, 4),
        "l": _clean(b.low, 4), "c": _clean(b.close, 4),
        "v": _clean(b.volume, 2),
    } for i, b in enumerate(series.bars)]

    # ---- 指标 ----
    indicators: dict[str, dict] = {}
    for key_name, (label, group) in CHART_INDICATORS.items():
        v = ind.values.get(key_name)
        if v is None:
            continue
        indicators[key_name] = {
            "label": label, "group": group, "values": _arr(v, 4),
        }

    # ---- 策略信号（供图上标记买卖点）----
    signals: dict[str, dict] = {}
    for key, sig in sigs.items():
        signals[key] = {
            "name": sig.name,
            "label": sig.label,
            "description": sig.description,
            "params": sig.params,
            "entries": [int(i) for i in np.flatnonzero(sig.entry)],
            "exits": [int(i) for i in np.flatnonzero(sig.exit)],
            "position": [int(x) for x in sig.position],
            "current": sig.current(),
            "lastReason": sig.last_reason(),
            "tradeCount": sig.trades_count(),
        }

    # ---- 实时快照（真实接口）----
    live: dict = {}
    try:
        live = quote(market, symbol)
    except Exception as e:
        live = {"error": f"{type(e).__name__}: {e}"}

    # ---- 最新一根的指标值（面板数字）----
    latest_snapshot = ind.snapshot(-1)

    # ---- 数据源降级链（让前端能显示当前用的哪个源）----
    meta = series.meta()
    meta["name"] = meta["name"] if meta["name"] != symbol else SYMBOL_NAMES.get(symbol, symbol)
    meta["elapsedMs"] = int((time.time() - t_start) * 1000)

    data = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta": meta,
        "live": live,
        "bars": bars,
        "indicators": indicators,
        "signals": signals,
        "backtests": backtests,
        "papers": papers,
        "equityCurves": equity_curves,
        "latest": latest_snapshot,
        "markets": {k: {"label": v["label"],
                        "symbols": v["symbols"],
                        "intervals": v["intervals"],
                        "defaultSymbol": v["defaultSymbol"],
                        "defaultInterval": v["defaultInterval"],
                        "initial": default_initial(k),
                        "providers": [p.label for p in v["providers"]]}
                    for k, v in MARKETS.items()},
        "strategies": {k: {"label": v[0]} for k, v in STRATEGIES.items()},
        "symbolNames": SYMBOL_NAMES,
    }
    # 统一清洗：numpy 标量 -> 原生类型，NaN -> None
    return jsonable(data)


def market_meta() -> dict:
    """市场/标的/策略元信息。不取行情，所以很快 —— 供前端初始化下拉框。"""
    return jsonable({
        "markets": {k: {"label": v["label"],
                        "symbols": v["symbols"],
                        "intervals": v["intervals"],
                        "defaultSymbol": v["defaultSymbol"],
                        "defaultInterval": v["defaultInterval"],
                        "initial": default_initial(k),
                        "providers": [p.label for p in v["providers"]]}
                    for k, v in MARKETS.items()},
        "strategies": {k: {"label": v[0]} for k, v in STRATEGIES.items()},
        "symbolNames": SYMBOL_NAMES,
    })


def _list_markets() -> None:
    for k, v in MARKETS.items():
        print(f"\n{k}  —  {v['label']}")
        print(f"  默认标的   {v['defaultSymbol']}")
        print(f"  可选标的   {', '.join(v['symbols'])}")
        print(f"  粒度       {', '.join(v['intervals'])}")
        print(f"  数据源链   {' → '.join(p.label for p in v['providers'])}")
        print(f"  默认资金   {default_initial(k):,.0f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="量化数据管道（输出前端用 JSON）")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    ap.add_argument("--initial", type=float, default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--meta", action="store_true", help="只输出市场/策略元信息（JSON，很快）")
    ap.add_argument("--compact", action="store_true", help="只输出摘要")
    args = ap.parse_args(argv)

    if args.meta:
        json.dump(market_meta(), sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0

    if args.list:
        _list_markets()
        return 0

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    try:
        data = build(args.market, symbol, interval, args.limit, args.strategy, args.initial)
    except DataSourceError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stdout)
        return 1

    if args.compact:
        m = data["meta"]
        print(f"{m['name']} {m['symbol']} {m['interval']}  {m['count']} 根  源={m['source']}  "
              f"{m['elapsedMs']}ms")
        print(f"实时: {data['live']}")
        for k, b in data["backtests"].items():
            if "error" in b:
                print(f"  {k}: 回测失败 {b['error']}")
            else:
                print(f"  {k:18} 收益 {b['total_return']:>8.2f}%  "
                      f"回撤 {b['max_drawdown']:>6.2f}%  夏普 {b['sharpe']:>6.2f}  "
                      f"交易 {b['trade_count']:>3}  胜率 {b['win_rate']:>5.1f}%")
        return 0

    json.dump(data, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
