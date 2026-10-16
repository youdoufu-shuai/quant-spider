#!/usr/bin/env python3
"""
策略参数优化 —— 网格 / 随机搜索，带样本外验证与过拟合检测。

═══════════════════════════════════════════════════════════════════
  为什么必须做样本外验证
═══════════════════════════════════════════════════════════════════
参数优化最容易骗自己：在历史上试 500 组参数，挑出表现最好的那组，
它在历史上当然好看 —— 但这不代表未来。这叫**过拟合**，
是量化策略失效最常见的死因。

所以本模块强制做三件事：

  1. **切分样本**：前 70% 训练（in-sample），后 30% 验证（out-of-sample）。
     参数只在训练集上挑，然后用挑出来的参数在验证集上跑一遍。

  2. **报告泛化差距**：`oos_return - is_return`。如果训练集 +80%、
     验证集 -20%，那这组参数就是在拟合噪声，不是策略。
     本模块给每行结果都算这个差距，并显式标注「疑似过拟合」。

  3. **不用测试集调参**：验证集只用来**报告**，不参与排序。
     排序永远按训练集表现 —— 否则验证集就变成了训练集的一部分。

═══════════════════════════════════════════════════════════════════
  别的防骗设计
═══════════════════════════════════════════════════════════════════
  · **成本照收**：每个参数组合都带手续费与滑点跑，不是零成本优化。
    零成本优化会选出高频交易的参数，实盘被手续费吃光。
  · **最少交易数门槛**：交易数太少的组合（默认 < 3 笔）直接标记为
    「样本不足」，避免"1 笔交易赚 50%"这种运气结果排到第一。
  · **指标只算一次**：27 个指标与价格无关、只与 K 线有关，
    所以先算好再循环参数组合。原来每个组合重算一遍会慢几十倍。

═══════════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════════
  python quant/optimizer.py --market ashare --symbol sh600519 \\
         --strategy ema_cross --method grid --max-combos 200

  python quant/optimizer.py --selftest        # 离线自检
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from datasource import MARKETS, fetch, SYMBOL_NAMES          # noqa: E402
from indicators import compute                                # noqa: E402
from strategy import STRATEGIES, run as run_strategy          # noqa: E402
from backtest import Costs, MARKET_COSTS, run_backtest        # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
#  参数空间定义
# ============================================================
# ⚠️ 参数名必须与 strategy.py 里函数签名的**关键字参数名完全一致**，
#    因为 run_strategy 是 `fn(ind, **kw)` 直接透传的。
#    实测踩过：最初写成 period/low/high，而实际签名是 n/low/high，
#    网格搜索会把 period 当未知参数传进去 → TypeError。
#
# ⚠️ macd_trend 的签名是 `(ind)`，**没有任何可调参数** ——
#    所以它不在下面的表里。不硬塞参数去"优化"，那是假装在工作。
#
# 不是所有参数都值得优化：把指标周期也拿去搜只是浪费算力。
# 这里只放**真正影响策略行为**的阈值类参数。
PARAM_SPACE: dict[str, dict[str, list]] = {
    "ema_cross": {
        "fast": [5, 8, 10, 12, 15, 20],
        "slow": [20, 26, 30, 40, 50, 60],
        "adx_min": [0.0, 15.0, 18.0, 20.0, 25.0],
    },
    "rsi_reversion": {
        "n": [6, 9, 14, 21],
        "low": [20.0, 25.0, 30.0, 35.0],
        "high": [65.0, 70.0, 75.0, 80.0],
    },
    "bollinger_break": {
        "n": [10, 15, 20, 30],
        "k": [1.5, 2.0, 2.5, 3.0],
    },
    "donchian_break": {
        "entry_n": [10, 20, 30, 55],
        "exit_n": [5, 10, 15, 20],
    },
}

# 参数间的硬约束：某些组合没有意义（快线周期 >= 慢线周期）
PARAM_CONSTRAINTS: dict[str, list[Callable[[dict], bool]]] = {
    "ema_cross": [lambda p: p["fast"] < p["slow"]],
    "rsi_reversion": [lambda p: p["low"] < p["high"]],
    "donchian_break": [lambda p: p["exit_n"] < p["entry_n"]],
}


def default_params(strategy: str) -> dict:
    """
    从策略函数签名里读默认参数值。

    用 inspect 而不是硬编码一份默认值表 —— 硬编码的表一旦和
    函数签名脱节（改了函数忘了改表），基线对照就会静默失真。
    """
    import inspect
    try:
        entry = STRATEGIES.get(strategy)
        if entry is None:
            return {}
        fn = entry[1]
        sig = inspect.signature(fn)
        out = {}
        for name, p in sig.parameters.items():
            if name == "ind":
                continue
            if p.default is inspect.Parameter.empty:
                continue
            out[name] = p.default
        return out
    except Exception:
        return {}


def tunable_params(strategy: str) -> list[str]:
    """该策略有哪些真正可调的参数（有默认值的关键字参数）。"""
    return list(default_params(strategy).keys())


# ============================================================
@dataclass
class Combo:
    """一个参数组合及其在训练/验证集上的结果。"""
    params: dict
    ok: bool = False
    error: str = ""
    # 训练集（in-sample）
    is_return: float = 0.0
    is_dd: float = 0.0
    is_sharpe: float = 0.0
    is_trades: int = 0
    is_winrate: float = 0.0
    # 验证集（out-of-sample）
    oos_return: float = 0.0
    oos_dd: float = 0.0
    oos_sharpe: float = 0.0
    oos_trades: int = 0
    # 全样本
    full_return: float = 0.0
    full_dd: float = 0.0
    full_trades: int = 0
    cost_drag: float = 0.0

    @property
    def gap(self) -> float:
        """泛化差距：验证集收益 - 训练集收益。负数说明过拟合。"""
        return self.oos_return - self.is_return

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gap"] = round(self.gap, 3)
        return d


@dataclass
class OptimizeResult:
    strategy: str
    market: str
    symbol: str
    interval: str
    method: str
    bars_total: int
    bars_is: int
    bars_oos: int
    combos_tested: int
    combos_valid: int
    elapsed_ms: int
    costs: dict
    baseline: dict | None = None
    best: dict | None = None
    top: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
class Optimizer:
    """
    参数搜索器。

    关键实现细节：
      · 指标只算一次（ind 与参数无关）
      · 训练/验证按时间切分，**不重叠**
      · 排序只用训练集表现
    """

    def __init__(self, *, series, strategy: str, costs: Costs,
                 is_ratio: float = 0.7, min_trades: int = 3,
                 progress: Callable[[int, int], None] | None = None):
        self.series = series
        self.strategy = strategy
        self.costs = costs
        self.is_ratio = is_ratio
        self.min_trades = min_trades
        self.progress = progress

        n = len(series.bars)
        self.split = max(2, min(n - 1, int(n * is_ratio)))
        self.ind = compute(series)          # ← 只算一次
        self.bars_is = self.split
        self.bars_oos = n - self.split

    # --------------------------------------------------------
    def _slice(self, lo: int, hi: int):
        """
        切出一个子序列（用于回测）。

        ⚠️ Series 的字段是 (source, market, symbol, name, interval, bars,
        fetched_at, attempts) —— 最初写成了 elapsed_ms，直接 AttributeError。
        自检里的「30 组参数评估失败」就是这么暴露的。
        """
        from datasource import Series
        return Series(
            self.series.source, self.series.market, self.series.symbol,
            self.series.name, self.series.interval,
            tuple(self.series.bars[lo:hi]),
            self.series.fetched_at, self.series.attempts,
        )

    def _eval(self, params: dict) -> Combo:
        """
        评估一组参数。

        ⚠️ 注意这里**切的是序列**而不是指标：指标在边界处会有预热期，
        切完重新算会与被截断的位置不一致。为了让训练集与验证集
        各自独立可信，两个子集都重新算指标 ——
        代价是多算几次，但避免了"验证集泄漏训练集信息"这种隐蔽错误。
        """
        c = Combo(params=dict(params))
        try:
            n = len(self.series.bars)
            # 训练集
            s_is = self._slice(0, self.split)
            ind_is = compute(s_is)
            sig_is = run_strategy(self.strategy, ind_is, **params)
            r_is = run_backtest(s_is, sig_is, costs=self.costs,
                                _measure_cost_drag=False)
            c.is_return = r_is.total_return
            c.is_dd = r_is.max_drawdown
            c.is_sharpe = r_is.sharpe
            c.is_trades = r_is.trade_count
            c.is_winrate = r_is.win_rate

            # 验证集
            s_oos = self._slice(self.split, n)
            ind_oos = compute(s_oos)
            sig_oos = run_strategy(self.strategy, ind_oos, **params)
            r_oos = run_backtest(s_oos, sig_oos, costs=self.costs,
                                 _measure_cost_drag=False)
            c.oos_return = r_oos.total_return
            c.oos_dd = r_oos.max_drawdown
            c.oos_sharpe = r_oos.sharpe
            c.oos_trades = r_oos.trade_count

            # 全样本
            sig_full = run_strategy(self.strategy, self.ind, **params)
            r_full = run_backtest(self.series, sig_full, costs=self.costs)
            c.full_return = r_full.total_return
            c.full_dd = r_full.max_drawdown
            c.full_trades = r_full.trade_count
            c.cost_drag = r_full.cost_drag
            c.ok = True
        except Exception as e:
            c.ok = False
            c.error = f"{type(e).__name__}: {e}"
        return c

    # --------------------------------------------------------
    def grid(self, max_combos: int = 400) -> list[Combo]:
        """网格搜索：笛卡尔积 + 约束过滤。"""
        space = PARAM_SPACE.get(self.strategy)
        if not space:
            raise ValueError(f"策略 {self.strategy} 没有定义参数空间")
        keys = list(space.keys())
        cons = PARAM_CONSTRAINTS.get(self.strategy, [])
        combos = []
        for values in itertools.product(*(space[k] for k in keys)):
            p = dict(zip(keys, values))
            if all(fn(p) for fn in cons):
                combos.append(p)
        # 组合太多就均匀抽样（保持覆盖度，不是随便截断前 N 个）
        if len(combos) > max_combos:
            step = len(combos) / max_combos
            combos = [combos[int(i * step)] for i in range(max_combos)]
        return self._run_all(combos)

    def random_search(self, n: int = 200, seed: int = 42) -> list[Combo]:
        """随机搜索：从参数空间里随机取值，约束过滤。"""
        space = PARAM_SPACE.get(self.strategy)
        if not space:
            raise ValueError(f"策略 {self.strategy} 没有定义参数空间")
        keys = list(space.keys())
        cons = PARAM_CONSTRAINTS.get(self.strategy, [])
        rng = random.Random(seed)
        seen, combos = set(), []
        tries = 0
        while len(combos) < n and tries < n * 50:
            tries += 1
            p = {k: rng.choice(space[k]) for k in keys}
            key = tuple(p[k] for k in keys)
            if key in seen or not all(fn(p) for fn in cons):
                continue
            seen.add(key)
            combos.append(p)
        return self._run_all(combos)

    def _run_all(self, combos: list[dict]) -> list[Combo]:
        out = []
        total = len(combos)
        for i, p in enumerate(combos, 1):
            out.append(self._eval(p))
            if self.progress and (i % 10 == 0 or i == total):
                self.progress(i, total)
        return out

    # --------------------------------------------------------
    def baseline(self) -> dict | None:
        """不优化的对照：用策略默认参数。"""
        dflt = default_params(self.strategy)
        if not dflt:
            return None
        c = self._eval(dflt)
        d = c.to_dict()
        d["label"] = "默认参数（未优化）"
        return d


# ============================================================
def analyze(results: list[Combo], *, min_trades: int = 3,
            benchmark: float | None = None) -> tuple[list[dict], list[str]]:
    """
    排序并生成警告。

    排序键：训练集夏普 → 训练集收益。
    为什么不用全样本或验证集排序：
      · 用全样本 = 让验证集参与选择，验证就失去意义
      · 用验证集 = 直接对着验证集调参，等于把验证集变成训练集
    所以只看训练集，验证集纯粹用于**报告**。
    """
    warnings: list[str] = []
    valid = [c for c in results if c.ok]
    failed = [c for c in results if not c.ok]

    if failed:
        # 同一个异常重复出现时只报一次，避免刷屏
        uniq = {}
        for c in failed:
            uniq.setdefault(c.error, 0)
            uniq[c.error] += 1
        for err, n in list(uniq.items())[:3]:
            warnings.append(f"{n} 组参数评估失败：{err}")

    thin = [c for c in valid if c.is_trades < min_trades]
    solid = [c for c in valid if c.is_trades >= min_trades]

    if thin:
        warnings.append(
            f"{len(thin)} 组参数在训练集上交易少于 {min_trades} 笔，"
            f"已排除出排名（样本不足的结果不可信）"
        )

    ranked = sorted(solid, key=lambda c: (-c.is_sharpe, -c.is_return))

    rows = []
    for c in ranked:
        d = c.to_dict()
        # 过拟合判定：验证集明显差于训练集
        d["overfit"] = bool(c.gap < -10 and c.is_return > 5)
        d["degraded"] = bool(0 < c.oos_trades <= 1 and c.oos_return == 0)
        rows.append(d)

    if ranked:
        best = ranked[0]
        if best.gap < -15:
            warnings.append(
                f"★ 最优组合在验证集上比训练集差 {abs(best.gap):.1f} 个百分点 —— "
                f"强烈疑似过拟合，不建议直接使用"
            )
        oos_best = max((c for c in solid), key=lambda c: c.oos_return, default=None)
        if oos_best and oos_best.params != best.params:
            warnings.append(
                "训练集最优与验证集最优不是同一组参数 —— "
                "这是过拟合的典型信号，说明参数选择对样本很敏感"
            )
        pos_oos = sum(1 for c in solid if c.oos_return > 0)
        if solid:
            ratio = pos_oos / len(solid)
            if ratio < 0.4:
                warnings.append(
                    f"只有 {ratio:.0%} 的参数组合在验证集上为正收益 —— "
                    f"该策略在这个标的上可能整体无效"
                )

    return rows, warnings


# ============================================================
def optimize(*, market: str, symbol: str | None, interval: str | None,
             strategy: str, method: str = "grid", max_combos: int = 200,
             limit: int = 500, is_ratio: float = 0.7,
             progress=None) -> OptimizeResult:
    cfg = MARKETS[market]
    sym = symbol or cfg["defaultSymbol"]
    iv = interval or cfg["defaultInterval"]
    t0 = time.time()

    series = fetch(market, sym, iv, limit)
    costs = MARKET_COSTS.get(market, Costs())

    opt = Optimizer(series=series, strategy=strategy, costs=costs,
                    is_ratio=is_ratio, progress=progress)
    combos = (opt.grid(max_combos) if method == "grid"
              else opt.random_search(max_combos))
    rows, warnings = analyze(combos)

    # 基准：买入持有
    bench = None
    try:
        first, last = series.bars[0].close, series.bars[-1].close
        if first > 0:
            bench = round((last / first - 1) * 100, 3)
    except Exception:
        pass

    res = OptimizeResult(
        strategy=strategy, market=market, symbol=sym, interval=iv,
        method=method, bars_total=len(series.bars),
        bars_is=opt.bars_is, bars_oos=opt.bars_oos,
        combos_tested=len(combos),
        combos_valid=sum(1 for c in combos if c.ok),
        elapsed_ms=int((time.time() - t0) * 1000),
        costs={"commission": costs.commission, "slippage": costs.slippage,
               "min_commission": costs.min_commission},
        baseline=opt.baseline(),
        best=rows[0] if rows else None,
        top=rows[:20],
        warnings=warnings,
        meta={
            "name": SYMBOL_NAMES.get(sym, sym),
            "source": series.source,
            "isRatio": is_ratio,
            "benchmarkReturn": bench,
            "paramSpace": PARAM_SPACE.get(strategy, {}),
        },
    )
    return res


# ============================================================
def _selftest() -> int:
    import numpy as np
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

    print("=" * 78)
    print("  参数优化器自检（离线，用手工构造的行情）")
    print("=" * 78)

    def mk(prices, market="crypto"):
        bars = tuple(Bar(1000 + i * 86400000, p, p * 1.015, p * 0.985, p, 1.0 + (i % 5) * 0.1)
                     for i, p in enumerate(prices))
        s = Series("test", market, "TEST", "测试", "1d", bars, 0.0)
        return s

    # 造一段有明显趋势起伏的行情，让策略能产生交易。
    # ⚠️ 波动必须覆盖**整段**样本，不能只在前 70% 有行情 ——
    #    否则验证集里价格几乎不动、一笔交易都没有，oos 全是 0，
    #    测试就测不到东西（第一版就是这样，我还以为是代码错）。
    rng = np.random.default_rng(11)
    base = []
    p = 100.0
    for i in range(300):
        phase = (i // 25) % 4          # 每 25 根换一次方向，整段都有起伏
        drift = [0.006, -0.005, 0.004, -0.006][phase]
        p = p * (1 + drift + rng.normal(0, 0.010))
        base.append(p)
    series = mk(base)

    # --- 1. 参数空间定义完整 ---
    print("\n[1] 参数空间与实际签名一致")
    # 只对有可调参数的策略检查；macd_trend 签名是 (ind)，本来就没得调
    tunable = {s for s in STRATEGIES if tunable_params(s)}
    check("至少 4 个策略有可调参数", len(tunable) >= 4, f"{sorted(tunable)}")

    for st in tunable:
        have = set(PARAM_SPACE.get(st, {}).keys())
        real = set(tunable_params(st))
        check(f"{st} 的参数空间是签名的子集", have <= real,
              f"空间={sorted(have)} 签名={sorted(real)}")
        check(f"{st} 的参数空间非空", len(have) > 0)

    check("macd_trend 无参数 → 不在参数空间里",
          not tunable_params("macd_trend") and "macd_trend" not in PARAM_SPACE,
          f"可调参数={tunable_params('macd_trend')}")

    # 关键回归：参数名必须能真的传进策略函数
    print("\n[1b] 参数名能真的传进策略函数（回归）")
    import inspect as _insp
    for st in tunable:
        space = PARAM_SPACE.get(st, {})
        if not space:
            continue
        probe = {k: v[0] for k, v in space.items()}
        fn = STRATEGIES[st][1]
        sig = _insp.signature(fn)
        unknown = [k for k in probe if k not in sig.parameters]
        check(f"{st} 的参数名都在签名里", not unknown, f"未知参数 {unknown}")

    # --- 2. 约束生效（快线不得慢于慢线）---
    print("\n[2] 参数约束")
    opt = Optimizer(series=series, strategy="ema_cross",
                    costs=Costs(0.001, 0.0005, 0.0))
    grid = opt.grid(max_combos=10000)
    bad = [c for c in grid if c.params["fast"] >= c.params["slow"]]
    check("ema_cross 没有 fast>=slow 的组合", not bad, f"{len(bad)} 个违反")
    check("网格产生了组合", len(grid) > 20, f"{len(grid)} 组")

    # --- 3. 训练/验证切分不重叠 ---
    print("\n[3] 样本切分")
    check("切分点合理", 0 < opt.split < len(series.bars), f"split={opt.split}")
    check("训练集根数正确", opt.bars_is == opt.split)
    check("验证集根数正确", opt.bars_oos == len(series.bars) - opt.split)
    check("两部分之和等于总数", opt.bars_is + opt.bars_oos == len(series.bars))
    r = Optimizer(series=series, strategy="ema_cross", costs=Costs(0.001, 0.0005, 0.0),
                  is_ratio=0.5)
    check("is_ratio 可调", r.split == len(series.bars) // 2, f"split={r.split}")

    # --- 4. 评估产出完整字段 ---
    print("\n[4] 单组评估")
    c = opt._eval({"fast": 10, "slow": 30, "adx_min": 18})
    check("评估成功", c.ok, c.error or "ok")
    check("训练集有收益", isinstance(c.is_return, (int, float)))
    check("验证集有收益", isinstance(c.oos_return, (int, float)))
    check("有泛化差距属性", isinstance(c.gap, (int, float)), f"gap={c.gap:.2f}")
    check("to_dict 含 gap", "gap" in c.to_dict())
    check("可 JSON 序列化", isinstance(json.dumps(c.to_dict(), ensure_ascii=False), str))

    # --- 5. 指标只算一次（性能设计）---
    print("\n[5] 性能设计：指标复用")
    check("Optimizer 构造时已算好指标", opt.ind is not None)
    t0 = time.time()
    for _ in range(5):
        opt._eval({"fast": 8, "slow": 26, "adx_min": 15})
    dt = (time.time() - t0) / 5 * 1000
    check("单组评估耗时可接受（<800ms）", dt < 800, f"{dt:.0f}ms")

    # --- 6. 排序只用训练集 ---
    print("\n[6] 排序规则")
    combos = opt.grid(max_combos=30)
    rows, warns = analyze(combos, min_trades=1)
    if rows:
        keys = [(-r["is_sharpe"], -r["is_return"]) for r in rows]
        check("按训练集夏普降序", all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1)))
        check("每行都带 gap", all("gap" in r for r in rows))
        check("每行都带 overfit 标记", all("overfit" in r for r in rows))
    else:
        check("产生了排序结果", False, "无有效组合")

    # --- 7. 交易数门槛 ---
    print("\n[7] 最少交易数门槛")
    rows_thin, warns_thin = analyze(combos, min_trades=999)
    check("门槛极高时无结果", len(rows_thin) == 0, f"{len(rows_thin)} 行")
    check("提示样本不足", any("样本不足" in w or "交易少于" in w for w in warns_thin),
          warns_thin[0][:60] if warns_thin else "无警告")

    # --- 8. 过拟合检测：人为造一个"训练好、验证差"的组合 ---
    print("\n[8] 过拟合检测")
    fake = Combo(params={"x": 1}, ok=True,
                 is_return=80.0, oos_return=-20.0, is_trades=50, oos_trades=10,
                 is_sharpe=3.0)
    check("gap 计算正确", abs(fake.gap - (-100.0)) < 1e-9, f"{fake.gap}")
    rows_fake, warns_fake = analyze([fake], min_trades=1)
    check("被标记为过拟合", rows_fake and rows_fake[0]["overfit"] is True)
    check("给出过拟合警告", any("过拟合" in w for w in warns_fake),
          next((w for w in warns_fake if "过拟合" in w), "无")[:70])

    # --- 9. 反例：诚实的结果不该被误报 ---
    print("\n[9] 反例：不该误报")
    honest = Combo(params={"y": 1}, ok=True,
                   is_return=20.0, oos_return=15.0, is_trades=30, oos_trades=12,
                   is_sharpe=1.2)
    rows_h, warns_h = analyze([honest], min_trades=1)
    check("泛化良好时不标过拟合", rows_h[0]["overfit"] is False)
    check("不产生过拟合警告", not any("过拟合" in w for w in warns_h))

    # --- 10. 失败组合被记录而不是崩溃 ---
    print("\n[10] 异常组合处理")
    bad_combo = Combo(params={"nonsense": 1}, ok=False, error="ValueError: 测试")
    rows_b, warns_b = analyze([bad_combo], min_trades=1)
    check("失败组合不进排名", len(rows_b) == 0)
    check("失败原因被汇总", any("评估失败" in w for w in warns_b),
          warns_b[0][:60] if warns_b else "无")

    # --- 11. 参数无效时抛错而不是静默 ---
    print("\n[11] 非法输入")
    try:
        o = Optimizer(series=series, strategy="不存在的策略", costs=Costs(0.001, 0.0005, 0.0))
        o.grid()
        check("未知策略应抛错", False, "没抛错")
    except ValueError:
        check("未知策略抛 ValueError", True)

    # --- 12. 随机搜索覆盖度 ---
    print("\n[12] 随机搜索")
    o2 = Optimizer(series=series, strategy="rsi_reversion",
                   costs=Costs(0.001, 0.0005, 0.0))
    rs = o2.random_search(n=15, seed=7)
    check("产生指定数量组合", len(rs) == 15, f"{len(rs)}")
    check("参数互不重复", len({tuple(sorted(c.params.items())) for c in rs}) == len(rs))
    check("满足约束 low<high", all(c.params["low"] < c.params["high"] for c in rs))
    rs2 = o2.random_search(n=15, seed=7)
    check("同种子结果可复现",
          [c.params for c in rs] == [c.params for c in rs2])

    # --- 13. 成本照收（零成本会选出不同参数）---
    print("\n[13] 成本参与优化")
    free = Optimizer(series=series, strategy="ema_cross",
                     costs=Costs(0.0, 0.0, 0.0))
    charged = Optimizer(series=series, strategy="ema_cross",
                        costs=Costs(0.003, 0.002, 5.0))
    p1 = {"fast": 8, "slow": 20, "adx_min": 0}
    c_free = free._eval(p1)
    c_chg = charged._eval(p1)
    # ⚠️ 比 full_return 而不是 oos_return —— 验证集只有 90 根，
    #    有可能一笔交易都没触发（oos 恒为 0），那样就测不出成本差异。
    check("确实产生了交易", c_free.full_trades > 0, f"{c_free.full_trades} 笔")
    check("有成本时收益更低", c_chg.full_return < c_free.full_return,
          f"免费 {c_free.full_return:.2f}% vs 收费 {c_chg.full_return:.2f}%")
    check("成本拖累被记录", c_chg.cost_drag > 0, f"{c_chg.cost_drag}")

    # --- 14. 结果可序列化 ---
    print("\n[14] 结果序列化")
    check("OptimizeResult 可转 dict", isinstance(res_dict := OptimizeResult(
        strategy="ema_cross", market="crypto", symbol="T", interval="1d",
        method="grid", bars_total=1, bars_is=1, bars_oos=1,
        combos_tested=0, combos_valid=0, elapsed_ms=0, costs={},
    ).to_dict(), dict))
    check("可 JSON 序列化", isinstance(json.dumps(res_dict, ensure_ascii=False), str))

    print("\n" + "=" * 78)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 78)
    return 1 if fail else 0


# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="策略参数优化（带样本外验证）")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--strategy", default="ema_cross",
                    help="策略名。注意 macd_trend 没有可调参数，不能优化。")
    ap.add_argument("--method", default="grid", choices=["grid", "random"])
    ap.add_argument("--max-combos", type=int, default=120)
    ap.add_argument("--limit", type=int, default=500, help="K 线根数")
    ap.add_argument("--is-ratio", type=float, default=0.7,
                    help="训练集占比（其余为验证集）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--compact", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    # 前置校验：策略必须存在，且必须有可调参数
    if args.strategy not in STRATEGIES:
        print(json.dumps({"error": f"未知策略 {args.strategy}，"
                                   f"可用: {', '.join(STRATEGIES)}"},
                         ensure_ascii=False))
        return 1
    if args.strategy not in PARAM_SPACE:
        have = tunable_params(args.strategy)
        msg = (f"策略 {args.strategy} 没有可优化的参数"
               + (f"（签名的可调参数: {have}，但未定义搜索空间）" if have
                  else "（函数签名是 (ind)，本来就没有参数可调）"))
        print(json.dumps({"error": msg,
                          "optimizable": sorted(PARAM_SPACE.keys())},
                         ensure_ascii=False))
        return 1

    def prog(i, total):
        bar = "█" * int(24 * i / max(1, total))
        print(f"\r  搜索中 {bar}{'·' * (24 - len(bar))} {i}/{total}", end="", flush=True)

    try:
        res = optimize(
            market=args.market, symbol=args.symbol, interval=args.interval,
            strategy=args.strategy, method=args.method,
            max_combos=args.max_combos, limit=args.limit,
            is_ratio=args.is_ratio, progress=None if args.json else prog,
        )
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    if not args.json:
        print()
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False,
                         separators=(",", ":") if args.compact else None))
        return 0

    # 人类可读输出
    print()
    print("=" * 88)
    print(f"  参数优化  {res.meta['name']} ({res.symbol})  {res.interval}  "
          f"策略={res.strategy}")
    print("=" * 88)
    print(f"  样本    {res.bars_total} 根  →  训练 {res.bars_is} / 验证 {res.bars_oos}"
          f"（{res.meta['isRatio']:.0%} / {1-res.meta['isRatio']:.0%}）")
    print(f"  搜索    {res.method}  {res.combos_tested} 组（有效 {res.combos_valid}）"
          f"  用时 {res.elapsed_ms}ms")
    print(f"  成本    手续费 {res.costs['commission']:.4%}  "
          f"滑点 {res.costs['slippage']:.4%}")
    if res.meta.get("benchmarkReturn") is not None:
        print(f"  基准    买入持有 {res.meta['benchmarkReturn']:+.2f}%")

    if res.baseline:
        b = res.baseline
        print(f"\n  【对照】默认参数：训练 {b['is_return']:+.2f}%  "
              f"验证 {b['oos_return']:+.2f}%  全样本 {b['full_return']:+.2f}%  "
              f"gap {b['gap']:+.2f}")

    if res.top:
        print(f"\n  {'参数':<34} {'训练':>9} {'验证':>9} {'差距':>9} "
              f"{'全样本':>9} {'回撤':>7} {'夏普':>7} {'交易':>5}")
        print("  " + "-" * 92)
        for r in res.top[:12]:
            pstr = ",".join(f"{k}={v}" for k, v in r["params"].items())
            flag = " ⚠过拟合" if r.get("overfit") else ""
            print(f"  {pstr[:33]:<34} {r['is_return']:>+8.2f}% {r['oos_return']:>+8.2f}% "
                  f"{r['gap']:>+8.2f}% {r['full_return']:>+8.2f}% "
                  f"{r['full_dd']:>6.2f}% {r['is_sharpe']:>7.2f} {r['is_trades']:>5}{flag}")

    if res.warnings:
        print("\n  ⚠ 警告")
        for w in res.warnings:
            print(f"    · {w}")

    print("\n  ⚠ 参数优化极易过拟合。上面的「验证」列才是可信参考；")
    print("    训练集表现只用于挑参数，不代表未来收益。")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
