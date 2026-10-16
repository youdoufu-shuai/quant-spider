#!/usr/bin/env python3
"""
边界与对抗性验证 —— 专门去找「不报错但结果错」的地方。

这一组用例的写法是：**先观察、再断言观察到的行为**，并在注释里写清楚
这是「缺陷」还是「可接受的契约」。凡是判定为缺陷的，都在报告里单独列出。

覆盖：
  · 空序列 / 单根 / 两根 / 极短序列的指标预热
  · 全 NaN 输入（价格、成交量）
  · 常数价格（零方差 → 布林上下轨相等）
  · 价格为 0 或负数
  · 单次巨幅跳空
  · 所有指标都缺失时的打分模型
  · 回测/模拟盘在退化数据下不崩

直接运行：python quant/tests/test_edge_cases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from datasource import Bar, Series                       # noqa: E402
from indicators import (                                 # noqa: E402
    IndicatorSet, adx, atr, bollinger, compute, donchian, ema, macd, obv,
    rolling_max, rolling_min, rolling_std, rsi, sma, stochastic, true_range, wilder,
)
from strategy import SignalSet, run_all as run_all_strategies   # noqa: E402
from backtest import Costs, run_backtest                        # noqa: E402
from paper import PaperTrader                                   # noqa: E402
from autotrader import AIDecider, AutoConfig, AutoDecider, score_factors  # noqa: E402

ZERO = Costs(commission=0.0, slippage=0.0, min_commission=0.0)


def mk_series(closes, *, highs=None, lows=None, opens=None, vols=None,
              market="crypto", interval="1d") -> Series:
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


def allnan_series(n=50, market="crypto") -> Series:
    bars = tuple(Bar(1_700_000_000_000 + i * 86_400_000,
                     float("nan"), float("nan"), float("nan"),
                     float("nan"), float("nan")) for i in range(n))
    return Series("test", market, "TEST", "测试", "1d", bars, 0.0)


def sig_pos(positions) -> SignalSet:
    pos = np.asarray(positions, dtype=np.int8)
    n = pos.size
    ent = np.zeros(n, bool)
    ext = np.zeros(n, bool)
    for i in range(1, n):
        if pos[i] == 1 and pos[i - 1] == 0:
            ent[i] = True
        if pos[i] == 0 and pos[i - 1] == 1:
            ext[i] = True
    return SignalSet("t", "测试", "", pos, ent, ext, ["信号"] * n, {})


INDICATOR_FUNCS = [
    ("sma", lambda a: sma(a, 5)),
    ("ema", lambda a: ema(a, 5)),
    ("wilder", lambda a: wilder(a, 5)),
    ("rolling_std", lambda a: rolling_std(a, 5)),
    ("rolling_max", lambda a: rolling_max(a, 5)),
    ("rolling_min", lambda a: rolling_min(a, 5)),
    ("rsi", lambda a: rsi(a, 14)),
    ("bollinger_mid", lambda a: bollinger(a, 20)[0]),
    ("bollinger_upper", lambda a: bollinger(a, 20)[1]),
    ("bollinger_width", lambda a: bollinger(a, 20)[3]),
    ("macd_dif", lambda a: macd(a, 12, 26)[0]),
    ("macd_hist", lambda a: macd(a, 12, 26)[2]),
    ("stochastic_k", lambda a: stochastic(a, a, a, 14)[0]),
    ("atr", lambda a: atr(a, a, a, 14)),
    ("adx", lambda a: adx(a, a, a, 14)[2]),
    ("obv", lambda a: obv(a, a)),
    ("donchian_up", lambda a: donchian(a, a, 20)[0]),
    ("true_range", lambda a: true_range(a, a, a)),
]


class TestDegenerateLengths(TestCase):
    name = "edge.序列长度退化"

    def test_empty_series_compute(self):
        empty = Series("test", "crypto", "T", "空", "1d", (), 0.0)
        ok, ind = self.no_raise(lambda: compute(empty), "compute(空序列) 不抛异常")
        if ok:
            self.eq(len(ind.names()), 27, "仍然产出 27 个指标列")
            self.true(all(v.size == 0 for v in ind.values.values()),
                      "所有列长度都是 0")
            self.eq(ind.latest("rsi"), None, "latest 返回 None")
            self.eq(ind.at("rsi", 0), None, "at(0) 返回 None（不抛 IndexError）")
            self.raises(IndexError, lambda: ind.snapshot(-1), "snapshot 明确抛错")

    def test_one_and_two_bars(self):
        for n in (1, 2, 3, 5, 13):
            s = mk_series([100.0 + i for i in range(n)])
            ok, ind = self.no_raise(lambda s=s: compute(s), f"{n} 根 → compute 不抛异常")
            if ok:
                self.true(all(v.size == n for v in ind.values.values()),
                          f"{n} 根 → 每列长度都是 {n}")
        # 单根时 RSI/ATR/SMA20 都还不可用
        ind1 = compute(mk_series([100.0]))
        self.eq(ind1.latest("rsi"), None, "单根 → RSI 不可用")
        self.eq(ind1.latest("sma20"), None, "单根 → SMA20 不可用")
        self.not_nan(ind1.latest("ema12"), "单根 → EMA12 可用（无预热期）")

    def test_no_indicator_function_raises_on_empty(self):
        empty = np.array([])
        bad = []
        for name, fn in INDICATOR_FUNCS:
            try:
                fn(empty)
            except Exception as e:                      # noqa: BLE001
                bad.append(f"{name}: {type(e).__name__}: {e}")
        self.eq(bad, [], f"18 个指标函数在空数组上都不抛异常")

    def test_no_indicator_function_raises_on_single(self):
        one = np.array([100.0])
        bad = []
        for name, fn in INDICATOR_FUNCS:
            try:
                fn(one)
            except Exception as e:                      # noqa: BLE001
                bad.append(f"{name}: {type(e).__name__}: {e}")
        self.eq(bad, [], "18 个指标函数在单元素上都不抛异常")

    def test_no_indicator_function_raises_on_two(self):
        two = np.array([100.0, 101.0])
        bad = []
        for name, fn in INDICATOR_FUNCS:
            try:
                fn(two)
            except Exception as e:                      # noqa: BLE001
                bad.append(f"{name}: {type(e).__name__}: {e}")
        self.eq(bad, [], "18 个指标函数在两元素上都不抛异常")

    def test_short_series_strategies_do_not_crash(self):
        """极短序列跑全部 5 个策略：不能抛异常，信号长度必须正确。"""
        ind = compute(mk_series([100.0, 101.0, 102.0]))
        ok, sigs = self.no_raise(lambda: run_all_strategies(ind),
                                 "3 根序列上 5 个策略都不抛异常")
        if ok:
            self.eq(len(sigs), 5, "5 个策略全部产出信号")
            self.true(all(s.position.size == 3 for s in sigs.values()),
                      "每个信号序列长度 = 3")

    def test_macd_signals_on_two_bars_others_do_not(self):
        """
        真实行为（值得注意）：MACD 的 EMA 没有预热期，DIF/DEA 从第 0 根就有值，
        于是 `macd_trend` 在**只有 2 根 K 线**时就能给出买入信号。
        其余 4 个策略都因为 RSI/ADX/布林/唐奇安还没预热而保持空仓。

        风险：autotrader 的 score 模式在短序列上会拿到 momentum 因子，
        而 trend/strength/reversion/position 全是缺失 —— 只有一个动能因子的
        加权分可能直接越过 0.25 阈值。所以「数据不足」时不要指望打分保护你。
        """
        ind2 = compute(mk_series([100.0, 101.0]))
        sigs2 = run_all_strategies(ind2)
        self.eq(int(sigs2["macd_trend"].position.sum()), 1,
                "2 根 K 线：macd_trend 已经给出 1 根持仓信号")
        others = [k for k, s in sigs2.items()
                  if k != "macd_trend" and int(s.position.sum()) != 0]
        self.eq(others, [], f"其余策略都不开仓（{others}）")
        # 对照：13 根时 MACD 的数据来源仍然只有 EMA，但至少不是 2 根了
        ind3 = compute(mk_series([100.0, 101.0, 102.0]))
        sigs3 = run_all_strategies(ind3)
        self.true(int(sigs3["macd_trend"].position.sum()) >= 1,
                  "3 根时 macd_trend 仍然持仓（同一机制）")

    def test_empty_series_strategies_do_not_crash(self):
        ind = compute(Series("test", "crypto", "T", "空", "1d", (), 0.0))
        ok, sigs = self.no_raise(lambda: run_all_strategies(ind),
                                 "空序列上 5 个策略都不抛异常")
        if ok:
            self.true(all(s.position.size == 0 for s in sigs.values()),
                      "空序列 → 信号长度 0")


class TestAllNanInput(TestCase):
    name = "edge.全 NaN 输入"

    def test_compute_on_all_nan(self):
        ok, ind = self.no_raise(lambda: compute(allnan_series(50)),
                                "全 NaN 行情 → compute 不抛异常")
        if ok:
            self.eq(len(ind.names()), 27, "仍然产出 27 列")
            self.eq(ind.latest("ema12"), None, "EMA 也是 NaN → at() 返回 None")

    def test_true_range_and_atr_with_nan(self):
        a = np.full(20, np.nan)
        self.true(bool(np.all(np.isnan(true_range(a, a, a)))),
                  "全 NaN → TR 全 NaN")
        self.true(bool(np.all(np.isnan(atr(a, a, a, 14)))), "全 NaN → ATR 全 NaN")

    def test_macd_with_nan_does_not_crash(self):
        a = np.full(40, np.nan)
        ok, out = self.no_raise(lambda: macd(a, 12, 26, 9), "全 NaN → MACD 不抛异常")
        if ok:
            self.true(all(bool(np.all(np.isnan(x))) for x in out), "DIF/DEA/柱 全 NaN")

    def test_bollinger_with_nan(self):
        a = np.full(40, np.nan)
        mid, up, lo, w = bollinger(a, 20, 2.0)
        self.true(bool(np.all(np.isnan(up))), "全 NaN → 上轨全 NaN")
        self.true(bool(np.all(np.isnan(w))), "全 NaN → 带宽全 NaN（np.where 保护住）")

    def test_nan_volume_does_not_break_obv(self):
        c = np.array([10., 11., 10.])
        v = np.full(3, np.nan)
        obv(c, v)
        o = obv(c, v)
        self.true(bool(np.isnan(o[1])), "NaN 成交量 → OBV 变 NaN（会一路传下去）")
        self.true(bool(np.isnan(o[2])), "而且不可恢复")

    def test_strategies_on_all_nan_never_signal(self):
        ind = compute(allnan_series(60))
        sigs = run_all_strategies(ind)
        self.true(all(int(s.position.sum()) == 0 for s in sigs.values()),
                  "全 NaN 行情下没有任何策略开仓（NaN 比较全为 False）")

    def test_backtest_on_all_nan_raises(self):
        """
        ✅ 修正后（2026-10-16）：全 NaN 行情**明确抛 ValueError**。

        原缺陷：回测不抛异常，而是安静地返回 total_return = nan、final = nan、
        equity_curve 全是 nan。调用方拿不到「数据坏了」的信号，
        只会看到一片 NaN 指标 —— 这类静默错误比崩溃危险得多。

        现在 run_backtest 在入口检查收盘价，坏数据超过阈值（≥3 根且 >5%）
        就报错。本例 30 根全 NaN，必然触发。
        """
        s = allnan_series(30)
        self.raises(ValueError,
                    lambda: run_backtest(s, sig_pos([0] * 30), costs=ZERO),
                    "全 NaN + 空仓信号 → 明确 ValueError（不再静默返回 NaN）")
        # 少量坏数据则容忍：只警告，不中断
        partly = mk_series([100.0, float("nan")] + [100.0 + i for i in range(28)])
        ok, r = self.no_raise(
            lambda: run_backtest(partly, sig_pos([0] * 30), costs=ZERO),
            "单根 NaN（30 根里 1 根）→ 容忍，不抛异常")
        if ok:
            self.true(np.isfinite(r.total_return),
                      f"权益没被单根 NaN 污染：total_return={r.total_return}")

    def test_backtest_with_all_in_signal_on_nan_prices(self):
        """
        ✅ 修正后：全 NaN 价格 + 全程看多信号同样抛 ValueError ——
        数据源坏了就该停下来，而不是给一个"没成交、权益 NaN"的结果。
        """
        s = allnan_series(30)
        self.raises(ValueError,
                    lambda: run_backtest(s, sig_pos([1] * 30), costs=ZERO,
                                         initial=10_000.0),
                    "全 NaN 价格 + 看多信号 → ValueError")


class TestConstantPrice(TestCase):
    name = "edge.常数价格（零方差）"

    def test_bollinger_bands_collapse(self):
        flat = np.full(60, 50.0)
        ind = compute(mk_series(list(flat)))
        self.near(ind.latest("bb_upper"), 50.0, 1e-12, "上轨 = 50")
        self.near(ind.latest("bb_lower"), 50.0, 1e-12, "下轨 = 50")
        self.near(ind.latest("bb_upper") - ind.latest("bb_lower"), 0.0, 1e-12,
                  "上下轨相等（零方差）")
        self.near(ind.latest("bb_width"), 0.0, 1e-12, "带宽 0%（不是 NaN / inf）")
        self.false(np.isinf(ind.values["bb_width"][-1]), "带宽不是 inf")

    def test_no_division_by_zero_anywhere(self):
        """
        真正「OHLC 全相等」的死盘（不是只有收盘价相等）：
        TR = H-L = 0 → ATR = 0 → ATR/价格 = 0%（不是 0/0 = NaN）。
        """
        flat = mk_series([50.0] * 60, highs=[50.0] * 60, lows=[50.0] * 60)
        ind = compute(flat)
        bad = []
        for key in ("atrPct", "volumeRatio", "bb_width", "rsi", "kdj_k", "kdj_d"):
            v = ind.values[key][-1]
            if np.isinf(v):
                bad.append(f"{key} 是 inf")
        self.eq(bad, [], "没有指标出现 inf")
        self.near(ind.latest("atr"), 0.0, 1e-12, "ATR = 0（H-L 恒为 0）")
        self.near(ind.latest("atrPct"), 0.0, 1e-12, "ATR/价格 = 0%（不是 NaN）")
        self.near(ind.latest("rsi"), 50.0, 1e-12, "RSI = 50")
        self.near(ind.latest("kdj_k"), 50.0, 1e-12, "KDJ K = 50")

    def test_close_equal_but_hl_spread_still_gives_atr(self):
        """
        对照：只让**收盘价**相等、但 high/low 有 1% 振幅时，ATR 不为 0（约 1.0）。
        这个用例是为了防止把上一条的 0% 当成「所有常数价格都算 0」的误解。
        """
        ind = compute(mk_series([50.0] * 60))   # mk_series 默认 high/low = ±1%
        self.true(abs(ind.latest("atr") - 1.0) < 0.05,
                  f"H-L = 1 → ATR ≈ 1.0（实际 {ind.latest('atr'):.4f}）")
        self.true(abs(ind.latest("atrPct") - 2.0) < 0.1,
                  f"ATR/价格 ≈ 2%（实际 {ind.latest('atrPct'):.4f}）")

    def test_adx_is_nan_on_dead_flat_market(self):
        """
        真实行为：完全横盘时 +DM = -DM = 0 → +DI = -DI = 0 → denom = 0
        → DX 置 NaN → ADX 整条 NaN。这是「契约」而非崩溃，但下游拿到 NaN
        时会退回趋势市权重（regime_weights 对 None 的处理）。
        """
        ind = compute(mk_series([50.0] * 80))
        self.true(bool(np.all(np.isnan(ind.values["adx"]))), "ADX 全 NaN")
        self.eq(ind.latest("adx"), None, "at('adx') 返回 None（不是 nan）")

    def test_score_factors_on_flat_market(self):
        ind = compute(mk_series([50.0] * 80))
        f, score, regime = score_factors(ind, 79)
        self.eq(regime, "未知", "ADX 为 None → 状态未知（不报错）")
        self.true(-1.0 <= score <= 1.0, f"总分仍在区间内（{score:+.4f}）")
        self.near(f.get("position", 0.0), 0.0, 1e-12,
                  "上下轨相等 → position 因子被跳过（不会除零）")

    def test_decider_on_flat_market_holds(self):
        ind = compute(mk_series([50.0] * 80))
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=79, position=0, fraction=0.0, entry_price=0.0,
            peak_price=0.0, drawdown=0.0, bars_since_exit=999, bars_held=0)
        self.eq(dec.action, "hold", f"死盘不动（分数 {dec.score:+.4f}）")

    def test_backtest_on_flat_prices_zero_return(self):
        s = mk_series([50.0] * 20)
        r = run_backtest(s, sig_pos([1] * 20), costs=ZERO, initial=10_000.0)
        self.near(r.total_return, 0.0, 1e-9, "常数价格 → 收益 0")
        self.near(r.max_drawdown, 0.0, 1e-9, "无回撤")
        self.eq(r.trade_count, 0, "未平仓 → 无完成交易")


class TestZeroAndNegativePrices(TestCase):
    name = "edge.价格为 0 / 负数"

    def _edge_series(self, px, n=60):
        return mk_series([px] * n)

    def test_zero_price_compute(self):
        # 价格序列里混一个 0（跌到 0 然后回来）
        closes = [10.0] * 20 + [0.0] + [10.0] * 20
        ok, ind = self.no_raise(lambda: compute(mk_series(closes)),
                                "价格含 0 → compute 不抛异常")
        if ok:
            self.eq(len(ind.names()), 27, "仍然产出 27 列")
            self.true(np.isnan(ind.values["atrPct"][20]) or
                      np.isfinite(ind.values["atrPct"][20]),
                      "close=0 那根 atrPct 是 NaN 或有限值，不是 inf")
            self.false(np.isinf(ind.values["atrPct"][20]), "atrPct 不是 inf")

    def test_all_zero_prices(self):
        ok, ind = self.no_raise(lambda: compute(self._edge_series(0.0)),
                                "全部价格为 0 → compute 不抛异常")
        if ok:
            self.true(np.isnan(ind.values["atrPct"][-1]),
                      "close=0 → atrPct = NaN（close > 0 保护住了）")
            self.true(np.isnan(ind.values["bb_width"][-1]),
                      "中轨 = 0 → 带宽 = NaN（mid != 0 的分支没走到）")

    def test_negative_price_compute(self):
        closes = [10.0] * 20 + [-5.0] + [10.0] * 20
        ok, ind = self.no_raise(lambda: compute(mk_series(closes)),
                                "价格含负数 → compute 不抛异常")
        if ok:
            self.eq(len(ind.names()), 27, "仍然产出 27 列")

    def test_rsi_on_zero_prices(self):
        a = np.array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        ok, r = self.no_raise(lambda: rsi(a, 2), "含 0 的价格算 RSI 不抛异常")
        if ok:
            self.true(bool(np.all(np.isfinite(r[~np.isnan(r)]))),
                      "已算出的 RSI 都是有限值")

    def test_obv_with_negative_volume(self):
        """负成交量（数据错误）不应让 OBV 抛异常，但符号会反。"""
        c = np.array([10., 11., 12.])
        v = np.array([100., -100., 100.])
        o = obv(c, v)
        self.near(o[1], -100.0, 1e-12, "负成交量直接减（不做校验）")
        self.near(o[2], 0.0, 1e-12, "结果被负值抵消")


class TestExtremeGap(TestCase):
    name = "edge.单次巨幅跳空"

    def test_gap_captured_by_atr_and_tr(self):
        closes = [100.0] * 30 + [1000.0] + [1000.0] * 10
        highs = [c * 1.001 for c in closes]
        lows = [c * 0.999 for c in closes]
        ind = compute(mk_series(closes, highs=highs, lows=lows))
        tr = true_range(np.array(highs), np.array(lows), np.array(closes))
        self.near(tr[30], 1000.0 * 1.001 - 100.0, 1e-6,
                  "跳空那根 TR = |H-前C| ≈ 901")
        self.true(ind.values["atr"][30] > 10.0,
                  f"ATR 立刻被抬高（{ind.values['atr'][30]:.2f}）")

    def test_regime_and_score_after_gap(self):
        closes = [100.0] * 30 + [1000.0] + [1000.0] * 10
        ind = compute(mk_series(closes))
        f, score, regime = score_factors(ind, 40)
        self.true(-1.0 <= score <= 1.0, f"跳空后总分仍在区间（{score:+.4f}）")
        self.true("trend" in f, "趋势因子仍然算得出来（用 ATR 归一化就不怕跳空）")

    def test_stop_loss_on_gap_down(self):
        """
        持仓成本 100，价格一夜跳到 80（-20%），止损线 6%：
        必须立刻触发，而不是等价格「慢慢跌」。
        """
        px = [100.0] * 40 + [80.0] * 5
        ind = compute(mk_series(px))
        i = len(px) - 1
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=i, position=1, fraction=0.9, entry_price=100.0,
            peak_price=100.0, drawdown=0.0, bars_since_exit=0, bars_held=5)
        self.true(dec.risk_hit is not None and "止损" in dec.risk_hit,
                  f"跳空缺口立刻触发止损（{dec.risk_hit}）")
        self.eq(dec.action, "sell", "清仓")

    def test_trailing_stop_on_gap(self):
        """峰值 100 → 跳到 70（-30%），成本 60（仍浮盈）→ 移动止损必须触发。"""
        px = [60.0] * 20 + [100.0] * 10 + [70.0] * 5
        ind = compute(mk_series(px))
        i = len(px) - 1
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=i, position=1, fraction=0.9, entry_price=60.0,
            peak_price=100.0, drawdown=0.0, bars_since_exit=0, bars_held=5)
        self.true(dec.risk_hit is not None, f"触发风控（{dec.risk_hit}）")
        self.true("移动止损" in (dec.risk_hit or "") or "止损" in (dec.risk_hit or ""),
                  f"原因是止损类（{dec.risk_hit}）")


class TestMissingIndicators(TestCase):
    name = "edge.指标全缺失时的打分"

    def test_score_factors_with_empty_values(self):
        ind = IndicatorSet(series=Series("t", "crypto", "T", "t", "1d", (), 0.0),
                           values={})
        ok, out = self.no_raise(lambda: score_factors(ind, 0),
                                "values 为空的 IndicatorSet → 不抛异常")
        if ok:
            f, score, regime = out
            self.eq(f, {}, "没有任何因子")
            self.near(score, 0.0, 1e-12, "总分 0（total_w <= 0 提前返回）")
            self.eq(regime, "未知", "状态未知")

    def test_decide_with_all_missing_indicators(self):
        ind = IndicatorSet(series=Series("t", "crypto", "T", "t", "1d", (), 0.0),
                           values={})
        ok, dec = self.no_raise(
            lambda: AutoDecider(AutoConfig(mode="score")).decide(
                ind=ind, i=0, position=0, fraction=0.0, entry_price=0.0,
                peak_price=0.0, drawdown=0.0, bars_since_exit=999, bars_held=0),
            "指标全缺失 → decide 不抛异常")
        if ok:
            self.eq(dec.action, "hold", "不动")
            self.near(dec.target, 0.0, 1e-12, "目标仓位 0")

    def test_decide_with_none_indicators(self):
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=None, i=0, position=0, fraction=0.0, entry_price=0.0,
            peak_price=0.0, drawdown=0.0, bars_since_exit=0, bars_held=0)
        self.eq(dec.action, "hold", "ind=None → hold")

    def test_decide_with_negative_index(self):
        ind = compute(mk_series([100.0 + i for i in range(40)]))
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=-1, position=0, fraction=0.0, entry_price=0.0,
            peak_price=0.0, drawdown=0.0, bars_since_exit=999, bars_held=0)
        self.eq(dec.action, "hold", "负下标 → hold（不抛异常）")
        self.eq(dec.note, "没有行情数据", "给出说明")

    def test_ai_context_with_empty_indicator_set(self):
        ind = IndicatorSet(series=Series("t", "crypto", "T", "t", "1d", (), 0.0),
                           values={})
        ok, ctx = self.no_raise(
            lambda: AIDecider.build_context(
                ind, 0, symbol="T", name="t", interval="1d", position=0,
                fraction=0.0, entry_price=0.0, equity=1.0, cash=1.0,
                peak_price=0.0, drawdown=0.0),
            "空 IndicatorSet → 构造 AI 上下文不抛异常")
        if ok:
            self.true(all(v is None for v in ctx["指标"].values()),
                      "所有指标为 null")


class TestDegenerateBacktestAndPaper(TestCase):
    name = "edge.回测/模拟盘退化输入"

    def test_backtest_with_constant_prices(self):
        s = mk_series([100.0] * 10)
        r = run_backtest(s, sig_pos([1, 1, 1, 1, 1, 0, 0, 0, 0, 0]),
                         costs=ZERO, initial=10_000.0)
        self.near(r.total_return, 0.0, 1e-9, "常数价格一买一卖 → 收益 0")
        self.eq(r.trade_count, 1, "1 笔交易（零利润）")
        self.near(r.avg_win, 0.0, 1e-12, "零利润不计为盈利")

    def test_backtest_with_zero_prices(self):
        """
        ✅ 修正后：全 0 价格明确抛 ValueError。

        原行为是"买不进（px>0 保护）→ 收益 0"，看起来无害，但 0 价格本身
        就是坏数据（真实行情不可能为 0），算出"收益 0"会让人以为策略
        在中性行情里表现平稳，实际是数据源坏了。
        """
        s = mk_series([0.0] * 10)
        self.raises(ValueError,
                    lambda: run_backtest(s, sig_pos([1] * 10), costs=ZERO,
                                         initial=10_000.0),
                    "全 0 价格 → ValueError（坏数据不静默）")

    def test_backtest_annual_return_guard_on_loss(self):
        """总收益为负时不能对负数开根号（会变成复数/NaN）。"""
        s = mk_series([200.0 - i * 10 for i in range(15)])
        r = run_backtest(s, sig_pos([1] * 15), costs=ZERO, initial=10_000.0)
        self.true(r.total_return < 0, f"下跌行情收益为负（{r.total_return}%）")
        self.true(np.isfinite(r.annual_return),
                  f"年化是有限值（{r.annual_return}），没有对负数开根号")

    def test_paper_with_constant_prices(self):
        s = mk_series([100.0] * 6)
        pt = PaperTrader(s, sig_pos([1] * 6), costs=ZERO, initial=10_000.0)
        ok, _ = self.no_raise(lambda: pt.run_all(), "常数价格 → 模拟盘不抛异常")
        if ok:
            self.near(pt.snapshot().total_return, 0.0, 1e-9, "收益 0")
            self.near(pt.snapshot().max_drawdown, 0.0, 1e-9, "无回撤")

    def test_paper_with_zero_prices(self):
        s = mk_series([0.0] * 6)
        pt = PaperTrader(s, sig_pos([1] * 6), costs=ZERO, initial=10_000.0)
        ok, _ = self.no_raise(lambda: pt.run_all(), "全 0 价格 → 模拟盘不抛异常")
        if ok:
            self.eq(len(pt.fills), 0, "买不进（价格无效）")
            rejects = [o for o in pt.orders if o.state.value == "rejected"]
            self.true(len(rejects) >= 1, "有被拒委托")

    def test_paper_with_all_nan_prices_rejected_not_crashed(self):
        """
        ✅ 修正后（2026-10-16）：全 NaN 行情下模拟盘**不再崩溃**，
        而是把买单正常拒掉（带明确原因），回放能跑完。

        原缺陷：抛 `ValueError: cannot convert float NaN to integer`。
        根因链（paper.py:300-307）：
            px = self.costs.buy_price(raw)      # raw = bar.open = NaN
            if px <= 0:                          # NaN <= 0 是 False → 保护不生效！
            qty = math.floor(raw_qty / lot)      # ← 这里抛 ValueError

        修法是把保护写成 `not (px > 0)` —— 对 NaN 得到 True，能正确拦住。
        这条断言的是"优雅拒绝"而不是"崩溃"：坏数据应该产生一张被拒委托，
        而不是让整个回放挂掉（用户至少要能看到发生了什么）。
        """
        pt = PaperTrader(allnan_series(6), sig_pos([1] * 6), costs=ZERO,
                         initial=10_000.0)
        ok, _ = self.no_raise(lambda: pt.run_all(),
                              "全 NaN 价格 → 模拟盘不崩溃")
        if ok:
            self.eq(len(pt.fills), 0, "一笔都没成交（价格无效）")
            rejects = [o for o in pt.orders if o.state.value == "rejected"]
            self.true(len(rejects) >= 1, f"有被拒委托（{len(rejects)} 张）")
            self.true(any("NaN" in (o.reject_reason or "") or "价格" in (o.reject_reason or "")
                          for o in rejects),
                      "拒绝原因说清了是价格问题："
                      + str([o.reject_reason for o in rejects][:2]))
        # 根因备忘：NaN 的比较全是 False —— 这正是当初写错的原因
        self.false(float("nan") <= 0, "NaN <= 0 为 False —— 所以 px <= 0 拦不住 NaN")
        self.true(not (float("nan") > 0), "not (NaN > 0) 为 True —— 正确写法")

    def test_paper_empty_series(self):
        s = Series("t", "crypto", "T", "空", "1d", (), 0.0)
        pt = PaperTrader(s, sig_pos([]), costs=ZERO, initial=10_000.0)
        ok, _ = self.no_raise(lambda: pt.run_all(), "空序列 → 模拟盘不抛异常")
        if ok:
            self.false(pt.step(), "没有 K 线 → step() 立刻返回 False")
            self.near(pt.snapshot().equity, 10_000.0, 1e-9, "权益 = 初始")

    def test_backtest_two_bars_rejected(self):
        """少于 3 根必须明确拒绝，不能算出假结果。"""
        s = mk_series([100.0, 110.0])
        self.raises(ValueError,
                    lambda: run_backtest(s, sig_pos([1, 1]), costs=ZERO),
                    "2 根 → ValueError")


class TestStrategyEdgeSignals(TestCase):
    name = "edge.策略在退化序列上的信号"

    def test_constant_prices_produce_no_signals(self):
        ind = compute(mk_series([100.0] * 60))
        sigs = run_all_strategies(ind)
        total = sum(int(s.position.sum()) for s in sigs.values())
        self.eq(total, 0, "常数价格下所有策略都不开仓（没有假信号）")

    def test_strong_uptrend_opens_positions(self):
        """对照：明显上涨时至少有一个策略会开仓（否则说明信号层坏了）。"""
        closes = [100.0 * (1.02 ** i) for i in range(120)]
        ind = compute(mk_series(closes))
        sigs = run_all_strategies(ind)
        total = sum(int(s.position.sum()) for s in sigs.values())
        self.true(total > 0, f"上涨趋势下有策略开仓（持仓根数合计 {total}）")

    def test_position_array_is_binary(self):
        closes = [100.0 + 10 * np.sin(i / 5.0) for i in range(200)]
        ind = compute(mk_series(closes))
        sigs = run_all_strategies(ind)
        bad = [k for k, s in sigs.items()
               if not set(np.unique(s.position)).issubset({0, 1})]
        self.eq(bad, [], "所有策略的 position 只含 0/1（不做空）")

    def test_entry_exit_marking_is_consistent(self):
        """entry/exit 只在仓位变化处打标，不能重复打。"""
        closes = [100.0 + 10 * np.sin(i / 5.0) for i in range(200)]
        ind = compute(mk_series(closes))
        for name, s in run_all_strategies(ind).items():
            d = np.diff(s.position.astype(int))
            self.eq(int(s.entry.sum()), int((d == 1).sum()),
                    f"{name}: entry 标记数 == 0→1 的变化次数")
            self.eq(int(s.exit.sum()), int((d == -1).sum()),
                    f"{name}: exit 标记数 == 1→0 的变化次数")


REGISTRY.extend([TestDegenerateLengths, TestAllNanInput, TestConstantPrice,
                 TestZeroAndNegativePrices, TestExtremeGap, TestMissingIndicators,
                 TestDegenerateBacktestAndPaper, TestStrategyEdgeSignals])

declare_counts({
    "TestDegenerateLengths": {"test_empty_series_compute": (6, 6),
                              "test_one_and_two_bars": (13, 13),
                              "test_no_indicator_function_raises_on_empty": (1, 1),
                              "test_no_indicator_function_raises_on_single": (1, 1),
                              "test_no_indicator_function_raises_on_two": (1, 1),
                              "test_short_series_strategies_do_not_crash": (3, 3),
                              "test_macd_signals_on_two_bars_others_do_not": (3, 3),
                              "test_empty_series_strategies_do_not_crash": (2, 2)},
    "TestAllNanInput": {"test_compute_on_all_nan": (3, 3),
                        "test_true_range_and_atr_with_nan": (2, 2),
                        "test_macd_with_nan_does_not_crash": (2, 2),
                        "test_bollinger_with_nan": (2, 2),
                        "test_nan_volume_does_not_break_obv": (2, 2),
                        "test_strategies_on_all_nan_never_signal": (1, 1),
                        # 2026-10-16：行为修正后同步更新
                        #   · 全 NaN 现在明确抛 ValueError（不再是静默 NaN）
                        #   · 用例改名 _does_not_crash → _raises
                        "test_backtest_on_all_nan_raises": (3, 3),
                        "test_backtest_with_all_in_signal_on_nan_prices": (1, 1)},
    "TestConstantPrice": {"test_bollinger_bands_collapse": (5, 5),
                          "test_no_division_by_zero_anywhere": (5, 5),
                          "test_close_equal_but_hl_spread_still_gives_atr": (2, 2),
                          "test_adx_is_nan_on_dead_flat_market": (2, 2),
                          "test_score_factors_on_flat_market": (3, 3),
                          "test_decider_on_flat_market_holds": (1, 1),
                          "test_backtest_on_flat_prices_zero_return": (3, 3)},
    "TestZeroAndNegativePrices": {"test_zero_price_compute": (4, 4),
                                  "test_all_zero_prices": (3, 3),
                                  "test_negative_price_compute": (2, 2),
                                  "test_rsi_on_zero_prices": (2, 2),
                                  "test_obv_with_negative_volume": (2, 2)},
    "TestExtremeGap": {"test_gap_captured_by_atr_and_tr": (2, 2),
                       "test_regime_and_score_after_gap": (2, 2),
                       "test_stop_loss_on_gap_down": (2, 2),
                       "test_trailing_stop_on_gap": (2, 2)},
    "TestMissingIndicators": {"test_score_factors_with_empty_values": (4, 4),
                              "test_decide_with_all_missing_indicators": (3, 3),
                              "test_decide_with_none_indicators": (1, 1),
                              "test_decide_with_negative_index": (2, 2),
                              "test_ai_context_with_empty_indicator_set": (2, 2)},
    "TestDegenerateBacktestAndPaper": {
        "test_backtest_with_constant_prices": (3, 3),
        # 2026-10-16：全 0 价格现在明确抛 ValueError，断言数 2 → 1
        "test_backtest_with_zero_prices": (1, 1),
        "test_backtest_annual_return_guard_on_loss": (2, 2),
        "test_paper_with_constant_prices": (3, 3),
        "test_paper_with_zero_prices": (3, 3),
        # 用例改名：_crashes → _rejected_not_crashed（现在优雅拒绝而非崩溃）
        "test_paper_with_all_nan_prices_rejected_not_crashed": (6, 6),
        "test_paper_empty_series": (3, 3),
        "test_backtest_two_bars_rejected": (1, 1)},
    "TestStrategyEdgeSignals": {"test_constant_prices_produce_no_signals": (1, 1),
                                "test_strong_uptrend_opens_positions": (1, 1),
                                "test_position_array_is_binary": (1, 1),
                                "test_entry_exit_marking_is_consistent": (10, 10)},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_edge_cases.py"))
