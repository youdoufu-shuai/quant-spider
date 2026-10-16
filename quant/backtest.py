#!/usr/bin/env python3
"""
回测引擎 —— 用真实历史价格评估策略，算真实绩效指标。

═══════════════════════════════════════════════════════════════════
  一、最重要的规则：禁止未来函数（look-ahead bias）
═══════════════════════════════════════════════════════════════════
第 i 根收盘产生的信号，**只能在第 i+1 根开盘成交**。

为什么要这样：策略看到的是收盘价，而收盘那一刻已经无法再以该价格成交了。
如果用「第 i 根收盘价」成交，等于假设自己能预知收盘价并刚好在那里成交 ——
回测收益会被严重高估。这是量化回测里最常见的自欺方式。

本引擎强制 shift 一根，且**不提供关闭开关**。想绕过要改源码，改不了配置。

═══════════════════════════════════════════════════════════════════
  二、交易成本必须建模
═══════════════════════════════════════════════════════════════════
  手续费：A股默认万三（0.03%）双边；加密默认千一（0.1%）
  滑点：  成交价向不利方向偏移。买入抬价、卖出压价。
  最小佣金：A股 5 元起（这里按比例近似，小于单笔最小额时按最低收）

不算成本的回测毫无意义 —— 高频策略的收益常常全部被成本吃掉。

═══════════════════════════════════════════════════════════════════
  三、绩效指标定义
═══════════════════════════════════════════════════════════════════
  总收益      (最终权益 / 初始 - 1) × 100%
  年化收益    按实际天数折算，不是简单乘 252
  最大回撤    权益曲线从历史最高点的最大跌幅（peak-to-trough）
  夏普        (年化收益 - 无风险利率) / 年化波动率
  索提诺      只用下行波动率，比夏普更贴近「亏钱的风险」
  卡玛        年化收益 / 最大回撤
  胜率        盈利交易数 / 已平仓交易数
  盈亏比      平均盈利 / 平均亏损
  期望        胜率×平均盈利 - 败率×平均亏损（每笔的数学期望）

未平仓的持仓**不计入胜率统计** —— 否则浮动盈亏会污染交易统计。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, asdict

import numpy as np

from datasource import Series, fetch, MARKETS
from indicators import compute
from strategy import SignalSet, run as run_strategy, STRATEGIES, run_all

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
@dataclass
class Costs:
    """交易成本。默认按 A 股散户水平。"""
    commission: float = 0.0003     # 手续费率（单边）
    slippage: float = 0.0005       # 滑点率（单边）
    min_commission: float = 0.0    # 单笔最低佣金（A股为 5 元，这里按金额比例近似）

    def buy_price(self, raw: float) -> float:
        """买入：价格上滑。"""
        return raw * (1 + self.slippage)

    def sell_price(self, raw: float) -> float:
        """卖出：价格下滑。"""
        return raw * (1 - self.slippage)

    def fee(self, notional: float) -> float:
        return max(abs(notional) * self.commission, self.min_commission)


# 各市场的合理默认成本
MARKET_COSTS = {
    "ashare": Costs(commission=0.0003, slippage=0.0005, min_commission=0.0),
    "crypto": Costs(commission=0.0010, slippage=0.0005, min_commission=0.0),
}


@dataclass
class Trade:
    """一笔已完成的交易（一买一卖）。"""
    entry_index: int
    entry_time: str
    entry_price: float
    exit_index: int
    exit_time: str
    exit_price: float
    shares: float
    pnl: float            # 已扣除手续费与滑点
    pnl_pct: float
    bars_held: int
    entry_reason: str
    exit_reason: str

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass
class BacktestResult:
    strategy: str
    label: str
    market: str
    symbol: str
    name: str
    interval: str
    source: str
    bar_count: int
    period: str

    initial: float
    final: float
    total_return: float
    annual_return: float
    benchmark_return: float        # 买入持有
    excess_return: float           # 相对基准

    max_drawdown: float
    max_drawdown_at: str
    volatility: float
    sharpe: float
    sortino: float
    calmar: float

    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float           # 盈亏比（平均盈利/平均亏损）
    payoff: float                  # 总盈利/总亏损
    expectancy: float              # 每笔期望收益
    best_trade: float
    worst_trade: float
    avg_bars_held: float
    total_fees: float
    cost_drag: float               # 成本吃掉的收益百分点

    exposure: float                # 持仓时间占比
    open_position: bool
    costs: dict
    equity_curve: list[float] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("equity_curve", None)
        d.pop("dates", None)
        d.pop("trades", None)
        return d


# ============================================================
def _annualize_factor(interval: str) -> float:
    """一根 K 线代表多少年。用于年化。"""
    return {
        "1m": 1 / (60 * 24 * 365),
        "3m": 3 / (60 * 24 * 365),
        "5m": 5 / (60 * 24 * 365),
        "15m": 15 / (60 * 24 * 365),
        "30m": 30 / (60 * 24 * 365),
        "60m": 1 / (24 * 365),
        "1h": 1 / (24 * 365),
        "4h": 4 / (24 * 365),
        "1d": 1 / 252,       # 交易日
        "1w": 1 / 52,
        "1M": 1 / 12,
    }.get(interval, 1 / 252)


def _max_drawdown(equity: np.ndarray) -> tuple[float, int]:
    """返回 (最大回撤比例, 触底位置)。"""
    peak = equity[0]
    worst = 0.0
    worst_idx = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < worst:
            worst = dd
            worst_idx = i
    return abs(worst), worst_idx


def run_backtest(series: Series, signals: SignalSet,
                 costs: Costs | None = None,
                 initial: float = 100_000.0,
                 risk_free: float = 0.02,
                 allow_short: bool = False,
                 _measure_cost_drag: bool = True) -> BacktestResult:
    """
    执行回测。

    资金模型：全仓进出（每次用全部可用资金买入，卖出时全部平掉）。
    这样得到的收益曲线最直观，也最容易和买入持有对比。

    执行时点：信号在第 i 根收盘产生 -> 第 i+1 根**开盘**成交（强制）。
    """
    if costs is None:
        costs = MARKET_COSTS.get(series.market, Costs())

    n = len(series.bars)
    if n < 3:
        raise ValueError(f"K 线太少（{n} 根），无法回测")

    opens = np.array([b.open for b in series.bars], dtype=float)
    closes = np.array([b.close for b in series.bars], dtype=float)
    iso = [b.as_dict()["iso"] for b in series.bars]

    pos_target = signals.position          # 目标仓位（第 i 根收盘后决定）
    cash = initial
    shares = 0.0
    equity = np.zeros(n)
    total_fees = 0.0

    trades: list[Trade] = []
    open_trade: dict | None = None
    bars_in_market = 0

    for i in range(n):
        # ---- 先用「昨收信号」在第 i 根开盘执行 ----
        if i >= 1:
            want = int(pos_target[i - 1])       # 关键：i-1 的信号
            held = 1 if shares > 0 else 0

            if want == 1 and held == 0:
                px = costs.buy_price(opens[i])
                if px > 0:
                    qty = cash / (px * (1 + costs.commission))
                    fee = costs.fee(qty * px)
                    cash -= qty * px + fee
                    total_fees += fee
                    shares = qty
                    open_trade = {
                        "index": i, "time": iso[i], "price": px, "shares": qty,
                        "reason": signals.reasons[i - 1] if i - 1 < len(signals.reasons) else "",
                    }
            elif want == 0 and held == 1:
                px = costs.sell_price(opens[i])
                gross = shares * px
                fee = costs.fee(gross)
                cash += gross - fee
                total_fees += fee
                if open_trade:
                    cost_basis = open_trade["shares"] * open_trade["price"]
                    pnl = gross - fee - cost_basis
                    trades.append(Trade(
                        entry_index=open_trade["index"], entry_time=open_trade["time"],
                        entry_price=open_trade["price"],
                        exit_index=i, exit_time=iso[i], exit_price=px,
                        shares=open_trade["shares"], pnl=pnl,
                        pnl_pct=pnl / cost_basis * 100 if cost_basis else 0.0,
                        bars_held=i - open_trade["index"],
                        entry_reason=open_trade["reason"],
                        exit_reason=signals.reasons[i - 1] if i - 1 < len(signals.reasons) else "",
                    ))
                shares = 0.0
                open_trade = None

        if shares > 0:
            bars_in_market += 1
        equity[i] = cash + shares * closes[i]

    # ---- 未平仓：按最后收盘价计入权益，但**不计入交易统计** ----
    open_position = shares > 0

    # ---- 绩效 ----
    final = float(equity[-1])
    total_return = (final / initial - 1) * 100

    bars_per_year = 1.0 / _annualize_factor(series.interval)
    years = n / bars_per_year
    annual_return = ((final / initial) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0

    returns = np.diff(equity) / equity[:-1]
    returns = returns[np.isfinite(returns)]
    vol = float(np.std(returns, ddof=1)) * math.sqrt(bars_per_year) * 100 if returns.size > 1 else 0.0

    downside = returns[returns < 0]
    dvol = float(np.std(downside, ddof=1)) * math.sqrt(bars_per_year) * 100 if downside.size > 1 else 0.0

    sharpe = (annual_return - risk_free * 100) / vol if vol > 0 else 0.0
    sortino = (annual_return - risk_free * 100) / dvol if dvol > 0 else 0.0

    mdd, mdd_idx = _max_drawdown(equity)
    mdd_pct = mdd * 100
    calmar = annual_return / mdd_pct if mdd_pct > 1e-9 else 0.0

    # 基准：买入持有（同样扣一次成本，公平对比）
    benchmark = (closes[-1] / costs.buy_price(opens[0]) - 1) * 100

    wins = [t for t in trades if t.win]
    losses = [t for t in trades if not t.win]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    payoff = gross_win / gross_loss if gross_loss > 1e-12 else 0.0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    expectancy = (win_rate / 100) * avg_win - (1 - win_rate / 100) * abs(avg_loss)

    # 成本对收益的影响：用零成本重跑一次做差（真实测量，不是估算）
    #
    # ⚠️ 这里必须用 _measure_cost_drag=False 切断递归：
    #    否则内部这次调用又会去算自己的 cost_drag，无限递归直到 RecursionError。
    #    实测踩过这个坑。
    if _measure_cost_drag:
        zero = Costs(commission=0.0, slippage=0.0, min_commission=0.0)
        r0 = run_backtest(series, signals, costs=zero, initial=initial,
                          risk_free=risk_free, allow_short=allow_short,
                          _measure_cost_drag=False)
        cost_drag = r0.total_return - total_return
    else:
        cost_drag = 0.0

    return BacktestResult(
        strategy=signals.name, label=signals.label,
        market=series.market, symbol=series.symbol, name=series.name,
        interval=series.interval, source=series.source,
        bar_count=n, period=f"{iso[0]} → {iso[-1]}",
        initial=initial, final=round(final, 2),
        total_return=round(total_return, 3),
        annual_return=round(annual_return, 3),
        benchmark_return=round(benchmark, 3),
        excess_return=round(total_return - benchmark, 3),
        max_drawdown=round(mdd_pct, 3),
        max_drawdown_at=iso[mdd_idx],
        volatility=round(vol, 3),
        sharpe=round(sharpe, 3),
        sortino=round(sortino, 3),
        calmar=round(calmar, 3),
        trade_count=len(trades),
        win_count=len(wins), loss_count=len(losses),
        win_rate=round(win_rate, 2),
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        profit_factor=round(profit_factor, 3),
        payoff=round(payoff, 3),
        expectancy=round(expectancy, 2),
        best_trade=round(max((t.pnl for t in trades), default=0.0), 2),
        worst_trade=round(min((t.pnl for t in trades), default=0.0), 2),
        avg_bars_held=round(float(np.mean([t.bars_held for t in trades])), 1) if trades else 0.0,
        total_fees=round(total_fees, 2),
        cost_drag=round(cost_drag, 3),
        exposure=round(bars_in_market / n * 100, 2),
        open_position=open_position,
        costs={"commission": costs.commission, "slippage": costs.slippage},
        equity_curve=[round(float(x), 2) for x in equity],
        dates=iso,
        trades=[asdict(t) for t in trades],
    )


# ============================================================
#  自检：用手算得出来的场景验证引擎没算错
# ============================================================
def _selftest() -> int:
    from datasource import Bar, Series
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    print("=" * 76)
    print("  回测引擎自检（手算可验证的场景）")
    print("=" * 76)

    # --- 场景 1：单调上涨，全程持仓 -> 收益应为 0 成本下的价格涨幅 ---
    print("\n[场景1] 10 根单调上涨，全程持仓，零成本")
    bars = tuple(Bar(1000 + i * 86400000, 100 + i * 10, 100 + i * 10,
                     100 + i * 10, 100 + i * 10, 1.0) for i in range(10))
    s = Series("test", "crypto", "T", "测试", "1d", bars, 0.0)
    pos = np.ones(10, dtype=np.int8)
    sig = SignalSet("t", "测试", "", pos, np.zeros(10, bool), np.zeros(10, bool), [""] * 10, {})
    r = run_backtest(s, sig, costs=Costs(0, 0), initial=10000.0)
    # 第 0 根收盘信号 -> 第 1 根开盘(110)买入；第 9 根收盘算权益(190)
    expect = (190 / 110 - 1) * 100
    check("收益 = 从第1根开盘到末根收盘", abs(r.total_return - expect) < 0.01,
          f"实测 {r.total_return:.3f}%  期望 {expect:.3f}%")
    check("未平仓标记正确", r.open_position)
    check("未平仓不计入交易数", r.trade_count == 0, f"{r.trade_count}")

    # --- 场景 2：滑点方向必须对（买入抬价、卖出压价）---
    print("\n[场景2] 滑点方向验证")
    c = Costs(commission=0.0, slippage=0.01)
    check("买入价上滑 1%", abs(c.buy_price(100) - 101.0) < 1e-9, f"{c.buy_price(100)}")
    check("卖出价下滑 1%", abs(c.sell_price(100) - 99.0) < 1e-9, f"{c.sell_price(100)}")

    # 有滑点时收益必须**低于**零滑点
    r_slip = run_backtest(s, sig, costs=Costs(0.0, 0.01), initial=10000.0)
    check("有滑点收益低于无滑点", r_slip.total_return < r.total_return,
          f"{r_slip.total_return:.3f}% < {r.total_return:.3f}%")

    # --- 场景 3：手续费必须真的扣掉 ---
    print("\n[场景3] 手续费扣减")
    r_fee = run_backtest(s, sig, costs=Costs(0.001, 0.0), initial=10000.0)
    check("有手续费收益低于零成本", r_fee.total_return < r.total_return,
          f"{r_fee.total_return:.3f}% < {r.total_return:.3f}%")
    check("累计手续费 > 0", r_fee.total_fees > 0, f"{r_fee.total_fees}")

    # --- 场景 4：一买一卖应该有 1 笔交易，且盈亏符号正确 ---
    print("\n[场景4] 一笔完整交易")
    pos2 = np.array([1, 1, 1, 1, 1, 0, 0, 0, 0, 0], dtype=np.int8)
    sig2 = SignalSet("t", "测试", "", pos2,
                     np.array([1, 0, 0, 0, 0, 0, 0, 0, 0, 0], bool),
                     np.array([0, 0, 0, 0, 0, 1, 0, 0, 0, 0], bool), [""] * 10, {})
    r2 = run_backtest(s, sig2, costs=Costs(0, 0), initial=10000.0)
    check("恰好 1 笔交易", r2.trade_count == 1, f"{r2.trade_count}")
    check("上涨趋势中该笔盈利", r2.trades[0]["pnl"] > 0, f"pnl={r2.trades[0]['pnl']:.2f}")
    check("胜率 100%", abs(r2.win_rate - 100.0) < 1e-9, f"{r2.win_rate}%")
    check("已平仓后 open_position=False", not r2.open_position)

    # --- 场景 5：最大回撤手算 ---
    print("\n[场景5] 最大回撤手算（100 -> 150 -> 75）")
    eq = np.array([100., 150., 75.])
    dd, idx = _max_drawdown(eq)
    check("回撤 = (150-75)/150 = 50%", abs(dd - 0.5) < 1e-12, f"{dd}")
    check("触底位置正确", idx == 2, f"idx={idx}")

    eq2 = np.array([100., 90., 120., 60.])
    dd2, _ = _max_drawdown(eq2)
    check("第二段回撤 (120-60)/120=50% 被捕获", abs(dd2 - 0.5) < 1e-12, f"{dd2}")

    # --- 场景 6：下跌策略应该亏，且基准对比合理 ---
    print("\n[场景6] 单调下跌")
    bars_dn = tuple(Bar(1000 + i * 86400000, 200 - i * 10, 200 - i * 10,
                        200 - i * 10, 200 - i * 10, 1.0) for i in range(10))
    sd = Series("test", "crypto", "T", "测试", "1d", bars_dn, 0.0)
    rd = run_backtest(sd, sig, costs=Costs(0, 0), initial=10000.0)
    check("下跌趋势全程持仓必亏", rd.total_return < 0, f"{rd.total_return:.3f}%")
    check("基准也是负的", rd.benchmark_return < 0, f"{rd.benchmark_return:.3f}%")

    # --- 场景 7：不成交时收益应为 0 ---
    print("\n[场景7] 全程空仓")
    sig_flat = SignalSet("t", "测试", "", np.zeros(10, dtype=np.int8),
                         np.zeros(10, bool), np.zeros(10, bool), [""] * 10, {})
    rf = run_backtest(s, sig_flat, costs=Costs(0, 0), initial=10000.0)
    check("空仓收益 = 0", abs(rf.total_return) < 1e-9, f"{rf.total_return}")
    check("空仓无交易", rf.trade_count == 0)
    check("持仓时间占比 = 0", abs(rf.exposure) < 1e-9, f"{rf.exposure}")

    print("\n" + "=" * 76)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 76)
    return 1 if fail else 0


# ============================================================
def _print_result(r: BacktestResult) -> None:
    print(f"\n  【{r.label}】")
    print(f"    区间      {r.period}   {r.bar_count} 根 {r.interval}   源={r.source}")
    print(f"    总收益    {r.total_return:>9.2f}%      基准(买入持有) {r.benchmark_return:>8.2f}%")
    print(f"    年化      {r.annual_return:>9.2f}%      超额          {r.excess_return:>8.2f}%")
    print(f"    最大回撤  {r.max_drawdown:>9.2f}%  @ {r.max_drawdown_at}")
    print(f"    波动率    {r.volatility:>9.2f}%      夏普 {r.sharpe:>6.2f}  索提诺 {r.sortino:>6.2f}  卡玛 {r.calmar:>6.2f}")
    print(f"    交易      {r.trade_count:>4} 笔   胜率 {r.win_rate:>5.1f}%  ({r.win_count}胜/{r.loss_count}负)")
    print(f"    盈亏      avgWin {r.avg_win:>10,.0f}  avgLoss {r.avg_loss:>10,.0f}  盈亏比 {r.profit_factor:.2f}")
    print(f"    期望      {r.expectancy:>10,.2f} / 笔   最好 {r.best_trade:,.0f}  最差 {r.worst_trade:,.0f}")
    print(f"    持仓时间  {r.exposure:>9.2f}%      平均持有 {r.avg_bars_held:.1f} 根")
    print(f"    成本      手续费 {r.total_fees:>10,.2f}   成本拖累 {r.cost_drag:.2f} 个百分点")
    if r.open_position:
        print(f"    ⚠ 期末仍有持仓，该笔未计入交易统计")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="策略回测（真实历史数据）")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--strategy", default="all", choices=["all", *STRATEGIES])
    ap.add_argument("--initial", type=float, default=100_000.0)
    ap.add_argument("--json", action="store_true", help="输出 JSON（供前端读取）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    series = fetch(args.market, symbol, interval, args.limit)
    ind = compute(series)
    sigs = run_all(ind) if args.strategy == "all" else {args.strategy: run_strategy(args.strategy, ind)}

    results = []
    for key, sig in sigs.items():
        try:
            results.append(run_backtest(series, sig, initial=args.initial))
        except Exception as e:
            print(f"  回测 {key} 失败: {type(e).__name__}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "meta": series.meta(),
            "results": [r.summary() for r in results],
        }, ensure_ascii=False, indent=2))
        return 0

    m = series.meta()
    print("=" * 78)
    print(f"  回测  {m['name']}  {m['symbol']}  {m['interval']}")
    print(f"  {m['first']} → {m['last']}   {m['count']} 根   源={m['source']}   初始资金 {args.initial:,.0f}")
    print("  执行规则：第 i 根收盘信号 → 第 i+1 根开盘成交（禁止未来函数）")
    print("=" * 78)

    for r in results:
        _print_result(r)

    if results:
        best = max(results, key=lambda x: x.sharpe)
        print("\n" + "=" * 78)
        print(f"  夏普最高：{best.label}  (夏普 {best.sharpe:.2f} / 收益 {best.total_return:.2f}% / 回撤 {best.max_drawdown:.2f}%)")
        beat = [r for r in results if r.excess_return > 0]
        print(f"  跑赢买入持有的策略：{len(beat)}/{len(results)}")
        print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
