#!/usr/bin/env python3
"""
能力实测 —— 这个项目现在到底能干什么、哪些是真能用的。

一次跑完所有主要功能并打印结果，不依赖网络以外的任何东西。
网络不可用时对应项会标「跳过」，而不是假装成功。

用法：python tools/what_it_does.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
QUANT = ROOT / "quant"
sys.path.insert(0, str(QUANT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

OK, FAIL, SKIP = "✅", "❌", "⏭"


def head(t: str) -> None:
    print(f"\n{'─' * 74}\n  {t}\n{'─' * 74}")


def run(args: list[str], timeout: int = 90) -> tuple[int, str]:
    """跑一条子命令，返回 (退出码, 输出)。"""
    try:
        r = subprocess.run([sys.executable, *args], capture_output=True,
                           text=True, encoding="utf8", errors="replace",
                           timeout=timeout, cwd=str(ROOT))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "（超时）"


# ══════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 74)
    print("  quant-workbench 能力实测")
    print("=" * 74)
    print(f"  Python {sys.version.split()[0]}   {ROOT}")

    # ── 1. 真实行情 ──────────────────────────────────────
    head("1. 抓真实行情（A 股 / 加密）")
    try:
        from datasource import fetch, MARKETS
        print(f"     支持的市场：{', '.join(MARKETS.keys())}")
        for mk in ("ashare", "crypto"):
            sym = MARKETS[mk]["defaultSymbol"]
            t0 = time.time()
            try:
                s = fetch(mk, sym, MARKETS[mk]["defaultInterval"], 300)
                ms = int((time.time() - t0) * 1000)
                print(f"  {OK} {mk:8} {sym:10} {len(s.bars):4} 根  "
                      f"{s.source:8} {ms:5}ms  "
                      f"{s.bars[0].as_dict()['iso'][:10]} → "
                      f"{s.bars[-1].as_dict()['iso'][:10]}  收 {s.bars[-1].close}")
            except Exception as e:                      # noqa: BLE001
                print(f"  {FAIL} {mk:8} {sym:10} {type(e).__name__}: {str(e)[:60]}")
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} 无法导入 datasource: {e}")
        return 1

    # ── 2. 指标 ─────────────────────────────────────────
    head("2. 算指标（27 个）")
    try:
        from indicators import compute
        s = fetch("ashare", "sh600519", "1d", 500)
        t0 = time.time()
        ind = compute(s)
        ms = int((time.time() - t0) * 1000)
        print(f"  {OK} 27 个指标算完，{len(s.bars)} 根耗时 {ms}ms")
        snap = ind.snapshot(-1)
        show = ["close", "ema12", "ema26", "rsi", "macd_dif", "adx",
                "atr", "bb_upper", "bb_lower", "kdj_k", "volume_ma20"]
        print("     最新一根快照：")
        for k in show:
            v = snap.get(k)
            print(f"       {k:14} {v if v is None else round(v, 4)}")
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {type(e).__name__}: {e}")

    # ── 3. 策略 ─────────────────────────────────────────
    head("3. 跑 5 个策略（出信号）")
    # ⚠️ run_all 要的是 IndicatorSet，不是 Series
    # ⚠️ SignalSet 的字段是 entry / exit（bool 数组），current 是**方法**
    try:
        from strategy import run_all
        sigs = run_all(ind)
        print(f"  {OK} {len(sigs)} 个策略"
              f"（{len(ind.values)} 个指标的 IndicatorSet 输入）")
        for k, sig in sigs.items():
            print(f"     {sig.label:14} 入场 {int(sig.entry.sum()):3}  "
                  f"出场 {int(sig.exit.sum()):3}  "
                  f"当前 {'持多' if sig.current() == 1 else '空仓'}  "
                  f"「{sig.last_reason()[:28]}」")
    except Exception as e:                              # noqa: BLE001
        import traceback
        print(f"  {FAIL} {type(e).__name__}: {e}")
        traceback.print_exc()

    # ── 4. 回测 ─────────────────────────────────────────
    head("4. 回测（含成本，对比买入持有）")
    # ⚠️ 三个容易踩的点（我第一版全踩了，数字荒谬到 -2149%）：
    #    · 初始资金必须用 default_initial(market)，A 股是 100 万。
    #      随手给 10 万 → 买不起 100 股一手 → 数字全废
    #    · Costs 的字段是 commission / slippage / min_commission，**没有 stamp**
    #    · BacktestResult 字段是蛇形（total_return），**没有 .perf 包装**
    try:
        from backtest import run_backtest, Costs
        from paper import default_initial
        costs = Costs(commission=0.0003, slippage=0.0005)
        init = default_initial("ashare")
        print(f"     初始资金 {init:,.0f}（default_initial('ashare')）"
              f"  手续费 {costs.commission*100:.3f}%  "
              f"滑点 {costs.slippage*100:.3f}%")
        print(f"  {'策略':18} {'收益':>9} {'买入持有':>9} {'超额':>9} "
              f"{'回撤':>8} {'夏普':>7} {'交易':>5} {'胜率':>7} {'持仓':>7}")
        results = {}
        for k, sig in sigs.items():
            r = run_backtest(s, sig, costs=costs, initial=init)
            results[k] = r
            print(f"  {sig.label:18} {r.total_return:+8.2f}% "
                  f"{r.benchmark_return:+8.2f}% {r.excess_return:+8.2f}% "
                  f"{r.max_drawdown:7.2f}% {r.sharpe:7.2f} "
                  f"{r.trade_count:5} {r.win_rate:6.1f}% {r.exposure:6.1f}%")
        beat = sum(1 for r in results.values() if r.excess_return > 0)
        print(f"\n     ★ 跑赢买入持有的策略：{beat}/{len(results)}")
        best = max(results.values(), key=lambda r: r.total_return)
        print(f"     ★ 最好的策略：{best.label}  {best.total_return:+.2f}%  "
              f"（买入持有 {best.benchmark_return:+.2f}%）")
        print(f"       成本拖累 {best.cost_drag:.2f} 个百分点  "
              f"平均持有 {best.avg_bars_held:.1f} 根  "
              f"盈亏比 {best.profit_factor:.2f}")
    except Exception as e:                              # noqa: BLE001
        import traceback
        print(f"  {FAIL} {type(e).__name__}: {e}")
        traceback.print_exc()

    # ── 5. 参数优化 ─────────────────────────────────────
    head("5. 参数优化（网格搜索）")
    try:
        from optimizer import Optimizer
        o = Optimizer(series=s, strategy="ema_cross", costs=costs)
        t0 = time.time()
        res = o.grid(max_combos=24)
        ms = int((time.time() - t0) * 1000)
        rows = res.get("results", res) if isinstance(res, dict) else res
        n = len(rows) if hasattr(rows, "__len__") else "?"
        print(f"  {OK} 跑完 {n} 组，耗时 {ms}ms")
        if hasattr(rows, "__getitem__") and len(rows):
            b = rows[0]
            if isinstance(b, dict):
                print(f"     最优参数 {b.get('params')}  "
                      f"收益 {(b.get('totalReturn') or 0)*100:.2f}%  "
                      f"回撤 {(b.get('maxDrawdown') or 0)*100:.2f}%")
            else:
                print(f"     最优: {b}")
        if isinstance(res, dict):
            for k in ("note", "warning", "tried", "elapsedMs"):
                if k in res:
                    print(f"     {k}: {res[k]}")
    except Exception as e:                              # noqa: BLE001
        import traceback
        print(f"  {FAIL} {type(e).__name__}: {e}")
        traceback.print_exc()

    # ── 6. 决策快照 ─────────────────────────────────────
    head("6. 决策快照（决策驾驶舱的数据源）")
    code, out = run([str(QUANT / "decision.py"), "--market", "ashare",
                     "--symbol", "sh600519", "--interval", "1d",
                     "--limit", "400", "--compact"])
    if code == 0:
        try:
            d = json.loads(out.strip().splitlines()[-1])
            print(f"  {OK} {d['meta']['name']} {d['meta']['bars']} 根  "
                  f"{d['meta']['elapsedMs']}ms  源 {d['meta']['source']}")
            print(f"     状态 {d['regime']['name']}（ADX {d['regime']['adx']}）")
            for f in d["factors"]:
                if f["present"]:
                    print(f"       {f['label']:8} {f['value']:+7.3f} × "
                          f"{f['weight']:.2f} = {f['contrib']:+7.3f}")
            print(f"     分数 {d['score']['clamped']:+.4f} → "
                  f"动作 {d['decision']['action']}  "
                  f"目标仓位 {d['decision']['target']*100:.0f}%")
        except Exception as e:                          # noqa: BLE001
            print(f"  {FAIL} 解析失败: {e}")
    else:
        print(f"  {FAIL} 退出码 {code}")

    # ── 7. 模拟盘 ───────────────────────────────────────
    head("7. 模拟盘（下单 / 风控 / 账户）")
    # PaperSession 的方法是 place_order（不是 place）
    # ⚠️ 故意不下真单：这会往 quant/state/ 写会话文件
    try:
        from paper_live import PaperSession, Side
        import inspect as _ins
        sig = _ins.signature(PaperSession.place_order)
        print(f"  {OK} PaperSession 可用")
        print(f"     place_order 签名: {sig}")
        print(f"     下单模式: next_open（下一根开盘成交，无未来函数）"
              f" / market（立即成交，有前视）")
        sess = PaperSession(market="ashare", symbol="sh600519",
                            initial=100_000.0)
        snap = sess.snapshot()
        print(f"  {OK} 开好会话，初始资金 100,000")
        print(f"     账户快照字段: {', '.join(sorted(snap.keys())[:10])}")
        print(f"     当前仓位: {snap.get('position', 0)}  "
              f"权益: {snap.get('equity', snap.get('cash', '—'))}")
        print(f"     （未真正下单，避免往 quant/state/ 写状态）")
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {type(e).__name__}: {str(e)[:100]}")

    # ── 8. AI 配置 ──────────────────────────────────────
    head("8. AI 决策配置（不联网验证配置链路）")
    try:
        from ai_settings import AIStore, PROVIDERS
        st = AIStore()
        s0 = st.load()
        pub = st.public_view() if hasattr(st, "public_view") else {}
        print(f"  {OK} 供应商 {len(PROVIDERS)} 个：{', '.join(PROVIDERS.keys())}")
        print(f"     当前配置：{s0.provider} / {s0.model}")
        print(f"     密钥已设置：{bool(s0.api_key)}"
              f"  （公开视图只给掩码：{pub.get('apiKeyHint', '—')}）")
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {type(e).__name__}: {str(e)[:100]}")

    # ── 9. 自动交易打分 ─────────────────────────────────
    head("9. 自动交易三层逻辑（打分 → 风控 → 仓位）")
    try:
        from autotrader import AutoDecider, AutoConfig, FACTOR_LABELS
        dec = AutoDecider(AutoConfig(mode="score")).decide(
            ind=ind, i=len(s.bars) - 1, position=0, fraction=0.0,
            entry_price=0.0, peak_price=0.0, drawdown=0.0,
            bars_since_exit=999, bars_held=0)
        print(f"  {OK} 动作 {dec.action}  分数 {dec.score:+.4f}  "
              f"目标仓位 {dec.target*100:.0f}%")
        print("     推理链：")
        for r in dec.reasons:
            print(f"       · {r}")
    except Exception as e:                              # noqa: BLE001
        print(f"  {FAIL} {type(e).__name__}: {str(e)[:100]}")

    # ── 10. Web 服务 ────────────────────────────────────
    head("10. Web 服务")
    import urllib.request
    for name, url in (("量化看板", "http://127.0.0.1:4400/"),
                      ("决策驾驶舱", "http://127.0.0.1:4400/decision.html"),
                      ("专业图表", "http://127.0.0.1:4400/pro.html"),
                      ("模拟盘", "http://127.0.0.1:4400/paper.html")):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                print(f"  {OK} {name:8} HTTP {r.status}  "
                      f"{len(r.read())} B")
        except Exception as e:                          # noqa: BLE001
            print(f"  {FAIL} {name:8} {type(e).__name__}")

    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
