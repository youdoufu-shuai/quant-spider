#!/usr/bin/env python3
"""
AI 选股增强的测试 —— 全部离线。

覆盖两处新逻辑：

  1. `enforce_sector_quota()` —— 行业配额硬校验
     SYSTEM_PROMPT 里要求了「同行业最多 2 只」，但那只是提示，模型可能不遵守。
     所以校验层必须硬执行，并且**不足要补位** —— 否则 AI 选 3 只银行被踢掉
     之后名单就不满，用户看到 6 只而不是 8 只。

  2. `market_context()` —— 市场背景
     给 AI 涨跌家数 + 板块涨跌前后列。离线时必须返回空串（不能联网），
     拿到时不能挡住选股。

写法沿用 test_screener.py 的适配层：模块级函数 + 裸 assert，
外面套一层薄壳接进 harness。断言没变，只是多一个注册入口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QUANT_OFFLINE", "1")

QUANT = Path(__file__).resolve().parent.parent
if str(QUANT) not in sys.path:
    sys.path.insert(0, str(QUANT))

from harness import TestCase, REGISTRY, declare_counts, run_all, module_summary  # noqa: E402
from stock_ai import (enforce_sector_quota, market_context,                     # noqa: E402
                      _build_user_prompt)


def _cands():
    """固定的一批候选：银行扎堆 3 只、电子 2 只、其它 2 只。"""
    return [
        {"symbol": "sh600001", "name": "A", "sector": "银行", "l2Score": 90},
        {"symbol": "sh600002", "name": "B", "sector": "银行", "l2Score": 85},
        {"symbol": "sh600003", "name": "C", "sector": "银行", "l2Score": 80},
        {"symbol": "sh600004", "name": "D", "sector": "电子", "l2Score": 70},
        {"symbol": "sh600005", "name": "E", "sector": "电子", "l2Score": 60},
        {"symbol": "sh600006", "name": "F", "sector": "其它", "l2Score": 50},
        {"symbol": "sh600007", "name": "G", "sector": "其它", "l2Score": 40},
    ]


# ══════════════════════════════════════════════════════════════
#  行业配额
# ══════════════════════════════════════════════════════════════

def test_quota_drops_third_same_sector():
    """AI 选了 3 只银行 → 第 3 只必须被踢出去。"""
    picks = [{"symbol": "sh600001"}, {"symbol": "sh600002"}, {"symbol": "sh600003"}]
    out, problems = enforce_sector_quota(picks, _cands(), n_pick=4)
    syms = [p["symbol"] for p in out]
    assert "sh600003" not in syms, f"第三只银行应被踢出，实际 {syms}"
    assert any("行业配额" in p for p in problems), problems


def test_quota_backfills_to_n_pick():
    """被踢之后名单不能不满，要按 L2 分补位。"""
    picks = [{"symbol": "sh600001"}, {"symbol": "sh600002"}, {"symbol": "sh600003"}]
    out, _ = enforce_sector_quota(picks, _cands(), n_pick=4)
    assert len(out) == 4, f"应补足到 4 只，实际 {len(out)}"
    assert out[2]["symbol"] == "sh600004", out


def test_quota_marks_filled():
    """补位项必须标 filled，前端才能区分「AI 选的」和「按分补的」。"""
    out, _ = enforce_sector_quota([{"symbol": "sh600001"}], _cands(), n_pick=3)
    filled = [p for p in out if p.get("filled")]
    assert filled, "补位项应带 filled 标记"
    assert "非 AI" in filled[0]["thesis"], filled[0]["thesis"]


def test_quota_other_sector_does_not_compete():
    """「其它」不是一个真实行业，不能互相挤配额。"""
    picks = [{"symbol": "sh600006"}, {"symbol": "sh600007"}]
    out, _ = enforce_sector_quota(picks, _cands(), n_pick=2)
    assert len(out) == 2, f"「其它」不应被配额挤掉，实际 {len(out)}"


def test_quota_rank_contiguous():
    """输出名次必须从 1 连续编号 —— 前端直接显示 rank。"""
    out, _ = enforce_sector_quota(
        [{"symbol": "sh600001"}, {"symbol": "sh600002"}], _cands(), n_pick=5)
    assert [p["rank"] for p in out] == list(range(1, len(out) + 1)), out


def test_quota_respects_custom_limit():
    """max_per_sector 可配：设成 1 时银行只能留 1 只。"""
    picks = [{"symbol": "sh600001"}, {"symbol": "sh600002"}]
    out, _ = enforce_sector_quota(picks, _cands(), n_pick=2, max_per_sector=1)
    banks = [p for p in out if p["symbol"] in ("sh600001", "sh600002")]
    assert len(banks) <= 1, [p["symbol"] for p in banks]


def test_quota_never_invents_symbols():
    """要的比候选池还多时，最多给到候选数量，不能造出假标的。"""
    out, _ = enforce_sector_quota([], _cands(), n_pick=99, max_per_sector=99)
    assert len(out) == len(_cands()), len(out)
    pool = {c["symbol"] for c in _cands()}
    assert all(p["symbol"] in pool for p in out), [p["symbol"] for p in out]


# ══════════════════════════════════════════════════════════════
#  市场背景
# ══════════════════════════════════════════════════════════════

def test_market_context_offline_is_empty():
    """离线时必须返回空串 —— 不能偷偷联网。"""
    old = os.environ.get("QUANT_OFFLINE")
    os.environ["QUANT_OFFLINE"] = "1"
    try:
        assert market_context() == "", "离线应返回空串"
    finally:
        if old is None:
            os.environ.pop("QUANT_OFFLINE", None)
        else:
            os.environ["QUANT_OFFLINE"] = old


def test_prompt_includes_context_block():
    """有背景时要插到候选清单前面，不能丢。"""
    pr = _build_user_prompt(_cands(), 3, "", "今日全市场：上涨 3301 / 下跌 2127")
    assert "今日市场背景" in pr, pr[:200]
    assert "上涨 3301" in pr, pr[:400]


def test_prompt_without_context_unchanged():
    """没有背景时不该出现空标题 —— 免得模型以为数据缺失。"""
    pr = _build_user_prompt(_cands(), 3)
    assert "今日市场背景" not in pr
    assert "以下是 7 只候选股票" in pr


# ══════════════════════════════════════════════════════════════
#  适配层：模块级用例 → harness 方法
# ══════════════════════════════════════════════════════════════
_CASES = [
    test_quota_drops_third_same_sector,
    test_quota_backfills_to_n_pick,
    test_quota_marks_filled,
    test_quota_other_sector_does_not_compete,
    test_quota_rank_contiguous,
    test_quota_respects_custom_limit,
    test_quota_never_invents_symbols,
    test_market_context_offline_is_empty,
    test_prompt_includes_context_block,
    test_prompt_without_context_unchanged,
]


def _make_case(fn):
    def run_case(self):
        try:
            fn()
        except AssertionError as e:
            self.eq(f"FAIL: {e}", "PASS", f"{fn.__name__} 断言失败")
            return
        except Exception as e:                                 # noqa: BLE001
            self.eq(f"{type(e).__name__}: {e}", "PASS", f"{fn.__name__} 抛异常")
            return
        self.eq("PASS", "PASS", f"{fn.__name__} 全部断言通过")
    run_case.__name__ = fn.__name__
    run_case.__doc__ = fn.__doc__
    return run_case


class TestAISelection(TestCase):
    """AI 选股增强：行业配额 + 市场背景。"""
    name = "ai.选股增强（离线）"


for _fn in _CASES:
    setattr(TestAISelection, _fn.__name__, _make_case(_fn))

REGISTRY.append(TestAISelection)

declare_counts({
    "TestAISelection": {fn.__name__: (1, 1) for fn in _CASES},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_stock_ai.py"))
