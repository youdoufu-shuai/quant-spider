#!/usr/bin/env python3
"""
选股引擎 —— 三层漏斗，只对少量候选做深度分析。

═══════════════════════════════════════════════════════════════
  为什么是三层（不是对全市场跑重逻辑）
═══════════════════════════════════════════════════════════════
参考 AlphaSift 的工程结论：**全市场阶段必须轻量、快速、可回退；
单股深度分析只适合最终少量候选。**

    L1  全市场快照 → 硬性条件过滤        约 2.3 秒   4500 → ~300 只
    L2  横截面因子打分 → 排序            ~1 秒（纯计算）
    L3  对 Top N 拉历史算指标 → AI 排序   30 只 × 171ms

如果对 8000 只每只都拉历史 K 线：8265 × 171ms = **23.6 分钟**。
而 L1 用的快照接口实测 **2.3 秒**拉完 4500 只。

═══════════════════════════════════════════════════════════════
  数据源
═══════════════════════════════════════════════════════════════
新浪行情中心 `Market_Center.getHQNodeData`。实测 19 个字段，
是唯一提供**全市场估值字段**（PE/PB/市值/换手率）的可用接口：

    symbol code name trade pricechange changepercent
    buy sell settlement(昨收) open high low volume
    amount(成交额) ticktime per(PE) pb(PB)
    mktcap(总市值) nmc(流通市值) turnoverratio(换手率)

⚠️ 单次上限 100 条（num=500 也只回 100），所以要 83 页。
   串行 20.4 秒，**并发 8 → 2.3 秒**。

⚠️ 东财（push2 / push2his）实测被**按 IP 封禁**，不要用。
   见 datasource.py 的说明。

字段可用率（实测 4500 只）：
    PB 98.9%   市值 100%   换手 99.8%   成交额 99.8%
    PE 73.6%   ← 亏损股没有 PE，这是正常的，不是数据缺失

用法：
  python quant/screener.py --strategy dual_low
  python quant/screener.py --strategy dual_low --json
  python quant/screener.py --list
  python quant/screener.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HQ_BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
           "json_v2.php/Market_Center.getHQNodeData")
HQ_REFERER = "https://vip.stock.finance.sina.com.cn/mkt/"

# 沪深 A 股。实测：sh_a 2319 只 / sz_a 2906 只 / hs_bjs 351 只
NODES = ("sh_a", "sz_a", "hs_bjs")


# ══════════════════════════════════════════════════════════════
#  L1：全市场快照
# ══════════════════════════════════════════════════════════════
def _num(v) -> float | None:
    """转 float，失败返回 None。空字符串、'-'、None 都算没有。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _fetch_page(node: str, page: int, num: int = 100, timeout: float = 25.0):
    """拉一页。返回 list[dict]，空页返回 []。"""
    url = (f"{HQ_BASE}?page={page}&num={num}&sort=symbol&asc=1"
           f"&node={node}&symbol=&_s_r_a=page")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": HQ_REFERER,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf8", "replace")
    # 新浪这个接口返回的是非严格 JSON（键无引号），但它其实是合法的
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # 空页会返回 null 或空串
        return []


@dataclass
class Snapshot:
    """一只股票的快照。字段名保持与新浪接口一致，便于溯源。"""
    symbol: str
    code: str
    name: str
    price: float | None          # trade
    prev_close: float | None     # settlement
    change_pct: float | None     # changepercent
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    amount: float | None         # 成交额（元）
    pe: float | None             # per
    pb: float | None
    mktcap: float | None         # 总市值（万元）
    float_cap: float | None      # nmc 流通市值（万元）
    turnover: float | None       # turnoverratio 换手率（%）

    # 派生
    is_st: bool = False
    is_suspended: bool = False   # 成交额为 0 = 停牌或未开盘

    def to_dict(self) -> dict:
        d = asdict(self)
        # 市值统一换成「亿元」，前端好读
        d["mktcapYi"] = round(self.mktcap / 1e4, 2) if self.mktcap else None
        d["floatCapYi"] = round(self.float_cap / 1e4, 2) if self.float_cap else None
        d["amountYi"] = round(self.amount / 1e8, 3) if self.amount else None
        return d


def _parse(item: dict) -> Snapshot | None:
    sym = (item.get("symbol") or "").strip()
    name = (item.get("name") or "").strip()
    if not sym or not name:
        return None
    amount = _num(item.get("amount"))
    return Snapshot(
        symbol=sym,
        code=str(item.get("code") or sym[2:]),
        name=name,
        price=_num(item.get("trade")),
        prev_close=_num(item.get("settlement")),
        change_pct=_num(item.get("changepercent")),
        open=_num(item.get("open")),
        high=_num(item.get("high")),
        low=_num(item.get("low")),
        volume=_num(item.get("volume")),
        amount=amount,
        pe=_num(item.get("per")),
        pb=_num(item.get("pb")),
        mktcap=_num(item.get("mktcap")),
        float_cap=_num(item.get("nmc")),
        turnover=_num(item.get("turnoverratio")),
        # ST 判定：名称含 ST（含 *ST、SST 等）。这是交易所的标记，可靠。
        is_st=("ST" in name.upper()),
        # 成交额为 0 或 None → 停牌
        is_suspended=(not amount or amount <= 0),
    )


def fetch_market(nodes: tuple[str, ...] = NODES, max_pages: int = 70,
                 workers: int = 12, verbose: bool = False) -> list[Snapshot]:
    """
    拉全市场快照。并发拉页。

    ⚠️ 实测：单测 83 页并发 8 约 2.3 秒，但整条选股链路实测 7.4 秒。
       差额在 DNS/TLS 握手、进程启动与并发调度上。所以做两件事：
         · workers 提到 12（再高收益递减，新浪会开始拖）
         · **提前停页**：某板块某页返回不足 100 条就说明到底了，
           不再等它尾部那些空页。每个板块能省 1~2 页的往返。

    页数上限 70 是护栏：万一接口返回无限重复内容，不会死循环。
    """
    jobs: list[tuple[str, int]] = [(n, p) for n in nodes for p in range(1, max_pages + 1)]
    out: dict[str, Snapshot] = {}
    seen_per_node: dict[str, int] = {n: 0 for n in nodes}
    done_nodes: set[str] = set()

    def one(args):
        node, page = args
        if node in done_nodes:
            return node, page, []
        try:
            return node, page, _fetch_page(node, page)
        except Exception:                            # noqa: BLE001
            return node, page, []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for node, page, items in ex.map(one, jobs):
            # 不满 100 条 = 该板块最后一页
            if len(items) < 100:
                done_nodes.add(node)
            for it in items:
                s = _parse(it)
                if s:
                    out[s.symbol] = s
                    seen_per_node[node] = seen_per_node.get(node, 0) + 1
            if len(done_nodes) >= len(nodes):
                break

    if verbose:
        detail = "  ".join(f"{n}:{seen_per_node.get(n, 0)}" for n in nodes)
        print(f"  拉到 {len(out)} 只  ({detail})", file=sys.stderr)
    return list(out.values())


