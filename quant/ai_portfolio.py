#!/usr/bin/env python3
"""
AI 组合 —— 把 AI 选股结果变成一个能跟踪收益的模拟盘组合。

═══════════════════════════════════════════════════════════════
  为什么单独写一个，不复用 PaperSession
═══════════════════════════════════════════════════════════════
`paper_live.PaperSession` 是**单标的**的（只有一个 `self.qty` /
`self.avg_price`）。而「组合」天生是多标的 —— 一个组合里 8 只股票。

硬套 PaperSession 会有两个问题：
  1. `MAX_SESSIONS = 8`，一个 8 只的组合就把槽位占满
  2. 超出上限的淘汰逻辑是「删最久未更新的」——
     组合成员会被**静默删掉**，收益统计就错了

所以这里写一个多标的版本，复用同样的成本模型（`backtest.Costs`）
和同样的行情接口（`datasource.fetch`），但状态独立存
`quant/state/ai-portfolios.json`，不干扰手动的模拟盘会话。

═══════════════════════════════════════════════════════════════
  这个组合是怎么运作的
═══════════════════════════════════════════════════════════════
  1. 跑选股（L1+L2+L3）拿到最终名单
  2. 等权分配资金：总资金 ÷ 只数
  3. 按当前价买入（A 股整手 100 股），记录买入价与成本
  4. 之后每次 refresh 拉最新价，重算市值与收益
  5. 跟沪深300 / 上证指数对比基准收益

⚠️ 这是一个**不做调仓**的组合 —— 买入后一直持有。
   为什么：调仓规则（什么时候卖、换成什么）是一个独立且困难的问题，
   不是这一版该解决的。周报式的持有收益已经能回答
   「AI 选的股票到底涨不涨」这个核心问题。

   想验证调仓逻辑，用模拟盘（paper.html）配自动交易；
   想验证**选股质量**，用这个组合。

═══════════════════════════════════════════════════════════════
  基准选择
═══════════════════════════════════════════════════════════════
用**上证指数 sh000001** 作为基准。理由：用户看 A 股习惯看上证。
不用沪深300是因为指数的成分股会调整，历史对齐更麻烦。
拿不到指数数据时明确返回 null，不编造基准数字。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from backtest import Costs, MARKET_COSTS                         # noqa: E402
from datasource import fetch, DataSourceError                    # noqa: E402

STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "ai-portfolios.json"

# 组合数量上限。每个组合是一个多标的持仓，不占模拟盘槽位。
MAX_PORTFOLIOS = 20
# 单组合最多几只。超过就不够分散也难管。
MAX_HOLDINGS = 20
# A 股整手
A_SHARE_LOT = 100
# 基准指数
BENCHMARK = ("ashare", "sh000001", "上证指数")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _offline() -> bool:
    """
    是否离线模式（不发起网络请求）。

    ⚠️ 与 screener._offline() 同一套约定：环境变量 QUANT_OFFLINE。
       离线时 refresh() 直接返回，不拉行情 ——
       否则测试里建个组合就会真的联网，结果依赖当时的真实价格，
       测试永远不稳定。
    """
    return os.environ.get("QUANT_OFFLINE", "").strip() not in ("", "0", "false")


class Holding:
    """组合里的一只股票。"""

    def __init__(self, symbol: str, name: str, sector: str,
                 qty: float, entry_price: float, entry_fee: float,
                 entry_time: str, reason: str = "", confidence: float = 0.5):
        self.symbol = symbol
        self.name = name
        self.sector = sector
        self.qty = float(qty)
        self.entry_price = float(entry_price)     # 含滑点的成交价
        self.entry_fee = float(entry_fee)
        self.entry_time = entry_time
        self.reason = reason                       # AI 给的买入逻辑
        self.confidence = float(confidence)
        # 运行时字段（不持久化的当前价会在 refresh 里填）
        self.last_price: float | None = None
        self.last_at: str | None = None

    def cost(self) -> float:
        """买入成本（含手续费）。"""
        return self.qty * self.entry_price + self.entry_fee

    def market_value(self) -> float:
        px = self.last_price if self.last_price is not None else self.entry_price
        return self.qty * px

    def pnl(self) -> float:
        """浮动盈亏（未扣卖出成本）。"""
        return self.market_value() - self.cost()

    def pnl_pct(self) -> float:
        c = self.cost()
        return (self.pnl() / c * 100) if c > 0 else 0.0

    def to_state(self) -> dict:
        return {
            "symbol": self.symbol, "name": self.name, "sector": self.sector,
            "qty": self.qty, "entryPrice": round(self.entry_price, 4),
            "entryFee": round(self.entry_fee, 4), "entryTime": self.entry_time,
            "reason": self.reason, "confidence": self.confidence,
            "lastPrice": self.last_price, "lastAt": self.last_at,
        }

    @classmethod
    def from_state(cls, st: dict) -> "Holding":
        h = cls(
            symbol=st["symbol"], name=st.get("name", st["symbol"]),
            sector=st.get("sector", "其它"),
            qty=st["qty"], entry_price=st["entryPrice"],
            entry_fee=st.get("entryFee", 0.0),
            entry_time=st.get("entryTime", ""),
            reason=st.get("reason", ""),
            confidence=st.get("confidence", 0.5),
        )
        h.last_price = st.get("lastPrice")
        h.last_at = st.get("lastAt")
        return h


class AIPortfolio:
    """
    一个 AI 选股组合。单位：元。

    资金分配：`initial` 平均分给每只股票（等权）。
    等权而不是按市值/分数加权，理由：等权是最难作弊的基准 ——
    如果 AI 真有选股能力，等权就该跑赢指数；靠权重调整来「优化」
    很容易变成过拟合。
    """

    def __init__(self, *, pid: str | None = None, strategy: str = "",
                 label: str = "", mode: str = "local",
                 initial: float = 1_000_000.0,
                 market: str = "ashare", created_at: str = ""):
        self.id = pid or uuid.uuid4().hex[:12]
        self.strategy = strategy
        self.label = label or f"AI 组合 · {strategy}"
        self.mode = mode                       # "ai" 或 "local"
        self.initial = float(initial)
        self.market = market
        self.created_at = created_at or _now()
        self.updated_at = self.created_at
        self.costs: Costs = MARKET_COSTS.get(market, Costs())
        self.holdings: list[Holding] = []
        self.cash = float(initial)             # 未用掉的零头（整手取整后剩的）
        self.benchmark_start: float | None = None
        self.benchmark_last: float | None = None
        self.benchmark_at: str | None = None
        self.problems: list[str] = []          # 建仓时的问题（钱不够买一手等）
        self.notes: list[str] = []

    # ── 建仓 ──────────────────────────────────────────────
    def add_holding(self, *, symbol: str, name: str, sector: str,
                    price: float, budget: float, reason: str = "",
                    confidence: float = 0.5, lot: int = A_SHARE_LOT) -> bool:
        """
        按给定预算买入一只。返回是否成功。

        ⚠️ 整手取整：A 股必须 100 股整数倍。
           预算 125000 买 4.54 元的股票 → 27533 股 → 取整 27500 股。
           取整剩下的钱留在 cash 里，不强行凑。
        """
        if not (price > 0):
            self.problems.append(f"{symbol} 价格无效（{price}），跳过")
            return False
        # 买入价含滑点
        px = self.costs.buy_price(price)
        raw_qty = budget / (px * (1 + self.costs.commission))
        # ⚠️ 整手取整只对 A 股类市场（lot > 1）成立。
        #    加密要保留小数：5000 元买 5 万美元的 BTC 应得 0.0998 个，
        #    早期实现无脑 `int()` 会取成 0，直接判定「买不起」——
        #    但函数注释明明写着「加密可以小数」。实测踩过。
        qty = ((int(raw_qty) // lot) * lot) if lot > 1 else raw_qty
        if qty <= 0:
            self.problems.append(
                f"{name}（{symbol}）预算 {budget:.0f} 买不起一手"
                f"（{price:.2f} × {lot} 股）")
            return False
        notional = qty * px
        fee = self.costs.fee(notional)
        cost = notional + fee
        if cost > self.cash + 1e-6:
            # 预算够但现金不够（前面几只花超了），按可用现金再算一次
            raw_qty = self.cash / (px * (1 + self.costs.commission))
            qty = ((int(raw_qty) // lot) * lot) if lot > 1 else raw_qty
            if qty <= 0:
                self.problems.append(f"{name}（{symbol}）现金不足，跳过")
                return False
            notional = qty * px
            fee = self.costs.fee(notional)
            cost = notional + fee
        h = Holding(symbol=symbol, name=name, sector=sector, qty=qty,
                    entry_price=px, entry_fee=fee, entry_time=_now(),
                    reason=reason, confidence=confidence)
        h.last_price = price
        h.last_at = _now()
        self.holdings.append(h)
        self.cash -= cost
        return True

    # ── 刷新行情 ──────────────────────────────────────────
    def refresh(self, verbose: bool = False) -> dict:
        """
        拉每只持仓的最新价，并更新基准。返回摘要。

        ⚠️ 单只失败不影响整体 —— 用上一次的价格，并记一条 note。
           整个组合因为一只股票拉不到就报错，是没必要的。

        ⚠️ 离线模式（QUANT_OFFLINE）直接返回，不联网。
           测试靠这个保证可复现：否则建仓时会拉真实价格，
           断言全部依赖当时的行情，根本没法写测试。
        """
        if _offline():
            self.updated_at = _now()
            return {"updated": 0, "failed": [], "total": len(self.holdings),
                    "offline": True}

        updated, failed = 0, []
        for h in self.holdings:
            try:
                s = fetch(self.market, h.symbol, "1d", 5)
                h.last_price = s.bars[-1].close
                h.last_at = s.bars[-1].as_dict()["iso"][:19]
                updated += 1
            except (DataSourceError, Exception) as e:       # noqa: BLE001
                failed.append(f"{h.name}（{h.symbol}）")
                if verbose:
                    print(f"    {h.symbol} 拉取失败: {type(e).__name__}",
                          file=sys.stderr)

        # 基准
        try:
            mk, sym, _label = BENCHMARK
            s = fetch(mk, sym, "1d", 5)
            px = s.bars[-1].close
            if self.benchmark_start is None:
                self.benchmark_start = px
            self.benchmark_last = px
            self.benchmark_at = s.bars[-1].as_dict()["iso"][:19]
        except (DataSourceError, Exception) as e:           # noqa: BLE001
            failed.append("基准（上证指数）")
            if verbose:
                print(f"    基准拉取失败: {type(e).__name__}", file=sys.stderr)

        self.updated_at = _now()
        if failed:
            self.notes.append(
                f"{self.updated_at} 有 {len(failed)} 项没拉到，沿用上次价格："
                + "、".join(failed))
            # notes 不要无限涨
            self.notes = self.notes[-20:]
        return {"updated": updated, "failed": failed,
                "total": len(self.holdings)}

    # ── 汇总 ──────────────────────────────────────────────
    def summary(self) -> dict:
        """组合层面的收益与风险。"""
        mv = sum(h.market_value() for h in self.holdings)
        cost = sum(h.cost() for h in self.holdings)
        equity = self.cash + mv
        pnl = equity - self.initial
        ret = (equity / self.initial - 1) * 100 if self.initial else 0.0

        # 基准收益
        bench_ret = None
        if self.benchmark_start and self.benchmark_last:
            bench_ret = (self.benchmark_last / self.benchmark_start - 1) * 100

        # 相对基准（超额收益）。拿不到基准就是 None，不编造。
        excess = None
        if bench_ret is not None:
            excess = ret - bench_ret

        # 持仓集中度
        sectors: dict[str, int] = {}
        for h in self.holdings:
            sectors[h.sector] = sectors.get(h.sector, 0) + 1
        top_sector = max(sectors.items(), key=lambda x: x[1]) if sectors else ("—", 0)

        rows = []
        for h in sorted(self.holdings, key=lambda x: -x.pnl_pct()):
            rows.append({
                "symbol": h.symbol, "name": h.name, "sector": h.sector,
                "qty": h.qty,
                "entryPrice": round(h.entry_price, 4),
                "lastPrice": round(h.last_price if h.last_price is not None
                                   else h.entry_price, 4),
                "cost": round(h.cost(), 2),
                "marketValue": round(h.market_value(), 2),
                "pnl": round(h.pnl(), 2),
                "pnlPct": round(h.pnl_pct(), 2),
                "weight": round(h.market_value() / equity * 100, 1) if equity else 0,
                "confidence": h.confidence,
                "reason": h.reason,
                "lastAt": h.last_at,
            })

        wins = sum(1 for h in self.holdings if h.pnl() > 0)
        return {
            "id": self.id,
            "label": self.label,
            "strategy": self.strategy,
            "mode": self.mode,
            "market": self.market,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "initial": round(self.initial, 2),
            "cash": round(self.cash, 2),
            "positionValue": round(mv, 2),
            "equity": round(equity, 2),
            "pnl": round(pnl, 2),
            "totalReturn": round(ret, 3),
            "benchmark": {
                "label": BENCHMARK[2],
                "start": round(self.benchmark_start, 4) if self.benchmark_start else None,
                "last": round(self.benchmark_last, 4) if self.benchmark_last else None,
                "return": round(bench_ret, 3) if bench_ret is not None else None,
                "at": self.benchmark_at,
            },
            "excessReturn": round(excess, 3) if excess is not None else None,
            "holdingsCount": len(self.holdings),
            "winCount": wins,
            "lossCount": len(self.holdings) - wins,
            "winRate": round(wins / len(self.holdings) * 100, 1) if self.holdings else 0.0,
            "sectors": dict(sorted(sectors.items(), key=lambda x: -x[1])),
            "topSector": {"name": top_sector[0], "count": top_sector[1]},
            "holdings": rows,
            "problems": self.problems,
            "notes": self.notes[-5:],
        }

    # ── 持久化 ────────────────────────────────────────────
    def to_state(self) -> dict:
        return {
            "id": self.id, "strategy": self.strategy, "label": self.label,
            "mode": self.mode, "initial": self.initial, "market": self.market,
            "createdAt": self.created_at, "updatedAt": self.updated_at,
            "cash": self.cash,
            "benchmarkStart": self.benchmark_start,
            "benchmarkLast": self.benchmark_last,
            "benchmarkAt": self.benchmark_at,
            "holdings": [h.to_state() for h in self.holdings],
            "problems": self.problems, "notes": self.notes[-20:],
        }

    @classmethod
    def from_state(cls, st: dict) -> "AIPortfolio":
        p = cls(pid=st["id"], strategy=st.get("strategy", ""),
                label=st.get("label", ""), mode=st.get("mode", "local"),
                initial=st.get("initial", 1_000_000.0),
                market=st.get("market", "ashare"),
                created_at=st.get("createdAt", ""))
        p.updated_at = st.get("updatedAt", p.created_at)
        p.cash = st.get("cash", p.initial)
        p.benchmark_start = st.get("benchmarkStart")
        p.benchmark_last = st.get("benchmarkLast")
        p.benchmark_at = st.get("benchmarkAt")
        p.holdings = [Holding.from_state(x) for x in st.get("holdings", [])]
        p.problems = st.get("problems", [])
        p.notes = st.get("notes", [])
        return p


# ══════════════════════════════════════════════════════════════
class PortfolioStore:
    """组合的持久化存储。与模拟盘会话的 state 文件分开。"""

    def __init__(self, state_file: Path = STATE_FILE, autosave: bool = True):
        self.state_file = state_file
        self.autosave = autosave
        self.portfolios: dict[str, AIPortfolio] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf8"))
        except (json.JSONDecodeError, OSError):
            return
        for st in (raw.get("portfolios") or []):
            try:
                p = AIPortfolio.from_state(st)
                self.portfolios[p.id] = p
            except (KeyError, TypeError, ValueError):
                continue

    def save(self) -> None:
        if not self.autosave:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "savedAt": _now(),
                   "portfolios": [p.to_state() for p in self.portfolios.values()]}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf8")
        tmp.replace(self.state_file)

    def create(self, **kw) -> AIPortfolio:
        if len(self.portfolios) >= MAX_PORTFOLIOS:
            # 淘汰最久未更新的 —— 但只淘汰「空仓」的，
            # 有持仓的组合不能静默删除（用户还要看它的收益）
            empty = [p for p in self.portfolios.values() if not p.holdings]
            if empty:
                oldest = min(empty, key=lambda p: p.updated_at)
                del self.portfolios[oldest.id]
            else:
                raise ValueError(
                    f"已有 {len(self.portfolios)} 个组合（上限 {MAX_PORTFOLIOS}），"
                    f"且都有持仓。请先删除一个。")
        p = AIPortfolio(**kw)
        self.portfolios[p.id] = p
        self.save()
        return p

    def get(self, pid: str) -> AIPortfolio | None:
        return self.portfolios.get(pid)

    def require(self, pid: str) -> AIPortfolio:
        p = self.portfolios.get(pid)
        if p is None:
            raise KeyError(f"组合不存在：{pid}")
        return p

    def delete(self, pid: str) -> bool:
        if pid in self.portfolios:
            del self.portfolios[pid]
            self.save()
            return True
        return False

    def list(self) -> list[dict]:
        """列出所有组合的摘要（不含持仓明细，页面列表用）。"""
        out = []
        for p in self.portfolios.values():
            mv = sum(h.market_value() for h in p.holdings)
            eq = p.cash + mv
            bench = None
            if p.benchmark_start and p.benchmark_last:
                bench = (p.benchmark_last / p.benchmark_start - 1) * 100
            ret = (eq / p.initial - 1) * 100 if p.initial else 0.0
            out.append({
                "id": p.id, "label": p.label, "strategy": p.strategy,
                "mode": p.mode, "createdAt": p.created_at,
                "updatedAt": p.updated_at,
                "initial": round(p.initial, 2),
                "equity": round(eq, 2),
                "totalReturn": round(ret, 3),
                "benchmarkReturn": round(bench, 3) if bench is not None else None,
                "excessReturn": round(ret - bench, 3) if bench is not None else None,
                "holdingsCount": len(p.holdings),
                "inPosition": len(p.holdings) > 0,
            })
        out.sort(key=lambda x: x["createdAt"], reverse=True)
        return out


# ══════════════════════════════════════════════════════════════
#  从选股结果建组合
# ══════════════════════════════════════════════════════════════
def create_from_picks(picks: list[dict], *, strategy: str, label: str = "",
                      mode: str = "local", initial: float = 1_000_000.0,
                      market: str = "ashare",
                      store: PortfolioStore | None = None) -> dict:
    """
    把选股结果建成一个组合并立即建仓。

    `picks` 每项至少要有 `symbol` / `name` / `price`，
    可选 `sector` / `reason`(或 thesis) / `confidence`。

    等权分配。返回组合摘要。
    """
    if not picks:
        raise ValueError("没有可建仓的标的")
    if len(picks) > MAX_HOLDINGS:
        picks = picks[:MAX_HOLDINGS]

    store = store or PortfolioStore()
    p = store.create(strategy=strategy, label=label or f"AI 组合 · {strategy}",
                     mode=mode, initial=initial, market=market)

    budget = initial / len(picks)
    for x in picks:
        price = x.get("price")
        if price is None:
            # 有的数据路径把现价放在 close 里
            price = x.get("close")
        p.add_holding(
            symbol=x["symbol"],
            name=x.get("name") or x["symbol"],
            sector=x.get("sector") or "其它",
            price=float(price or 0),
            budget=budget,
            reason=(x.get("reason") or x.get("thesis") or "")[:200],
            confidence=float(x.get("confidence") or 0.5),
            lot=A_SHARE_LOT if market == "ashare" else 1,
        )

    # 建完仓立刻拉一次基准，这样从第一天就有对比
    p.refresh(verbose=False)
    store.save()
    return p.summary()


# ══════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="AI 选股组合（模拟盘）")
    ap.add_argument("--list", action="store_true", help="列出所有组合")
    ap.add_argument("--show", metavar="ID", help="看某个组合的明细")
    ap.add_argument("--refresh", metavar="ID", help="刷新某个组合的行情")
    ap.add_argument("--refresh-all", action="store_true", help="刷新全部")
    ap.add_argument("--delete", metavar="ID", help="删除组合")
    ap.add_argument("--create", metavar="STRATEGY",
                    help="跑选股并建组合（如 dual_low）")
    ap.add_argument("--create-from-file", metavar="JSON_PATH",
                    help="按给定 JSON 里的 picks 建组合（页面传来的选股结果）")
    ap.add_argument("--top", type=int, default=8, help="组合持有几只")
    ap.add_argument("--capital", type=float, default=1_000_000.0, help="总资金")
    ap.add_argument("--no-ai", action="store_true", help="只本地打分")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    store = PortfolioStore()

    # ── 按给定的 picks 建组合（页面调用这条）──
    #
    # ⚠️ 为什么走文件而不是命令行参数：
    #    picks 是结构化数据（含中文名、理由），拼进命令行有长度限制
    #    和转义风险。服务端写一个临时 JSON 文件，这里读，最稳。
    if a.create_from_file:
        try:
            payload = json.loads(
                Path(a.create_from_file).read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"读取 picks 失败：{type(e).__name__}: {e}"},
                             ensure_ascii=False))
            return 1
        picks = payload.get("picks") or []
        try:
            out = create_from_picks(
                picks,
                strategy=payload.get("strategy") or "custom",
                label=payload.get("label") or "",
                mode=payload.get("mode") or "local",
                initial=float(payload.get("capital") or 1_000_000.0),
                market=payload.get("market") or "ashare",
                store=store)
        except (ValueError, KeyError) as e:
            print(json.dumps({"error": f"建组合失败：{e}"}, ensure_ascii=False))
            return 1
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if a.list:
        rows = store.list()
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("  还没有组合。用 --create dual_low 建一个。")
        else:
            print("=" * 86)
            print(f"  AI 组合（{len(rows)} 个）")
            print("=" * 86)
            print(f"  {'组合':<28}{'资金':>12}{'市值':>12}{'收益':>9}"
                  f"{'基准':>9}{'超额':>9}{'持仓':>6}")
            for r in rows:
                bench = (f"{r['benchmarkReturn']:+.2f}%"
                         if r["benchmarkReturn"] is not None else "—")
                excess = (f"{r['excessReturn']:+.2f}%"
                          if r["excessReturn"] is not None else "—")
                print(f"  {r['label'][:26]:<28}{r['initial']:>12,.0f}"
                      f"{r['equity']:>12,.0f}{r['totalReturn']:>8.2f}%"
                      f"{bench:>9}{excess:>9}{r['holdingsCount']:>6}")
        return 0

    if a.refresh_all:
        n = 0
        for p in store.portfolios.values():
            r = p.refresh(verbose=not a.json)
            n += r["updated"]
            if not a.json:
                print(f"  {p.label[:30]}: 更新 {r['updated']} 只"
                      + (f"，失败 {len(r['failed'])} 项" if r["failed"] else ""))
        store.save()
        if a.json:
            print(json.dumps({"updated": n}, ensure_ascii=False))
        return 0

    if a.refresh:
        p = store.require(a.refresh)
        r = p.refresh(verbose=True)
        store.save()
        print(json.dumps(r, ensure_ascii=False) if a.json
              else f"  更新 {r['updated']}/{r['total']} 只")
        return 0

    if a.delete:
        ok = store.delete(a.delete)
        print(json.dumps({"deleted": ok}) if a.json
              else ("  已删除" if ok else "  不存在"))
        return 0

    if a.show:
        p = store.require(a.show)
        s = p.summary()
        if a.json:
            print(json.dumps(s, ensure_ascii=False))
            return 0
        bench = ("—" if s["benchmark"]["return"] is None
                 else f"{s['benchmark']['return']:+.2f}%")
        excess = ("—" if s["excessReturn"] is None
                  else f"{s['excessReturn']:+.2f}%")
        print("=" * 86)
        print(f"  {s['label']}")
        print("=" * 86)
        print(f"  建仓 {s['createdAt']}   模式 {s['mode']}   策略 {s['strategy']}")
        print(f"  资金 {s['initial']:,.0f}   市值 {s['equity']:,.0f}   "
              f"收益 {s['totalReturn']:+.2f}%")
        print(f"  基准（{s['benchmark']['label']}）{bench}   "
              f"超额 {excess}")
        print(f"  持仓 {s['holdingsCount']} 只  盈利 {s['winCount']} / "
              f"亏损 {s['lossCount']}  (胜率 {s['winRate']:.0f}%)")
        if s["sectors"]:
            print("  行业：" + "、".join(f"{k} {v}" for k, v in s["sectors"].items()))
        print()
        print(f"  {'代码':<10}{'名称':<10}{'行业':<10}{'买入':>9}{'现价':>9}"
              f"{'盈亏':>10}{'盈亏%':>9}{'权重':>7}")
        for h in s["holdings"]:
            print(f"  {h['symbol']:<10}{h['name'][:9]:<10}{h['sector'][:9]:<10}"
                  f"{h['entryPrice']:>9.2f}{h['lastPrice']:>9.2f}"
                  f"{h['pnl']:>10,.0f}{h['pnlPct']:>8.2f}%{h['weight']:>6.1f}%")
        if s["problems"]:
            print("\n  建仓问题：")
            for x in s["problems"]:
                print(f"    · {x}")
        if s["notes"]:
            print("\n  备注：")
            for x in s["notes"]:
                print(f"    · {x}")
        return 0

    if a.create:
        from screener import run_strategy
        from stock_ai import enrich, ai_rank, local_rank

        print(f"  跑选股：{a.create} …", file=sys.stderr)
        l2 = run_strategy(a.create, top_n=max(a.top * 3, 30))
        print(f"  L1+L2: {l2['funnel']['market']} → "
              f"{l2['funnel']['afterFilter']} → {l2['funnel']['top']} 只",
              file=sys.stderr)
        enriched = enrich(l2["results"], verbose=True)
        res = local_rank(enriched) if a.no_ai else ai_rank(enriched, n_pick=a.top)

        if res.get("mode") == "ai" and res.get("picks"):
            picks = res["picks"]
            mode = "ai"
        else:
            # 本地模式：取综合分最高的几只
            picks = [{
                "symbol": e["symbol"], "name": e["name"],
                "sector": e.get("sector"), "price": e.get("price"),
                "reason": "、".join((e.get("techWhy") or [])[:3]),
                "confidence": 0.5,
            } for e in (res.get("results") or [])[:a.top]]
            mode = "local"

        if not picks:
            print("  没有选出任何标的，未建组合。")
            return 1
        out = create_from_picks(picks, strategy=a.create, mode=mode,
                                initial=a.capital)
        print(json.dumps(out, ensure_ascii=False) if a.json else "")
        if not a.json:
            print(f"  已建组合：{out['label']}")
            print(f"  {len(out['holdings'])} 只，资金 {out['initial']:,.0f}，"
                  f"市值 {out['equity']:,.0f}")
            for h in out["holdings"]:
                print(f"    {h['symbol']:<10}{h['name'][:9]:<10}"
                      f"{h['sector'][:9]:<10}{h['qty']:>8.0f} 股 @ {h['entryPrice']:.2f}")
            if out["problems"]:
                print("  建仓问题：")
                for x in out["problems"]:
                    print(f"    · {x}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
