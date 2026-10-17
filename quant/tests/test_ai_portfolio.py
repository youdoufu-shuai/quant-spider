#!/usr/bin/env python3
"""
AI 组合测试 —— 全部离线，构造数据。

⚠️ 这个模块**处理钱**（资金分配、整手取整、成本计算），
   算错的后果比别的模块严重得多，所以单独一个测试文件。

⚠️ 离线：ai_portfolio.refresh() 会拉行情，但建仓与汇总不联网。
   涉及 refresh 的用临时 state 文件 + 注入好的价格绕过。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
REPO = QUANT.parent
for p in (HERE, REPO, QUANT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("QUANT_OFFLINE", "1")

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
import ai_portfolio as AP                                  # noqa: E402


def tmp_store() -> AP.PortfolioStore:
    """每次一个全新的临时 store，不污染真实 state。"""
    d = Path(tempfile.mkdtemp(prefix="pf-test-"))
    return AP.PortfolioStore(state_file=d / "pf.json")


def mk_picks(n: int = 3, price: float = 10.0) -> list[dict]:
    """造 n 条选股结果。"""
    return [{
        "symbol": f"sh60{i:04d}",
        "name": f"测试股{i}",
        "sector": ["银行", "钢铁", "电子", "汽车", "传媒"][i % 5],
        "price": price + i,
        "thesis": f"测试逻辑{i}",
        "confidence": 0.6,
    } for i in range(n)]


# ══════════════════════════════════════════════════════════════
def test_equal_weight_allocation():
    """
    ⚠️ 等权分配是核心设计 —— 每只股票的**预算**必须相等。

    为什么等权：这是最难作弊的基准。按分数/市值加权看起来更「聪明」，
    但很容易变成过拟合；等权如果跑不赢指数，说明选股本身没价值。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=300_000.0)
    budget = 300_000.0 / 3
    for i in range(3):
        p.add_holding(symbol=f"sh60{i:04d}", name=f"股{i}", sector="银行",
                      price=10.0, budget=budget, lot=100)
    # 每只花的钱（含费）应接近 budget
    for h in p.holdings:
        spent = h.cost()
        assert abs(spent - budget) < budget * 0.02, \
            f"{h.name} 花了 {spent:.0f}，预算 {budget:.0f}"


def test_lot_rounding_a_share():
    """
    ⚠️ A 股必须整手（100 股）。这条测死：
       预算买不满一手 → 不建仓（记问题），不能买 50 股。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=100_000.0)
    # 预算 1000 元买 50 元的股票 → 只能买 20 股 → 不足一手
    ok = p.add_holding(symbol="sh600000", name="贵股", sector="银行",
                       price=50.0, budget=1000.0, lot=100)
    assert ok is False, "买不起一手时不该建仓"
    assert any("买不起一手" in x for x in p.problems), p.problems
    # 预算够一手就正常
    ok2 = p.add_holding(symbol="sh600001", name="便宜股", sector="银行",
                        price=5.0, budget=1000.0, lot=100)
    assert ok2 is True
    h = p.holdings[0]
    assert h.qty % 100 == 0, f"必须是整手，实得 {h.qty}"
    assert h.qty == 100, f"1000 元买 5 元股票应买 100 股，实得 {h.qty}"


def test_crypto_has_no_lot_requirement():
    """加密没有整手限制（lot=1），能买小数。"""
    store = tmp_store()
    p = store.create(strategy="t", initial=10_000.0, market="crypto")
    ok = p.add_holding(symbol="BTCUSDT", name="BTC", sector="其它",
                       price=50000.0, budget=5000.0, lot=1)
    assert ok is True, f"加密应能买入小数数量，问题：{p.problems}"
    h = p.holdings[0]
    # ⚠️ 数量必须保留小数。第一版实现里 `int(raw_qty)` 会取成 0，
    #    直接判定「买不起一手」—— 而加密根本没有整手概念。
    expect = 5000 / (50000 * (1 + p.costs.slippage) * (1 + p.costs.commission))
    assert h.qty > 0, f"加密数量应 > 0，实得 {h.qty}"
    assert h.qty == expect, f"应得 {expect}，实得 {h.qty}"
    assert h.qty < 1, f"5000 元买 5 万美元的币应不足 1 个，实得 {h.qty}"


def test_cash_never_goes_negative():
    """
    ⚠️ 现金绝不能为负 —— 那等于凭空加杠杆。
       真实情况：多只股票各自按预算算，累加后可能略微超过总资金，
       必须靠「现金不足则按可用现金重算」兜住。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=100_000.0)
    # 5 只，每只预算 20000（合计 100000），价格都能买满
    for i in range(5):
        p.add_holding(symbol=f"sh60{i:04d}", name=f"股{i}", sector="银行",
                      price=10.0, budget=20_000.0, lot=100)
    assert p.cash >= -1e-6, f"现金变负了：{p.cash}"
    total_cost = sum(h.cost() for h in p.holdings)
    assert abs(total_cost + p.cash - p.initial) < 1e-6, \
        f"成本 {total_cost:.2f} + 现金 {p.cash:.2f} != 初始 {p.initial:.2f}"