# ══════════════════════════════════════════════════════════════
#  L1：硬性条件（各策略共用的一套过滤）
# ══════════════════════════════════════════════════════════════
@dataclass
class HardFilters:
    """
    硬性条件。全部照搬主流选股项目的口径（见 alphasift 的策略 YAML）。

    为什么硬性条件用基本面/估值字段：这些是**客观、可审计**的，
    不像技术指标那样对参数敏感。它们的作用是把 8000 只砍到几百只，
    让后面的深度分析成为可能。
    """
    exclude_st: bool = True              # 剔除 ST
    exclude_suspended: bool = True       # 剔除停牌
    amount_min: float | None = 5e7       # 成交额下限（元），5000 万
    amount_max: float | None = None
    pe_min: float | None = 0.0           # 0 = 剔除亏损股（PE 为负）
    pe_max: float | None = 15.0
    pb_min: float | None = 0.0
    pb_max: float | None = 2.0
    mktcap_min: float | None = 5e5       # 总市值下限（**万元**），50 亿
    mktcap_max: float | None = None
    price_min: float | None = 3.0
    price_max: float | None = 80.0
    change_min: float | None = None      # 当日涨跌幅下限（%）
    change_max: float | None = None      # 上限，用于「不追高」

    def check(self, s: Snapshot) -> str | None:
        """通过返回 None，否则返回被拒的原因（中文，前端可显示）。"""
        if self.exclude_st and s.is_st:
            return "ST"
        if self.exclude_suspended and s.is_suspended:
            return "停牌"

        # 两个「必须有」的字段
        if s.price is None or s.price <= 0:
            return "无价格"
        if s.mktcap is None or s.mktcap <= 0:
            return "无市值"

        # 成交额
        if self.amount_min is not None:
            if s.amount is None or s.amount < self.amount_min:
                return "成交额不足"
        if self.amount_max is not None and s.amount is not None and s.amount > self.amount_max:
            return "成交额过大"

        # PE：注意**只对「有 PE 且要求 PE」时检查**。
        # 亏损股 per 为空 —— 如果策略要求 pe_min=0，亏损股会被刷掉；
        # 如果策略不关心 PE（pe_min/pe_max 都是 None），亏损股应放行。
        if self.pe_min is not None or self.pe_max is not None:
            if s.pe is None:
                return "无 PE（亏损或未披露）"
            if self.pe_min is not None and s.pe < self.pe_min:
                return "PE 过低"
            if self.pe_max is not None and s.pe > self.pe_max:
                return "PE 过高"

        if self.pb_min is not None or self.pb_max is not None:
            if s.pb is None:
                return "无 PB"
            if self.pb_min is not None and s.pb < self.pb_min:
                return "PB 过低"
            if self.pb_max is not None and s.pb > self.pb_max:
                return "PB 过高"

        if self.mktcap_min is not None and s.mktcap < self.mktcap_min:
            return "市值过小"
        if self.mktcap_max is not None and s.mktcap > self.mktcap_max:
            return "市值过大"

        if self.price_min is not None and s.price < self.price_min:
            return "价格过低"
        if self.price_max is not None and s.price > self.price_max:
            return "价格过高"

        if self.change_min is not None or self.change_max is not None:
            if s.change_pct is None:
                return "无涨跌幅"
            if self.change_min is not None and s.change_pct < self.change_min:
                return "跌幅过大"
            if self.change_max is not None and s.change_pct > self.change_max:
                return "涨幅过大（追高）"
        return None

    def describe(self) -> list[str]:
        """人类可读的条件清单，页面上要显示。"""
        out = []
        if self.exclude_st:
            out.append("剔除 ST")
        if self.exclude_suspended:
            out.append("剔除停牌")
        if self.amount_min:
            out.append(f"成交额 ≥ {self.amount_min/1e8:.2f} 亿")
        if self.amount_max:
            out.append(f"成交额 ≤ {self.amount_max/1e8:.2f} 亿")
        if self.pe_min is not None or self.pe_max is not None:
            lo = "0" if self.pe_min == 0 else (f"{self.pe_min:g}" if self.pe_min is not None else "")
            hi = f"{self.pe_max:g}" if self.pe_max is not None else ""
            out.append(f"PE {lo}~{hi}" if hi else f"PE ≥ {lo}")
        if self.pb_min is not None or self.pb_max is not None:
            lo = f"{self.pb_min:g}" if self.pb_min is not None else ""
            hi = f"{self.pb_max:g}" if self.pb_max is not None else ""
            out.append(f"PB {lo}~{hi}" if lo and hi else (f"PB ≤ {hi}" if hi else f"PB ≥ {lo}"))
        if self.mktcap_min:
            out.append(f"市值 ≥ {self.mktcap_min/1e4:.0f} 亿")
        if self.mktcap_max:
            out.append(f"市值 ≤ {self.mktcap_max/1e4:.0f} 亿")
        if self.price_min is not None or self.price_max is not None:
            lo = f"{self.price_min:g}" if self.price_min is not None else ""
            hi = f"{self.price_max:g}" if self.price_max is not None else ""
            out.append(f"价格 {lo}~{hi}" if lo and hi else (f"价格 ≤ {hi}" if hi else f"价格 ≥ {lo}"))
        if self.change_min is not None or self.change_max is not None:
            lo = f"{self.change_min:g}" if self.change_min is not None else ""
            hi = f"{self.change_max:g}" if self.change_max is not None else ""
            out.append(f"当日涨跌幅 {lo}~{hi}%" if lo and hi else
                       (f"涨幅 ≤ {hi}%" if hi else f"跌幅 ≥ {lo}%"))
        return out


def apply_filters(market: list[Snapshot],
                  f: HardFilters) -> tuple[list[Snapshot], dict[str, int]]:
    """
    应用硬性条件。返回 (通过的, 各原因计数)。

    为什么要统计原因：让用户看到「为什么只剩这么少」。
    只说「筛出 295 只」而不说「因为成交额刷掉了 3000 只」，
    用户没法判断条件是不是太严。
    """
    kept: list[Snapshot] = []
    reasons: dict[str, int] = {}
    for s in market:
        why = f.check(s)
        if why is None:
            kept.append(s)
        else:
            reasons[why] = reasons.get(why, 0) + 1
    return kept, reasons


