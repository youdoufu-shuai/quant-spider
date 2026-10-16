#!/usr/bin/env python3
"""
性能与复杂度验证 —— 确认没有 O(n²) 或者重复计算。

默认只跑中小规模（秒级）；大样本基准用例登记在 SLOW_CASES 里，
用 `python quant/tests/run.py --bench` 才会执行。

本文件里的「复杂度」用例是有意义的：只要有人把某个指标改回
「每个位置重算整段」的写法（曾经的 cumsum/重复切片风格），
每根耗时就会随 n 明显上升，这里会直接失败。

直接运行：python quant/tests/test_perf.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from harness import (REGISTRY, TestCase, declare_counts, declare_slow,  # noqa: E402
                     run_all, module_summary)
from datasource import Bar, Series                       # noqa: E402
from indicators import compute                           # noqa: E402
from strategy import run_all as run_all_strategies       # noqa: E402
from backtest import run_backtest                        # noqa: E402
from paper import PaperTrader                            # noqa: E402

# 复现结果用的固定种子；benchmark.json 与报告里的数字都来自这里
SEED = 20261016


def synthetic(n: int, seed: int = SEED) -> Series:
    """带趋势 + 噪声的日线序列（几何随机游走），OHLCV 齐全。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    bars = []
    for i in range(n):
        o = close[i] * (1 + float(rng.normal(0, 0.002)))
        h = max(o, close[i]) * (1 + abs(float(rng.normal(0, 0.003))))
        l = min(o, close[i]) * (1 - abs(float(rng.normal(0, 0.003))))
        v = float(abs(rng.normal(1e6, 2e5)))
        bars.append(Bar(1_700_000_000_000 + i * 86_400_000, o, h, l, close[i], v))
    return Series("bench", "crypto", "BENCH", "基准", "1d", tuple(bars), 0.0)


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


class TestNoQuadraticBehaviour(TestCase):
    name = "perf.复杂度（无 O(n²)）"

    def test_compute_per_bar_cost_is_stable(self):
        """
        compute() 的「每根耗时」在 n 增大 10 倍后不应显著变差。
        线性实现下两者接近；O(n²) 实现下每根耗时会长约 10 倍。
        留 4 倍余量以容忍缓存/噪声。
        """
        s_small = synthetic(500)
        s_big = synthetic(5000)
        compute(s_small)                       # 预热，避免首次导入/分配计入
        t_small, _ = timed(lambda: compute(s_small))
        t_big, _ = timed(lambda: compute(s_big))
        per_small = t_small / 500
        per_big = t_big / 5000
        ratio = per_big / per_small if per_small > 0 else 1.0
        self.true(ratio < 4.0,
                  f"每根耗时比 {ratio:.2f} < 4（500 根 {per_small*1e6:.1f}µs/根，"
                  f"5000 根 {per_big*1e6:.1f}µs/根）")

    def test_backtest_scales_linearly(self):
        """回测是单趟循环 + 一次零成本重跑，应该是严格线性。"""
        def run(n):
            s = synthetic(n)
            sigs = run_all_strategies(compute(s))
            sig = sigs["ema_cross"]
            t, _ = timed(lambda: run_backtest(s, sig))
            return t / n

        per_500 = run(500)
        per_5000 = run(5000)
        ratio = per_5000 / per_500 if per_500 > 0 else 1.0
        self.true(ratio < 3.0,
                  f"每根耗时比 {ratio:.2f} < 3（500 根 {per_500*1e6:.1f}µs/根，"
                  f"5000 根 {per_5000*1e6:.1f}µs/根）")

    def test_paper_scales_linearly(self):
        def run(n):
            s = synthetic(n)
            sig = run_all_strategies(compute(s))["ema_cross"]
            pt = PaperTrader(s, sig)
            t, _ = timed(lambda: pt.run_all())
            return t / n

        per_500 = run(500)
        per_5000 = run(5000)
        ratio = per_5000 / per_500 if per_500 > 0 else 1.0
        self.true(ratio < 4.0,
                  f"每根耗时比 {ratio:.2f} < 4（500 根 {per_500*1e6:.1f}µs/根，"
                  f"5000 根 {per_5000*1e6:.1f}µs/根）")

    def test_indicator_functions_are_not_quadratic(self):
        """
        逐个指标函数做同样的检验。任何一个是 O(n²) 都会让长序列变慢 10 倍。
        """
        from indicators import (adx, atr, bollinger, ema, macd, obv, rsi, sma,
                                stochastic, wilder)
        close_s = np.asarray(synthetic(500).closes, dtype=float)
        close_b = np.asarray(synthetic(5000).closes, dtype=float)
        high_s, low_s = close_s * 1.01, close_s * 0.99
        high_b, low_b = close_b * 1.01, close_b * 0.99
        funcs = {
            "sma": lambda c, h, l: sma(c, 20),
            "ema": lambda c, h, l: ema(c, 26),
            "wilder": lambda c, h, l: wilder(c, 14),
            "rsi": lambda c, h, l: rsi(c, 14),
            "bollinger": lambda c, h, l: bollinger(c, 20),
            "macd": lambda c, h, l: macd(c, 12, 26, 9),
            "atr": lambda c, h, l: atr(h, l, c, 14),
            "adx": lambda c, h, l: adx(h, l, c, 14),
            "stochastic": lambda c, h, l: stochastic(h, l, c, 14),
            "obv": lambda c, h, l: obv(c, np.ones_like(c)),
        }
        worst_name, worst_ratio = "", 0.0
        for name, fn in funcs.items():
            fn(close_s, high_s, low_s)                       # 预热
            t_s, _ = timed(lambda fn=fn: fn(close_s, high_s, low_s))
            t_b, _ = timed(lambda fn=fn: fn(close_b, high_b, low_b))
            per_s, per_b = t_s / 500, t_b / 5000
            ratio = per_b / per_s if per_s > 1e-9 else 1.0
            if ratio > worst_ratio:
                worst_name, worst_ratio = name, ratio
        self.true(worst_ratio < 4.0,
                  f"最差的是 {worst_name}：每根耗时比 {worst_ratio:.2f} < 4") 

    def test_compute_dominates_total_pipeline_time(self):
        """
        记录耗时构成：compute() 占大头是合理的（27 个指标），
        但如果回测比 compute 还慢，说明回测里有重复劳动。
        这里只断言「回测 ≤ compute × 5」这种宽松上界。
        """
        s = synthetic(1000)
        t_compute, ind = timed(lambda: compute(s))
        sig = run_all_strategies(ind)["ema_cross"]
        t_bt, _ = timed(lambda: run_backtest(s, sig))
        self.true(t_bt <= max(t_compute * 5, 0.05),
                  f"回测（{t_bt*1000:.1f}ms）不超过 compute（{t_compute*1000:.1f}ms）的 5 倍")


