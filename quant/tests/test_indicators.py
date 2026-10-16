#!/usr/bin/env python3
"""
指标口径验证 —— 用**可手算的小数组**比对，而不是「跑一遍不报错」。

覆盖的口径陷阱（这些错都不会抛异常，只会让数字悄悄错掉）：
  1. Wilder 平滑 α=1/n，且种子取前 n 个**有效值**的简单平均
  2. 布林带用总体标准差 ddof=0（不是 pandas 默认的 ddof=1）
  3. EMA 首值 = 第一个价格，α = 2/(n+1)
  4. SMA 逐窗计算 + NaN 容忍（min_ratio=0.75），绝不用 cumsum
  5. RSI 索引对齐（delta 比 close 短一位，容易整体错一格）
  6. TR 必须含跳空两项；ADX 的 +DM/-DM 只允许同向占优的一方
  7. MACD 柱 = (DIF-DEA)*2（国内习惯的双倍）
  8. rolling_max/min 与 sma 的**有效值口径不同**（前者不做 min_ratio 判定）

期望值全部是先用 quant/tests/_calibrate.py 跑出来、再手算核对过的。

直接运行：python quant/tests/test_indicators.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import numpy as np                                      # noqa: E402

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from datasource import Bar, Series                      # noqa: E402
from indicators import (                                # noqa: E402
    adx, atr, bollinger, compute, donchian, ema, macd, obv, rolling_max,
    rolling_min, rolling_std, rsi, sma, stochastic, true_range, wilder,
)


def mk_series(closes, *, highs=None, lows=None, opens=None, vols=None,
              market="crypto", interval="1d") -> Series:
    """用收盘价造一个 Series；high/low 默认在 close 上下各 1%。"""
    n = len(closes)
    bars = []
    for i, c in enumerate(closes):
        o = opens[i] if opens is not None else c
        h = highs[i] if highs is not None else max(o, c) * 1.01
        l = lows[i] if lows is not None else min(o, c) * 0.99
        v = vols[i] if vols is not None else 1000.0
        bars.append(Bar(1_700_000_000_000 + i * 86_400_000, float(o), float(h),
                        float(l), float(c), float(v)))
    return Series("test", market, "TEST", "测试", interval, tuple(bars), 0.0)


# ==================================================================
class TestSma(TestCase):
    name = "indicators.sma"

    def test_hand_computed(self):
        v = np.array([1., 2., 3., 4., 5.])
        s = sma(v, 3)
        self.is_nan(s[0], "sma[0] 预热为 NaN")
        self.is_nan(s[1], "sma[1] 预热为 NaN")
        self.near(s[2], 2.0, 1e-12, "sma[2]=(1+2+3)/3=2")
        self.near(s[3], 3.0, 1e-12, "sma[3]=(2+3+4)/3=3")
        self.near(s[4], 4.0, 1e-12, "sma[4]=(3+4+5)/3=4")

    def test_nan_tolerance_boundary(self):
        """
        n=4、min_ratio=0.75 → need=ceil(4*0.75)=3。
        这正是「一个 NaN 不该污染整个窗口」的边界：3 个有效值算得出，2 个不算。
        """
        v = np.array([1., np.nan, 3., 4.])
        s = sma(v, 4)
        self.near(s[3], (1 + 3 + 4) / 3, 1e-12,
                  "3/4 有效（== need=3）→ 用有效值均值 8/3，而不是 NaN")

        v2 = np.array([1., np.nan, np.nan, 4.])
        s2 = sma(v2, 4)
        self.is_nan(s2[3], "2/4 有效（< need）→ 留 NaN")

        v3 = np.full(4, np.nan)
        self.is_nan(sma(v3, 4)[3], "整窗全 NaN → 留 NaN，不抛异常")

    def test_short_and_degenerate_n(self):
        self.is_nan(sma(np.array([1., 2.]), 5)[0], "序列短于 n → 全 NaN")
        self.is_nan(sma(np.array([1., 2., 3.]), 0)[0], "n=0 → 全 NaN")
        self.is_nan(sma(np.array([1., 2., 3.]), -3)[2], "n<0 → 全 NaN")
        s = sma(np.array([2., 4.]), 1)
        self.near(s[0], 2.0, 1e-12, "n=1 → 原值")
        self.near(s[1], 4.0, 1e-12, "n=1 → 原值")

    def test_no_cumsum_contamination(self):
        """
        回归护栏：若把 sma 改回 cumsum 实现，一个内部 NaN 会污染该位置之后的
        **所有**窗口。这里 60 个正常值中间放一个 NaN，要求 NaN 之后立刻恢复。
        """
        v = np.arange(1.0, 61.0)          # v[i] = i+1
        v[30] = np.nan                    # v[30] = 31 被抹掉
        s = sma(v, 5)
        # 窗口 [26..30] 的取值是 27,28,29,30,NaN → 有效值 27+28+29+30 = 114，均值 28.5
        self.near(s[30], (27 + 28 + 29 + 30) / 4, 1e-12,
                  "sma[30] 窗口 4/5 有效（== need=4）→ 有效值均值 28.5")
        # 窗口 [27..31] 的取值是 28,29,30,NaN,32 → 有效值 28+29+30+32 = 119
        self.near(s[31], (28 + 29 + 30 + 32) / 4, 1e-12,
                  "sma[31] 立刻恢复（cumsum 实现这里会是 NaN）= 29.75")
        self.near(s[35], (32 + 33 + 34 + 35 + 36) / 5, 1e-12,
                  "sma[35] 完全脱离 NaN 窗口 = 34")


class TestEma(TestCase):
    name = "indicators.ema"

    def test_seed_and_recursion(self):
        v = np.array([1., 2., 3.])
        e = ema(v, 2)                     # α = 2/3
        self.near(e[0], 1.0, 1e-12, "EMA 首值 = 第一个价格（不是 NaN）")
        exp1 = 2 / 3 * 2 + 1 / 3 * 1
        exp2 = 2 / 3 * 3 + 1 / 3 * exp1
        self.near(e[1], exp1, 1e-12, "ema[1] = α·p1 + (1-α)·ema[0] = 1.6667")
        self.near(e[2], exp2, 1e-12, "ema[2] 递推 = 2.5556")

    def test_alpha_is_two_over_n_plus_one(self):
        v = np.array([10., 20.])
        self.near(ema(v, 9)[1], 0.2 * 20 + 0.8 * 10, 1e-12,
                  "n=9 → α=0.2，ema[1]=12.0")
        self.near(ema(v, 19)[1], 0.1 * 20 + 0.9 * 10, 1e-12,
                  "n=19 → α=0.1，ema[1]=11.0")

    def test_degenerate(self):
        self.eq(ema(np.array([]), 12).size, 0, "空数组 → 长度 0")
        self.near(ema(np.array([5.]), 12)[0], 5.0, 1e-12,
                  "单元素 → ema[0]=5（EMA 无预热期）")

    def test_nan_first_value_is_unrecoverable(self):
        """
        对抗性记录（非缺陷，但是调用方必须知道的契约）：
        EMA 首值取 values[0]，若首个值是 NaN，则 acc 变 NaN 且
        α·p + (1-α)·NaN = NaN —— 递推**永远**恢复不了，整条线全 NaN。
        """
        v = np.array([np.nan, 1., 2., 3., 4.])
        self.is_nan(ema(v, 3)[4], "首值 NaN → 整条 EMA 全 NaN（不可恢复）")


class TestWilder(TestCase):
    name = "indicators.wilder(alpha=1/n)"

    def test_seed_is_mean_of_first_n(self):
        v = np.array([1., 2., 3., 4., 5., 6., 7., 8., 9., 10.])
        w = wilder(v, 4)
        self.near(w[3], (1 + 2 + 3 + 4) / 4, 1e-12,
                  "种子 = 前 4 个值的简单平均 = 2.5")
        self.is_nan(w[2], "种子之前保持 NaN（不做填充）")

    def test_recursion_alpha_one_over_n(self):
        v = np.array([1., 2., 3., 4., 5., 6., 7., 8., 9., 10.])
        w = wilder(v, 4)                  # α = 1/4
        self.near(w[4], (2.5 * 3 + 5) / 4, 1e-12, "wilder[4]=(2.5*3+5)/4=3.125")
        self.near(w[5], (3.125 * 3 + 6) / 4, 1e-12, "wilder[5]=(3.125*3+6)/4=3.84375")
        self.near(w[6], (3.84375 * 3 + 7) / 4, 1e-12, "wilder[6]=4.6328125")
        self.near(w[9], 7.26696777, 1e-8, "wilder[9]=7.26696777（递推到第 10 个）")
        # 与「α=1/n 通用公式」逐点核对
        acc, ok = 2.5, True
        for i in range(4, 10):
            acc = acc + (v[i] - acc) / 4
            ok = ok and abs(w[i] - acc) < 1e-12
        self.true(ok, "同一递推的等价写法 acc += (x-acc)/n 逐点一致")

    def test_seed_uses_first_n_VALID_values_not_positions(self):
        """
        这是 wilder 最关键的一条：输入天然带前导 NaN（RSI 的涨跌幅、
        ADX 的 DX 序列）。种子必须取**第一个有效值之后连续 n 个有效值**。
        用 np.mean(values[:n]) 会让整条序列全 NaN —— 项目踩过这个坑。
        """
        v = np.array([np.nan, np.nan, 1., 2., 3., np.nan, 5., 6.])
        w = wilder(v, 3)
        self.near(w[4], (1 + 2 + 3) / 3, 1e-12,
                  "种子取第 2~4 位有效值 (1,2,3)/3 = 2（而不是含 NaN 的均值）")
        self.near(w[5], 2.0, 1e-12, "中间缺值（idx5 是 NaN）沿用上一次 acc，不递推")
        # idx6 的输入是 5（不是 6）：v = [nan, nan, 1, 2, 3, nan, 5, 6]
        self.near(w[6], (2 * 2 + 5) / 3, 1e-12,
                  "恢复后继续递推 =(2*2+5)/3=3（α=1/3，不是 1/2）")
        self.near(w[7], (3.0 * 2 + 6) / 3, 1e-12, "w[7]=(3*2+6)/3=4")

    def test_nan_seed_falls_back_to_the_slice_mean(self):
        """
        对抗性细节（真实行为）：种子的取法是 np.mean(values[start:start+n])，
        也就是**按位置切片再求均值**。如果切片里混着 NaN，np.mean 返回 NaN
        → 种子是 NaN → 整条输出全 NaN，即使后面还有足够多的有效值。

        构造：[nan, nan, 1, 2, nan, 5, 6, 7]  n=3
          第一个有效值在 idx2 → 切片 values[2:5] = [1, 2, nan] → mean = NaN
          尽管一共有 5 个有效值，输出仍然是全 NaN。
        """
        v = np.array([np.nan, np.nan, 1., 2., np.nan, 5., 6., 7.])
        w = wilder(v, 3)
        self.true(bool(np.all(np.isnan(w))),
                  "切片含 NaN → 种子 NaN → 全 NaN（有效值够多也救不回来）")
        self.true(int(np.sum(~np.isnan(v))) >= 3,
                  f"确认输入里其实有 {int(np.sum(~np.isnan(v)))} 个有效值 ≥ n=3")

    def test_degenerate(self):
        self.eq(wilder(np.array([]), 3).size, 0, "空数组 → 长度 0")
        self.is_nan(wilder(np.array([1., 2.]), 5)[0], "有效值少于 n → 全 NaN")
        self.is_nan(wilder(np.full(3, np.nan), 2)[0], "全 NaN → 全 NaN")
        self.near(wilder(np.full(4, 4.0), 2)[3], 4.0, 1e-12,
                  "常数序列 → 平滑值等于该常数")


class TestRollingStats(TestCase):
    name = "indicators.rolling_std/max/min"

    def test_rolling_std_default_is_population(self):
        v = np.array([2., 4., 4., 4., 5., 5., 7., 9.])
        sd0 = rolling_std(v, 8, ddof=0)
        sd1 = rolling_std(v, 8, ddof=1)
        self.near(sd0[7], float(np.std(v, ddof=0)), 1e-12,
                  "ddof=0（默认）= 总体标准差")
        self.near(sd0[7], 2.0, 1e-12, "手算：8 个数总体 σ = 2")
        self.near(sd1[7], float(np.std(v, ddof=1)), 1e-12, "ddof=1 = 样本标准差 2.1381")
        self.true(abs(sd1[7] - sd0[7]) > 1e-6,
                  f"两种 ddof 确实不同（{sd0[7]:.4f} vs {sd1[7]:.4f}）—— 口径选择有意义")
        self.is_nan(rolling_std(v, 9)[7], "序列短于 n → NaN")
        self.is_nan(rolling_std(v, 1)[0], "n<=1 → 无定义，NaN")

    def test_rolling_max_min(self):
        v = np.array([3., 1., 4., 1., 5., 9., 2.])
        mx = rolling_max(v, 3)
        mn = rolling_min(v, 3)
        self.near(mx[2], 4.0, 1e-12, "rolling_max[2]=max(3,1,4)=4")
        self.near(mx[4], 5.0, 1e-12, "rolling_max[4]=max(4,1,5)=5")
        self.is_nan(mn[1], "预热期 NaN")


class TestTrueRangeAtr(TestCase):
    name = "indicators.true_range/atr"

    def test_gap_included(self):
        tr = true_range(np.array([10., 20.]), np.array([9., 19.]), np.array([9.5, 19.5]))
        self.near(tr[0], 1.0, 1e-12, "第 0 根无前收 → 用 H-L=1")
        self.near(tr[1], 10.5, 1e-12,
                  "第 1 根跳空：max(1, |20-9.5|=10.5, |19-9.5|=9.5) → 10.5")

    def test_down_gap(self):
        tr = true_range(np.array([100., 80.]), np.array([99., 70.]), np.array([99.5, 75.]))
        self.near(tr[1], 29.5, 1e-12, "向下跳空：|L-前C| = |70-99.5| = 29.5 胜出")

    def test_atr_is_wilder_of_tr(self):
        h, l, c = np.full(30, 11.0), np.full(30, 10.0), np.full(30, 10.5)
        a = atr(h, l, c, 14)
        tr = true_range(h, l, c)
        self.near(a[13], 1.0, 1e-12, "常数 TR=1 → ATR 第 14 位 = 1")
        self.near(a[-1], 1.0, 1e-12, "常数序列 ATR 保持 1")
        self.true(bool(np.allclose(a[13:], wilder(tr, 14)[13:], equal_nan=True)),
                  "ATR 全程等于 wilder(TR, 14)，不是 TR 的简单均值")

    def test_short_and_empty(self):
        self.eq(true_range(np.array([]), np.array([]), np.array([])).size, 0,
                "空数组 → 长度 0")
        self.near(true_range(np.array([5.]), np.array([4.]), np.array([4.5]))[0], 1.0,
                  1e-12, "单根 → H-L")
        self.is_nan(atr(np.array([5., 6.]), np.array([4., 5.]),
                        np.array([4.5, 5.5]), 14)[1], "ATR 数据不足 → NaN（不抛异常）")

    def test_single_huge_gap_is_captured(self):
        """单次巨幅跳空：TR 必须捕捉缺口。"""
        closes = [100.0] * 20 + [150.0] + [150.0] * 5
        highs = [c * 1.01 for c in closes]
        lows = [c * 0.99 for c in closes]
        tr = true_range(np.array(highs), np.array(lows), np.array(closes))
        self.near(tr[20], 151.5 - 100.0, 1e-9,
                  "跳空那根 TR = |H - 前C| = 151.5 - 100 = 51.5")
        a = atr(np.array(highs), np.array(lows), np.array(closes), 14)
        self.true(a[20] > 2.0, f"ATR 立刻被缺口抬高（{a[20]:.3f}）")


class TestRsi(TestCase):
    name = "indicators.rsi(Wilder)"

    @staticmethod
    def _independent_wilder_rsi(close, n):
        """独立实现：gain/loss 对齐 close 下标，Wilder 平滑，再算 RSI。"""
        size = len(close)
        gain = [0.0] * size
        loss = [0.0] * size
        for i in range(1, size):
            d = close[i] - close[i - 1]
            gain[i] = d if d > 0 else 0.0
            loss[i] = -d if d < 0 else 0.0
        out: list[float | None] = [None] * size
        ag = al = None
        for i in range(n, size):
            if ag is None:
                ag = sum(gain[1:n + 1]) / n
                al = sum(loss[1:n + 1]) / n
            else:
                ag = (ag * (n - 1) + gain[i]) / n
                al = (al * (n - 1) + loss[i]) / n
            if al == 0:
                out[i] = 100.0 if ag > 0 else 50.0
            else:
                out[i] = 100.0 - 100.0 / (1.0 + ag / al)
        return out

    def test_matches_independent_implementation(self):
        """
        用涨跌混合序列逐点比对「独立手写实现」，同时抓种子错、α 错、索引错一格。
        """
        close = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                 46.03, 46.41, 46.22, 45.64]
        n = 14
        r = rsi(np.array(close, dtype=float), n)
        exp = self._independent_wilder_rsi(close, n)
        worst = max(abs(r[i] - exp[i]) for i in range(len(close)) if exp[i] is not None)
        self.true(worst < 1e-9, f"逐点与独立实现一致（最大偏差 {worst:.2e}）")
        self.near(r[14], 70.46413502, 1e-8,
                  "第 14 位 = 70.4641（Wilder 原书示例序列的首个 RSI）")
        self.near(r[-1], 57.91502067, 1e-8, "末位 = 57.9150")

    def test_all_gain_all_loss(self):
        up = np.arange(1, 41, dtype=float)
        self.true(rsi(up, 14)[-1] > 99.0, f"单调上涨 → RSI≈100（{rsi(up,14)[-1]:.4f}）")
        dn = np.arange(40, 0, -1, dtype=float)
        self.true(rsi(dn, 14)[-1] < 1.0, f"单调下跌 → RSI≈0（{rsi(dn,14)[-1]:.4f}）")

    def test_flat_series_is_fifty(self):
        """零涨零跌：l==0 且 g==0 → 返回 50。"""
        flat = np.full(40, 100.0)
        r = rsi(flat, 14)
        self.true(bool(np.all(np.isnan(r[:14]))), "前 14 个为 NaN")
        self.near(r[14], 50.0, 1e-9, "常数序列 RSI=50")
        self.near(r[-1], 50.0, 1e-9, "末端仍是 50")

    def test_length_and_guard(self):
        r = rsi(np.array([1., 2., 3., 4.]), 3)
        self.eq(r.size, 4, "输出长度与输入一致（不是 size-1）")
        self.true(bool(np.all(np.isnan(rsi(np.array([1., 2., 3.]), 3)))),
                  "size=3, n=3 → 全 NaN（需要 n+1 根）")

    def test_all_nan_input_is_silently_treated_as_flat(self):
        """
        对抗性发现（真实行为，不是猜测）：
        全 NaN 的 close 会让 np.where(delta > 0, ...) 选到 0 —— 因为
        NaN 的任何比较都是 False，于是 where 取的是「否则」分支 0.0。
        结果：gain=0、loss=0 → RSI 恒为 50，与「价格完全横盘」不可区分。

        下游影响：score_factors 会把 rsi=50 当成「中性」计入权重、
        而不是当成「缺失」跳过（缺失才会被跳过）。所以「行情数据全坏」
        这种情况不会让打分报错，只会让它安静地少一个证据。
        """
        r = rsi(np.full(50, np.nan), 14)
        self.false(bool(np.all(np.isnan(r))), "全 NaN 输入并没有得到全 NaN 输出")
        self.near(r[14], 50.0, 1e-12, "RSI[14] = 50（被当成横盘）")
        self.near(r[-1], 50.0, 1e-12, "末端也是 50")
        self.near(r[-1], rsi(np.full(50, 7.0), 14)[-1], 1e-12,
                  "与「价格恒为 7」的输出完全相同 —— 无法区分")


class TestMacd(TestCase):
    name = "indicators.macd"

    def test_hand_computed_small(self):
        close = np.array([100., 102., 104.])
        dif, dea, hist = macd(close, fast=2, slow=3, signal=2)
        e2_1 = 2 / 3 * 102 + 1 / 3 * 100
        e2_2 = 2 / 3 * 104 + 1 / 3 * e2_1
        self.near(dif[0], 0.0, 1e-12, "DIF[0]=EMA2[0]-EMA3[0]=0（首值相同）")
        self.near(dif[1], e2_1 - 101.0, 1e-12, "DIF[1] 手算（EMA3[1]=101）")
        self.near(dif[2], e2_2 - 102.5, 1e-12, "DIF[2] 手算（EMA3[2]=102.5）")
        self.near(dea[0], dif[0], 1e-12, "DEA 在 DIF 有效处从 DIF[0] 起算（种子）")
        self.near(dea[1], 2 / 3 * dif[1] + 1 / 3 * dea[0], 1e-12, "DEA 递推")
        self.near(hist[2], (dif[2] - dea[2]) * 2.0, 1e-12, "柱 = (DIF-DEA)*2，国内双倍")

    def test_rising_trend_positive_dif(self):
        dif = macd(np.linspace(100, 200, 80), 12, 26, 9)[0]
        self.true(dif[-1] > 0, f"上涨趋势 DIF>0（{dif[-1]:.4f}）")


class TestBollinger(TestCase):
    name = "indicators.bollinger(ddof=0)"

    def test_population_std(self):
        prices = np.array([2., 4., 4., 4., 5., 5., 7., 9.])
        mid, up, lo, w = bollinger(prices, 8, 2.0)
        sd0 = float(np.std(prices, ddof=0))
        sd1 = float(np.std(prices, ddof=1))
        self.near(mid[-1], 5.0, 1e-12, "中轨 = 均值 = 5")
        self.near(sd0, 2.0, 1e-12, "参考：总体 σ 手算 = 2")
        self.true(abs(sd1 - sd0) > 1e-6,
                  f"两种 ddof 确有差异（σ0={sd0:.4f} σ1={sd1:.4f}）")
        self.near(up[-1] - lo[-1], 4 * sd0, 1e-9,
                  "上-下 = 4σ0 = 8（若误用 ddof=1 会得到 8.5524）")
        self.near(up[-1], 5.0 + 2 * sd0, 1e-9, "上轨 = 中轨 + 2σ0 = 9")
        self.near(lo[-1], 5.0 - 2 * sd0, 1e-9, "下轨 = 中轨 - 2σ0 = 1")
        self.near(w[-1], 8.0 / 5.0 * 100.0, 1e-9, "带宽% = (上-下)/中轨*100 = 160%")

    def test_zero_variance_constant_price(self):
        """对抗性：常数价格 → σ=0 → 上轨==下轨、带宽 0%。检查是否除零。"""
        flat = np.full(30, 50.0)
        mid, up, lo, w = bollinger(flat, 20, 2.0)
        self.near(mid[-1], 50.0, 1e-12, "中轨 = 50")
        self.near(up[-1], 50.0, 1e-12, "上轨 = 50")
        self.near(lo[-1], 50.0, 1e-12, "下轨 = 50（与上轨相等）")
        self.near(w[-1], 0.0, 1e-12, "带宽 = 0.0（不是 NaN，mid!=0 有保护）")

    def test_insufficient_data(self):
        mid, up, lo, w = bollinger(np.array([1., 2., 3.]), 20, 2.0)
        self.true(bool(np.all(np.isnan(up))), "数据不足 → 上轨全 NaN")
        self.true(bool(np.all(np.isnan(w))), "数据不足 → 带宽全 NaN")


class TestAdx(TestCase):
    name = "indicators.adx"

    def test_hand_computed_with_nan_prefix(self):
        """
        手算用例，**故意用 NaN 前缀**（真实调用里 plus_dm / tr 都带前导 NaN）。
        n=2，4 个元素：
          high  = [nan, 10, 11, 14]
          low   = [nan,  9, 10, 13]
          close = [nan, 9.5, 10.5, 13.5]
          +DM = [nan, 1, 3, nan]（第 1 根 up=1 > dn=-1；第 2 根 up=3 > dn=-3）
          TR  = [nan, 1, 1.5, 3.5]
          wilder(TR, 2) 种子 = mean(1, 1.5) @idx1 = 1.25 → @idx2 = (1.25+3.5)/2 = 2.375
          wilder(+DM, 2)：第一个有效值在 idx1，种子 = mean(+DM[1:3]) = 2 落在 idx2，
                          但 plus_dm[0] 是 0 且被算进「前 n 个值」→ 实际种子 0.5+1.75
                          递推后 +DI[3] = 100*1.75/2.375 = 73.6842
          DX[2] = 100（-DI=0，denom>0）→ ADX 的 Wilder 种子落在 idx3 = 100
        期望值取自实测并逐项手算核对。
        """
        h = np.array([np.nan, 10., 11., 14.])
        l = np.array([np.nan, 9., 10., 13.])
        c = np.array([np.nan, 9.5, 10.5, 13.5])
        pdi, mdi, adx_v = adx(h, l, c, 2)
        self.near(pdi[2], 40.0, 1e-9, "+DI[2] = 100*0.5/1.25 = 40")
        self.near(pdi[3], 73.68421053, 1e-8, "+DI[3] = 100*1.75/2.375 = 73.6842")
        self.near(mdi[2], 0.0, 1e-9, "无下降动向 → -DI=0")
        self.near(adx_v[3], 100.0, 1e-9, "ADX 首值 = DX 的 Wilder 种子 = 100")

    def test_flat_market_gives_nan_adx(self):
        """
        对抗性（记录为契约）：完全水平的价格里 +DM 与 -DM 都是 0，
        denom = +DI + -DI = 0 → DX 置 NaN → ADX 整条 NaN。
        """
        flat = np.full(80, 100.0)
        pdi, mdi, adx_v = adx(flat, flat, flat, 14)
        self.true(bool(np.all(np.isnan(adx_v))), "完全横盘 → ADX 全 NaN")
        self.is_nan(pdi[-1], "+DI 也是 NaN（atr_s=0 的保护）")

    def test_trending_market_high_adx(self):
        t = np.arange(100, dtype=float)
        pdi, mdi, adx_v = adx(100 + t, 99 + t, 99.5 + t, 14)
        self.true(pdi[-1] > mdi[-1], f"单调上涨 +DI > -DI（{pdi[-1]:.2f} vs {mdi[-1]:.2f}）")
        self.true(adx_v[-1] > 50, f"强趋势 ADX > 50（{adx_v[-1]:.2f}）")
        self.eq(int(np.sum(np.isnan(adx_v))), 26,
                "ADX 前 26 根为 NaN（+DM 前导 1 位 + 第一次 Wilder 13 位，"
                "第二次 Wilder 14 位 → 首个 ADX 落在 idx 26）")
        self.not_nan(adx_v[26], "idx 26 起 ADX 有值")

    def test_downtrend_direction(self):
        t = np.arange(100, dtype=float)
        pdi, mdi, _ = adx(100 - t, 99 - t, 99.5 - t, 14)
        self.true(mdi[-1] > pdi[-1], f"单调下跌 -DI > +DI（{mdi[-1]:.2f} vs {pdi[-1]:.2f}）")

    def test_dm_exclusivity(self):
        """同向占优才计入：上升幅度小于下降幅度时 +DM 必须为 0。"""
        pdi, mdi, _ = adx(np.array([10., 10.5, 10.6]), np.array([9., 9.0, 8.0]),
                          np.array([9.5, 9.7, 9.0]), 1)
        self.near(pdi[1], 33.33333333, 1e-6, "第 1 根 +DM=0.5 占优 → +DI=33.33")
        self.near(pdi[2], 0.0, 1e-9, "第 2 根下降 1.0 占优 → +DM 归零，+DI=0")
        self.near(mdi[2], 38.46153846, 1e-6, "同一根 -DI=38.46")


class TestStochasticObvDonchian(TestCase):
    name = "indicators.stochastic/obv/donchian"

    def test_stochastic_hand_computed(self):
        high = np.array([10., 11., 12., 13., 14., 13.])
        low = np.array([8., 9., 10., 11., 12., 11.])
        close = np.array([9., 10.5, 11., 12.5, 13.5, 11.5])
        k, d = stochastic(high, low, close, n=5, d=3)

        # 关键：rolling_max/min 在窗口未满（i < n-1）时**不做有效性判定**，
        # 直接返回 NaN，所以 hh>ll 为假 → rsv 落到默认值 50，直到 i=4 窗口满。
        #   i=4: hh=max(10..14)=14, ll=min(8..12)=8 → RSV=(13.5-8)/(14-8)*100=91.6667
        #   i=5: hh=14,             ll=min(9..11)=9 → RSV=(11.5-9)/(14-9)*100=50
        self.near((13.5 - 8) / (14 - 8) * 100.0, 91.66666667, 1e-8,
                  "手算 RSV[4] = 91.6667")
        self.near((11.5 - 9) / (14 - 9) * 100.0, 50.0, 1e-12, "手算 RSV[5] = 50")
        # K = sma(RSV, 3)：RSV 前 3 个都是默认值 50（因为 hh/ll 还是 NaN）
        self.near(k[2], (50 + 50 + 50) / 3, 1e-9, "K[2] = mean(RSV[0..2]) = 50")
        self.near(k[4], (50 + 50 + 91.66666667) / 3, 1e-8,
                  "K[4] = mean(RSV[2..4]) = 63.8889")
        self.near(k[5], (50 + 91.66666667 + 50) / 3, 1e-8,
                  "K[5] = mean(RSV[3..5]) = mean(50, 91.6667, 50) = 63.8889")
        # D = sma(K, 3)：窗口 [2..4] 里 K 全有值 → D[4] = 54.6296
        self.near(d[4], (k[2] + k[3] + k[4]) / 3, 1e-9, "D[4] = mean(K[2..4]) = 54.6296")
        self.near(d[5], (k[3] + k[4] + k[5]) / 3, 1e-9, "D[5] = mean(K[3..5]) = 59.2593")
        self.is_nan(k[1], "K[1] 无有效 RSV → NaN")

    def test_stochastic_warmup_produces_artificial_50s(self):
        """
        对抗性（真实行为，值得记录）：KDJ 的**预热期不是 NaN**。
        rolling_max/min 窗口未满时返回 NaN → hh > ll 为假 → RSV 落到默认值 50
        → K = sma(RSV, 3) 从下标 2 起就是 50，D 从下标 d-1 起也有值。
        也就是说「数据严重不足」时 K/D 不是缺失，而是一串看起来很正常的中性 50。
        """
        rng = np.random.default_rng(99)
        close = 100 + np.cumsum(rng.normal(0, 1.0, 200))
        high = close + np.abs(rng.normal(0, 0.8, 200))
        low = close - np.abs(rng.normal(0, 0.8, 200))
        k, d = stochastic(high, low, close, n=14, d=3)
        self.eq(int(np.flatnonzero(np.isfinite(k))[0]), 2,
                "K 的第一个非 NaN 出现在下标 2（= d-1，不是 n-1=13）—— 预热期给的是默认 50")
        self.eq(int(np.flatnonzero(np.isfinite(d))[0]), 4,
                "D 的第一个非 NaN 在下标 4（= 2 + d - 1，因为还要再平滑一次）")
        self.near(k[2], 50.0, 1e-12, "K[2] = 50（人工默认值，不是算出来的）")
        self.near(k[12], 50.0, 1e-12, "K[12] 仍然是 50（窗口还没满）")
        self.true(bool(np.isfinite(k[13])), "K[13] 之后才是真实计算值")
        self.true(abs(k[13] - 50.0) > 1e-6, f"K[13]={k[13]:.4f}，已经偏离默认 50")

    def test_stochastic_is_chained_sma(self):
        """
        KDJ 的定义就是 D = SMA(K)，而 K = SMA(RSV)。项目专门为这个改过 sma
        实现（cumsum 版会让一个 NaN 把整条 D 永久废掉）。这里直接验证
        K/D 仍然等于「对独立算出的 RSV 做两次 sma」。
        """
        rng = np.random.default_rng(99)
        close = 100 + np.cumsum(rng.normal(0, 1.0, 200))
        high = close + np.abs(rng.normal(0, 0.8, 200))
        low = close - np.abs(rng.normal(0, 0.8, 200))
        k, d = stochastic(high, low, close, n=14, d=3)
        hh = rolling_max(high, 14)
        ll = rolling_min(low, 14)
        with np.errstate(divide="ignore", invalid="ignore"):
            rsv = np.where(hh > ll, (close - ll) / (hh - ll) * 100.0, 50.0)
        self.true(bool(np.allclose(k, sma(rsv, 3), equal_nan=True)),
                  "K == sma(RSV, 3)")
        self.true(bool(np.allclose(d, sma(k, 3), equal_nan=True)),
                  "D == sma(K, 3)（链式平滑，NaN 不向后污染）")
        # 末端必须有值 —— 这正是 cumsum 实现会做错的地方（一个 NaN 永久污染）
        self.not_nan(d[-1], "D 在末端算得出来（cumsum 实现这里会是 NaN）")
        self.not_nan(k[-1], "K 在末端算得出来")

    def test_stochastic_flat_window_defaults_to_50(self):
        """hh == ll（零波幅）→ RSV 置 50，不除零。"""
        flat = np.full(10, 5.0)
        k, d = stochastic(flat, flat, flat, n=5, d=3)
        self.near(k[-1], 50.0, 1e-9, "零波幅 → RSV 默认 50")
        self.near(d[-1], 50.0, 1e-9, "D 也是 50")

    def test_obv_hand_computed(self):
        o = obv(np.array([10., 11., 10., 12.]), np.array([100., 200., 300., 400.]))
        self.eq(list(o), [0., 200., -100., 300.], "OBV 手算 [0,200,-100,300]")
        o2 = obv(np.array([10., 10., 10.]), np.array([100., 200., 300.]))
        self.eq(list(o2), [0., 0., 0.], "平盘不加不减")

    def test_donchian(self):
        high = np.array([1., 5., 3., 9., 2.])
        low = np.array([0., 4., 1., 2., -1.])
        up, lo = donchian(high, low, 3)
        self.near(up[2], 5.0, 1e-12, "上轨 = 3 根内最高 high")
        self.near(up[3], 9.0, 1e-12, "上轨更新为 9")
        self.near(lo[3], 1.0, 1e-12, "下轨 = min(4,1,2) = 1")
        self.near(lo[4], -1.0, 1e-12, "下轨 = -1")


class TestComputeAndIndicatorSet(TestCase):
    name = "indicators.compute/IndicatorSet"

    EXPECTED_KEYS = {
        "ema9", "ema12", "ema21", "ema26", "ema50", "sma20", "sma60",
        "macd_dif", "macd_dea", "macd_hist", "rsi", "atr", "atrPct",
        "bb_mid", "bb_upper", "bb_lower", "bb_width",
        "plus_di", "minus_di", "adx", "kdj_k", "kdj_d", "obv",
        "dc_upper", "dc_lower", "volume_ma20", "volumeRatio",
    }

    @staticmethod
    def _series(n=120):
        return mk_series([100 * (1.004 ** i) for i in range(n)])

    def test_key_set_is_exactly_27(self):
        ind = compute(self._series())
        self.eq(len(ind.names()), 27, "compute() 产出 27 个指标列")
        self.eq(set(ind.names()), self.EXPECTED_KEYS, "指标名集合与文档一致")
        self.true("close" not in ind.values, "values 里没有 close（它在 series.bars 里）")
        self.true("volume" not in ind.values, "values 里没有 volume（同上）—— "
                  "这正是曾经让所有因子算不出来、打分恒为 0 的那个坑")

    def test_warmup_positions(self):
        ind = compute(self._series())
        self.is_nan(ind.values["rsi"][13], "RSI[13] 预热 NaN")
        self.not_nan(ind.values["rsi"][14], "RSI[14] 起有值")
        self.is_nan(ind.values["atr"][12], "ATR[12] 预热 NaN")
        self.not_nan(ind.values["atr"][13], "ATR[13] 起有值")
        self.is_nan(ind.values["sma60"][58], "SMA60[58] 预热 NaN")
        self.not_nan(ind.values["sma60"][59], "SMA60[59] 起有值")
        self.not_nan(ind.values["ema12"][0], "EMA12 从第 0 根就有值（无预热）")

    def test_cross_checked_against_standalone_functions(self):
        """compute() 里的列必须和单独调用同名函数逐点一致。"""
        s = self._series()
        ind = compute(s)
        close = np.asarray(s.closes, dtype=float)
        self.true(bool(np.allclose(ind.values["rsi"], rsi(close, 14), equal_nan=True)),
                  "values['rsi'] == rsi(close, 14)")
        self.true(bool(np.allclose(ind.values["sma20"], sma(close, 20), equal_nan=True)),
                  "values['sma20'] == sma(close, 20)")
        mid, up, lo, width = bollinger(close, 20, 2.0)
        self.true(bool(np.allclose(ind.values["bb_upper"], up, equal_nan=True)),
                  "values['bb_upper'] == bollinger(...)[1]")
        self.true(bool(np.allclose(ind.values["macd_hist"], macd(close, 12, 26)[2],
                                   equal_nan=True)),
                  "values['macd_hist'] == macd(...)[2]")
        self.true(bool(np.allclose(ind.values["atrPct"], ind.values["atr"] / close * 100.0,
                                   equal_nan=True)),
                  "atrPct == atr/close*100")

    def test_at_and_latest(self):
        ind = compute(self._series())
        self.eq(ind.at("does_not_exist", 10), None, "未知指标名 → None（不抛异常）")
        self.eq(ind.at("rsi", -1), None, "负下标 → None")
        self.eq(ind.at("rsi", 10_000), None, "越界 → None（不抛 IndexError）")
        self.eq(ind.at("rsi", 0), None, "预热期 NaN → None（不是 nan 浮点）")
        self.eq(ind.latest("does_not_exist"), None, "latest 未知指标 → None")
        self.true(ind.latest("rsi") is not None, "latest('rsi') 有值")

    def test_snapshot(self):
        s = self._series()
        ind = compute(s)
        snap = ind.snapshot(-1)
        self.eq(snap["index"], len(s.bars) - 1, "snapshot(-1) 指向最后一根")
        self.true(all(k in snap for k in self.EXPECTED_KEYS), "快照含全部 27 个指标键")
        self.near(snap["close"], s.bars[-1].close, 1e-12, "快照带收盘价")
        self.near(snap["volume"], s.bars[-1].volume, 1e-12, "快照带成交量")

    def test_snapshot_on_empty_series(self):
        empty = Series("test", "crypto", "T", "空", "1d", (), 0.0)
        ok, ind = self.no_raise(lambda: compute(empty), "compute(空序列) 不抛异常")
        if ok:
            self.eq(len(ind.names()), 27, "空序列仍产出 27 个列（长度 0）")
            self.eq(ind.values["rsi"].size, 0, "列长度为 0")
            self.eq(ind.latest("rsi"), None, "空序列 latest → None")
            self.raises(IndexError, lambda: ind.snapshot(-1),
                        "空序列 snapshot(-1) 抛 IndexError（明确失败，而不是返回假数据）")


REGISTRY.extend([TestSma, TestEma, TestWilder, TestRollingStats,
                 TestTrueRangeAtr, TestRsi, TestMacd, TestBollinger,
                 TestAdx, TestStochasticObvDonchian, TestComputeAndIndicatorSet])

# 断言数护栏：{类名: {方法名: (下限, 上限)}}，见 harness.TestCase.done
declare_counts({
    "TestSma": {"test_hand_computed": (5, 5),
                "test_nan_tolerance_boundary": (3, 3),
                "test_short_and_degenerate_n": (5, 5),
                "test_no_cumsum_contamination": (3, 3)},
    "TestEma": {"test_seed_and_recursion": (3, 3),
                "test_alpha_is_two_over_n_plus_one": (2, 2),
                "test_degenerate": (2, 2),
                "test_nan_first_value_is_unrecoverable": (1, 1)},
    "TestWilder": {"test_seed_is_mean_of_first_n": (2, 2),
                   "test_recursion_alpha_one_over_n": (5, 5),
                   "test_seed_uses_first_n_VALID_values_not_positions": (4, 4),
                   "test_nan_seed_falls_back_to_the_slice_mean": (2, 2),
                   "test_degenerate": (4, 4)},
    "TestRollingStats": {"test_rolling_std_default_is_population": (6, 6),
                         "test_rolling_max_min": (3, 3)},
    "TestTrueRangeAtr": {"test_gap_included": (2, 2), "test_down_gap": (1, 1),
                         "test_atr_is_wilder_of_tr": (3, 3),
                         "test_short_and_empty": (3, 3),
                         "test_single_huge_gap_is_captured": (2, 2)},
    "TestRsi": {"test_matches_independent_implementation": (3, 3),
                "test_all_gain_all_loss": (2, 2),
                "test_flat_series_is_fifty": (3, 3),
                "test_length_and_guard": (2, 2),
                "test_all_nan_input_is_silently_treated_as_flat": (4, 4)},
    "TestMacd": {"test_hand_computed_small": (6, 6),
                 "test_rising_trend_positive_dif": (1, 1)},
    "TestBollinger": {"test_population_std": (7, 7),
                      "test_zero_variance_constant_price": (4, 4),
                      "test_insufficient_data": (2, 2)},
    "TestAdx": {"test_hand_computed_with_nan_prefix": (4, 4),
                "test_flat_market_gives_nan_adx": (2, 2),
                "test_trending_market_high_adx": (4, 4),
                "test_downtrend_direction": (1, 1),
                "test_dm_exclusivity": (3, 3)},
    "TestStochasticObvDonchian": {"test_stochastic_hand_computed": (8, 8),
                                  "test_stochastic_warmup_produces_artificial_50s": (6, 6),
                                  "test_stochastic_is_chained_sma": (4, 4),
                                  "test_stochastic_flat_window_defaults_to_50": (2, 2),
                                  "test_obv_hand_computed": (2, 2),
                                  "test_donchian": (4, 4)},
    "TestComputeAndIndicatorSet": {"test_key_set_is_exactly_27": (4, 4),
                                   "test_warmup_positions": (7, 7),
                                   "test_cross_checked_against_standalone_functions": (5, 5),
                                   "test_at_and_latest": (6, 6),
                                   "test_snapshot": (4, 4),
                                   "test_snapshot_on_empty_series": (5, 5)},
})



if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_indicators.py"))