# ══════════════════════════════════════════════════════════════
#  L2：横截面因子
# ══════════════════════════════════════════════════════════════
#  「横截面」= 同一天把全市场放在一起排名。这是选股的核心思路：
#  不是问「这只股票好不好」，而是问「这只比其它 3000 只更好吗」。
#
#  因子分统一归一到 0~100（百分位），这样不同量纲的因子可以直接加权。
#
#  ⚠️ 局限：快照只有「当前这一刻」，没有历史。
#     所以这里只能做横截面因子，做不了 MA/RSI/ADX 那类时序指标。
#     时序指标留给 L3（对少数候选拉历史）。
def _percentile_rank(values: list[float], v: float,
                     ascending: bool = True) -> float:
    """
    返回 v 在 values 里的得分（0~100）。ascending=True 表示值越小得分越高。

    ⚠️ 用**秩百分比**，不用 min-max 归一化。
       min-max 会被极端值压扁：候选池里只要有一个 PE=1500 的股票，
       其余几百只的 PE 全挤在 0~5% 区间，区分度就没了。
       秩百分比只关心「排第几」，天然抗极端值。

    ⚠️ 用「不同值的秩」而不是逐元素秩：
       否则池子里 3 个值时，最好的只得 66.7 分（因为还有 2 个比它差的位置
       被算进分母）。按不同值排名能让「池中最好」稳定得 100 分，
       与池子大小无关。

    例（ascending=True，值越小越好）:
        [1,2,3,4,5] 求 1 → 100 分（最好）
        [1,2,3,4,5] 求 3 →  50 分
        [1,2,3,4,5] 求 5 →   0 分
    """
    if not values:
        return 50.0
    uniq = sorted(set(values))
    n = len(uniq)
    if n == 1:
        return 50.0          # 池子里只有一个值，没有区分度 → 中性
    # 找 v 落在哪个位置（浮点用容差比较）
    idx = None
    for i, x in enumerate(uniq):
        if abs(x - v) <= 1e-12 * max(1.0, abs(x), abs(v)):
            idx = i
            break
    if idx is None:
        # v 不在池子里：插到合适位置
        idx = sum(1 for x in uniq if x < v)
        if idx >= n:
            idx = n - 1
    frac = idx / (n - 1)              # 0 = 最好，1 = 最差
    return round((1.0 - frac) * 100.0, 4) if ascending else round(frac * 100.0, 4)


FACTORS = {
    # key: (中文名, 取值函数, ascending, 说明)
    # ascending=True 表示「值越小得分越高」（如 PE、PB）
    "value_pe": ("PE 低", lambda s: s.pe if s.pe and s.pe > 0 else None, True),
    "value_pb": ("PB 低", lambda s: s.pb if s.pb and s.pb > 0 else None, True),
    "momentum": ("动量", lambda s: s.change_pct, False),
    "liquidity": ("流动性", lambda s: s.amount, False),
    "turnover": ("换手活跃", lambda s: s.turnover, False),
    "size": ("市值", lambda s: s.mktcap, False),
}


def score_cross_section(cands: list[Snapshot],
                        weights: dict[str, float],
                        max_per_sector: int | None = None,
                        top_n: int | None = None,
                        penalty: float = 6.0) -> list[dict]:
    """
    对候选做横截面打分。返回按总分降序的列表。

    每个因子：先取有效值集合，再算得分。缺该因子的股票得 50 分（中性），
    **不剔除** —— 因为 PE 缺失只说明亏损，不代表这只股票差。

    max_per_sector: 同一行业最多取几只。超过的部分按 penalty 逐级扣分，
                    这样同行业股票会自然排到后面，而不是被硬性丢弃。
                    为什么用扣分而不是直接剔除：扣分保留了"实在没有别的
                    选择时也能选它"的余地，而且排名仍然透明可解释。
    """
    if not cands:
        return []

    # 预取每个因子的有效值分布
    pools: dict[str, list[float]] = {}
    for key, (_, getter, _asc) in FACTORS.items():
        vals = []
        for s in cands:
            v = getter(s)
            if v is not None and not (isinstance(v, float) and v != v):
                vals.append(float(v))
        pools[key] = vals

    out = []
    for s in cands:
        parts = {}
        total = 0.0
        wsum = 0.0
        for key, w in weights.items():
            if w <= 0 or key not in FACTORS:
                continue
            label, getter, asc = FACTORS[key]
            v = getter(s)
            if v is None or not pools[key]:
                pct = 50.0                 # 缺失 → 中性
            else:
                pct = _percentile_rank(pools[key], float(v), asc)
            parts[key] = round(pct, 1)
            total += pct * w
            wsum += w
        score = total / wsum if wsum > 0 else 50.0
        sec = classify_sector(s.name, s.symbol)
        out.append({
            "snapshot": s,
            "score": round(score, 2),
            "rawScore": round(score, 2),
            "sector": sec,
            # ── 配额用的键 ──
            # ⚠️ 「其它」是「没分类」的兜底桶，**不能当做一个行业**参与配额。
            #    踩过：某策略选出的前 30 只里有 11 只「其它」，它们分属
            #    完全不同的行业（只是名称不含我的关键词），却被压在同一个
            #    配额里 —— 既错误地限制了它们，也让分散度统计失去意义。
            #    所以未分类的每只单独成桶（用代码做键，互不竞争）。
            "quotaKey": sec if sec != "其它" else f"其它:{s.symbol}",
            "factors": parts,
            "penalty": 0.0,
        })

    # 按原始分排序
    out.sort(key=lambda x: -x["rawScore"])

    limit = top_n or len(out)
    if limit <= 0:
        return []

    # 没有行业限制时就是纯排序取前 N。
    # ⚠️ 这里曾经漏了切片，导致 max_per_sector=None 时返回全部候选
    #    （实测 50 只输入返回 50 只，而 top_n=10）。
    if not max_per_sector or max_per_sector <= 0:
        return out[:limit]

    # ── 行业分散：两段式，先装满配额内的，再用罚分补足 ──
    #
    # ⚠️ 这段改过三次，三次都是真 bug，留给后来人：
    #    v1 边选边扣分、扣完塞回队列 → 候选全属同一行业时会把股票
    #       `pool.remove` 掉，**静默少返回**（4 只只回 3 只）
    #    v2 兜底循环里忘了更新 per_sector → 同行业第二只被反复跳过，
    #       4 只只回 1 只（实测）
    #    v3 用 `same - max` 算超配额数 → 第 3 只（配额 2）罚分为 0，
    #       实测 top=30 时金融 3 只、罚分全 0，配额形同失效
    #
    # 现在：配额内直接选；不够则按「原始分 - 罚分」排序补足；
    # 每补一只就更新计数，保证不会死循环也不会少给。
    #
    # ⚠️ 配额用 `quotaKey` 而不是 `sector`：
    #    「其它」是没分类的兜底桶，每只单独成键，不互相竞争配额。
    selected: list[dict] = []
    per_sector: dict[str, int] = {}
    for x in out:
        if len(selected) >= limit:
            break
        key = x["quotaKey"]
        if per_sector.get(key, 0) >= max_per_sector:
            continue
        per_sector[key] = per_sector.get(key, 0) + 1
        x["penalty"] = 0.0
        x["score"] = x["rawScore"]
        selected.append(x)

    # 给落选的算一个「罚分后的分数」，并按它排序作为补足顺序。
    #
    # ⚠️ `over` 里的 +1 不能省：某行业已入选 2 只、配额 2 时，
    #    第 3 只的 over 应该是 **1**（它是第 1 个超配额的）。
    def _over(x: dict) -> int:
        same = per_sector.get(x["quotaKey"], 0)
        return (same - max_per_sector + 1) if same >= max_per_sector else 0

    def penalized(x: dict) -> float:
        return x["rawScore"] - penalty * _over(x)

    if len(selected) < limit:
        rest = [x for x in out if x not in selected]
        rest.sort(key=lambda x: -penalized(x))
        for x in rest:
            if len(selected) >= limit:
                break
            over = _over(x)
            x["penalty"] = round(penalty * over, 2)
            x["score"] = round(x["rawScore"] - x["penalty"], 2)
            # ⚠️ 必须更新计数，否则同一行业的后续股票会被反复算成同一 over
            per_sector[x["quotaKey"]] = per_sector.get(x["quotaKey"], 0) + 1
            selected.append(x)

    return selected


