#!/usr/bin/env python3
"""
指标回归测试 —— 固化已知正确答案，防止以后改坏。

═══════════════════════════════════════════════════════════════════
  为什么必须有这个文件
═══════════════════════════════════════════════════════════════════
开发过程中，指标实现里出现过 4 个**会静默算错**的 bug：

  #1 wilder() 种子取 values[:n]
     输入含前导 NaN 时 np.mean 返回 NaN，种子 NaN 让整条输出作废。
     症状：ADX 全序列 = NaN。ADX 是趋势判断的核心，废了策略跟着废。
     修法：种子取「前 n 个**有效值**」的均值。

  #2 sma() 用 cumsum 实现
     窗口内只要有一个 NaN，结果就是 NaN 并向后传播。
     KDJ 的 D = SMA(K)，而 K 开头天然是 NaN → **D 线永远算不出来**。
     修法：逐窗计算，容忍窗口内有 NaN（有效占比 >= 75% 即计算）。

  #3 RSI 索引错位一格
     delta = np.diff(close) 比 close 短一位；直接同下标混用会整体错开，
     结果末尾恒为 NaN 且数值全偏。
     修法：pad 一位后显式赋值回原位。

  #4 RSI 数组越界
     avg_gain 长度与 close 不一致，循环到末尾 IndexError。

这四个都不会抛错就悄悄算错 —— 量化里最危险的情况。
所以每个都固化成断言，改代码时必须仍然通过。

运行：python quant/test_indicators.py
"""

from __future__ import annotations

import sys
import math
import numpy as np

from indicators import (
    sma, ema, wilder, true_range, atr, rsi, macd, bollinger,
    adx, stochastic, obv, donchian, rolling_std,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        _FAILURES.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def near(a, b, tol=1e-9) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, float) and math.isnan(a):
        return False
    return abs(float(a) - float(b)) <= tol


def is_nan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


# ============================================================
def test_sma() -> None:
    print("\n─── SMA ───")
    v = np.array([1., 2., 3., 4., 5.])
    s = sma(v, 3)
    check("前两个为 NaN", is_nan(s[0]) and is_nan(s[1]))
    check("sma[2]=2", near(s[2], 2.0), f"{s[2]}")
    check("sma[3]=3", near(s[3], 3.0), f"{s[3]}")
    check("sma[4]=4", near(s[4], 4.0), f"{s[4]}")

    # ★ 回归 #2：前导 NaN 不能污染整个结果
    print("  [回归 #2] 含前导 NaN 的 SMA")
    x = np.full(10, np.nan)
    x[2:] = 1.0
    sx = sma(x, 3)
    check("窗口内 NaN 不过多时仍能算出", not is_nan(sx[-1]), f"sx[-1]={sx[-1]}")
    check("结果正确（全 1 的均值=1）", near(sx[-1], 1.0, 1e-12), f"{sx[-1]}")
    check("开头确实是 NaN（数据真不够）", is_nan(sx[0]) and is_nan(sx[1]))
    # 有效值数不足时必须是 NaN，不能硬凑
    y = np.full(10, np.nan)
    y[8:] = 1.0
    check("有效值不足时保持 NaN", is_nan(sma(y, 3)[-1]))


def test_ema() -> None:
    print("\n─── EMA ───")
    e = ema(np.array([1., 2., 3.]), 2)
    exp1 = 2/3*2 + 1/3*1
    exp2 = 2/3*3 + 1/3*exp1
    check("ema[0]=首值", near(e[0], 1.0))
    check("ema[1]=α·2+(1-α)·1", near(e[1], exp1), f"{e[1]:.8f}")
    check("ema[2] 递推正确", near(e[2], exp2), f"{e[2]:.8f}")


def test_wilder() -> None:
    print("\n─── Wilder 平滑 ───")
    w = wilder(np.arange(1, 41, dtype=float), 14)
    check("种子=前14个的均值 (1..14 -> 7.5)", near(w[13], 7.5), f"{w[13]}")
    check("种子之前为 NaN", is_nan(w[12]))

    # ★ 回归 #1：前导 NaN 时种子必须从第一个有效值起算
    print("  [回归 #1] 含前导 NaN 的 Wilder（ADX 依赖这条）")
    x = np.full(30, np.nan)
    x[5:] = 2.0
    wx = wilder(x, 3)
    check("能算出结果而不是整条 NaN", not is_nan(wx[-1]), f"wx[-1]={wx[-1]}")
    check("常数输入 -> 输出等于该常数", near(wx[-1], 2.0, 1e-12), f"{wx[-1]}")
    check("种子出现在第一个有效值之后 n-1 位 (index 7)", not is_nan(wx[7]) and is_nan(wx[6]),
          f"wx[6]={wx[6]} wx[7]={wx[7]}")


