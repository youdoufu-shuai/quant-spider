#!/usr/bin/env python3
"""探索/校准脚本：把待写进测试的手算期望值先跑出来看。不参与 run.py 收集。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from indicators import (adx, atr, bollinger, compute, ema, macd, obv, rsi,  # noqa: E402
                        sma, stochastic, true_range, wilder, rolling_std)
from datasource import Bar, Series  # noqa: E402

np.set_printoptions(precision=8, suppress=False, linewidth=200)

print("== wilder n=4 on 1..10 ==")
w = wilder(np.arange(1., 11.), 4)
print(w)

print("\n== wilder seed with leading NaN ==")
print(wilder(np.array([np.nan, np.nan, 1., 2., 3., np.nan, 5., 6.]), 3))

print("\n== sma NaN tolerance ==")
print(sma(np.array([1., np.nan, 3., 4.]), 4))
print(sma(np.array([1., np.nan, np.nan, 4.]), 4))
v = np.arange(1.0, 61.0); v[30] = np.nan
s = sma(v, 5)
print("sma[30]", s[30], "sma[31]", s[31], "sma[35]", s[35])

print("\n== rsi reference series ==")
close = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
         45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
         46.03, 46.41, 46.22, 45.64]
r = rsi(np.array(close, float), 14)
print(r)
print("first RSI@14 =", r[14])
print("flat 40 ->", rsi(np.full(40, 100.0), 14)[14], rsi(np.full(40, 100.0), 14)[-1])
print("len3 n3 all nan:", np.all(np.isnan(rsi(np.array([1.,2.,3.]), 3))))

print("\n== bollinger classic ==")
p = np.array([2., 4., 4., 4., 5., 5., 7., 9.])
mid, up, lo, wd = bollinger(p, 8, 2.0)
print("mid", mid[-1], "up", up[-1], "lo", lo[-1], "width", wd[-1])
print("sd0", np.std(p), "sd1", np.std(p, ddof=1))
flat = bollinger(np.full(30, 50.0), 20, 2.0)
print("flat mid/up/lo/width", flat[0][-1], flat[1][-1], flat[2][-1], flat[3][-1])

print("\n== macd small ==")
d, e, h = macd(np.array([100., 102., 104.]), fast=2, slow=3, signal=2)
print("dif", d, "\ndea", e, "\nhist", h)

print("\n== adx n=2 hand case ==")
h3 = np.array([10., 11., 14.]); l3 = np.array([9., 10., 13.]); c3 = np.array([9.5, 10.5, 13.5])
pdi, mdi, ax = adx(h3, l3, c3, 2)
print("pdi", pdi, "mdi", mdi, "adx", ax)

print("\n== adx flat ==")
flat80 = np.full(80, 100.0)
p2, m2, a2 = adx(flat80, flat80, flat80, 14)
print("pdi[-1]", p2[-1], "adx all nan?", np.all(np.isnan(a2)))

print("\n== adx trending ==")
t = np.arange(100, dtype=float)
p3, m3, a3 = adx(100 + t, 99 + t, 99.5 + t, 14)
print("pdi[-1]", p3[-1], "mdi[-1]", m3[-1], "adx[-1]", a3[-1],
      "nan prefix count", int(np.sum(np.isnan(a3))))

print("\n== adx dm exclusivity ==")
p4, m4, _ = adx(np.array([10., 10.5, 10.6]), np.array([9., 9.0, 8.0]), np.array([9.5, 9.7, 9.0]), 1)
print("pdi", p4, "mdi", m4)

print("\n== stochastic n=5 d=3 ==")
high = np.array([10., 11., 12., 13., 14., 13.])
low = np.array([8., 9., 10., 11., 12., 11.])
cls = np.array([9., 10.5, 11., 12.5, 13.5, 11.5])
k, dd = stochastic(high, low, cls, n=5, d=3)
print("k", k, "\nd", dd)
kf, df = stochastic(np.full(10, 5.0), np.full(10, 5.0), np.full(10, 5.0), n=5, d=3)
print("flat k[-1]", kf[-1], "d[-1]", df[-1])

print("\n== obv ==")
print(obv(np.array([10., 11., 10., 12.]), np.array([100., 200., 300., 400.])))

print("\n== atr gap ==")
closes = [100.0] * 20 + [150.0] + [150.0] * 5
highs = [c * 1.01 for c in closes]; lows = [c * 0.99 for c in closes]
tr = true_range(np.array(highs), np.array(lows), np.array(closes))
print("tr[20]", tr[20], "atr[20]", atr(np.array(highs), np.array(lows), np.array(closes), 14)[20])

print("\n== rolling_std ddof ==")
print(rolling_std(p, 8, 0)[7], rolling_std(p, 8, 1)[7], rolling_std(p, 9)[7], rolling_std(p, 1)[0])

print("\n== ema empty/n=0 ==")
print("empty size", ema(np.array([]), 12).size, "single", ema(np.array([5.]), 12))
print("nan first:", ema(np.array([np.nan, 1., 2., 3., 4.]), 3))
print("sma n=0:", sma(np.array([1., 2., 3.]), 0), "sma n=-3:", sma(np.array([1., 2., 3.]), -3))

print("\n== compute on 120 rising ==")
close120 = [100 * (1.004 ** i) for i in range(120)]
bars = tuple(Bar(1_700_000_000_000 + i * 86_400_000, c, c * 1.01, c * 0.99, c, 1000.0)
             for i, c in enumerate(close120))
s = Series("test", "crypto", "TEST", "测试", "1d", bars, 0.0)
ind = compute(s)
print("keys", len(ind.names()), sorted(ind.names()))
print("rsi[13]", ind.values["rsi"][13], "rsi[14]", ind.values["rsi"][14])
print("atr[12]", ind.values["atr"][12], "atr[13]", ind.values["atr"][13])
print("sma60[58]", ind.values["sma60"][58], "sma60[59]", ind.values["sma60"][59])
print("at missing", ind.at("nope", 3), "at neg", ind.at("rsi", -1),
      "at oob", ind.at("rsi", 10_000), "at warmup", ind.at("rsi", 0))
print("latest nope", ind.latest("nope"))

print("\n== compute on empty ==")
empty = Series("test", "crypto", "T", "空", "1d", (), 0.0)
ie = compute(empty)
print("keys", len(ie.names()), "rsi size", ie.values["rsi"].size, "latest", ie.latest("rsi"))
try:
    ie.snapshot(-1)
    print("snapshot(-1) 没抛异常 —— 需要改测试期望")
except Exception as ex:
    print("snapshot(-1) raised", type(ex).__name__, ex)
