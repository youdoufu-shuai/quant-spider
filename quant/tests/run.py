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
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # quant/tests
QUANT = HERE.parent                             # quant
REPO = QUANT.parent                             # 仓库根

# ⚠️ 全局离线开关，必须在 import 任何 quant 模块**之前**设。
#
# 为什么：量化测试套件是**离线**的（CI 用 --offline 跑，超时 300 秒）。
# 但 screener.classify_sector 默认会去拉「申万一级行业映射」（2.5 秒 + 网络）。
# 不拦住的话：
#   · 离线测试真的联网 → 慢、不稳定、CI 可能超时
#   · 结果依赖本机缓存 → 同一份代码在不同机器上跑出不同结果
#
# 设了之后 screener 直接走名称关键词兜底，测试可复现。
# 要测「真实映射」的路径，用 screener.set_industry_map() 手工注入。
os.environ["QUANT_OFFLINE"] = "1"

# ⚠️ 插入顺序：**倒着插**，因为 insert(0) 是往前放。
#    写成正序（tests, quant, repo）会让最终的 path 变成 [repo, quant, tests] ——
#    正好反了，于是 `import test_indicators` 会命中 quant/ 下那个同名旧文件。
#    实测踩过：run.py 报「808 断言全过」，而 quant/tests/test_indicators.py
#    的 213 条**一条都没跑**，真身单独跑时还有 12 条失败。
for p in (str(REPO), str(QUANT), str(HERE)):
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
# 现在顺序是 [HERE, QUANT, REPO, ...]，tests 在最前

import harness  # noqa: E402  （必须在 sys.path 调整之后）

TEST_FILES = [
    "test_indicators.py",
    "test_backtest.py",
    "test_paper.py",
    "test_paper_live.py",
    "test_autotrader.py",
    "test_edge_cases.py",
    "test_perf.py",
    "test_screener.py",
    "test_ai_portfolio.py",
    "test_stock_ai.py",
    "test_symbols_cache.py",
]

BANNED_NAMES = {
    "enum", "struct", "types", "json", "copy", "io", "abc", "re", "code",
    "token", "keyword", "stat", "select", "socket", "signal", "time", "math",
    "random", "string", "operator", "platform", "hashlib", "datetime", "pathlib",
}

# 每个测试文件至少应注册这么多用例。低于它就判失败。
#
# 为什么需要这个：光有「断言数护栏」不够 —— 护栏只在用例**被执行**时才生效。
# 如果整个文件没被加载（同名遮蔽、import 失败被吞、路径写错），
# 护栏根本不会触发，汇总却报全过。实测就是这么骗过一次。
# 数字取当前实际值略留余量，删用例时会提醒你同步更新。
MIN_CASES_PER_FILE = {
    "test_indicators.py": 40,
    "test_backtest.py": 25,
    "test_paper.py": 25,
    "test_paper_live.py": 18,
    "test_autotrader.py": 35,
    "test_edge_cases.py": 40,
    "test_perf.py": 2,
    "test_screener.py": 30,
    "test_ai_portfolio.py": 15,
    "test_stock_ai.py": 8,
    "test_symbols_cache.py": 5,
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


def verify_no_duplicate_test_names() -> list[str]:
    """
    检查是否有同名测试文件散落在 sys.path 的多个目录里。

    ⚠️ 这是本项目踩过的最隐蔽的一个坑：quant/test_indicators.py（旧的、55 断言）
    与 quant/tests/test_indicators.py（新的、213 断言）同名，
    而 sys.path 顺序一错就会加载到旧的那个 —— 11 个测试类一个都不注册，
    汇总却报「全过」。**静默加载错文件比报错危险得多。**

    所以每次运行都主动扫一遍，发现重名直接拒绝启动。
    """
    problems: list[str] = []
    for fn in TEST_FILES:
        stem = fn[:-3]
        found = []
        for d in (HERE, QUANT, REPO):
            f = d / fn
            if f.exists():
                found.append(f)
        if len(found) > 1:
            problems.append(
                f"{fn} 有 {len(found)} 个同名副本：\n      "
                + "\n      ".join(str(f) for f in found)
                + f"\n      sys.path 会命中第一个，其余永远不执行。"
                  f"请删掉旧的（保留 {HERE / fn}）")
    return problems


def import_test_modules(verbose: bool) -> list[str]:
    """
    加载测试模块。

    ⚠️ 用**文件路径**显式加载，而不是 `importlib.import_module(mod_name)`。
       按名字 import 会走 sys.path 搜索，一旦有同名文件就会拿错 —— 而且不报错。
       按路径加载从根上消除这个可能（重名检测是第二道防线）。
       模块名加 `qtests_` 前缀，避免与 sys.path 上的同名模块互相污染。
    """
    import importlib.util

    loaded: list[str] = []
    for fn in TEST_FILES:
        path = HERE / fn
        if not path.exists():
            print(f"  [SKIP] 缺少测试文件 {fn}")
            continue
        mod_name = f"qtests_{fn[:-3]}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            print(f"  [FAIL] 无法为 {fn} 建立加载器")
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        before = len(harness.REGISTRY)
        spec.loader.exec_module(mod)
        added = len(harness.REGISTRY) - before
        loaded.append(mod_name)
        if verbose:
            print(f"  已加载 {fn}  (+{added} 个测试类)")

    return loaded


def verify_registration(loaded: list[str]) -> list[str]:
    """
    加载完之后核对：每个文件是否注册了足够多的用例。

    这一步是「断言数护栏」的补充 —— 护栏管「用例跑了但断言少了」，
    这里管「用例压根没跑」。
    """
    problems: list[str] = []
    # 统计每个测试类来自哪个文件（通过模块名反推）
    by_file: dict[str, int] = {}
    for cls in harness.REGISTRY:
        mod = getattr(cls, "__module__", "")
        # qtests_test_indicators → test_indicators.py
        if mod.startswith("qtests_"):
            fn = mod[len("qtests_"):] + ".py"
            n = len([m for m in dir(cls) if m.startswith("test_")])
            by_file[fn] = by_file.get(fn, 0) + n

    for fn, minimum in MIN_CASES_PER_FILE.items():
        if fn not in [f for f in TEST_FILES]:
            continue
        got = by_file.get(fn, 0)
        if got < minimum:
            problems.append(
                f"{fn} 只注册了 {got} 个用例（至少应有 {minimum}）—— "
                f"该文件可能没被正确加载。检查是否有同名文件遮蔽，"
                f"或 import 阶段抛了异常。")
    return problems



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

    dupes = verify_no_duplicate_test_names()
    if dupes:
        print("\n  [FATAL] 发现同名测试文件副本（会导致静默加载错文件）：")
        for d in dupes:
            print(f"    · {d}")
        # 明确说明后果，否则看的人会以为只是"重复代码"
        print("\n  为什么这是致命的：按名字 import 只会命中 sys.path 上的第一个，"
              "\n  其余副本永远不执行，但汇总统计仍然会说「全部通过」。")
        return 1
    print("  同名检查  通过（每个测试文件只有一份）")

    t0 = time.perf_counter()
    loaded = import_test_modules(args.verbose)

    # ★ 加载完整性检查：没注册够用例就判失败。
    #   这一步在 collect() 之前 —— 「一条都没加载」比「加载了但断言少」更隐蔽。
    reg = verify_registration(loaded)
    if reg:
        print("\n  [FATAL] 测试注册不完整：")
        for r in reg:
            print(f"    · {r}")
        return 1

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