def test_true_range() -> None:
    print("\n─── 真实波幅 TR ───")
    tr = true_range(np.array([10., 20.]), np.array([9., 19.]), np.array([9.5, 19.5]))
    check("第0根 TR=H-L=1", near(tr[0], 1.0))
    # 跳空：H-L=1，|H-前C|=10.5，|L-前C|=9.5 -> 取 10.5
    check("跳空时取 |H-前C|=10.5", near(tr[1], 10.5), f"{tr[1]}")
    check("若漏掉跳空项会得 1.0（错误值）", not near(tr[1], 1.0))


def test_atr() -> None:
    print("\n─── ATR ───")
    n = 30
    a = atr(np.full(n, 11.), np.full(n, 10.), np.full(n, 10.5), 14)
    check("常数 TR=1 -> ATR[13]=1", near(a[13], 1.0), f"{a[13]}")
    check("ATR 末端仍=1", near(a[-1], 1.0), f"{a[-1]}")
    check("ATR 前 13 位为 NaN", all(is_nan(a[i]) for i in range(13)))


def test_rsi() -> None:
    print("\n─── RSI（Wilder）───")
    # ★ 回归 #3/#4：索引对齐与越界
    flat = np.full(40, 100.0)
    r = rsi(flat, 14)
    check("前 14 位为 NaN", all(is_nan(r[i]) for i in range(14)))
    check("[回归 #3] 常数序列末尾=50（不再是 NaN）",
          not is_nan(r[-1]) and near(r[-1], 50.0, 1e-9), f"{r[-1]}")
    check("[回归 #4] 不因长度不一致越界", r.size == flat.size, f"size={r.size}")

    up = np.arange(1, 41, dtype=float)
    r_up = rsi(up, 14)
    check("单调上涨 RSI≈100", not is_nan(r_up[-1]) and r_up[-1] > 99.0, f"{r_up[-1]:.4f}")

    dn = np.arange(40, 0, -1, dtype=float)
    r_dn = rsi(dn, 14)
    check("单调下跌 RSI≈0", not is_nan(r_dn[-1]) and r_dn[-1] < 1.0, f"{r_dn[-1]:.4f}")

    # RSI 必须在 0..100
    rnd = np.cumsum(np.random.RandomState(3).randn(300)) + 100
    rv = rsi(rnd, 14)
    vals = rv[~np.isnan(rv)]
    check("全部取值落在 [0,100]", bool(vals.min() >= 0 and vals.max() <= 100),
          f"[{vals.min():.4f}, {vals.max():.4f}]")
    check("与输入等长（索引对齐）", rv.size == rnd.size)


def test_bollinger() -> None:
    print("\n─── 布林带（ddof=0）───")
    p = np.array([2., 4., 4., 4., 5., 5., 7., 9.])
    mid, up, lo, width = bollinger(p, 8, 2.0)
    sd0 = float(np.std(p, ddof=0))
    sd1 = float(np.std(p, ddof=1))
    check("中轨=均值 5", near(mid[-1], 5.0), f"{mid[-1]}")
    check("带宽=4σ (ddof=0)", near(up[-1] - lo[-1], 4*sd0, 1e-9),
          f"实测 {up[-1]-lo[-1]:.6f} vs 4σ0 {4*sd0:.6f}")
    check("确实不同于 ddof=1（说明这个选择有意义）", abs(sd0 - sd1) > 1e-6,
          f"σ0={sd0:.4f} σ1={sd1:.4f}")
    check("上轨 > 中轨 > 下轨", up[-1] > mid[-1] > lo[-1])


def test_macd() -> None:
    print("\n─── MACD ───")
    rising = np.linspace(100, 200, 80)
    dif, dea, hist = macd(rising, 12, 26, 9)
    check("上涨趋势 DIF>0", dif[-1] > 0, f"{dif[-1]:.4f}")
    check("柱=(DIF-DEA)×2", near(hist[-1], (dif[-1]-dea[-1])*2, 1e-12))
    falling = np.linspace(200, 100, 80)
    d2, _, _ = macd(falling, 12, 26, 9)
    check("下跌趋势 DIF<0", d2[-1] < 0, f"{d2[-1]:.4f}")


def test_adx() -> None:
    print("\n─── ADX（依赖回归 #1）───")
    t = np.arange(100, dtype=float)
    pdi, mdi, dx = adx(100 + t, 99 + t, 99.5 + t, 14)
    check("[回归 #1] ADX 能算出值（曾整条 NaN）", not is_nan(dx[-1]), f"ADX={dx[-1]}")
    check("单调上涨 +DI > -DI", pdi[-1] > mdi[-1], f"+DI={pdi[-1]:.2f} -DI={mdi[-1]:.2f}")
    check("强趋势 ADX 较高 (>50)", dx[-1] > 50, f"ADX={dx[-1]:.2f}")

    # 震荡市 ADX 应该低
    osc = 100 + np.sin(np.arange(200) / 3.0) * 2
    h = osc + 0.5; l = osc - 0.5
    _, _, dx2 = adx(h, l, osc, 14)
    check("震荡市 ADX 低于强趋势", dx2[-1] < dx[-1], f"震荡={dx2[-1]:.2f} vs 趋势={dx[-1]:.2f}")


