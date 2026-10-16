#!/usr/bin/env python3
"""
策略层 —— 基于真实指标产生买卖信号。

═══════════════════════════════════════════════════════════════════
  策略来源（都是公开的经典逻辑，不是自己编的）
═══════════════════════════════════════════════════════════════════
1. ema_cross      双均线趋势跟踪
   出处：最古老的趋势跟踪法，Turtle Traders / 各类 CTA 基金的基本款。
   逻辑：快线上穿慢线做多，下穿平仓。
   已加 ADX 过滤 —— ADX 低（震荡市）时不进场，这是 CTA 常见改进。

2. rsi_reversion  RSI 均值回归
   出处：Wilder 1978《New Concepts in Technical Trading Systems》原书逻辑。
   逻辑：RSI < 30 超卖买入，RSI > 70 超买卖出。

3. bollinger_break 布林带突破
   出处：John Bollinger 的经典用法（挤压后突破）。
   逻辑：收盘突破上轨做多（趋势启动），跌破中轨离场。

4. macd_trend     MACD 趋势
   出处：Gerald Appel 的 MACD 原义。
   逻辑：DIF 上穿 DEA 且柱状转正做多。

5. donchian_break 唐奇安通道突破
   出处：Turtle Traders 的海龟法则核心（20 日突破入场，10 日反向离场）。
   逻辑：收盘创 20 日新高做多，跌破 10 日新低离场。

═══════════════════════════════════════════════════════════════════
  ⚠️ 关于「未来函数」（look-ahead bias）
═══════════════════════════════════════════════════════════════════
第 i 根 K 线的信号只允许使用 <= i 的信息。
所有指标都是用截至 i 的数据算出来的，所以指标本身没问题。

但**执行**必须往后推一根：用第 i 根收盘产生的信号，只能在
第 i+1 根开盘成交。否则就是用「已知收盘价」去交易「同一根的收盘价」，
回测收益会被严重高估 —— 这是量化回测最常见的自欺方式。

本模块只负责产生信号；执行时点的处理在 backtest.py 里，
默认 shift 一根（next-bar open 成交），并禁止关闭这个保护。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from indicators import IndicatorSet, compute
from datasource import Series

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
@dataclass
class SignalSet:
    """
    策略输出的信号序列。

    position[i] 取值：
        1  = 目标持仓为多
        0  = 目标持仓为空
      用于做多/空仓策略（本模块全部是这类），不用 -1。
    entry[i] / exit[i] 是**触发点**，用于图上标记。
    """
    name: str
    label: str
    description: str
    position: np.ndarray
    entry: np.ndarray          # bool
    exit: np.ndarray           # bool
    reasons: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def trades_count(self) -> int:
        return int(self.entry.sum())

    def current(self) -> int:
        return int(self.position[-1]) if self.position.size else 0

    def last_reason(self) -> str:
        for r in reversed(self.reasons):
            if r:
                return r
        return ""


# ============================================================
#  信号生成辅助
# ============================================================
def _cross_up(a: np.ndarray, b: np.ndarray, i: int) -> bool:
    """a 在 i 处上穿 b。必须检查 i-1 是否有效，否则 NaN 比较永远 False。"""
    if i < 1:
        return False
    a0, a1, b0, b1 = a[i-1], a[i], b[i-1], b[i]
    if any(np.isnan(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 <= b0 and a1 > b1


def _cross_down(a: np.ndarray, b: np.ndarray, i: int) -> bool:
    if i < 1:
        return False
    a0, a1, b0, b1 = a[i-1], a[i], b[i-1], b[i]
    if any(np.isnan(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 >= b0 and a1 < b1


def _build(name: str, label: str, desc: str, n: int,
           decide: Callable[[int], tuple[int, str]],
           params: dict) -> SignalSet:
    """
    通用装配：遍历每根 K 线，调 decide(i) 得到目标持仓与理由。
    只在**目标仓位发生变化**时记 entry/exit，避免重复标记。
    """
    pos = np.zeros(n, dtype=np.int8)
    ent = np.zeros(n, dtype=bool)
    ext = np.zeros(n, dtype=bool)
    reasons: list[str] = [""] * n

    cur = 0
    for i in range(n):
        target, why = decide(i)
        if target == 1 and cur == 0:
            ent[i] = True
            reasons[i] = why
        elif target == 0 and cur == 1:
            ext[i] = True
            reasons[i] = why
        cur = target
        pos[i] = cur

    return SignalSet(name, label, desc, pos, ent, ext, reasons, params)


# ============================================================
#  策略 1：双均线趋势（带 ADX 过滤）
# ============================================================
def ema_cross(ind: IndicatorSet, fast: int = 12, slow: int = 26,
              adx_min: float = 18.0) -> SignalSet:
    """
    快线上穿慢线做多，下穿平仓；ADX 低于阈值时不开新仓。

    ADX 过滤的意义：均线策略在震荡市里会被反复打脸（连续假突破），
    ADX 低说明没有趋势，此时不进场能显著减少无效交易。
    """
    f = ind.values[f"ema{fast}"] if f"ema{fast}" in ind.values else ind.values["ema12"]
    s = ind.values[f"ema{slow}"] if f"ema{slow}" in ind.values else ind.values["ema26"]
    adx_v = ind.values["adx"]
    n = ind.series.bars.__len__()

    state = {"held": 0}

    def decide(i: int):
        if state["held"] == 0:
            if _cross_up(f, s, i):
                a = adx_v[i]
                if not np.isnan(a) and a >= adx_min:
                    state["held"] = 1
                    return 1, f"EMA{fast} 上穿 EMA{slow}，ADX={a:.1f}≥{adx_min:g} 确认趋势"
                return 0, f"上穿但 ADX={a:.1f}<{adx_min:g}，趋势不足不进场"
        else:
            if _cross_down(f, s, i):
                state["held"] = 0
                return 0, f"EMA{fast} 下穿 EMA{slow}，离场"
        return state["held"], ""

    return _build("ema_cross", f"双均线趋势 EMA{fast}/{slow}",
                  f"快线上穿慢线且 ADX≥{adx_min:g} 进场；下穿离场。"
                  f"ADX 过滤用于避开震荡市的假突破。",
                  n, decide, {"fast": fast, "slow": slow, "adxMin": adx_min})


# ============================================================
#  策略 2：RSI 均值回归
# ============================================================
def rsi_reversion(ind: IndicatorSet, n: int = 14,
                  low: float = 30.0, high: float = 70.0) -> SignalSet:
    """RSI 超卖买入、超买卖出（Wilder 原书逻辑）。"""
    r = ind.values["rsi"]
    total = ind.series.bars.__len__()
    state = {"held": 0}

    def decide(i: int):
        v = r[i]
        if np.isnan(v):
            return state["held"], ""
        if state["held"] == 0 and v < low:
            state["held"] = 1
            return 1, f"RSI={v:.1f} < {low:g} 超卖，买入"
        if state["held"] == 1 and v > high:
            state["held"] = 0
            return 0, f"RSI={v:.1f} > {high:g} 超买，卖出"
        return state["held"], ""

    return _build("rsi_reversion", f"RSI 均值回归 {low:g}/{high:g}",
                  f"RSI<{low:g} 超卖买入，RSI>{high:g} 超买卖出。"
                  f"出自 Wilder《New Concepts in Technical Trading Systems》。",
                  total, decide, {"n": n, "low": low, "high": high})


# ============================================================
#  策略 3：布林带突破
# ============================================================
def bollinger_break(ind: IndicatorSet, n: int = 20, k: float = 2.0) -> SignalSet:
    """
    收盘突破上轨做多（趋势启动），跌破中轨离场。
    这是 Bollinger 本人推荐的顺势用法（不是「触上轨就卖」的逆势用法）。
    """
    close = np.asarray(ind.series.closes, dtype=float)
    up = ind.values["bb_upper"]
    mid = ind.values["bb_mid"]
    total = close.size
    state = {"held": 0}

    def decide(i: int):
        u, m = up[i], mid[i]
        if np.isnan(u) or np.isnan(m):
            return state["held"], ""
        if state["held"] == 0 and close[i] > u:
            state["held"] = 1
            return 1, f"收盘 {close[i]:.2f} 突破上轨 {u:.2f}，趋势启动"
        if state["held"] == 1 and close[i] < m:
            state["held"] = 0
            return 0, f"收盘 {close[i]:.2f} 跌破中轨 {m:.2f}，离场"
        return state["held"], ""

    return _build("bollinger_break", f"布林带突破 {n}/{k:g}",
                  f"收盘突破上轨进场，跌破中轨离场。Bollinger 的顺势用法。",
                  total, decide, {"n": n, "k": k})


# ============================================================
#  策略 4：MACD 趋势
# ============================================================
def macd_trend(ind: IndicatorSet) -> SignalSet:
    """DIF 上穿 DEA 且柱状转正做多；DIF 下穿 DEA 离场。"""
    dif = ind.values["macd_dif"]
    dea = ind.values["macd_dea"]
    hist = ind.values["macd_hist"]
    total = ind.series.bars.__len__()
    state = {"held": 0}

    def decide(i: int):
        if _cross_up(dif, dea, i) and not np.isnan(hist[i]) and hist[i] > 0:
            if state["held"] == 0:
                state["held"] = 1
                return 1, f"DIF 上穿 DEA，柱={hist[i]:.3f} 转正"
        elif _cross_down(dif, dea, i):
            if state["held"] == 1:
                state["held"] = 0
                return 0, f"DIF 下穿 DEA，离场"
        return state["held"], ""

    return _build("macd_trend", "MACD 趋势",
                  "DIF 上穿 DEA 且柱状转正进场；下穿离场。Gerald Appel 的 MACD 原义。",
                  total, decide, {})


# ============================================================
#  策略 5：唐奇安通道突破（海龟法则）
# ============================================================
def donchian_break(ind: IndicatorSet, entry_n: int = 20,
                   exit_n: int = 10) -> SignalSet:
    """
    收盘创 entry_n 日新高做多，跌破 exit_n 日新低离场。
    这是 Turtle Traders 海龟法则的核心系统（原版是 20 进 / 10 出）。
    """
    close = np.asarray(ind.series.closes, dtype=float)
    high = np.asarray(ind.series.highs, dtype=float)
    low = np.asarray(ind.series.lows, dtype=float)
    from indicators import rolling_max, rolling_min
    dc_up = rolling_max(high, entry_n)
    dc_lo = rolling_min(low, exit_n)
    total = close.size
    state = {"held": 0}

    def decide(i: int):
        u, l = dc_up[i], dc_lo[i]
        if state["held"] == 0:
            # 用前一日的通道值比较，避免「自己包含自己」导致的必然突破
            pu = dc_up[i - 1] if i >= 1 else np.nan
            if not np.isnan(pu) and close[i] > pu:
                state["held"] = 1
                return 1, f"收盘 {close[i]:.2f} 创 {entry_n} 日新高（前高 {pu:.2f}）"
        else:
            pl = dc_lo[i - 1] if i >= 1 else np.nan
            if not np.isnan(pl) and close[i] < pl:
                state["held"] = 0
                return 0, f"收盘 {close[i]:.2f} 跌破 {exit_n} 日新低（前低 {pl:.2f}）"
        return state["held"], ""

    return _build("donchian_break", f"唐奇安突破 {entry_n}/{exit_n}",
                  f"创 {entry_n} 日新高进场，跌破 {exit_n} 日新低离场。"
                  f"Turtle Traders 海龟法则核心系统。",
                  total, decide, {"entryN": entry_n, "exitN": exit_n})


# ============================================================
#  注册表
# ============================================================
STRATEGIES: dict[str, tuple[str, Callable[..., SignalSet]]] = {
    "ema_cross":       ("双均线趋势（ADX过滤）", ema_cross),
    "rsi_reversion":   ("RSI 均值回归", rsi_reversion),
    "bollinger_break": ("布林带突破", bollinger_break),
    "macd_trend":      ("MACD 趋势", macd_trend),
    "donchian_break":  ("唐奇安通道突破（海龟）", donchian_break),
}


def run(name: str, ind: IndicatorSet, **kw) -> SignalSet:
    if name not in STRATEGIES:
        raise KeyError(f"未知策略 {name}，可用: {', '.join(STRATEGIES)}")
    return STRATEGIES[name][1](ind, **kw)


def run_all(ind: IndicatorSet) -> dict[str, SignalSet]:
    """一次跑完所有策略，用于对比 —— 面板上可以同时展示多条策略。"""
    out = {}
    for k in STRATEGIES:
        try:
            out[k] = run(k, ind)
        except Exception as e:
            print(f"  策略 {k} 失败: {type(e).__name__}: {e}", file=sys.stderr)
    return out


# ============================================================
#  CLI
# ============================================================
def main(argv: list[str] | None = None) -> int:
    import argparse
    from datasource import MARKETS, fetch

    ap = argparse.ArgumentParser(description="策略信号生成")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    args = ap.parse_args(argv)

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    s = fetch(args.market, symbol, interval, args.limit)
    ind = compute(s)
    m = s.meta()

    print("=" * 76)
    print(f"  {m['name']}  {m['symbol']}  {m['interval']}   {m['count']} 根   源={m['source']}")
    print(f"  {m['first']} → {m['last']}   收盘 {m['lastClose']}")
    print("=" * 76)

    sigs = run_all(ind) if args.strategy == "all" else {args.strategy: run(args.strategy, ind)}

    for key, sig in sigs.items():
        cur = sig.current()
        state = "持仓" if cur == 1 else "空仓"
        print(f"\n  【{sig.label}】")
        print(f"    {sig.description}")
        print(f"    信号次数 {sig.trades_count()}   当前 {state}")
        # 最近 4 次触发
        idxs = [i for i in range(sig.position.size) if sig.entry[i] or sig.exit[i]]
        for i in idxs[-4:]:
            kind = "买入" if sig.entry[i] else "卖出"
            bar = s.bars[i]
            print(f"      {bar.as_dict()['iso']}  {kind}  {bar.close:>10.2f}   {sig.reasons[i]}")

    print("\n" + "=" * 76)
    print("  注意：信号基于收盘价产生，实际成交必须顺延到下一根开盘（见 backtest.py）")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
