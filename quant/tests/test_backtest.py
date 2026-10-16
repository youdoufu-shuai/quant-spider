#!/usr/bin/env python3
"""
回测引擎验证 —— 全部用**能手工算出期望值**的场景。

重点：
  1. 执行时点：第 i 根收盘的信号，只能在第 i+1 根**开盘**成交（禁止未来函数）
  2. 成本：佣金 + 滑点方向（买入抬价、卖出压价）必须都起作用
  3. 绩效口径：最大回撤、年化因子、波动率年化、成本拖累
  4. 交易统计：胜率/盈亏比/期望，只统计**已平仓**交易

直接运行：python quant/tests/test_backtest.py
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
from strategy import SignalSet                           # noqa: E402
from backtest import Costs, MARKET_COSTS, _annualize_factor, _max_drawdown, run_backtest  # noqa: E402


def mk_bars(prices, *, opens=None, market="crypto", interval="1d", vol=1.0) -> Series:
    """OHLC 全等于价格（除非给 opens），这样任何「用错价格」都会立刻暴露。"""
    bars = []
    for i, p in enumerate(prices):
        o = opens[i] if opens is not None else p
        bars.append(Bar(1_700_000_000_000 + i * 86_400_000, float(o),
                        float(max(o, p)), float(min(o, p)), float(p), float(vol)))
    return Series("test", market, "TEST", "测试", interval, tuple(bars), 0.0)


def mk_sig(positions, n=None, reasons=None) -> SignalSet:
    pos = np.asarray(positions, dtype=np.int8)
    n = n or pos.size
    ent = np.zeros(n, bool)
    ext = np.zeros(n, bool)
    for i in range(1, min(n, pos.size)):
        if pos[i] == 1 and pos[i - 1] == 0:
            ent[i] = True
        if pos[i] == 0 and pos[i - 1] == 1:
            ext[i] = True
    return SignalSet("t", "测试策略", "", pos, ent, ext,
                     reasons or [""] * n, {})


RAMP = [100., 110., 120., 130., 140., 150., 160., 170., 180., 190.]   # 等差数列
ZERO = Costs(commission=0.0, slippage=0.0, min_commission=0.0)


class TestCosts(TestCase):
    name = "backtest.Costs"

    def test_slippage_direction(self):
        c = Costs(commission=0.0, slippage=0.01)
        self.near(c.buy_price(100), 101.0, 1e-12, "买入价上滑 1% → 101")
        self.near(c.sell_price(100), 99.0, 1e-12, "卖出价下滑 1% → 99")

    def test_fee_and_min_commission(self):
        self.near(Costs(commission=0.001).fee(10_000), 10.0, 1e-12,
                  "费 = 金额 × 费率 = 10")
        self.near(Costs(commission=0.001, min_commission=5.0).fee(100), 5.0, 1e-12,
                  "低于最低佣金时按 5 元收")
        self.near(Costs(commission=0.001, min_commission=5.0).fee(10_000), 10.0, 1e-12,
                  "高于最低佣金时按比例")
        self.near(Costs(commission=0.001).fee(-10_000), 10.0, 1e-12,
                  "负数金额取绝对值（不该出现，但不能算出负费）")

    def test_market_defaults(self):
        self.near(MARKET_COSTS["ashare"].commission, 0.0003, 1e-12, "A股默认万三")
        self.near(MARKET_COSTS["crypto"].commission, 0.0010, 1e-12, "加密默认千一")


class TestMaxDrawdown(TestCase):
    name = "backtest._max_drawdown"

    def test_hand_computed(self):
        dd, idx = _max_drawdown(np.array([100., 150., 75.]))
        self.near(dd, 0.5, 1e-12, "(150-75)/150 = 50%")
        self.eq(idx, 2, "触底在 idx=2")

    def test_two_drawdowns_takes_worst(self):
        dd, idx = _max_drawdown(np.array([100., 90., 120., 60.]))
        self.near(dd, 0.5, 1e-12, "第二段 (120-60)/120 = 50% 被捕获")
        self.eq(idx, 3, "触底在 idx=3")

    def test_monotone_rise_no_drawdown(self):
        dd, idx = _max_drawdown(np.array([100., 110., 120.]))
        self.near(dd, 0.0, 1e-12, "单调上涨 → 无回撤")
        self.eq(idx, 0, "触底位置保持 0")

    def test_all_zero_equity_does_not_divide_by_zero(self):
        """peak=0 时会走到 peak>0 的保护分支；这里确认不抛 ZeroDivisionError。"""
        ok, _ = self.no_raise(lambda: _max_drawdown(np.array([0., 0., 0.])),
                              "权益全为 0 → 不抛除零异常")
        if ok:
            dd, _ = _max_drawdown(np.array([0., 0., 0.]))
            self.near(dd, 0.0, 1e-12, "权益全 0 → 回撤记 0")


class TestAnnualizeFactor(TestCase):
    name = "backtest._annualize_factor"

    def test_known_intervals(self):
        self.near(_annualize_factor("1d"), 1 / 252, 1e-15, "日线：252 交易日/年")
        self.near(_annualize_factor("1w"), 1 / 52, 1e-15, "周线：52 周/年")
        self.near(_annualize_factor("1M"), 1 / 12, 1e-15, "月线：12 月/年")
        self.near(_annualize_factor("60m"), 1 / (24 * 365), 1e-15, "60 分钟线按自然日")
        self.near(_annualize_factor("1m"), 1 / (60 * 24 * 365), 1e-18, "1 分钟线")

    def test_unknown_interval_falls_back_to_daily(self):
        self.near(_annualize_factor("3d"), 1 / 252, 1e-15,
                  "未知粒度回退到日线口径（不抛异常）")


class TestNextBarOpenExecution(TestCase):
    name = "backtest.执行时点（禁止未来函数）"

    def test_buy_fills_at_next_open(self):
        """
        第 0 根收盘信号 → 第 1 根开盘成交。
        价格 100→110→120…，若用「第 0 根收盘 100」成交，收益会是 90%；
        正确（110 入场）应是 190/110-1 = 72.727%。
        """
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1] * 10), costs=ZERO, initial=10_000.0)
        self.near(r.total_return, (190 / 110 - 1) * 100, 0.01,
                  "收益 = 从第 1 根开盘 110 到末根收盘 190 = 72.727%")
        self.near(r.equity_curve[0], 10_000.0, 1e-9,
                  "第 0 根权益仍是初始资金 —— 证明第 0 根没有成交")
        self.near(r.trades[0]["entry_price"] if r.trades else 0, 0.0, 1e-9,
                  "未平仓 → 交易列表为空（不是用假成交凑数）")

    def test_next_open_differs_from_signal_bar_close(self):
        """
        构造「收盘 100，次日开盘 500 跳空」：若成交价是 100（用了信号那根的收盘），
        说明有未来函数。正确成交价必须是 500。
        """
        s = mk_bars([100., 600., 700.], opens=[100., 500., 600.])
        r = run_backtest(s, mk_sig([1, 1, 1]), costs=ZERO, initial=10_000.0)
        # 成交价 500，末根收盘 700 → 权益 14000
        self.near(r.equity_curve[-1], 14_000.0, 1e-6,
                  "10000/500 × 700 = 14000（成交价用的是次日开盘 500，不是信号日收盘 100）")
        self.near(r.total_return, 40.0, 1e-6, "总收益 40%")

    def test_sell_fills_at_next_open(self):
        """卖出同样顺延一根：信号在第 5 根收盘转空 → 第 6 根开盘卖出。"""
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1, 1, 1, 1, 1, 0, 0, 0, 0, 0]),
                         costs=ZERO, initial=10_000.0)
        self.eq(r.trade_count, 1, "恰好 1 笔已平仓交易")
        t = r.trades[0]
        self.near(t["entry_price"], 110.0, 1e-9, "第 1 根开盘 110 买入（信号来自第 0 根）")
        self.near(t["exit_price"], 160.0, 1e-9, "第 6 根开盘 160 卖出（信号来自第 5 根）")
        self.eq(t["entry_index"], 1, "成交下标 = 1")
        self.eq(t["exit_index"], 6, "成交下标 = 6")
        self.eq(t["bars_held"], 5, "持有 5 根")
        self.near(t["pnl"], 10_000.0 / 110 * (160 - 110), 1e-6,
                  "盈亏 = (10000/110) × 50 = 4545.4545")
        self.near(r.total_return, 45.4545, 1e-3, "总收益 45.45%")
        self.near(r.win_rate, 100.0, 1e-9, "唯一一笔盈利 → 胜率 100%")


class TestCostImpact(TestCase):
    name = "backtest.成本拖累"

    def test_fee_reduces_return(self):
        s = mk_bars(RAMP)
        sig = mk_sig([1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
        r_free = run_backtest(s, sig, costs=ZERO, initial=10_000.0)
        r_fee = run_backtest(s, sig, costs=Costs(commission=0.001, slippage=0.0),
                             initial=10_000.0)
        self.true(r_fee.total_return < r_free.total_return,
                  f"有手续费收益更低（{r_fee.total_return}% < {r_free.total_return}%）")
        self.true(r_fee.total_fees > 0, f"累计手续费 > 0（{r_fee.total_fees}）")

    def test_slippage_reduces_return(self):
        s = mk_bars(RAMP)
        sig = mk_sig([1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
        r_free = run_backtest(s, sig, costs=ZERO, initial=10_000.0)
        r_slip = run_backtest(s, sig, costs=Costs(0.0, 0.01), initial=10_000.0)
        self.true(r_slip.total_return < r_free.total_return,
                  f"有滑点收益更低（{r_slip.total_return}% < {r_free.total_return}%）")
        # 手算：入场 110×1.01=111.1，出场 160×0.99=158.4
        t = r_slip.trades[0]
        self.near(t["entry_price"], 111.1, 1e-6, "买入价 = 110 × 1.01 = 111.1")
        self.near(t["exit_price"], 158.4, 1e-6, "卖出价 = 160 × 0.99 = 158.4")

    def test_cost_drag_is_measured_not_estimated(self):
        """
        cost_drag 的定义是「零成本重跑的收益 − 实际收益」，是真实测量。
        这里验证它确实等于两者的差，而不是任何估算公式。
        """
        s = mk_bars(RAMP)
        sig = mk_sig([1, 1, 1, 1, 1, 0, 0, 0, 0, 0])
        c = Costs(commission=0.001, slippage=0.001)
        r = run_backtest(s, sig, costs=c, initial=10_000.0)
        r0 = run_backtest(s, sig, costs=ZERO, initial=10_000.0,
                          _measure_cost_drag=False)
        self.near(r.cost_drag, r0.total_return - r.total_return, 1e-3,
                  "cost_drag == 零成本收益 − 含成本收益")
        self.true(r.cost_drag > 0, f"成本拖累为正（{r.cost_drag} 个百分点）")

    def test_zero_cost_has_zero_fees_and_zero_drag(self):
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1] * 10), costs=ZERO, initial=10_000.0)
        self.near(r.total_fees, 0.0, 1e-12, "零成本 → 手续费 0")
        self.near(r.cost_drag, 0.0, 1e-9, "零成本 → 成本拖累 0")


class TestPerformanceMetrics(TestCase):
    name = "backtest.绩效口径"

    def test_flat_signal_keeps_equity_constant(self):
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([0] * 10), costs=ZERO, initial=10_000.0)
        self.near(r.total_return, 0.0, 1e-9, "全程空仓 → 收益 0")
        self.eq(r.trade_count, 0, "无交易")
        self.near(r.exposure, 0.0, 1e-9, "持仓时间占比 0")
        self.near(r.max_drawdown, 0.0, 1e-9, "无回撤")
        self.near(r.final, 10_000.0, 1e-9, "期末权益 = 初始")
        self.near(r.equity_curve[-1], 10_000.0, 1e-9, "权益曲线末端 = 初始")
        self.true(all(abs(x - 10_000.0) < 1e-9 for x in r.equity_curve),
                  "权益曲线恒定")

    def test_exposure_hand_computed(self):
        """全程持仓 10 根：第 0 根尚未成交（空仓），第 1~9 根持仓 → 9/10 = 90%。"""
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1] * 10), costs=ZERO, initial=10_000.0)
        self.near(r.exposure, 90.0, 1e-9, "持仓 9/10 根 = 90%")

    def test_volatility_annualization(self):
        """
        3 根日线的权益 [10000, 10099.01, 10297.03]：
        单根收益序列 = [0, 0.009901, 0.019609]（diff 后去掉第一个 0 之前…见下）
        年化波动率 = std(returns, ddof=1) × sqrt(252) × 100。
        直接按定义重算一遍比对。
        """
        s = mk_bars([100., 102., 104.])
        r = run_backtest(s, mk_sig([1, 1, 1]), costs=ZERO, initial=10_000.0)
        eq = np.array(r.equity_curve)
        rets = np.diff(eq) / eq[:-1]
        rets = rets[np.isfinite(rets)]
        expect = float(np.std(rets, ddof=1)) * np.sqrt(252) * 100
        self.near(r.volatility, expect, 1e-3,
                  f"波动率按 ddof=1 × sqrt(252) 年化（{expect:.3f}；结果已 round 到 3 位）")
        self.near(r.equity_curve[0], 10_000.0, 1e-9, "第 0 根权益 = 初始")
        self.true(r.equity_curve[-1] > 10_000.0, "权益上升")

    def test_annual_return_formula(self):
        """日线 10 根、收益 72.727% → 年化 = (1.72727)^(252/10) - 1。"""
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1] * 10), costs=ZERO, initial=10_000.0)
        expect = ((190 / 110) ** (252 / 10) - 1) * 100
        self.near(r.annual_return, round(expect, 3), 0.01,
                  "年化 = 总收益按实际天数（252/10 年）折算，不是简单乘 252")

    def test_benchmark_is_buy_and_hold_with_cost(self):
        s = mk_bars(RAMP)
        c = Costs(commission=0.0, slippage=0.01)
        r = run_backtest(s, mk_sig([0] * 10), costs=c, initial=10_000.0)
        self.near(r.benchmark_return, (190 / 101.0 - 1) * 100, 1e-3,
                  "基准 = 第 0 根开盘买入（含滑点 101）持有到末根收盘 190")

    def test_sharpe_and_sortino_zero_when_no_volatility(self):
        """权益完全不动 → 波动率 0 → 夏普/索提诺按 0 处理，不除零。"""
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([0] * 10), costs=ZERO, initial=10_000.0)
        self.near(r.volatility, 0.0, 1e-9, "波动率 0")
        self.near(r.sharpe, 0.0, 1e-12, "夏普 0（不是 inf/NaN）")
        self.near(r.sortino, 0.0, 1e-12, "索提诺 0")
        self.near(r.calmar, 0.0, 1e-12, "卡玛 0（回撤 0 的保护）")


class TestTradeStatistics(TestCase):
    name = "backtest.交易统计"

    def _two_win_series(self):
        """
        10 根：0-1 空仓，2-3 持仓（140→160），4-5 空仓，6-7 持仓（180→200），8-9 空仓。
        两笔都盈利。
        """
        prices = [100., 120., 140., 160., 170., 175., 180., 200., 210., 220.]
        sig = mk_sig([0, 0, 1, 1, 0, 0, 1, 1, 0, 0])
        return mk_bars(prices), sig

    def test_two_round_trips(self):
        s, sig = self._two_win_series()
        r = run_backtest(s, sig, costs=ZERO, initial=10_000.0)
        self.eq(r.trade_count, 2, "两笔已平仓交易")
        self.eq(r.win_count, 2, "两笔都盈利")
        self.eq(r.loss_count, 0, "无亏损")
        self.near(r.win_rate, 100.0, 1e-9, "胜率 100%")

        # 交易 A：第 2 根收盘信号 → 第 3 根开盘 160 买入；第 4 根信号→第 5 根开盘 175 卖出
        a, b = r.trades
        self.near(a["entry_price"], 160.0, 1e-9, "A 入场 160")
        self.near(a["exit_price"], 175.0, 1e-9, "A 出场 175")
        self.near(a["pnl"], 10_000.0 / 160 * 15, 1e-6, "A 盈利 = 62.5 × 15 = 937.5")
        self.near(b["entry_price"], 200.0, 1e-9, "B 入场 200（第 5 根信号→第 6 根开盘）")
        self.near(b["exit_price"], 220.0, 1e-9, "B 出场 220（第 7 根信号→第 8 根开盘）")
        self.near(b["pnl"], (10_000.0 / 160 * 175) / 200 * 20, 1e-6,
                  "B 盈利 = 上一笔平仓后的权益（A 末 = 10937.5）/200 × 20 = 1093.75"
                  "（全仓复利：第二笔的本金是第一笔的结果）")
        self.near(r.avg_win, round((a["pnl"] + b["pnl"]) / 2, 2), 1e-9,
                  "平均盈利 = 两笔均值（avg_win 已 round 到 2 位）")
        self.near(r.avg_loss, 0.0, 1e-12, "无亏损交易 → 平均亏损 0")
        self.near(r.payoff, 0.0, 1e-12, "总亏损 0 → 盈亏比按 0 处理（不除零）")
        self.near(r.expectancy, 1.0 * r.avg_win, 1e-6, "期望 = 胜率×avgWin = avgWin")

    def test_losing_trade_statistics(self):
        s = mk_bars([100., 100., 100., 80., 80., 80.])
        r = run_backtest(s, mk_sig([0, 1, 1, 0, 0, 0]), costs=ZERO, initial=10_000.0)
        self.eq(r.trade_count, 1, "一笔")
        self.eq(r.loss_count, 1, "亏损一笔")
        self.near(r.win_rate, 0.0, 1e-9, "胜率 0%")
        self.true(r.trades[0]["pnl"] < 0, f"盈亏为负（{r.trades[0]['pnl']:.2f}）")
        self.near(r.expectancy, -abs(r.avg_loss), 1e-6, "期望 = -(1-0)×|avgLoss|")
        self.near(r.profit_factor, 0.0, 1e-12, "无盈利 → 盈亏比 0（不除零）")
        self.true(r.max_drawdown > 0, f"有回撤（{r.max_drawdown}%）")

    def test_open_position_excluded_from_stats(self):
        """
        期末未平仓：计入权益（按末根收盘），但**不计入**交易统计。
        这防止浮动盈亏污染胜率。
        """
        s = mk_bars(RAMP)
        r = run_backtest(s, mk_sig([1] * 10), costs=ZERO, initial=10_000.0)
        self.true(r.open_position, "标记为期末持仓")
        self.eq(r.trade_count, 0, "未平仓 → 0 笔已平仓交易")
        self.near(r.win_rate, 0.0, 1e-12, "无已平仓交易 → 胜率 0（而不是 NaN）")
        self.near(r.avg_win, 0.0, 1e-12, "平均盈利 0")
        self.near(r.best_trade, 0.0, 1e-12, "最好交易 0（无交易时的默认值）")
        self.near(r.worst_trade, 0.0, 1e-12, "最差交易 0")
        self.true(r.final > 10_000.0, "但权益确实反映了浮盈")


class TestInputGuards(TestCase):
    name = "backtest.输入边界"

    def test_too_few_bars_raises(self):
        self.raises(ValueError, lambda: run_backtest(mk_bars([100., 110.]),
                                                     mk_sig([1, 1]), costs=ZERO),
                    "2 根 K 线 → 明确抛 ValueError（而不是算出假结果）")
        self.raises(ValueError, lambda: run_backtest(mk_bars([]),
                                                     mk_sig([], n=0), costs=ZERO),
                    "0 根 → ValueError")

    def test_three_bars_is_allowed(self):
        r = run_backtest(mk_bars([100., 110., 120.]), mk_sig([1, 1, 1]),
                         costs=ZERO, initial=10_000.0)
        self.near(r.equity_curve[-1], round(10_000.0 / 110 * 120, 2), 1e-2,
                  "3 根可以回测（边界值 3 放行）；权益曲线已 round 到 2 位")

    def test_zero_open_price_skips_that_bar_then_buys_later(self):
        """
        对抗性：第 i 根的买入价 <= 0 时，`if px > 0` 直接跳过 ——
        **不报错、不计一笔被拒交易、也没有任何日志**，只是这一根不成交。
        由于 held 仍是 0，下一根会再试一次。这里确认这个「静默重试」行为。
        """
        s = mk_bars([100., 0., 110., 120.], opens=[100., 0., 110., 120.])
        ok, r = self.no_raise(
            lambda: run_backtest(s, mk_sig([1, 1, 1, 1]), costs=ZERO, initial=10_000.0),
            "开盘价为 0 → 不抛异常")
        if ok:
            self.eq(r.equity_curve[1], 10_000.0, "第 1 根（开盘 0）没有成交，权益不变")
            self.true(r.open_position, "第 2 根开盘 110 补上了买入，期末持仓")
            self.near(r.equity_curve[-1], round(10_000.0 / 110 * 120, 2), 1e-2,
                      "成交价用第 2 根开盘 110（不是 0，也不是信号那根的收盘）")
            self.near(r.total_fees, 0.0, 1e-12, "零成本 → 无费用")

    def test_negative_open_price_also_skipped(self):
        """负开盘价同样被 px > 0 挡住；后面正常买入。"""
        s = mk_bars([100., -5., 110., 120.], opens=[100., -5., 110., 120.])
        ok, r = self.no_raise(
            lambda: run_backtest(s, mk_sig([1, 1, 1, 1]), costs=ZERO, initial=10_000.0),
            "开盘价为负 → 不抛异常")
        if ok:
            self.eq(r.equity_curve[1], 10_000.0, "负开盘价那根不成交")
            self.near(r.equity_curve[-1], round(10_000.0 / 110 * 120, 2), 1e-2,
                      "第 2 根开盘 110 成交")

    def test_short_is_not_supported_by_signal_contract(self):
        """allow_short 参数存在，但 SignalSet 只有 0/1，验证它不影响结果。"""
        s = mk_bars(RAMP)
        sig = mk_sig([1] * 10)
        r1 = run_backtest(s, sig, costs=ZERO, initial=10_000.0, allow_short=False)
        r2 = run_backtest(s, sig, costs=ZERO, initial=10_000.0, allow_short=True)
        self.near(r1.total_return, r2.total_return, 1e-12,
                  "allow_short 对 0/1 信号无影响（当前无做空实现）")


class TestSignalSet(TestCase):
    name = "strategy.SignalSet"

    def test_trades_count_and_current(self):
        sig = mk_sig([0, 1, 1, 0])
        self.eq(sig.trades_count(), 1, "入场标记 1 次")
        self.eq(sig.current(), 0, "末位是空仓")
        self.eq(mk_sig([0, 1]).current(), 1, "末位是持仓")

    def test_last_reason_skips_empty(self):
        sig = SignalSet("t", "l", "d", np.array([1, 1], np.int8),
                        np.zeros(2, bool), np.zeros(2, bool), ["", "有理由"], {})
        self.eq(sig.last_reason(), "有理由", "跳过空字符串取最后一个非空理由")
        empty = SignalSet("t", "l", "d", np.array([1], np.int8),
                          np.zeros(1, bool), np.zeros(1, bool), [""], {})
        self.eq(empty.last_reason(), "", "全空 → 返回空串（不是异常）")


REGISTRY.extend([TestCosts, TestMaxDrawdown, TestAnnualizeFactor,
                 TestNextBarOpenExecution, TestCostImpact, TestPerformanceMetrics,
                 TestTradeStatistics, TestInputGuards, TestSignalSet])

declare_counts({
    "TestCosts": {"test_slippage_direction": (2, 2), "test_fee_and_min_commission": (4, 4),
                  "test_market_defaults": (2, 2)},
    "TestMaxDrawdown": {"test_hand_computed": (2, 2),
                        "test_two_drawdowns_takes_worst": (2, 2),
                        "test_monotone_rise_no_drawdown": (2, 2),
                        "test_all_zero_equity_does_not_divide_by_zero": (2, 3)},
    "TestAnnualizeFactor": {"test_known_intervals": (5, 5),
                            "test_unknown_interval_falls_back_to_daily": (1, 1)},
    "TestNextBarOpenExecution": {"test_buy_fills_at_next_open": (3, 3),
                                 "test_next_open_differs_from_signal_bar_close": (2, 2),
                                 "test_sell_fills_at_next_open": (9, 9)},
    "TestCostImpact": {"test_fee_reduces_return": (2, 2),
                       "test_slippage_reduces_return": (3, 3),
                       "test_cost_drag_is_measured_not_estimated": (2, 2),
                       "test_zero_cost_has_zero_fees_and_zero_drag": (2, 2)},
    "TestPerformanceMetrics": {"test_flat_signal_keeps_equity_constant": (7, 7),
                               "test_exposure_hand_computed": (1, 1),
                               "test_volatility_annualization": (3, 3),
                               "test_annual_return_formula": (1, 1),
                               "test_benchmark_is_buy_and_hold_with_cost": (1, 1),
                               "test_sharpe_and_sortino_zero_when_no_volatility": (4, 4)},
    "TestTradeStatistics": {"test_two_round_trips": (14, 14),
                            "test_losing_trade_statistics": (7, 7),
                            "test_open_position_excluded_from_stats": (7, 7)},
    "TestInputGuards": {"test_too_few_bars_raises": (2, 2),
                        "test_three_bars_is_allowed": (1, 1),
                        "test_zero_open_price_skips_that_bar_then_buys_later": (4, 5),
                        "test_negative_open_price_also_skipped": (2, 3),
                        "test_short_is_not_supported_by_signal_contract": (1, 1)},
    "TestSignalSet": {"test_trades_count_and_current": (3, 3),
                      "test_last_reason_skips_empty": (2, 2)},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_backtest.py"))