def test_stochastic() -> None:
    print("\n─── KDJ（依赖回归 #2）───")
    rnd = np.cumsum(np.random.RandomState(7).randn(120)) + 100
    h = rnd + 0.5; l = rnd - 0.5
    k, d = stochastic(h, l, rnd, 14, 3)
    check("[回归 #2] D 线能算出值（曾整条 NaN）", not is_nan(d[-1]), f"D={d[-1]}")
    check("K 有值", not is_nan(k[-1]), f"K={k[-1]}")
    kv = k[~np.isnan(k)]; dv = d[~np.isnan(d)]
    check("K 落在 [0,100]", bool(kv.min() >= -1e-9 and kv.max() <= 100 + 1e-9),
          f"[{kv.min():.2f}, {kv.max():.2f}]")
    check("D 落在 [0,100]", bool(dv.min() >= -1e-9 and dv.max() <= 100 + 1e-9),
          f"[{dv.min():.2f}, {dv.max():.2f}]")
    check("D 比 K 平滑（标准差更小）", float(np.std(dv)) <= float(np.std(kv)) + 1e-9,
          f"σK={np.std(kv):.3f} σD={np.std(dv):.3f}")


def test_obv_donchian() -> None:
    print("\n─── OBV / 唐奇安通道 ───")
    o = obv(np.array([10., 11., 10., 12.]), np.array([100., 200., 300., 400.]))
    check("obv=[0,200,-100,300]", list(o) == [0., 200., -100., 300.], f"{list(o)}")

    h = np.array([1., 5., 3., 8., 2.])
    l = np.array([0., 4., 2., 7., 1.])
    up, lo = donchian(h, l, 3)
    check("20->3 窗口上轨=8", near(up[-1], 8.0), f"{up[-1]}")
    check("窗口下轨=1", near(lo[-1], 1.0), f"{lo[-1]}")


def test_consistency() -> None:
    """跨指标一致性：这些关系必须永远成立，否则实现里一定有 bug。"""
    print("\n─── 跨指标一致性 ───")
    rnd = np.cumsum(np.random.RandomState(11).randn(400)) + 200
    h = rnd + np.abs(np.random.RandomState(12).randn(400)) * 0.8
    l = rnd - np.abs(np.random.RandomState(13).randn(400)) * 0.8
    c = rnd

    check("high >= close >= low 恒成立", bool(np.all(h >= c - 1e-12) and np.all(c >= l - 1e-12)))
    tr = true_range(h, l, c)
    check("TR >= high-low 恒成立", bool(np.all(tr >= (h - l) - 1e-9)))
    check("TR >= 0", bool(np.all(tr >= -1e-12)))

    a = atr(h, l, c, 14)
    av = a[~np.isnan(a)]
    check("ATR > 0", bool((av > 0).all()))
    check("ATR 平滑（比 TR 波动小）",
          float(np.std(av)) < float(np.std(tr[13:])),
          f"σATR={np.std(av):.4f} σTR={np.std(tr[13:]):.4f}")

    mid, up, lo, _ = bollinger(c, 20, 2.0)
    ok = ~(np.isnan(mid) | np.isnan(up) | np.isnan(lo))
    check("布林带 上>=中>=下", bool(np.all(up[ok] >= mid[ok] - 1e-9) and np.all(mid[ok] >= lo[ok] - 1e-9)))

    r = rsi(c, 14)
    rv = r[~np.isnan(r)]
    check("RSI 有效值个数足够（>300）", rv.size > 300, f"{rv.size} 个")


def main() -> int:
    print("=" * 74)
    print("  指标回归测试 —— 固化已知正确答案 + 4 个历史 bug 的回归断言")
    print("=" * 74)
    for fn in (test_sma, test_ema, test_wilder, test_true_range, test_atr,
               test_rsi, test_bollinger, test_macd, test_adx,
               test_stochastic, test_obv_donchian, test_consistency):
        fn()
    print("\n" + "=" * 74)
    if _FAIL:
        print(f"  {_PASS} 通过 / {_FAIL} 失败")
        print("  失败项：")
        for f in _FAILURES:
            print(f"    - {f}")
    else:
        print(f"  全部通过：{_PASS} 项断言")
    print("=" * 74)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
