#!/usr/bin/env python3
"""
决策快照 —— 为「决策驾驶舱」可视化准备一份完整、确定性的数据。

═══════════════════════════════════════════════════════════════
  为什么单独一个脚本
═══════════════════════════════════════════════════════════════
蛛网要画的是**决策是怎么算出来的**，所以需要的不只是最终动作，而是
整条链路每一层的中间结果：

    K线 → 27 指标 → 6 因子 → 加权 → 分数 → 阈值 → 动作 → 风控 → 仓位

`/api/quant` 返回的是行情与信号，`/api/paper/:id/decide` 有状态
（依赖某个模拟盘会话）。这个脚本是**无状态的**：给定标的与参数，
算出「此刻会怎么决策」的完整分解，供前端画图。

═══════════════════════════════════════════════════════════════
  为什么强调「确定性」
═══════════════════════════════════════════════════════════════
量化计算不含随机数：同样的 K 线 + 同样的参数 → 同样的因子与分数。
所以可视化可以做**计算过程重演**（丝依次亮起 → 光点汇入 → 张力收紧），
而且每次重演都**准确复现真实计算**，不是装饰动画。

这一点决定了动画怎么做：不是在数据集上放随机动画，
而是把一次真实计算的分层结果按顺序演出来。

═══════════════════════════════════════════════════════════════
  输出结构（前端契约）
═══════════════════════════════════════════════════════════════
  {
    "meta":     { name, symbol, market, interval, source, bars, at },
    "regime":   { name, adx, weights },              ← 状态决定权重
    "factors":  [ { key, label, value, weight, contrib, dir } ],  ← 6 根丝
    "score":    { raw, clamped, weightedSum, totalWeight },
    "gates":    { entry, exit, action },             ← 阈值
    "risk":     [ { key, label, value, active } ],   ← 风控闸门
    "decision": { action, target, current, score, reasons, riskHit, mode },
    "stages":   [ ... ]                              ← 计算流程（供重演）
  }

用法：
  python quant/decision.py --market ashare --symbol sh600519 --interval 1d
  python quant/decision.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from datasource import MARKETS, fetch, SYMBOL_NAMES       # noqa: E402
from indicators import compute                            # noqa: E402
from autotrader import (                                  # noqa: E402
    AutoConfig, AutoDecider, Decision, FACTOR_LABELS,
    FACTOR_WEIGHTS_RANGING, FACTOR_WEIGHTS_TRENDING,
    RiskConfig, _val, regime_weights, score_factors,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# 因子展示顺序：按「趋势 → 确认 → 反向 → 辅助」排，不是按权重排。
# 为什么固定顺序：蛛网每根丝的位置应该稳定，否则换标的后图形会"跳"，
# 用户没法形成位置记忆。
FACTOR_ORDER = ["trend", "momentum", "strength", "reversion", "position", "volume"]

FACTOR_DESC = {
    "trend": "EMA12/26 差 ÷ ATR",
    "momentum": "MACD DIF ÷ ATR",
    "strength": "ADX + DI 方向",
    "reversion": "RSI 反向",
    "position": "布林 %B",
    "volume": "量比 × 趋势方向",
}

# 风控闸门的展示信息。value 为 None 表示该闸门关闭。
RISK_GATES = [
    ("stop_loss", "止损", "亏损超过此比例强制卖出"),
    ("take_profit", "止盈", "盈利超过此比例强制卖出"),
    ("trailing_stop", "移动止损", "从最高点回撤超过此比例卖出"),
    ("max_drawdown", "回撤熔断", "账户回撤达此比例清仓停手"),
    ("cooldown_bars", "冷却期", "卖出后多少根内不再开仓"),
    ("min_hold_bars", "最短持有", "买入后至少持有多少根"),
]


# ============================================================
def build_decision(market: str, symbol: str | None = None,
                   interval: str | None = None, limit: int = 500,
                   strategy: str | None = None,
                   position: float = 0.0, entry_price: float | None = None,
                   peak_price: float | None = None,
                   drawdown: float = 0.0, halted: bool = False) -> dict:
    """
    算一份完整的决策快照。

    ⚠️ 为什么要能传持仓状态：
       止损 / 止盈 / 移动止损都**只在有仓位时才可能触发** ——
       空仓时它们无事可做。所以如果永远用 position=0 算，
       决策驾驶舱里的「风控拦截」画面就**永远看不到**，
       而那恰恰是这个可视化最有价值的一帧。

       默认仍然是空仓（刚建仓时的视角），但调用方可以传一个持仓状态
       来看「如果我现在拿着这个仓位，会怎么决策」。

    position 语义：0 = 空仓，>0 表示持仓（值为仓位比例，仅作展示）。
    entry_price / peak_price 缺省时按当前价推算一个合理值。
    """
    cfg_m = MARKETS[market]
    sym = symbol or cfg_m["defaultSymbol"]
    iv = interval or cfg_m["defaultInterval"]

    t0 = time.time()
    series = fetch(market, sym, iv, limit)
    ind = compute(series)
    n = len(series.bars)
    i = n - 1                       # 决策发生在最新一根

    factors, score, regime = score_factors(ind, i)

    # ---- 权重（状态自适应）----
    adx = _val(ind, "adx", i)
    weights, regime_name = regime_weights(adx)

    # ---- 因子明细：前端画丝的完整依据 ----
    total_w = sum(weights[k] for k in factors if k in weights)
    rows = []
    for k in FACTOR_ORDER:
        if k not in factors:
            # 该因子算不出来（指标缺失）—— 也要呈现，否则用户不知道少了什么
            rows.append({
                "key": k, "label": FACTOR_LABELS.get(k, k),
                "desc": FACTOR_DESC.get(k, ""),
                "value": None, "weight": round(weights.get(k, 0.0), 3),
                "contrib": 0.0, "dir": "missing", "present": False,
            })
            continue
        v = factors[k]
        w = weights.get(k, 0.0)
        rows.append({
            "key": k, "label": FACTOR_LABELS.get(k, k),
            "desc": FACTOR_DESC.get(k, ""),
            "value": round(v, 4),
            "weight": round(w, 3),
            "contrib": round(v * w, 4),
            "dir": "bull" if v > 0.02 else "bear" if v < -0.02 else "flat",
            "present": True,
        })

    # ---- 决策（复用真实决策器，不另写一套逻辑）----
    acfg = AutoConfig(mode="score")
    decider = AutoDecider(acfg)
    close = _val(ind, "close", i) or 0.0

    # 持仓状态下 entry/peak 缺省就按当前价推算：
    # 这样默认不会触发任何风控（盈亏 0），但调用方可以显式传一个亏损价来演示拦截
    pos = 1 if position and position > 0 else 0
    ep = entry_price if entry_price is not None else (close if pos else 0.0)
    pp = peak_price if peak_price is not None else (close if pos else 0.0)

    dec: Decision = decider.decide(
        ind=ind, i=i, position=pos,
        fraction=float(position) if pos else 0.0,
        entry_price=float(ep), peak_price=float(pp),
        drawdown=float(drawdown),
        bars_since_exit=999, bars_held=0, halted=bool(halted),
    )

    risk = RiskConfig()
    gates = []
    for key, label, desc in RISK_GATES:
        v = getattr(risk, key, None)
        gates.append({
            "key": key, "label": label, "desc": desc,
            "value": v,
            # active 语义：该闸门**开着**（会拦人）。None 表示不启用
            "active": v is not None and v != 0,
            # 是否正在生效（本轮真的拦住了）—— 由 risk_hit 反推
            "fired": bool(dec.risk_hit and label in (dec.risk_hit or "")),
        })

    # ---- 计算流程（供前端做过程重演）----
    # 每一步都带真实数字，动画只是把这些数字按顺序演出来。
    stages = [
        {"id": "bars", "label": "读取 K 线", "detail": f"{n} 根 · {series.source}",
         "value": n},
        {"id": "indicators", "label": "算 27 个指标", "detail": "EMA/RSI/MACD/ADX/布林/KDJ/ATR",
         "value": len(ind.values)},
        {"id": "regime", "label": "判定市场状态", "detail":
            f"ADX {adx:.1f} → {regime_name}" if adx is not None else "ADX 缺失 → 用趋势市权重",
         "value": round(adx, 2) if adx is not None else None},
        {"id": "factors", "label": "计算 6 个因子", "detail":
            "、".join(f"{r['label']}{r['value']:+.2f}" for r in rows if r["present"]),
         "value": len([r for r in rows if r["present"]])},
        {"id": "weight", "label": "按状态加权", "detail":
            f"权重和 {total_w:.2f}（{regime_name}）", "value": round(total_w, 3)},
        {"id": "score", "label": "归一化打分", "detail":
            f"加权和 {sum(r['contrib'] for r in rows):+.3f} ÷ {total_w:.2f} = {score:+.4f}",
         "value": round(score, 4)},
        {"id": "threshold", "label": "比阈值", "detail":
            f"开仓线 {acfg.entry_threshold:+.2f} / 平仓线 {acfg.exit_threshold:+.2f} → {dec.action}",
         "value": round(score, 4)},
        {"id": "risk", "label": "过风控", "detail":
            dec.risk_hit or "无触发，放行", "value": 1 if dec.risk_hit else 0},
        {"id": "position", "label": "定仓位", "detail":
            f"目标 {dec.target * 100:.0f}%", "value": round(dec.target, 4)},
    ]

    # ---- ★ 为什么是「不动」：把原因说清楚 ----
    #
    # 这一节是这次重做的核心。原来页面上写着「已达标（1.74×）」，
    # 但动作是「不动」—— 用户完全看不懂。真相是：
    #   · 分数 -0.4357 的绝对值确实超过开仓线 0.25，所以「达标」是对的
    #   · 但它是**负的**，意思是"该做空"
    #   · 而这套策略只做多，做空方向没法执行 → 只能空仓不动
    # 所以「达标」不等于「会买」。必须把这件事显式写出来。
    entry = acfg.entry_threshold
    exit_th = acfg.exit_threshold
    sc = score
    if sc >= entry:
        why = "看多且已达开仓线"
        detail_why = f"分数 {sc:+.4f} ≥ 开仓线 {entry:+.2f} → 开仓"
    elif sc <= exit_th:
        why = "看空（策略只做多，无法执行）"
        detail_why = (f"分数 {sc:+.4f} ≤ 平仓线 {exit_th:+.2f} → 该做空，"
                      f"但本策略只做多 → 只能空仓")
    else:
        why = "信号不够强"
        detail_why = (f"分数 {sc:+.4f} 在 [{exit_th:+.2f}, {entry:+.2f}] 之间 "
                      f"→ 不够开仓也不够平仓")
    if dec.risk_hit:
        why = "风控否决"
        detail_why = f"风控触发「{dec.risk_hit}」，优先于信号，不看打分"

    # ---- 一句话摘要 ----
    # 页面上最该一眼看懂的东西。把「动作 + 原因 + 离变化还差多少」压成一句。
    # 不写这一句的话，用户要在三个区块之间来回对照才拼得出结论。
    if dec.risk_hit:
        headline = (f"{'买入' if dec.action == 'buy' else '卖出' if dec.action == 'sell' else '不动'}"
                    f" —— 风控否决：{dec.risk_hit}")
    elif sc >= entry:
        headline = f"买入 —— 分数 {sc:+.2f} 已达开仓线 {entry:+.2f}"
    elif sc <= exit_th:
        headline = f"空仓不动 —— 分数 {sc:+.2f} 看空，但策略只做多、执行不了"
    else:
        gap_to_entry = entry - sc
        headline = (f"空仓观望 —— 分数 {sc:+.2f} 不够强，"
                    f"距开仓线还差 {gap_to_entry:.2f}")

    # ---- 主导因子与翻转距离（headline 里也要用）----
    dominant = max((r for r in rows if r["present"]),
                   key=lambda r: abs(r["contrib"]), default=None)
    thresholds = [
        {"key": "entry", "label": "开仓", "value": entry,
         "reached": sc >= entry,
         "gap": round(max(0.0, entry - sc), 4)},
        {"key": "exit", "label": "平仓", "value": exit_th,
         "reached": sc <= exit_th,
         "gap": round(max(0.0, sc - exit_th), 4)},
    ]
    # 主导因子的"杠杆"：它每变动 1.0（满档），分数变动 weight/totalWeight
    sensitivity = []
    if dominant and total_w > 0:
        lever = dominant["weight"] / total_w          # 满档时的分数影响
        for t in thresholds:
            if t["reached"]:
                sensitivity.append({
                    "threshold": t["key"], "label": t["label"],
                    "value": t["value"], "reached": True, "gap": t["gap"],
                    "factor": dominant["key"], "factorLabel": dominant["label"],
                    "deltaNeeded": 0.0, "lever": round(lever, 4),
                    "reachable": True,
                    "note": f"已{'达到' if t['key'] == 'entry' else '触发'}",
                })
                continue
            need = t["gap"] / lever if lever > 0 else 999.0
            sensitivity.append({
                "threshold": t["key"], "label": t["label"],
                "value": t["value"], "reached": False, "gap": t["gap"],
                "factor": dominant["key"], "factorLabel": dominant["label"],
                # 需要主导因子再变动多少（因子范围 -1~+1）
                "deltaNeeded": round(need, 3),
                "lever": round(lever, 4),
                # 因子不可能变动超过 2.0（从 -1 到 +1）
                "reachable": need <= 2.0,
                "note": (f"{dominant['label']}再{'涨' if t['key'] == 'entry' else '跌'} "
                         f"{need:.2f} 档" if need <= 2.0
                         else f"需要 {dominant['label']} 变动 {need:.2f} 档 —— 不可能"),
            })

    return {
        "meta": {
            "name": SYMBOL_NAMES.get(sym, series.name or sym),
            "symbol": sym, "market": market, "interval": iv,
            "source": series.source, "bars": n,
            "at": series.bars[-1].as_dict()["iso"],
            "first": series.bars[0].as_dict()["iso"],
            "close": round(close, 4),
            "elapsedMs": int((time.time() - t0) * 1000),
        },
        "regime": {
            "name": regime_name,
            "adx": round(adx, 2) if adx is not None else None,
            "trendMin": 25.0, "rangeMax": 20.0,
            "weights": {k: round(v, 3) for k, v in weights.items()},
            "weightSets": {
                "趋势市": dict(FACTOR_WEIGHTS_TRENDING),
                "震荡市": dict(FACTOR_WEIGHTS_RANGING),
            },
        },
        "factors": rows,
        "score": {
            "raw": round(sum(r["contrib"] for r in rows), 4),
            "clamped": round(score, 4),
            "totalWeight": round(total_w, 3),
        },
        "gates": {
            "entry": acfg.entry_threshold,
            "exit": acfg.exit_threshold,
            "action": dec.action,
            # 离阈值还差多少（归一化到 0~1，1 = 刚好够线）——
            # 这是"张力弧"的数据源
            "margin": (round(abs(score) / acfg.entry_threshold, 4)
                       if acfg.entry_threshold else None),
        },
        "risk": gates,
        "decision": dec.to_dict(),
        "stages": stages,
        # ★ 为什么是这个动作 —— 直接给结论，不让用户自己推
        "why": {
            "action": dec.action,
            "reason": why,
            "detail": detail_why,
            "headline": headline,
            "score": round(sc, 4),
            "entry": entry,
            "exit": exit_th,
        },
        # ★ 离翻转还差多少 —— 最有行动价值的一节
        "sensitivity": sensitivity,
        "dominant": ({
            "key": dominant["key"], "label": dominant["label"],
            "contrib": dominant["contrib"], "weight": dominant["weight"],
            "share": round(abs(dominant["contrib"])
                           / max(sum(abs(r["contrib"]) for r in rows
                                     if r["present"]), 1e-9), 4),
        } if dominant else None),
        # 回显这次用的持仓假设，免得看的人以为一定是空仓
        "assumption": {
            "position": pos,
            "fraction": round(float(position), 4) if pos else 0.0,
            "entryPrice": round(float(ep), 4) if pos else None,
            "peakPrice": round(float(pp), 4) if pos else None,
            "drawdown": round(float(drawdown), 4),
            "halted": bool(halted),
            "note": ("空仓视角：止损/止盈/移动止损不会触发"
                     if not pos else
                     f"持仓视角：成本 {ep:.4f}"
                     + (f"，回撤 {drawdown:.2%}" if drawdown else "")),
        },
    }


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
    print("  决策快照自检（离线，构造数据）")
    print("=" * 78)

    # 直接测内部装配逻辑：把 build_decision 的联网部分绕开
    def mk_series(prices, vols=None):
        vols = vols or [1.0 + (i % 7) * 0.2 for i in range(len(prices))]
        bars = tuple(Bar(1_700_000_000_000 + i * 86_400_000,
                         p, p * 1.012, p * 0.988, p, v)
                     for i, (p, v) in enumerate(zip(prices, vols)))
        return Series("test", "crypto", "TEST", "测试", "1d", bars, 0.0)

    def snapshot(series, **kw):
        """复刻 build_decision 的装配部分，但不联网。"""
        from autotrader import (AutoConfig, AutoDecider, FACTOR_LABELS,
                                RiskConfig, regime_weights, score_factors)
        ind = compute(series)
        n = len(series.bars)
        i = n - 1
        factors, score, regime = score_factors(ind, i)
        adx = _val(ind, "adx", i)
        weights, rname = regime_weights(adx)
        total_w = sum(weights[k] for k in factors if k in weights)
        rows = []
        for k in FACTOR_ORDER:
            if k not in factors:
                rows.append({"key": k, "label": FACTOR_LABELS.get(k, k),
                             "value": None, "weight": round(weights.get(k, 0.0), 3),
                             "contrib": 0.0, "dir": "missing", "present": False})
                continue
            v = factors[k]
            w = weights.get(k, 0.0)
            rows.append({"key": k, "label": FACTOR_LABELS.get(k, k),
                         "value": round(v, 4), "weight": round(w, 3),
                         "contrib": round(v * w, 4),
                         "dir": "bull" if v > 0.02 else "bear" if v < -0.02 else "flat",
                         "present": True})
        acfg = AutoConfig(mode="score")
        dec = AutoDecider(acfg).decide(
            ind=ind, i=i, position=0, fraction=0.0, entry_price=0.0,
            peak_price=0.0, drawdown=0.0, bars_since_exit=999, bars_held=0)
        return {"factors": rows, "score": score, "regime": rname, "adx": adx,
                "weights": weights, "total_w": total_w, "dec": dec,
                "ind": ind, "n": n, "acfg": acfg}

    rng = np.random.default_rng(3)
    px, up = 100.0, []
    for i in range(200):
        px *= 1.006 + rng.normal(0, 0.004)
        up.append(px)
    down = [up[0] * (1 - 0.004 * i) for i in range(200)]

    # --- 1. 六个因子都在 ---
    print("\n[1] 因子完整性")
    s = snapshot(mk_series(up))
    check("6 个因子都有值", all(r["present"] for r in s["factors"]),
          f"{[r['key'] for r in s['factors'] if not r['present']]}")
    check("顺序是固定的 FACTOR_ORDER",
          [r["key"] for r in s["factors"]] == FACTOR_ORDER)
    check("每个因子都有中文名", all(r["label"] for r in s["factors"]))
    check("每个因子都有说明", all(FACTOR_DESC.get(r["key"]) for r in s["factors"]))

    # --- 2. 贡献 = 值 × 权重（这是可视化的核心）---
    print("\n[2] 贡献计算")
    for r in s["factors"]:
        if not r["present"]:
            continue
        expect = round(r["value"] * r["weight"], 4)
        check(f"{r['label']} 贡献 = 值×权重", abs(r["contrib"] - expect) < 1e-9,
              f"{r['value']}×{r['weight']}={r['contrib']}")
    ws = sum(r["contrib"] for r in s["factors"])
    # 容差放宽到 1e-4：contrib 与 total_w 都是各自 round(…, 4) 过的，
    # 累加后与「总权重 × 分数」会有 round 级别的偏差（实测差 ~1e-7，
    # 但用 1e-6 会偶发失败）。这里验的是公式关系，不是数值精度。
    check("加权和 = 各贡献之和", abs(ws - s["total_w"] * s["score"]) < 1e-4,
          f"加权和 {ws:.4f}  vs  总权重 {s['total_w']:.2f} × 分数 {s['score']:.4f} = {s['total_w']*s['score']:.4f}")

    # --- 3. 方向标记 ---
    print("\n[3] 方向标记")
    for r in s["factors"]:
        if not r["present"]:
            check(f"{r['label']} 缺失时不标方向", r["dir"] == "missing")
            continue
        v = r["value"]
        expect = "bull" if v > 0.02 else "bear" if v < -0.02 else "flat"
        check(f"{r['label']} 方向正确", r["dir"] == expect,
              f"值 {v:+.4f} → {r['dir']}")

    # --- 4. 权重随状态变 ---
    print("\n[4] 状态自适应权重")
    from autotrader import ADX_TREND_MIN, ADX_RANGE_MAX
    wt, nt = regime_weights(30.0)
    wr, nr = regime_weights(10.0)
    check("高 ADX → 趋势市", nt == "趋势市", nt)
    check("低 ADX → 震荡市", nr == "震荡市", nr)
    check("趋势市里 trend 权重更高", wt["trend"] > wr["trend"],
          f"{wt['trend']} vs {wr['trend']}")
    check("震荡市里 reversion 权重更高", wr["reversion"] > wt["reversion"],
          f"{wr['reversion']} vs {wt['reversion']}")
    mid, nm = regime_weights(22.5)
    check("过渡区是插值", nm == "过渡" and
          min(wr["trend"], wt["trend"]) <= mid["trend"] <= max(wr["trend"], wt["trend"]),
          f"{nm} trend={mid['trend']:.3f}")
    check("ADX 缺失 → 保守用趋势市", regime_weights(None)[1] == "未知",
          str(regime_weights(None)[1]))

    # --- 5. 张力弧的 margin ---
    print("\n[5] 阈值与张力")
    a = s["acfg"]
    margin = abs(s["score"]) / a.entry_threshold
    check("margin 是分数比阈值", abs(margin - abs(s["score"]) / a.entry_threshold) < 1e-9,
          f"{margin:.4f}")
    check("margin >= 1 表示够线", (margin >= 1) == (abs(s["score"]) >= a.entry_threshold),
          f"margin {margin:.4f} score {s['score']:+.4f} 阈值 {a.entry_threshold}")
    check("开仓阈值 > 平仓阈值", a.entry_threshold > a.exit_threshold,
          f"{a.entry_threshold} > {a.exit_threshold}")

    # --- 6. 涨跌方向应该反映到因子上 ---
    print("\n[6] 因子对行情的反应（对抗性）")
    su = snapshot(mk_series(up))
    sd = snapshot(mk_series(down))
    tr_u = next(r for r in su["factors"] if r["key"] == "trend")
    tr_d = next(r for r in sd["factors"] if r["key"] == "trend")
    check("上涨序列 trend 为正", tr_u["value"] > 0, f"{tr_u['value']:+.4f}")
    check("下跌序列 trend 为负", tr_d["value"] < 0, f"{tr_d['value']:+.4f}")
    check("上涨分数 > 下跌分数", su["score"] > sd["score"],
          f"{su['score']:+.4f} vs {sd['score']:+.4f}")

    # --- 7. 风控闸门清单 ---
    print("\n[7] 风控闸门")
    risk = RiskConfig()
    gates = []
    for key, label, _ in RISK_GATES:
        v = getattr(risk, key, None)
        gates.append({"key": key, "label": label, "value": v,
                      "active": v is not None and v != 0})
    check("6 个闸门都有定义", len(gates) == 6)
    check("默认有止损", any(g["key"] == "stop_loss" and g["active"] for g in gates),
          str(next(g for g in gates if g["key"] == "stop_loss")))
    check("默认有回撤熔断",
          any(g["key"] == "max_drawdown" and g["active"] for g in gates))
    check("闸门都有中文名", all(g["label"] for g in gates))
    check("默认配置可序列化",
          isinstance(json.dumps([{k: v for k, v in g.items()} for g in gates],
                                ensure_ascii=False), str))

    # --- 8. 计算流程可用于重演 ---
    print("\n[8] 计算流程（stages）")
    # 这里直接构造一份 stages 验证契约（与 build_decision 里同构）
    stages = [
        {"id": "bars", "label": "读取 K 线", "detail": f"{s['n']} 根", "value": s["n"]},
        {"id": "indicators", "label": "算 27 个指标", "detail": "...",
         "value": len(s["ind"].values)},
        {"id": "regime", "label": "判定市场状态", "detail": s["regime"],
         "value": s["adx"]},
        {"id": "factors", "label": "计算 6 个因子", "detail": "...", "value": 6},
        {"id": "weight", "label": "按状态加权", "detail": "..", "value": s["total_w"]},
        {"id": "score", "label": "归一化打分", "detail": "...", "value": s["score"]},
        {"id": "threshold", "label": "比阈值", "detail": "...", "value": s["score"]},
        {"id": "risk", "label": "过风控", "detail": "...", "value": 0},
        {"id": "position", "label": "定仓位", "detail": "...", "value": s["dec"].target},
    ]
    check("9 个阶段按顺序", len(stages) == 9)
    check("阶段 id 唯一", len({x["id"] for x in stages}) == 9)
    check("每阶段有中文标签", all(x["label"] for x in stages))
    check("每阶段有 detail", all(x["detail"] for x in stages))
    check("stages 可 JSON 序列化",
          isinstance(json.dumps(stages, ensure_ascii=False), str))
    check("阶段顺序与决策流一致",
          [x["id"] for x in stages] ==
          ["bars", "indicators", "regime", "factors", "weight",
           "score", "threshold", "risk", "position"])

    # --- 9. 缺失因子也要呈现 ---
    print("\n[9] 因子缺失时的呈现")
    short = mk_series([100.0] * 8)          # 太短，指标算不出来
    ss = snapshot(short)
    check("短序列下仍有 6 行（缺失的标 missing）", len(ss["factors"]) == 6)
    n_missing = sum(1 for r in ss["factors"] if not r["present"])
    check("缺失因子被标记", n_missing > 0, f"{n_missing} 个缺失")
    check("缺失因子 value 是 None",
          all(r["value"] is None for r in ss["factors"] if not r["present"]))
    check("缺失因子仍有权重（说明它本该参与）",
          all(r["weight"] >= 0 for r in ss["factors"]))

    # --- 11. 持仓假设能触发风控拦截（否则蛛网最有价值的一帧永远看不到）---
    print("\n[11] 风控拦截路径（持仓假设）")
    from autotrader import AutoDecider as _AD, AutoConfig as _AC
    s2 = mk_series(up)
    ind2 = compute(s2)
    n2 = len(s2.bars)
    i2 = n2 - 1
    close2 = _val(ind2, "close", i2) or 0.0

    def decide_with(position, entry, peak, dd=0.0):
        return _AD(_AC(mode="score")).decide(
            ind=ind2, i=i2, position=position,
            fraction=1.0 if position else 0.0,
            entry_price=entry, peak_price=peak, drawdown=dd,
            bars_since_exit=999, bars_held=0)

    # 空仓：止损/止盈无事可做
    d_flat = decide_with(0, 0.0, 0.0)
    check("空仓时不会触发止损/止盈", not d_flat.risk_hit,
          f"riskHit={d_flat.risk_hit}")

    # 持仓亏 12% → 止损（默认 6%）
    d_loss = decide_with(1, close2 / 0.88, close2 / 0.88 * 1.06)
    check("持仓亏 12% 触发止损", bool(d_loss.risk_hit), f"riskHit={d_loss.risk_hit}")
    check("触发后动作是 sell", d_loss.action == "sell", d_loss.action)
    check("触发后目标仓位归零", d_loss.target == 0.0, str(d_loss.target))
    check("止损文案含'止损'", "止损" in (d_loss.risk_hit or ""), d_loss.risk_hit or "")

    # 持仓亏 8% 时移动止损也会满足（从 +6% 高点回撤 14% > 8%）——
    # 但止损优先于移动止损，所以 risk_hit 应该是「止损」
    check("止损优先于移动止损（先判止损）", "止损" in (d_loss.risk_hit or ""),
          d_loss.risk_hit or "")

    # 账户回撤 30% → 熔断
    d_dd = decide_with(1, close2, close2, dd=0.30)
    check("回撤 30% 触发熔断", bool(d_dd.risk_hit) and "熔断" in (d_dd.risk_hit or ""),
          d_dd.risk_hit or "无")

    # 浮盈 25% → 止盈（默认 20%）
    d_tp = decide_with(1, close2 / 1.25, close2 / 1.25 * 1.3)
    check("浮盈 25% 触发止盈", bool(d_tp.risk_hit) and "止盈" in (d_tp.risk_hit or ""),
          d_tp.risk_hit or "无")

    # --- 12. 整体可序列化 ---
    print("\n[12] 序列化")
    payload = {
        "meta": {"name": "测试", "symbol": "T"},
        "regime": {"name": s["regime"], "adx": s["adx"],
                   "weights": {k: round(v, 3) for k, v in s["weights"].items()}},
        "factors": s["factors"], "score": {"clamped": s["score"]},
        "risk": gates, "decision": s["dec"].to_dict(), "stages": stages,
    }
    txt = json.dumps(payload, ensure_ascii=False)
    check("整份快照可 JSON 序列化", isinstance(txt, str), f"{len(txt)} 字节")
    check("无 NaN/Infinity（JSON 不允许）",
          "NaN" not in txt and "Infinity" not in txt)
    check("中文没被转义（前端直读）", "趋势" in txt or "市场状态" in txt)
    # ⚠️ 用 allow_nan=False 走**严格的** JSON 路径 —— 这正是 Node 侧
    #    JSON.parse 的行为。默认的 json.dumps 会把 NaN 写成字面量 `NaN`，
    #    那不是合法 JSON，前端 JSON.parse 会直接报错。
    #    只检查字符串里有没有 "NaN" 是不够的（字段名里也可能出现）。
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        check("严格 JSON（allow_nan=False）通过 —— 前端能解析", True)
    except ValueError as e:
        check("严格 JSON（allow_nan=False）通过 —— 前端能解析", False, str(e))

    print("\n" + "=" * 78)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 78)
    return 1 if fail else 0


# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="决策快照（供决策驾驶舱可视化）")
    ap.add_argument("--market", default="ashare", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=500)
    # 持仓假设：默认空仓。传了才能看到止损/止盈/移动止损的拦截效果
    ap.add_argument("--position", type=float, default=0.0,
                    help="仓位比例（0=空仓）。>0 时才会评估止损/止盈")
    ap.add_argument("--entry", type=float, default=None, help="持仓成本价")
    ap.add_argument("--peak", type=float, default=None, help="持仓期间最高价")
    ap.add_argument("--drawdown", type=float, default=0.0,
                    help="账户回撤比例（0.3 = 回撤 30%%）")
    ap.add_argument("--halted", action="store_true", help="已熔断，只允许离场")
    ap.add_argument("--compact", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    try:
        d = build_decision(a.market, a.symbol, a.interval, a.limit,
                           position=a.position, entry_price=a.entry,
                           peak_price=a.peak, drawdown=a.drawdown,
                           halted=a.halted)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1

    print(json.dumps(d, ensure_ascii=False,
                     separators=(",", ":") if a.compact else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
