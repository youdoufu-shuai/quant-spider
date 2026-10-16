#!/usr/bin/env python3
"""
L3 选股分析 —— 对少量候选做深度分析，可选交给 AI 排序。

═══════════════════════════════════════════════════════════════
  为什么只处理少量候选
═══════════════════════════════════════════════════════════════
L1（screener.py）用全市场快照 + 硬性条件，2~7 秒把 5500 只砍到几百只。
L2 用横截面因子排序取 Top N。

到了 L3，要做的是**拉历史 K 线算技术指标** —— 这个每只 171ms。
对 5500 只做是 15 分钟；对 Top 30 做只要 5 秒。

所以三层漏斗不是"设计得优雅"，是**被数据源吞吐逼出来的**。

═══════════════════════════════════════════════════════════════
  AI 在这里做什么、不做什么
═══════════════════════════════════════════════════════════════
参考 AlphaSift 的职责划分：**硬条件由代码执行，软判断由 LLM 执行。**

AI 做（规则难覆盖的）：
  · 解释因子冲突 —— 估值好但趋势弱、资金强但在追高
  · 提炼交易逻辑 —— 把候选归到「低估值修复」「资金热度」等主题
  · 识别共同风险 —— 最终名单是不是全押在同一行业/风格
  · 输出可复盘字段 —— watch_items / invalidators，以后能验证判断对不对

AI **不**做：
  · 不做硬性筛选（那是 L1 的活，可审计）
  · **不能推荐候选池以外的股票**（prompt 里明确禁止，并且我们要校验输出）

═══════════════════════════════════════════════════════════════
  没有 API key 时怎么办
═══════════════════════════════════════════════════════════════
不报错、不假装成功。走**本地规则打分**（用 L2 分数 + 技术面确认度），
并在结果里标明 `mode: "local"`，页面上也会显示。
这样没配 key 也能用，配了 key 就升级成 AI 排序。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from datasource import fetch, DataSourceError                  # noqa: E402
from indicators import compute                                 # noqa: E402
from strategy import run_all                                   # noqa: E402
from autotrader import (                                       # noqa: E402
    AutoConfig, AutoDecider, score_factors, regime_weights,
    FACTOR_LABELS, _val,
)


# ══════════════════════════════════════════════════════════════
#  技术面富化：给每只候选补上「时序」信息
# ══════════════════════════════════════════════════════════════
def enrich_one(symbol: str, market: str = "ashare", interval: str = "1d",
               bars: int = 250, timeout_ok: bool = True) -> dict | None:
    """
    拉一只股票的历史，算 27 指标 + 6 因子 + 5 策略信号。

    返回 None 表示这只拉不到数据（网络问题或代码不对）——
    **不要抛异常**，因为一只失败不该让整批失败。
    """
    try:
        s = fetch(market, symbol, interval, bars)
    except (DataSourceError, Exception):                       # noqa: BLE001
        return None
    if len(s.bars) < 60:
        return None

    try:
        ind = compute(s)
        i = len(s.bars) - 1
        factors, score, regime = score_factors(ind, i)
        adx = _val(ind, "adx", i)
        weights, _ = regime_weights(adx)
        sigs = run_all(ind)
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=i, position=0, fraction=0.0, entry_price=0.0,
            peak_price=0.0, drawdown=0.0, bars_since_exit=999, bars_held=0)
    except Exception:                                          # noqa: BLE001
        return None

    # 技术面摘要：只取对判断有用的几个，不要把所有指标都塞给 AI
    snap = ind.snapshot(i)
    recent = s.bars[-1]
    # 近 20 日涨跌幅 —— 比单日涨跌更能说明趋势
    c20 = s.bars[-21].close if len(s.bars) > 21 else s.bars[0].close
    chg20 = (recent.close / c20 - 1) * 100 if c20 else 0.0
    # 距 250 日高点
    hi = max(b.high for b in s.bars)
    lo = min(b.low for b in s.bars)
    pos = ((recent.close - lo) / (hi - lo) * 100) if hi > lo else 50.0

    return {
        "symbol": symbol,
        "name": s.name or symbol,
        "bars": len(s.bars),
        "period": f"{s.bars[0].as_dict()['iso'][:10]} → {recent.as_dict()['iso'][:10]}",
        "close": round(recent.close, 3),
        "chg20": round(chg20, 2),            # 近 20 日涨跌幅
        "rangePos": round(pos, 1),           # 在 250 日区间的位置（0=最低，100=最高）
        "tech": {
            "rsi": round(_val(ind, "rsi", i) or 0, 1),
            "adx": round(adx, 1) if adx is not None else None,
            "atrPct": round((_val(ind, "atr", i) or 0) / recent.close * 100, 2),
            "macdHist": round(_val(ind, "macd_hist", i) or 0, 4),
            "kdjK": round(_val(ind, "kdj_k", i) or 0, 1),
            "ma20": round(_val(ind, "sma20", i) or 0, 3),
            "aboveMa20": bool((_val(ind, "sma20", i) or 0) and recent.close > _val(ind, "sma20", i)),
        },
        "signals": {
            k: {"label": v.label, "current": v.current(),
                "entries": int(v.entry.sum()), "exits": int(v.exit.sum())}
            for k, v in sigs.items()
        },
        "factors": {k: round(v, 3) for k, v in factors.items()},
        "factorLabels": FACTOR_LABELS,
        "score": round(score, 4),
        "regime": regime,
        "weights": {k: round(v, 2) for k, v in weights.items()},
        "decision": {"action": dec.action, "target": dec.target,
                     "reasons": dec.reasons[:4]},
    }


def enrich(cands: list[dict], market: str = "ashare", interval: str = "1d",
           bars: int = 250, workers: int = 6, verbose: bool = False) -> list[dict]:
    """
    并发富化一批候选。失败的会被剔除（并记数）。

    ⚠️ 用线程池而不是串行：30 只 × 171ms = 5.1 秒串行，
       6 并发约 1 秒。而且这是纯 IO 等待，线程池足够。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: list[dict] = []
    failed: list[str] = []
    t0 = time.time()

    def one(c):
        sym = c.get("symbol")
        e = enrich_one(sym, market, interval, bars)
        if e is None:
            return None
        # 把 L2 的信息并进来（L2 有估值，L3 没有）
        e["l2Score"] = c.get("score")
        e["sector"] = c.get("sector")
        e["l2Factors"] = c.get("factors")
        # ⚠️ 名称要用 L2 快照里的，不要用 datasource 返回的。
        #    datasource.fetch() 拿不到中文名时会把 name 设成代码本身，
        #    结果界面上显示「sh601668」而不是「中国建筑」（实测踩过）。
        if c.get("name"):
            e["name"] = c["name"]
        # 估值字段：L2 快照里有，L3 要用
        for k in ("pe", "pb", "mktcapYi", "amountYi", "turnover", "change_pct",
                  "price"):
            if k in c:
                e[k] = c[k]
        return e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, cands):
            if r is None:
                continue
            out.append(r)

    elapsed = int((time.time() - t0) * 1000)
    if verbose:
        print(f"  富化 {len(out)} 只 / {len(cands)} 只，{elapsed}ms",
              file=sys.stderr)
    return out


