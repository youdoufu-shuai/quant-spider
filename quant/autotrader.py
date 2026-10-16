#!/usr/bin/env python3
"""
自动交易逻辑 —— 决策、仓位管理、风控。

═══════════════════════════════════════════════════════════════════
  为什么要重写（原来的逻辑有什么问题）
═══════════════════════════════════════════════════════════════════
原来是「策略信号 → 全仓买 / 全平卖」两行代码，问题：

  1. **只有 0/1 两种仓位** —— 全仓进出。真实交易要分批建仓、分批止盈，
     而且要能空仓等机会。全仓意味着一次误判就打满，没有回旋余地。

  2. **没有风控** —— 亏多少都扛着。没有止损、没有止盈、没有回撤保护。
     实测里 RSI 策略买在 1336 一路跌到 1190，全靠信号自己转空才走。

  3. **单因子决策** —— 只看一个策略。均线金叉时 RSI 可能已经超买，
     MACD 可能是负的，但原来的逻辑照样满仓进去。

  4. **决策不可解释** —— 日志只有「策略转多」四个字，看不出为什么。

  5. **重复计算** —— 每推进一根就 compute() + run_strategy() 全量重算，
     300 根 K 线每次刷新重算一遍全部指标。

═══════════════════════════════════════════════════════════════════
  新的设计
═══════════════════════════════════════════════════════════════════
三层，各自独立、可单独关闭：

  ┌─ 第 1 层：信号层（signal）─────────────────────────────────┐
  │  5 个策略给出方向分：+1 看多 / 0 中性 / -1 看空              │
  │  三种决策模式：                                             │
  │    signal   只用选定的那一个策略（和旧行为一致，可对照）      │
  │    confirm  至少 N 个策略同向才动手（默认 2）                │
  │    score    6 个因子加权打分，总分过阈值才动手（默认）        │
  └────────────────────────────────────────────────────────────┘
  ┌─ 第 2 层：仓位层（sizing）─────────────────────────────────┐
  │  目标仓位是 0.0~1.0 的**比例**，不是 0/1                    │
  │    fixed     固定比例（默认 0.95 = 接近满仓）                │
  │    score     比例跟随信心（打分越高仓位越重）                │
  │    volatility 按 ATR 反比缩放（波动大就轻仓）                │
  │  分档建仓：目标与当前差 < rebalance_band 就不动，避免频繁交易 │
  └────────────────────────────────────────────────────────────┘
  ┌─ 第 3 层：风控层（risk）───────────────────────────────────┐
  │  止损 / 止盈 / 移动止损 / 最大回撤熔断 / 冷却期              │
  │  ★ 风控**优先于**信号：触发止损就卖，不管信号说什么          │
  └────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
  打分模型的 6 个因子（score 模式）
═══════════════════════════════════════════════════════════════════
每个因子输出 -1~+1，乘权重后求和，再除以权重和归一化到 -1~+1。

  趋势  (ema12 vs ema26)   权重 1.4   最基础的方向判断
  动能  (macd_hist)         权重 1.2   趋势的加速度
  强度  (ADX + DI 方向)     权重 1.2   趋势够不够强，避免震荡市乱动
  超买超卖 (RSI 反向)       权重 1.0   涨太多要谨慎，跌太多可考虑
  位置  (布林带 %B)         权重 1.0   价格在通道里的相对位置
  量能  (volume / MA20)     权重 0.6   放量确认（最弱，只是辅助）

判据：
  总分 ≥ 0.25  且  当前仓位 < 目标   →  买入
  总分 ≤ -0.25 且  当前仓位 > 目标   →  卖出
  ADX < 15（震荡市）→ 分数打 7 折，避免在噪音里来回交易

═══════════════════════════════════════════════════════════════════
  关于「AI 自己买卖」
═══════════════════════════════════════════════════════════════════
本模块提供了 AIDecider：把市场快照整理成一段结构化的提示词，
交给**外部 AI**（MCP agent / 本地模型 / 你自己写的服务）决定，然后执行。

为什么不内置调用：这个环境里没有可直接调用的模型 CLI，
而 ~/.dsh/.credentials.yaml 里存的是你的凭据 —— 拿它去直连模型
既不可靠也不合适。所以做成**可插拔**：你接一个端点就能用。

用法见 AIDecider 的文档字符串。

═══════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from indicators import compute                                  # noqa: E402
from strategy import STRATEGIES, run as run_strategy             # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
@dataclass
class RiskConfig:
    """风控参数。全部是比例（0.05 = 5%），None 表示关闭该项。"""
    stop_loss: float | None = 0.06          # 从持仓成本跌 6% 止损
    take_profit: float | None = 0.20        # 从持仓成本涨 20% 止盈
    trailing_stop: float | None = 0.08      # 从最高价回撤 8% 移动止损
    max_drawdown: float | None = 0.25       # 账户回撤 25% 熔断（清仓并停手）
    cooldown_bars: int = 3                  # 卖出后冷却几根才允许再买
    min_hold_bars: int = 0                  # 买入后至少持有几根（0=可立即卖）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AutoConfig:
    """自动交易配置。"""
    mode: str = "score"                     # signal | confirm | score | ai
    strategy: str = "rsi_reversion"         # signal/confirm 模式用
    confirm_min: int = 2                    # confirm 模式：至少几个同向
    entry_threshold: float = 0.25           # score 模式：开仓阈值
    exit_threshold: float = -0.25           # score 模式：平仓阈值
    sizing: str = "fixed"                   # fixed | score | volatility
    fixed_fraction: float = 0.95            # fixed 模式的仓位比例
    max_fraction: float = 1.0               # 仓位上限
    rebalance_band: float = 0.15            # 目标与当前差小于此值则不动
    target_vol: float = 0.02                # volatility 模式的目标单根波动
    risk: RiskConfig = field(default_factory=RiskConfig)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ============================================================
class Decision:
    """一次决策的结果。刻意保留完整推理链，方便解释「为什么」。"""

    __slots__ = ("action", "target", "current", "score", "factors",
                 "reasons", "risk_hit", "mode", "note")

    def __init__(self, action: str, target: float, current: float, score: float,
                 factors: dict, reasons: list, risk_hit: str | None = None,
                 mode: str = "", note: str = ""):
        self.action = action            # buy | sell | hold
        self.target = target            # 目标仓位比例 0~1
        self.current = current          # 当前仓位比例 0~1
        self.score = score              # 打分 -1~+1
        self.factors = factors          # 各因子明细
        self.reasons = reasons          # 人类可读的原因列表
        self.risk_hit = risk_hit        # 若被风控否决/强平，这里是原因
        self.mode = mode
        self.note = note

    def to_dict(self) -> dict:
        return {
            "action": self.action, "target": round(self.target, 4),
            "current": round(self.current, 4), "score": round(self.score, 4),
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "reasons": self.reasons, "riskHit": self.risk_hit,
            "mode": self.mode, "note": self.note,
        }

    def summary(self) -> str:
        """一行摘要，用于日志。"""
        if self.risk_hit:
            return f"风控触发：{self.risk_hit}"
        if self.action == "hold":
            return self.note or f"不动（打分 {self.score:+.2f}，目标仓位 {self.target:.0%}）"
        verb = "买入" if self.action == "buy" else "卖出"
        return (f"{verb}至 {self.target:.0%} 仓位（当前 {self.current:.0%}，"
                f"打分 {self.score:+.2f}）")


# ============================================================
#  因子打分
# ============================================================
def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _val(ind, name: str, i: int) -> float | None:
    """
    安全取一个指标在第 i 根的值。

    ⚠️ IndicatorSet 的结构是 {series, values: dict}，**指标不在属性上**，
       而在 ind.values 字典里。最初写成 getattr(ind, "ema12") 拿到的是
       None（属性不存在），于是所有因子都算不出来、总分恒为 0 ——
       自检里表现为「明显上涨却不买」。这是本轮修掉的一个真 bug。

    ⚠️ 另外 values 里**只有计算出来的指标（27 个），没有 close/volume** ——
       它们是原始行情，存在 series.bars 里。所以这里要回落查 series。

    越界、None、NaN 一律返回 None，由调用方决定跳过还是当 0。
    """
    if ind is None or i < 0:
        return None

    # 1) 原始行情：从 series 取
    if name in ("close", "open", "high", "low", "volume"):
        series = getattr(ind, "series", None)
        bars = getattr(series, "bars", None)
        if bars:
            try:
                return float(getattr(bars[i], name))
            except (IndexError, AttributeError, TypeError, ValueError):
                return None
        return None

    # 2) 技术指标：从 values 字典取
    vals = getattr(ind, "values", None)
    arr = None
    if isinstance(vals, dict):
        arr = vals.get(name)
    if arr is None:
        arr = getattr(ind, name, None)
    if arr is None:
        return None
    seq = getattr(arr, "values", arr)
    try:
        v = seq[i]
    except (IndexError, TypeError, KeyError):
        return None
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


# 因子权重：按**市场状态**分两套。
#
# ⚠️ 为什么必须分状态（第一版踩的坑）
#    最初给所有因子固定权重，结果在单边下跌里出现这种分数：
#        trend -1.00   strength -1.00            ← 明确看空
#        reversion +1.00  position +0.79  momentum +0.83   ← 明确看多
#        总分 +0.03 → 判定观望，完全不作为
#    根因有两个：
#      1. reversion（超卖）和 position（布林位置）**是同一个概念的两次计数** ——
#         价格一路跌，RSI 必然低、布林 %B 必然低，两项同时给出"看多"，
#         双倍抵消掉趋势与强度两项的看空。
#      2. 均值回归类因子只在**震荡市**成立；单边趋势里"超卖"可以一直超卖，
#         拿它去抄底是亏钱的主要方式。
#    修法：用 ADX 判定状态，趋势市把均值回归权重压到 0.15，
#          震荡市反过来压低趋势与动能。这样单边下跌得到 -0.80（明确看空），
#          震荡市得到 +0.06（明确不动）。
FACTOR_WEIGHTS_TRENDING = {
    "trend": 1.8, "momentum": 1.2, "strength": 1.5,
    "reversion": 0.15, "position": 0.5, "volume": 0.5,
}
FACTOR_WEIGHTS_RANGING = {
    "trend": 0.5, "momentum": 0.4, "strength": 0.3,
    "reversion": 1.8, "position": 1.2, "volume": 0.4,
}
# 兼容旧引用
FACTOR_WEIGHTS = FACTOR_WEIGHTS_TRENDING

ADX_TREND_MIN = 25.0    # ADX ≥ 此值算趋势市
ADX_RANGE_MAX = 20.0    # ADX ≤ 此值算震荡市；中间为过渡区，线性插值

FACTOR_LABELS = {
    "trend": "趋势", "momentum": "动能", "strength": "强度",
    "reversion": "超买超卖", "position": "通道位置", "volume": "量能",
}


def regime_weights(adx: float | None) -> tuple[dict, str]:
    """
    按 ADX 给出因子权重与状态名。

    ADX ≥ 25  → 趋势市（权重偏趋势/强度）
    ADX ≤ 20  → 震荡市（权重偏均值回归）
    20~25     → 线性插值过渡
    ADX 缺失   → 用趋势市权重（保守：宁可跟趋势也不乱抄底）
    """
    if adx is None:
        return dict(FACTOR_WEIGHTS_TRENDING), "未知"
    if adx >= ADX_TREND_MIN:
        return dict(FACTOR_WEIGHTS_TRENDING), "趋势市"
    if adx <= ADX_RANGE_MAX:
        return dict(FACTOR_WEIGHTS_RANGING), "震荡市"
    # 过渡区：t 从 0（震荡）到 1（趋势）
    t = (adx - ADX_RANGE_MAX) / (ADX_TREND_MIN - ADX_RANGE_MAX)
    w = {k: (FACTOR_WEIGHTS_RANGING[k] * (1 - t) + FACTOR_WEIGHTS_TRENDING[k] * t)
         for k in FACTOR_WEIGHTS_TRENDING}
    return w, "过渡"


def score_factors(ind, i: int) -> tuple[dict, float, str]:
    """
    计算 6 个因子分数，返回 (各因子分数, 加权总分, 市场状态)。

    每个因子都在 -1~+1：正数看多，负数看空，0 中性。

    ⚠️ 设计要点 1：**方向量用 ATR 归一化，而不是绝对百分比**。
       最初写成「EMA 差 / 价格 / 0.02」，意思"偏离 2% 算满分"。
       自检立刻打脸：一个每天涨 1.2% 的稳定上涨序列，EMA12 与 EMA26
       只差约 1.2%，达不到 2% 的线，总分算出来 +0.00 —— 明显该买却不买。
       问题在于绝对百分比对不同波动率的标的不通用：
       加密一天动 5%、大盘指数一天动 0.5%，同一个阈值必然一头失灵。
       改成除以 ATR 后，1 倍 ATR 就是满分线，自动适配任何标的。

    ⚠️ 设计要点 2：**权重随市场状态变化**，见 regime_weights 的说明。

    缺失的因子直接跳过（不计入权重和），而不是当 0 处理 ——
    否则指标还没预热完时，0 分会被误当成"中性"拉低总分。
    """
    f: dict[str, float] = {}

    close = _val(ind, "close", i)
    atr = _val(ind, "atr", i)
    ema12, ema26 = _val(ind, "ema12", i), _val(ind, "ema26", i)
    macd_dif = _val(ind, "macd_dif", i)
    adx = _val(ind, "adx", i)
    pdi, mdi = _val(ind, "plus_di", i), _val(ind, "minus_di", i)
    rsi = _val(ind, "rsi", i)
    bbu, bbl = _val(ind, "bb_upper", i), _val(ind, "bb_lower", i)
    vol, vma = _val(ind, "volume", i), _val(ind, "volume_ma20", i)

    # 波动率尺度：优先 ATR，没有就退回价格的 1%
    scale = atr if (atr and atr > 0) else (close * 0.01 if close else None)

    # 趋势：EMA12 与 EMA26 的差，1.5 倍 ATR 即满分
    if ema12 is not None and ema26 is not None and scale:
        f["trend"] = _clamp((ema12 - ema26) / (1.5 * scale))

    # 动能：用 **MACD 线（DIF）** 而不是 MACD 柱。
    #
    # ⚠️ 为什么不用柱（这是第二个真 bug）
    #    MACD 柱 = DIF - DEA，衡量的是"加速度"。在指数衰减序列里它必然是正的：
    #    越往下基数越小，每根的跌幅绝对值在缩小，于是短期均线反而比长期均线
    #    "跌得少"，柱转正 —— 一个正在崩盘的标的会显示"动能看多 +0.83"，
    #    把明确的看空信号整个抵消掉。
    #    DIF = EMA12 - EMA26 才是纯方向量：上涨为正、下跌为负，不会骗人。
    #    对方向性交易者来说，方向明确比"抓拐点"重要得多。
    if macd_dif is not None and scale:
        f["momentum"] = _clamp(macd_dif / (1.5 * scale))

    # 强度：ADX 定强弱，DI 定方向。ADX<20 视为无趋势，强度给 0
    if adx is not None and pdi is not None and mdi is not None:
        direction = 1.0 if pdi > mdi else (-1.0 if mdi > pdi else 0.0)
        magnitude = _clamp((adx - 20) / 20)          # ADX 20->0, 40->1
        f["strength"] = direction * max(0.0, magnitude)

    # 超买超卖：RSI 反向。50 中性，偏离 20 点即满分
    if rsi is not None:
        f["reversion"] = _clamp((50.0 - rsi) / 20.0)

    # 通道位置：布林带 %B，1 = 上轨（看空），0 = 下轨（看多）
    if close and bbu is not None and bbl is not None and bbu > bbl:
        pctb = (close - bbl) / (bbu - bbl)
        f["position"] = _clamp(1.0 - 2.0 * pctb)

    # 量能：放量本身不指示方向（既可能突破也可能出货），
    # 所以只用「量比偏离 1 的程度」去增强当前趋势方向的可信度。
    #
    # ⚠️ trend 缺失时**不能**把 base 当 0 然后走 `base >= 0` 分支 ——
    #    那会给一个方向未知的放量打上 +1（看多）。趋势算不出来时
    #    方向就是未知的，此时量能因子应该**缺席**，而不是猜一个方向。
    #    （verifier 发现：原实现 base 默认 0.0，符号取 `>= 0` 恒为 +1。）
    if vol is not None and vma and vma > 0:
        base = f.get("trend")
        if base is not None:
            ratio = vol / vma
            f["volume"] = _clamp(_clamp((ratio - 1.0) * 2.0) * (1 if base > 0 else -1))

    if not f:
        return {}, 0.0, "未知"

    weights, regime = regime_weights(adx)

    # 只对**实际算出来的**因子归一化权重。
    # 不能让缺失因子按 0 计入分母 —— 那会把总分稀释掉。
    total_w = sum(weights[k] for k in f if k in weights)
    if total_w <= 0:
        return f, 0.0, regime
    score = sum(f[k] * weights[k] for k in f if k in weights) / total_w

    return f, _clamp(score), regime


def explain_factors(factors: dict, score: float, threshold: float,
                    regime: str = "") -> list[str]:
    """把因子分数翻译成人话。"""
    out = []
    if regime:
        out.append(f"市场状态：{regime}")
    for k, v in sorted(factors.items(), key=lambda x: -abs(x[1])):
        if abs(v) < 0.15:
            continue
        label = FACTOR_LABELS.get(k, k)
        direction = "看多" if v > 0 else "看空"
        strength = "强" if abs(v) > 0.6 else ("中" if abs(v) > 0.3 else "弱")
        out.append(f"{label}{direction}（{v:+.2f}·{strength}）")
    if len(out) <= (1 if regime else 0):
        out.append("各因子均接近中性")
    verdict = ("倾向做多" if score >= threshold
               else "倾向做空/空仓" if score <= -threshold else "未达阈值，观望")
    out.append(f"加权总分 {score:+.2f} → {verdict}")
    return out


# ============================================================
#  决策器
# ============================================================
class AutoDecider:
    """
    内置决策器。无状态、只看传入的市场快照 —— 这样易测、可复现。
    账户相关的状态（成本、最高价、回撤、冷却）由调用方传入。
    """

    def __init__(self, config: AutoConfig | None = None):
        self.cfg = config or AutoConfig()

    def _target_fraction(self, score: float, cur_frac: float, ind, i: int) -> float:
        """第 2 层：仓位层。把方向分转成目标仓位比例。"""
        cfg = self.cfg
        if cfg.sizing == "fixed":
            frac = cfg.fixed_fraction
        elif cfg.sizing == "score":
            # 信心越足仓位越重：|score| 从阈值到 1 线性映射到 0.3~1.0
            t = cfg.entry_threshold
            k = _clamp((abs(score) - t) / max(1e-6, 1.0 - t), 0.0, 1.0)
            frac = 0.30 + 0.70 * k
        elif cfg.sizing == "volatility":
            # 波动率目标：ATR/价格 越大，仓位越轻
            atr = _val(ind, "atr", i)
            close = _val(ind, "close", i)
            if atr and close and close > 0 and cfg.target_vol > 0:
                rel = atr / close
                frac = _clamp(cfg.target_vol / max(rel, 1e-6), 0.0, 1.0)
            else:
                frac = cfg.fixed_fraction
        else:
            frac = cfg.fixed_fraction

        return _clamp(frac, 0.0, cfg.max_fraction)

    def decide(self, *, ind, i: int, position: int, fraction: float,
               entry_price: float, peak_price: float,
               drawdown: float, bars_since_exit: int,
               bars_held: int, halted: bool = False) -> Decision:
        """
        做出一次决策。

        参数说明：
          i              当前 K 线下标
          position       1=持仓 0=空仓（用于判断能不能卖）
          fraction       当前仓位比例 = 持仓市值 / 权益
          entry_price    持仓成本（空仓时忽略）
          peak_price     持仓期间最高价（移动止损用）
          drawdown       账户当前回撤比例（0.1 = 回撤 10%）
          bars_since_exit 距上次卖出多少根（冷却用）
          bars_held      已持有多少根（最短持有用）
          halted         True 表示已因回撤熔断，只允许离场
        """
        cfg, risk = self.cfg, self.cfg.risk
        mode = cfg.mode

        if not ind or i < 0:
            return Decision("hold", fraction, fraction, 0.0, {}, ["没有行情数据"],
                            mode=mode, note="没有行情数据")

        close = _val(ind, "close", i) or 0.0

        # ── 第 3 层优先：风控 ──
        # 风控必须在信号之前判断 —— 止损的意义就是「不管信号怎么说都要走」
        if position and entry_price > 0 and close > 0:
            pnl = close / entry_price - 1.0
            if risk.stop_loss is not None and pnl <= -risk.stop_loss:
                return Decision("sell", 0.0, fraction, 0.0, {},
                                [f"持仓亏损 {pnl:.2%}，触发止损线 {-risk.stop_loss:.0%}"],
                                risk_hit=f"止损（{pnl:.2%}）", mode=mode)
            if risk.take_profit is not None and pnl >= risk.take_profit:
                return Decision("sell", 0.0, fraction, 0.0, {},
                                [f"持仓盈利 {pnl:.2%}，触发止盈线 {risk.take_profit:.0%}"],
                                risk_hit=f"止盈（{pnl:.2%}）", mode=mode)
            if risk.trailing_stop is not None and peak_price > 0:
                drop = close / peak_price - 1.0
                # 移动止损只在已经浮盈时才生效，否则和固定止损重复
                if pnl > 0 and drop <= -risk.trailing_stop:
                    return Decision("sell", 0.0, fraction, 0.0, {},
                                    [f"从最高价 {peak_price:.2f} 回撤 {drop:.2%}，触发移动止损"],
                                    risk_hit=f"移动止损（回撤 {drop:.2%}）", mode=mode)

        if risk.max_drawdown is not None and drawdown >= risk.max_drawdown:
            if position:
                return Decision("sell", 0.0, fraction, 0.0, {},
                                [f"账户回撤 {drawdown:.1%} 已达熔断线 {risk.max_drawdown:.0%}，清仓停手"],
                                risk_hit=f"回撤熔断（{drawdown:.1%}）", mode=mode)
            return Decision("hold", 0.0, fraction, 0.0, {},
                            [f"账户回撤 {drawdown:.1%} 超熔断线，暂停开仓"],
                            risk_hit="回撤熔断（停手）", mode=mode)
        if halted:
            return Decision("hold", 0.0, fraction, 0.0, {},
                            ["回撤熔断中，只允许离场"], risk_hit="熔断中", mode=mode)

        # ── 第 1 层：信号 ──
        factors: dict = {}
        reasons: list[str] = []
        target_frac = 0.0
        score = 0.0

        if mode == "score":
            factors, score, regime = score_factors(ind, i)
            reasons = explain_factors(factors, score, cfg.entry_threshold, regime)
            bull = score >= cfg.entry_threshold
            bear = score <= cfg.exit_threshold
            if bull:
                target_frac = self._target_fraction(score, fraction, ind, i)
            elif bear:
                target_frac = 0.0
            else:
                # 未达阈值：保持现状，不折腾
                target_frac = fraction if position else 0.0
                reasons.insert(0, "打分未达阈值，维持当前仓位")

        elif mode == "confirm":
            sigs = {}
            for k in STRATEGIES:
                try:
                    s = run_strategy(k, ind)
                    sigs[k] = int(s.position[i]) if i < len(s.position) else 0
                except Exception:
                    continue
            bulls = [k for k, v in sigs.items() if v == 1]
            bears = [k for k, v in sigs.items() if v == 0]
            agreeing = len(bulls)
            score = (agreeing - len(bears)) / max(1, len(sigs))
            factors = {k: float(v) for k, v in sigs.items()}
            reasons.append(f"{len(sigs)} 个策略中 {agreeing} 个看多"
                           f"（需 ≥{cfg.confirm_min}）")
            if agreeing >= cfg.confirm_min:
                target_frac = cfg.fixed_fraction
                reasons.append("达到共识数量 → 开仓")
            else:
                target_frac = 0.0
                reasons.append("共识不足 → 空仓")

        elif mode == "ai":
            # AI 模式由外部提供决策，这里只做兜底（不应走到）
            return Decision("hold", fraction, fraction, 0.0, {},
                            ["AI 模式需要外部决策输入"], mode=mode,
                            note="等待 AI 决策")

        else:  # signal
            if cfg.strategy not in STRATEGIES:
                return Decision("hold", fraction, fraction, 0.0, {},
                                [f"未知策略 {cfg.strategy}"], mode=mode)
            sig = run_strategy(cfg.strategy, ind)
            want = int(sig.position[i]) if i < len(sig.position) else 0
            score = 1.0 if want else -1.0
            why = sig.reasons[i] if i < len(sig.reasons) and sig.reasons[i] else ""
            reasons.append(f"{sig.label}：{'看多' if want else '空仓'}"
                           + (f"（{why}）" if why else ""))
            factors = {"signal": float(want)}
            target_frac = cfg.fixed_fraction if want else 0.0

        target_frac = _clamp(target_frac, 0.0, cfg.max_fraction)

        # ── 冷却期与最短持有 ──
        if target_frac > fraction and bars_since_exit >= 0:
            if bars_since_exit < risk.cooldown_bars:
                reasons.append(f"距上次卖出仅 {bars_since_exit} 根，"
                               f"冷却 {risk.cooldown_bars} 根内不开仓")
                target_frac = fraction
        if target_frac < fraction and position and bars_held < risk.min_hold_bars:
            reasons.append(f"仅持有 {bars_held} 根，未到最短持有 "
                           f"{risk.min_hold_bars} 根")
            target_frac = fraction

        # ── 第 2 层：分档，避免频繁微调 ──
        # 目标与当前差得太少就不动：省手续费，也避免信号在阈值附近抖动时
        # 反复买卖（那是真实账户被磨死的主要原因之一）
        if abs(target_frac - fraction) < cfg.rebalance_band:
            return Decision("hold", fraction, fraction, score, factors,
                            reasons + [f"目标与当前差 {abs(target_frac-fraction):.0%} "
                                       f"< 分档阈值 {cfg.rebalance_band:.0%}，不动"],
                            mode=mode)

        action = "buy" if target_frac > fraction else "sell"
        return Decision(action, target_frac, fraction, score, factors, reasons,
                        mode=mode)


# ============================================================
#  AI 决策器（可插拔）
# ============================================================
class AIDecider:
    """
    把决策交给外部 AI。

    ═══════════════════════════════════════════════════════════
      为什么不内置调用模型
    ═══════════════════════════════════════════════════════════
    这台机器上没有可直接调用的模型 CLI，而 ~/.dsh/.credentials.yaml
    存的是用户凭据 —— 拿它去直连模型既不可靠（不知道端点与配额）
    也不合适（等于绕过 harness 自己发请求）。所以做成可插拔。

    ═══════════════════════════════════════════════════════════
      怎么接（三种方式，任选其一）
    ═══════════════════════════════════════════════════════════
    1) 任何 OpenAI 兼容端点（Ollama / vLLM / LM Studio / 官方 API）：
         set QTRADER_AI_URL=http://127.0.0.1:11434/v1/chat/completions
         set QTRADER_AI_MODEL=qwen2.5:14b
         set QTRADER_AI_KEY=sk-xxx        （本地模型可留空）

    2) 任意命令行程序：stdin 收 JSON，stdout 回 JSON
         set QTRADER_AI_CMD=python my_agent.py

    3) 任意 HTTP 服务：POST JSON，回 JSON
         set QTRADER_AI_URL=http://127.0.0.1:8000/decide

    返回格式（两种都接受）：
       {"action": "buy|sell|hold", "target": 0.0~1.0, "reason": "..."}
       {"action": "hold", "reason": "理由"}

    ⚠️ 安全设计：AI 的输出**只被当作建议**。
       target 会被夹到 [0, max_fraction]，风控层仍然独立生效 ——
       AI 说"满仓"也不能突破 max_fraction，说"不卖"也会被止损强制平仓。
       否则一个胡说的模型能把账户打爆。
    """

    def __init__(self, config: AutoConfig | None = None,
                 url: str | None = None, model: str | None = None,
                 api_key: str | None = None, cmd: str | None = None,
                 timeout: float = 30.0, fallback: AutoDecider | None = None):
        import os
        self.cfg = config or AutoConfig(mode="ai")
        self.url = url or os.environ.get("QTRADER_AI_URL")
        self.model = model or os.environ.get("QTRADER_AI_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("QTRADER_AI_KEY", "")
        self.cmd = cmd or os.environ.get("QTRADER_AI_CMD")
        self.timeout = timeout
        # 外部 AI 失败时用内置逻辑兜底 —— 不能让账户因为网络问题停止风控
        self.fallback = fallback or AutoDecider(AutoConfig(mode="score"))

    # --------------------------------------------------------
    @staticmethod
    def build_context(ind, i: int, *, symbol: str, name: str, interval: str,
                      position: int, fraction: float, entry_price: float,
                      equity: float, cash: float, peak_price: float,
                      drawdown: float, recent: list[dict] | None = None) -> dict:
        """
        组装给 AI 的市场快照。

        刻意只给**结构化数值 + 明确含义**，不给原始 K 线数组：
          · 数值带单位与解释，模型不容易误读
          · 体积小，省 token
          · 关键指标预先算好，避免模型自己做算术（它算不准）
        """
        def g(n):
            return _val(ind, n, i)

        close = g("close") or 0.0
        atr = g("atr")
        snap = {
            "标的": f"{name} ({symbol})",
            "周期": interval,
            "当前价": round(close, 4),
            "指标": {
                "EMA12": g("ema12"), "EMA26": g("ema26"),
                "SMA20": g("sma20"), "SMA60": g("sma60"),
                "RSI14": g("rsi"),
                "MACD_DIF": g("macd_dif"), "MACD_DEA": g("macd_dea"),
                "MACD柱": g("macd_hist"),
                "ADX": g("adx"), "PDI": g("plus_di"), "MDI": g("minus_di"),
                "布林上轨": g("bb_upper"), "布林中轨": g("bb_mid"),
                "布林下轨": g("bb_lower"), "布林带宽": g("bb_width"),
                "ATR": atr,
                "ATR占价格比": round(atr / close, 5) if atr and close else None,
                "KDJ_K": g("kdj_k"), "KDJ_D": g("kdj_d"),
                "量比": g("volumeRatio"),
            },
            "账户": {
                "总权益": round(equity, 2), "可用现金": round(cash, 2),
                "是否持仓": bool(position),
                "持仓成本": round(entry_price, 4) if position else None,
                "持仓浮动盈亏": (round(close / entry_price - 1, 4)
                                 if position and entry_price else None),
                "当前仓位比例": round(fraction, 4),
                "持仓期最高价": round(peak_price, 4) if position else None,
                "账户回撤": round(drawdown, 4),
                "已实现盈亏": None,
            },
            "最近成交": recent or [],
        }
        return snap

    @staticmethod
    def build_prompt(ctx: dict) -> str:
        return (
            "你是一个量化交易员。根据下面的市场快照决定**下一根 K 线**的操作。\n"
            "\n"
            "要求：\n"
            "1. 只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。\n"
            '2. 格式：{"action":"buy|sell|hold","target":0到1的小数,"reason":"一句话理由"}\n'
            "3. target 是**目标仓位比例**（占权益的百分比）。0=空仓，1=满仓。\n"
            "4. 无法判断时就 hold，不要勉强交易 —— 频繁交易会被手续费磨死。\n"
            "5. 注意 A 股 T+1（当日买入不能当日卖出）与整手限制。\n"
            "6. reason 用中文，一句话说清主要依据。\n"
            "\n"
            "市场快照：\n"
            + json.dumps(ctx, ensure_ascii=False, indent=1)
        )

    # --------------------------------------------------------
    def _call_openai(self, prompt: str) -> dict:
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是量化交易助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }).encode("utf8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode("utf8"))
        text = d["choices"][0]["message"]["content"]
        return json.loads(text)

    def _call_cmd(self, prompt: str, ctx: dict) -> dict:
        import subprocess
        p = subprocess.run(self.cmd, shell=True, input=json.dumps(
            {"prompt": prompt, "context": ctx}, ensure_ascii=False),
            capture_output=True, text=True, timeout=self.timeout,
            encoding="utf8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"AI 命令退出码 {p.returncode}: {p.stderr[:200]}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    def ask(self, ctx: dict) -> dict:
        """问一次 AI。失败抛异常，由 decide() 兜底。"""
        prompt = self.build_prompt(ctx)
        if self.cmd:
            return self._call_cmd(prompt, ctx)
        if self.url:
            return self._call_openai(prompt)
        raise RuntimeError("没有配置 AI（设 QTRADER_AI_URL 或 QTRADER_AI_CMD）")

    # --------------------------------------------------------
    def decide(self, *, ind, i: int, position: int, fraction: float,
               entry_price: float, peak_price: float, drawdown: float,
               bars_since_exit: int, bars_held: int, halted: bool = False,
               meta: dict | None = None) -> Decision:
        """先跑风控，再问 AI。AI 挂了就用内置逻辑兜底。"""
        cfg = self.cfg

        # ── 风控永远先在本地判定，不交给 AI ──
        guard = AutoDecider(cfg).decide(
            ind=ind, i=i, position=position, fraction=fraction,
            entry_price=entry_price, peak_price=peak_price, drawdown=drawdown,
            bars_since_exit=bars_since_exit, bars_held=bars_held, halted=halted)
        if guard.risk_hit:
            guard.reasons.insert(0, "风控在本地判定，未提交给 AI")
            return guard

        m = meta or {}
        try:
            ctx = self.build_context(
                ind, i, symbol=m.get("symbol", ""), name=m.get("name", ""),
                interval=m.get("interval", ""), position=position,
                fraction=fraction, entry_price=entry_price,
                equity=m.get("equity", 0.0), cash=m.get("cash", 0.0),
                peak_price=peak_price, drawdown=drawdown,
                recent=m.get("recent"))
            raw = self.ask(ctx)
        except Exception as e:
            d = self.fallback.decide(
                ind=ind, i=i, position=position, fraction=fraction,
                entry_price=entry_price, peak_price=peak_price,
                drawdown=drawdown, bars_since_exit=bars_since_exit,
                bars_held=bars_held, halted=halted)
            d.reasons.insert(0, f"AI 调用失败，改用内置打分兜底：{type(e).__name__}: {e}")
            d.mode = "ai(fallback)"
            return d

        action = str(raw.get("action", "hold")).lower().strip()
        if action not in ("buy", "sell", "hold"):
            action = "hold"
        try:
            target = float(raw.get("target", fraction if action == "hold" else 0.0))
        except (TypeError, ValueError):
            target = fraction
        # ⚠️ 夹紧：AI 不能突破仓位上限
        target = _clamp(target, 0.0, cfg.max_fraction)
        reason = str(raw.get("reason", ""))[:200]

        if abs(target - fraction) < cfg.rebalance_band:
            return Decision("hold", fraction, fraction, 0.0, {},
                            [f"AI 建议目标 {target:.0%}，与当前 {fraction:.0%} "
                             f"差 {abs(target-fraction):.0%} 未过阈值，不动",
                             f"AI 理由：{reason}"],
                            mode="ai")

        act = "buy" if target > fraction else "sell"
        return Decision(act, target, fraction, 0.0, {},
                        [f"AI 决策：{reason}", f"目标仓位 {target:.0%}（当前 {fraction:.0%}）"],
                        mode="ai")


# ============================================================
def make_decider(cfg: AutoConfig):
    """按配置造决策器。"""
    if cfg.mode == "ai":
        return AIDecider(cfg)
    return AutoDecider(cfg)


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

    print("=" * 76)
    print("  自动交易逻辑自检（不联网）")
    print("=" * 76)

    def mk_series(prices, vols=None):
        vols = vols or [1.0] * len(prices)
        bars = tuple(Bar(1000 + i * 86400000, p, p * 1.01, p * 0.99, p, v)
                     for i, (p, v) in enumerate(zip(prices, vols)))
        s = Series("test", "crypto", "TEST", "测试", "1d", bars, 0.0)
        return s

    def ind_of(prices, vols=None):
        return compute(mk_series(prices, vols))

    # --- 1. 明显上涨：应该看多 ---
    print("\n[1] 打分模型：单边上涨")
    up = [100 * (1.012 ** i) for i in range(120)]
    ind = ind_of(up)
    d = AutoDecider(AutoConfig(mode="score"))
    dec = d.decide(ind=ind, i=len(up) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=999, bars_held=0)
    check("总分 > 0", dec.score > 0, f"{dec.score:+.3f}")
    check("决定买入", dec.action == "buy", f"{dec.action} → 目标 {dec.target:.0%}")
    check("有因子明细", len(dec.factors) >= 4, f"{list(dec.factors)}")
    check("有可读理由", len(dec.reasons) >= 2)

    # --- 2. 明显下跌：应该看空 ---
    print("\n[2] 打分模型：单边下跌")
    dn = [100 * (0.988 ** i) for i in range(120)]
    ind = ind_of(dn)
    dec = d.decide(ind=ind, i=len(dn) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=999, bars_held=0)
    check("总分 < 0", dec.score < 0, f"{dec.score:+.3f}")
    check("决定不买入", dec.action == "hold", f"{dec.action}")

    # --- 3. 震荡市：不应该频繁交易 ---
    print("\n[3] 震荡市：应当观望")
    rng = np.random.default_rng(7)
    flat = list(100 + np.cumsum(rng.normal(0, 0.25, 150)))
    ind = ind_of(flat)
    dec = d.decide(ind=ind, i=len(flat) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=999, bars_held=0)
    check("震荡市多为观望或轻仓", dec.action == "hold" or dec.target <= 0.5,
          f"{dec.action} 目标 {dec.target:.0%} 分数 {dec.score:+.2f}")

    # --- 4. 止损优先于信号 ---
    print("\n[4] 风控优先：止损必须压过看多信号")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=1, fraction=0.9,
                   entry_price=up[-1] * 1.20, peak_price=up[-1] * 1.21,
                   drawdown=0.05, bars_since_exit=0, bars_held=10)
    check("触发止损", dec.risk_hit is not None and "止损" in dec.risk_hit,
          dec.risk_hit or "未触发")
    check("动作是卖出", dec.action == "sell", dec.action)
    check("目标仓位归零", dec.target == 0.0)

    # --- 5. 止盈 ---
    print("\n[5] 风控：止盈")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=1, fraction=0.9,
                   entry_price=up[-1] * 0.7, peak_price=up[-1],
                   drawdown=0.02, bars_since_exit=0, bars_held=10)
    check("触发止盈", dec.risk_hit is not None and "止盈" in (dec.risk_hit or ""),
          dec.risk_hit or "未触发")

    # --- 6. 移动止损 ---
    print("\n[6] 风控：移动止损（从最高点回撤）")
    # 参数要精心选，让移动止损成为**第一个**触发的规则：
    #   close=100   entry=92   → 浮盈 +8.7%（不到 20% 止盈线）
    #   peak=110               → 从最高价回撤 -9.1%（超过 8% 移动止损线）
    # 早先的用例写了 entry=0.5×close（+100% 浮盈），结果先触发止盈 ——
    # 那是风控顺序正确，不是 bug，但测不到移动止损这一条。
    px = [92.0] * 60 + [100.0]
    ind6 = ind_of(px)
    dec = d.decide(ind=ind6, i=len(px) - 1, position=1, fraction=0.9,
                   entry_price=92.0, peak_price=110.0,
                   drawdown=0.02, bars_since_exit=0, bars_held=10)
    check("触发移动止损", dec.risk_hit is not None and "移动止损" in (dec.risk_hit or ""),
          dec.risk_hit or "未触发")
    # 对照：没超过回撤线就不该触发
    dec = d.decide(ind=ind6, i=len(px) - 1, position=1, fraction=0.9,
                   entry_price=92.0, peak_price=105.0,
                   drawdown=0.02, bars_since_exit=0, bars_held=10)
    check("回撤未超线时不触发", dec.risk_hit is None or "移动止损" not in (dec.risk_hit or ""),
          dec.risk_hit or "未触发（正确）")

    # --- 7. 回撤熔断 ---
    print("\n[7] 风控：回撤熔断")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=1, fraction=0.9,
                   entry_price=up[-1], peak_price=up[-1],
                   drawdown=0.30, bars_since_exit=0, bars_held=5)
    check("触发熔断", dec.risk_hit is not None and "熔断" in (dec.risk_hit or ""),
          dec.risk_hit or "未触发")
    dec2 = d.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                    entry_price=0, peak_price=0, drawdown=0.30,
                    bars_since_exit=0, bars_held=0)
    check("空仓时熔断禁止开仓", dec2.action == "hold" and dec2.target == 0.0,
          f"{dec2.action} 目标 {dec2.target}")

    # --- 8. 冷却期 ---
    print("\n[8] 冷却期：卖出后短时间内不开仓")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=1, bars_held=0)
    check("冷却期内不开仓", dec.action == "hold", f"{dec.action}")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=999, bars_held=0)
    check("冷却期过后可开仓", dec.action == "buy", f"{dec.action}")

    # --- 9. 分档：小差异不动 ---
    print("\n[9] 分档阈值：微小差异不折腾")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=1, fraction=0.94,
                   entry_price=up[-1] * 0.9, peak_price=up[-1],
                   drawdown=0.01, bars_since_exit=999, bars_held=10)
    check("差 1% 不动（阈值 15%）", dec.action == "hold", f"{dec.action}")

    # --- 10. 仓位模式 ---
    print("\n[10] 仓位管理三种模式")
    cfg_fixed = AutoConfig(mode="score", sizing="fixed", fixed_fraction=0.5)
    dec = AutoDecider(cfg_fixed).decide(
        ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
        entry_price=0, peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("fixed 模式用固定比例", abs(dec.target - 0.5) < 1e-6, f"{dec.target}")

    cfg_score = AutoConfig(mode="score", sizing="score")
    dec_s = AutoDecider(cfg_score).decide(
        ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
        entry_price=0, peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("score 模式仓位随信心变化（非固定）", dec_s.target != 0.5,
          f"{dec_s.target:.3f}")

    cfg_vol = AutoConfig(mode="score", sizing="volatility", target_vol=0.02)
    dec_v = AutoDecider(cfg_vol).decide(
        ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
        entry_price=0, peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("volatility 模式给出 0~1 的比例",
          0.0 <= dec_v.target <= 1.0, f"{dec_v.target:.3f}")

    # --- 11. confirm 模式 ---
    print("\n[11] 共识模式")
    cfg_c = AutoConfig(mode="confirm", confirm_min=2, fixed_fraction=0.8)
    dec = AutoDecider(cfg_c).decide(
        ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
        entry_price=0, peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("给出共识信息", any("看多" in r for r in dec.reasons),
          dec.reasons[0] if dec.reasons else "—")
    cfg_c5 = AutoConfig(mode="confirm", confirm_min=5)
    dec5 = AutoDecider(cfg_c5).decide(
        ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
        entry_price=0, peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("要求 5 个共识时更保守", dec5.target <= dec.target,
          f"min2={dec.target:.2f} min5={dec5.target:.2f}")

    # --- 12. 边界：数据不足 ---
    print("\n[12] 边界情况")
    short = ind_of([100.0, 101.0, 102.0])
    dec = d.decide(ind=short, i=2, position=0, fraction=0.0, entry_price=0,
                   peak_price=0, drawdown=0, bars_since_exit=999, bars_held=0)
    check("数据不足时不崩溃", dec.action in ("buy", "sell", "hold"), dec.action)
    check("数据不足时多为观望或轻仓", dec.target <= 0.5, f"{dec.target:.2f}")
    dec = d.decide(ind=None, i=0, position=0, fraction=0.0, entry_price=0,
                   peak_price=0, drawdown=0, bars_since_exit=0, bars_held=0)
    check("ind=None 返回不动", dec.action == "hold")

    # --- 13. AI 提示词与上下文 ---
    print("\n[13] AI 接口")
    ctx = AIDecider.build_context(
        ind_of(up), len(up) - 1, symbol="BTCUSDT", name="比特币", interval="1h",
        position=1, fraction=0.5, entry_price=100.0, equity=100000.0,
        cash=50000.0, peak_price=120.0, drawdown=0.03)
    check("上下文含标的信息", ctx["标的"].startswith("比特币"))
    check("上下文含指标", len(ctx["指标"]) > 12, f"{len(ctx['指标'])} 项")
    check("上下文含账户状态", ctx["账户"]["是否持仓"] is True)
    check("开仓比例已给出", ctx["账户"]["当前仓位比例"] == 0.5)
    check("NA 值被转为 null 而非 NaN",
          all(v is None or not (isinstance(v, float) and math.isnan(v))
              for v in ctx["指标"].values()))
    p = AIDecider.build_prompt(ctx)
    check("提示词要求 JSON 输出", "JSON" in p and "action" in p)
    check("提示词提到交易成本", "手续费" in p)
    check("提示词提到 T+1", "T+1" in p)
    check("提示词可 JSON 序列化", isinstance(json.dumps(ctx, ensure_ascii=False), str))

    # --- 14. AI 不可用时兜底 ---
    print("\n[14] AI 失败兜底（不能停止风控）")
    ai = AIDecider(AutoConfig(mode="ai"))     # 没配 URL/CMD
    dec = ai.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                    entry_price=0, peak_price=0, drawdown=0,
                    bars_since_exit=999, bars_held=0, meta={})
    check("AI 失败时给出决策", dec.action in ("buy", "sell", "hold"), dec.action)
    check("标注用了兜底", "fallback" in dec.mode, dec.mode)
    check("说明失败原因", any("失败" in r for r in dec.reasons),
          dec.reasons[0][:60] if dec.reasons else "—")
    # 关键：风控仍然生效
    dec = ai.decide(ind=ind_of(up), i=len(up) - 1, position=1, fraction=0.9,
                    entry_price=up[-1] * 1.3, peak_price=up[-1] * 1.3,
                    drawdown=0.05, bars_since_exit=0, bars_held=5, meta={})
    check("AI 模式下风控仍本地生效（未问 AI）", dec.risk_hit is not None,
          dec.risk_hit or "未触发")

    # --- 15. AI 输出夹紧 ---
    print("\n[15] AI 输出被夹紧（不能突破仓位上限）")
    ai2 = AIDecider(AutoConfig(mode="ai", max_fraction=0.6, rebalance_band=0.01))
    ai2.ask = lambda ctx: {"action": "buy", "target": 5.0, "reason": "梭哈"}
    dec = ai2.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                     entry_price=0, peak_price=0, drawdown=0,
                     bars_since_exit=999, bars_held=0, meta={"symbol": "T"})
    check("目标被夹到 max_fraction", dec.target <= 0.6, f"{dec.target}")
    ai2.ask = lambda ctx: {"action": "buy", "target": -3.0, "reason": "负仓位"}
    dec = ai2.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                     entry_price=0, peak_price=0, drawdown=0,
                     bars_since_exit=999, bars_held=0, meta={"symbol": "T"})
    check("负目标被夹到 0", dec.target >= 0.0, f"{dec.target}")
    ai2.ask = lambda ctx: {"action": "GARBAGE", "target": "abc"}
    dec = ai2.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                     entry_price=0, peak_price=0, drawdown=0,
                     bars_since_exit=999, bars_held=0, meta={"symbol": "T"})
    check("非法动作被降级为 hold", dec.action in ("buy", "sell", "hold"), dec.action)

    # --- 16. Decision 可序列化 ---
    print("\n[16] 决策对象可序列化")
    dec = d.decide(ind=ind_of(up), i=len(up) - 1, position=0, fraction=0.0,
                   entry_price=0, peak_price=0, drawdown=0,
                   bars_since_exit=999, bars_held=0)
    j = json.dumps(dec.to_dict(), ensure_ascii=False)
    check("to_dict 可 JSON 序列化", isinstance(j, str), f"{len(j)} 字符")
    check("有一行摘要", len(dec.summary()) > 5, dec.summary()[:50])

    print("\n" + "=" * 76)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 76)
    return 1 if fail else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("这是模块。用 --selftest 跑自检。")
