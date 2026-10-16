#!/usr/bin/env python3
"""
极小测试框架（零依赖，不依赖 pytest）。

为什么自己写而不引入 pytest：
  这个项目的要求是「零第三方依赖」—— 只有 numpy 可用（pandas 装了但这里没用）。
  引入 pytest 会让 CI 与 Docker 镜像多一个依赖，而我们需要的能力其实很少：
  注册用例、跑用例、把异常记下来、统计断言数、决定退出码。

【关键设计：让「断言没跑」无法伪装成「断言通过」】
  每个用例自己报断言数。如果它报的数量和实际执行到的 check() 调用数不一致，
  用例直接判失败（见 TestCase.done）。这条规则是为了防止改测试时误删断言、
  或者提前 return 导致后半段静默不跑 —— 那种情况下「0 失败」是假象。

用法（在测试文件里）：
    from harness import TestCase

    class TestX(TestCase):
        name = "模块.分组"

        def test_something(self):
            self.eq(actual, expected, "说明")
            self.near(actual, expected, 1e-9, "说明")
            self.true(cond, "说明")

断言计数器是模块级全局的，所以测试文件即使直接 `python test_xxx.py` 跑，
也会打印自己那份小计。
"""

from __future__ import annotations

import math
import traceback

# ------------------------------------------------------------------
#  模块级计数（TestRunner 与直接运行都读它）
# ------------------------------------------------------------------
COUNTER = {"checks": 0, "failures": 0, "cases": 0, "cases_failed": 0}

REGISTRY: list[type] = []

# 每个 test_* 方法声明的断言数区间 {类名: {方法名: (下限, 上限)}}。
# 断言数掉出区间 → 该用例失败（防止「断言没跑」伪装成「断言通过」）。
# 各测试文件在末尾调用 declare_counts({...}) 填进来。
EXPECT_CHECK_COUNTS: dict[str, dict[str, tuple[int, int]]] = {}


def declare_counts(spec: dict[str, dict[str, tuple[int, int]]]) -> None:
    EXPECT_CHECK_COUNTS.update(spec)


# 默认跳过的慢用例（方法名）。用 run.py --bench 才会跑。
SLOW_CASES: set[str] = set()


def declare_slow(names) -> None:
    SLOW_CASES.update(names)