# ══════════════════════════════════════════════════════════════
#  本地规则打分（没有 AI 时用）
# ══════════════════════════════════════════════════════════════
def local_rank(enriched: list[dict]) -> dict:
    """
    纯代码打分。可审计，每一步都能解释。

    分数构成：
      · L2 横截面分（60%）  —— 相对同批候选的估值/流动性优势
      · 技术面确认（40%）    —— 趋势方向、是否过热、位置
    权重写死在这里，不做调参 —— 它只是 AI 不在时的兜底，不是研究成果。
    """
    for e in enriched:
        t = e.get("tech") or {}
        l2 = e.get("l2Score") or 50.0
        pts = 0.0
        why = []

        # 趋势方向：站上 MA20 加分
        if t.get("aboveMa20"):
            pts += 10
            why.append("站上 20 日线")
        else:
            pts -= 6
            why.append("跌破 20 日线")

        # ADX 确认趋势强度
        adx = t.get("adx") or 0
        if adx >= 25:
            pts += 10
            why.append(f"ADX {adx:.0f} 趋势明确")
        elif adx < 18:
            pts -= 4
            why.append(f"ADX {adx:.0f} 无趋势")

        # RSI 过热要扣分（不追高是共同纪律）
        rsi = t.get("rsi") or 50
        if rsi >= 78:
            pts -= 12
            why.append(f"RSI {rsi:.0f} 过热")
        elif rsi <= 32:
            pts += 6
            why.append(f"RSI {rsi:.0f} 超卖")
        else:
            pts += 3

        # 20 日涨幅过大要扣分
        c20 = e.get("chg20") or 0
        if c20 >= 25:
            pts -= 10
            why.append(f"20 日已涨 {c20:.0f}%")
        elif c20 > 0:
            pts += 4

        # 位置：太高说明涨过了
        pos = e.get("rangePos") or 50
        if pos >= 92:
            pts -= 6
            why.append("接近年内高点")
        elif pos <= 25:
            pts += 4
            why.append("处于年内低位")

        e["techScore"] = round(pts, 1)
        e["techWhy"] = why
        e["finalScore"] = round(l2 * 0.6 + (50 + pts) * 0.4, 2)

    ranked = sorted(enriched, key=lambda x: -x["finalScore"])
    return {
        "mode": "local",
        "note": "未配置 AI（AI 设置里填 API key 可启用）。这里是本地规则打分。",
        "results": ranked,
    }


