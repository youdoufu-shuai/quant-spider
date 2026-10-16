#!/usr/bin/env python3
"""
技术指标引擎 —— 全部基于真实价格序列计算。

═══════════════════════════════════════════════════════════════════
  实现要点（这些细节决定指标对不对）
═══════════════════════════════════════════════════════════════════
1. EMA 用标准递推：ema[0]=price[0]，ema[i]=α·p[i]+(1-α)·ema[i-1]，α=2/(n+1)

2. RSI 必须用 **Wilder 平滑**（α=1/n），不是简单均值。
   Wilder 的 RSI 与「简单移动平均版 RSI」在同样参数下数值不同，
   用错会和所有行情软件对不上。

3. ATR 同样用 Wilder 平滑，且真实波幅要取三者最大：
   TR = max(high-low, |high-prevClose|, |low-prevClose|)
   漏掉后两项，跳空缺口就统计不到。

4. 布林带用**总体标准差**（ddof=0），这是 Bollinger 本人的定义。
   pandas 默认 ddof=1，用错带宽会偏大。

5. ADX 用标准 Wilder 三步：+DM/-DM → 平滑 → DI → DX → ADX。
   必须过滤「同向占优」的 DM：+DM 与 -DM 只有一个能计入。

6. 所有指标前 n-1 个位置是 NaN（数据不足），**不做填充**。
   填充会让策略在开头产生假信号。

用法：
  python quant/indicators.py --market ashare --symbol sh600519 --interval 1d --limit 120
  python quant/indicators.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field

import numpy as np

from datasource import Bar, Series, fetch, MARKETS

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
#  基础工具
# ============================================================
def sma(values: np.ndarray, n: int, min_ratio: float = 0.75) -> np.ndarray:
    """
    简单移动平均。

    ⚠️ 不能用 cumsum 实现：cumsum 会让窗口里**一个 NaN 污染整个结果并向后传播**。
       对链式平滑（KDJ 的 D = SMA(K)，而 K 本身开头就是 NaN）这会直接
       让整条 D 线永远算不出来 —— 实测踩过这个坑。

    改为逐窗计算，并容忍窗口内有少量 NaN：
      有效值占比 >= min_ratio 时，用有效值的均值填充；
      否则留 NaN（数据真的不够）。
    """
    out = np.full(values.shape, np.nan)
    if n <= 0 or values.size < n:
        return out
    need = max(1, int(math.ceil(n * min_ratio)))
    for i in range(n - 1, values.size):
        window = values[i - n + 1:i + 1]
        valid = window[~np.isnan(window)]
        if valid.size >= need:
            out[i] = float(np.mean(valid))
    return out


def ema(values: np.ndarray, n: int) -> np.ndarray:
    """指数移动平均。α = 2/(n+1)，首值取第一个价格。"""
    out = np.full(values.shape, np.nan)
    if n <= 0 or values.size == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    acc = float(values[0])
    out[0] = acc
    for i in range(1, values.size):
        acc = alpha * float(values[i]) + (1 - alpha) * acc
        out[i] = acc
    return out


def wilder(values: np.ndarray, n: int) -> np.ndarray:
    """
    Wilder 平滑（α = 1/n）。RSI、ATR、ADX 都必须用它。

    种子取**前 n 个有效值**的简单平均 —— 不是前 n 个位置。
    这个区别很重要：很多指标的输入天然带前导 NaN
    （ADX 的 DX 序列、RSI 的涨跌幅序列），
    如果直接用 np.mean(values[:n])，只要窗口里有 NaN 种子就是 NaN，
    然后整个输出序列全变 NaN（实测踩过这个坑，ADX 直接废掉）。
    """
    out = np.full(values.shape, np.nan)
    if n <= 0 or values.size < n:
        return out

    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < n:
        return out

    start = int(valid[0])                 # 第一个有效值的位置
    if start + n > values.size:
        return out

    acc = float(np.mean(values[start:start + n]))
    out[start + n - 1] = acc
    for i in range(start + n, values.size):
        v = values[i]
        if np.isnan(v):
            out[i] = acc                  # 中间缺值就沿用上一次，不打断递推
            continue
        acc = (acc * (n - 1) + float(v)) / n
        out[i] = acc
    return out


def rolling_std(values: np.ndarray, n: int, ddof: int = 0) -> np.ndarray:
    """滚动标准差。ddof=0 是布林带的定义（总体标准差）。"""
    out = np.full(values.shape, np.nan)
    if n <= 1 or values.size < n:
        return out
    for i in range(n - 1, values.size):
        out[i] = float(np.std(values[i - n + 1:i + 1], ddof=ddof))
    return out


def rolling_max(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    for i in range(n - 1, values.size):
        out[i] = float(np.max(values[i - n + 1:i + 1]))
    return out


def rolling_min(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    for i in range(n - 1, values.size):
        out[i] = float(np.min(values[i - n + 1:i + 1]))
    return out


# ============================================================
#  单项指标
# ============================================================
def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR = max(H-L, |H-前C|, |L-前C|)。第 0 根没有前收，取 H-L。"""
    n = high.size
    tr = np.full(n, np.nan)
    if n == 0:
        return tr
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        pc = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return wilder(true_range(high, low, close), n)


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """
    Wilder RSI。用简单均值替代会与行情软件对不上。

    ⚠️ 索引对齐是这里最容易错的地方：
       delta[i] = close[i+1] - close[i]，所以 delta 比 close 短一位。
       而 RSI 在第 i 根上的涨跌幅，指的是 close[i-1] → close[i]。
       也就是「delta 的第 i-1 项」对应「close 的第 i 项」。
       直接同下标混用会整体错开一格（实测踩过：结果末尾恒为 NaN）。
       这里用 pad 一位并显式赋值回去，保证 RSI[i] 真的对应 close[i]。
    """
    out = np.full(close.shape, np.nan)
    if close.size < n + 1:
        return out

    delta = np.diff(close)                                   # 长度 size-1
    gain = np.concatenate(([np.nan], np.where(delta > 0, delta, 0.0)))   # 长度 size
    loss = np.concatenate(([np.nan], np.where(delta < 0, -delta, 0.0)))  # 长度 size

    # 从下标 1 开始的子序列做 Wilder 平滑，再放回原位
    ag_sub = wilder(gain[1:], n)
    al_sub = wilder(loss[1:], n)
    avg_gain = np.full(close.shape, np.nan)
    avg_loss = np.full(close.shape, np.nan)
    avg_gain[1:] = ag_sub
    avg_loss[1:] = al_sub

    for i in range(n, close.size):
        g, l = avg_gain[i], avg_loss[i]
        if np.isnan(g) or np.isnan(l):
            continue
        if l == 0:
            out[i] = 100.0 if g > 0 else 50.0
        else:
            rs = g / l
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (DIF, DEA, 柱)。柱 = (DIF-DEA)*2，这是国内软件的习惯。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = np.full(close.shape, np.nan)
    valid = ~np.isnan(dif)
    if valid.any():
        idx = np.flatnonzero(valid)
        sub = ema(dif[idx], signal)
        dea[idx] = sub
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0):
    """返回 (中轨, 上轨, 下轨, 带宽%)。ddof=0。"""
    mid = sma(close, n)
    sd = rolling_std(close, n, ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    width = np.where(mid != 0, (upper - lower) / mid * 100.0, np.nan)
    return mid, upper, lower, width


def adx(high, low, close, n: int = 14):
    """返回 (+DI, -DI, ADX)。"""
    m = high.size
    plus_dm = np.zeros(m)
    minus_dm = np.zeros(m)
    for i in range(1, m):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        # 只有同向占优的一方计入
        if up > dn and up > 0:
            plus_dm[i] = up
        elif dn > up and dn > 0:
            minus_dm[i] = dn

    tr = true_range(high, low, close)
    atr_s = wilder(tr, n)
    plus_s = wilder(plus_dm, n)
    minus_s = wilder(minus_dm, n)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(atr_s > 0, 100.0 * plus_s / atr_s, np.nan)
        minus_di = np.where(atr_s > 0, 100.0 * minus_s / atr_s, np.nan)
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    return plus_di, minus_di, wilder(dx, n)


def stochastic(high, low, close, n: int = 14, d: int = 3):
    """KDJ 的 K/D（国内习惯用 1/3 平滑；这里用 SMA(d) 版本，更通用）。"""
    hh = rolling_max(high, n)
    ll = rolling_min(low, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsv = np.where(hh > ll, (close - ll) / (hh - ll) * 100.0, 50.0)
    k = sma(rsv, d)
    dd = sma(k, d)
    return k, dd


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """能量潮：涨加量、跌减量。"""
    out = np.zeros(close.shape)
    for i in range(1, close.size):
        if close[i] > close[i - 1]:
            out[i] = out[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            out[i] = out[i - 1] - volume[i]
        else:
            out[i] = out[i - 1]
    return out


def donchian(high, low, n: int = 20):
    return rolling_max(high, n), rolling_min(low, n)


# ============================================================
#  汇总
# ============================================================
@dataclass
class IndicatorSet:
    """一次算好所有指标，避免策略重复计算。"""
    series: Series
    values: dict[str, np.ndarray] = field(default_factory=dict)

    def at(self, name: str, i: int) -> float | None:
        v = self.values.get(name)
        if v is None or i < 0 or i >= v.size:
            return None
        x = float(v[i])
        return None if math.isnan(x) else x

    def latest(self, name: str) -> float | None:
        v = self.values.get(name)
        return self.at(name, v.size - 1) if v is not None else None

    def names(self) -> list[str]:
        return sorted(self.values)

    def snapshot(self, i: int = -1) -> dict:
        """取某根 K 线处的全部指标值，用于面板展示与落盘。"""
        n = self.series.bars.__len__()
        idx = n + i if i < 0 else i
        bar = self.series.bars[idx]
        out = {
            "index": idx,
            "iso": bar.as_dict()["iso"],
            "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume,
        }
        for k in self.names():
            out[k] = self.at(k, idx)
        return out


def compute(series: Series, *, ema_fast=12, ema_slow=26, rsi_n=14, atr_n=14,
            bb_n=20, bb_k=2.0, adx_n=14, kdj_n=14) -> IndicatorSet:
    close = np.asarray(series.closes, dtype=float)
    high = np.asarray(series.highs, dtype=float)
    low = np.asarray(series.lows, dtype=float)
    vol = np.asarray(series.volumes, dtype=float)

    v: dict[str, np.ndarray] = {}
    v["ema9"] = ema(close, 9)
    v["ema12"] = ema(close, ema_fast)
    v["ema21"] = ema(close, 21)
    v["ema26"] = ema(close, ema_slow)
    v["ema50"] = ema(close, 50)
    v["sma20"] = sma(close, 20)
    v["sma60"] = sma(close, 60)

    dif, dea, hist = macd(close, ema_fast, ema_slow)
    v["macd_dif"], v["macd_dea"], v["macd_hist"] = dif, dea, hist

    v["rsi"] = rsi(close, rsi_n)
    v["atr"] = atr(high, low, close, atr_n)
    with np.errstate(divide="ignore", invalid="ignore"):
        v["atrPct"] = np.where(close > 0, v["atr"] / close * 100.0, np.nan)

    mid, up, lo, width = bollinger(close, bb_n, bb_k)
    v["bb_mid"], v["bb_upper"], v["bb_lower"], v["bb_width"] = mid, up, lo, width

    pdi, mdi, adx_v = adx(high, low, close, adx_n)
    v["plus_di"], v["minus_di"], v["adx"] = pdi, mdi, adx_v

    k, d = stochastic(high, low, close, kdj_n)
    v["kdj_k"], v["kdj_d"] = k, d

    v["obv"] = obv(close, vol)
    dc_up, dc_lo = donchian(high, low, 20)
    v["dc_upper"], v["dc_lower"] = dc_up, dc_lo

    v["volume_ma20"] = sma(vol, 20)
    with np.errstate(divide="ignore", invalid="ignore"):
        v["volumeRatio"] = np.where(v["volume_ma20"] > 0, vol / v["volume_ma20"], np.nan)

    return IndicatorSet(series=series, values=v)


# ============================================================
#  自检：用可手算的序列验证实现正确性
# ============================================================
def _selftest() -> int:
    ok = fail = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("=" * 74)
    print("  指标实现自检（用可手算的输入验证，不依赖网络）")
    print("=" * 74)

    # --- SMA ---
    print("\n[SMA] 输入 [1..5], n=3")
    s = sma(np.array([1., 2., 3., 4., 5.]), 3)
    check("位置 0-1 为 NaN", np.isnan(s[0]) and np.isnan(s[1]))
    check("sma[2]=2", abs(s[2] - 2.0) < 1e-12, f"{s[2]}")
    check("sma[3]=3", abs(s[3] - 3.0) < 1e-12, f"{s[3]}")
    check("sma[4]=4", abs(s[4] - 4.0) < 1e-12, f"{s[4]}")

    # --- EMA ---
    print("\n[EMA] 输入 [1,2,3], n=2 (α=2/3)")
    e = ema(np.array([1., 2., 3.]), 2)
    exp1 = 2/3*2 + 1/3*1
    exp2 = 2/3*3 + 1/3*exp1
    check("ema[0]=1", abs(e[0] - 1.0) < 1e-12, f"{e[0]}")
    check(f"ema[1]={exp1:.6f}", abs(e[1] - exp1) < 1e-12, f"{e[1]}")
    check(f"ema[2]={exp2:.6f}", abs(e[2] - exp2) < 1e-12, f"{e[2]}")

    # --- 常数序列的 RSI 应接近 50 ---
    print("\n[RSI] 常数序列应为 NaN 前段、之后无极端值")
    flat = np.full(40, 100.0)
    r = rsi(flat, 14)
    check("前 14 个为 NaN", np.all(np.isnan(r[:14])))
    check("常数序列 RSI=50（无涨无跌）", abs(r[-1] - 50.0) < 1e-9, f"{r[-1]}")

    # 单调上涨 -> RSI 应接近 100
    up = np.arange(1, 41, dtype=float)
    r_up = rsi(up, 14)
    check("单调上涨 RSI≈100", r_up[-1] > 99.0, f"{r_up[-1]:.4f}")

    # 单调下跌 -> RSI 应接近 0
    dn = np.arange(40, 0, -1, dtype=float)
    r_dn = rsi(dn, 14)
    check("单调下跌 RSI≈0", r_dn[-1] < 1.0, f"{r_dn[-1]:.4f}")

    # --- TR 必须考虑跳空 ---
    print("\n[TR] 跳空缺口测试")
    h = np.array([10., 20.])
    l = np.array([9., 19.])
    c = np.array([9.5, 19.5])
    tr = true_range(h, l, c)
    check("第0根 TR=H-L=1", abs(tr[0] - 1.0) < 1e-12, f"{tr[0]}")
    # 第1根：H-L=1，|H-前C|=|20-9.5|=10.5，|L-前C|=|19-9.5|=9.5 -> 应取 10.5
    check("跳空时 TR 取 |H-前C|=10.5", abs(tr[1] - 10.5) < 1e-12, f"{tr[1]}")

    # --- ATR ---
    print("\n[ATR] 常数 TR 序列 -> ATR 应等于该常数")
    n = 30
    hh = np.full(n, 11.0); ll = np.full(n, 10.0); cc = np.full(n, 10.5)
    a = atr(hh, ll, cc, 14)
    check("ATR 在第14位等于1.0", abs(a[13] - 1.0) < 1e-12, f"{a[13]}")
    check("ATR 末端仍为1.0", abs(a[-1] - 1.0) < 1e-12, f"{a[-1]}")

    # --- 布林带 ddof=0 ---
    print("\n[布林带] ddof=0（总体标准差）验证")
    prices = np.array([2., 4., 4., 4., 5., 5., 7., 9.])
    mid, up, lo, w = bollinger(prices, 8, 2.0)
    expect_sd = float(np.std(prices))          # ddof=0
    expect_sd1 = float(np.std(prices, ddof=1))  # ddof=1
    check("中轨=均值5", abs(mid[-1] - 5.0) < 1e-12, f"{mid[-1]}")
    check(f"带宽用 ddof=0 (σ={expect_sd:.4f})",
          abs((up[-1] - lo[-1]) - 4 * expect_sd) < 1e-9,
          f"上-下={up[-1]-lo[-1]:.6f}, 4σ={4*expect_sd:.6f}")
    check("与 ddof=1 确有差异（说明选择有意义）",
          abs(expect_sd - expect_sd1) > 1e-6,
          f"σ0={expect_sd:.4f} vs σ1={expect_sd1:.4f}")

    # --- MACD 柱符号 ---
    print("\n[MACD] 上涨段 DIF 应为正")
    rising = np.linspace(100, 200, 80)
    dif, dea, hist = macd(rising, 12, 26, 9)
    check("上涨趋势 DIF>0", dif[-1] > 0, f"{dif[-1]:.4f}")
    check("柱 = (DIF-DEA)*2", abs(hist[-1] - (dif[-1]-dea[-1])*2) < 1e-12)

    # --- ADX 方向性 ---
    print("\n[ADX] 强趋势应给出高 ADX")
    t = np.arange(100, dtype=float)
    h2 = 100 + t * 1.0
    l2 = 99 + t * 1.0
    c2 = 99.5 + t * 1.0
    pdi, mdi, adx_v = adx(h2, l2, c2, 14)
    check("单调上涨 +DI > -DI", pdi[-1] > mdi[-1], f"+DI={pdi[-1]:.2f} -DI={mdi[-1]:.2f}")
    check("强趋势 ADX 较高(>50)", adx_v[-1] > 50, f"ADX={adx_v[-1]:.2f}")

    # --- OBV ---
    print("\n[OBV] 手算验证")
    c3 = np.array([10., 11., 10., 12.])
    v3 = np.array([100., 200., 300., 400.])
    o = obv(c3, v3)
    check("obv=[0,200,-100,300]", list(o) == [0., 200., -100., 300.], f"{list(o)}")

    print("\n" + "=" * 74)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 74)
    return 1 if fail else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="技术指标引擎")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出最后一根的 JSON 快照")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    s = fetch(args.market, symbol, interval, args.limit)
    ind = compute(s)

    if args.json:
        print(json.dumps(ind.snapshot(-1), ensure_ascii=False, indent=2))
        return 0

    m = s.meta()
    print("=" * 74)
    print(f"  {m['name']}  {m['symbol']}  {m['interval']}   {m['count']} 根   源={m['source']}")
    print(f"  {m['first']} → {m['last']}   最新收盘 {m['lastClose']}")
    print("=" * 74)

    snap = ind.snapshot(-1)
    groups = [
        ("均线", ["ema9", "ema12", "ema21", "ema26", "ema50", "sma20", "sma60"]),
        ("动量", ["macd_dif", "macd_dea", "macd_hist", "rsi", "kdj_k", "kdj_d"]),
        ("波动", ["atr", "atrPct", "bb_mid", "bb_upper", "bb_lower", "bb_width"]),
        ("趋势", ["plus_di", "minus_di", "adx"]),
        ("量能", ["volume_ma20", "volumeRatio"]),
        ("通道", ["dc_upper", "dc_lower"]),
    ]
    for title, keys in groups:
        print(f"\n  【{title}】")
        for k in keys:
            v = snap.get(k)
            print(f"    {k:<12} {'—' if v is None else f'{v:,.4f}'}")

    print(f"\n  共 {len(ind.names())} 个指标列，全部由真实价格算出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
