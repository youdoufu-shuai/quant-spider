#!/usr/bin/env python3
"""
量化数据源层 —— 多源自动降级，返回统一 OHLCV。

═══════════════════════════════════════════════════════════════════
  为什么是「多源 + 降级」而不是写死一个源
═══════════════════════════════════════════════════════════════════
本机网络实测（2026-10-16）：

  Binance 镜像  data-api.binance.vision      245ms   ✅ 稳定
  新浪财经      money.finance.sina.com.cn    122ms   ✅ 稳定
  腾讯财经      web.ifzq.gtimg.cn            246ms   ✅ 稳定
  东方财富      push2his.eastmoney.com        ——     ❌ 被封禁

东方财富一开始是好的（首次自检 7/7 通过），随后**整个 IP 被重置**：
加完整浏览器头、加 1.2s 间隔、换 push2 主机 —— 全部在 60ms 内
RemoteDisconnected。这不是限流，是 IP 级封禁。

★ 教训：单一数据源在生产里一定会出事。所以设计成链式降级 ——
  每个市场配一串来源，逐个尝试，谁先成功用谁。

═══════════════════════════════════════════════════════════════════
  ⚠️ 一个极易踩的坑：字段顺序不同
═══════════════════════════════════════════════════════════════════
  新浪 : [日期, open, high, low, close, volume]   ← 标准 OHLC 顺序
  腾讯 : [日期, open, close, high, low, volume]   ← close 在第二位！

按腾讯的顺序当 OHLC 解析，**high 和 low 会互换**，
所有基于最高/最低价的指标（ATR、唐奇安通道、布林带）以及
回测里的止损/止盈判断**全部是错的**，而且不会报错 —— 只是结果悄悄错掉。
所以两个解析器必须分开写。

用法：
  python quant/datasource.py --selftest
  python quant/datasource.py --market crypto --symbol BTCUSDT --interval 1m --limit 20
  python quant/datasource.py --market ashare --symbol sh600519 --interval 1d --limit 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Callable

# Windows 控制台默认 GBK，直接 print 中文会乱码。必须最先重设。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ============================================================
#  数据模型
# ============================================================
@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts / 1000, tz=timezone.utc)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["iso"] = self.dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
        return d


@dataclass(frozen=True)
class Series:
    source: str
    market: str
    symbol: str
    name: str
    interval: str
    bars: tuple[Bar, ...]
    fetched_at: float
    attempts: tuple[str, ...] = ()

    @property
    def closes(self) -> list[float]: return [b.close for b in self.bars]
    @property
    def highs(self) -> list[float]: return [b.high for b in self.bars]
    @property
    def lows(self) -> list[float]: return [b.low for b in self.bars]
    @property
    def opens(self) -> list[float]: return [b.open for b in self.bars]
    @property
    def volumes(self) -> list[float]: return [b.volume for b in self.bars]
    @property
    def last(self) -> Bar | None: return self.bars[-1] if self.bars else None

    def meta(self) -> dict:
        last = self.last
        return {
            "source": self.source,          # 必须暴露来源，图表上要标出来
            "market": self.market,
            "symbol": self.symbol,
            "name": self.name,
            "interval": self.interval,
            "count": len(self.bars),
            "first": self.bars[0].dt.astimezone(CST).strftime("%Y-%m-%d %H:%M") if self.bars else None,
            "last": last.dt.astimezone(CST).strftime("%Y-%m-%d %H:%M") if last else None,
            "lastClose": round(last.close, 4) if last else None,
            "fetchedAgoSec": round(time.time() - self.fetched_at, 2),
        }


class DataSourceError(RuntimeError):
    pass


# ============================================================
#  HTTP（带重试；4xx 是确定性错误不重试）
# ============================================================
def _http(url: str, timeout: float = 12.0, referer: str | None = None,
          retries: int = 2, encoding: str = "utf-8") -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout
            ) as resp:
                raw = resp.read()
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="replace")
        except urllib.error.HTTPError as e:
            raise DataSourceError(f"HTTP {e.code}") from e   # 4xx/5xx 都不重试，交给降级链
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.25 * attempt)
    raise DataSourceError(f"{type(last).__name__}: {last}")


def _json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise DataSourceError(f"非合法 JSON（前 100 字符: {text[:100]!r}）") from e


# ============================================================
#  加密：Binance（数据镜像，实测可达且稳）
# ============================================================
BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")

BINANCE_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w")


def _binance_klines(host: str, symbol: str, interval: str, limit: int) -> Series:
    if interval not in BINANCE_INTERVALS:
        raise DataSourceError(f"不支持的粒度 {interval}")
    limit = max(1, min(int(limit), 1000))
    url = f"{host}/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    rows = _json(_http(url))
    if not isinstance(rows, list) or not rows:
        raise DataSourceError("返回空或不合法")
    bars = tuple(
        Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        for r in rows
    )
    return Series("binance", "crypto", symbol.upper(), symbol.upper(), interval,
                  bars, time.time())


# ============================================================
#  A股：新浪（主）/ 腾讯（备）/ 东方财富（末位）
# ============================================================
# 新浪 scale: 5/15/30/60/240(日)；不支持 1 分钟
SINA_INTERVALS = {"5m": 5, "15m": 15, "30m": 30, "60m": 60, "1d": 240}


def _sina_kline(symbol: str, interval: str, limit: int) -> Series:
    scale = SINA_INTERVALS.get(interval)
    if scale is None:
        raise DataSourceError(f"新浪不支持 {interval}（可用 {', '.join(SINA_INTERVALS)}）")
    limit = max(1, min(int(limit), 1023))
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={limit}")
    rows = _json(_http(url, referer="https://finance.sina.com.cn"))
    if not isinstance(rows, list) or not rows:
        raise DataSourceError("返回空或不合法")

    bars: list[Bar] = []
    for r in rows:
        day = str(r.get("day", ""))
        if not day:
            continue
        # 新浪字段顺序：open, high, low, close  ← 标准顺序
        bars.append(Bar(
            ts=_parse_cst(day),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            volume=float(r.get("volume") or 0),
        ))
    if not bars:
        raise DataSourceError("解析后 0 根")
    return Series("sina", "ashare", symbol, symbol, interval, tuple(bars), time.time())


# 腾讯 param 的周期名
TENCENT_INTERVALS = {"1d": "day", "1w": "week", "1M": "month"}


def _tencent_kline(symbol: str, interval: str, limit: int) -> Series:
    period = TENCENT_INTERVALS.get(interval, "day")
    limit = max(1, min(int(limit), 800))
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},{period},,,{limit},qfq")
    d = _json(_http(url, referer="https://gu.qq.com/"))
    node = ((d or {}).get("data") or {}).get(symbol) or {}
    rows = node.get(f"qfq{period}") or node.get(period) or []
    if not rows:
        raise DataSourceError(f"无 K 线（键: {list(node)[:6]}）")

    bars: list[Bar] = []
    for r in rows:
        if len(r) < 6:
            continue
        # ⚠️ 腾讯字段顺序：open, CLOSE, HIGH, LOW, volume
        #    不是标准的 open, high, low, close。搞错会让 high/low 互换。
        bars.append(Bar(
            ts=_parse_cst(str(r[0])),
            open=float(r[1]), close=float(r[2]),
            high=float(r[3]), low=float(r[4]),
            volume=float(r[5]),
        ))
    if not bars:
        raise DataSourceError("解析后 0 根")
    name = ((node.get("qt") or {}).get(symbol) or [None, symbol])[1]
    return Series("tencent", "ashare", symbol, str(name), interval, tuple(bars), time.time())


EASTMONEY_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1d": 101, "1w": 102}


def _eastmoney_kline(symbol: str, interval: str, limit: int) -> Series:
    klt = EASTMONEY_KLT.get(interval)
    if klt is None:
        raise DataSourceError(f"不支持 {interval}")
    limit = max(1, min(int(limit), 1000))
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={symbol}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56&klt={klt}&fqt=1&end=20500101&lmt={limit}")
    d = _json(_http(url, referer="https://quote.eastmoney.com/"))
    data = (d or {}).get("data") or {}
    rows = data.get("klines") or []
    if not rows:
        raise DataSourceError("无 K 线")
    bars: list[Bar] = []
    for line in rows:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        # 东财顺序：时间, open, close, high, low, volume  ← 也是 close 在第二位
        bars.append(Bar(_parse_cst(p[0]), float(p[1]), float(p[3]),
                        float(p[4]), float(p[2]), float(p[5])))
    if not bars:
        raise DataSourceError("解析后 0 根")
    return Series("eastmoney", "ashare", symbol, str(data.get("name") or symbol),
                  interval, tuple(bars), time.time())


def _parse_cst(text: str) -> int:
    """支持 'YYYY-MM-DD' 与 'YYYY-MM-DD HH:MM[:SS]'，均按北京时间。"""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=CST).timestamp() * 1000)
        except ValueError:
            continue
    raise DataSourceError(f"无法解析时间 {text!r}")


# ============================================================
#  市场配置：每个市场一串来源，按顺序降级
# ============================================================
@dataclass(frozen=True)
class Provider:
    label: str
    fn: Callable[[str, str, int], Series]


MARKETS: dict[str, dict] = {
    "crypto": {
        "label": "加密货币",
        "defaultSymbol": "BTCUSDT",
        "defaultInterval": "1m",
        "providers": [
            Provider("Binance 镜像", lambda s, i, l: _binance_klines(BINANCE_HOSTS[0], s, i, l)),
            Provider("Binance 主站", lambda s, i, l: _binance_klines(BINANCE_HOSTS[1], s, i, l)),
        ],
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"],
        "intervals": list(BINANCE_INTERVALS),
    },
    "ashare": {
        "label": "A股 / 指数",
        "defaultSymbol": "sh000001",
        "defaultInterval": "1d",
        "providers": [
            Provider("新浪财经", _sina_kline),
            Provider("腾讯财经", _tencent_kline),
            Provider("东方财富", _eastmoney_kline),
        ],
        "symbols": [
            "sh000001",  # 上证指数
            "sz399001",  # 深证成指
            "sz399006",  # 创业板指
            "sh000300",  # 沪深300
            "sh600519",  # 贵州茅台
            "sz000001",  # 平安银行
            "sh601318",  # 中国平安
            "sh600036",  # 招商银行
        ],
        "intervals": ["5m", "15m", "30m", "60m", "1d", "1w"],
    },
}

# 常用标的的中文名（接口不稳时兜底显示）
SYMBOL_NAMES = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
    "sh000300": "沪深300", "sh600519": "贵州茅台", "sz000001": "平安银行",
    "sh601318": "中国平安", "sh600036": "招商银行",
    "BTCUSDT": "比特币", "ETHUSDT": "以太坊", "SOLUSDT": "Solana",
    "BNBUSDT": "BNB", "XRPUSDT": "XRP", "DOGEUSDT": "狗狗币",
}


def fetch(market: str, symbol: str, interval: str, limit: int = 200,
          on_attempt: Callable[[str, str], None] | None = None) -> Series:
    """
    按降级链取数。全部失败才抛错，并把每次尝试都记在 Series.attempts 里，
    这样面板可以显示「当前用的是哪个源」以及「哪些源挂了」。
    """
    cfg = MARKETS.get(market)
    if cfg is None:
        raise DataSourceError(f"未知市场 {market}，可用: {', '.join(MARKETS)}")

    attempts: list[str] = []
    errors: list[str] = []
    for p in cfg["providers"]:
        if on_attempt:
            on_attempt(p.label, "try")
        t0 = time.time()
        try:
            s = p.fn(symbol, interval, limit)
            ms = int((time.time() - t0) * 1000)
            attempts.append(f"{p.label}:ok({ms}ms)")
            if on_attempt:
                on_attempt(p.label, f"ok {ms}ms")
            name = s.name if (s.name and s.name != symbol) else SYMBOL_NAMES.get(symbol, symbol)
            return Series(s.source, market, symbol, name, interval,
                          s.bars, s.fetched_at, tuple(attempts))
        except Exception as e:
            attempts.append(f"{p.label}:fail({type(e).__name__})")
            errors.append(f"{p.label} → {e}")
            if on_attempt:
                on_attempt(p.label, f"失败 {type(e).__name__}")
            continue

    raise DataSourceError(
        f"{market}/{symbol}/{interval} 所有数据源均失败:\n  " + "\n  ".join(errors))


def quote(market: str, symbol: str) -> dict:
    """实时快照。目前只做新浪 + Binance，够面板顶部用。"""
    if market == "crypto":
        d = _json(_http(f"{BINANCE_HOSTS[0]}/api/v3/ticker/24hr?symbol={symbol.upper()}"))
        # ⚠️ 必须返回与 A 股分支**相同的键集**。
        #    之前这里漏了 open/prevClose/change，前端直接读这三个字段，
        #    于是加密行显示「今开 — / 昨收 —」，涨跌行是「▲ — (+1.86%)」——
        #    数字对但符号后面跟个破折号，看起来像坏了（实测）。
        #    ticker/24hr 本身就提供 openPrice/prevClosePrice/priceChange，补上即可。
        return {
            "symbol": d.get("symbol"), "name": SYMBOL_NAMES.get(symbol.upper(), symbol),
            "open": float(d.get("openPrice", 0)),
            "prevClose": float(d.get("prevClosePrice", 0)),
            "last": float(d.get("lastPrice", 0)),
            "high": float(d.get("highPrice", 0)),
            "low": float(d.get("lowPrice", 0)),
            "change": float(d.get("priceChange", 0)),
            "changePct": float(d.get("priceChangePercent", 0)),
            "volume": float(d.get("volume", 0)),
            "amount": float(d.get("quoteVolume", 0)),
            "source": "binance",
        }
    # A股：新浪快照，逗号分隔字段
    text = _http(f"https://hq.sinajs.cn/list={symbol}", referer="https://finance.sina.com.cn")
    seg = text.split('"')
    if len(seg) < 2 or not seg[1]:
        raise DataSourceError(f"新浪快照无数据: {text[:80]!r}")
    f = seg[1].split(",")
    if len(f) < 10:
        raise DataSourceError(f"快照字段不足: {len(f)}")
    last, prev = float(f[3]), float(f[2])
    return {
        "symbol": symbol, "name": f[0],
        "open": float(f[1]), "prevClose": prev,
        "last": last, "high": float(f[4]), "low": float(f[5]),
        "change": round(last - prev, 3),
        "changePct": round((last - prev) / prev * 100, 3) if prev else 0.0,
        "volume": float(f[8]), "amount": float(f[9]),
        "source": "sina",
    }


# ============================================================
#  CLI
# ============================================================
def _selftest() -> int:
    print("=" * 74)
    print("  数据源自检 —— 全部真实接口，含降级链验证")
    print("=" * 74)
    cases = [
        ("crypto", "BTCUSDT", "1m", 5),
        ("crypto", "BTCUSDT", "1d", 5),
        ("crypto", "ETHUSDT", "1h", 5),
        ("ashare", "sh000001", "1d", 5),
        ("ashare", "sh600519", "1d", 5),
        ("ashare", "sh600519", "60m", 5),
        ("ashare", "sz399006", "1d", 5),
    ]
    ok = fail = 0
    for market, sym, iv, lim in cases:
        trace: list[str] = []
        t0 = time.time()
        try:
            s = fetch(market, sym, iv, lim, on_attempt=lambda l, r: trace.append(f"{l}={r}"))
            ms = int((time.time() - t0) * 1000)
            m = s.meta()
            print(f"\n  [{market}] {sym} {iv}   {ms}ms   源={m['source']}")
            print(f"      {m['name']}  {m['count']} 根  {m['first']} → {m['last']}  收={m['lastClose']}")
            for b in s.bars[-2:]:
                print(f"        {b.as_dict()['iso']}  O={b.open:<11.2f} H={b.high:<11.2f} "
                      f"L={b.low:<11.2f} C={b.close:<11.2f} V={b.volume:.0f}")
            print(f"      降级链: {' | '.join(trace)}")
            ok += 1
        except Exception as e:
            print(f"\n  [{market}] {sym} {iv}   失败\n      {e}")
            fail += 1

    print("\n" + "=" * 74)
    print(f"  自检: {ok} 成功 / {fail} 失败")
    print("=" * 74)
    return 1 if fail else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="量化数据源（只读行情）")
    ap.add_argument("--market", default="crypto", choices=list(MARKETS))
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--quote", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出标的与粒度")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.list:
        for k, v in MARKETS.items():
            print(f"\n{k}  ({v['label']})  默认 {v['defaultSymbol']}")
            print(f"  标的: {', '.join(v['symbols'])}")
            print(f"  粒度: {', '.join(v['intervals'])}")
            print(f"  来源: {' → '.join(p.label for p in v['providers'])}")
        return 0

    cfg = MARKETS[args.market]
    symbol = args.symbol or cfg["defaultSymbol"]
    interval = args.interval or cfg["defaultInterval"]

    try:
        if args.quote:
            print(json.dumps(quote(args.market, symbol), ensure_ascii=False, indent=2))
            return 0
        trace: list[str] = []
        s = fetch(args.market, symbol, interval, args.limit,
                  on_attempt=lambda l, r: trace.append(f"{l}={r}"))
    except DataSourceError as e:
        print(f"取数失败: {e}", file=sys.stderr)
        return 1

    print(json.dumps(s.meta(), ensure_ascii=False, indent=2))
    print(f"降级链: {' | '.join(trace)}")
    print(f"\n最近 {min(5, len(s.bars))} 根:")
    for b in s.bars[-5:]:
        print(f"  {b.as_dict()['iso']}  O={b.open:<11.2f} H={b.high:<11.2f} "
              f"L={b.low:<11.2f} C={b.close:<11.2f} V={b.volume:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