# ══════════════════════════════════════════════════════════════
#  AI 排序
# ══════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """你是一个 A 股选股助手。用户会给你一批**已经过硬性条件筛选**的候选股票，
每只都带估值、技术面、策略信号数据。

你的任务是**在这批候选里做横向比较和取舍**，不是单只点评。

═══ 硬性规则（必须遵守）═══

1. **只能从给定候选里选。绝对不要推荐任何未出现在列表里的股票。**
   如果你觉得整批都不好，就返回空列表，并在 market_view 里说明原因。

2. 输出必须是**合法 JSON**，不要 markdown 代码块，不要任何解释性前后缀。

3. 每只入选的股票必须给出：
   - symbol：必须与输入完全一致（如 sh601668）
   - rank：1 开始的名次
   - thesis：一句话买入逻辑（20~40 字）
   - risks：这只股票的主要风险，数组，1~3 条
   - watch：接下来该盯什么（如"能否站稳 20 日线"）
   - invalidators：什么情况说明逻辑错了、该放弃
   - confidence：0~1 的把握度

4. 组合层面要注意：
   - **避免全押在同一行业**。如果候选里某个行业很多，最多选 2 只。
   - 如果发现最终名单有共同风险（都是周期股、都依赖同一个政策等），
     必须在 portfolio_risks 里指出来。

═══ A 股的特殊性 ═══
- 这套数据里 PE 为空的股票是**亏损股**，不是数据缺失。
- RSI > 78 通常是追高，ADX < 18 说明没有趋势。
- 涨幅过大的标的要谨慎，A 股散户追高被套是常见模式。

