#!/usr/bin/env python3
"""
模拟盘（paper.py）验证 —— 逐根推进的状态机。

重点：
  1. 成交时点：第 i 根收盘的信号 → 第 i+1 根**开盘**成交
  2. T+1 必须按**成交日**判断（不是信号产生日）
  3. 涨跌停一字板买不进 / 卖不出
  4. 手数取整（A股 100 股一手）
  5. 权益盯市、最大回撤、已实现盈亏统计

这部分的每一条几乎都能手工算出来，所以期望值都是手算的。

直接运行：python quant/tests/test_paper.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from datasource import Bar, Series                       # noqa: E402
from strategy import SignalSet                           # noqa: E402
from backtest import Costs                               # noqa: E402
from paper import (                                      # noqa: E402
    OrderState, PaperTrader, Side, default_initial,
)

ZERO = Costs(commission=0.0, slippage=0.0, min_commission=0.0)


def mk(prices, *, opens=None, market="crypto") -> Series:
    bars = []
    for i, p in enumerate(prices):
        o = opens[i] if opens is not None else p
        bars.append(Bar(1_700_000_000_000 + i * 86_400_000, float(o),
                        float(max(o, p)), float(min(o, p)), float(p), 1.0))
    return Series("test", market, "TEST", "测试", "1d", tuple(bars), 0.0)


def sig_pos(positions) -> SignalSet:
    """只给 position 序列，entry/exit 由「仓位变化」自动推出。"""
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


def trader(series, position, *, costs=ZERO, initial=100_000.0, **kw) -> PaperTrader:
    return PaperTrader(series, sig_pos(position), costs=costs, initial=initial, **kw)


class TestDefaultInitial(TestCase):
    name = "paper.default_initial"

    def test_ashare_needs_more_money(self):
        """
        A 股必须整手：贵州茅台一手就要 13 万，给 10 万会导致所有委托被拒。
        所以 A 股默认 100 万，加密默认 10 万。
        """
        self.near(default_initial("ashare"), 1_000_000.0, 1e-9, "A股默认 100 万")
        self.near(default_initial("crypto"), 100_000.0, 1e-9, "加密默认 10 万")
        self.true(default_initial("ashare") > default_initial("crypto"),
                  "A股默认资金更多（整手约束）")


class TestExecutionTiming(TestCase):
    name = "paper.成交时点"

    def test_signal_on_bar_i_fills_on_bar_i_plus_1_open(self):
        """
        开盘价 100→110→120→130→140，信号全程持仓：
        第 0 根收盘看到信号 → 第 1 根开盘 110 成交。
        初始资金取 110000 = 110 × 1000，让数量正好是整数 1000，
        避免「数量截断」这个无关因素干扰时点验证（该行为另有专门用例）。
        """
        s = mk([100., 110., 120., 130., 140.], opens=[100., 110., 120., 130., 140.])
        pt = trader(s, [1] * 5, initial=110_000.0).run_all()
        self.eq(len(pt.fills), 1, "只成交一次（不会每根都买）")
        f = pt.fills[0]
        self.near(f.price, 110.0, 1e-9, "成交价 = 第 1 根开盘价 110（不是第 0 根收盘 100）")
        self.eq(f.index, 1, "成交发生在 index 1")
        self.near(f.qty, 1000.0, 1e-9, "全仓买入 110000/110 = 1000 股")
        self.near(pt.cash, 0.0, 1e-9, "零成本下现金全部投出去")

    def test_equity_is_marked_at_close(self):
        s = mk([100., 110., 120., 130., 140.], opens=[100., 110., 120., 130., 140.])
        pt = trader(s, [1] * 5, initial=110_000.0).run_all()
        curve = [p.equity for p in pt.equity_curve]
        self.eq(len(curve), 5, "权益曲线长度 = K 线数")
        self.near(curve[0], 110_000.0, 1e-9, "第 0 根还没成交 → 权益 = 初始资金")
        self.near(curve[1], 110_000.0, 1e-6, "第 1 根按收盘 110 盯市 = 110000")
        # 第 4 根：收盘 140 → 权益 = 1000 × 140 = 140000
        self.near(curve[4], 140_000.0, 1e-6, "末根权益 = 1000×140 = 140000")

    def test_no_signal_no_trades(self):
        s = mk([100., 110., 120.])
        pt = trader(s, [0, 0, 0]).run_all()
        self.eq(len(pt.fills), 0, "无成交")
        self.near(pt.snapshot().equity, 100_000.0, 1e-9, "权益始终等于初始")
        self.near(pt.snapshot().total_return, 0.0, 1e-9, "收益 0")

    def test_run_all_returns_self_and_steps_exactly_n_times(self):
        s = mk([100., 110., 120., 130.])
        pt = trader(s, [1] * 4)
        self.eq(pt.run_all(), pt, "run_all 返回自身（可链式调用）")
        self.eq(len(pt.equity_curve), 4, "推进次数 = K 线数")
        self.false(pt.step(), "到底之后再 step() 返回 False")

    def test_signal_not_acted_on_last_bar(self):
        """
        最后一根收盘产生的信号没有「下一根开盘」可以成交 ——
        所以第 n-1 根不会提交委托（i < n-1 判断）。
        """
        s = mk([100., 110., 120.])
        # 信号在第 2 根（最后一根）才转多 → 不可能成交
        pt = trader(s, [0, 0, 1]).run_all()
        self.eq(len(pt.fills), 0, "末根信号无法成交（没有下一根）")
        self.eq(len(pt.orders), 0, "末根也不会提交委托")


class TestCostsAndFees(TestCase):
    name = "paper.成本"

    def test_buy_price_and_fee_hand_computed(self):
        """
        验证「买入价 = 开盘×(1+滑点)」「数量 = 资金/(价×(1+费率))」
        「手续费 = 成交额×费率」三条公式，以及现金扣减。

        ⚠️ 数量口径（2026-10-16 修正）：
        `mk()` 默认 market="crypto"，lot_size=1，**允许小数**。
        原实现用 `math.floor` 一律截断成整数 —— 201000 元按 200.4 买入
        只成交 1001 股，白留约 199 元现金；注释里却写着「加密可小数」。
        现在是真实可成交数量（保留 8 位小数），所以期望值不再是整数。
        A 股（lot_size=100）仍必须整百股，见 test_ashare_lot_still_aligns_to_hundred。
        """
        c = Costs(commission=0.001, slippage=0.002)
        s = mk([100., 200.], opens=[100., 200.])
        px = 200.0 * 1.002                     # 买入抬价 → 200.4
        initial = 201_000.0
        raw_qty = initial / (px * 1.001)
        expect_qty = math.floor(raw_qty * 1e8) / 1e8   # lot=1 → 8 位小数
        pt = trader(s, [1, 1], costs=c, initial=initial).run_all()
        f = pt.fills[0]
        self.near(f.price, px, 1e-9, "买入价 = 开盘 × (1+滑点) = 200.4")
        self.near(f.qty, expect_qty, 1e-9,
                  f"数量 = 资金/(价×(1+费率)) = {expect_qty:.6f}（加密可小数）")
        self.true(f.qty > 1001.0, f"比旧的截断值 1001 大（{f.qty:.6f}）")
        self.near(f.fee, expect_qty * px * 0.001, 1e-6, "手续费 = 成交额 × 0.1%")
        self.near(pt.cash, initial - expect_qty * px - f.fee, 1e-6,
                  "现金 = 初始 − 成交额 − 手续费")
        self.near(pt.snapshot().total_fees, round(f.fee, 2), 1e-6, "累计手续费只算这一次")
        # 修正后剩余现金应远小于 1%：几乎全部资金都投进去了。
        # 旧实现留了约 199 元（0.099%），修正后只剩零头。
        self.true(pt.cash < initial * 0.0001,
                  f"剩余现金不到初始资金的 0.01%（实际 {pt.cash:.6f} 元）")

    def test_sell_price_direction(self):
        c = Costs(commission=0.0, slippage=0.01)
        # 需要 6 根：卖出信号必须在第 n-2 根之前出现，第 n-1 根才能成交
        s = mk([100.] * 6, opens=[100.] * 6)
        pt = trader(s, [1, 1, 1, 1, 0, 0], costs=c, initial=100_000.0).run_all()
        buys = [f for f in pt.fills if f.side == "buy"]
        sells = [f for f in pt.fills if f.side == "sell"]
        self.eq(len(buys), 1, "一次买入")
        self.eq(len(sells), 1, "一次卖出（末根信号不会成交，所以要多给一根）")
        self.near(buys[0].price, 101.0, 1e-9, "买入价上滑 1% → 101")
        self.near(sells[0].price, 99.0, 1e-9, "卖出价下滑 1% → 99")

    def test_cost_reduces_equity(self):
        s = mk([100., 110., 120., 130., 140.])
        free = trader(s, [1] * 5, costs=ZERO).run_all()
        cost = trader(s, [1] * 5, costs=Costs(0.001, 0.001)).run_all()
        self.true(cost.snapshot().equity < free.snapshot().equity,
                  f"有成本权益更低（{cost.snapshot().equity} < {free.snapshot().equity}）")


class TestTPlusOne(TestCase):
    name = "paper.T+1（按成交日判断）"

    def test_buy_then_same_bar_sell_is_rejected(self):
        s = mk([100., 100., 100., 100., 100.], market="ashare")
        pt = PaperTrader(s, sig_pos([1] * 5), costs=ZERO, initial=100_000.0)
        pt.step()          # index 0
        pt.step()          # index 1 → 买入成交
        self.eq(pt._last_buy_index, 1, "买入成交日记为 index 1（成交日，不是信号日）")

        pt._submit(Side.SELL, 1, "测试当日卖出")
        manual = pt.orders[-1]
        pt._try_fill(manual, 1)
        self.eq(manual.state, OrderState.REJECTED, "当日卖出被拒")
        self.true("T+1" in (manual.reject_reason or ""),
                  f"拒绝原因提到 T+1：{manual.reject_reason}")

    def test_next_day_sell_is_allowed(self):
        s = mk([100., 100., 100.], market="ashare")
        pt = PaperTrader(s, sig_pos([1] * 3), costs=ZERO, initial=100_000.0)
        pt.step()
        pt.step()
        pt.step()
        pt._submit(Side.SELL, 2, "隔日卖出")
        manual = pt.orders[-1]
        pt._try_fill(manual, 2)
        self.eq(manual.state, OrderState.FILLED, "隔日（index 2 > 1）可以卖出")

    def test_t_plus_one_disabled_allows_same_bar(self):
        s = mk([100., 100., 100.], market="ashare")
        pt = PaperTrader(s, sig_pos([1] * 3), costs=ZERO, initial=100_000.0,
                         t_plus_1=False)
        pt.step()
        pt.step()
        pt._submit(Side.SELL, 1, "当日卖出（T+1 关闭）")
        manual = pt.orders[-1]
        pt._try_fill(manual, 1)
        self.eq(manual.state, OrderState.FILLED, "关闭 T+1 后当日卖出放行")

    def test_crypto_defaults_to_no_t_plus_one(self):
        s = mk([100., 100.], market="crypto")
        pt = PaperTrader(s, sig_pos([1, 1]), ZERO, initial=10_000.0)
        self.false(pt.t_plus_1, "加密默认不开 T+1")
        self.false(pt.price_limit, "加密默认不校验涨跌停")
        self.eq(pt.lot_size, 1, "加密可以买小数股")


class TestPriceLimit(TestCase):
    name = "paper.涨跌停校验"

    def test_one_word_limit_up_blocks_buy(self):
        """一字涨停：当日 low 已经 >= 昨收×1.10 → 买不进。"""
        s = mk([100.0, 120.0], opens=[100.0, 115.0], market="ashare")
        # 手工把第 1 根的 low 顶到涨停价（110）以上
        bars = list(s.bars)
        bars[1] = Bar(bars[1].ts, 115.0, 120.0, 110.0, 120.0, 1.0)
        s2 = Series("test", "ashare", "TEST", "测试", "1d", tuple(bars), 0.0)
        pt = PaperTrader(s2, sig_pos([1, 1]), costs=ZERO, initial=100_000.0)
        pt.step()
        pt.step()
        self.eq(len(pt.fills), 0, "一字涨停买不进（没有成交）")
        rejects = [o for o in pt.orders if o.state == OrderState.REJECTED]
        self.true(len(rejects) == 1, f"有一张被拒委托（{len(rejects)}）")
        self.true("涨停" in (rejects[0].reject_reason or ""),
                  f"原因说明涨停：{rejects[0].reject_reason}")

    def test_one_word_limit_down_blocks_sell(self):
        """一字跌停：当日 high <= 昨收×0.90 → 卖不出。"""
        bars = (
            Bar(1_700_000_000_000, 100.0, 100.0, 100.0, 100.0, 1.0),
            Bar(1_700_086_400_000, 100.0, 100.0, 100.0, 100.0, 1.0),
            Bar(1_700_172_800_000, 90.0, 90.0, 80.0, 80.0, 1.0),
        )
        s = Series("test", "ashare", "TEST", "测试", "1d", bars, 0.0)
        pt = PaperTrader(s, sig_pos([1, 1, 0]), costs=ZERO, initial=100_000.0)
        pt.run_all()
        sells = [f for f in pt.fills if f.side == "sell"]
        self.eq(len(sells), 0, "一字跌停卖不出去")
        self.true(pt.snapshot().in_position, "持仓仍然保留（卖单被拒）")


class TestLotSize(TestCase):
    name = "paper.手数取整"

    def test_ashare_rounds_down_to_100(self):
        """1050 元买 10 元的股票：最多 100 股（一手），余 50 元。"""
        s = mk([10., 10., 10.], market="ashare")
        pt = PaperTrader(s, sig_pos([1, 1, 1]), costs=ZERO, initial=1050.0)
        pt.run_all()
        qty = pt.snapshot().position_qty
        self.near(qty, 100.0, 1e-9, "买 100 股（向下取整到手）")
        self.near(pt.cash, 50.0, 1e-9, "剩余现金 50")
        self.eq(int(qty) % 100, 0, "持仓量是 100 的整数倍")

    def test_insufficient_cash_is_rejected(self):
        """50 元买 10 元股票：一手要 1000 元 → 资金不足，明确拒绝。"""
        s = mk([10., 10.], market="ashare")
        pt = PaperTrader(s, sig_pos([1, 1]), costs=ZERO, initial=50.0)
        pt.run_all()
        self.eq(len(pt.fills), 0, "没有成交")
        rejects = [o for o in pt.orders if o.state == OrderState.REJECTED]
        self.true(len(rejects) == 1, "有一张被拒委托")
        self.true("资金不足" in (rejects[0].reject_reason or ""),
                  f"原因说明资金不足：{rejects[0].reject_reason}")

    def test_crypto_lot_is_one(self):
        s = mk([333.33, 333.33], market="crypto")
        pt = PaperTrader(s, sig_pos([1, 1]), costs=ZERO, initial=1_000_000.0)
        pt.run_all()
        self.eq(pt.lot_size, 1, "加密的 lot_size 是 1（注释说可以买小数股）")
        self.true(pt.snapshot().position_qty > 1.0,
                  f"能买到上千股（{pt.snapshot().position_qty}）")

    def test_lot_one_allows_fractional_units(self):
        """
        修正后（2026-10-16）：`lot_size=1` 时**允许小数**，与注释一致。

        原实现的对抗性发现：`qty = math.floor(raw_qty / lot_size) * lot_size`
        对 lot_size=1 也执行取整 —— math.floor 返回 int，所以「加密可以小数」
        在 paper.py 里并未实现，100000 元按 110 买入只成交 909 股，
        白留 10 元现金。

        现在用 `_round_to_lot()`：lot<=1 保留 8 位小数，lot>1 仍按手对齐。
        这条用例断言**修正后**的正确行为（原用例断言的是缺陷本身）。
        """
        import math as _math
        s = mk([110., 110.], market="crypto")
        pt = PaperTrader(s, sig_pos([1, 1]), costs=ZERO, initial=100_000.0)
        pt.run_all()
        qty = pt.fills[0].qty
        self.eq(pt.lot_size, 1, "加密 lot_size = 1")
        expect = _math.floor(100_000.0 / 110.0 * 1e8) / 1e8
        self.near(qty, expect, 1e-9,
                  f"数量是真实可成交值 {expect:.8f}，不是截断的 909")
        self.true(qty > 909.0, f"比截断值大（{qty:.8f} > 909）")
        self.true(isinstance(qty, float), f"类型是 float：{type(qty).__name__}")
        # 现金应该基本用完（不足以再买最小单位）
        self.true(pt.cash < 110.0, f"剩余现金不足最小单位（{pt.cash:.6f} 元）")
        self.true(pt.cash < 1.0, f"修正后剩余远小于 1 元（实际 {pt.cash:.6f}）")

    def test_ashare_lot_still_aligns_to_hundred(self):
        """A 股必须整百股 —— 修正加密的小数行为后，A 股规则不能被带偏。"""
        s = mk([100., 100.], market="ashare")
        pt = PaperTrader(s, sig_pos([1, 1]), costs=ZERO, initial=10_050.0)
        pt.run_all()
        qty = pt.fills[0].qty
        self.eq(pt.lot_size, 100, "A 股 lot_size = 100")
        self.near(qty % 100, 0.0, 1e-9, f"A 股数量必须是 100 的整数倍（实际 {qty}）")
        self.eq(qty, 100.0, "10050 元只能买 100 股（整百）")


class TestSellAndPnL(TestCase):
    name = "paper.卖出与盈亏"

    def test_round_trip_pnl_hand_computed(self):
        """
        200 买入（第 1 根开盘）→ 150 卖出（第 3 根开盘）。
        初始资金 200000 → qty = 1000 股，买入花光现金；
        卖出得到 1000×150 = 150000 → 权益 150000，亏损 50000。
        """
        s = mk([100., 200., 300., 150., 150.], opens=[100., 200., 300., 150., 150.])
        pt = trader(s, [1, 1, 1, 0, 0], initial=200_000.0).run_all()
        snap = pt.snapshot()
        self.eq(len([f for f in pt.fills if f.side == "buy"]), 1, "一次买入")
        self.eq(len([f for f in pt.fills if f.side == "sell"]), 1, "一次卖出")
        self.near(pt.fills[0].price, 200.0, 1e-9, "买入价 200")
        self.near(pt.fills[0].qty, 1000.0, 1e-9, "买 1000 股")
        self.near(pt.fills[1].price, 150.0, 1e-9, "卖出价 150")
        self.near(snap.equity, 150_000.0, 1e-6, "期末权益 150000")
        self.near(snap.realized_pnl, -50_000.0, 1e-6, "已实现盈亏 -50000")
        self.near(snap.total_return, -25.0, 1e-6, "总收益 -25%")
        self.eq(snap.trade_count, 1, "1 笔完成交易")
        self.eq(snap.loss_count, 1, "1 笔亏损")
        self.near(snap.win_rate, 0.0, 1e-9, "胜率 0%")
        self.false(snap.in_position, "已清仓")

    def test_sell_without_position_is_rejected(self):
        s = mk([100., 100., 100.])
        pt = PaperTrader(s, sig_pos([0, 0, 0]), costs=ZERO, initial=100_000.0)
        pt.step()
        pt.step()
        pt._submit(Side.SELL, 1, "无持仓卖出")
        manual = pt.orders[-1]
        pt._try_fill(manual, 1)
        self.eq(manual.state, OrderState.REJECTED, "无持仓卖出被拒")
        self.eq(manual.reject_reason, "无持仓", "原因明确")

    def test_win_stats(self):
        """两笔都赚：胜率 100%、win_count=2。价格用 100→200→400 与 400→800。"""
        s = mk([100., 100., 200., 200., 400., 400., 800., 800.])
        pt = trader(s, [1, 1, 0, 1, 1, 0, 0, 0]).run_all()
        snap = pt.snapshot()
        self.eq(snap.trade_count, 2, "2 笔完成交易（每笔都要留出「下一根开盘」）")
        self.eq(snap.win_count, 2, "2 笔盈利")
        self.near(snap.win_rate, 100.0, 1e-9, "胜率 100%")
        self.true(snap.realized_pnl > 0, f"已实现盈亏为正（{snap.realized_pnl}）")

    def test_unrealized_and_realized_are_separate(self):
        s = mk([100., 100., 200.])
        pt = trader(s, [1, 1, 1]).run_all()
        snap = pt.snapshot()
        self.eq(snap.trade_count, 0, "没有完成交易")
        self.near(snap.realized_pnl, 0.0, 1e-9, "已实现盈亏 0")
        self.true(snap.unrealized_pnl > 0, f"浮盈为正（{snap.unrealized_pnl}）")
        self.true(snap.in_position, "持仓中")


class TestDrawdownAndPeak(TestCase):
    name = "paper.回撤与峰值"

    def test_max_drawdown_hand_computed(self):
        """
        全程持仓，价格 100 → 200 → 100。
        初始资金 110 → 第 1 根开盘 200 成交 0 股？不行；用 200000 初始资金
        配合第 1 根开盘 200 得到 1000 股，第 1 根收盘 200 时权益才等于初始，
        所以取价格 100 → 300 → 100 与初始 300000 → 1000 股。
        """
        s = mk([100., 300., 100.], opens=[100., 300., 100.])
        pt = trader(s, [1, 1, 1], initial=300_000.0).run_all()
        snap = pt.snapshot()
        self.near(pt.fills[0].qty, 1000.0, 1e-9, "1000 股（300000/300）")
        self.near(snap.peak_equity, 300_000.0, 1e-6, "峰值权益 = 1000×300 = 300000")
        self.near(snap.equity, 100_000.0, 1e-6, "末根权益 = 1000×100 = 100000")
        self.near(snap.max_drawdown, 66.6667, 1e-3, "最大回撤 = (300000-100000)/300000")

    def test_no_drawdown_when_flat(self):
        s = mk([100., 50., 10.])
        pt = trader(s, [0, 0, 0]).run_all()
        snap = pt.snapshot()
        self.near(snap.max_drawdown, 0.0, 1e-9, "空仓无回撤")
        self.near(snap.peak_equity, 100_000.0, 1e-9, "峰值就是初始资金")

    def test_peak_not_reset_by_later_recovery(self):
        """先创新高再回落再创新高：峰值取历史最高。"""
        s = mk([100., 400., 200., 800.], opens=[100., 400., 200., 800.])
        pt = trader(s, [1, 1, 1, 1], initial=400_000.0).run_all()
        snap = pt.snapshot()
        self.near(pt.fills[0].qty, 1000.0, 1e-9, "1000 股（400000/400）")
        self.near(snap.peak_equity, 800_000.0, 1e-6, "峰值 = 1000×800（历史最高）")


class TestOrderHandling(TestCase):
    name = "paper.委托处理"

    def test_pending_order_is_retried_until_filled(self):
        """
        一字涨停被拒之后：持仓仍为 0，信号仍是看多 →
        下一根会重新提交委托（而不是永久放弃）。
        """
        bars = (
            Bar(1_700_000_000_000, 100.0, 100.0, 100.0, 100.0, 1.0),
            Bar(1_700_086_400_000, 115.0, 121.0, 110.0, 121.0, 1.0),   # 一字涨停
            Bar(1_700_172_800_000, 120.0, 125.0, 118.0, 122.0, 1.0),   # 正常
        )
        s = Series("test", "ashare", "TEST", "测试", "1d", bars, 0.0)
        pt = PaperTrader(s, sig_pos([1, 1, 1]), costs=ZERO, initial=100_000.0)
        pt.run_all()
        self.eq(len(pt.fills), 1, "第 1 根买不进，第 2 根买到了")
        self.near(pt.fills[0].price, 120.0, 1e-9, "成交价 = 第 2 根开盘 120")

    def test_orders_are_not_submitted_twice_while_holding(self):
        s = mk([100., 110., 120., 130.])
        pt = trader(s, [1, 1, 1, 1]).run_all()
        self.eq(len(pt.orders), 1, "一路持仓只提交 1 张委托")

    def test_order_ids_are_unique_and_increasing(self):
        s = mk([100., 110., 100., 110.])
        pt = trader(s, [1, 1, 0, 0]).run_all()
        ids = [o.id for o in pt.orders]
        self.eq(len(ids), len(set(ids)), "委托 ID 唯一")
        self.eq(ids, sorted(ids), "委托 ID 递增")

    def test_snapshot_exposes_process_data(self):
        s = mk([100., 110., 100.])
        pt = trader(s, [1, 1, 0]).run_all()
        snap = pt.snapshot()
        self.true(len(snap.equity_curve) == 3, "权益曲线进了快照（逐帧渲染用）")
        self.true(len(snap.fills) >= 1, "成交进了快照")
        self.true(len(snap.events) >= 1, "事件日志进了快照")
        self.true(isinstance(snap.pending, list), "挂单列表存在")
        summary = snap.summary()
        self.true("equity_curve" not in summary, "summary() 剔除大字段")
        self.true("fills" not in summary, "summary() 剔除 fills")


REGISTRY.extend([TestDefaultInitial, TestExecutionTiming, TestCostsAndFees,
                 TestTPlusOne, TestPriceLimit, TestLotSize, TestSellAndPnL,
                 TestDrawdownAndPeak, TestOrderHandling])

declare_counts({
    "TestDefaultInitial": {"test_ashare_needs_more_money": (3, 3)},
    "TestExecutionTiming": {
        "test_signal_on_bar_i_fills_on_bar_i_plus_1_open": (5, 5),
        "test_equity_is_marked_at_close": (4, 4),
        "test_no_signal_no_trades": (3, 3),
        "test_run_all_returns_self_and_steps_exactly_n_times": (3, 3),
        "test_signal_not_acted_on_last_bar": (2, 2)},
    "TestCostsAndFees": {"test_buy_price_and_fee_hand_computed": (7, 7),
                         "test_sell_price_direction": (4, 4),
                         "test_cost_reduces_equity": (1, 1)},
    "TestTPlusOne": {"test_buy_then_same_bar_sell_is_rejected": (3, 3),
                     "test_next_day_sell_is_allowed": (1, 1),
                     "test_t_plus_one_disabled_allows_same_bar": (1, 1),
                     "test_crypto_defaults_to_no_t_plus_one": (3, 3)},
    "TestPriceLimit": {"test_one_word_limit_up_blocks_buy": (3, 3),
                       "test_one_word_limit_down_blocks_sell": (2, 2)},
    "TestLotSize": {"test_ashare_rounds_down_to_100": (3, 3),
                    "test_insufficient_cash_is_rejected": (3, 3),
                    "test_crypto_lot_is_one": (2, 2),
                    # 2026-10-16：用例改名并改断言 —— 原断言的是缺陷本身
                    # （「数量被截断成整数 909」），修正后加密允许小数。
                    "test_lot_one_allows_fractional_units": (6, 6),
                    "test_ashare_lot_still_aligns_to_hundred": (3, 3)},
    "TestSellAndPnL": {"test_round_trip_pnl_hand_computed": (12, 12),
                       "test_sell_without_position_is_rejected": (2, 2),
                       "test_win_stats": (4, 4),
                       "test_unrealized_and_realized_are_separate": (4, 4)},
    "TestDrawdownAndPeak": {"test_max_drawdown_hand_computed": (4, 4),
                            "test_no_drawdown_when_flat": (2, 2),
                            "test_peak_not_reset_by_later_recovery": (2, 2)},
    "TestOrderHandling": {"test_pending_order_is_retried_until_filled": (2, 2),
                          "test_orders_are_not_submitted_twice_while_holding": (1, 1),
                          "test_order_ids_are_unique_and_increasing": (2, 2),
                          "test_snapshot_exposes_process_data": (6, 6)},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_paper.py"))
