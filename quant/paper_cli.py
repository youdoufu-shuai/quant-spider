#!/usr/bin/env python3
"""
模拟盘 CLI 桥接 —— 供 Node 服务调用。

═══════════════════════════════════════════════════════════════════
  架构（沿用项目已有分层）
═══════════════════════════════════════════════════════════════════
  Node (server.mjs)   负责 HTTP：路由、缓存、并发
  Python (本文件)     负责计算：行情、策略、账户状态

为什么不让 Python 自己起 HTTP 服务：
  项目里所有 Python 模块都是「被 Node 调用的计算单元」，
  只有 server.mjs 一个 HTTP 层。再加一个 HTTP 服务会多一个端口、
  多一套错误处理、多一处要同步的安全边界。

═══════════════════════════════════════════════════════════════════
  状态文件是唯一真相来源
═══════════════════════════════════════════════════════════════════
每次调用都是「读状态 → 执行操作 → **原子**写回」：
  · Node 不在内存维护账户（只转发），重启不丢
  · Python 每次是全新进程，也不持有状态，重启不丢
  · os.replace 原子替换，并发请求不会读到写了一半的文件

用法（每条命令输出单个 JSON 到 stdout）：
  python quant/paper_cli.py meta
  python quant/paper_cli.py list
  python quant/paper_cli.py create --market ashare --symbol sh600519
  python quant/paper_cli.py get --id <sid>
  python quant/paper_cli.py refresh --id <sid> [--limit 300]
  python quant/paper_cli.py order --id <sid> --side buy --type market [--qty 100]
  python quant/paper_cli.py cancel --id <sid> [--order <oid>]
  python quant/paper_cli.py auto --id <sid> --on [--strategy macd_trend]
  python quant/paper_cli.py reset --id <sid> [--initial 500000]
  python quant/paper_cli.py delete --id <sid>
  python quant/paper_cli.py selftest      （输出文本，不走 JSON）
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datasource import MARKETS, SYMBOL_NAMES              # noqa: E402
from strategy import STRATEGIES                            # noqa: E402
from paper_live import PaperStore, STATE_FILE, MAX_SESSIONS  # noqa: E402


def out(obj) -> None:
    """输出单个 JSON 对象。所有命令都走这里，格式统一。"""
    json.dump(obj, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


def fail(msg: str, code: int = 1) -> None:
    out({"error": msg})
    sys.exit(code)


def market_meta() -> dict:
    # 标的目录：常用项进下拉，全量（A股 6741 只 / 加密 511 对）走搜索。
    # 把 6741 项塞进 <select> 会直接卡死浏览器，所以这里只给常用的。
    try:
        import symbols as _sym
        sets = _sym.meta_sets()
    except Exception as e:
        print(f"  标的目录不可用，用内置兜底: {e}", file=sys.stderr)
        sets = {"ashare": {"popular": [], "total": 0}, "crypto": {"popular": [], "total": 0}}

    return {
        "markets": {
            k: {
                "label": v["label"],
                # 保留 symbols 字段以兼容旧调用方，但前端应优先用 symbolSets
                "symbols": [p["value"] for p in sets.get(k, {}).get("popular", [])]
                           or v["symbols"],
                "symbolSets": sets.get(k, {"popular": [], "total": 0}),
                "symbolTotal": sets.get(k, {}).get("total", 0),
                "intervals": v["intervals"],
                "defaultSymbol": v["defaultSymbol"],
                "defaultInterval": v["defaultInterval"],
                "providers": [p.label for p in v["providers"]],
                # A 股必须整手 100 股；初始资金按能买得起几手高价股给
                "lot": 100 if k == "ashare" else 1,
                "tPlus1": k == "ashare",
                "defaultInitial": 1_000_000 if k == "ashare" else 100_000,
                "commission": 0.0003 if k == "ashare" else 0.0010,
                "slippage": 0.0005,
            }
            for k, v in MARKETS.items()
        },
        "strategies": {k: v[0] for k, v in STRATEGIES.items()},
        "symbolNames": SYMBOL_NAMES,
        "stateFile": str(STATE_FILE),
        "maxSessions": MAX_SESSIONS,
        "safety": "模拟盘：只读行情，无交易接口，不产生任何真实委托",
    }


# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘 CLI（供 Node 调用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("meta")
    sub.add_parser("list")

    p_create = sub.add_parser("create")
    p_create.add_argument("--market", default="ashare", choices=list(MARKETS))
    p_create.add_argument("--symbol", default=None)
    p_create.add_argument("--interval", default=None)
    p_create.add_argument("--initial", type=float, default=None)
    p_create.add_argument("--label", default="")

    for name in ("get", "refresh", "delete"):
        p = sub.add_parser(name)
        p.add_argument("--id", required=True)
        if name == "refresh":
            p.add_argument("--limit", type=int, default=300)

    p_order = sub.add_parser("order")
    p_order.add_argument("--id", required=True)
    p_order.add_argument("--side", required=True, choices=["buy", "sell"])
    p_order.add_argument("--type", default="next_open", choices=["next_open", "market"])
    p_order.add_argument("--qty", type=float, default=None, help="省略=全仓买/全卖")
    p_order.add_argument("--reason", default="手动下单")

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--id", required=True)
    p_cancel.add_argument("--order", default=None, help="省略=全部撤单")

    p_auto = sub.add_parser("auto")
    p_auto.add_argument("--id", required=True)
    p_auto.add_argument("--on", action="store_true")
    p_auto.add_argument("--off", action="store_true")
    p_auto.add_argument("--strategy", default=None)
    p_auto.add_argument("--mode", default=None,
                        choices=["off", "score", "confirm", "signal", "ai"],
                        help="score=多因子打分（默认） confirm=多策略共识 "
                             "signal=单策略 ai=外部 AI")
    p_auto.add_argument("--config", default=None,
                        help="JSON 覆盖配置，例如 "
                             '{"entry_threshold":0.3,"risk":{"stop_loss":0.05}}')

    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--id", required=True)
    p_reset.add_argument("--initial", type=float, default=None)

    p_decide = sub.add_parser("decide", help="手动跑一次自动决策（调试用）")
    p_decide.add_argument("--id", required=True)

    p_risk = sub.add_parser("risk", help="查看/修改风控参数")
    p_risk.add_argument("--id", required=True)
    p_risk.add_argument("--config", default=None, help="JSON，例如 "
                        '{"stop_loss":0.05,"take_profit":0.25,"cooldown_bars":5}')

    sub.add_parser("selftest")

    args = ap.parse_args()

    if args.cmd == "selftest":
        import paper_live
        return paper_live._selftest()

    if args.cmd == "meta":
        out(market_meta())
        return 0

    store = PaperStore()

    try:
        if args.cmd == "list":
            out({"sessions": store.list(), "maxSessions": MAX_SESSIONS,
                 "stateFile": str(STATE_FILE)})
            return 0

        if args.cmd == "create":
            cfg = MARKETS[args.market]
            s = store.create(
                market=args.market,
                symbol=args.symbol or cfg["defaultSymbol"],
                interval=args.interval or cfg["defaultInterval"],
                initial=args.initial,
                label=args.label,
            )
            # 建会话时先拉一次行情，否则 price=0 下不了单
            info = s.pull_market()
            store.save()
            out({"session": s.snapshot(), "market": info})
            return 0

        if args.cmd == "get":
            s = store.require(args.id)
            out({"session": s.snapshot()})
            return 0

        if args.cmd == "refresh":
            s = store.require(args.id)
            info = s.pull_market(limit=args.limit)
            store.save()
            out({"session": s.snapshot(), "market": info})
            return 0

        if args.cmd == "order":
            s = store.require(args.id)
            # 下单前先同步一次行情，否则用的是过期价格
            s.pull_market()
            o = s.place_order(args.side, args.qty, args.type, reason=args.reason)
            store.save()
            out({"order": {
                "id": o.id, "side": o.side, "type": o.type, "qty": o.qty,
                "state": o.state, "fillPrice": o.fill_price, "fee": o.fee,
                "rejectReason": o.reject_reason,
            }, "session": s.snapshot()})
            return 0

        if args.cmd == "cancel":
            s = store.require(args.id)
            if args.order:
                ok = s.cancel_order(args.order)
                n = 1 if ok else 0
            else:
                n = s.cancel_all()
            store.save()
            out({"cancelled": n, "session": s.snapshot()})
            return 0

        if args.cmd == "auto":
            s = store.require(args.id)
            on = bool(args.on) and not args.off
            cfg = None
            if args.config:
                try:
                    cfg = json.loads(args.config)
                except json.JSONDecodeError as e:
                    fail(f"--config 不是合法 JSON: {e}", 3)
            res = s.set_auto(on, args.strategy, args.mode, cfg)
            store.save()
            out({**res, "session": s.snapshot()})
            return 0

        if args.cmd == "decide":
            # 手动触发一次自动决策（不改行情，只跑逻辑），用于调试与演示。
            # force=True 让它**在自动交易关闭时也能试算** ——
            # 用户想先看清楚它会怎么判断，再决定要不要开启。
            s = store.require(args.id)
            s.pull_market()
            d = s._auto_step(s.bar_index, force=True)
            store.save()
            out({"decision": d, "session": s.snapshot()})
            return 0

        if args.cmd == "risk":
            # 只改风控参数，不开关自动交易
            s = store.require(args.id)
            if not args.config:
                out({"config": s.auto_config.to_dict()})
                return 0
            try:
                cfg = json.loads(args.config)
            except json.JSONDecodeError as e:
                fail(f"--config 不是合法 JSON: {e}", 3)
            for k, v in cfg.items():
                if hasattr(s.auto_config.risk, k):
                    setattr(s.auto_config.risk, k, v)
            store.save()
            out({"config": s.auto_config.to_dict()})
            return 0

        if args.cmd == "reset":
            s = store.require(args.id)
            s.reset(args.initial)
            store.save()
            out({"session": s.snapshot()})
            return 0

        if args.cmd == "delete":
            ok = store.delete(args.id)
            out({"deleted": ok})
            return 0

    except KeyError as e:
        # KeyError 的 str() 会带上引号（str(KeyError("x")) == "'x'"），
        # 直接透出去前端会显示 "'会话不存在：...'"。这里剥掉一层。
        msg = e.args[0] if e.args else str(e)
        fail(str(msg), 2)
    except ValueError as e:
        fail(str(e), 3)
    except Exception as e:
        out({"error": f"{type(e).__name__}: {e}",
             "trace": traceback.format_exc().splitlines()[-4:]})
        return 1

    fail(f"未知命令 {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
