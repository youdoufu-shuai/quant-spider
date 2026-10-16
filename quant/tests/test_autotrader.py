#!/usr/bin/env python3
"""
自动交易逻辑验证 —— 三层结构（信号 / 仓位 / 风控）与打分模型。

重点（这些地方出问题不会抛异常，只会做出错误决策）：
  1. 因子口径：ATR 归一化、MACD 用 DIF 而不是柱、%B 的除零保护
  2. 状态自适应权重：趋势市 vs 震荡市，权重不同、总分也不同
  3. 风控优先级：止损/止盈/移动止损/回撤熔断必须压过信号
  4. 冷却期、最短持有、分档阈值
  5. AI 输出夹紧（不能突破 max_fraction），AI 不可用时用内置逻辑兜底

为了把因子口径**精确**测出来，这里用桩 IndicatorSet 直接给定指标值，
而不是只喂价格让 compute() 顺带算出来 —— 那样测的是指标不是打分。

直接运行：python quant/tests/test_autotrader.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from datasource import Bar, Series                                   # noqa: E402
from indicators import compute                                       # noqa: E402
from autotrader import (                                             # noqa: E402
    ADX_RANGE_MAX, ADX_TREND_MIN, FACTOR_WEIGHTS_RANGING, FACTOR_WEIGHTS_TRENDING,
    AIDecider, AutoConfig, AutoDecider, Decision, RiskConfig,
    explain_factors, make_decider, regime_weights, score_factors,
)


# ------------------------------------------------------------------
#  造数据的小工具
# ------------------------------------------------------------------
def mk_series(closes, *, vols=None, market="crypto", interval="1d") -> Series:
    bars = []
    for i, c in enumerate(closes):
        h = c * 1.01
        l = c * 0.99
        v = vols[i] if vols is not None else 1000.0
        bars.append(Bar(1_700_000_000_000 + i * 86_400_000, float(c), float(h),
                        float(l), float(c), float(v)))
    return Series("test", market, "TEST", "测试", interval, tuple(bars), 0.0)


def ind_of(closes, vols=None):
    return compute(mk_series(closes, vols=vols))


def ramp(n=120, rate=1.012, start=100.0):
    """单边上涨。"""
    return [start * (rate ** i) for i in range(n)]


def decay(n=120, rate=0.988, start=100.0):
    """单边下跌。"""
    return [start * (rate ** i) for i in range(n)]


def choppy(n=200, seed=7, vol=0.25):
    """震荡市：均值回复的随机游走。"""
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = 100.0
    for i in range(1, n):
        x[i] = x[i - 1] + rng.normal(0, vol) - 0.15 * (x[i - 1] - 100.0)
    return list(x)


class StubInd:
    """桩 IndicatorSet：直接给定某一下标的指标值。用于精确验证因子口径。"""

    def __init__(self, close: float, **vals):
        # 注意：close/volume 必须放在 series.bars 里 —— autotrader._val 对
        # 这五个原始行情字段是**先去 series.bars 找**的，放进 values 字典没用。
        bar = Bar(1_700_000_000_000, close, close, close, close,
                  float(vals.pop("series_volume", 0.0)))
        self.series = Series("stub", "crypto", "STUB", "桩", "1d", (bar,), 0.0)
        self.values = {k: np.array([v], dtype=float) for k, v in vals.items()}


def decide_kwargs(**over):
    kw = dict(i=0, position=0, fraction=0.0, entry_price=0.0, peak_price=0.0,
              drawdown=0.0, bars_since_exit=999, bars_held=0)
    kw.update(over)
    return kw


def last_close(ind) -> float:
    """
    取最后一根收盘价。

    注意：不能写 ind.at("close", i) —— IndicatorSet.values 里**没有** close
    （它是原始行情，存在 series.bars 里），at() 会返回 None。
    这正是 autotrader._val 专门做回落查询的原因。
    """
    return float(ind.series.bars[-1].close)


# ==================================================================
class TestRegimeWeights(TestCase):
    name = "autotrader.regime_weights"

    def test_trending_and_ranging_are_different(self):
        w_t, r_t = regime_weights(30.0)
        w_r, r_r = regime_weights(15.0)
        self.eq(r_t, "趋势市", "ADX=30 ≥ 25 → 趋势市")
        self.eq(r_r, "震荡市", "ADX=15 ≤ 20 → 震荡市")
        self.near(w_t["trend"], FACTOR_WEIGHTS_TRENDING["trend"], 1e-12,
                  "趋势市用 TRENDING 权重")
        self.near(w_r["reversion"], FACTOR_WEIGHTS_RANGING["reversion"], 1e-12,
                  "震荡市用 RANGING 权重")
        self.true(w_t["trend"] > w_r["trend"],
                  f"趋势市更看重趋势（{w_t['trend']} vs {w_r['trend']}）")
        self.true(w_r["reversion"] > w_t["reversion"],
                  f"震荡市更看重超买超卖（{w_r['reversion']} vs {w_t['reversion']}）")
        self.true(w_t["reversion"] < 0.3,
                  "趋势市把均值回归权重压到 0.15 —— 这正是修过的那个坑")

    def test_transition_is_linear(self):
        w, name = regime_weights(22.5)     # 20~25 的中点
        self.eq(name, "过渡", "ADX=22.5 落在过渡区")
        mid = (FACTOR_WEIGHTS_RANGING["trend"] + FACTOR_WEIGHTS_TRENDING["trend"]) / 2
        self.near(w["trend"], mid, 1e-9, "过渡区权重 = 两端线性插值的中点")

    def test_boundary_values(self):
        self.eq(regime_weights(ADX_TREND_MIN)[1], "趋势市", "ADX=25 边界归趋势市")
        self.eq(regime_weights(ADX_RANGE_MAX)[1], "震荡市", "ADX=20 边界归震荡市")
        self.eq(regime_weights(None)[1], "未知", "ADX 缺失 → 状态未知")
        self.near(regime_weights(None)[0]["trend"], FACTOR_WEIGHTS_TRENDING["trend"],
                  1e-12, "ADX 缺失时用趋势市权重（保守：宁可跟趋势也不乱抄底）")

    def test_weights_are_copies(self):
        """返回的字典必须是副本，否则调用方改动会污染全局常量。"""
        w, _ = regime_weights(30.0)
        w["trend"] = 999.0
        self.ne(FACTOR_WEIGHTS_TRENDING["trend"], 999.0,
                "修改返回值不影响全局常量（返回的是 copy）")


class TestScoreFactors(TestCase):
    name = "autotrader.score_factors"

    def test_trend_factor_normalized_by_atr(self):
        """
        趋势因子 = (ema12-ema26) / (1.5 × ATR)，夹到 ±1。
        ATR 归一化是关键：绝对百分比阈值对加密（一天动 5%）和指数（一天动
        0.5%）不可能通用 —— 项目为此踩过坑（每天涨 1.2% 的序列算出 +0.00）。
        """
        ind = StubInd(100.0, ema12=110.0, ema26=100.0, atr=10.0, adx=30.0)
        f, score, regime = score_factors(ind, 0)
        self.near(f["trend"], 10.0 / (1.5 * 10.0), 1e-12,
                  "trend = 10/(1.5×10) = 0.6667（正好 1 倍 ATR 差 → 0.667）")
        self.near(score, round(f["trend"], 12), 1e-9,
                  "只有 trend 一个因子时总分就等于它（按有效因子归一化）")
        self.eq(regime, "趋势市", "ADX=30 → 趋势市")

    def test_trend_factor_clamped(self):
        ind = StubInd(100.0, ema12=400.0, ema26=100.0, atr=10.0, adx=30.0)
        f, _, _ = score_factors(ind, 0)
        self.near(f["trend"], 1.0, 1e-12, "差值远超 1.5 ATR → 夹到 +1")

    def test_scale_falls_back_to_one_percent_of_price(self):
        """ATR 缺失时用 close × 1% 当尺度；连 close 都没有就跳过该因子。"""
        ind = StubInd(100.0, ema12=103.0, ema26=100.0, adx=30.0)
        f, _, _ = score_factors(ind, 0)
        self.near(f["trend"], 3.0 / (1.5 * 1.0), 1e-12,
                  "无 ATR → 尺度 = close×1% = 1，trend=3/1.5=2 → 夹到 1"
                  ) if False else None
        self.near(f["trend"], 1.0, 1e-12, "3/(1.5×1)=2 → 夹到 1")

    def test_zero_atr_falls_back(self):
        ind = StubInd(100.0, ema12=101.0, ema26=100.0, atr=0.0, adx=30.0)
        f, _, _ = score_factors(ind, 0)
        self.true("trend" in f, "ATR=0 时改用 close×1% 当尺度，因子仍然算得出来")

    def test_no_scale_skips_directional_factors(self):
        """close 与 atr 都缺失 → 趋势/动能/量能的方向基准都拿不到。"""
        ind = StubInd(0.0, ema12=110.0, ema26=100.0, adx=30.0, rsi=50.0)
        f, score, _ = score_factors(ind, 0)
        self.true("trend" not in f, "无尺度 → trend 被跳过（不是当 0 计入分母）")
        self.true("momentum" not in f, "无尺度 → momentum 也被跳过")
        self.near(f["reversion"], 0.0, 1e-12, "RSI=50 → 超买超卖中性")
        self.near(score, 0.0, 1e-12, "只剩中性因子 → 总分 0")

    def test_momentum_uses_dif_not_hist(self):
        """
        MACD 因子必须用 DIF（方向量），不能用柱（加速度）。
        在指数衰减序列里柱会转正 —— 正在崩盘的标的会显示「动能看多」。
        """
        ind = StubInd(100.0, macd_dif=-20.0, macd_hist=+5.0, atr=10.0, adx=30.0)
        f, _, _ = score_factors(ind, 0)
        self.near(f["momentum"], -1.0, 1e-12,
                  "momentum 用 DIF=-20 → -20/15=-1.333 → 夹到 -1（看空）")
        self.true("hist" not in str(f.keys()), "factor 名里没有 hist（柱不参与打分）")

    def test_strength_direction_and_magnitude(self):
        """强度 = 方向(±1) × clamp((ADX-20)/20)：ADX=20 → 0，ADX=40 → 1。"""
        base = dict(plus_di=30.0, minus_di=10.0, atr=10.0)
        f40, _, _ = score_factors(StubInd(100.0, adx=40.0, **base), 0)
        self.near(f40["strength"], 1.0, 1e-12, "ADX=40 且 +DI>-DI → +1")
        f20, _, _ = score_factors(StubInd(100.0, adx=20.0, **base), 0)
        self.near(f20["strength"], 0.0, 1e-12, "ADX=20 → 幅度 0（视为无趋势）")
        f30, _, _ = score_factors(StubInd(100.0, adx=30.0, **base), 0)
        self.near(f30["strength"], 0.5, 1e-12, "ADX=30 → 0.5")
        f_dn, _, _ = score_factors(
            StubInd(100.0, adx=40.0, plus_di=10.0, minus_di=30.0, atr=10.0), 0)
        self.near(f_dn["strength"], -1.0, 1e-12, "ADX=40 且 -DI>+DI → -1")

    def test_reversion_is_rsi_inverted(self):
        """超买超卖 = (50-RSI)/20，偏离 20 点即满分。"""
        self.near(score_factors(StubInd(100.0, rsi=50.0, atr=10.0), 0)[0]["reversion"],
                  0.0, 1e-12, "RSI=50 → 0")
        self.near(score_factors(StubInd(100.0, rsi=30.0, atr=10.0), 0)[0]["reversion"],
                  1.0, 1e-12, "RSI=30 → +1（超卖看多）")
        self.near(score_factors(StubInd(100.0, rsi=70.0, atr=10.0), 0)[0]["reversion"],
                  -1.0, 1e-12, "RSI=70 → -1（超买看空）")
        self.near(score_factors(StubInd(100.0, rsi=10.0, atr=10.0), 0)[0]["reversion"],
                  1.0, 1e-12, "RSI=10 → 仍然夹在 +1")

    def test_position_pctb_hand_computed(self):
        """%B = (close-bbl)/(bbu-bbl)，因子 = 1-2×%B。"""
        ind = StubInd(100.0, bb_upper=110.0, bb_lower=90.0, atr=10.0)
        f, _, _ = score_factors(ind, 0)
        self.near(f["position"], 0.0, 1e-12, "%B=0.5 → 因子 0")
        ind2 = StubInd(90.0, bb_upper=110.0, bb_lower=90.0, atr=10.0)
        self.near(score_factors(ind2, 0)[0]["position"], 1.0, 1e-12,
                  "贴下轨 → +1（看多）")
        ind3 = StubInd(110.0, bb_upper=110.0, bb_lower=90.0, atr=10.0)
        self.near(score_factors(ind3, 0)[0]["position"], -1.0, 1e-12,
                  "贴上轨 → -1（看空）")

    def test_position_zero_variance_band_is_skipped(self):
        """
        对抗性：常数价格 → 布林上下轨相等 → bbu > bbl 为假 → position 因子被跳过。
        这里确认它**不会**变成除零/NaN，代价只是少一个因子。
        """
        ind = StubInd(100.0, bb_upper=100.0, bb_lower=100.0, atr=10.0)
        f, _, _ = score_factors(ind, 0)
        self.true("position" not in f, "上下轨相等 → 跳过 position 因子（不除零）")

    def test_volume_factor_follows_trend_direction(self):
        """
        量能因子 = clamp((量比-1)×2) × 趋势方向符号。
        放量本身不指示方向，所以这里只是「增强当前趋势的可信度」。
        """
        up = StubInd(100.0, series_volume=2000.0, volume_ma20=1000.0,
                     ema12=110.0, ema26=100.0, atr=10.0)
        f_up, _, _ = score_factors(up, 0)
        self.true(f_up["trend"] > 0, "先确认趋势因子为正")
        self.near(f_up["volume"], 1.0, 1e-12, "趋势向上 + 量比 2 → +1")
        dn = StubInd(100.0, series_volume=2000.0, volume_ma20=1000.0,
                     ema12=90.0, ema26=100.0, atr=10.0)
        f_dn, _, _ = score_factors(dn, 0)
        self.true(f_dn["trend"] < 0, "确认趋势因子为负")
        self.near(f_dn["volume"], -1.0, 1e-12, "趋势向下 + 放量 → -1")

    def test_volume_lookup_requires_series_bars(self):
        """
        对抗性细节：_val() 对 close/open/high/low/volume 是**先去 series.bars 找**，
        找不到才不看 values 字典。所以一个「volume 只在 values 里」的对象
        拿不到量能，量能因子会静默消失。
        真实的 compute() 输出正是把 volume 放在 bars 里，所以生产路径没问题；
        但任何自造 IndicatorSet 的调用方（含测试、外部集成）必须知道这一点。
        """
        import numpy as _np
        from autotrader import _val as _v

        only_values = type("X", (), {})()
        only_values.series = type("S", (), {"bars": ()})()
        only_values.values = {"volume": _np.array([2000.0])}
        self.eq(_v(only_values, "volume", 0), None,
                "volume 只在 values 里、bars 为空 → 取不到（返回 None）")
        self.near(_v(StubInd(100.0, series_volume=2000.0), "volume", 0), 2000.0, 1e-12,
                  "volume 在 bars 里 → 取得到")

    def test_volume_factor_absent_without_trend(self):
        """
        ✅ 修正后（2026-10-16）：没有 trend 因子时，量能因子**缺席**，
        不再默认看多。

        原行为（对抗性发现）：方向取自 `base = f.get("trend", 0.0)`，
        再用 `1 if base >= 0 else -1` 决定符号。没有趋势因子时 base 默认 0.0，
        于是符号恒为 +1 —— 一个方向未知的量能信号被当成看多，
        把总分推向买入侧。

        现在：base 为 None 时直接不设置 volume 因子。方向未知就不猜方向。
        """
        ind = StubInd(0.0, series_volume=2000.0, volume_ma20=1000.0, rsi=50.0)
        f, s, _ = score_factors(ind, 0)
        self.true("trend" not in f, "没有尺度 → trend 因子缺失")
        self.true("volume" not in f,
                  f"缺 trend 时量能因子缺席（不再默认看多），实际因子={list(f)}")
        self.near(s, 0.0, 1e-12,
                  f"只有中性的 reversion → 总分为 0（{s:+.4f}），不被推向看多")

    def test_volume_factor_missing_when_ma_is_zero(self):
        ind = StubInd(100.0, volume=2000.0, volume_ma20=0.0, atr=10.0, rsi=50.0)
        f, _, _ = score_factors(ind, 0)
        self.true("volume" not in f, "量 MA=0 → 跳过量能因子（vma > 0 保护）")

    def test_all_factors_missing_returns_zero(self):
        """对抗性：所有指标都缺失时，不能崩、也不能给出非零分数。"""
        ind = StubInd(0.0)
        f, score, regime = score_factors(ind, 0)
        self.eq(f, {}, "没有任何因子")
        self.near(score, 0.0, 1e-12, "总分 0（total_w = 0 时提前返回，不做除零）")
        self.eq(regime, "未知", "状态未知")

    def test_out_of_range_index_is_safe(self):
        ind = StubInd(100.0, rsi=30.0, atr=10.0)
        f, score, _ = score_factors(ind, 99)
        self.eq(f, {}, "下标越界 → 所有取值返回 None → 无因子")
        self.near(score, 0.0, 1e-12, "总分 0（不抛 IndexError）")

    def test_score_always_in_range(self):
        """总分必须始终落在 -1~1。"""
        cases = [
            dict(rsi=0.0, atr=1.0, ema12=1000.0, ema26=1.0, adx=100.0,
                 plus_di=99.0, minus_di=1.0, bb_upper=200.0, bb_lower=-100.0),
            dict(rsi=100.0, atr=1.0, ema12=1.0, ema26=1000.0, adx=100.0,
                 plus_di=1.0, minus_di=99.0, bb_upper=200.0, bb_lower=-100.0),
        ]
        for kw in cases:
            f, score, _ = score_factors(StubInd(100.0, **kw), 0)
            self.true(-1.0 <= score <= 1.0, f"总分在区间内（{score:.4f}）")

    def test_regime_changes_the_total_score(self):
        """
        同一个「趋势 +0.667 / 超卖 +1 / 强度 0.75」组合，在两套权重下的**贡献结构**
        必须完全不同 —— 这才是分状态权重的意义：单边趋势里不能靠超卖抄底。

        ⚠️ 注意（对抗性观察）：总分本身**不一定**「趋势市更高」。
        这里实测趋势市 +0.7174 < 震荡市 +0.8205 —— 因为震荡市把 reversion 的
        权重从 0.15 提到 1.8（12 倍），一个 RSI=30 的超卖信号就足以把总分推得
        比趋势市还高。断言按「权重贡献」来写，而不是按「总分谁大」。
        """
        kw = dict(ema12=110.0, ema26=100.0, atr=10.0, rsi=30.0,
                  plus_di=30.0, minus_di=5.0)
        f_t, s_t, r_t = score_factors(StubInd(100.0, adx=35.0, **kw), 0)
        f_r, s_r, r_r = score_factors(StubInd(100.0, adx=10.0, **kw), 0)
        self.eq((r_t, r_r), ("趋势市", "震荡市"), "两次分别落到趋势市与震荡市")
        self.near(f_t["trend"], f_r["trend"], 1e-12,
                  "因子值本身相同（只是权重不同）—— 总分的差异全部来自权重")
        w_t, _ = regime_weights(35.0)
        w_r, _ = regime_weights(10.0)
        c_t = f_t["reversion"] * w_t["reversion"]
        c_r = f_r["reversion"] * w_r["reversion"]
        self.true(c_r > 8 * c_t,
                  f"震荡市里超卖因子的加权贡献是趋势市的 {c_r/c_t:.0f} 倍"
                  f"（{c_t:.2f} → {c_r:.2f}）")
        t_t = f_t["trend"] * w_t["trend"]
        t_r = f_r["trend"] * w_r["trend"]
        self.true(t_t > t_r,
                  f"反过来，趋势因子的加权贡献在趋势市更大（{t_t:.2f} > {t_r:.2f}）")
        self.true(abs(s_t - s_r) > 0.05,
                  f"总分确实随状态变化（趋势市 {s_t:+.4f} vs 震荡市 {s_r:+.4f}）")
        self.true(s_r > s_t,
                  "⚠️ 实测：这个「上升趋势 + RSI=30」组合在震荡市里分数反而更高")


class TestScoreFactorsOnRealSeries(TestCase):
    name = "autotrader.打分（真实序列）"

    def test_uptrend_scores_positive_and_decide_buys(self):
        ind = ind_of(ramp())
        f, score, regime = score_factors(ind, len(ind.series.bars) - 1)
        self.true(score > 0.25, f"单边上涨总分 {score:+.3f} > 0.25")
        self.true(len(f) >= 5, f"至少 5 个因子有值（{sorted(f)}）")
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=len(ind.series.bars) - 1))
        self.eq(dec.action, "buy", f"决定买入（目标 {dec.target:.0%}）")

    def test_downtrend_scores_negative(self):
        ind = ind_of(decay())
        f, score, _ = score_factors(ind, len(ind.series.bars) - 1)
        self.true(score < -0.25, f"单边下跌总分 {score:+.3f} < -0.25")
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=len(ind.series.bars) - 1))
        self.ne(dec.action, "buy", f"不会买入（{dec.action}）")

    def test_downtrend_is_read_as_bearish_not_bullish(self):
        """
        回归护栏（这是项目修过的真 bug）：最初的固定权重下，单边下跌会得到
        「trend -1、strength -1、reversion +1、position +0.79、momentum +0.83」
        → 总分 +0.03 → 完全不动。要求现在的分数明确看空。
        """
        ind = ind_of(decay())
        i = len(ind.series.bars) - 1
        f, score, _ = score_factors(ind, i)
        self.true(f["trend"] < 0, f"趋势因子看空（{f['trend']:+.2f}）")
        self.true(score < -0.3, f"总分明确看空（{score:+.2f}），不会被超卖因子抵消")
        self.true(f["reversion"] > 0,
                  f"超卖因子确实还在看多（{f['reversion']:+.2f}）—— "
                  f"但它在趋势市里被压到 0.15 权重")

    def test_multifactor_weights_sum_matches_manual_computation(self):
        """
        用真实指标值，按权重定义手工重算总分，与 score_factors 的输出比对。
        这验证的是「加权平均」这一步本身没算错。
        """
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        f, score, regime = score_factors(ind, i)
        w, _ = regime_weights(ind.at("adx", i))
        total_w = sum(w[k] for k in f if k in w)
        manual = sum(f[k] * w[k] for k in f if k in w) / total_w
        self.near(score, max(-1.0, min(1.0, manual)), 1e-12,
                  "总分 == Σ(factor×weight)/Σweight（与手工重算一致）")

    def test_missing_factors_not_counted_in_denominator(self):
        """
        短序列：只有少数因子有值。分母必须只算**实际算出来的**因子，
        否则缺失因子会按 0 摊薄总分（项目注释专门写了这一点）。
        """
        ind = ind_of(ramp(25))
        i = 24
        f, score, _ = score_factors(ind, i)
        self.true(len(f) < 6, f"短序列因子不全（只有 {sorted(f)}）")
        if f:
            w, _ = regime_weights(ind.at("adx", i))
            total_w = sum(w[k] for k in f if k in w)
            manual = sum(f[k] * w[k] for k in f if k in w) / total_w
            self.near(score, max(-1.0, min(1.0, manual)), 1e-12,
                      "总分仍按「有效因子的权重和」归一化")
            all_w = sum(w.values())
            self.true(total_w < all_w + 1e-12,
                      "分母小于全部权重之和（确认没把缺失因子算进去）")


class TestExplainFactors(TestCase):
    name = "autotrader.explain_factors"

    def test_translates_and_verdicts(self):
        out = explain_factors({"trend": 0.9, "reversion": -0.5}, 0.4, 0.25, "趋势市")
        self.true(any("趋势市" in s for s in out), "包含市场状态")
        self.true(any("趋势看多" in s for s in out), "趋势因子翻译成看多")
        self.true(any("超买超卖看空" in s for s in out), "负因子翻译成看空")
        self.true(any("倾向做多" in s for s in out), "分数过阈值 → 倾向做多")
        self.true(any("0.40" in s or "+0.40" in s for s in out), "总分写进说明")

    def test_neutral_factors_are_filtered(self):
        out = explain_factors({"trend": 0.01, "momentum": -0.02}, 0.0, 0.25, "")
        self.true(any("接近中性" in s for s in out), "全部接近中性时给一句话说明")
        self.true(any("观望" in s for s in out), "未达阈值 → 观望")

    def test_empty_factors(self):
        out = explain_factors({}, 0.0, 0.25, "")
        self.true(len(out) >= 1, f"空因子列表也不返回空说明（{out}）")


class TestRiskLayer(TestCase):
    name = "autotrader.风控层（优先级最高）"

    def setUp_uptrend(self):
        ind = ind_of(ramp())
        return ind, len(ind.series.bars) - 1

    def test_stop_loss_beats_bullish_signal(self):
        ind, i = self.setUp_uptrend()
        close = last_close(ind)
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=close / 0.90,   # 亏 10%
                                     peak_price=close, bars_held=5))
        self.true(dec.risk_hit is not None and "止损" in dec.risk_hit, dec.risk_hit or "未触发")
        self.eq(dec.action, "sell", "动作是卖出")
        self.near(dec.target, 0.0, 1e-12, "目标仓位归零")
        self.near(dec.score, 0.0, 1e-12, "风控触发时不计算分数（直接返回）")

    def test_stop_loss_boundary_at_exactly_six_percent(self):
        ind, i = self.setUp_uptrend()
        close = last_close(ind)
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=close / 0.94,   # 恰好 -6%
                                     peak_price=close, bars_held=5))
        self.true(dec.risk_hit is not None, "亏 6.000% 恰好触及 6% 止损线（<= 判定）")

    def test_take_profit(self):
        ind, i = self.setUp_uptrend()
        close = last_close(ind)
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=close / 1.25,   # 赚 25%
                                     peak_price=close, bars_held=5))
        self.true(dec.risk_hit is not None and "止盈" in dec.risk_hit, dec.risk_hit or "未触发")
        self.eq(dec.action, "sell", "止盈也是清仓")

    def test_trailing_stop_requires_profit(self):
        """
        移动止损只在**已经浮盈**时生效（否则和固定止损重复）。
        构造：成本 92、现价 100（浮盈 +8.7%）、峰值 110（回撤 -9.1% > 8%）→ 触发。
        """
        px = [92.0] * 60 + [100.0]
        ind = ind_of(px)
        i = len(px) - 1
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=92.0, peak_price=110.0, bars_held=10))
        self.true(dec.risk_hit is not None and "移动止损" in dec.risk_hit,
                  dec.risk_hit or "未触发")
        # 对照：回撤没超线
        dec2 = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=92.0, peak_price=105.0, bars_held=10))
        self.true(dec2.risk_hit is None or "移动止损" not in dec2.risk_hit,
                  f"回撤 -4.8% 未超线 → 不触发（{dec2.risk_hit}）")

    def test_trailing_stop_ignored_when_losing(self):
        """
        亏损中（pnl <= 0）不判移动止损 —— 这条规则把「亏损」交给固定止损。
        构造：成本 100、现价 95（-5%，不到 6% 止损）、峰值 120（回撤 -20.8%）。
        期望：移动止损**不**触发（因为 pnl <= 0）。
        """
        px = [100.0] * 60 + [95.0]
        ind = ind_of(px)
        i = len(px) - 1
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=100.0, peak_price=120.0, bars_held=10))
        self.true(dec.risk_hit is None or "移动止损" not in dec.risk_hit,
                  f"浮亏时移动止损不生效（{dec.risk_hit}）")

    def test_max_drawdown_circuit_breaker(self):
        ind, i = self.setUp_uptrend()
        close = last_close(ind)
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=close, peak_price=close,
                                     drawdown=0.30, bars_held=5))
        self.true(dec.risk_hit is not None and "熔断" in dec.risk_hit, dec.risk_hit or "未触发")
        self.eq(dec.action, "sell", "持仓时熔断 → 清仓")

    def test_max_drawdown_blocks_entry_when_flat(self):
        ind, i = self.setUp_uptrend()
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, drawdown=0.30))
        self.eq(dec.action, "hold", "空仓时熔断 → 禁止开仓")
        self.near(dec.target, 0.0, 1e-12, "目标仓位 0")

    def test_halted_flag_blocks_entry(self):
        ind, i = self.setUp_uptrend()
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, halted=True))
        self.eq(dec.action, "hold", "halted=True → 只允许离场")
        self.eq(dec.risk_hit, "熔断中", "标注熔断状态")

    def test_risk_check_needs_valid_close(self):
        """
        对抗性：close 缺失/为 0 时 **跳过全部价格类风控**（stop/tp/trailing），
        只保留账户回撤熔断。这意味着「行情坏掉」时止损是不会响的。
        """
        ind = StubInd(0.0, atr=1.0)          # close = 0
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=0, position=1, fraction=0.9,
                                     entry_price=100.0, peak_price=100.0))
        self.true(dec.risk_hit is None,
                  "close=0 → 三个价格类风控全部跳过（不会误报止损）")
        # 但账户级熔断仍然生效（它不依赖 close）
        dec2 = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=0, position=1, fraction=0.9,
                                     entry_price=100.0, peak_price=100.0,
                                     drawdown=0.30))
        self.true(dec2.risk_hit is not None and "熔断" in dec2.risk_hit,
                  "回撤熔断不依赖价格，仍然生效")

    def test_risk_rules_can_be_disabled(self):
        ind, i = self.setUp_uptrend()
        close = last_close(ind)
        cfg = AutoConfig(mode="score")
        cfg.risk = RiskConfig(stop_loss=None, take_profit=None, trailing_stop=None,
                              max_drawdown=None)
        # 用「成本远低于现价」制造浮盈，而不是浮亏 —— 否则会被别的东西干扰
        dec = AutoDecider(cfg).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9,
                                     entry_price=close * 0.5, peak_price=close))
        self.true(dec.risk_hit is None, "全部风控置 None → 不触发")
        self.ne(dec.action, "sell", "赚 100% 也不会被动平仓（因为风控被显式关闭）")

    def test_ind_none_is_safe(self):
        dec = AutoDecider(AutoConfig(mode="score")).decide(ind=None, **decide_kwargs())
        self.eq(dec.action, "hold", "ind=None → hold")
        self.eq(dec.note, "没有行情数据", "给出可读说明")


class TestSizingLayer(TestCase):
    name = "autotrader.仓位层"

    def test_fixed_sizing(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        dec = AutoDecider(AutoConfig(mode="score", sizing="fixed",
                                     fixed_fraction=0.5)).decide(
            ind=ind, **decide_kwargs(i=i))
        self.near(dec.target, 0.5, 1e-12, "fixed 模式给固定比例 0.5")

    def test_max_fraction_caps_target(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        dec = AutoDecider(AutoConfig(mode="score", sizing="fixed",
                                     fixed_fraction=0.95,
                                     max_fraction=0.6)).decide(
            ind=ind, **decide_kwargs(i=i))
        self.near(dec.target, 0.6, 1e-12, "目标被 max_fraction=0.6 夹住")

    def test_score_sizing_scales_with_confidence(self):
        """
        score 模式：|score| 从阈值到 1 线性映射到 0.30~1.00。
        这里用同一个系列、同一根 K 线，只改 entry_threshold 来改变 (|s|-t)/(1-t)。
        """
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        _, s, _ = score_factors(ind, i)
        self.true(s > 0.25, f"分数足够高（{s:.3f}）以便测试")
        base = AutoConfig(mode="score", sizing="score", entry_threshold=0.25)
        dec = AutoDecider(base).decide(ind=ind, **decide_kwargs(i=i))
        k = max(0.0, min(1.0, (abs(s) - 0.25) / (1.0 - 0.25)))
        self.near(dec.target, 0.30 + 0.70 * k, 1e-9,
                  "目标 = 0.30 + 0.70 × (|score|-t)/(1-t)")
        self.true(dec.target > 0.30, f"信心越足仓位越重（{dec.target:.3f} > 0.30）")

    def test_volatility_sizing_is_inverse_to_volatility(self):
        """波动率目标：ATR/close 越大 → 仓位越轻；被夹在 0~1。"""
        calm = StubInd(100.0, atr=1.0, rsi=50.0)
        wild = StubInd(100.0, atr=10.0, rsi=50.0)
        cfg = AutoConfig(mode="score", sizing="volatility", target_vol=0.02,
                         fixed_fraction=0.95)
        # 直接调私有方法：decide() 里未达阈值时不会走 sizing 分支
        d = AutoDecider(cfg)
        frac_calm = d._target_fraction(0.5, 0.0, calm, 0)
        frac_wild = d._target_fraction(0.5, 0.0, wild, 0)
        self.near(frac_calm, 1.0, 1e-12, "ATR/close=1% → 0.02/0.01=2 → 夹到 1.0")
        self.near(frac_wild, 0.2, 1e-12, "ATR/close=10% → 0.02/0.10=0.2")
        self.true(frac_wild < frac_calm, "波动大的标的仓位更轻")

    def test_volatility_sizing_falls_back_without_atr(self):
        cfg = AutoConfig(mode="score", sizing="volatility", fixed_fraction=0.7)
        d = AutoDecider(cfg)
        ind = StubInd(100.0, rsi=50.0)          # 没有 atr
        self.near(d._target_fraction(0.5, 0.0, ind, 0), 0.7, 1e-12,
                  "无 ATR → 回退到 fixed_fraction")

    def test_unknown_sizing_mode_falls_back_to_fixed(self):
        cfg = AutoConfig(mode="score", sizing="nonsense", fixed_fraction=0.42)
        ind = StubInd(100.0, atr=1.0, rsi=50.0)
        self.near(AutoDecider(cfg)._target_fraction(0.5, 0.0, ind, 0), 0.42, 1e-12,
                  "未知 sizing → 回退 fixed_fraction")


class TestCooldownAndBand(TestCase):
    name = "autotrader.冷却期与分档"

    def _ind(self):
        ind = ind_of(ramp())
        return ind, len(ind.series.bars) - 1

    def test_cooldown_blocks_entry(self):
        ind, i = self._ind()
        cfg = AutoConfig(mode="score")            # cooldown_bars=3
        seen_cooling = False
        for bars_since, expect_buy in ((0, False), (1, False), (2, False), (3, True)):
            dec = AutoDecider(cfg).decide(ind=ind, **decide_kwargs(
                i=i, bars_since_exit=bars_since))
            if expect_buy:
                self.eq(dec.action, "buy",
                        f"距上次卖出 {bars_since} 根 → 冷却结束，可开仓")
            else:
                self.ne(dec.action, "buy",
                        f"距上次卖出 {bars_since} 根 → 冷却期内不开仓（{dec.action}）")
                seen_cooling = seen_cooling or any("冷却" in r for r in dec.reasons)
        self.true(seen_cooling, "冷却期的理由里明确写了「冷却」而不是静默不动")

    def test_cooldown_reason_is_recorded(self):
        ind, i = self._ind()
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, **decide_kwargs(i=i, bars_since_exit=0))
        self.true(any("冷却" in r for r in dec.reasons),
                  f"理由里写明冷却：{dec.reasons[:2]}")

    def test_min_hold_blocks_sell(self):
        ind, i = self._ind()
        cfg = AutoConfig(mode="score", sizing="fixed", fixed_fraction=0.95)
        cfg.risk = RiskConfig(stop_loss=None, take_profit=None, trailing_stop=None,
                              max_drawdown=None, min_hold_bars=5)
        # 打分看多但当前仓位远高于目标 → 会想卖；但 bars_held < 5 应被拦住
        dec = AutoDecider(cfg).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.10, bars_held=2))
        self.true(any("最短持有" in r for r in dec.reasons) or dec.action != "sell",
                  f"最短持有未到 → 不卖（{dec.action}）")

    def test_rebalance_band_prevents_small_trades(self):
        ind, i = self._ind()
        cfg = AutoConfig(mode="score", sizing="fixed", fixed_fraction=0.95,
                         rebalance_band=0.15)
        dec = AutoDecider(cfg).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.94))
        self.eq(dec.action, "hold",
                "差值 0.12 < 0.15 → 不动（这就是分档的意义：省手续费）")
        self.true(any("分档" in r for r in dec.reasons),
                  f"理由里写明分档：{dec.reasons[-1]}")

    def test_rebalance_band_boundary(self):
        """
        分档判定是 `abs(target-fraction) < band`（严格小于），而且浮点减法会让
        「看起来正好等于阈值」的情形落到阈值**下方**：
            0.95 - 0.80 = 0.1499999999999999  （不是 0.15）
        所以 0.95/0.80/0.15 这组参数**不会**交易；理由里的 "%.0f%%" 把它显示成
        15%，和 15% 的阈值看起来一样，但比较结果是 `0.1499… < 0.15` → 不动。
        边界行为因此是「差一点就到阈值时反而不动」—— 这是安全方向，记录清楚即可。
        """
        ind, i = self._ind()
        cfg = AutoConfig(mode="score", sizing="fixed", fixed_fraction=0.95,
                         rebalance_band=0.15)
        dec = AutoDecider(cfg).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.80, bars_held=10))
        self.near(dec.current, 0.80, 1e-12, "当前仓位回传正确")
        self.true(abs(0.95 - 0.80) < 0.15,
                  "浮点：0.95-0.80 = 0.1499999999999999 < 0.15")
        self.eq(dec.action, "hold",
                "差值略小于阈值 → 不动（显示成 15% 但实际小于 15%）")
        # 对照：差值明确大于阈值时必须动作
        dec2 = AutoDecider(cfg).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.70, bars_held=10))
        self.eq(dec2.action, "buy", "差值 0.25 > 0.15 → 执行 buy")

    def test_target_below_current_is_sell(self):
        ind, i = self._ind()
        dec = AutoDecider(AutoConfig(mode="score", sizing="fixed",
                                     fixed_fraction=0.0)).decide(
            ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9, bars_held=10))
        self.eq(dec.action, "sell", "目标 0 < 当前 90% → 卖出")


class TestSignalAndConfirmModes(TestCase):
    name = "autotrader.signal/confirm 模式"

    def test_signal_mode_matches_strategy(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        cfg = AutoConfig(mode="signal", strategy="ema_cross", fixed_fraction=0.8)
        dec = AutoDecider(cfg).decide(ind=ind, **decide_kwargs(i=i))
        self.true(dec.action in ("buy", "hold"), f"动作合法（{dec.action}）")
        self.true(any("双均线" in r for r in dec.reasons),
                  f"理由里带策略名：{dec.reasons[:1]}")
        self.true("signal" in dec.factors, "因子表里带 signal 键")

    def test_signal_mode_unknown_strategy_holds(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        dec = AutoDecider(AutoConfig(mode="signal", strategy="nope")).decide(
            ind=ind, **decide_kwargs(i=i))
        self.eq(dec.action, "hold", "未知策略 → hold（不抛异常）")
        self.true(any("未知策略" in r for r in dec.reasons), "说明原因")

    def test_confirm_mode_requires_agreement(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        dec = AutoDecider(AutoConfig(mode="confirm", confirm_min=2,
                                     fixed_fraction=0.8)).decide(
            ind=ind, **decide_kwargs(i=i))
        self.true(any("看多" in r for r in dec.reasons), f"给出共识信息：{dec.reasons[0]}")
        self.true(len(dec.factors) >= 1, "因子表里列出各策略的方向")

    def test_confirm_min_five_is_more_conservative(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        d2 = AutoDecider(AutoConfig(mode="confirm", confirm_min=2,
                                    fixed_fraction=0.8)).decide(
            ind=ind, **decide_kwargs(i=i))
        d5 = AutoDecider(AutoConfig(mode="confirm", confirm_min=5,
                                    fixed_fraction=0.8)).decide(
            ind=ind, **decide_kwargs(i=i))
        self.true(d5.target <= d2.target,
                  f"要求 5 个共识时目标仓位不高于 2 个（{d5.target} <= {d2.target}）")

    def test_ai_mode_without_endpoint_holds(self):
        ind = ind_of(ramp())
        i = len(ind.series.bars) - 1
        dec = AutoDecider(AutoConfig(mode="ai")).decide(ind=ind, **decide_kwargs(i=i))
        self.eq(dec.action, "hold", "AutoDecider 不处理 ai 模式 → hold")
        self.eq(dec.note, "等待 AI 决策", "给出说明")


class TestAIDecider(TestCase):
    name = "autotrader.AIDecider"

    def _ind(self):
        ind = ind_of(ramp())
        return ind, len(ind.series.bars) - 1

    def test_context_has_no_nan(self):
        """NaN 必须转成 null，否则 JSON 序列化会产出非法 JSON。"""
        ind, i = self._ind()
        ctx = AIDecider.build_context(
            ind, i, symbol="BTCUSDT", name="比特币", interval="1h",
            position=1, fraction=0.5, entry_price=100.0, equity=100_000.0,
            cash=50_000.0, peak_price=120.0, drawdown=0.03)
        import json as _json
        self.true(ctx["标的"].startswith("比特币"), "标的带名字")
        self.true(len(ctx["指标"]) > 12, f"指标项数 {len(ctx['指标'])} > 12")
        self.true(all(v is None or not (isinstance(v, float) and v != v)
                      for v in ctx["指标"].values()),
                  "指标里没有 NaN（要么数值要么 null）")
        ok, _ = self.no_raise(lambda: _json.dumps(ctx, ensure_ascii=False),
                              "上下文可 JSON 序列化（strict 模式）")
        self.true(ok, "JSON 序列化通过")

    def test_context_on_short_series_has_nulls_not_fabricated_values(self):
        """
        极短序列：算不出来的指标必须是 null，**不能编造数值**。
        逐项验证：ctx 里为 None 的项，在原始指标数组里确实是 NaN
        （也就是「缺失」而不是「恰好等于某个数」）。
        """
        ind = ind_of([100.0])
        ctx = AIDecider.build_context(
            ind, 0, symbol="T", name="t", interval="1d", position=0, fraction=0.0,
            entry_price=0.0, equity=1.0, cash=1.0, peak_price=0.0, drawdown=0.0)
        vals = ctx["指标"]
        bad = []
        for key, arr in (("EMA12", "ema12"), ("RSI14", "rsi"), ("ATR", "atr"),
                         ("SMA20", "sma20"), ("KDJ_K", "kdj_k"), ("量比", "volumeRatio")):
            is_null = vals[key] is None
            raw_is_nan = bool(np.isnan(ind.values[arr][0]))
            if is_null != raw_is_nan:
                bad.append(f"{key}: ctx={vals[key]!r} raw_nan={raw_is_nan}")
        self.eq(bad, [], "ctx 为 null 的项与原始数组的 NaN 一一对应（没有编造数值）")
        self.eq(ctx["账户"]["持仓成本"], None, "空仓时成本为 null 而不是 0")
        self.true(ctx["账户"]["持仓浮动盈亏"] is None, "空仓时浮盈也是 null")

    def test_context_keeps_ema_on_three_bars(self):
        """3 根：EMA 类指标算得出来（无预热期），其余为 null。"""
        ind = ind_of([100.0, 101.0, 102.0])
        ctx = AIDecider.build_context(
            ind, 2, symbol="T", name="t", interval="1d", position=0, fraction=0.0,
            entry_price=0.0, equity=1.0, cash=1.0, peak_price=0.0, drawdown=0.0)
        vals = ctx["指标"]
        self.true(vals["EMA12"] is not None, "EMA12 有值（EMA 无预热期）")
        self.true(vals["SMA20"] is None, "SMA20 为 null（数据不足）")
        self.true(vals["RSI14"] is None, "RSI14 为 null")
        self.true(vals["ATR"] is None, "ATR 为 null")

    def test_prompt_mentions_constraints(self):
        ind, i = self._ind()
        ctx = AIDecider.build_context(
            ind, i, symbol="T", name="t", interval="1d", position=0, fraction=0.0,
            entry_price=0.0, equity=1.0, cash=1.0, peak_price=0.0, drawdown=0.0)
        p = AIDecider.build_prompt(ctx)
        self.true("JSON" in p and "action" in p, "要求 JSON 输出格式")
        self.true("手续费" in p, "提示交易成本")
        self.true("T+1" in p, "提示 A 股 T+1")
        self.true("target" in p, "说明 target 的含义")

    def test_output_clamped_to_max_fraction(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", max_fraction=0.6, rebalance_band=0.01))
        ai.ask = lambda ctx: {"action": "buy", "target": 5.0, "reason": "梭哈"}
        dec = ai.decide(ind=ind, **decide_kwargs(i=i))
        self.near(dec.target, 0.6, 1e-12, "AI 要 500% 仓位 → 夹到 max_fraction=0.6")
        self.true(dec.target <= 0.6, "绝不突破上限")

    def test_negative_target_clamped_to_zero(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", max_fraction=0.6, rebalance_band=0.01))
        ai.ask = lambda ctx: {"action": "sell", "target": -3.0, "reason": "做空"}
        dec = ai.decide(ind=ind, **decide_kwargs(i=i, position=1, fraction=0.5))
        self.near(dec.target, 0.0, 1e-12, "负目标夹到 0（本项目不支持做空）")

    def test_garbage_output_is_degraded(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", rebalance_band=0.01))
        ai.ask = lambda ctx: {"action": "GARBAGE", "target": "abc", "reason": 123}
        ok, dec = self.no_raise(lambda: ai.decide(ind=ind, **decide_kwargs(i=i)),
                                "非法 AI 输出（动作非法 + target 非数值）不抛异常")
        if ok:
            self.true(dec.action in ("buy", "sell", "hold"), f"动作降级（{dec.action}）")
            self.true(0.0 <= dec.target <= 1.0, f"目标仍在 0~1（{dec.target}）")

    def test_missing_keys_use_defaults(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", rebalance_band=0.01))
        ai.ask = lambda ctx: {}
        ok, dec = self.no_raise(lambda: ai.decide(ind=ind, **decide_kwargs(i=i)),
                                "AI 返回空对象 → 不抛异常")
        if ok:
            self.eq(dec.action, "hold", "缺 action → hold")

    def test_ai_failure_falls_back_to_score(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai"))        # 没配 URL/CMD
        dec = ai.decide(ind=ind, **decide_kwargs(i=i), meta={})
        self.true("fallback" in dec.mode, f"标注用了兜底（mode={dec.mode}）")
        self.true(any("失败" in r for r in dec.reasons), f"说明失败原因：{dec.reasons[0][:60]}")

    def test_risk_still_local_in_ai_mode(self):
        """风控必须在本地判定 —— AI 说「不卖」也不能阻止止损。"""
        ind, i = self._ind()
        close = last_close(ind)
        ai = AIDecider(AutoConfig(mode="ai"))
        asked = {"n": 0}

        def spy(ctx):
            asked["n"] += 1
            return {"action": "hold", "target": 1.0, "reason": "不卖"}

        ai.ask = spy
        dec = ai.decide(ind=ind, **decide_kwargs(
            i=i, position=1, fraction=0.9, entry_price=close / 0.5,
            peak_price=close, bars_held=5), meta={})
        self.true(dec.risk_hit is not None, f"止损仍然生效（{dec.risk_hit}）")
        self.eq(asked["n"], 0, "风控触发时**根本不问 AI**（省一次调用，也避免被误导）")

    def test_ai_suggestion_below_band_is_hold(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", rebalance_band=0.15))
        ai.ask = lambda ctx: {"action": "buy", "target": 0.05, "reason": "轻仓试试"}
        dec = ai.decide(ind=ind, **decide_kwargs(i=i))
        self.eq(dec.action, "hold", "AI 建议与当前仓位差 5% < 15% → 不动")
        self.true(any("AI 理由" in r for r in dec.reasons), "把 AI 的理由也记下来")

    def test_ai_buy_and_sell_actions(self):
        ind, i = self._ind()
        ai = AIDecider(AutoConfig(mode="ai", rebalance_band=0.01))
        ai.ask = lambda ctx: {"action": "buy", "target": 0.8, "reason": "看多"}
        dec = ai.decide(ind=ind, **decide_kwargs(i=i))
        self.eq(dec.action, "buy", "target 0.8 > 当前 0 → buy")
        ai.ask = lambda ctx: {"action": "sell", "target": 0.1, "reason": "减仓"}
        dec2 = ai.decide(ind=ind, **decide_kwargs(i=i, position=1, fraction=0.9))
        self.eq(dec2.action, "sell", "target 0.1 < 当前 0.9 → sell")

    def test_make_decider_dispatch(self):
        self.true(isinstance(make_decider(AutoConfig(mode="ai")), AIDecider),
                  "mode=ai → AIDecider")
        self.true(isinstance(make_decider(AutoConfig(mode="score")), AutoDecider),
                  "mode=score → AutoDecider")


class TestDecisionObject(TestCase):
    name = "autotrader.Decision"

    def test_serializable(self):
        import json as _json
        d = Decision("buy", 0.5, 0.0, 0.3, {"trend": 0.3}, ["理由"],
                     mode="score", note="")
        js = _json.dumps(d.to_dict(), ensure_ascii=False)
        self.true(isinstance(js, str) and len(js) > 10, "to_dict 可序列化")
        self.true(len(d.summary()) > 5, f"有摘要（{d.summary()}）")
        self.near(d.to_dict()["target"], 0.5, 1e-12, "target 进字典")

    def test_summary_prioritizes_risk(self):
        d = Decision("sell", 0.0, 0.9, 0.0, {}, ["触发器"], risk_hit="止损（-8%）")
        self.true("风控触发" in d.summary(), f"风控摘要优先（{d.summary()}）")

    def test_slots_prevent_typos(self):
        d = Decision("hold", 0.0, 0.0, 0.0, {}, [])
        self.raises(AttributeError, lambda: setattr(d, "nonexistent", 1),
                    "__slots__ 生效：不能随便加属性")


REGISTRY.extend([TestRegimeWeights, TestScoreFactors, TestScoreFactorsOnRealSeries,
                 TestExplainFactors, TestRiskLayer, TestSizingLayer,
                 TestCooldownAndBand, TestSignalAndConfirmModes, TestAIDecider,
                 TestDecisionObject])

declare_counts({
    "TestRegimeWeights": {"test_trending_and_ranging_are_different": (7, 7),
                          "test_transition_is_linear": (2, 2),
                          "test_boundary_values": (4, 4),
                          "test_weights_are_copies": (1, 1)},
    "TestScoreFactors": {"test_trend_factor_normalized_by_atr": (3, 3),
                         "test_trend_factor_clamped": (1, 1),
                         "test_scale_falls_back_to_one_percent_of_price": (1, 1),
                         "test_zero_atr_falls_back": (1, 1),
                         "test_no_scale_skips_directional_factors": (4, 4),
                         "test_momentum_uses_dif_not_hist": (2, 2),
                         "test_strength_direction_and_magnitude": (4, 4),
                         "test_reversion_is_rsi_inverted": (4, 4),
                         "test_position_pctb_hand_computed": (3, 3),
                         "test_position_zero_variance_band_is_skipped": (1, 1),
                         "test_volume_factor_follows_trend_direction": (4, 4),
                         # 2026-10-16：用例改名 —— 修正后缺 trend 时量能因子缺席，
                        # 不再默认看多
                        "test_volume_factor_absent_without_trend": (3, 3),
                         "test_volume_lookup_requires_series_bars": (2, 2),
                         "test_volume_factor_missing_when_ma_is_zero": (1, 1),
                         "test_all_factors_missing_returns_zero": (3, 3),
                         "test_out_of_range_index_is_safe": (2, 2),
                         "test_score_always_in_range": (2, 2),
                         "test_regime_changes_the_total_score": (6, 6)},
    "TestScoreFactorsOnRealSeries": {
        "test_uptrend_scores_positive_and_decide_buys": (3, 3),
        "test_downtrend_scores_negative": (2, 2),
        "test_downtrend_is_read_as_bearish_not_bullish": (3, 3),
        "test_multifactor_weights_sum_matches_manual_computation": (1, 1),
        "test_missing_factors_not_counted_in_denominator": (3, 3)},
    "TestExplainFactors": {"test_translates_and_verdicts": (5, 5),
                           "test_neutral_factors_are_filtered": (2, 2),
                           "test_empty_factors": (1, 1)},
    "TestRiskLayer": {"test_stop_loss_beats_bullish_signal": (4, 4),
                      "test_stop_loss_boundary_at_exactly_six_percent": (1, 1),
                      "test_take_profit": (2, 2),
                      "test_trailing_stop_requires_profit": (2, 2),
                      "test_trailing_stop_ignored_when_losing": (1, 1),
                      "test_max_drawdown_circuit_breaker": (2, 2),
                      "test_max_drawdown_blocks_entry_when_flat": (2, 2),
                      "test_halted_flag_blocks_entry": (2, 2),
                      "test_risk_check_needs_valid_close": (2, 2),
                      "test_risk_rules_can_be_disabled": (2, 2),                      "test_ind_none_is_safe": (2, 2)},
    "TestSizingLayer": {"test_fixed_sizing": (1, 1),
                        "test_max_fraction_caps_target": (1, 1),
                        "test_score_sizing_scales_with_confidence": (3, 3),
                        "test_volatility_sizing_is_inverse_to_volatility": (3, 3),
                        "test_volatility_sizing_falls_back_without_atr": (1, 1),
                        "test_unknown_sizing_mode_falls_back_to_fixed": (1, 1)},
    "TestCooldownAndBand": {"test_cooldown_blocks_entry": (5, 5),
                            "test_cooldown_reason_is_recorded": (1, 1),
                            "test_min_hold_blocks_sell": (1, 1),
                            "test_rebalance_band_prevents_small_trades": (2, 2),
                            "test_rebalance_band_boundary": (4, 4),
                            "test_target_below_current_is_sell": (1, 1)},
    "TestSignalAndConfirmModes": {"test_signal_mode_matches_strategy": (3, 3),
                                  "test_signal_mode_unknown_strategy_holds": (2, 2),
                                  "test_confirm_mode_requires_agreement": (2, 2),
                                  "test_confirm_min_five_is_more_conservative": (1, 1),
                                  "test_ai_mode_without_endpoint_holds": (2, 2)},
    "TestAIDecider": {"test_context_has_no_nan": (5, 5),
                      "test_context_on_short_series_has_nulls_not_fabricated_values": (3, 3),
                      "test_context_keeps_ema_on_three_bars": (4, 4),                      "test_prompt_mentions_constraints": (4, 4),
                      "test_output_clamped_to_max_fraction": (2, 2),
                      "test_negative_target_clamped_to_zero": (1, 1),
                      "test_garbage_output_is_degraded": (3, 4),
                      "test_missing_keys_use_defaults": (2, 3),
                      "test_ai_failure_falls_back_to_score": (2, 2),
                      "test_risk_still_local_in_ai_mode": (2, 2),
                      "test_ai_suggestion_below_band_is_hold": (2, 2),
                      "test_ai_buy_and_sell_actions": (2, 2),
                      "test_make_decider_dispatch": (2, 2)},
    "TestDecisionObject": {"test_serializable": (3, 3),
                           "test_summary_prioritizes_risk": (1, 1),
                           "test_slots_prevent_typos": (1, 1)},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_autotrader.py"))
