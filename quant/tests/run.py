#!/usr/bin/env python3
"""
quant 测试套件入口 —— 纯离线、零第三方依赖（只用 numpy）。

用法：
  python quant/tests/run.py                 # 跑全部
  python quant/tests/run.py -v              # 每个用例打印一行
  python quant/tests/run.py --only indicators   # 只跑名字里含 indicators 的用例
  python quant/tests/run.py --bench         # 额外跑 5000 根的性能基准
  python quant/tests/run.py --list          # 只看收集到哪些用例

设计约束（踩过的坑，别改回去）：
  · **不联网**：不调用 datasource.fetch() / quote()，全部用构造数据。
  · 测试文件放 quant/tests/ 下且以 test_ 开头。
  · 绝不创建 enum.py / struct.py / types.py / json.py 之类会遮蔽标准库的文件名 ——
    这个项目已经因此挂过两次（一次是 enum.py 导致 ImportError: StrEnum）。
    run.py 的 verify_no_stdlib_shadowing() 会在每次运行时主动检查这一点。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # quant/tests
QUANT = HERE.parent                             # quant
REPO = QUANT.parent                             # 仓库根

# 顺序很重要：tests 目录必须在最前，否则同名的旧副本（比如 quant/test_indicators.py）
# 会被抢先导入 —— quant/ 下确实有一个老的 test_indicators.py。
for p in (str(HERE), str(QUANT), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import harness  # noqa: E402  （必须在 sys.path 调整之后）

TEST_FILES = [
    "test_indicators.py",
    "test_backtest.py",
    "test_paper.py",
    "test_autotrader.py",
    "test_edge_cases.py",
    "test_perf.py",
]

BANNED_NAMES = {
    "enum", "struct", "types", "json", "copy", "io", "abc", "re", "code",
    "token", "keyword", "stat", "select", "socket", "signal", "time", "math",
    "random", "string", "operator", "platform", "hashlib", "datetime", "pathlib",
}


def verify_no_stdlib_shadowing() -> list[str]:
    """
    检查 quant/ 与 quant/tests/ 下有没有和标准库同名的 .py 文件。
    这种事不会报错，只会让 import 悄悄拿错模块 —— 必须在启动时就拦下来。
    """
    problems: list[str] = []
    for d in (QUANT, HERE):
        for f in sorted(d.glob("*.py")):
            if f.stem in BANNED_NAMES:
                problems.append(f"{f} 与标准库模块 {f.stem} 同名，会遮蔽它")
    return problems


def import_test_modules(verbose: bool) -> list[str]:
    loaded: list[str] = []
    for fn in TEST_FILES:
        path = HERE / fn
        if not path.exists():
            print(f"  [SKIP] 缺少测试文件 {fn}")
            continue
        mod_name = fn[:-3]
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)
        loaded.append(mod_name)
        if verbose:
            print(f"  已加载 {fn}")
    return loaded


def benchmark() -> int:
    """
    性能基准：compute() 对 5000 根的耗时 / run_backtest 的耗时。
    返回非零表示基准本身出问题（不是性能不达标）。
    """
    import numpy as np

    from datasource import Bar, Series
    from indicators import compute
    from strategy import run_all
    from backtest import run_backtest
    from paper import PaperTrader

    def mk_series(n: int) -> Series:
        rng = np.random.default_rng(20261016)
        steps = rng.normal(0.0, 0.012, n)
        close = 100.0 * np.exp(np.cumsum(steps))
        bars = []
        for i in range(n):
            o = close[i] * (1 + float(rng.normal(0, 0.002)))
            h = max(o, close[i]) * (1 + abs(float(rng.normal(0, 0.003))))
            l = min(o, close[i]) * (1 - abs(float(rng.normal(0, 0.003))))
            v = float(abs(rng.normal(1e6, 2e5)))
            bars.append(Bar(1_700_000_000_000 + i * 86_400_000, o, h, l, close[i], v))
        return Series("bench", "crypto", "BENCH", "基准", "1d", tuple(bars), 0.0)

    print("\n" + "=" * 72)
    print("  性能基准（构造数据，不联网）")
    print("=" * 72)
    out = {}
    for n in (500, 1000, 5000):
        s = mk_series(n)
        t0 = time.perf_counter()
        ind = compute(s)
        t_compute = time.perf_counter() - t0

        t0 = time.perf_counter()
        sigs = run_all(ind)
        t_sig = time.perf_counter() - t0

        t0 = time.perf_counter()
        results = [run_backtest(s, sig) for sig in sigs.values()]
        t_bt = time.perf_counter() - t0

        t0 = time.perf_counter()
        pt = PaperTrader(s, sigs["ema_cross"])
        pt.run_all()
        t_paper = time.perf_counter() - t0

        out[n] = {
            "compute_ms": round(t_compute * 1000, 1),
            "strategies_ms": round(t_sig * 1000, 1),
            "run_backtest_all_ms": round(t_bt * 1000, 1),
            "paper_ms": round(t_paper * 1000, 1),
        }
        print(f"\n  {n} 根 K 线")
        print(f"    compute()               {t_compute*1000:9.1f} ms")
        print(f"    run_all() 5 个策略       {t_sig*1000:9.1f} ms")
        print(f"    run_backtest() × {len(results)}        {t_bt*1000:9.1f} ms"
              f"   （含零成本重跑，每次调用实测两遍）")
        print(f"    PaperTrader.run_all()   {t_paper*1000:9.1f} ms")
        print(f"    compute 单根均摊         {t_compute/n*1e6:9.1f} µs/根")

    # 记录一份供报告引用
    (HERE / "benchmark.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf8")
    print(f"\n  基准已写入 {(HERE / 'benchmark.json')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="quant 离线测试套件")
    ap.add_argument("--only", default=None, help="只跑名字里含该子串的用例")
    ap.add_argument("--list", action="store_true", help="只列出收集到的用例")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐个用例打印")
    ap.add_argument("--bench", action="store_true", help="额外跑性能基准")
    args = ap.parse_args(argv)

    py = sys.executable
    print("=" * 72)
    print("  quant 离线测试套件")
    print("=" * 72)
    print(f"  Python   {sys.version.split()[0]}   ({py})")
    try:
        import numpy
        print(f"  numpy    {numpy.__version__}")
    except Exception as e:                          # noqa: BLE001
        print(f"  numpy    不可用: {e}")
    print(f"  工作目录  {REPO}")
    print("  联网      否（全部为构造数据）")

    shadow = verify_no_stdlib_shadowing()
    if shadow:
        print("\n  [FATAL] 发现会遮蔽标准库的文件名：")
        for s in shadow:
            print(f"    · {s}")
        return 1
    print("  遮蔽检查  通过（无 enum.py / struct.py / types.py 等同名文件）")

    t0 = time.perf_counter()
    import_test_modules(args.verbose)

    cases = harness.collect()
    if args.list:
        print(f"\n  收集到 {len(cases)} 个用例：")
        for cls_name, method, _ in cases:
            print(f"    {cls_name}::{method}")
        return 0

    print(f"\n  收集到 {len(cases)} 个用例，开始执行"
          + ("" if args.bench else "（跳过性能基准用例，加 --bench 可跑）"))
    failures = harness.run_all(verbose=True, only=args.only,
                               skip_slow=not args.bench)
    elapsed = time.perf_counter() - t0

    c = harness.COUNTER
    print("\n" + "=" * 72)
    print(f"  结果：断言 {c['checks']} 条 — {c['checks'] - c['failures']} 通过 / "
          f"{c['failures']} 失败")
    print(f"        用例 {c['cases']} 个 — {c['cases'] - c['cases_failed']} 通过 / "
          f"{c['cases_failed']} 失败")
    print(f"        耗时 {elapsed:.2f}s")
    print("=" * 72)

    if args.bench:
        benchmark()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