═══ 诚实要求 ═══
- 不要因为"必须推荐"就硬选。候选质量差时，少选或返回空是正确做法。
- confidence 要真实反映你的不确定程度，不要都给 0.8。
"""


def _build_user_prompt(cands: list[dict], n_pick: int, extra: str = "") -> str:
    """把候选整理成给 AI 的文本。只给有用的字段，不塞原始数组。"""
    lines = [f"以下是 {len(cands)} 只候选股票，请从中选出最多 {n_pick} 只。", ""]
    for c in cands:
        t = c.get("tech") or {}
        lines.append(
            f"【{c['symbol']}】{c['name']}  行业:{c.get('sector') or '未知'}")
        lines.append(
            f"  现价 {c.get('price')}  当日 {c.get('change_pct')}%  "
            f"近20日 {c.get('chg20')}%  年内位置 {c.get('rangePos')}%")
        lines.append(
            f"  估值 PE {c.get('pe')}  PB {c.get('pb')}  "
            f"市值 {c.get('mktcapYi')}亿  成交额 {c.get('amountYi')}亿  "
            f"换手 {c.get('turnover')}%")
        lines.append(
            f"  技术 RSI {t.get('rsi')}  ADX {t.get('adx')}  "
            f"MACD柱 {t.get('macdHist')}  ATR {t.get('atrPct')}%  "
            f"{'站上' if t.get('aboveMa20') else '跌破'}20日线")
        lines.append(
            f"  L2横截面分 {c.get('l2Score')}  因子分 {c.get('l2Factors')}")
        lines.append(
            f"  策略信号 " + "  ".join(
                f"{v['label']}{'持多' if v['current'] == 1 else '空仓'}"
                for v in (c.get("signals") or {}).values()))
        lines.append("")
    if extra:
        lines += ["额外背景（用户提供）：", extra, ""]
    lines.append(
        "请输出 JSON，格式："
        '{"market_view":"整体看法一句话",'
        '"picks":[{"symbol":"...","rank":1,"thesis":"...",'
        '"risks":["..."],"watch":"...","invalidators":"...","confidence":0.7}],'
        '"portfolio_risks":["..."],'
        '"avoided":[{"symbol":"...","why":"..."}]}')
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """
    从 AI 回复里抠出 JSON。

    为什么要这么麻烦：即使要求"只输出 JSON"，模型仍可能加 ```json 包裹
    或前后缀说明。所以要：
      1. 先去 markdown 代码块
      2. 再找第一个 { 到最后一个 }
      3. 失败就返回 None（调用方会回退到本地打分）
    """
    if not text:
        return None
    t = text.strip()
    # 去 ```json ... ```
    if "```" in t:
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    # 找最外层大括号
    lo = t.find("{")
    hi = t.rfind("}")
    if lo < 0 or hi <= lo:
        return None
    try:
        return json.loads(t[lo:hi + 1])
    except json.JSONDecodeError:
        return None