def test_costs_are_included():
    """
    ⚠️ 成本必须计入，否则收益虚高。
       买入价含滑点、另收手续费 —— 所以建仓当天的收益是**负的**。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=1_000_000.0)
    p.add_holding(symbol="sh600000", name="测试", sector="银行",
                  price=10.0, budget=1_000_000.0, lot=100)
    s = p.summary()
    # 建仓当天、价格没变 → 收益必然为负（滑点+手续费）
    assert s["totalReturn"] < 0, f"建仓当天收益应为负，实得 {s['totalReturn']}"
    # 但不能亏太多（滑点 0.05% + 佣金 0.03%）
    assert s["totalReturn"] > -1.0, f"成本异常高：{s['totalReturn']}%"
    # 买入价必须高于原始价（滑点）
    assert p.holdings[0].entry_price > 10.0


def test_summary_return_math():
    """
    收益计算：涨 10% 时组合收益应约 +10%（减去成本）。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=1_000_000.0)
    p.add_holding(symbol="sh600000", name="测试", sector="银行",
                  price=10.0, budget=1_000_000.0, lot=100)
    # 手动把现价改成 11（涨 10%）
    p.holdings[0].last_price = 11.0
    s = p.summary()
    # ⚠️ 期望值算清楚再写，别拍脑袋：
    #   买入价含滑点 10.005，数量取整到 99,900 股，剩现金约 200 元。
    #   现价 11 → 市值 1,098,900 + 现金 200 = 1,099,100，
    #   收益 = 1,099,100/1,000,000 - 1 = 9.91%。
    #   （第一版我写 8.5~9.5 是错的 —— 把「涨 10%」和「涨 9%」搞混了。）
    assert 9.5 < s["totalReturn"] < 10.1, \
        f"涨 10% 时收益应约 9.9%，实得 {s['totalReturn']}"
    assert s["pnl"] > 0


