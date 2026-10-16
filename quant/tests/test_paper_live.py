#!/usr/bin/env python3
"""
paper_live.py（长驻模拟盘会话）验证 —— 完全不联网。

PaperSession 的行情入口 pull_market() 会真的去 fetch，所以这里不用它：
直接把价格序列与 bar_index 注入会话，再用 place_order(type="market") 触发
成交路径。这样验证的是**账户数学、校验、风控、序列化**，而不是网络。

覆盖：
  · 下单前置校验：side/type 非法、无行情、资金不足、无持仓、超卖、T+1
  · 成交数学：买入均价、卖出已实现盈亏、手续费、权益、回撤
  · 风控状态：清仓重置 peak_price / bars_held / bars_since_exit
  · 快照与 to_state/from_state 往返
  · 手数取整与拒绝路径

直接运行：python quant/tests/test_paper_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from datasource import Bar, Series                       # noqa: E402
from backtest import Costs                               # noqa: E402
from paper_live import (                                 # noqa: E402
    OrderState, PaperSession, Side, symbol_name,
)

ZERO = Costs(commission=0.0, slippage=0.0, min_commission=0.0)


def mk_series(prices, *, market="crypto", interval="1d") -> Series:
    bars = tuple(Bar(1_700_000_000_000 + i * 86_400_000, float(p), float(p) * 1.01,
                     float(p) * 0.99, float(p), 1000.0)
                 for i, p in enumerate(prices))
    return Series("inject", market, "TEST", "测试", interval, bars, 0.0)


def session_with_prices(prices, *, market="crypto", costs=ZERO,
                        initial=None, bar_index=None) -> PaperSession:
    """
    造一个「已经拉到行情」的会话，不联网：
    直接把 series / price_series / bar_index / source / bar_at 注入。

    ⚠️ 默认 bar_index 指向**最后一根**，所以会话的当前价就是最后一个价格。
    想在「价格 100 时买入、价格 150 时卖出」，必须显式
    `s.bar_index = 0` 再下单，然后用 advance(s, 1) 推进 —— 不能偷懒。
    """
    s = PaperSession(market=market, costs=costs,
                     initial=initial if initial is not None else 100_000.0)
    series = mk_series(prices, market=market)
    s.series = series
    s.price_series = [b.close for b in series.bars]
    s.bar_index = len(prices) - 1 if bar_index is None else bar_index
    s.source = series.source
    s.bar_at = series.bars[s.bar_index].as_dict()["iso"]
    return s


def advance(s: PaperSession, index: int) -> PaperSession:
    """把会话推进到指定下标（模拟「又拉到一根新 K 线」）。"""
    s.bar_index = index
    s.price_series = [b.close for b in s.series.bars[:index + 1]]
    s.bar_at = s.series.bars[index].as_dict()["iso"]
    return s


class TestOrderValidation(TestCase):
    name = "paper_live.下单校验"

    def test_invalid_side_and_type(self):
        s = session_with_prices([100.0, 100.0])
        self.raises(ValueError, lambda: s.place_order("longshort"),
                    "非法 side → ValueError")
        self.raises(ValueError, lambda: s.place_order("buy", order_type="limit"),
                    "非法 order_type → ValueError")

    def test_no_market_data(self):
        s = PaperSession(market="crypto", costs=ZERO, initial=100_000.0)
        self.raises(ValueError, lambda: s.place_order("buy"),
                    "还没行情 → ValueError（提示先刷新）")

    def test_buy_without_enough_cash(self):
        s = session_with_prices([100.0, 100.0], initial=50.0)
        o = s.place_order("buy")
        self.eq(o.state, OrderState.REJECTED.value, "资金不足 → 被拒")
        self.true("资金不足" in (o.reject_reason or ""), f"原因：{o.reject_reason}")

    def test_sell_without_position(self):
        s = session_with_prices([100.0, 100.0])
        o = s.place_order("sell")
        self.eq(o.state, OrderState.REJECTED.value, "无持仓 → 被拒")
        self.eq(o.reject_reason, "无持仓可卖", "原因明确")

    def test_sell_more_than_held(self):
        s = session_with_prices([100.0, 100.0], initial=100_000.0)
        s.place_order("buy", order_type="market")          # 买到 1000 股
        o = s.place_order("sell", qty=5000)
        self.eq(o.state, OrderState.REJECTED.value, "卖超过持仓 → 被拒")
        self.true("持仓不足" in (o.reject_reason or ""), f"原因：{o.reject_reason}")

    def test_t_plus_one_same_bar_sell(self):
        """A股：同一根 K 线内买入后立刻卖出 → 被 T+1 拦住。"""
        s = session_with_prices([10.0, 10.0], market="ashare", initial=1_000_000.0)
        buy = s.place_order("buy", order_type="market")
        self.eq(buy.state, OrderState.FILLED.value, "先买入成功")
        sell = s.place_order("sell", order_type="market")
        self.eq(sell.state, OrderState.REJECTED.value, "同一根内卖出被 T+1 拒绝")
        self.true("T+1" in (sell.reject_reason or ""), f"原因：{sell.reject_reason}")

    def test_crypto_has_no_t_plus_one(self):
        s = session_with_prices([100.0, 100.0], market="crypto", initial=100_000.0)
        s.place_order("buy", order_type="market")
        sell = s.place_order("sell", order_type="market")
        self.eq(sell.state, OrderState.FILLED.value, "加密没有 T+1，可以当日平仓")


class TestFillMath(TestCase):
    name = "paper_live.成交数学"

    def test_buy_math_hand_computed(self):
        """100000 元按 100 买入：qty = 1000（lot=1），现金 0，均价 100。"""
        s = session_with_prices([100.0, 100.0], initial=100_000.0)
        o = s.place_order("buy", order_type="market")
        self.eq(o.state, OrderState.FILLED.value, "成交")
        self.near(o.qty, 1000.0, 1e-9, "买入 1000 股")
        self.near(o.fill_price, 100.0, 1e-9, "成交价 = 当前参考价")
        self.near(s.cash, 0.0, 1e-9, "现金清零")
        self.near(s.qty, 1000.0, 1e-9, "持仓 1000")
        self.near(s.avg_price, 100.0, 1e-9, "均价 100")
        self.near(s.equity, 100_000.0, 1e-6, "权益不变（价格没动）")
        self.near(s.fees, 0.0, 1e-12, "零成本 → 无手续费")

    def test_buy_math_with_costs(self):
        c = Costs(commission=0.001, slippage=0.002)
        s = session_with_prices([100.0, 100.0], costs=c, initial=100_000.0)
        o = s.place_order("buy", order_type="market")
        px = 100.0 * 1.002
        qty = float(int(100_000.0 / (px * 1.001)))
        self.near(o.fill_price, px, 1e-9, "成交价 = 价 × (1+滑点) = 100.2")
        self.near(o.qty, qty, 1e-9, "数量按 (价×(1+费率)) 计算")
        self.near(o.fee, qty * px * 0.001, 1e-6, "手续费 = 成交额 × 0.1%")

    def test_sell_realized_pnl_hand_computed(self):
        """
        价格 100 时买入 1000 股 → 推进到价格 150 全部卖出：
        毛利 150000 - 100000 = 50000。
        """
        s = session_with_prices([100.0, 150.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", order_type="market")
        self.near(s.avg_price, 100.0, 1e-9, "在 100 成交（bar_index=0 那根）")
        advance(s, 1)
        sell = s.place_order("sell", order_type="market")
        self.eq(sell.state, OrderState.FILLED.value, "卖出成交")
        self.near(sell.fill_price, 150.0, 1e-9, "成交价 150")
        self.near(s.qty, 0.0, 1e-9, "已清仓")
        self.near(s.cash, 150_000.0, 1e-6, "现金 = 1000×150")
        self.near(s.realized, 50_000.0, 1e-6, "已实现盈亏 50000")
        self.near(s.equity, 150_000.0, 1e-6, "权益 150000")
        self.near(s.avg_price, 0.0, 1e-12, "清仓后均价归零")

    def test_average_price_on_two_buys(self):
        """
        两次买入的均价按金额加权：
        100 买 500 股（50000），150 买 333 股（49950）
        → 均价 = 99950 / 833 = 119.988
        """
        s = session_with_prices([100.0, 150.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", qty=500, order_type="market")
        advance(s, 1)
        s.place_order("buy", qty=333, order_type="market")
        expect = (500 * 100.0 + 333 * 150.0) / (500 + 333)
        self.near(s.qty, 833.0, 1e-9, "持仓 833 股")
        self.near(s.avg_price, expect, 1e-6,
                  f"加权均价 = {expect:.4f}（不是两次价格的简单平均 125）")

    def test_lot_size_rounding_on_ashare(self):
        """A股 100 股一手：下达 250 股 → 实际 200 股。"""
        s = session_with_prices([10.0, 10.0], market="ashare", initial=1_000_000.0)
        o = s.place_order("buy", qty=250, order_type="market")
        self.eq(o.state, OrderState.FILLED.value, "成交")
        self.near(o.qty, 200.0, 1e-9, "250 → 200（向下取整到手）")

    def test_full_position_buy_when_qty_none(self):
        s = session_with_prices([50.0, 50.0], market="crypto", initial=10_000.0)
        o = s.place_order("buy", order_type="market")
        self.near(o.qty, 200.0, 1e-9, "10000/50 = 200 股")
        self.near(s.cash, 0.0, 1e-9, "全仓买入后现金为 0")


class TestRiskStateReset(TestCase):
    name = "paper_live.风控状态复位"

    def test_clearing_resets_peak_and_counters(self):
        s = session_with_prices([100.0, 120.0, 110.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", order_type="market")
        self.near(s.peak_price, 100.0, 1e-9, "建仓时 peak_price = 成交价 100")
        self.eq(s.bars_held, 0, "建仓时 bars_held = 0")
        # 手动推进 peak_price，模拟持仓期创新高
        s.peak_price = 120.0
        s.bars_held = 5
        advance(s, 2)
        s.place_order("sell", order_type="market")
        self.near(s.peak_price, 0.0, 1e-12, "清仓后 peak_price 归零")
        self.eq(s.bars_held, 0, "清仓后 bars_held 归零")
        self.eq(s.bars_since_exit, 0, "清仓后 bars_since_exit 归零（冷却期从这里算）")

    def test_add_on_keeps_tracking(self):
        """加仓不算「首次建仓」，不能重置 peak_price / bars_held。"""
        s = session_with_prices([100.0, 110.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", qty=100, order_type="market")
        s.peak_price = 105.0
        s.bars_held = 3
        advance(s, 1)
        s.place_order("buy", qty=100, order_type="market")
        self.near(s.peak_price, 105.0, 1e-9, "加仓不重置 peak_price")
        self.eq(s.bars_held, 3, "加仓不重置 bars_held")

    def test_mark_updates_peak_and_drawdown(self):
        s = session_with_prices([100.0, 200.0, 100.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", order_type="market")
        advance(s, 1)
        s._mark()
        self.near(s.peak, 200_000.0, 1e-6, "峰值权益 200000")
        advance(s, 2)
        s._mark()
        self.near(s.max_dd, 50.0, 1e-6, "回撤 50%")
        self.near(s.equity, 100_000.0, 1e-6, "权益回到 100000")


class TestSnapshotAndState(TestCase):
    name = "paper_live.快照与持久化"

    def test_snapshot_keys_and_values(self):
        s = session_with_prices([100.0, 150.0], initial=100_000.0)
        advance(s, 0)
        s.place_order("buy", order_type="market")
        snap = s.snapshot()
        self.true(all(k in snap for k in ("equity", "cash", "qty", "avgPrice")),
                  "快照含账户核心字段")
        self.true("equityCurve" in snap and "prices" in snap, "快照含曲线数据")
        self.true("recentOrders" in snap and "recentFills" in snap,
                  "快照含委托与成交（键名是 recentOrders / recentFills）")
        self.near(snap["equity"], 100_000.0, 1e-6, "权益正确（100 买入、价格没动）")
        self.eq(len(snap["recentFills"]), 1, "有 1 笔成交记录")
        self.true(snap["inPosition"], "标记为持仓中")
        self.eq(snap["tPlus1"], False, "加密会话 tPlus1 = False")
        self.eq(snap["lot"], 1, "加密手数 = 1")

    def test_state_round_trip(self):
        """
        to_state → from_state 必须保住账户与过程数据。
        series 本身不序列化（太大），所以 from_state 后 self.series 是 None ——
        这也是为什么 source / bar_at 要单独存字段。
        """
        s = session_with_prices([100.0, 150.0], initial=100_000.0)
        s.place_order("buy", order_type="market")
        s.auto = True
        s.auto_mode = "score"
        s.bars_held = 7
        st = s.to_state()
        back = PaperSession.from_state(st)
        self.eq(back.id, s.id, "会话 id 保留")
        self.near(back.cash, s.cash, 1e-9, "现金保留")
        self.near(back.qty, s.qty, 1e-9, "持仓保留")
        self.near(back.avg_price, s.avg_price, 1e-9, "均价保留")
        self.eq(len(back.fills), len(s.fills), "成交记录保留")
        self.eq(len(back.orders), len(s.orders), "委托记录保留")
        self.eq(back.auto, True, "自动交易开关保留")
        self.eq(back.bars_held, 7, "持仓根数保留")
        self.eq(back.source, s.source, "数据来源保留（否则重启后页头显示「源 —」）")
        self.eq(back.bar_at, s.bar_at, "bar 时间保留")
        self.true(back.series is None, "series 本身不序列化（预期行为）")

    def test_state_is_json_serializable(self):
        import json
        s = session_with_prices([100.0, 150.0], initial=100_000.0)
        s.place_order("buy", order_type="market")
        ok, text = self.no_raise(
            lambda: json.dumps(s.to_state(), ensure_ascii=False),
            "to_state 可 JSON 序列化（strict 模式）")
        self.true(ok, "序列化通过")
        if ok:
            self.true(len(text) > 100, f"状态体积 {len(text)} 字符")

    def test_auto_config_round_trip(self):
        """auto_config 的嵌套 RiskConfig 也必须能恢复。"""
        s = session_with_prices([100.0], initial=100_000.0)
        s.auto_config.max_fraction = 0.6
        s.auto_config.risk.stop_loss = 0.03
        back = PaperSession.from_state(s.to_state())
        self.near(back.auto_config.max_fraction, 0.6, 1e-12, "max_fraction 恢复")
        self.near(back.auto_config.risk.stop_loss, 0.03, 1e-12,
                  "嵌套的 risk.stop_loss 恢复")

    def test_symbol_name_falls_back_to_code(self):
        """查不到名字时原样返回代码，不编造。"""
        self.eq(symbol_name("crypto", "ZZZUSDT"), "ZZZUSDT",
                "未知加密标的 → 返回代码本身")
        name = symbol_name("ashare", "sh600519")
        self.true(isinstance(name, str) and len(name) > 0, f"已知标的有名字：{name}")


REGISTRY.extend([TestOrderValidation, TestFillMath, TestRiskStateReset,
                 TestSnapshotAndState])

declare_counts({
    "TestOrderValidation": {"test_invalid_side_and_type": (2, 2),
                            "test_no_market_data": (1, 1),
                            "test_buy_without_enough_cash": (2, 2),
                            "test_sell_without_position": (2, 2),
                            "test_sell_more_than_held": (2, 2),
                            "test_t_plus_one_same_bar_sell": (3, 3),
                            "test_crypto_has_no_t_plus_one": (1, 1)},
    "TestFillMath": {"test_buy_math_hand_computed": (8, 8),
                     "test_buy_math_with_costs": (3, 3),
                     "test_sell_realized_pnl_hand_computed": (8, 8),
                     "test_average_price_on_two_buys": (2, 2),
                     "test_lot_size_rounding_on_ashare": (2, 2),
                     "test_full_position_buy_when_qty_none": (2, 2)},
    "TestRiskStateReset": {"test_clearing_resets_peak_and_counters": (5, 5),
                           "test_add_on_keeps_tracking": (2, 2),
                           "test_mark_updates_peak_and_drawdown": (3, 3)},
    "TestSnapshotAndState": {"test_snapshot_keys_and_values": (8, 8),
                             "test_state_round_trip": (11, 11),
                             "test_state_is_json_serializable": (2, 3),
                             "test_auto_config_round_trip": (2, 2),
                             "test_symbol_name_falls_back_to_code": (2, 2)},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_paper_live.py"))