# ══════════════════════════════════════════════════════════════
#  行业分类（用于分散度控制）
# ══════════════════════════════════════════════════════════════
#
# ═══════════════════════════════════════════════════════════════
#  为什么必须有这个
# ═══════════════════════════════════════════════════════════════
#  实测「双低价值」策略选出的前 15 只里，**有 10 只是银行**。
#  这不是 bug —— 银行天然 PE 4~7、PB 0.3~0.8，在「低 PE + 低 PB」
#  条件下载必然霸榜。但这样的名单没有任何实用价值：
#  买 10 只银行和买 1 只银行的风险几乎一样，等于没分散。
#
#  ═════════════════════════════════════════════════════════════
#  ★ 第一版用名称关键词猜行业，覆盖率只有 ~50%，不可用
#  ═════════════════════════════════════════════════════════════
#  实测：volume_breakout 策略选出的前 30 只里 **21 只被判为「其它」**，
#  而这 21 只实际分属 10 多个完全不同的行业（远东股份=电力设备、
#  内蒙一机=国防军工、中科曙光=计算机…）。名称里根本不含行业词，
#  猜不出来。这样分散度控制等于没生效。
#
#  ★ 现在用新浪的申万一级行业节点
#  ═════════════════════════════════════════════════════════════
#  `Market_Center.getHQNodes` 返回完整节点树，里面 `sw1_*` 是申万一级：
#
#      getHQNodes → [["A股",[["申万一级",[["农林牧渔","","sw1_110000"],...]]]]]
#      getHQNodeData?node=sw1_110000 → 该行业全部成员
#
#  实测：31 个行业、**覆盖率 99.7%**（5560/5576）、拉取 **2.5 秒**。
#  结果缓存 7 天（行业归属很少变），缓存命中时 0 开销。
#
#  ⚠️ 退化策略：缓存没有 + 网络失败时，回退到名称关键词。
#     名称法覆盖率低，但聊胜于无 —— 不能因为拿不到行业就整个筛选失败。
INDUSTRY_CACHE = HERE / "state" / "industry-map.json"
INDUSTRY_TTL_DAYS = 7
INDUSTRY_NODES_URL = (HQ_BASE.replace("Market_Center.getHQNodeData",
                                      "Market_Center.getHQNodes"))

# 行业映射的进程内缓存。None = 还没加载过。
_INDUSTRY_MAP: dict[str, str] | None = None

# 名称关键词兜底（覆盖率低，仅在拿不到真实映射时用）
SECTOR_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("金融", ("银行", "证券", "保险", "信托", "期货", "农商", "信用社")),
    ("地产建筑", ("地产", "置业", "城建", "建筑", "建设", "路桥", "铁建",
                  "中铁", "建工", "隧道", "工程", "基建", "水泥", "建材",
                  "钢构")),
    ("钢铁", ("钢铁", "钢股份", "特钢", "不锈钢", "钢管", "宝钢", "鞍钢")),
    ("煤炭", ("煤业", "煤炭", "煤电", "焦煤", "焦炭", "神华")),
    ("有色金属", ("有色", "铜业", "铝业", "锌业", "稀土", "黄金", "矿业",
                  "钨业", "钼业")),
    ("基础化工", ("化工", "化学", "石化", "化纤", "橡胶", "塑料", "农药",
                  "涂料")),
    ("医药生物", ("医药", "制药", "药业", "生物", "医疗", "疫苗", "健康",
                  "基因")),
    ("农林牧渔", ("种业", "农业", "牧业", "饲料", "化肥", "林业", "渔业",
                  "养殖")),
    ("食品饮料", ("食品", "饮料", "白酒", "酒业", "乳业", "肉业")),
    ("纺织服饰", ("纺织", "服装", "服饰", "鞋业", "家纺")),
    ("商贸零售", ("零售", "商业", "百货", "超市")),
    ("社会服务", ("旅游", "酒店", "餐饮", "教育")),
    ("家用电器", ("家电", "电器", "家居", "家具")),
    ("汽车", ("汽车", "客车", "重汽", "轮胎", "零部件", "车业", "摩托")),
    ("电子", ("科技", "电子", "半导体", "芯片", "光电", "数码")),
    ("计算机", ("软件", "计算机", "信息", "网络", "数据", "智能", "互联网")),
    ("通信", ("通信", "通讯")),
    ("电力设备", ("新能源", "光伏", "锂", "电池", "储能", "风电", "核电",
                  "氢能", "太阳能")),
    ("公用事业", ("电力", "水电", "燃气", "水务", "环保", "热力", "公用")),
    ("机械设备", ("机械", "重工", "装备", "机床", "轴承", "机电")),
    ("国防军工", ("军工", "航发", "航天", "船舶", "兵器", "航空")),
    ("交通运输", ("机场", "港口", "航运", "物流", "铁路", "高速", "快递",
                  "海运")),
    ("传媒", ("传媒", "文化", "影视", "出版", "广电", "游戏")),
]


