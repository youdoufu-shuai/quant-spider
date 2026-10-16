#!/usr/bin/env python3
"""探索/校准脚本 2：ADX 手算用例、随机对比、不变量检查。不参与 run.py 收集。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import indicators as I  # noqa: E402
from datasource import Bar, Series  # noqa: E402

np.set_printoptions(precision=8, suppress=False, linewidth=200)

print("== ADX 手算用例（NaN 前缀，模拟真实调用方式 n=2）==")
h = np.array([np.nan, 10., 11., 14.])
l = np.array([np.nan, 9., 10., 13.])
c = np.array([np.nan, 9.5, 10.5, 13.5])
pdi, mdi, ax = I.adx(h, l, c, 2)
print("pdi", pdi, "\nmdi", mdi, "\nadx", ax)
print("wilder(plus,2)", I.wilder(np.array([np.nan, 1., 3., np.nan]), 2))
print("wilder(tr,2)", I.wilder(np.array([np.nan, 1., 1.5, 3.5]), 2))

print("\n== ADX: 前导 0 会被当成有效值（与 NaN 前缀行为不同）==")
p0, m0, _ = I.adx(np.array([10., 11., 14.]), np.array([9., 10., 13.]), np.array([9.5, 10.5, 13.5]), 2)
print("pdi", p0)

print("\n== 随机序列：compute 各列 vs 独立实现 ==")
rng = np.random.default_rng(1234)
n = 400
close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
vol = np.abs(rng.normal(1e6, 2e5, n))
bars = tuple(Bar(1_700_000_000_000 + i * 86_400_000, close[i], high[i], low[i], close[i], vol[i])
             for i in range(n))
s = Series("t", "crypto", "T", "t", "1d", bars, 0.0)
ind = I.compute(s)


def mrs(close, nn=14):
    size = len(close)
    gain = [None] * size
    loss = [None] * size
    for i in range(1, size):
        d = close[i] - close[i - 1]
        gain[i] = d if d > 0 else 0.0
        loss[i] = -d if d < 0 else 0.0
    out = [None] * size
    ag = al = None
    for i in range(1, size):
        if i < nn:
            continue
        if ag is None:
            ag = sum(gain[1:nn + 1]) / nn
            al = sum(loss[1:nn + 1]) / nn
        else:
            ag = (ag * (nn - 1) + gain[i]) / nn
            al = (al * (nn - 1) + loss[i]) / nn
        out[i] = 50.0 if al == 0 and ag == 0 else (100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


exp_rsi = mrs(list(close), 14)
diff = max((abs(ind.values["rsi"][i] - exp_rsi[i]) for i in range(n) if exp_rsi[i] is not None), default=-1)
print("RSI max abs diff vs independent impl:", diff)

# 不变量：R² 不必，检查 bb_upper >= bb_mid >= bb_lower（k>0）
mid, up, lo = ind.values["bb_mid"], ind.values["bb_upper"], ind.values["bb_lower"]
mask = ~np.isnan(mid)
print("bollinger ordering holds:", bool(np.all(up[mask] >= mid[mask] - 1e-12) and np.all(lo[mask] <= mid[mask] + 1e-12)))
print("hist == (dif-dea)*2:", bool(np.allclose(ind.values["macd_hist"], (ind.values["macd_dif"] - ind.values["macd_dea"]) * 2, equal_nan=True)))
print("volRatio == vol/volMA20:", bool(np.allclose(ind.values["volumeRatio"], vol / ind.values["volume_ma20"], equal_nan=True)))

print("\n== 边界：全 NaN 输入 ==")
nan_arr = np.full(50, np.nan)
for fn, args in [("sma", (nan_arr, 5)), ("ema", (nan_arr, 5)), ("wilder", (nan_arr, 5)),
                 ("rolling_std", (nan_arr, 5)), ("rolling_max", (nan_arr, 5)),
                 ("rolling_min", (nan_arr, 5)), ("rsi", (nan_arr, 14)),
                 ("bollinger", (nan_arr, 20)), ("stochastic", (nan_arr, nan_arr, nan_arr, 5))]:
    try:
        out = getattr(I, fn)(*args)
        arr = out[0] if isinstance(out, tuple) else out
        print(f"  {fn}: ok, all nan = {bool(np.all(np.isnan(arr)))}")
    except Exception as e:
        print(f"  {fn}: RAISED {type(e).__name__}: {e}")

print("\n== 边界：全 NaN 的 compute ==")
bars_nan = tuple(Bar(1_700_000_000_000 + i * 86_400_000, np.nan, np.nan, np.nan, np.nan, np.nan)
                 for i in range(50))
try:
    indn = I.compute(Series("t", "crypto", "T", "t", "1d", bars_nan, 0.0))
    print("  compute ok, keys", len(indn.names()), "snapshot[-1] ok:", bool(indn.snapshot(-1)))
except Exception as e:
    print("  compute RAISED", type(e).__name__, e)

print("\n== 边界：常数价格 compute（零方差）==")
bars_flat = tuple(Bar(1_700_000_000_000 + i * 86_400_000, 50., 50., 50., 50., 1000.)
                  for i in range(60))
indf = I.compute(Series("t", "crypto", "T", "t", "1d", bars_flat, 0.0))
for k in ("bb_upper", "bb_lower", "bb_width", "rsi", "atr", "atrPct", "adx", "kdj_k",
          "volumeRatio", "macd_hist", "ema12"):
    v = indf.values[k][-1]
    print(f"  {k:12} = {v}")

print("\n== 边界：价格含 0 / 负数 ==")
for label, px in [("含 0", [10., 10., 0., 10., 10.]),
                  ("负数", [10., 10., -5., 10., 10.]),
                  ("全 0", [0., 0., 0., 0., 0.])]:
    bb = tuple(Bar(1_700_000_000_000 + i * 86_400_000, p, p, p, p, 100.) for i, p in enumerate(px * 6))
    try:
        ii = I.compute(Series("t", "crypto", "T", "t", "1d", bb, 0.0))
        print(f"  {label}: atrPct[-1]={ii.values['atrPct'][-1]}, bb_width[-1]={ii.values['bb_width'][-1]}, "
              f"rsi[-1]={ii.values['rsi'][-1]}, volumeRatio[-1]={ii.values['volumeRatio'][-1]}")
    except Exception as e:
        print(f"  {label}: RAISED {type(e).__name__}: {e}")

print("\n== 边界：空 / 单根 / 两根 ==")
for label, k in [("空", 0), ("单根", 1), ("两根", 2)]:
    bb = tuple(Bar(1_700_000_000_000 + i * 86_400_000, 10., 11., 9., 10., 100.) for i in range(k))
    try:
        ii = I.compute(Series("t", "crypto", "T", "t", "1d", bb, 0.0))
        print(f"  {label}: ok, rsi size {ii.values['rsi'].size}, latest rsi {ii.latest('rsi')}")
    except Exception as e:
        print(f"  {label}: RAISED {type(e).__name__}: {e}")
