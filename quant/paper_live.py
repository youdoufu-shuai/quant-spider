#!/usr/bin/env python3
"""
交互式模拟盘 —— 持续运行、可手动下单、可自动交易、状态可持久化。

═══════════════════════════════════════════════════════════════════
  与 paper.py 的区别（那是个离线回放器，不是模拟盘）
═══════════════════════════════════════════════════════════════════
  paper.py      PaperTrader：拿一段历史 K 线**一次性跑完**，输出最终账户。
                没有下单接口、没有实时更新、跑完就结束 —— 本质是回测的另一种呈现。

  paper_live.py PaperSession：**长驻会话**。行情推到哪就跟到哪，
                用户可以随时下单/撤单/平仓/重置，账户状态跨请求保持。
                这才是"模拟盘"该有的样子。

═══════════════════════════════════════════════════════════════════
  两种下单模式（对应两种真实使用场景）
═══════════════════════════════════════════════════════════════════
  next_open（默认，推荐）
      和回测口径一致：下单后等下一根 K 线开盘成交。
      ★ 这是**无未来函数**的做法。同一根 K 线内先看到收盘价再下单、
        然后按那个收盘价成交 —— 那是自欺。
      但纯手工操作时"等下一根"体验不好，所以提供下面这个：

  market
      立刻按当前价成交（含滑点）。
      标的是**当前未收盘的那根 K 线的最新价**。
      ⚠️ 这会引入「同一根内的前视」：如果你看着已确定的收盘价下单，
         等于用了当时还拿不到的信息。所以界面上必须写明。

  两种模式都记在成交记录里，导出的账单能看出用的哪种。

═══════════════════════════════════════════════════════════════════
  安全边界（必须说清楚）
═══════════════════════════════════════════════════════════════════
  这是模拟盘，**不是实盘**：
    · 只读行情；没有任何交易接口的接入代码
    · 不持有 API Key；不产生任何真实委托
    · 所有成交只发生在内存与本地 JSON 文件里
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from datasource import MARKETS, Series, fetch, DataSourceError, SYMBOL_NAMES
from indicators import compute
from strategy import STRATEGIES, run as run_strategy, run_all
from backtest import Costs, MARKET_COSTS
from autotrader import AutoConfig, RiskConfig, AutoDecider, AIDecider, make_decider

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CST = timezone(timedelta(hours=8))
STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "paper-sessions.json"

MAX_SESSIONS = 8
MAX_FILLS = 2000          # 每个会话保留的成交记录上限（防止 JSON 无限膨胀）


# ============================================================
def symbol_name(market: str, symbol: str) -> str:
    """
    取标的中文名。

    统一走 symbols 目录（A 股 8265 只全量列表 + 加密 511 对的币种中文名表），
    它查不到再退回内置 SYMBOL_NAMES。
    查不到就**原样返回代码** —— 不编造名字。
    """
    try:
        import symbols as _sym
        n = _sym.display_name(market, symbol)
        # display_name 查不到时会原样返回代码，这时再试一次内置表
        if n and n != symbol:
            return n
    except Exception:
        pass
    return SYMBOL_NAMES.get(symbol.upper() if market == "crypto" else symbol, symbol)



# ============================================================
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    NEXT_OPEN = "next_open"   # 下一根开盘成交（推荐，无未来函数）
    MARKET = "market"         # 立即按当前价成交（有前视，界面需提示）


class OrderState(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class PaperOrder:
    id: str
    side: str
    type: str
    qty: float
    created_at: str
    created_bar: int
    created_price: float        # 下单时的参考价（仅供记录）
    state: str = OrderState.PENDING.value
    reason: str = ""
    source: str = "manual"      # manual | auto
    fill_bar: int | None = None
    fill_at: str | None = None
    fill_price: float | None = None
    fee: float = 0.0
    reject_reason: str | None = None


@dataclass
class PaperFill:
    at: str
    bar: int
    side: str
    price: float
    qty: float
    notional: float
    fee: float
    realized: float = 0.0
    source: str = "manual"
    reason: str = ""


@dataclass
class PaperLog:
    at: str
    bar: int
    kind: str          # quote | order | fill | reject | cancel | reset | auto
    text: str


# ============================================================
class PaperSession:
    """
    一个模拟盘会话。长驻内存，可被 HTTP 请求反复操作。

    状态可序列化到 JSON —— 服务重启后账户还在（这是"模拟盘"该有的持续性，
    否则每次重启账户都归零，没法连续观察）。
    """

    def __init__(self, *, session_id: str | None = None, market: str = "ashare",
                 symbol: str | None = None, interval: str | None = None,
                 initial: float | None = None, costs: Costs | None = None,
                 label: str = ""):
        cfg = MARKETS.get(market)
        if cfg is None:
            raise ValueError(f"未知市场 {market}")
        self.id = session_id or str(uuid.uuid4())
        self.market = market
        self.symbol = symbol or cfg["defaultSymbol"]
        self.interval = interval or cfg["defaultInterval"]

        # A 股必须整手；加密可以小数
        self.lot = 100 if market == "ashare" else 1
        self.t_plus_1 = market == "ashare"
        self.price_limit = market == "ashare"
        self.costs = costs or MARKET_COSTS.get(market, Costs())

        self.initial = float(initial) if initial else float(
            1_000_000 if market == "ashare" else 100_000)
        self.cash = self.initial
        self.qty = 0.0
        self.avg_price = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.peak = self.initial
        self.max_dd = 0.0

        self.bar_index = -1
        self.orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self.logs: list[PaperLog] = []
        self.equity_curve: list[dict] = []

        # 自动交易
        #
        # mode 说明：
        #   off    关闭
        #   score  多因子打分（默认，内置，推荐）
        #   confirm 多策略共识
        #   signal  单个策略（和最早的版本一致，可用来对照）
        #   ai      交给外部 AI（见 autotrader.AIDecider）
        self.auto = False
        self.auto_mode = "score"
        self.strategy = "rsi_reversion"
        self.auto_config = AutoConfig(mode="score")
        self._last_auto_target = 0
        self._auto_ind = None            # 缓存指标，避免每次全量重算
        self._auto_ind_len = -1
        # 风控需要的持仓期状态
        self.peak_price = 0.0            # 持仓期间最高价（移动止损用）
        self.bars_held = 0               # 已持有多少根
        self.bars_since_exit = 9999      # 距上次卖出多少根（冷却用）
        self.halted = False              # 回撤熔断后只允许离场
        # 最后一次决策：存进快照，这样打开页面就能看到上次的判断，
        # 而不是必须自己点一次「试算」（否则卡片永远是空的）
        self.last_decision: dict | None = None
        self.last_decision_at: str | None = None

        self.last_buy_bar: int | None = None
        self.label = label or f"{market}/{self.symbol}/{self.interval}"
        self.created_at = self._now()
        self.updated_at = self.created_at
        self.series: Series | None = None
        self.price_series: list[float] = []     # 每根收盘价（图表用）
        # ⚠️ source/bar_at 必须**单独存字段**，不能只从 self.series 推导：
        #    保存状态时只序列化 price_series，不序列化 series（太大且没必要），
        #    所以 from_state() 恢复后 self.series 是 None，
        #    任何依赖它的属性（源名、bar 时间）就会变成 None/"—"。
        #    实测踩过：服务重启后页头显示"源 —"、柱时间显示"—"。
        self.source: str | None = None
        self.bar_at: str | None = None

    # --------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    def _log(self, kind: str, text: str) -> None:
        self.logs.append(PaperLog(self._now(), self.bar_index, kind, text))
        if len(self.logs) > 600:
            del self.logs[:-500]

    def _iso(self, i: int) -> str:
        if self.series and 0 <= i < len(self.series.bars):
            return self.series.bars[i].as_dict()["iso"]
        return "—"

    @property
    def price(self) -> float:
        """当前参考价：最新一根的收盘价。"""
        if self.price_series:
            return self.price_series[-1]
        return 0.0

    @property
    def equity(self) -> float:
        return self.cash + self.qty * self.price

    @property
    def unrealized(self) -> float:
        return (self.price - self.avg_price) * self.qty if self.qty else 0.0

    # --------------------------------------------------------
    #  行情推进
    # --------------------------------------------------------
    def pull_market(self, limit: int = 300, advance: bool = True) -> dict:
        """
        拉一次真实行情并把会话推进到最新一根。
        返回本次推进的摘要（新增几根、价格变化）。
        """
        series = fetch(self.market, self.symbol, self.interval, limit)
        prev_len = len(self.price_series)
        self.series = series
        self.source = series.source          # 单独存，重启后不丢
        new_closes = [b.close for b in series.bars]
        self.price_series = new_closes

        newest = len(new_closes) - 1
        advanced = 0
        if advance:
            if self.bar_index < 0:
                # 首次：从第一根开始建权益曲线，但不触发历史成交
                self.bar_index = newest
            else:
                advanced = max(0, newest - self.bar_index)
                self.bar_index = newest
        self.bar_at = self._iso(self.bar_index)   # 单独存，重启后不丢

        # 成交挂单（在推进到的新价格上）
        if advanced > 0:
            for o in list(self.orders):
                if o.state == OrderState.PENDING.value:
                    self._try_fill(o, newest)

        self._mark()
        self.updated_at = self._now()

        # 自动交易
        if self.auto and advanced >= 0:
            self._auto_step(newest, first=(prev_len == 0))

        return {
            "advancedBars": advanced,
            "barIndex": newest,
            "price": self.price,
            "source": series.source,
            "count": len(new_closes),
            "at": self._iso(newest),
        }

    def _mark(self) -> None:
        """按当前价盯市，更新峰值与回撤。"""
        eq = self.equity
        if eq > self.peak:
            self.peak = eq
        if self.peak > 0:
            dd = (self.peak - eq) / self.peak * 100
            if dd > self.max_dd:
                self.max_dd = dd
        point = {
            "bar": self.bar_index,
            "at": self._iso(self.bar_index),
            "price": round(self.price, 6),
            "equity": round(eq, 2),
            "cash": round(self.cash, 2),
            "positionValue": round(self.qty * self.price, 2),
            "inPosition": self.qty > 0,
        }
        if self.equity_curve and self.equity_curve[-1]["bar"] == self.bar_index:
            self.equity_curve[-1] = point      # 同一根只保留最新
        else:
            self.equity_curve.append(point)
        if len(self.equity_curve) > 1200:
            del self.equity_curve[:-1000]

    # --------------------------------------------------------
    #  下单
    # --------------------------------------------------------
    def _round_lot(self, qty: float) -> float:
        """
        把数量对齐到「手」的整数倍。

        加密手数为 1 时允许小数 —— 否则 10 万本金按 8 万/个的 BTC 买入
        只能成交 1 个，剩下 2 万白白闲置。A 股仍必须整百股。
        """
        q = float(qty)
        if not (q > 0):
            return 0.0
        if self.lot <= 1:
            return math.floor(q * 1e8) / 1e8
        return float(math.floor(q / self.lot) * self.lot)

    def place_order(self, side: str, qty: float | None = None,
                    order_type: str = OrderType.NEXT_OPEN.value,
                    reason: str = "", source: str = "manual") -> PaperOrder:
        """
        下单。qty 为 None 时按「全仓买入 / 全部卖出」计算。
        """
        try:
            sd = Side(side)
        except ValueError:
            raise ValueError(f"side 必须是 buy 或 sell，收到 {side!r}")
        try:
            ot = OrderType(order_type)
        except ValueError:
            raise ValueError(f"type 必须是 next_open 或 market，收到 {order_type!r}")

        px = self.price
        # ⚠️ 同样要写成 not (px > 0)：NaN <= 0 是 False，保护会漏过 NaN。
        if not (px > 0):
            raise ValueError("还没有行情，先刷新一次")

        # 数量计算
        if qty is None or qty <= 0:
            if sd == Side.BUY:
                raw = self.cash / (self.costs.buy_price(px) * (1 + self.costs.commission))
                qty = self._round_lot(raw)
            else:
                qty = self.qty
        else:
            qty = self._round_lot(float(qty))

        o = PaperOrder(
            id=str(uuid.uuid4())[:8], side=sd.value, type=ot.value, qty=qty,
            created_at=self._now(), created_bar=self.bar_index,
            created_price=px, reason=reason, source=source,
        )

        # 前置校验（能立刻判断的先拒掉，省得挂个必死的单）
        if qty <= 0:
            o.state = OrderState.REJECTED.value
            o.reject_reason = (f"资金不足（一手需 {self.lot} 股）"
                               if sd == Side.BUY else "无持仓可卖")
        elif sd == Side.BUY:
            need = qty * self.costs.buy_price(px) * (1 + self.costs.commission)
            if need > self.cash:
                o.state = OrderState.REJECTED.value
                o.reject_reason = f"资金不足（需 {need:,.2f}，可用 {self.cash:,.2f}）"
        elif self.qty <= 0:
            o.state = OrderState.REJECTED.value
            o.reject_reason = "无持仓可卖"
        elif qty > self.qty:
            o.state = OrderState.REJECTED.value
            o.reject_reason = f"持仓不足（持有 {self.qty:g}，要卖 {qty:g}）"
        # T+1：按**成交日**判断，这里只能拦"同一根内买卖"
        elif (self.t_plus_1 and self.last_buy_bar is not None
              and self.bar_index <= self.last_buy_bar):
            o.state = OrderState.REJECTED.value
            o.reject_reason = f"T+1 限制：本根（{self._iso(self.bar_index)}）刚买入，当日不能卖"

        self.orders.append(o)
        if len(self.orders) > 400:
            del self.orders[:-350]

        if o.state == OrderState.REJECTED.value:
            self._log("reject", f"下单被拒：{o.reject_reason}")
            return o

        self._log("order", f"{'买入' if sd == Side.BUY else '卖出'} "
                           f"{qty:g} @ 参考 {px:.2f}（{ot.value}）{('· ' + reason) if reason else ''}")

        # 市价单立刻成交
        if ot == OrderType.MARKET:
            self._fill(o, px, self.bar_index)
        self.updated_at = self._now()
        return o

    def cancel_order(self, order_id: str) -> bool:
        for o in self.orders:
            if o.id == order_id and o.state == OrderState.PENDING.value:
                o.state = OrderState.CANCELLED.value
                self._log("cancel", f"撤单 {o.side} {o.qty:g}")
                self.updated_at = self._now()
                return True
        return False

    def cancel_all(self) -> int:
        n = 0
        for o in self.orders:
            if o.state == OrderState.PENDING.value:
                o.state = OrderState.CANCELLED.value
                n += 1
        if n:
            self._log("cancel", f"全部撤单（{n} 笔）")
            self.updated_at = self._now()
        return n

    # --------------------------------------------------------
    def _try_fill(self, o: PaperOrder, bar: int) -> None:
        """在指定 K 线上尝试成交挂单（用该根的开盘价，无未来函数）。"""
        if not self.series or not (0 <= bar < len(self.series.bars)):
            return
        raw = self.series.bars[bar].open
        self._fill(o, raw, bar)

    def _fill(self, o: PaperOrder, raw_price: float, bar: int) -> None:
        sd = Side(o.side)
        px = self.costs.buy_price(raw_price) if sd == Side.BUY else self.costs.sell_price(raw_price)
        if px <= 0:
            o.state = OrderState.REJECTED.value
            o.reject_reason = "价格无效"
            return

        notional = o.qty * px
        fee = self.costs.fee(notional)

        if sd == Side.BUY:
            if notional + fee > self.cash:
                o.state = OrderState.REJECTED.value
                o.reject_reason = f"成交时资金不足（需 {notional + fee:,.2f}）"
                self._log("reject", f"买入成交失败：{o.reject_reason}")
                return
            self.cash -= notional + fee
            new_qty = self.qty + o.qty
            self.avg_price = ((self.avg_price * self.qty + notional) / new_qty) if new_qty else px
            self.qty = new_qty
            self.last_buy_bar = bar
            # 首次建仓：重置持仓期跟踪
            if new_qty == o.qty:
                self.peak_price = px
                self.bars_held = 0
            realized = 0.0
        else:
            gross = notional - fee
            realized = gross - self.avg_price * o.qty
            self.cash += gross
            self.qty -= o.qty
            if self.qty <= 1e-9:
                self.qty = 0.0
                self.avg_price = 0.0
                # 清仓：重置持仓期状态，让冷却期与移动止损从头开始
                self.peak_price = 0.0
                self.bars_held = 0
                self.bars_since_exit = 0
            self.realized += realized

        self.fees += fee
        o.state = OrderState.FILLED.value
        o.fill_bar, o.fill_at, o.fill_price, o.fee = bar, self._iso(bar), px, fee
        self.fills.append(PaperFill(
            at=self._iso(bar), bar=bar, side=sd.value, price=round(px, 6),
            qty=o.qty, notional=round(notional, 2), fee=round(fee, 2),
            realized=round(realized, 2), source=o.source, reason=o.reason,
        ))
        if len(self.fills) > MAX_FILLS:
            del self.fills[:-MAX_FILLS + 200]
        self._log("fill", f"{'买入' if sd == Side.BUY else '卖出'} {o.qty:g} @ {px:.2f}"
                          + (f"，本笔盈亏 {realized:+,.2f}" if sd == Side.SELL else "")
                          + f"（{o.source}）")
        self._mark()

    # --------------------------------------------------------
    #  自动交易（三层：信号 → 仓位 → 风控）
    # --------------------------------------------------------
    def _indicators(self):
        """
        取指标，按 series 长度缓存。

        原来每次推进都 compute() 全量重算 —— 300 根 K 线每次刷新重算
        27 个指标，纯浪费。行情没变长就直接复用。
        """
        if not self.series:
            return None
        n = len(self.series.bars)
        if self._auto_ind is None or self._auto_ind_len != n:
            try:
                self._auto_ind = compute(self.series)
                self._auto_ind_len = n
            except Exception as e:
                self._log("auto", f"指标计算失败：{type(e).__name__}: {e}")
                return None
        return self._auto_ind

    def _position_fraction(self) -> float:
        """当前仓位比例 = 持仓市值 / 权益。"""
        eq = self.equity
        return (self.qty * self.price / eq) if eq > 0 else 0.0

    def _decider(self):
        """
        按 auto_mode 造决策器。

        ⚠️ AI 模式走 ai_settings 的配置（页面里能改），
           不再只读环境变量 —— 这样用户可以换模型而不用重启服务。
           配置没弄好时**明确抛错**，由 _auto_step 捕获后回落内置打分，
           并在事件日志里留下原因（不静默降级）。
        """
        cfg = self.auto_config
        cfg.mode = self.auto_mode
        cfg.strategy = self.strategy
        if self.auto_mode == "ai":
            from ai_settings import AIStore
            store = AIStore()
            store.load()
            return store.build_decider(cfg)
        return AutoDecider(cfg)

    def _auto_step(self, bar: int, first: bool = False,
                   force: bool = False) -> dict | None:
        """
        跑一次自动决策。返回决策 dict（便于调用方记录/展示），无决策返回 None。

        三层顺序（和 autotrader.py 里的说明一致）：
          1. 风控   —— 止损/止盈/移动止损/回撤熔断。**优先于信号**，
                       触发就下单，不问策略意见。
          2. 信号   —— 打分 / 共识 / 单策略，产出目标仓位比例。
          3. 仓位   —— 把目标比例换算成股数，走「下一根开盘」成交。

        force=True 时即使自动交易没开启也会跑（用于「试算一次决策」）。
        **试算必须能在关闭状态下预览** —— 用户想先看看它会怎么判断
        再决定要不要开，这是主要使用方式。
        """
        if (not self.auto and not force) or not self.series:
            return None
        ind = self._indicators()
        if ind is None:
            return None

        # 持仓期统计（移动止损与最短持有要用）
        if self.qty > 0:
            if self.price > self.peak_price:
                self.peak_price = self.price
            self.bars_held += (0 if first else 1)
        else:
            self.bars_held = 0
            self.peak_price = 0.0
            self.bars_since_exit += (0 if first else 1)

        decider = self._decider()
        try:
            kwargs = dict(
                ind=ind, i=bar, position=1 if self.qty > 0 else 0,
                fraction=self._position_fraction(),
                entry_price=self.avg_price, peak_price=self.peak_price,
                drawdown=self.max_dd / 100.0,
                bars_since_exit=self.bars_since_exit,
                bars_held=self.bars_held,
                halted=self.halted,
            )
            # ⚠️ meta 只有 AIDecider 认这个参数。
            #    AutoDecider.decide() 没有它，无条件传会 TypeError:
            #    "got an unexpected keyword argument 'meta'"，而且被下面的
            #    except 吞掉，表现为「自动交易开了但永远不下单」——
            #    自检里就是这条抓出来的。
            if self.auto_mode == "ai":
                kwargs["meta"] = {
                    "symbol": self.symbol,
                    "name": symbol_name(self.market, self.symbol),
                    "interval": self.interval, "equity": self.equity,
                    "cash": self.cash,
                    "recent": [asdict(f) for f in self.fills[-5:]],
                }
            dec = decider.decide(**kwargs)
        except Exception as e:
            self._log("auto", f"决策失败：{type(e).__name__}: {e}")
            return None

        d = dec.to_dict()
        # 记录最后一次决策（含试算），供前端展示「为什么这么判断」
        self.last_decision = d
        self.last_decision_at = self._now()

        if first:
            self._last_auto_target = dec.target
            self._log("auto", f"自动交易已启用（{self._mode_label()}）"
                              f"：当前仓位 {self._position_fraction():.0%}，"
                              f"目标 {dec.target:.0%}")
            return d

        # 试算模式：只返回决策，不下单、不改状态 —— 用户可以放心预览
        if force and not self.auto:
            return d

        # 回撤熔断：标记后只允许离场
        if dec.risk_hit and "熔断" in dec.risk_hit:
            self.halted = True
            self._log("auto", f"⚠ {dec.risk_hit} —— 进入停手状态，只允许离场")

        if dec.action == "hold":
            # 只在有意义时记日志，避免每根都刷屏
            if dec.risk_hit:
                self._log("auto", dec.summary())
            return d

        # 把目标比例换算成股数
        eq = self.equity
        target_value = eq * dec.target
        cur_value = self.qty * self.price
        delta_value = target_value - cur_value

        if dec.action == "buy" and delta_value > 0:
            px = self.price * (1 + self.costs.slippage) * (1 + self.costs.commission)
            qty = self._round_lot(delta_value / px)
            # 不能超过可用现金
            afford = self._round_lot(self.cash / (px * (1 + self.costs.commission)))
            qty = min(qty, afford)
            if qty < self.lot:
                self._log("auto", f"目标仓位需买入 {delta_value:,.0f}，"
                                  f"但可用现金不足（{self.cash:,.0f}），跳过")
                return d
            self.place_order("buy", qty, OrderType.NEXT_OPEN.value,
                             reason=dec.summary(), source="auto")
        elif dec.action == "sell" and delta_value < 0:
            qty = self._round_lot(min(-delta_value / self.price, self.qty))
            if self.lot > 1 and qty == 0 and self.qty > 0:
                qty = self.qty        # 目标清仓但不足一手时，一次卖掉全部
            if qty <= 0:
                return d
            self.place_order("sell", qty, OrderType.NEXT_OPEN.value,
                             reason=dec.summary(), source="auto")

        self._last_auto_target = dec.target
        return d

    def _mode_label(self) -> str:
        return {
            "score": "多因子打分", "confirm": "多策略共识",
            "signal": f"单策略（{STRATEGIES[self.strategy][0] if self.strategy in STRATEGIES else self.strategy}）",
            "ai": "AI 决策",
        }.get(self.auto_mode, self.auto_mode)

    def set_auto(self, on: bool, strategy: str | None = None,
                 mode: str | None = None, config: dict | None = None) -> dict:
        """
        开关自动交易 / 改配置。

        config 支持：
          mode, strategy, confirm_min, entry_threshold, exit_threshold,
          sizing, fixed_fraction, max_fraction, rebalance_band, target_vol
          以及 risk.*（stop_loss / take_profit / trailing_stop /
          max_drawdown / cooldown_bars / min_hold_bars）
        """
        if strategy:
            if strategy not in STRATEGIES:
                raise ValueError(f"未知策略 {strategy}")
            self.strategy = strategy
        if mode:
            if mode not in ("off", "score", "confirm", "signal", "ai"):
                raise ValueError(f"未知自动模式 {mode}")
            self.auto_mode = mode

        if config:
            risk_keys = set(RiskConfig().to_dict())
            risk_kw, auto_kw = {}, {}
            for k, v in config.items():
                if k in risk_keys:
                    risk_kw[k] = v
                elif hasattr(self.auto_config, k):
                    auto_kw[k] = v
            for k, v in auto_kw.items():
                setattr(self.auto_config, k, v)
            for k, v in risk_kw.items():
                setattr(self.auto_config.risk, k, v)

        self.auto = bool(on) and self.auto_mode != "off"
        if self.auto:
            # 关闭熔断重新开始（用户显式重启自动交易）
            self.halted = False
            self._auto_step(self.bar_index, first=True)
            self._log("auto", f"自动交易开启（{self._mode_label()}）")
        else:
            self._log("auto", "自动交易关闭")
        self.updated_at = self._now()
        return {
            "auto": self.auto, "mode": self.auto_mode, "strategy": self.strategy,
            "config": self.auto_config.to_dict(),
        }

    # --------------------------------------------------------
    def reset(self, initial: float | None = None) -> None:
        self.initial = float(initial) if initial else self.initial
        self.cash = self.initial
        self.qty = 0.0
        self.avg_price = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.peak = self.initial
        self.max_dd = 0.0
        self.orders.clear()
        self.fills.clear()
        self.equity_curve.clear()
        self.logs.clear()
        self.last_buy_bar = None
        self._last_auto_target = 0
        self._log("reset", f"账户已重置，初始资金 {self.initial:,.0f}")
        self._mark()
        self.updated_at = self._now()

    # --------------------------------------------------------
    def snapshot(self, *, with_curve: bool = True) -> dict:
        wins = [f.realized for f in self.fills if f.side == "sell" and f.realized > 0]
        losses = [f.realized for f in self.fills if f.side == "sell" and f.realized <= 0]
        closed = len(wins) + len(losses)
        eq = self.equity
        return {
            "id": self.id,
            "label": self.label,
            "market": self.market,
            "symbol": self.symbol,
            "interval": self.interval,
            "name": symbol_name(self.market, self.symbol),
            # 用独立字段而不是 self.series —— 重启后 series 是 None
            "source": self.source,

            "initial": round(self.initial, 2),
            "cash": round(self.cash, 2),
            "qty": self.qty,
            "avgPrice": round(self.avg_price, 4),
            "price": round(self.price, 6),
            "positionValue": round(self.qty * self.price, 2),
            "equity": round(eq, 2),
            "unrealized": round(self.unrealized, 2),
            "realized": round(self.realized, 2),
            "fees": round(self.fees, 2),
            "totalReturn": round((eq / self.initial - 1) * 100, 3) if self.initial else 0,
            "peak": round(self.peak, 2),
            "maxDrawdown": round(self.max_dd, 3),

            "tradeCount": closed,
            "winCount": len(wins),
            "lossCount": len(losses),
            "winRate": round(len(wins) / closed * 100, 2) if closed else 0.0,
            "avgWin": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avgLoss": round(sum(losses) / len(losses), 2) if losses else 0.0,

            "barIndex": self.bar_index,
            "barAt": self.bar_at or self._iso(self.bar_index),
            "barsTotal": len(self.price_series),
            "inPosition": self.qty > 0,

            "auto": self.auto,
            "autoMode": self.auto_mode,
            "autoConfig": self.auto_config.to_dict(),
            "halted": self.halted,
            "peakPrice": round(self.peak_price, 6),
            "barsHeld": self.bars_held,
            "barsSinceExit": self.bars_since_exit,
            "lastDecision": self.last_decision,
            "lastDecisionAt": self.last_decision_at,
            "strategy": self.strategy,
            "strategyLabel": STRATEGIES[self.strategy][0] if self.strategy in STRATEGIES else self.strategy,
            "lot": self.lot,
            "tPlus1": self.t_plus_1,
            "commission": self.costs.commission,
            "slippage": self.costs.slippage,

            "createdAt": self.created_at,
            "updatedAt": self.updated_at,

            "pendingOrders": [asdict(o) for o in self.orders
                              if o.state == OrderState.PENDING.value],
            "recentOrders": [asdict(o) for o in self.orders[-40:]],
            "recentFills": [asdict(f) for f in self.fills[-60:]],
            "logs": [asdict(l) for l in self.logs[-160:]],
            "equityCurve": self.equity_curve[-400:] if with_curve else [],
            "prices": [round(p, 6) for p in self.price_series[-400:]],
        }

    # --------------------------------------------------------
    def to_state(self) -> dict:
        """完整可序列化状态（存盘用）。"""
        return {
            "id": self.id, "label": self.label, "market": self.market,
            "symbol": self.symbol, "interval": self.interval,
            "initial": self.initial, "cash": self.cash, "qty": self.qty,
            "avg_price": self.avg_price, "realized": self.realized, "fees": self.fees,
            "peak": self.peak, "max_dd": self.max_dd, "bar_index": self.bar_index,
            "last_buy_bar": self.last_buy_bar, "auto": self.auto,
            "auto_mode": self.auto_mode, "auto_config": self.auto_config.to_dict(),
            "halted": self.halted, "peak_price": self.peak_price,
            "bars_held": self.bars_held, "bars_since_exit": self.bars_since_exit,
            "last_decision": self.last_decision,
            "last_decision_at": self.last_decision_at,
            "strategy": self.strategy, "_last_auto_target": self._last_auto_target,
            "created_at": self.created_at, "updated_at": self.updated_at,
            # series 不存（太大），但 source/bar_at 必须存，否则重启后丢失
            "source": self.source, "bar_at": self.bar_at,
            "orders": [asdict(o) for o in self.orders],
            "fills": [asdict(f) for f in self.fills],
            "logs": [asdict(l) for l in self.logs],
            "equity_curve": self.equity_curve,
            "price_series": self.price_series,
        }

    @classmethod
    def from_state(cls, st: dict) -> "PaperSession":
        s = cls(session_id=st["id"], market=st["market"], symbol=st["symbol"],
                interval=st["interval"], initial=st["initial"], label=st.get("label", ""))
        for k in ("cash", "qty", "avg_price", "realized", "fees", "peak", "max_dd",
                  "bar_index", "last_buy_bar", "auto", "strategy",
                  "_last_auto_target", "created_at", "updated_at",
                  "source", "bar_at", "auto_mode", "halted", "peak_price",
                  "bars_held", "bars_since_exit",
                  "last_decision", "last_decision_at"):
            if k in st:
                setattr(s, k, st[k])
        if st.get("auto_config"):
            try:
                from autotrader import AutoConfig as _AC, RiskConfig as _RC
                ac = _AC()
                rk = set(_RC().to_dict())
                for k, v in st["auto_config"].items():
                    if k == "risk" and isinstance(v, dict):
                        for rk2, rv in v.items():
                            if hasattr(ac.risk, rk2):
                                setattr(ac.risk, rk2, rv)
                    elif k in rk:
                        setattr(ac.risk, k, v)
                    elif hasattr(ac, k):
                        setattr(ac, k, v)
                s.auto_config = ac
            except Exception:
                pass
        if st.get("price_series"):
            s.price_series = st["price_series"]
        s.orders = [PaperOrder(**o) for o in st.get("orders", [])]
        s.fills = [PaperFill(**f) for f in st.get("fills", [])]
        s.logs = [PaperLog(**l) for l in st.get("logs", [])]
        s.equity_curve = st.get("equity_curve", [])
        return s


# ============================================================
class PaperStore:
    """会话管理器：创建/查找/删除 + 落盘。"""

    def __init__(self, state_file: Path = STATE_FILE, autosave: bool = True):
        self.path = state_file
        self.autosave = autosave
        self.sessions: dict[str, PaperSession] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf8"))
            for st in raw.get("sessions", []):
                try:
                    s = PaperSession.from_state(st)
                    self.sessions[s.id] = s
                except Exception as e:
                    print(f"  跳过损坏的会话 {st.get('id')}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  读取状态文件失败（忽略）: {e}", file=sys.stderr)

    def save(self) -> None:
        if not self.autosave:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"updatedAt": time.time(),
                    "sessions": [s.to_state() for s in self.sessions.values()]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf8")
            os.replace(tmp, self.path)      # 原子替换
        except Exception as e:
            print(f"  保存状态失败: {e}", file=sys.stderr)

    def create(self, **kw) -> PaperSession:
        if len(self.sessions) >= MAX_SESSIONS:
            # 淘汰最久未更新的
            oldest = min(self.sessions.values(), key=lambda s: s.updated_at)
            del self.sessions[oldest.id]
        s = PaperSession(**kw)
        self.sessions[s.id] = s
        self.save()
        return s

    def get(self, sid: str) -> PaperSession | None:
        return self.sessions.get(sid)

    def require(self, sid: str) -> PaperSession:
        s = self.sessions.get(sid)
        if s is None:
            raise KeyError(f"会话不存在：{sid}")
        return s

    def delete(self, sid: str) -> bool:
        if sid in self.sessions:
            del self.sessions[sid]
            self.save()
            return True
        return False

    def list(self) -> list[dict]:
        out = []
        for s in self.sessions.values():
            out.append({
                "id": s.id, "label": s.label, "market": s.market,
                "symbol": s.symbol, "interval": s.interval,
                "name": symbol_name(s.market, s.symbol),
                "equity": round(s.equity, 2), "initial": round(s.initial, 2),
                "totalReturn": round((s.equity / s.initial - 1) * 100, 3) if s.initial else 0,
                "inPosition": s.qty > 0, "auto": s.auto, "strategy": s.strategy,
                "barIndex": s.bar_index, "updatedAt": s.updated_at,
            })
        return sorted(out, key=lambda x: x["updatedAt"], reverse=True)


# ============================================================
def _selftest() -> int:
    import numpy as np
    from datasource import Bar

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
    print("  交互式模拟盘自检（不联网，用手工构造的行情）")
    print("=" * 78)

    def mk_session(prices, market="crypto", initial=100000.0):
        s = PaperSession(market=market, symbol="TEST", interval="1d", initial=initial)
        bars = tuple(Bar(1000 + i * 86400000, p, p, p, p, 1.0) for i, p in enumerate(prices))
        s.series = Series("test", market, "TEST", "测试", "1d", bars, 0.0)
        s.price_series = list(prices)
        s.bar_index = 0
        return s

    # --- 1. 市价买入立刻成交 ---
    print("\n[1] 市价单立即成交")
    s = mk_session([100., 100., 100.])
    o = s.place_order("buy", 10, "market")
    check("订单已成交", o.state == "filled", o.state)
    check("持仓 10", s.qty == 10, f"{s.qty}")
    check("现金减少", s.cash < s.initial, f"{s.cash:.2f}")
    check("均价 = 买入价（含滑点）", s.avg_price > 100, f"{s.avg_price:.4f}")
    check("有成交记录", len(s.fills) == 1, f"{len(s.fills)}")

    # --- 2. 挂单等待下一根 ---
    print("\n[2] next_open 挂单不立即成交")
    s2 = mk_session([100., 110., 120.])
    o2 = s2.place_order("buy", 10, "next_open")
    check("订单状态为 pending", o2.state == "pending", o2.state)
    check("尚无持仓", s2.qty == 0, f"{s2.qty}")
    check("现金未变", abs(s2.cash - s2.initial) < 1e-9)
    # 模拟推进到下一根
    s2.bar_index = 1
    s2.price_series = [100., 110., 120.]
    s2.series = Series("test", "crypto", "TEST", "测试", "1d",
                       tuple(Bar(1000 + i * 86400000, p, p, p, p, 1.0)
                             for i, p in enumerate([100., 110., 120.])), 0.0)
    s2._try_fill(o2, 1)
    check("推进后成交", o2.state == "filled", o2.state)
    check("成交价 = 第1根开盘 110", abs(o2.fill_price - 110.0 * (1 + s2.costs.slippage)) < 1e-9,
          f"{o2.fill_price}")

    # --- 3. 卖出结算已实现盈亏 ---
    print("\n[3] 卖出结算盈亏")
    s3 = mk_session([100., 100.])
    s3.place_order("buy", 10, "market")
    cost = s3.avg_price * 10
    s3.price_series = [120., 120.]          # 涨价
    s3.place_order("sell", 10, "market")
    check("已清仓", s3.qty == 0, f"{s3.qty}")
    check("已实现盈亏 > 0", s3.realized > 0, f"{s3.realized:.2f}")
    expect = (120 * (1 - s3.costs.slippage)) * 10 - cost - s3.fees
    check("盈亏量级合理（含手续费滑点）", abs(s3.realized - (120 * (1 - s3.costs.slippage) * 10 - cost - 0)) < 100,
          f"realized={s3.realized:.2f}")

    # --- 4. 资金不足被拒 ---
    print("\n[4] 风控：资金不足")
    s4 = mk_session([100.], initial=1000.0)
    o4 = s4.place_order("buy", 1000, "market")   # 需要 10 万
    check("被拒", o4.state == "rejected", o4.state)
    check("拒绝原因提到资金", "资金不足" in (o4.reject_reason or ""), o4.reject_reason)
    check("账户未变", abs(s4.cash - 1000.0) < 1e-9, f"{s4.cash}")

    # --- 5. 无持仓不能卖 ---
    print("\n[5] 风控：无持仓卖出")
    s5 = mk_session([100.])
    o5 = s5.place_order("sell", 10, "market")
    check("被拒", o5.state == "rejected", o5.state)
    check("原因提到无持仓", "无持仓" in (o5.reject_reason or ""), o5.reject_reason)

    # --- 6. 超量卖出被拒 ---
    print("\n[6] 风控：持仓不足")
    s6 = mk_session([100.])
    s6.place_order("buy", 10, "market")
    o6 = s6.place_order("sell", 50, "market")
    check("被拒", o6.state == "rejected", o6.state)
    check("原因提到持仓不足", "持仓不足" in (o6.reject_reason or ""), o6.reject_reason)

    # --- 7. A 股整手 ---
    print("\n[7] A 股整手约束")
    s7 = mk_session([100.], market="ashare", initial=100000.0)
    o7 = s7.place_order("buy", None, "market")     # 全仓
    check("持仓是 100 的整数倍", s7.qty % 100 == 0, f"{s7.qty}")
    check("成交", o7.state == "filled", o7.state)

    # --- 8. A 股 T+1 ---
    print("\n[8] A 股 T+1")
    s8 = mk_session([100., 100.], market="ashare", initial=100000.0)
    s8.place_order("buy", 100, "market")
    o8 = s8.place_order("sell", 100, "market")     # 同一根卖出
    check("同根卖出被拒", o8.state == "rejected", o8.state)
    check("原因提到 T+1", "T+1" in (o8.reject_reason or ""), o8.reject_reason)
    s8.bar_index = 1                                # 推进一天
    o8b = s8.place_order("sell", 100, "market")
    check("隔日卖出成交", o8b.state == "filled", o8b.state)

    # --- 9. 撤单 ---
    print("\n[9] 撤单")
    s9 = mk_session([100.])
    o9 = s9.place_order("buy", 10, "next_open")
    check("撤单成功", s9.cancel_order(o9.id))
    check("状态为 cancelled", o9.state == "cancelled", o9.state)
    check("重复撤单失败", not s9.cancel_order(o9.id))

    # --- 10. 重置 ---
    print("\n[10] 重置账户")
    s10 = mk_session([100.])
    s10.place_order("buy", 10, "market")
    s10.reset()
    check("现金回到初始", abs(s10.cash - s10.initial) < 1e-9, f"{s10.cash}")
    check("清仓", s10.qty == 0)
    check("成交记录清空", len(s10.fills) == 0)
    check("已实现归零", abs(s10.realized) < 1e-9)

    # --- 11. 快照字段完整 ---
    print("\n[11] 快照字段")
    s11 = mk_session([100., 110.])
    s11.place_order("buy", 10, "market")
    snap = s11.snapshot()
    need = ["initial", "cash", "qty", "avgPrice", "price", "equity", "unrealized",
            "realized", "fees", "totalReturn", "maxDrawdown", "tradeCount",
            "winRate", "barIndex", "auto", "strategy", "pendingOrders",
            "recentFills", "logs", "equityCurve"]
    miss = [k for k in need if k not in snap]
    check("关键字段齐全", not miss, f"缺 {miss}")
    check("JSON 可序列化", isinstance(json.dumps(snap), str))

    # --- 12. 存/取状态 ---
    print("\n[12] 状态持久化")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store = PaperStore(Path(td) / "s.json")
        a = store.create(market="crypto", symbol="TEST", interval="1d", initial=50000.0)
        a.series = Series("test", "crypto", "TEST", "测试", "1d",
                          (Bar(1000, 100., 100., 100., 100., 1.),), 0.0)
        a.price_series = [100.0]
        a.place_order("buy", 5, "market")
        store.save()
        store2 = PaperStore(Path(td) / "s.json")
        b = store2.get(a.id)
        check("会话被恢复", b is not None)
        check("持仓恢复", b and b.qty == 5, f"{b.qty if b else '—'}")
        check("现金恢复", b and abs(b.cash - a.cash) < 1e-6)
        check("成交记录恢复", b and len(b.fills) == len(a.fills))

    # --- 13. 自动交易开关 ---
    print("\n[13] 自动交易开关")
    s13 = mk_session([100.] * 30)
    r = s13.set_auto(True, "rsi_reversion")
    check("自动已开", r["auto"] is True)
    check("模式默认 score", r["mode"] == "score", r["mode"])
    check("初始目标已对齐（不立刻下单）", len([o for o in s13.orders if o.state == "pending"]) == 0,
          f"{len(s13.orders)} 单")
    s13.set_auto(False)
    check("自动可关闭", s13.auto is False)

    # --- 14. 自动逻辑：上涨行情应当建仓 ---
    print("\n[14] 自动逻辑接入后会真的下单")
    up_px = [100. * (1.012 ** i) for i in range(120)]
    s14 = mk_session(up_px, initial=100000.0)
    s14.set_auto(True, mode="score")
    dec = s14._auto_step(119)              # 第一根之后，正式决策
    pend = [o for o in s14.orders if o.state == "pending" and o.side == "buy"]
    check("上涨行情发出买入委托", len(pend) >= 1, f"{len(pend)} 笔待成交")
    check("决策对象带打分", dec is not None and abs(dec.get("score", 0)) > 0,
          f"score={dec.get('score') if dec else None}")
    check("决策对象带因子明细", dec is not None and len(dec.get("factors", {})) >= 4,
          f"{len(dec.get('factors', {}))} 个因子")
    check("决策对象带可读理由", dec is not None and len(dec.get("reasons", [])) >= 2)
    # 推进一根让委托成交
    s14.bar_index = 119
    s14.price_series = up_px
    for o in list(s14.orders):
        if o.state == "pending":
            s14._try_fill(o, 119)
    check("委托成交后建仓", s14.qty > 0, f"持仓 {s14.qty}")
    check("仓位比例接近目标", 0.5 <= s14._position_fraction() <= 1.0,
          f"{s14._position_fraction():.0%}")

    # --- 15. 自动逻辑：风控能强制平仓 ---
    print("\n[15] 风控能在自动模式下强制平仓")
    # 资金要够：120 根 ×1.2% 复利后价格是初始的 4.1 倍，
    # 95% 仓位按 10 万本金买不起（成交时会因资金不足被拒）—— 这是风控正确行为。
    s15 = mk_session(up_px, initial=2_000_000.0)
    s15.set_auto(True, mode="score")
    # ⚠️ set_auto 只做「目标对齐」（第 0 根指标还没预热，目标必然是 0%），
    #    不会下单。必须再跑一次正式决策 —— 这里就是漏了这一步导致
    #    「先有持仓」拿到 0，后面两条跟着一起失败。
    s15._auto_step(119)
    s15.bar_index = 119
    s15.price_series = up_px
    for o in list(s15.orders):
        if o.state == "pending":
            s15._try_fill(o, 119)
    check("先有持仓", s15.qty > 0, f"{s15.qty}")
    # 人为把成本抬高，制造 -30% 浮亏
    s15.avg_price = up_px[-1] * 1.45
    s15.peak_price = up_px[-1]
    dec = s15._auto_step(119)
    sells = [o for o in s15.orders if o.state == "pending" and o.side == "sell"]
    check("触发止损并挂出卖单", len(sells) >= 1, f"{len(sells)} 笔")
    check("决策标注风控", dec is not None and dec.get("riskHit"),
          (dec or {}).get("riskHit") or "未标注")

    # --- 16. 自动配置可调且持久化 ---
    print("\n[16] 自动配置可调 + 持久化")
    s16 = mk_session([100.] * 30)
    r = s16.set_auto(True, mode="confirm", strategy="macd_trend",
                     config={"confirm_min": 3, "fixed_fraction": 0.6,
                             "stop_loss": 0.04, "cooldown_bars": 5})
    cfg = r["config"]
    check("模式已切到 confirm", r["mode"] == "confirm")
    check("confirm_min 生效", cfg["confirm_min"] == 3, cfg["confirm_min"])
    check("fixed_fraction 生效", cfg["fixed_fraction"] == 0.6)
    check("stop_loss 生效（嵌套 risk）", cfg["risk"]["stop_loss"] == 0.04,
          cfg["risk"]["stop_loss"])
    check("cooldown_bars 生效", cfg["risk"]["cooldown_bars"] == 5)
    st = s16.to_state()
    check("状态里含 auto_config", "auto_config" in st)
    s16b = PaperSession.from_state(st)
    check("恢复后模式保留", s16b.auto_mode == "confirm", s16b.auto_mode)
    check("恢复后 confirm_min 保留", s16b.auto_config.confirm_min == 3,
          s16b.auto_config.confirm_min)
    check("恢复后 stop_loss 保留",
          abs(s16b.auto_config.risk.stop_loss - 0.04) < 1e-9,
          s16b.auto_config.risk.stop_loss)

    # --- 17. 非法配置被拒 ---
    print("\n[17] 非法输入被拒")
    s17 = mk_session([100.] * 10)
    try:
        s17.set_auto(True, mode="不存在的模式")
        check("非法模式应抛错", False, "没抛错")
    except ValueError:
        check("非法模式抛 ValueError", True)
    try:
        s17.set_auto(True, strategy="不存在的策略")
        check("非法策略应抛错", False, "没抛错")
    except ValueError:
        check("非法策略抛 ValueError", True)

    print("\n" + "=" * 78)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 78)
    return 1 if fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("这是模块，不是 CLI。用 --selftest 跑自检，或通过 server.mjs 的 API 使用。")