def _fetch_industry_map(verbose: bool = False) -> dict[str, str]:
    """
    从新浪拉申万一级行业 → 股票映射。

    实测：31 个行业、覆盖率 99.7%、并发 8 约 2.5 秒。
    """
    import urllib.request

    def _get(url: str) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": HQ_REFERER, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf8", "replace")

    tree = json.loads(_get(INDUSTRY_NODES_URL))

    def walk(node):
        """递归找所有 (名称, 代码) 三元组。"""
        out = []
        if isinstance(node, list):
            if (len(node) == 3 and isinstance(node[0], str)
                    and isinstance(node[2], str) and node[2]):
                out.append((node[0], node[2]))
            else:
                for x in node:
                    out.extend(walk(x))
        return out

    sw1 = [(n, c) for n, c in walk(tree) if c.startswith("sw1_")]
    if not sw1:
        raise RuntimeError("节点树里没有 sw1_* 申万一级节点")

    def members(code: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 8):          # 最大的行业约 600 只 → 6 页
            body = _get(f"{HQ_BASE}?page={page}&num=100&sort=symbol&asc=1"
                        f"&node={code}&symbol=&_s_r_a=page")
            try:
                batch = json.loads(body) if body.strip().startswith("[") else []
            except json.JSONDecodeError:
                batch = []
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 100:
                break
        return rows

    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for (name, _code), rows in zip(sw1, ex.map(lambda x: members(x[1]), sw1)):
            for r in rows:
                sym = (r.get("symbol") or "").strip()
                if sym:
                    mapping[sym] = name
    if verbose:
        print(f"  行业映射：{len(sw1)} 个申万一级行业，覆盖 {len(mapping)} 只",
              file=sys.stderr)
    return mapping


def _offline() -> bool:
    """
    是否处于离线模式（不发起任何网络请求）。

    ⚠️ 为什么需要：CI 用 `--offline` 跑测试，而 `classify_sector` 会去拉
       行业映射（2.5 秒 + 网络）。如果不拦住，离线测试会真的联网 ——
       慢、不稳定、还可能超时。

    测试里设 `QUANT_OFFLINE=1`，`_load_industry_map()` 就直接返回空，
    走名称关键词兜底。
    """
    return os.environ.get("QUANT_OFFLINE", "").strip() not in ("", "0", "false")


def _load_industry_map(verbose: bool = False) -> dict[str, str]:
    """
    取行业映射：进程内缓存 → 磁盘缓存 → 网络。拿不到就返回 {}（回退名称法）。

    绝不抛异常：行业分类只是分散度控制，拿不到不该让整个选股失败。
    离线模式下直接返回 {}，不读缓存也不联网（保证测试可复现）。
    """
    global _INDUSTRY_MAP
    if _INDUSTRY_MAP is not None:
        return _INDUSTRY_MAP

    # 离线：不读缓存也不联网。读缓存会让测试结果依赖本机状态，
    # 那样「同一份代码在不同机器上结果不同」，测试就没意义了。
    if _offline():
        _INDUSTRY_MAP = {}
        return _INDUSTRY_MAP

    # 磁盘缓存
    try:
        if INDUSTRY_CACHE.exists():
            raw = json.loads(INDUSTRY_CACHE.read_text(encoding="utf8"))
            age_days = (time.time() - float(raw.get("at", 0))) / 86400.0
            if age_days <= INDUSTRY_TTL_DAYS and raw.get("map"):
                _INDUSTRY_MAP = {str(k): str(v) for k, v in raw["map"].items()}
                if verbose:
                    print(f"  行业映射：磁盘缓存 {len(_INDUSTRY_MAP)} 只"
                          f"（{age_days:.1f} 天前）", file=sys.stderr)
                return _INDUSTRY_MAP
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass

    # 网络
    try:
        mapping = _fetch_industry_map(verbose=verbose)
    except Exception:                                    # noqa: BLE001
        _INDUSTRY_MAP = {}
        return _INDUSTRY_MAP

    _INDUSTRY_MAP = mapping
    try:
        INDUSTRY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = INDUSTRY_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "map": mapping},
                                  ensure_ascii=False), encoding="utf8")
        tmp.replace(INDUSTRY_CACHE)
    except OSError:
        pass                                          # 缓存写失败不影响使用
    return _INDUSTRY_MAP


def set_industry_map(mapping: dict[str, str] | None) -> None:
    """
    手动注入行业映射（测试用）。

    这样测试既能验证「走真实映射」的路径，又完全离线、
    不依赖网络和本机缓存。
    """
    global _INDUSTRY_MAP
    _INDUSTRY_MAP = dict(mapping) if mapping is not None else None


def classify_sector(name: str, symbol: str = "") -> str:
    """
    判定行业。优先真实申万一级映射，拿不到才用名称关键词猜。

    返回申万一级行业名（如「银行」→「金融」不在这一层，实际是
    「银行」属于「银行」… 注意申万一级没有「金融」这个大类，
    银行/非银金融是分开的两级）。拿不到时返回「其它」。
    """
    m = _load_industry_map()
    if symbol and symbol in m:
        return m[symbol]
    if not name:
        return "其它"
    for sector, keywords in SECTOR_RULES:
        for kw in keywords:
            if kw in name:
                return sector
    return "其它"


# ══════════════════════════════════════════════════════════════
#  内置策略
# ══════════════════════════════════════════════════════════════
#
#  4 个策略，条件和权重都参考主流开源选股项目的口径
#  （alphasift 的 dual_low / momentum_quality 等策略 YAML）。
#
#  设计原则：
#    · 硬性条件用**客观可审计**的基本面字段，不用技术指标
#      —— 技术指标对参数敏感，不适合当"一票否决"
#    · 每个策略覆盖一种风格，避免 4 个策略选出同一批股票
#    · change_max 都设了上限：**不追高**是共同纪律
#
@dataclass
class Strategy:
    key: str
    label: str
    desc: str
    filters: HardFilters
    weights: dict[str, float]
    top_n: int = 30
    # 同一行业最多取几只。None 或 0 = 不限制。
    # 为什么默认 2：实测不限制时「双低价值」前 15 只有 10 只银行，
    # 买 10 只银行和买 1 只银行的风险几乎一样，等于没分散。
    max_per_sector: int | None = 2
    # 排序时额外提示给 AI 的话
    hint: str = ""


