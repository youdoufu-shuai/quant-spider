#!/usr/bin/env python3
"""
模拟盘（纸上交易）—— 用真实行情驱动，但**不下任何真实订单**。

═══════════════════════════════════════════════════════════════════
  与 backtest.py 的分工
═══════════════════════════════════════════════════════════════════
  backtest.py  一次性跑完历史，算最终统计量（收益/回撤/夏普…）
  paper.py     **逐根推进**的状态机，每来一根新 K 线就推进一步，
               持仓、权益、挂单、成交、盈亏都是**逐步累积**的，
               所以它能实时展示「账户现在长什么样」。

这正是「实时展现量化过程」需要的形态：不是事后算总账，
而是把每一步的决策、成交、权益变化都暴露出来，供前端逐帧渲染。

═══════════════════════════════════════════════════════════════════
  严格禁止未来函数（同 backtest.py）
═══════════════════════════════════════════════════════════════════
第 i 根收盘产生的信号 -> 第 i+1 根**开盘**成交。

═══════════════════════════════════════════════════════════════════
  ⚠️ 安全边界：这是模拟盘，不是实盘
═══════════════════════════════════════════════════════════════════
本模块：
  - 只读行情，不连接任何交易接口
  - 不持有任何 API Key，没有下单能力
  - 不产生任何真实委托

所有成交都发生在内存里，是「如果当时这样做会怎样」的推演。

★ 特别说明 A 股：A 股是 T+1 交易（当日买入次日才能卖），
  且有涨跌停限制。本模块实现了 T+1 与涨跌停的可选校验，
  默认对 ashare 开启 —— 否则回测出来的收益是假的。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, asdict
from enum import Enum

import numpy as np

from datasource import Series, fetch, MARKETS, Bar
from indicators import compute, IndicatorSet
from strategy import SignalSet, run as run_strategy, STRATEGIES, run_all
from backtest import Costs, MARKET_COSTS

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _round_to_lot(qty: float, lot_size: int) -> float:
    """
    把数量对齐到「手」的整数倍。

    ⚠️ 手数为 1 时（加密）应当**允许小数** —— 这是类注释里承诺过的行为。
    用 math.floor 会一律截断成整数：100000 元按 110 买入只成交 909 股，
    白留 10 元现金不用。对 BTC 这种高价标的误差不大，但语义是错的。
    （verifier 发现：注释说"可小数"，实现却截断一切。）

    A 股 lot_size=100，仍然必须是 100 的整数倍（真实规则）。
    """
    if lot_size <= 1:
        # 保留 8 位小数：足够表达 0.00000001 BTC 级别的仓位，又不会浮点噪声
        return math.floor(float(qty) * 1e8) / 1e8
    return float(math.floor(float(qty) / lot_size) * lot_size)


def default_initial(market: str) -> float:
    """
    各市场的合理默认初始资金。

    为什么 A 股要更多：A 股必须**整手交易**（1 手 = 100 股）。
    贵州茅台约 1329 元/股，一手就要 13.3 万元。
    初始资金给 10 万的话，所有委托都会因「资金不足」被拒 ——
    面板上会显示收益恒为 0，看起来像策略没信号，实际是**买不起一手**。

    这是 A 股的真实约束，不是 bug。所以默认资金按「至少能买几手
    高价股」来给：100 万可以买 7 手茅台，够走完一轮回撤。
    """
    return 1_000_000.0 if market == "ashare" else 100_000.0


# ============================================================
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderState(str, Enum):
    PENDING = "pending"      # 已提交，等待下一根开盘
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass
class Order:
    """一张委托。信号产生时创建，下一根开盘时尝试成交。"""
    id: int
    side: Side
    created_index: int
    created_time: str
    reason: str
    qty: float = 0.0
    state: OrderState = OrderState.PENDING
    fill_index: int | None = None
    fill_time: str | None = None
    fill_price: float | None = None
    fee: float = 0.0
    reject_reason: str | None = None


@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0
    opened_index: int | None = None
    opened_time: str | None = None

    @property
    def is_flat(self) -> bool:
        return self.qty <= 0


@dataclass
class Fill:
    index: int
    time: str
    side: str
    price: float
    qty: float
    notional: float
    fee: float
    reason: str
    realized_pnl: float = 0.0


@dataclass
class EquityPoint:
    index: int
    time: str
    close: float
    cash: float
    position_value: float
    equity: float
    in_position: bool


@dataclass
class PaperAccount:
    """账户快照，供前端直接渲染。"""
    strategy: str
    label: str
    market: str
    symbol: str
    name: str
    interval: str
    source: str

    initial: float
    cash: float
    position_qty: float
    position_avg_price: float
    position_value: float
    equity: float
    unrealized_pnl: float
    realized_pnl: float
    total_fees: float

    total_return: float
    max_drawdown: float
    peak_equity: float
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float

    bar_index: int
    bar_time: str
    bars_total: int
    in_position: bool

    # 过程数据（前端逐帧渲染用）
    equity_curve: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("equity_curve", None)
        d.pop("fills", None)
        d.pop("events", None)
        return d


# ============================================================
class PaperTrader:
    """
    逐根推进的模拟盘。

    用法：
        pt = PaperTrader(series, signals, costs=..., t_plus_1=True)
        while pt.step():      # 每调一次推进一根 K 线
            snap = pt.snapshot()
    """

    def __init__(self, series: Series, signals: SignalSet,
                 costs: Costs | None = None, initial: float = 100_000.0,
                 t_plus_1: bool | None = None, price_limit: bool | None = None,
                 lot_size: int | None = None):
        self.series = series
        self.signals = signals
        self.costs = costs or MARKET_COSTS.get(series.market, Costs())
        self.initial = initial

        # A 股默认开启 T+1 与涨跌停校验；加密 7×24 无此限制
        if t_plus_1 is None:
            t_plus_1 = series.market == "ashare"
        if price_limit is None:
            price_limit = series.market == "ashare"
        if lot_size is None:
            lot_size = 100 if series.market == "ashare" else 1
        self.t_plus_1 = t_plus_1
        self.price_limit = price_limit
        self.lot_size = max(1, int(lot_size))

        self.cash = initial
        self.position = Position()
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[EquityPoint] = []
        self.events: list[dict] = []

        self._order_seq = 0
        self._cursor = -1
        self._peak = initial
        self._max_dd = 0.0
        self._last_buy_index: int | None = None
        self._closed_trades: list[float] = []

        # 未平仓交易的建仓成本，用于算已实现盈亏
        self._open_cost = 0.0

    # --------------------------------------------------------
    @property
    def bars(self) -> tuple[Bar, ...]:
        return self.series.bars

    def _iso(self, i: int) -> str:
        return self.bars[i].as_dict()["iso"]

    def _log(self, kind: str, text: str, i: int | None = None) -> None:
        self.events.append({
            "kind": kind,
            "at": self._iso(i) if i is not None and 0 <= i < len(self.bars) else None,
            "index": i,
            "text": text,
        })

    # --------------------------------------------------------
    def _submit(self, side: Side, i: int, reason: str) -> None:
        """在**第 i 根收盘**提交委托，等待第 i+1 根开盘成交。"""
        self._order_seq += 1
        o = Order(id=self._order_seq, side=side, created_index=i,
                  created_time=self._iso(i), reason=reason)
        self.orders.append(o)
        self._log("order", f"{'买入' if side == Side.BUY else '卖出'}委托已提交：{reason}", i)

    def _try_fill(self, o: Order, i: int) -> None:
        """在第 i 根（开盘）尝试成交这张委托。"""
        bar = self.bars[i]
        raw = bar.open

        # ---- 涨跌停校验（A股）----
        if self.price_limit and i >= 1:
            prev_close = self.bars[i - 1].close
            if prev_close > 0:
                up_limit = prev_close * 1.10
                dn_limit = prev_close * 0.90
                # 一字涨停买不进、一字跌停卖不出（用高低价判断是否封死）
                if o.side == Side.BUY and bar.low >= up_limit * 0.999:
                    o.state = OrderState.REJECTED
                    o.reject_reason = "一字涨停，无法买入"
                    self._log("reject", f"买入被拒：{o.reject_reason}", i)
                    return
                if o.side == Side.SELL and bar.high <= dn_limit * 1.001:
                    o.state = OrderState.REJECTED
                    o.reject_reason = "一字跌停，无法卖出"
                    self._log("reject", f"卖出被拒：{o.reject_reason}", i)
                    return

        # ---- T+1 校验 ----
        # ⚠️ 必须按**成交日**判断，不是按信号产生日。
        #    第 i 根收盘的信号在第 i+1 根开盘成交，所以：
        #      买入成交于 d 日  -> 最早只能在 d+1 日卖出。
        #    我最初把 _last_buy_index 记成了「信号产生的那根」，
        #    结果买入成交于 i+1 却拿 i 去比，判断偏了一天（实测踩过）。
        if (self.t_plus_1 and o.side == Side.SELL
                and self._last_buy_index is not None
                and i <= self._last_buy_index):
            o.state = OrderState.REJECTED
            o.reject_reason = (
                f"T+1 限制：{self._iso(self._last_buy_index)} 买入，"
                f"当日（{self._iso(i)}）不能卖出")
            self._log("reject", f"卖出被拒：{o.reject_reason}", i)
            return

        if o.side == Side.BUY:
            px = self.costs.buy_price(raw)
            # ⚠️ 必须写成 `not (px > 0)` 而不是 `px <= 0`。
            #    NaN 参与任何比较都是 False —— 所以 NaN <= 0 是 False，
            #    保护会被**跳过**，紧接着 math.floor(nan) 抛
            #    「ValueError: cannot convert float NaN to integer」直接崩。
            #    而 not (NaN > 0) 是 True，能正确拦住。
            #    实测：全 NaN 行情会让整个回放崩在成交这一步（verifier 发现）。
            if not (px > 0):
                o.state = OrderState.REJECTED
                o.reject_reason = "价格无效" if px == px else "价格为 NaN（行情数据异常）"
                self._log("reject", f"买入被拒：{o.reject_reason}", i)
                return
            # 按手数取整
            raw_qty = self.cash / (px * (1 + self.costs.commission))
            qty = _round_to_lot(raw_qty, self.lot_size)
            if qty <= 0:
                o.state = OrderState.REJECTED
                o.reject_reason = f"资金不足（需至少 {self.lot_size} 股）"
                self._log("reject", f"买入被拒：{o.reject_reason}", i)
                return

            notional = qty * px
            fee = self.costs.fee(notional)
            self.cash -= notional + fee
            new_qty = self.position.qty + qty
            self.position.avg_price = (
                (self.position.avg_price * self.position.qty + notional) / new_qty
                if new_qty > 0 else px
            )
            self.position.qty = new_qty
            self.position.opened_index = self.position.opened_index or i
            self.position.opened_time = self.position.opened_time or self._iso(i)
            self._open_cost += notional + fee
            # 记「成交日」，T+1 判断要用它（不是信号产生日）
            self._last_buy_index = i
            o.state = OrderState.FILLED
            o.qty, o.fill_index, o.fill_time, o.fill_price, o.fee = qty, i, self._iso(i), px, fee
            self.fills.append(Fill(i, self._iso(i), "buy", px, qty, notional, fee, o.reason))
            self._log("fill", f"买入成交 {qty:g} @ {px:.2f}（{o.reason}）", i)

        else:  # SELL
            if self.position.is_flat:
                o.state = OrderState.REJECTED
                o.reject_reason = "无持仓"
                self._log("reject", "卖出被拒：无持仓", i)
                return
            px = self.costs.sell_price(raw)
            qty = self.position.qty
            gross = qty * px
            fee = self.costs.fee(gross)
            self.cash += gross - fee
            realized = gross - fee - self._open_cost
            self._closed_trades.append(realized)

            o.state = OrderState.FILLED
            o.qty, o.fill_index, o.fill_time, o.fill_price, o.fee = qty, i, self._iso(i), px, fee
            self.fills.append(Fill(i, self._iso(i), "sell", px, qty, gross, fee, o.reason, realized))
            self._log("fill", f"卖出成交 {qty:g} @ {px:.2f}，本笔盈亏 {realized:+,.2f}（{o.reason}）", i)

            self.position = Position()
            self._open_cost = 0.0
            self._last_buy_index = None

    # --------------------------------------------------------
    def step(self) -> bool:
        """推进一根 K 线。返回 False 表示已到末尾。"""
        n = len(self.bars)
        nxt = self._cursor + 1
        if nxt >= n:
            return False

        # 1) 先处理上一根提交的委托（在实际成交时点）
        for o in self.orders:
            if o.state == OrderState.PENDING and o.created_index < nxt:
                self._try_fill(o, nxt)

        self._cursor = nxt
        i = nxt
        bar = self.bars[i]

        # 2) 用第 i 根收盘信号，为第 i+1 根提交委托
        if i < n - 1:
            want = int(self.signals.position[i])
            held = 0 if self.position.is_flat else 1
            # 清理上一根未成交的挂单（避免堆积）
            self.orders = [o for o in self.orders
                           if not (o.state == OrderState.PENDING and o.created_index < i)]
            if want == 1 and held == 0:
                why = self.signals.reasons[i] or "信号转多"
                self._submit(Side.BUY, i, why)
            elif want == 0 and held == 1:
                why = self.signals.reasons[i] or "信号转空"
                self._submit(Side.SELL, i, why)

        # 3) 按收盘价盯市
        pos_value = self.position.qty * bar.close
        equity = self.cash + pos_value
        if equity > self._peak:
            self._peak = equity
        dd = (equity - self._peak) / self._peak * 100 if self._peak > 0 else 0.0
        if dd < -self._max_dd:
            self._max_dd = abs(dd)

        self.equity_curve.append(EquityPoint(
            index=i, time=bar.as_dict()["iso"], close=bar.close,
            cash=self.cash, position_value=pos_value, equity=equity,
            in_position=not self.position.is_flat,
        ))
        return True

    def run_all(self) -> "PaperTrader":
        while self.step():
            pass
        return self

    # --------------------------------------------------------
    def snapshot(self) -> PaperAccount:
        i = max(0, self._cursor)
        bar = self.bars[i] if self.bars else None
        last_close = bar.close if bar else 0.0
        pos_value = self.position.qty * last_close
        equity = self.cash + pos_value
        unreal = (last_close - self.position.avg_price) * self.position.qty if self.position.qty else 0.0

        wins = [x for x in self._closed_trades if x > 0]
        losses = [x for x in self._closed_trades if x <= 0]

        return PaperAccount(
            strategy=self.signals.name, label=self.signals.label,
            market=self.series.market, symbol=self.series.symbol, name=self.series.name,
            interval=self.series.interval, source=self.series.source,
            initial=self.initial,
            cash=round(self.cash, 2),
            position_qty=self.position.qty,
            position_avg_price=round(self.position.avg_price, 4),
            position_value=round(pos_value, 2),
            equity=round(equity, 2),
            unrealized_pnl=round(unreal, 2),
            realized_pnl=round(sum(self._closed_trades), 2),
            total_fees=round(sum(f.fee for f in self.fills), 2),
            total_return=round((equity / self.initial - 1) * 100, 3),
            max_drawdown=round(self._max_dd, 3),
            peak_equity=round(self._peak, 2),
            trade_count=len(self._closed_trades),
            win_count=len(wins), loss_count=len(losses),
            win_rate=round(len(wins) / len(self._closed_trades) * 100, 2) if self._closed_trades else 0.0,
            bar_index=i,
            bar_time=bar.as_dict()["iso"] if bar else "",
            bars_total=len(self.bars),
            in_position=not self.position.is_flat,
            equity_curve=[asdict(p) for p in self.equity_curve],
            fills=[asdict(f) for f in self.fills],
            pending=[asdict(o) for o in self.orders if o.state == OrderState.PENDING],
            events=self.events[-400:],
        )


# ============================================================
#  自检
# ============================================================
def _selftest() -> int:
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("=" * 78)
    print("  模拟盘自检（手算可验证）")
    print("=" * 78)

    def mk(prices, market="crypto", opens=None):
        bars = []
        for i, p in enumerate(prices):
            o = opens[i] if opens else p
            bars.append(Bar(1000 + i * 86400000, o, max(o, p), min(o, p), p, 1.0))
        return Series("test", market, "T", "测试", "1d", tuple(bars), 0.0)

    def sig_of(pos_list, n):
        pos = np.array(pos_list, dtype=np.int8)
        ent = np.zeros(n, bool); ext = np.zeros(n, bool)
        for i in range(1, n):
            if pos[i] == 1 and pos[i-1] == 0: ent[i] = True
            if pos[i] == 0 and pos[i-1] == 1: ext[i] = True
        return SignalSet("t", "测试", "", pos, ent, ext, ["信号"] * n, {})

    # --- 场景1：买入在下一根开盘成交 ---
    print("\n[场景1] 成交时点必须是下一根开盘（禁止未来函数）")
    opens = [100., 110., 120., 130., 140.]
    s = mk([100., 110., 120., 130., 140.], opens=opens)
    sig = sig_of([1, 1, 1, 1, 1], 5)
    pt = PaperTrader(s, sig, costs=Costs(0, 0), initial=100000.0, t_plus_1=False, price_limit=False)
    pt.run_all()
    first_fill = pt.fills[0] if pt.fills else None
    check("有成交", first_fill is not None)
    if first_fill:
        check("成交价 = 第1根开盘价 110（不是第0根的100）",
              abs(first_fill.price - 110.0) < 1e-9, f"{first_fill.price}")
        check("成交发生在 index 1", first_fill.index == 1, f"index={first_fill.index}")

    # --- 场景2：手续费与滑点方向 ---
    print("\n[场景2] 成本方向")
    c = Costs(commission=0.001, slippage=0.002)
    check("买入价上滑", c.buy_price(100) > 100, f"{c.buy_price(100)}")
    check("卖出价下滑", c.sell_price(100) < 100, f"{c.sell_price(100)}")
    pt2 = PaperTrader(s, sig, costs=c, initial=100000.0, t_plus_1=False, price_limit=False)
    pt2.run_all()
    check("累计手续费 > 0", pt2.snapshot().total_fees > 0, f"{pt2.snapshot().total_fees}")
    pt0 = PaperTrader(s, sig, costs=Costs(0, 0), initial=100000.0, t_plus_1=False, price_limit=False)
    pt0.run_all()
    check("有成本时权益更低", pt2.snapshot().equity < pt0.snapshot().equity,
          f"{pt2.snapshot().equity} < {pt0.snapshot().equity}")

    # --- 场景3：T+1 限制必须真的拦住当日卖出 ---
    print("\n[场景3] A股 T+1 校验")
    # 买入成交于 index1。要让「卖出也在 index1 成交」，卖出委托必须在 index0 就挂出。
    # 但 index0 时还没有持仓 -> 会被「无持仓」拒掉。
    # 所以真正能触发 T+1 的情形是：**同一根 K 线内部先买后卖**（日内交易），
    # 这需要一天有多根 K 线（如 1 分钟线），用两根 K 线模拟不了。
    # 这里直接构造：手动提交一张当天平仓的卖出委托来验证校验逻辑本身。
    s3 = mk([100., 100., 100., 100., 100.], market="ashare")
    sig3 = sig_of([1, 1, 1, 1, 1], 5)   # 全程持仓，不会自己产生卖出信号
    pt3 = PaperTrader(s3, sig3, costs=Costs(0, 0), initial=100000.0)
    pt3.step()   # index 0
    pt3.step()   # index 1 -> 买入成交于此
    buy_idx = pt3._last_buy_index
    check("买入成交并记录了成交日", buy_idx == 1, f"_last_buy_index={buy_idx}")

    # 手动挂一张「当日平仓」的卖出委托，然后在同一根上尝试成交
    pt3._submit(Side.SELL, buy_idx, "测试当日卖出")
    manual = pt3.orders[-1]
    pt3._try_fill(manual, buy_idx)      # 用**买入当日**去成交
    check("T+1 拦住了当日卖出",
          manual.state == OrderState.REJECTED,
          f"状态={manual.state.value} 原因={manual.reject_reason}")
    check("拒绝原因说明了 T+1",
          "T+1" in (manual.reject_reason or ""),
          f"{manual.reject_reason}")

    # 隔一天再试，应该能成交
    pt3.step()   # index 2
    pt3._submit(Side.SELL, 2, "测试隔日卖出")
    manual2 = pt3.orders[-1]
    pt3._try_fill(manual2, 2)
    check("隔日卖出可以成交",
          manual2.state == OrderState.FILLED,
          f"状态={manual2.state.value} 原因={manual2.reject_reason}")

    # 关闭 T+1 后，当日卖出应放行
    pt3b = PaperTrader(s3, sig3, costs=Costs(0, 0), initial=100000.0, t_plus_1=False)
    pt3b.step(); pt3b.step()
    pt3b._submit(Side.SELL, 1, "测试当日卖出（T+1关闭）")
    m3 = pt3b.orders[-1]
    pt3b._try_fill(m3, 1)
    check("关闭 T+1 后当日卖出放行",
          m3.state == OrderState.FILLED,
          f"状态={m3.state.value}")

    # --- 场景4：手数取整（A股 100 股一手）---
    print("\n[场景4] 手数取整")
    s4 = mk([10., 10., 10.], market="ashare")
    sig4 = sig_of([1, 1, 1], 3)
    pt4 = PaperTrader(s4, sig4, costs=Costs(0, 0), initial=1050.0)
    pt4.run_all()
    q = pt4.snapshot().position_qty
    check("持仓量是 100 的整数倍", q % 100 == 0, f"{q}")
    check("1050 元买 10 元股票最多 100 股", q == 100, f"{q}")

    # --- 场景5：权益曲线长度与逐根推进一致 ---
    print("\n[场景5] 逐根推进")
    pt5 = PaperTrader(s, sig, costs=Costs(0, 0), initial=100000.0)
    steps = 0
    while pt5.step():
        steps += 1
    check("推进次数 = K线数", steps == 5, f"{steps}")
    check("权益曲线长度 = K线数", len(pt5.equity_curve) == 5, f"{len(pt5.equity_curve)}")

    # --- 场景6：最大回撤 ---
    print("\n[场景6] 回撤统计")
    prices = [100., 120., 90., 95., 80., 130.]
    s6 = mk(prices)
    sig6 = sig_of([1, 1, 1, 1, 1, 1], 6)
    pt6 = PaperTrader(s6, sig6, costs=Costs(0, 0), initial=100000.0, t_plus_1=False)
    pt6.run_all()
    snap6 = pt6.snapshot()
    check("最大回撤 > 0", snap6.max_drawdown > 0, f"{snap6.max_drawdown:.2f}%")
    check("权益曲线单调性检查（峰值被记录）", snap6.peak_equity > 0, f"{snap6.peak_equity}")

    # --- 场景7：不成交时权益不变 ---
    print("\n[场景7] 无信号 -> 权益不变")
    sig7 = sig_of([0, 0, 0, 0, 0], 5)
    pt7 = PaperTrader(s, sig7, costs=Costs(0, 0), initial=100000.0)
    pt7.run_all()
    check("无成交", len(pt7.fills) == 0)
    check("权益始终 = 初始", abs(pt7.snapshot().equity - 100000.0) < 1e-9,
          f"{pt7.snapshot().equity}")

    print("\n" + "=" * 78)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 78)
    return 1 if fail else 0


# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="模拟盘（纸上交易，不下真实订单）")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    ap.add_argument("--initial", type=float, default=None,
                    help="初始资金，默认按市场选择（A股 100 万 / 加密 10 万）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    initial = args.initial if args.initial is not None else default_initial(args.market)

    series = fetch(args.market, symbol, interval, args.limit)
    ind = compute(series)
    sigs = run_all(ind) if args.strategy == "all" else {args.strategy: run_strategy(args.strategy, ind)}

    accounts = {}
    for key, sig in sigs.items():
        pt = PaperTrader(series, sig, initial=initial)
        pt.run_all()
        accounts[key] = pt

    if args.json:
        print(json.dumps({
            "meta": series.meta(),
            "accounts": {k: v.snapshot().summary() for k, v in accounts.items()},
        }, ensure_ascii=False, indent=2))
        return 0

    m = series.meta()
    print("=" * 80)
    print(f"  模拟盘  {m['name']}  {m['symbol']}  {m['interval']}")
    print(f"  {m['first']} → {m['last']}   {m['count']} 根   源={m['source']}   初始 {initial:,.0f}")
    print(f"  规则：信号次日开盘成交 | T+1={series.market == 'ashare'} | "
          f"手数={100 if series.market == 'ashare' else 1} | 含手续费与滑点")
    print("=" * 80)

    for key, pt in accounts.items():
        a = pt.snapshot()
        print(f"\n  【{a.label}】")
        print(f"    权益      {a.equity:>12,.2f}   初始 {a.initial:>12,.2f}   收益 {a.total_return:>7.2f}%")
        print(f"    现金      {a.cash:>12,.2f}   持仓市值 {a.position_value:>12,.2f}")
        print(f"    持仓      {a.position_qty:>12g} 股 @ {a.position_avg_price:>10.2f}   "
              f"{'持仓中' if a.in_position else '空仓'}")
        print(f"    浮动盈亏  {a.unrealized_pnl:>12,.2f}   已实现 {a.realized_pnl:>12,.2f}")
        print(f"    手续费    {a.total_fees:>12,.2f}   最大回撤 {a.max_drawdown:>6.2f}%")
        print(f"    交易      {a.trade_count:>4} 笔完成   胜率 {a.win_rate:>5.1f}%  ({a.win_count}胜/{a.loss_count}负)")

        rej = [o for o in pt.orders if o.state == OrderState.REJECTED]
        if rej:
            print(f"    被拒委托  {len(rej)} 笔")
            seen = set()
            for o in rej[:6]:
                r = o.reject_reason or ""
                if r in seen:
                    continue
                seen.add(r)
                print(f"      · {o.created_time}  {'买入' if o.side == Side.BUY else '卖出'} — {r}")

        if pt.fills:
            print(f"    最近成交：")
            for f in pt.fills[-5:]:
                extra = f"  已实现 {f.realized_pnl:+,.2f}" if f.side == "sell" else ""
                print(f"      {f.time}  {'买入' if f.side == 'buy' else '卖出'} "
                      f"{f.qty:g} @ {f.price:.2f}{extra}")

    print("\n" + "=" * 80)
    print("  ⚠ 这是模拟盘：只读行情，不连接交易接口，不产生任何真实委托。")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
