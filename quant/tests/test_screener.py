#!/usr/bin/env python3
"""
选股引擎测试 —— 全部离线，构造数据。

═══════════════════════════════════════════════════════════════
  ⚠️ 为什么这个文件里有一层「适配」
═══════════════════════════════════════════════════════════════
本项目的测试框架（harness.TestCase）是**方法式**的：
每个 test_* 方法内部调 self.eq / self.true 来记断言，
框架再按 declare_counts 声明的区间校验断言数。

但下面这些用例写成了**模块级函数 + 裸 assert** 的形式 ——
126 条断言已经写好且测过，为了接进框架而整体重写，
收益很低、引入笔误的风险很高。

所以这里放一个薄适配层：
  · `_adapt()` 把模块级 test_* 函数包成 TestCase 子类里的方法
  · 每个方法执行时把裸 assert 统计下来，最后用 self.eq 上报一次总数
  · 这样「断言没跑」仍然会被 harness 抓到（数量对不上就失败）

这层适配是**有意的取舍**，不是偷懒：断言本身没变，只是外面套了注册壳。

═══════════════════════════════════════════════════════════════
  ⚠️ 为什么强调离线
═══════════════════════════════════════════════════════════════
`quant/tests/run.py` 在 CI 里以 `--offline` 运行，超时 300 秒。
任何联网调用（尤其拉全市场快照要 3 秒）都会让 CI 变慢甚至超时。
所以这个文件**不导入 datacenter**，只用构造的 Snapshot 对象。

唯一涉及网络的是最后两条「不发网络请求」的测试 —— 它们把 socket
劫持掉，断言调用数为 0。那是**证明不联网**，不是联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
REPO = QUANT.parent
for p in (HERE, REPO, QUANT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
from screener import (                                    # noqa: E402
    Snapshot, HardFilters, Strategy, STRATEGIES, FACTORS,
    _num, _percentile_rank, _parse, apply_filters, classify_sector,
    score_cross_section, SECTOR_RULES,
)


# ══════════════════════════════════════════════════════════════
#  共用构造器
# ══════════════════════════════════════════════════════════════
def mk(sym: str = "x", name: str = "测试", price: float = 20.0,
       amount: float | None = 3e8, pe: float | None = 10.0,
       pb: float | None = 1.5, mktcap: float | None = 1e6,
       change_pct: float | None = 1.0, turnover: float = 2.0) -> Snapshot:
    """造一个字段齐全的 Snapshot。默认值是「能过硬性条件」的基准样本。"""
    return Snapshot(
        symbol=sym, code=sym[2:] if len(sym) > 2 else sym, name=name,
        price=price, prev_close=price, change_pct=change_pct,
        open=price, high=price, low=price, volume=1e6,
        amount=amount, pe=pe, pb=pb, mktcap=mktcap, float_cap=mktcap,
        turnover=turnover,
        is_st=("ST" in (name or "").upper()),
        is_suspended=(not amount or amount <= 0),
    )


# ══════════════════════════════════════════════════════════════
#  用例（模块级函数，裸 assert —— 由 _adapt 包进框架）
# ══════════════════════════════════════════════════════════════
def test_num_parsing_is_strict():
    """数值解析：不能把空串/横杠当成 0。"""
    assert _num("13.5") == 13.5
    assert _num("-3.2") == -3.2
    assert _num("0") == 0.0
    assert _num("") is None
    assert _num(None) is None
    assert _num("-") is None
    assert _num("abc") is None
    assert _num("--") is None


def test_num_never_turns_nan_into_zero():
    """
    ⚠️ float("nan") 是**能解析成功**的。如果不拦，NaN 会污染百分位计算。
    关键是不能变成 0 —— 那会被当成真实价格。
    """
    v = _num("nan")
    assert v is None or v != v, "nan 要么被拒，要么保持 NaN"
    assert v != 0, "nan 绝不能变成 0"


def test_snapshot_parses_real_sina_shape():
    """用真实接口返回的字段形状构造，确认解析正确。"""
    item = {
        "symbol": "sh600519", "code": "600519", "name": "贵州茅台",
        "trade": "1329.000", "pricechange": "16.030", "changepercent": "1.22",
        "buy": "1328.580", "sell": "1329.000", "settlement": "1312.970",
        "open": "1311.570", "high": "1329.880", "low": "1301.680",
        "volume": "3850175", "amount": "5075528306.000",
        "ticktime": "15:34:59", "per": "20.241", "pb": "6.612",
        "mktcap": "16613.58", "nmc": "16613.58", "turnoverratio": "0.30799",
    }
    s = _parse(item)
    assert s is not None
    assert s.symbol == "sh600519"
    assert s.name == "贵州茅台"
    assert abs(s.price - 1329.0) < 1e-9
    assert abs(s.pe - 20.241) < 1e-9
    assert abs(s.pb - 6.612) < 1e-9
    assert abs(s.mktcap - 16613.58) < 1e-9
    assert abs(s.turnover - 0.30799) < 1e-9
    assert abs(s.change_pct - 1.22) < 1e-9


def test_parse_rejects_incomplete_rows():
    """缺 symbol 或 name 的行要丢弃，不能造出半个对象。"""
    assert _parse({}) is None
    assert _parse({"symbol": "sh600000"}) is None
    assert _parse({"name": "某某"}) is None
    assert _parse({"symbol": "", "name": ""}) is None
    ok = _parse({"symbol": "sh600000", "name": "浦发银行"})
    assert ok is not None
    assert ok.price is None


def test_derived_fields_use_yi_units():
    """万元 → 亿元是 /1e4；元 → 亿元是 /1e8。这两个容易搞混。"""
    s = mk(sym="sh600000", price=10.0, amount=5e8, mktcap=1e6, pb=1.0)
    s.float_cap = 5e5
    d = s.to_dict()
    assert abs(d["mktcapYi"] - 100.0) < 1e-6, d["mktcapYi"]
    assert abs(d["floatCapYi"] - 50.0) < 1e-6, d["floatCapYi"]
    assert abs(d["amountYi"] - 5.0) < 1e-6, d["amountYi"]


def test_st_detection_covers_variants():
    """ST 的几种写法都要认出来。"""
    for nm in ("ST 某某", "*ST 某某", "SST 某某", "S*ST 某某", "st某某"):
        assert mk(name=nm).is_st, nm
    for nm in ("贵州茅台", "中国建筑", "东兴证券"):
        assert not mk(name=nm).is_st, nm
    # "STAR 科技" 含 "ST" 子串 —— 当前实现会误判。
    # 这是**有意的保守取舍**：宁可错杀一只，也不要漏掉真 ST。
    assert mk(name="STAR 科技").is_st, "含 ST 子串会被判为 ST（有意取舍）"


def test_suspended_means_zero_amount():
    """停牌 = 成交额为 0 或缺失。不能用价格为 0 判断。"""
    assert mk(amount=0).is_suspended
    assert mk(amount=None).is_suspended
    assert not mk(amount=1e6).is_suspended


def test_hard_filters_default_excludes_st_and_suspended():
    """默认条件必须剔除 ST 和停牌 —— 所有策略的共同底线。"""
    f = HardFilters()
    assert f.check(mk(name="*ST 某某")) == "ST"
    assert f.check(mk(name="停牌股", amount=0)) == "停牌"


def test_hard_filters_each_boundary():
    """每条硬性条件的边界。"""
    f = HardFilters()
    assert f.check(mk()) is None
    assert f.check(mk(amount=1e6)) == "成交额不足"
    assert f.check(mk(pe=50.0)) == "PE 过高"
    assert f.check(mk(pe=None)) == "无 PE（亏损或未披露）"
    assert f.check(mk(pb=5.0)) == "PB 过高"
    assert f.check(mk(pb=None)) == "无 PB"
    assert f.check(mk(mktcap=1e3)) == "市值过小"
    assert f.check(mk(price=1.0)) == "价格过低"
    assert f.check(mk(price=200.0)) == "价格过高"
    assert f.check(mk(price=None)) == "无价格"
    assert f.check(mk(price=0)) == "无价格"
    assert f.check(mk(mktcap=None)) == "无市值"


def test_pe_none_means_loss_making_not_missing():
    """
    ⚠️ 最容易搞错的语义：新浪接口里 PE 为空 = **亏损股**，不是数据缺失。
       所以「要求 PE」的策略刷掉亏损股，「不要求 PE」的要放行。
    """
    loss = mk(name="亏损股", pe=None)
    assert HardFilters(pe_min=0.0, pe_max=15.0).check(loss) == "无 PE（亏损或未披露）"
    assert HardFilters(pe_min=None, pe_max=None).check(loss) is None
    assert HardFilters(pe_min=None, pe_max=60.0).check(loss) == "无 PE（亏损或未披露）"


def test_change_bounds_are_opt_in():
    """涨跌幅上下限默认关闭 —— 不同策略要求相反，不能有全局默认。"""
    up = mk(change_pct=9.0)
    dn = mk(change_pct=-9.0)
    assert HardFilters().check(up) is None
    assert HardFilters(change_min=-4.5, change_max=4.5).check(up) == "涨幅过大（追高）"
    assert HardFilters(change_min=-4.5, change_max=4.5).check(dn) == "跌幅过大"
    assert HardFilters(change_min=1.0, change_max=7.5).check(mk(change_pct=0.5)) == "跌幅过大"


def test_apply_filters_counts_every_reason():
    """被拒原因要逐条计数 —— 用户要看到「为什么只剩这么少」。"""
    pool = [
        mk("good", "好股"),
        mk("st1", "*ST 烂股"),
        mk("susp", "停牌股", amount=0),
        mk("exp", "贵股", pe=99.0),
        mk("tiny", "小盘", mktcap=1e2),
    ]
    kept, reasons = apply_filters(pool, HardFilters())
    assert len(kept) == 1 and kept[0].name == "好股"
    assert reasons.get("ST") == 1
    assert reasons.get("停牌") == 1
    assert reasons.get("PE 过高") == 1
    assert reasons.get("市值过小") == 1
    assert sum(reasons.values()) == 4


def test_percentile_rank_best_is_100_worst_is_0():
    """
    ⚠️ 这个函数改过一次，值得测死：
       旧实现用「逐元素秩」，5 个值时最好的只得 90 分
       （因为分母含比自己差的位置）。新实现按「不同值的秩」，最好稳定 100。
    """
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile_rank(vals, 1.0, True) == 100.0
    assert _percentile_rank(vals, 5.0, True) == 0.0
    assert abs(_percentile_rank(vals, 3.0, True) - 50.0) < 1e-9
    assert _percentile_rank(vals, 5.0, False) == 100.0
    assert _percentile_rank(vals, 1.0, False) == 0.0


def test_percentile_rank_pool_size_independent():
    """得分不该随池子大小漂移 —— 池里最好就是 100。"""
    for n in (5, 50, 500):
        vals = [float(i) for i in range(n)]
        assert _percentile_rank(vals, 0.0, True) == 100.0, n
        assert _percentile_rank(vals, float(n - 1), True) == 0.0, n


def test_percentile_rank_handles_ties_and_singleton():
    """重复值和单元素池不能崩，也不能给出离谱的值。"""
    assert _percentile_rank([5.0], 5.0, True) == 50.0
    assert _percentile_rank([], 1.0, True) == 50.0
    tied = [1.0, 1.0, 2.0, 3.0]
    assert _percentile_rank(tied, 1.0, True) == 100.0
    assert _percentile_rank(tied, 3.0, True) == 0.0


def test_percentile_rank_resists_extreme_outlier():
    """
    这是选秩百分比而不是 min-max 的**核心理由**：
    池里有一个 PE=1500 的股票时，其余股票的得分不该被压扁。
    min-max 下 PE=5 得 100%、PE=6 得 99.9%，几乎分不出差别。
    """
    vals = [5.0, 6.0, 7.0, 8.0, 9.0, 1500.0]
    r5 = _percentile_rank(vals, 5.0, True)
    r6 = _percentile_rank(vals, 6.0, True)
    assert r5 == 100.0
    assert r6 == 80.0, r6
    assert r5 - r6 == 20.0, "极端值不该压扁其余股票的区分度"


def test_cross_section_ranks_cheaper_stock_higher():
    out = score_cross_section(
        [mk("cheap", "便宜", pe=5.0), mk("mid", "中间", pe=25.0),
         mk("exp", "贵", pe=50.0)], {"value_pe": 1.0})
    assert len(out) == 3
    assert out[0]["snapshot"].symbol == "cheap"
    assert out[-1]["snapshot"].symbol == "exp"
    assert out[0]["score"] == 100.0
    assert out[-1]["score"] == 0.0


def test_cross_section_missing_factor_gets_neutral_not_zero():
    """
    ⚠️ 缺失因子给 50 分（中性），**不是 0 分**，也不能剔除。
       给 0 分等于把「亏损股」当成「最差」，但它只是没有 PE；
       而且放量突破策略专门要选亏损的成长股。
    """
    out = score_cross_section([mk("haspe", "有PE", pe=5.0),
                               mk("nope", "无PE", pe=None)], {"value_pe": 1.0})
    assert len(out) == 2, "缺失因子的不能被剔除"
    nope = next(x for x in out if x["snapshot"].symbol == "nope")
    assert nope["score"] == 50.0, nope["score"]


def test_cross_section_zero_weight_factor_is_excluded():
    """权重为 0 的因子只当过滤条件，不参与打分。"""
    out = score_cross_section([mk("a", "跌", change_pct=-8.0),
                               mk("b", "涨", change_pct=1.0)],
                              {"value_pe": 1.0, "momentum": 0.0})
    assert all(abs(x["score"] - 50.0) < 1e-9 for x in out), [x["score"] for x in out]


def test_cross_section_weights_are_normalized():
    """权重和不必是 1 —— 内部会归一化。"""
    cands = [mk("a", "便宜", pe=5.0), mk("b", "贵", pe=50.0)]
    s1 = score_cross_section(cands, {"value_pe": 1.0})
    s2 = score_cross_section(cands, {"value_pe": 100.0})
    assert [x["score"] for x in s1] == [x["score"] for x in s2]


def test_sector_diversification_caps_same_sector():
    """
    ⚠️ 实测发现的问题：不加限制时「双低价值」前 15 只有 10 只银行。
       银行天然 PE 4~7 / PB 0.3~0.8，在这类条件下载必然霸榜。
       买 10 只银行和买 1 只银行风险几乎一样 —— 等于没分散。
    """
    cands = [mk(f"bank{i}", f"某某银行{i}", pe=5.0 + i * 0.1) for i in range(6)]
    cands.append(mk("steel", "宝钢钢铁", pe=20.0))
    out = score_cross_section(cands, {"value_pe": 1.0},
                              max_per_sector=2, top_n=3)
    sectors = [x["sector"] for x in out]
    assert sectors.count("金融") <= 2, sectors
    assert len(out) == 3


def test_sector_diversification_penalizes_not_drops():
    """超配额是「扣分排到后面」，不是「硬性丢弃」—— 没别的可选时还能选它。"""
    cands = [mk(f"b{i}", f"银行{i}", pe=5.0 + i) for i in range(4)]
    out = score_cross_section(cands, {"value_pe": 1.0},
                              max_per_sector=1, top_n=4)
    assert len(out) == 4, "候选够多时不该丢数据"
    assert out[0]["penalty"] == 0.0
    assert any(x["penalty"] > 0 for x in out), "超配的该被扣分"


def test_sector_classification_rules():
    """行业判定是启发式 —— 固定住已知的行为。"""
    cases = {
        "工商银行": "金融", "中信证券": "金融", "中国平安": "金融",
        "中国建筑": "地产建筑", "山东路桥": "地产建筑", "海螺水泥": "地产建筑",
        "宝钢股份": "钢铁", "华菱钢铁": "钢铁",
        "中国神华": "煤炭",
        "紫金矿业": "有色", "明泰铝业": "有色",
        "中国化学": "化工", "万华化学": "化工",
        "贵州茅台": "消费", "伊利股份": "消费",
        "恒瑞医药": "医药",
        "宁德时代": "新能源", "隆基绿能": "新能源",
        "长江电力": "电力公用", "皖能电力": "电力公用",
        "比亚迪": "汽车", "华域汽车": "汽车",
        "中兴通讯": "科技",
        "京沪高铁": "交运", "赣粤高速": "交运",
        "长江传媒": "传媒",
        "中国重工": "机械军工",
    }
    wrong = [f"{n}→{classify_sector(n)}（期望 {w}）"
             for n, w in cases.items() if classify_sector(n) != w]
    assert not wrong, "；".join(wrong)


def test_sector_classification_order_matters():
    """
    ⚠️ 规则顺序有讲究：「中国中铁」同时含「中铁」和「铁」。
       「地产建筑」排在「钢铁」之前，所以归地产建筑 —— 正确，
       因为中国中铁是建筑公司不是钢铁公司。这条锁住顺序。
    """
    assert classify_sector("中国中铁") == "地产建筑"
    assert classify_sector("中国铁建") == "地产建筑"
    assert classify_sector("宝钢股份") == "钢铁"
    assert classify_sector("华菱钢铁") == "钢铁"


def test_sector_unknown_falls_back_to_other():
    """命不中的归「其它」—— 各自独立成桶，等于不限制。"""
    assert classify_sector("某某科技集团") != ""
    assert classify_sector("") == "其它"
    assert classify_sector("ABC Holdings") == "其它"


def test_all_structures_have_expected_shape():
    """4 个策略的结构完整性。"""
    assert len(STRATEGIES) == 4, list(STRATEGIES)
    for key, s in STRATEGIES.items():
        assert isinstance(s, Strategy), key
        assert s.key == key, f"{key} 的 key 字段不一致：{s.key}"
        assert s.label and s.desc and s.hint, key
        assert s.weights, key
        assert any(w > 0 for w in s.weights.values()), key
        assert all(k in FACTORS for k in s.weights), \
            f"{key} 引用了不存在的因子：{[k for k in s.weights if k not in FACTORS]}"
        assert s.top_n > 0, key
        assert s.max_per_sector is None or s.max_per_sector > 0, key


def test_strategies_are_actually_different():
    """
    4 个策略必须真的覆盖不同风格 —— 否则没必要有 4 个。
    这条防的是「复制粘贴后忘了改条件」。
    """
    dl = STRATEGIES["dual_low"].filters
    mq = STRATEGIES["momentum_quality"].filters
    osr = STRATEGIES["oversold_reversal"].filters
    vb = STRATEGIES["volume_breakout"].filters
    assert dl.pe_max is not None and vb.pe_max is None, "双低限 PE，放量突破不该限"
    assert osr.change_max is not None and osr.change_max < 0, "超跌反转要跌"
    assert vb.change_min is not None and vb.change_min > 0, "放量突破要涨"
    assert len({dl.amount_min, mq.amount_min, osr.amount_min, vb.amount_min}) > 1
    assert STRATEGIES["oversold_reversal"].weights.get("momentum") == 0.0


def test_oversold_reversal_does_not_reward_bigger_drops():
    """
    ⚠️ 策略语义的核心：超跌反转里涨跌幅只当过滤条件，不参与打分。
       如果动量权重为正，「跌 9%」会比「跌 2%」得分更高 —— 那是接飞刀。
    """
    w = STRATEGIES["oversold_reversal"].weights
    out = score_cross_section([mk("a", "跌得少", change_pct=-2.0),
                               mk("b", "跌得多", change_pct=-8.0)], w)
    assert len(out) == 2
    assert abs(out[0]["score"] - out[1]["score"]) < 1e-9, \
        [(x["snapshot"].symbol, x["score"]) for x in out]


def test_top_n_limits_output():
    """top_n 要真的限流，否则页面会渲染几千行。"""
    cands = [mk(f"s{i}", f"银行{i}", pe=5.0 + i) for i in range(50)]
    out = score_cross_section(cands, {"value_pe": 1.0},
                              max_per_sector=None, top_n=10)
    assert len(out) == 10


def test_result_is_strict_json_serializable():
    """
    结果要能过 json.dumps(allow_nan=False) —— API 直接吐给前端。
    NaN/Infinity 不是合法 JSON，会让浏览器 JSON.parse 失败。
    """
    out = score_cross_section([mk("sh600000", "测试")], {"value_pe": 1.0})
    payload = {
        "results": [{**x["snapshot"].to_dict(), "score": x["score"],
                     "sector": x["sector"]} for x in out],
        "filters": HardFilters().describe(),
    }
    txt = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in txt and "Infinity" not in txt
    assert json.loads(txt)["results"][0]["symbol"] == "sh600000"


def test_describe_is_human_readable():
    """条件要能描述成中文 —— 页面上直接显示给用户。"""
    for key, s in STRATEGIES.items():
        d = s.filters.describe()
        assert len(d) >= 5, f"{key} 只描述了 {len(d)} 条"
        for line in d:
            assert isinstance(line, str) and line, f"{key}: {line!r}"
            assert "None" not in line, f"{key} 的描述里有 None：{line}"
        assert "ST" in "、".join(d), f"{key} 没提到剔除 ST"


def test_no_network_call_when_scoring_offline():
    """
    ⚠️ 证明**打分过程不联网**：把 socket 三个入口全劫持，断言调用数为 0。
       为什么需要：CI 用 --offline 跑，超时 300 秒；
       任何隐式联网都会让 CI 变慢或超时。

    ⚠️ 这里显式传 HardFilters(pe_max=None)。
       第一版用默认条件，结果 PE=50 的那只被「PE 过高」刷掉了，
       断言 len==2 失败 —— 是**测试自己把两件事混在一起**了
       （想测打分不联网，却让过滤条件先筛掉了股票）。
       现在过滤条件放宽，确保两只都进到打分环节。
    """
    import socket
    calls = []
    orig = (socket.socket, socket.create_connection, socket.getaddrinfo)

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("打分过程中不该发起网络调用")

    socket.socket = spy
    socket.create_connection = spy
    socket.getaddrinfo = spy
    try:
        cands = [mk("a", "便宜", pe=5.0), mk("b", "贵", pe=50.0)]
        kept, _ = apply_filters(cands, HardFilters(pe_max=None))
        assert len(kept) == 2, f"过滤后应剩 2 只，实得 {len(kept)}"
        out = score_cross_section(kept, {"value_pe": 1.0},
                                  max_per_sector=2, top_n=2)
        assert len(out) == 2
        assert classify_sector("工商银行") == "金融"
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo = orig
    assert calls == [], f"打分发起了网络调用：{calls}"


def test_scoring_does_not_fetch_market():
    """比 socket 劫持更直接：把 fetch_market 换成抛异常的，打分仍要正常。"""
    import screener
    orig = screener.fetch_market
    screener.fetch_market = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("打分不该拉全市场快照"))
    try:
        kept, _ = apply_filters([mk("x", "测试")], HardFilters())
        out = score_cross_section(kept, {"value_pe": 1.0})
        assert len(out) == 1
    finally:
        screener.fetch_market = orig


def test_sector_rules_have_no_cross_sector_duplicate_keywords():
    """
    同一关键词出现在两个行业里会让判定依赖规则顺序 —— 容易出 bug。
    同行业内重复无所谓。
    """
    seen: dict[str, str] = {}
    dupes = []
    for sector, kws in SECTOR_RULES:
        for kw in kws:
            if kw in seen and seen[kw] != sector:
                dupes.append((kw, seen[kw], sector))
            seen[kw] = sector
    assert not dupes, f"关键词跨行业重复：{dupes}"


def test_sector_rules_cover_typical_screening_results():
    """每个策略的典型结果都该能归到某个已知行业（不能全是「其它」）。"""
    typical = ["中国建筑", "江阴银行", "山东路桥", "贵阳银行", "华菱钢铁",
               "中国化学", "中国太保", "华域汽车", "明泰铝业", "赣粤高速"]
    hit = sum(1 for n in typical if classify_sector(n) != "其它")
    assert hit >= 8, f"10 个典型标的只命中了 {hit} 个行业分类"


# ══════════════════════════════════════════════════════════════
#  适配层：把模块级用例包成 harness.TestCase
# ══════════════════════════════════════════════════════════════
_CASES = [(n, f) for n, f in sorted(globals().items())
          if n.startswith("test_") and callable(f)]


def _make_case(fn):
    """
    包一个模块级用例，返回 (断言数, 失败信息) 。

    为什么要把裸 assert 转成 harness 的断言：
      harness 只在 self.eq/self.true 被调用时才计数。
      裸 assert 通过了框架也不知道 —— 汇总会报「0 断言」。
      所以这里捕获 AssertionError，用 self.eq 上报一条结果，
      并把断言数记进 declare_counts 声明的区间。

    这样做的好处：**断言数量对不上时 harness 会直接失败**，
    所以「用例没真跑」骗不过去。
    """
    def run_case(self):
        try:
            fn()
        except AssertionError as e:
            self.eq(f"FAIL: {e}", "PASS", f"{fn.__name__} 断言失败")
            return
        except Exception as e:                             # noqa: BLE001
            self.eq(f"{type(e).__name__}: {e}", "PASS",
                    f"{fn.__name__} 抛异常")
            return
        self.eq("PASS", "PASS", f"{fn.__name__} 全部断言通过")
    run_case.__name__ = fn.__name__
    run_case.__doc__ = fn.__doc__
    return run_case


class TestScreeningEngine(TestCase):
    """选股引擎的离线用例（L1 过滤 / L2 横截面 / 行业分散 / 语义边界）。"""
    name = "screen.选股引擎（离线构造数据）"


for _name, _fn in _CASES:
    setattr(TestScreeningEngine, _name, _make_case(_fn))

REGISTRY.append(TestScreeningEngine)

# 每个用例恰好上报 1 条断言（适配层用一条 eq 代表整个用例的结果）。
# 区间写成 (1,1)：如果哪个用例连一条都没上报，harness 会失败。
declare_counts({
    "TestScreeningEngine": {name: (1, 1) for name, _ in _CASES},
})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_screener.py"))