STRATEGIES: dict[str, Strategy] = {
    # ── 1. 双低价值 ──
    # 低 PE + 低 PB。最经典的价值策略。
    # 实测：条件从 4500 只筛出 295 只。
    "dual_low": Strategy(
        key="dual_low",
        label="双低价值",
        desc="低 PE + 低 PB，偏稳健的估值修复。剔除了追高与冷门股。",
        filters=HardFilters(
            amount_min=5e7,          # 成交额 ≥ 5000 万（要有流动性）
            pe_min=0.0, pe_max=15.0,  # 0 表示剔除亏损股
            pb_min=0.0, pb_max=2.0,
            mktcap_min=5e5,          # 市值 ≥ 50 亿
            mktcap_max=3e7,          # 市值 ≤ 3000 亿（避免巨型股没弹性）
            price_min=3.0, price_max=80.0,
            change_min=-4.5, change_max=4.5,   # 不追涨也不接暴跌
        ),
        weights={"value_pe": 0.34, "value_pb": 0.26,
                 "liquidity": 0.14, "turnover": 0.10,
                 "momentum": 0.10, "size": 0.06},
        hint=("优先低 PE/PB 且成交活跃的估值修复候选。"
              "不要只因为便宜就反复选同一批银行、地产、周期股。"),
    ),

    # ── 2. 趋势质量 ──
    # 已经确认趋势、但还没进入极端追涨阶段的。
    "momentum_quality": Strategy(
        key="momentum_quality",
        label="趋势质量",
        desc="兼顾趋势确认与基本面质量的中线候选。成交额门槛更高。",
        filters=HardFilters(
            amount_min=2e8,          # 成交额 ≥ 2 亿（趋势要有资金）
            pe_min=0.0, pe_max=60.0,
            pb_min=0.0, pb_max=8.0,
            mktcap_min=8e5,          # 市值 ≥ 80 亿
            price_min=4.0, price_max=220.0,
            change_min=-3.5, change_max=7.5,
        ),
        weights={"momentum": 0.30, "liquidity": 0.20, "turnover": 0.14,
                 "value_pe": 0.14, "value_pb": 0.10, "size": 0.12},
        hint=("优先已确认趋势但未进入极端追涨的候选。"
              "基本面质量（估值、资金活跃）要能为趋势提供支撑。"),
    ),

    # ── 3. 超跌反转 ──
    # 当日跌得多、但估值低、流动性还在 —— 均值回归。
    "oversold_reversal": Strategy(
        key="oversold_reversal",
        label="超跌反转",
        desc="估值偏低且当日明显回调的均值回归候选。",
        filters=HardFilters(
            amount_min=5e7,
            pe_min=0.0, pe_max=30.0,
            pb_min=0.0, pb_max=3.0,
            mktcap_min=3e5,          # 市值 ≥ 30 亿
            price_min=3.0, price_max=120.0,
            change_min=-9.0, change_max=-1.5,   # ★ 只选当天确实跌了的
        ),
        weights={"value_pb": 0.28, "value_pe": 0.20,
                 "liquidity": 0.18, "turnover": 0.14,
                 # 跌得越多分越高？不 —— 这里动量是"越接近 0 越好"，
                 # 因为暴跌 9% 往往是坏消息。用 ascending=False 会让
                 # 跌得最狠的排最前，那是抄底接飞刀。
                 # 所以这里把动量权重设为 0，只当过滤条件用。
                 "momentum": 0.0, "size": 0.20},
        hint=("当日明显回调但估值低、成交未萎缩的候选。"
              "注意区分「错杀」与「有坏消息」—— 跌幅接近上限的要多加怀疑。"),
    ),

    # ── 4. 放量突破 ──
    # 成交量放大 + 适度上涨（不追涨停）。
    "volume_breakout": Strategy(
        key="volume_breakout",
        label="放量突破",
        desc="成交活跃且温和上涨的动量候选。刻意排除涨停板。",
        filters=HardFilters(
            amount_min=1e8,          # 成交额 ≥ 1 亿（放量要真放量）
            pe_min=None, pe_max=None,  # ★ 不限制 PE —— 成长股常常亏损
            pb_min=None, pb_max=None,
            mktcap_min=2e5,          # 市值 ≥ 20 亿
            price_min=2.0, price_max=150.0,
            change_min=1.0, change_max=7.5,   # ★ 涨了但没涨停
        ),
        weights={"liquidity": 0.34, "turnover": 0.28,
                 "momentum": 0.22, "size": 0.16},
        hint=("成交额与换手显著放大的突破候选。"
              "涨幅接近 7.5% 的说明已经很热，风险高。"),
    ),
}


# ══════════════════════════════════════════════════════════════
#  编排：跑一个完整策略
# ══════════════════════════════════════════════════════════════
def run_strategy(strategy_key: str,
                 market: list[Snapshot] | None = None,
                 top_n: int | None = None,
                 verbose: bool = False) -> dict:
    """
    跑一个策略。返回完整结果（含中间统计，前端要显示漏斗）。

    market 可以外部传入（重复用同一份快照跑多个策略，省一次 2.3 秒）。
    """
    st = STRATEGIES.get(strategy_key)
    if st is None:
        raise KeyError(f"未知策略 {strategy_key}，可用：{', '.join(STRATEGIES)}")

    t0 = time.time()
    if market is None:
        market = fetch_market(verbose=verbose)
    t_fetch = time.time() - t0

    t0 = time.time()
    kept, reasons = apply_filters(market, st.filters)
    t_filter = time.time() - t0

    t0 = time.time()
    n = top_n or st.top_n
    ranked = score_cross_section(kept, st.weights,
                                 max_per_sector=st.max_per_sector,
                                 top_n=n)
    t_score = time.time() - t0

    top = ranked[:n]

    # 行业分布 —— 让用户看到名单是不是挤在一个行业里
    sector_dist: dict[str, int] = {}
    for r in top:
        sec = r["sector"]
        sector_dist[sec] = sector_dist.get(sec, 0) + 1

    return {
        "strategy": st.key,
        "label": st.label,
        "desc": st.desc,
        "hint": st.hint,
        "filters": st.filters.describe(),
        "weights": st.weights,
        "maxPerSector": st.max_per_sector,
        "funnel": {
            "market": len(market),
            "afterFilter": len(kept),
            "top": len(top),
            "rejected": reasons,
        },
        "sectors": dict(sorted(sector_dist.items(), key=lambda x: -x[1])),
        "timing": {
            "fetchMs": int(t_fetch * 1000),
            "filterMs": int(t_filter * 1000),
            "scoreMs": int(t_score * 1000),
            "totalMs": int((t_fetch + t_filter + t_score) * 1000),
        },
        "results": [{
            **r["snapshot"].to_dict(),
            "score": r["score"],
            "rawScore": r["rawScore"],
            "penalty": round(r["penalty"], 2),
            "sector": r["sector"],
            "factors": r["factors"],
        } for r in top],
    }