def test_benchmark_and_excess_return():
    """
    超额收益 = 组合收益 − 基准收益。基准拿不到时必须是 None，不编造。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=1_000_000.0)
    p.add_holding(symbol="sh600000", name="测试", sector="银行",
                  price=10.0, budget=1_000_000.0, lot=100)
    p.holdings[0].last_price = 11.0

    # 没基准 → excessReturn 必须是 None
    s1 = p.summary()
    assert s1["benchmark"]["return"] is None
    assert s1["excessReturn"] is None, "没基准就不能算超额"

    # 有基准：组合 +9%，基准 +3% → 超额 +6%
    p.benchmark_start = 3000.0
    p.benchmark_last = 3090.0
    s2 = p.summary()
    assert abs(s2["benchmark"]["return"] - 3.0) < 1e-9
    assert abs(s2["excessReturn"] - (s2["totalReturn"] - 3.0)) < 1e-9
    assert s2["excessReturn"] > 0


def test_holdings_sorted_by_pnl():
    """持仓按盈亏排序，最赚的在前面 —— 页面直接按这个顺序显示。"""
    store = tmp_store()
    p = store.create(strategy="t", initial=300_000.0)
    for i, px in enumerate((10.0, 20.0, 30.0)):
        p.add_holding(symbol=f"sh60{i:04d}", name=f"股{i}", sector="银行",
                      price=px, budget=100_000.0, lot=100)
    # 第 1 只涨 20%，第 2 只不动，第 3 只跌 10%
    p.holdings[0].last_price = p.holdings[0].entry_price * 1.2
    p.holdings[2].last_price = p.holdings[2].entry_price * 0.9
    s = p.summary()
    pcts = [h["pnlPct"] for h in s["holdings"]]
    assert pcts == sorted(pcts, reverse=True), f"应按盈亏降序: {pcts}"
    # ⚠️ 只有 1 只盈利 —— 因为「价格没变」的那只**买价含滑点**，
    #    现价等于原始价也算小幅亏损（-0.05%）。这是成本的真实影响，
    #    第一版我写 lossCount==1 是没考虑到滑点。
    assert s["winCount"] == 1, f"只有 1 只涨了，实得 {s['winCount']}"
    assert s["lossCount"] == 2, (
        f"价格没变的那只也算小亏（滑点），应有 2 只亏损，实得 {s['lossCount']}")


def test_persistence_roundtrip():
    """组合要能存下来再读回来 —— 否则服务重启收益就没了。"""
    store = tmp_store()
    p = store.create(strategy="dual_low", label="测试组合",
                     mode="ai", initial=500_000.0)
    p.add_holding(symbol="sh600000", name="测试", sector="银行",
                  price=10.0, budget=250_000.0, lot=100)
    p.holdings[0].last_price = 12.0
    p.benchmark_start = 3000.0
    p.benchmark_last = 3030.0
    store.save()

    store2 = AP.PortfolioStore(state_file=store.state_file)
    assert len(store2.portfolios) == 1
    p2 = list(store2.portfolios.values())[0]
    assert p2.id == p.id
    assert p2.label == "测试组合"
    assert p2.mode == "ai"
    assert len(p2.holdings) == 1
    assert p2.holdings[0].symbol == "sh600000"
    assert p2.holdings[0].last_price == 12.0
    assert p2.benchmark_start == 3000.0
    # 收益要能复算出来
    s = p2.summary()
    assert s["totalReturn"] > 0


def test_delete_and_list():
    """删除与列表。"""
    store = tmp_store()
    p1 = store.create(strategy="a")
    p2 = store.create(strategy="b")
    assert len(store.list()) == 2
    assert store.delete(p1.id) is True
    assert len(store.list()) == 1
    assert store.delete("不存在") is False
    assert store.get(p2.id) is not None


def test_create_from_picks():
    """从选股结果建仓的完整路径。"""
    store = tmp_store()
    picks = mk_picks(4, price=10.0)
    out = AP.create_from_picks(picks, strategy="dual_low", initial=400_000.0,
                               store=store)
    assert out["holdingsCount"] == 4
    assert out["initial"] == 400_000.0
    # 等权
    weights = [h["weight"] for h in out["holdings"]]
    for w in weights:
        assert abs(w - 25.0) < 1.0, f"等权应约 25%，实得 {w}"
    # 理由要保留（AI 给的 thesis）
    assert any("测试逻辑" in h["reason"] for h in out["holdings"])


def test_create_from_picks_rejects_empty():
    """空名单不能建组合 —— 否则会建出一个空账户。"""
    store = tmp_store()
    try:
        AP.create_from_picks([], strategy="t", store=store)
    except ValueError:
        pass
    else:
        raise AssertionError("空名单应该抛 ValueError")
    assert len(store.list()) == 0, "失败的建仓不该留下空组合"


def test_create_from_picks_caps_holdings():
    """超过 MAX_HOLDINGS 要截断，不能建出几百只的账户。"""
    store = tmp_store()
    picks = mk_picks(AP.MAX_HOLDINGS + 10, price=10.0)
    out = AP.create_from_picks(picks, strategy="t", initial=1_000_000.0,
                               store=store)
    assert out["holdingsCount"] <= AP.MAX_HOLDINGS


def test_invalid_price_is_skipped_not_crash():
    """价格无效（0/None/负数）的标的不让整个建仓失败，记问题跳过。"""
    store = tmp_store()
    picks = [
        {"symbol": "sh600000", "name": "正常", "price": 10.0, "sector": "银行"},
        {"symbol": "sh600001", "name": "零价", "price": 0.0, "sector": "银行"},
        {"symbol": "sh600002", "name": "无价", "sector": "银行"},
        {"symbol": "sh600003", "name": "正常2", "price": 20.0, "sector": "钢铁"},
    ]
    out = AP.create_from_picks(picks, strategy="t", initial=400_000.0,
                               store=store)
    assert out["holdingsCount"] == 2, f"应只建 2 只，实得 {out['holdingsCount']}"
    assert len(out["problems"]) >= 2, out["problems"]


def test_sector_distribution_in_summary():
    """组合的行业分布要正确统计（页面显示用）。"""
    store = tmp_store()
    p = store.create(strategy="t", initial=500_000.0)
    for i, sec in enumerate(("银行", "银行", "钢铁", "电子", "汽车")):
        p.add_holding(symbol=f"sh60{i:04d}", name=f"股{i}", sector=sec,
                      price=10.0, budget=100_000.0, lot=100)
    s = p.summary()
    assert s["sectors"]["银行"] == 2
    assert s["sectors"]["钢铁"] == 1
    assert s["topSector"]["name"] == "银行"
    assert s["topSector"]["count"] == 2


def test_summary_is_strict_json_serializable():
    """组合摘要要能过严格 JSON（API 直接吐给前端）。"""
    store = tmp_store()
    picks = mk_picks(3)
    out = AP.create_from_picks(picks, strategy="t", initial=300_000.0,
                               store=store)
    txt = json.dumps(out, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in txt and "Infinity" not in txt
    assert json.loads(txt)["holdingsCount"] == 3


def test_max_portfolios_evicts_only_empty():
    """
    ⚠️ 组合数超上限时，只能淘汰**空仓**的组合。
       有持仓的不能静默删除 —— 用户还要看它的收益。
    """
    store = tmp_store()
    # 先建满上限，全部空仓
    made = [store.create(strategy=f"s{i}") for i in range(AP.MAX_PORTFOLIOS)]
    assert len(made) == AP.MAX_PORTFOLIOS
    # 给最后一个加个持仓
    made[-1].add_holding(symbol="sh600000", name="有仓", sector="银行",
                         price=10.0, budget=10_000.0, lot=100)
    with_holding = made[-1].id
    # 再建一个 → 应该淘汰某个空仓的，有仓的必须还在
    store.create(strategy="new")
    assert with_holding in store.portfolios, "有持仓的组合被误删了"
    assert len(store.portfolios) == AP.MAX_PORTFOLIOS


def test_refresh_tolerates_single_failure():
    """
    ⚠️ 单只拉不到不该让整个组合报错，也不能改坏已有价格。
       这只在真联网时才有意义，所以用「不存在的代码」验证降级。
    """
    store = tmp_store()
    p = store.create(strategy="t", initial=100_000.0)
    # 造一个绝对拉不到的代码
    p.add_holding(symbol="sh999999", name="不存在", sector="其它",
                  price=10.0, budget=50_000.0, lot=100)
    before = p.holdings[0].last_price
    # 不联网环境下 refresh 会把失败记录下来，且不崩
    try:
        r = p.refresh(verbose=False)
    except Exception as e:                              # noqa: BLE001
        raise AssertionError(f"refresh 不该抛异常：{e}") from e
    assert "failed" in r and isinstance(r["failed"], list)
    # 价格要么保持原值，要么被成功更新，不能被弄坏成 None
    assert p.holdings[0].last_price is not None
    if r["failed"]:
        assert p.holdings[0].last_price == before, "拉取失败时不该改价格"


# ══════════════════════════════════════════════════════════════
#  适配层（同 test_screener.py 的做法：包成 harness.TestCase）
# ══════════════════════════════════════════════════════════════
_CASES = [(n, f) for n, f in sorted(globals().items())
          if n.startswith("test_") and callable(f)]


def _make_case(fn):
    def run_case(self):
        try:
            fn()
        except AssertionError as e:
            self.eq(f"FAIL: {e}", "PASS", f"{fn.__name__} 断言失败")
            return
        except Exception as e:                          # noqa: BLE001
            self.eq(f"{type(e).__name__}: {e}", "PASS",
                    f"{fn.__name__} 抛异常")
            return
        self.eq("PASS", "PASS", f"{fn.__name__} 全部断言通过")
    run_case.__name__ = fn.__name__
    run_case.__doc__ = fn.__doc__
    return run_case


class TestAIPortfolio(TestCase):
    """AI 组合（多标的模拟盘）的资金、成本、收益与持久化。"""
    name = "portfolio.AI 组合（离线构造数据）"


for _n, _f in _CASES:
    setattr(TestAIPortfolio, _n, _make_case(_f))

REGISTRY.append(TestAIPortfolio)

declare_counts({"TestAIPortfolio": {n: (1, 1) for n, _ in _CASES}})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_ai_portfolio.py"))