def _record_failure(case_name: str, label: str, detail: str) -> None:
    COUNTER["failures"] += 1
    print(f"    [FAIL] {case_name} :: {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"           {line}")


class TestCase:
    """
    一个测试类 = 一组用例。子类里所有 test_* 方法都会被收集。

    设计说明：这里刻意**不**用「类级 expect_checks」。一个类里每个方法的
    断言数不同，用类级总数会让每个方法都报「数量不符」，那就等于没校验。
    """

    name = "(未命名)"
    expect_checks: int | None = None   # 期望的断言数；不一致则判失败

    def __init__(self) -> None:
        self._checks = 0
        # 由 Runner 在调用每个 test_* 之前设置：(方法名, 下限, 上限)
        self._current = ""
        self._expected: tuple[int, int] | None = None

    # ---------------- 断言 API ----------------
    def check(self, cond, label: str, detail: str = "") -> bool:
        """最小断言原语。所有断言最终都走这里。"""
        self._checks += 1
        COUNTER["checks"] += 1
        if cond:
            return True
        _record_failure(self.name, label, detail)
        return False

    def true(self, cond, label: str) -> bool:
        return self.check(bool(cond), label)

    def false(self, cond, label: str) -> bool:
        return self.check(not cond, label)

    def eq(self, actual, expected, label: str) -> bool:
        return self.check(actual == expected, label,
                          f"实际 {actual!r} != 期望 {expected!r}")

    def ne(self, actual, unexpected, label: str) -> bool:
        return self.check(actual != unexpected, label,
                          f"实际 {actual!r} == 不应出现 {unexpected!r}")

    def near(self, actual, expected, tol: float, label: str) -> bool:
        try:
            diff = abs(float(actual) - float(expected))
        except (TypeError, ValueError):
            return self.check(False, label, f"无法比较：{actual!r} vs {expected!r}")
        return self.check(diff <= tol, label,
                          f"实际 {actual!r} 与期望 {expected!r} 相差 {diff:.3e} > {tol:.3e}")

    def is_nan(self, value, label: str) -> bool:
        try:
            return self.check(math.isnan(float(value)), label, f"实际 {value!r} 不是 NaN")
        except (TypeError, ValueError):
            return self.check(False, label, f"实际 {value!r} 不是数值，无法判 NaN")

    def not_nan(self, value, label: str) -> bool:
        try:
            return self.check(not math.isnan(float(value)), label, f"实际 {value!r} 是 NaN")
        except (TypeError, ValueError):
            return self.check(False, label, f"实际 {value!r} 不是数值")

    def raises(self, exc_types, fn, label: str) -> bool:
        """断言 fn() 抛出指定异常（异常也可视为一种正确的契约）。"""
        try:
            fn()
        except exc_types as e:
            return self.check(True, label, f"如预期抛出 {type(e).__name__}")
        except Exception as e:                      # noqa: BLE001
            return self.check(False, label,
                              f"抛出了 {type(e).__name__}: {e}，不是期望的 {exc_types}")
        return self.check(False, label, "没有抛出任何异常")

    def no_raise(self, fn, label: str):
        """断言 fn() 不抛异常，返回 (是否成功, 返回值或异常)。"""
        try:
            v = fn()
        except Exception as e:                      # noqa: BLE001
            self.check(False, label, f"{type(e).__name__}: {e}")
            return False, e
        self.check(True, label)
        return True, v

    # ---------------- 断言数护栏 ----------------
    def _expect_for(self, method_name: str) -> tuple[int, int] | None:
        """
        取该方法声明的断言数区间。

        为什么要有这个：测试最常见的「变绿」方式不是修 bug，而是
        · 提前 return / 提前 continue，后半段断言静默不跑
        · 改测试时误删断言
        两种情况都是「0 失败但测试已经残废」。声明区间后，断言数掉出
        区间就会判失败并打印出来。

        下界取「必须至少跑到的条数」，上界用于抓「断言数意外暴涨」
        （通常是循环里重复断言同一个东西）。
        """
        spec = EXPECT_CHECK_COUNTS.get(type(self).__name__)
        if not spec:
            return None
        return spec.get(method_name)

    def done(self) -> None:
        """用例跑完由 Runner 调用：校验断言数落在声明区间内。"""
        if self._expected is None:
            return
        lo, hi = self._expected
        self.check(lo <= self._checks <= hi,
                   f"断言数护栏（{self._current} 声明 {lo}~{hi} 条）",
                   f"实际执行了 {self._checks} 条。若确实改了测试请同步更新 "
                   f"EXPECT_CHECK_COUNTS；若是提前 return / 异常导致漏跑，"
                   f"说明这条用例已经不再覆盖它声称覆盖的东西了。")



def assert_module_contracts() -> None:
    """给测试文件自检用：确认被测模块真的被 import 到了，而不是被同名文件遮蔽。"""
    import datasource
    import indicators
    assert indicators.__file__.endswith("indicators.py"), indicators.__file__
    assert datasource.__file__.endswith("datasource.py"), datasource.__file__


# ------------------------------------------------------------------
#  收藏器
# ------------------------------------------------------------------
def collect() -> list[tuple[str, str, TestCase]]:
    """把注册表里每个类的 test_* 方法展开成 (类名, 方法名, 实例) 列表。"""
    out: list[tuple[str, str, TestCase]] = []
    for cls in REGISTRY:
        inst = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
        for m in methods:
            out.append((cls.name, m, inst))
    return out


def run_all(verbose: bool = True, only: str | None = None,
            skip_slow: bool = True) -> int:
    """
    跑全部已注册用例。返回失败数（0 = 全通过）。

    skip_slow=True 时跳过 SLOW_CASES 里登记的方法名（几十秒级的基准用例），
    让默认的测试轮次保持秒级；`run.py --bench` 会传 skip_slow=False。
    """
    cases = collect()
    if only:
        cases = [c for c in cases if only in c[0] or only in c[1]]
    if skip_slow:
        cases = [c for c in cases if c[1] not in SLOW_CASES]
    if not cases:
        print("没有收集到任何用例 —— 检查测试文件是否 import 了 harness 并定义了 TestCase。")
        return 1

    last_group = None
    for cls_name, method_name, inst in cases:
        if verbose and cls_name != last_group:
            print(f"\n  ── {cls_name} " + "─" * max(0, 60 - len(cls_name)))
            last_group = cls_name
        COUNTER["cases"] += 1
        inst._checks = 0
        inst._current = method_name
        inst._expected = inst._expect_for(method_name)
        before_fail = COUNTER["failures"]
        try:
            getattr(inst, method_name)()
        except Exception:                            # noqa: BLE001
            COUNTER["failures"] += 1
            print(f"    [ERROR] {cls_name}::{method_name} 抛异常（用例本身崩了）")
            for line in traceback.format_exc().splitlines()[-6:]:
                print(f"           {line}")
        inst.done()
        if COUNTER["failures"] > before_fail:
            COUNTER["cases_failed"] += 1
    return COUNTER["failures"]


def module_summary(title: str) -> int:
    """测试文件直接运行时的收尾输出。返回 0/1。"""
    ok = COUNTER["checks"] - COUNTER["failures"]
    print("\n" + "=" * 72)
    print(f"  {title}")
    print(f"  断言 {COUNTER['checks']} 条：{ok} 通过 / {COUNTER['failures']} 失败"
          f"    用例 {COUNTER['cases']} 个：{COUNTER['cases'] - COUNTER['cases_failed']} 通过"
          f" / {COUNTER['cases_failed']} 失败")
    print("=" * 72)
    return 1 if COUNTER["failures"] else 0