# ══════════════════════════════════════════════════════════════
#  自检（离线：用构造的快照，不联网）
# ══════════════════════════════════════════════════════════════
def _selftest() -> int:
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    def mk(sym, name, price, amt, pe, pb, cap, chg, turn=2.0):
        return Snapshot(
            symbol=sym, code=sym[2:], name=name, price=price, prev_close=price,
            change_pct=chg, open=price, high=price, low=price,
            volume=1e6, amount=amt, pe=pe, pb=pb,
            mktcap=cap, float_cap=cap, turnover=turn,
            is_st=("ST" in name.upper()), is_suspended=(not amt or amt <= 0),
        )

    print("=" * 74)
    print("  选股引擎自检（离线，构造数据）")
    print("=" * 74)

    print("\n[1] 数值解析")
    check("正常数字", _num("13.5") == 13.5)
    check("空串 → None", _num("") is None)
    check("None → None", _num(None) is None)
    check("横杠 → None", _num("-") is None)
    check("非法 → None", _num("abc") is None)
    check("负数能解析", _num("-3.2") == -3.2)

    print("\n[2] ST 与停牌识别")
    st = mk("sh600001", "*ST 某某", 5, 1e8, 10, 1, 1e6, 1)
    check("名称含 ST → is_st", st.is_st, st.name)
    normal = mk("sh600002", "正常公司", 5, 1e8, 10, 1, 1e6, 1)
    check("正常名称 → 不是 ST", not normal.is_st)
    susp = mk("sh600003", "停牌股", 5, 0, 10, 1, 1e6, 0)
    check("成交额 0 → 停牌", susp.is_suspended)
    check("有成交额 → 不停牌", not normal.is_suspended)

    print("\n[3] 硬性条件：逐条判定")
    # ⚠️ 注意：HardFilters 的**默认值不含涨跌幅上下限**（change_min/max 都是 None），
    #    因为不同策略对涨跌幅的要求完全不同（超跌反转要跌、放量突破要涨）。
    #    所以测涨跌幅必须显式构造一个带上下限的。
    f = HardFilters()
    check("默认剔除 ST", f.check(st) == "ST", f.check(st) or "")
    check("默认剔除停牌", f.check(susp) == "停牌", f.check(susp) or "")
    good = mk("sh600004", "好公司", 20, 3e8, 10, 1.5, 1e6, 1.0)
    check("合格样本通过", f.check(good) is None, f.check(good) or "通过")

    # 逐条边界
    check("成交额不足被拒", f.check(mk("x", "t", 20, 1e6, 10, 1.5, 1e6, 1)) == "成交额不足")
    check("PE 过高被拒", f.check(mk("x", "t", 20, 3e8, 50, 1.5, 1e6, 1)) == "PE 过高")
    check("PB 过高被拒", f.check(mk("x", "t", 20, 3e8, 10, 5.0, 1e6, 1)) == "PB 过高")
    check("市值过小被拒", f.check(mk("x", "t", 20, 3e8, 10, 1.5, 1e3, 1)) == "市值过小")
    check("价格过低被拒", f.check(mk("x", "t", 1, 3e8, 10, 1.5, 1e6, 1)) == "价格过低")
    check("价格过高被拒", f.check(mk("x", "t", 200, 3e8, 10, 1.5, 1e6, 1)) == "价格过高")
    check("亏损股（无 PE）被 PE 条件拒",
          f.check(mk("x", "t", 20, 3e8, None, 1.5, 1e6, 1)) == "无 PE（亏损或未披露）")

    # 涨跌幅：显式带上限才检查。用同花顺口径的「不追高」条件。
    f_chg = HardFilters(change_min=-4.5, change_max=4.5)
    check("默认不限涨跌幅（涨 9% 也放行）", f.check(mk("x", "t", 20, 3e8, 10, 1.5, 1e6, 9)) is None,
          "默认 change_max=None，所以不拒 —— 这是有意的")
    check("带上下限时涨幅过大被拒",
          f_chg.check(mk("x", "t", 20, 3e8, 10, 1.5, 1e6, 9)) == "涨幅过大（追高）")
    check("带上下限时跌幅过大被拒",
          f_chg.check(mk("x", "t", 20, 3e8, 10, 1.5, 1e6, -9)) == "跌幅过大")
    check("带上下限时正常涨跌放行",
          f_chg.check(mk("x", "t", 20, 3e8, 10, 1.5, 1e6, 1.0)) is None)

    print("\n[4] PE 条件的开关语义（容易搞错）")
    f_no_pe = HardFilters(pe_min=None, pe_max=None)
    check("不要求 PE 时，亏损股放行",
          f_no_pe.check(mk("x", "t", 20, 3e8, None, 1.5, 1e6, 1)) is None)
    f_pos = HardFilters(pe_min=0.0, pe_max=None)
    check("pe_min=0 时，亏损股被拒（因为要 0<PE）",
          f_pos.check(mk("x", "t", 20, 3e8, None, 1.5, 1e6, 1)) == "无 PE（亏损或未披露）")

    print("\n[5] 过滤统计")
    pool = [
        good,
        st,
        susp,
        mk("sh600005", "贵的", 20, 3e8, 50, 1.5, 1e6, 1),
        mk("sh600006", "小的", 20, 3e8, 10, 1.5, 1e3, 1),
    ]
    kept, reasons = apply_filters(pool, f)
    check("5 只里通过 1 只", len(kept) == 1, f"{len(kept)} 只")
    check("统计了拒绝原因", len(reasons) >= 3, str(reasons))
    check("原因计数正确", reasons.get("ST") == 1 and reasons.get("PE 过高") == 1,
          str(reasons))

    print("\n[6] 百分位排名")
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    check("最小值 ascending → 100 分", _percentile_rank(vals, 1.0, True) == 100.0,
          str(_percentile_rank(vals, 1.0, True)))
    check("最大值 ascending → 0 分", _percentile_rank(vals, 5.0, True) == 0.0)
    check("中位数 → 50 分", abs(_percentile_rank(vals, 3.0, True) - 50.0) < 1e-9)
    check("ascending=False 反过来", _percentile_rank(vals, 5.0, False) == 100.0)
    check("单元素 → 50", _percentile_rank([1.0], 1.0, True) == 50.0)

    print("\n[7] 横截面打分")
    cands = [
        mk("a", "便宜好股", 20, 3e8, 5, 0.5, 1e6, 1.0),
        mk("b", "贵股", 20, 3e8, 50, 5.0, 1e6, 1.0),
        mk("c", "中间", 20, 3e8, 25, 2.5, 1e6, 1.0),
    ]
    w = {"value_pe": 1.0}
    sc = score_cross_section(cands, w)
    check("打分结果数量正确", len(sc) == 3)
    check("PE 最低的排第一", sc[0]["snapshot"].symbol == "a",
          f"第一是 {sc[0]['snapshot'].symbol} ({sc[0]['score']}分)")
    check("PE 最高的排最后", sc[-1]["snapshot"].symbol == "b")
    check("分数在 0~100", all(0 <= x["score"] <= 100 for x in sc),
          str([x["score"] for x in sc]))
    check("按分数降序", sc[0]["score"] >= sc[1]["score"] >= sc[2]["score"])

    print("\n[8] 缺失因子得中性分")
    cands2 = [
        mk("a", "有PE", 20, 3e8, 5, 1, 1e6, 1),
        mk("b", "无PE", 20, 3e8, None, 1, 1e6, 1),
    ]
    sc2 = score_cross_section(cands2, {"value_pe": 1.0})
    b = next(x for x in sc2 if x["snapshot"].symbol == "b")
    check("无 PE 的得 50 分（不是 0 分）", b["score"] == 50.0, str(b["score"]))
    check("无 PE 的不会被剔除", len(sc2) == 2)

    print("\n[9] 四个策略的配置合理性")
    check("有 4 个策略", len(STRATEGIES) == 4, str(list(STRATEGIES)))
    for key, s in STRATEGIES.items():
        check(f"{key} 有权重", bool(s.weights), str(s.weights))
        # ⚠️ 允许权重为 0 —— 那是「只用它当过滤条件，不参与打分」。
        #    超跌反转策略就是故意的：把动量权重设 0，
        #    因为「跌得越多分越高」等于鼓励接飞刀。
        check(f"{key} 至少有一个正权重因子",
              any(w > 0 for w in s.weights.values()),
              str({k: v for k, v in s.weights.items() if v > 0}))
        check(f"{key} 权重引用的因子都存在",
              all(k in FACTORS for k in s.weights),
              str([k for k in s.weights if k not in FACTORS]))
        check(f"{key} 有说明文字", bool(s.desc))
    check("超跌反转的动量权重是 0（只当过滤，不参与打分）",
          STRATEGIES["oversold_reversal"].weights.get("momentum") == 0.0,
          str(STRATEGIES["oversold_reversal"].weights.get("momentum")))

    print("\n[10] 策略之间不该选出同一批")
    # 双低要求 PE 0~15，放量突破不限 PE —— 条件必须真的不同
    dl = STRATEGIES["dual_low"].filters
    vb = STRATEGIES["volume_breakout"].filters
    check("双低限 PE，放量突破不限 PE",
          dl.pe_max is not None and vb.pe_max is None,
          f"dual_low pe_max={dl.pe_max}  volume_breakout pe_max={vb.pe_max}")
    check("超跌反转要求跌（change_max<0）",
          STRATEGIES["oversold_reversal"].filters.change_max < 0,
          str(STRATEGIES["oversold_reversal"].filters.change_max))
    check("放量突破要求涨（change_min>0）",
          STRATEGIES["volume_breakout"].filters.change_min > 0,
          str(STRATEGIES["volume_breakout"].filters.change_min))
    check("四个策略的成交额门槛不全相同",
          len({s.filters.amount_min for s in STRATEGIES.values()}) > 1,
          str({k: s.filters.amount_min for k, s in STRATEGIES.items()}))

    print("\n[11] 条件描述可读")
    for key, s in STRATEGIES.items():
        desc = s.filters.describe()
        check(f"{key} 的条件能描述出来", len(desc) >= 5, "、".join(desc))

    print("\n[12] 结果可 JSON 序列化")
    payload = run_strategy.__doc__ and {
        "strategy": "dual_low",
        "filters": STRATEGIES["dual_low"].filters.describe(),
        "results": [good.to_dict()],
    }
    try:
        txt = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        check("严格 JSON 通过", True, f"{len(txt)} 字节")
    except (ValueError, TypeError) as e:
        check("严格 JSON 通过", False, str(e))
    check("to_dict 有派生字段",
          "mktcapYi" in good.to_dict() and "amountYi" in good.to_dict())

    print("\n" + "=" * 74)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 74)
    return 1 if fail else 0