class TestLargeSampleBenchmarks(TestCase):
    name = "perf.大样本基准（默认跳过）"

    def test_compute_5000_bars_under_budget(self):
        """5000 根必须能在 2 秒内算完（正常应在 100ms 量级）。"""
        s = synthetic(5000)
        compute(s)
        t, _ = timed(lambda: compute(s))
        self.true(t < 2.0, f"compute(5000) = {t*1000:.1f} ms < 2000 ms")

    def test_run_backtest_5000_bars_under_budget(self):
        s = synthetic(5000)
        sig = run_all_strategies(compute(s))["ema_cross"]
        t, _ = timed(lambda: run_backtest(s, sig))
        self.true(t < 2.0, f"run_backtest(5000) = {t*1000:.1f} ms < 2000 ms")

    def test_full_pipeline_5000_bars(self):
        """compute + 5 个策略 + 5 次回测 + 5 次模拟盘，端到端耗时。"""
        s = synthetic(5000)

        def full():
            ind = compute(s)
            sigs = run_all_strategies(ind)
            for sig in sigs.values():
                run_backtest(s, sig)
            for sig in sigs.values():
                PaperTrader(s, sig).run_all()

        t, _ = timed(full)
        self.true(t < 20.0, f"完整流水线(5000 根) = {t:.2f}s < 20s")


declare_slow({"test_compute_5000_bars_under_budget",
              "test_run_backtest_5000_bars_under_budget",
              "test_full_pipeline_5000_bars",
              "test_compute_per_bar_cost_is_stable",
              "test_backtest_scales_linearly",
              "test_paper_scales_linearly",
              "test_indicator_functions_are_not_quadratic"})

REGISTRY.extend([TestNoQuadraticBehaviour, TestLargeSampleBenchmarks])

declare_counts({
    "TestNoQuadraticBehaviour": {
        "test_compute_per_bar_cost_is_stable": (1, 1),
        "test_backtest_scales_linearly": (1, 1),
        "test_paper_scales_linearly": (1, 1),
        "test_indicator_functions_are_not_quadratic": (1, 1),
        "test_compute_dominates_total_pipeline_time": (1, 1)},
    "TestLargeSampleBenchmarks": {
        "test_compute_5000_bars_under_budget": (1, 1),
        "test_run_backtest_5000_bars_under_budget": (1, 1),
        "test_full_pipeline_5000_bars": (1, 1)},
})


if __name__ == "__main__":
    # 单独运行时把慢用例也跑上（这个文件本身就是性能用的）
    run_all(verbose=True, skip_slow=False)
    raise SystemExit(module_summary("test_perf.py"))