def validate_picks(parsed: dict, cands: list[dict]) -> tuple[dict, list[str]]:
    """
    校验 AI 输出。返回 (清洗后的结果, 问题列表)。

    ⚠️ 这是**安全边界**，不是可选项：
       模型可能编造股票代码（幻觉），也可能推荐候选池外的"更好的股票"。
       用户的界面会展示这些代码，如果不校验就等于把幻觉当事实。
    """
    problems: list[str] = []
    by_sym = {c["symbol"]: c for c in cands}
    # 也接受不带前缀的代码（模型有时会写 601668）
    by_code = {c["symbol"][2:]: c for c in cands if len(c["symbol"]) > 2}

    picks = parsed.get("picks")
    if not isinstance(picks, list):
        return {"market_view": parsed.get("market_view", ""),
                "picks": [], "portfolio_risks": parsed.get("portfolio_risks", []),
                "avoided": parsed.get("avoided", [])}, ["picks 不是数组"]

    clean: list[dict] = []
    seen: set[str] = set()
    for p in picks:
        if not isinstance(p, dict):
            problems.append("picks 里有非对象项")
            continue
        sym = str(p.get("symbol") or "").strip()
        # 归一化：允许 601668 / sh601668 / SH601668
        hit = by_sym.get(sym) or by_sym.get(sym.lower())
        if hit is None:
            hit = by_code.get(sym) or by_code.get(sym.lstrip("shszbj"))
        if hit is None:
            problems.append(f"丢弃 AI 编造的标的：{sym}（不在候选池里）")
            continue
        if hit["symbol"] in seen:
            problems.append(f"重复标的已去重：{hit['symbol']}")
            continue
        seen.add(hit["symbol"])

        try:
            conf = float(p.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.5
        clean.append({
            "symbol": hit["symbol"],
            "name": hit["name"],
            "rank": len(clean) + 1,          # 名次以清洗后的顺序为准
            "thesis": str(p.get("thesis") or "")[:200],
            "risks": [str(x)[:120] for x in (p.get("risks") or [])][:3]
                     if isinstance(p.get("risks"), list) else [],
            "watch": str(p.get("watch") or "")[:120],
            "invalidators": str(p.get("invalidators") or "")[:160],
            "confidence": max(0.0, min(1.0, conf)),
            # 把原始数据一起带上，页面要显示
            "price": hit.get("price"),
            "change_pct": hit.get("change_pct"),
            "pe": hit.get("pe"),
            "pb": hit.get("pb"),
            "sector": hit.get("sector"),
            "l2Score": hit.get("l2Score"),
            "chg20": hit.get("chg20"),
            "rangePos": hit.get("rangePos"),
            "tech": hit.get("tech"),
        })

    out = {
        "market_view": str(parsed.get("market_view") or "")[:300],
        "picks": clean,
        "portfolio_risks": [str(x)[:160] for x in (parsed.get("portfolio_risks") or [])][:5]
                           if isinstance(parsed.get("portfolio_risks"), list) else [],
        "avoided": parsed.get("avoided") if isinstance(parsed.get("avoided"), list) else [],
    }
    return out, problems


def ai_rank(enriched: list[dict], n_pick: int = 8,
            extra_context: str = "") -> dict:
    """
    让 AI 排序。没有可用配置时**回退到本地打分**（并标明）。

    返回 {mode, results/picks, problems, note, ...}
    """
    from ai_settings import AIStore

    try:
        store = AIStore()
        cfg = store.load()
    except Exception as e:                                     # noqa: BLE001
        out = local_rank(enriched)
        out["note"] = f"读取 AI 配置失败（{type(e).__name__}），已回退本地打分。"
        return out

    if not cfg.api_key:
        return local_rank(enriched)
    if not cfg.enabled:
        out = local_rank(enriched)
        out["note"] = "AI 决策未启用（AI 设置里的开关），已回退本地打分。"
        return out

    # 直接用 provider 的 chat 接口。不通过 AutoDecider —— 那个是单股决策用的。
    import urllib.request

    url = cfg.url
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(enriched, n_pick, extra_context)},
        ],
        "temperature": min(cfg.temperature, 0.6),   # 选股要稳定，压低温度
        "stream": False,
    }
    # DeepSeek 等支持 response_format 的可以强制 JSON；不支持的服务会报错，
    # 所以这里不强制，靠 prompt + 解析兜底。
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.api_key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout or 60) as r:
            resp = json.loads(r.read().decode("utf8", "replace"))
    except Exception as e:                                     # noqa: BLE001
        out = local_rank(enriched)
        out["note"] = f"AI 请求失败（{type(e).__name__}: {str(e)[:80]}），已回退本地打分。"
        return out
    elapsed = int((time.time() - t0) * 1000)

    try:
        text = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        out = local_rank(enriched)
        out["note"] = "AI 返回格式异常，已回退本地打分。"
        return out

    parsed = _extract_json(text)
    if parsed is None:
        out = local_rank(enriched)
        out["note"] = "AI 没有返回可解析的 JSON，已回退本地打分。"
        out["rawReply"] = text[:500]
        return out

    clean, problems = validate_picks(parsed, enriched)
    return {
        "mode": "ai",
        "provider": cfg.provider,
        "model": cfg.model,
        "elapsedMs": elapsed,
        "marketView": clean["market_view"],
        "picks": clean["picks"],
        "portfolioRisks": clean["portfolio_risks"],
        "avoided": clean["avoided"],
        "problems": problems,
        "nCandidates": len(enriched),
        "tokens": (resp.get("usage") or {}),
    }