# ══════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A 股选股引擎（三层漏斗）")
    ap.add_argument("--strategy", default="dual_low",
                    choices=list(STRATEGIES), help="用哪个策略")
    ap.add_argument("--top", type=int, default=None, help="取前 N 只")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--list", action="store_true", help="列出所有策略")
    ap.add_argument("--all", action="store_true",
                    help="跑全部策略（共用一份快照，省时间）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.list:
        if a.json:
            print(json.dumps([{
                "key": s.key, "label": s.label, "desc": s.desc,
                "filters": s.filters.describe(),
                "weights": s.weights, "topN": s.top_n,
            } for s in STRATEGIES.values()], ensure_ascii=False))
        else:
            print("=" * 74)
            print("  选股策略")
            print("=" * 74)
            for s in STRATEGIES.values():
                print(f"\n  {s.label}  ({s.key})")
                print(f"    {s.desc}")
                print("    条件：" + "、".join(s.filters.describe()))
                print("    权重：" + "、".join(
                    f"{FACTORS[k][0]} {v:.2f}" for k, v in s.weights.items() if v > 0))
        return 0

    try:
        if a.all:
            market = fetch_market(verbose=a.verbose)
            out = [run_strategy(k, market=market, top_n=a.top, verbose=False)
                   for k in STRATEGIES]
        else:
            out = run_strategy(a.strategy, top_n=a.top, verbose=a.verbose)
    except Exception as e:                            # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1

    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for r in (out if isinstance(out, list) else [out]):
            fn = r["funnel"]
            print("=" * 74)
            print(f"  {r['label']}  ({r['strategy']})")
            print("=" * 74)
            print(f"  {r['desc']}")
            print(f"\n  条件：" + "、".join(r["filters"]))
            print(f"  漏斗：{fn['market']} 只 → 过滤后 {fn['afterFilter']} 只 "
                  f"→ 取前 {fn['top']} 只   ({r['timing']['totalMs']}ms)")
            if fn["rejected"]:
                top_rej = sorted(fn["rejected"].items(), key=lambda x: -x[1])[:6]
                print("  被拒原因：" + "、".join(f"{k} {v}" for k, v in top_rej))
            print(f"\n  {'代码':<10}{'名称':<10}{'现价':>9}{'涨跌':>8}"
                  f"{'PE':>8}{'PB':>7}{'市值亿':>10}{'成交额亿':>10}{'得分':>7}")
            for x in r["results"]:
                print(f"  {x['symbol']:<10}{x['name'][:9]:<10}"
                      f"{(x['price'] or 0):>9.2f}{(x['change_pct'] or 0):>7.2f}%"
                      f"{(x['pe'] if x['pe'] is not None else 0):>8.2f}"
                      f"{(x['pb'] if x['pb'] is not None else 0):>7.2f}"
                      f"{(x['mktcapYi'] or 0):>10.1f}{(x['amountYi'] or 0):>10.2f}"
                      f"{x['score']:>7.1f}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