# ══════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L3 选股分析（技术面富化 + AI 排序）")
    ap.add_argument("--strategy", default="dual_low")
    ap.add_argument("--top", type=int, default=30, help="L2 取前 N 只做富化")
    ap.add_argument("--pick", type=int, default=8, help="AI 最终选几只")
    ap.add_argument("--no-ai", action="store_true", help="强制用本地打分")
    ap.add_argument("--context", default="", help="给 AI 的额外背景")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    from screener import run_strategy

    t0 = time.time()
    l2 = run_strategy(a.strategy, top_n=a.top)
    print(f"  L1+L2: {l2['funnel']['market']} → {l2['funnel']['afterFilter']} "
          f"→ {l2['funnel']['top']} 只 ({l2['timing']['totalMs']}ms)", file=sys.stderr)

    enriched = enrich(l2["results"], verbose=True)
    print(f"  L3 富化: {len(enriched)} 只 ({int((time.time()-t0)*1000)}ms 累计)",
          file=sys.stderr)

    if a.no_ai:
        res = local_rank(enriched)
    else:
        res = ai_rank(enriched, n_pick=a.pick, extra_context=a.context)

    res["strategy"] = l2["strategy"]
    res["label"] = l2["label"]
    res["funnel"] = l2["funnel"]
    res["sectors"] = l2["sectors"]
    # ⚠️ timing 也要带上 —— 页面上「总耗时」读的是这个字段。
    #    第一版漏了，结果页面显示「总耗时 —ms」（实测踩过）。
    res["timing"] = l2["timing"]
    res["filters"] = l2["filters"]
    res["maxPerSector"] = l2.get("maxPerSector")
    res["elapsedTotalMs"] = int((time.time() - t0) * 1000)
    # 富化阶段的耗时单独给一份，前端能拆开看
    res["timing"] = {**l2["timing"],
                     "enrichMs": res["elapsedTotalMs"] - l2["timing"]["totalMs"]}

    if a.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0

    print("=" * 74)
    print(f"  {l2['label']} · {res['mode']} 模式")
    print("=" * 74)
    if res.get("note"):
        print(f"  注意：{res['note']}")
    if res["mode"] == "ai":
        print(f"  模型：{res['provider']} / {res['model']}  "
              f"({res['elapsedMs']}ms, {res.get('tokens', {}).get('total_tokens', '?')} tokens)")
        if res.get("marketView"):
            print(f"  整体看法：{res['marketView']}")
        if res.get("problems"):
            print("  ⚠ 校验问题：")
            for p in res["problems"]:
                print(f"     · {p}")
        print()
        for p in res["picks"]:
            print(f"  {p['rank']}. {p['symbol']} {p['name']}  "
                  f"[{p.get('sector')}]  把握 {p['confidence']:.0%}")
            print(f"     逻辑：{p['thesis']}")
            if p.get("risks"):
                print(f"     风险：{'；'.join(p['risks'])}")
            print(f"     盯：{p['watch']}")
            print(f"     放弃条件：{p['invalidators']}")
            print()
        if res.get("portfolioRisks"):
            print("  组合风险：")
            for r in res["portfolioRisks"]:
                print(f"     · {r}")
    else:
        print(f"  {'代码':<10}{'名称':<10}{'行业':<10}{'L2分':>7}{'技术分':>8}{'综合':>8}")
        for e in res["results"][:a.pick]:
            print(f"  {e['symbol']:<10}{e['name'][:9]:<10}{(e.get('sector') or '-'):<10}"
                  f"{(e.get('l2Score') or 0):>7.1f}{e.get('techScore', 0):>8.1f}"
                  f"{e.get('finalScore', 0):>8.1f}  {'、'.join((e.get('techWhy') or [])[:3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
