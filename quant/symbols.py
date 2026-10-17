#!/usr/bin/env python3
"""
标的目录 —— 全量列表、搜索、名称解析。

═══════════════════════════════════════════════════════════════════
  为什么需要这个模块
═══════════════════════════════════════════════════════════════════
原来 MARKETS 里给每个市场写死了 8 个标的（A 股 8 个、加密 6 个）。
可实际上 A 股有 **5923 只**、Binance 有 **511 个 USDT 交易对** ——
用户想找「宁德时代」或「ADAUSDT」根本找不到，只能改代码。

所以这里把「标的」从代码里拿出来，变成**可查询的数据**：
  · A 股    走东方财富 clist 接口，一次拿到全部 5923 只（含中文名），78ms
  · 加密    走 Binance exchangeInfo，511 个 USDT 对，10.4s / 17MB
  · 搜索    支持代码与中文名模糊匹配
  · 缓存    落盘 JSON。全量拉取很贵（加密 17MB），不能每次都拉。

═══════════════════════════════════════════════════════════════════
  实测确认的事实（2026-10-16）
═══════════════════════════════════════════════════════════════════
  东方财富 clist/list     ✅ 可用   5923 只 / 78ms      ← 只用它取「列表」
  东方财富 push2his/kline ❌ 被封   RemoteDisconnected  ← 所以行情仍走新浪
  新浪 K线                ✅ 可用   任意代码 / ~130ms
  新浪快照                ✅ 可用   可取名称 / 131ms
  Binance exchangeInfo    ✅ 可用   3689 对 / 511 USDT / 10.4s

注意：列表接口和行情接口是**不同的域名**（push2 vs push2his），
所以「东财能用」和「东财被封」都对 —— 取决于具体端点。别一概而论。

用法：
  python quant/symbols.py --list ashare            # 前 50 个
  python quant/symbols.py --list ashare --all      # 全部
  python quant/symbols.py --search 宁德
  python quant/symbols.py --search ada
  python quant/symbols.py --resolve sz300750
  python quant/symbols.py --selftest
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "state"
CACHE_FILE = CACHE_DIR / "symbols-cache.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 全量列表的缓存时长。
#
# ⚠️ 这里改过：原来是 12 小时，实测是个**危险的脆弱点**。
#    重新枚举要 70 秒（51997 个代码逐个探测），而 12 小时就判定过期。
#    结果：每天至少有两次「过期期间来的第一个请求」会阻塞 70 秒，
#    超过 HTTP 超时（30 秒）→ 返回 500 → 依赖它的页面（专业图表、
#    模拟盘）全部卡在「加载中」。
#
#    两个修复：
#      1. TTL 提到 7 天 —— A 股一周内新增几只新股，列表长度变化
#         在万分之一级别，对选股/看图毫无影响。
#      2. 见 fetch_ashare_list：缓存只要还在（90 天内），
#         就先用旧的返回，不阻塞。要强制刷新用 force=True。
CACHE_TTL_SEC = 7 * 24 * 3600
# 超过这个年龄才认为缓存彻底不可用（宁愿枚举也不返回上古数据）
CACHE_STALE_MAX_SEC = 90 * 24 * 3600


# ============================================================
#  HTTP
# ============================================================
def _get(url: str, timeout: float = 25.0, referer: str | None = None,
         retries: int = 2) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from e
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.3 * attempt)
    raise RuntimeError(f"{type(last).__name__}: {last}")


# ============================================================
#  缓存
# ============================================================
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text("utf8"))
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        print(f"  写标的缓存失败（忽略）: {e}", file=sys.stderr)


def _fresh(entry: dict | None) -> bool:
    """
    缓存是否新鲜可用。

    ⚠️ 必须同时要求 items 非空。只判断 `bool(entry)` 是不够的 ——
       `{"at": ..., "items": []}` 本身是真值，会被当成「新鲜」，
       于是返回空列表且**永远不去枚举**，页面上一只股票都没有。
       实测踩过（写了个空缓存的情况）。
    """
    return (bool(entry)
            and bool(entry.get("items"))
            and (time.time() - entry.get("at", 0)) < CACHE_TTL_SEC)


# ============================================================
#  A 股：新浪批量枚举
# ============================================================
def _to_sina_code(code: str) -> str:
    """
    6 位代码 -> 新浪代码（带市场前缀）。
    新浪前缀规则：sh=沪市, sz=深市, bj=北交所
    """
    code = code.strip().lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


# ⚠️ 为什么不用东方财富的 clist 接口
#    它确实能一次返回 5923 只（含中文名，78ms），我也实测成功过。
#    但**几分钟后整个 IP 就被封了**：push2 / 82.push2 / push2delay / push2his
#    四个主机、三种请求头组合、http 与 https 全部 RemoteDisconnected，
#    连打 5 次全失败，至今未恢复。
#    东财的 K 线接口更早就封了。所以东财在本项目里是**不可依赖**的。
#
# ✅ 改用新浪快照批量枚举：
#    一次最多查 ~120 个代码，返回「代码,名称,今开,昨收,现价,...」。
#    所以一次请求就能同时完成「发现存在的代码」和「拿到中文名」两件事。
#    实测 38999 个候选、72 秒、0 失败，找到 6741 只在市 A 股。
#
#    代价：这是一次**昂贵的枚举**（1~2 分钟），必须落盘缓存。
#    所以它只在缓存缺失/过期/显式 --refresh 时才跑。

SINA_QUOTE = "https://hq.sinajs.cn/list="

# 候选代码段：(前缀, 起, 止)
#
# ⚠️ 指数段必须单独列出来。
#    上证指数是 sh000001，落在 sh 000000-000999 —— 不在 sh600000-605999 里。
#    深证成指是 sz399001，落在 sz 399001-399999 —— 也不在 sz 0001-3999 里。
#    实测踩过：漏了这两段，下拉里指数就显示成光秃秃的代码（因为查不到中文名）。
ASHARE_RANGES: list[tuple[str, int, int]] = [
    # 指数
    ("sh", 1, 999),           # 上证系列指数（上证指数 sh000001 等）
    ("sz", 399001, 399999),   # 深证系列指数（深证成指 sz399001、创业板指 sz399006）
    ("sh", 950000, 950999),   # 中证系列
    # 股票
    ("sh", 600000, 605999),   # 沪市主板
    ("sh", 688000, 689999),   # 科创板
    ("sz", 1, 3999),          # 深市主板（000001 平安银行、002594 比亚迪）
    ("sz", 300000, 301999),   # 创业板
    # 基金 / ETF
    ("sh", 510000, 512999),
    ("sh", 513000, 513999),   # 跨境 ETF
    ("sh", 515000, 515999),
    ("sh", 516000, 516999),
    ("sh", 517000, 517999),
    ("sh", 518000, 518999),   # 黄金 ETF
    ("sh", 560000, 563999),
    ("sh", 588000, 588999),   # 科创板 ETF（588000 科创50ETF）
    ("sz", 159000, 159999),   # 深市 ETF
    # 北交所
    ("bj", 430000, 439999),
    ("bj", 830000, 839999),
    ("bj", 920000, 920999),
]

ENUM_BATCH = 120          # 实测 120 可用；再大响应会变慢
ENUM_SLEEP = 0.10         # 批间隔。0.1s 实测 0 失败；再快怕触发限流


def _sina_quote_batch(codes: list[str], timeout: float = 25.0) -> str:
    return _get(SINA_QUOTE + ",".join(codes), timeout=timeout,
                referer="https://finance.sina.com.cn")


def enumerate_ashare(progress=None) -> list[dict]:
    """
    枚举全量 A 股（约 72 秒）。

    返回 [{code(带前缀), plain(6位), name, price}]，**只含在市标的**。
    过滤规则：新股与退市股在快照里价格是 0.000，直接剔除 ——
    否则用户会选到一堆永远没有行情的死代码。
    """
    found: dict[str, dict] = {}
    total = sum(b - a + 1 for _, a, b in ASHARE_RANGES)
    done = 0
    failed = 0

    for pref, lo, hi in ASHARE_RANGES:
        for start in range(lo, hi + 1, ENUM_BATCH):
            chunk = [f"{pref}{i:06d}" for i in range(start, min(start + ENUM_BATCH, hi + 1))]
            try:
                txt = _sina_quote_batch(chunk)
            except Exception:
                failed += 1
                done += len(chunk)
                continue
            for line in txt.split("\n"):
                if '="' not in line:
                    continue
                try:
                    code = line.split("=")[0].replace("var hq_str_", "").strip()
                    body = line.split('"')[1]
                    if not body:
                        continue
                    parts = body.split(",")
                    name = parts[0].strip()
                    if not name:
                        continue
                    price = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
                    if price <= 0:
                        continue          # 退市 / 长期停牌
                    found[code] = {
                        "code": code, "plain": code[2:], "name": name,
                        "price": price,
                        "changePct": (round((price / float(parts[2]) - 1) * 100, 2)
                                      if len(parts) > 2 and parts[2] and float(parts[2]) > 0
                                      else None),
                    }
                except Exception:
                    continue
            done += len(chunk)
            if progress and done % 2400 < ENUM_BATCH:
                progress(done, total, len(found))
            time.sleep(ENUM_SLEEP)

    return sorted(found.values(), key=lambda x: x["code"])


def fetch_ashare_list(force: bool = False) -> list[dict]:
    """
    取 A 股全量列表（优先缓存）。

    返回项统一为 {code, sina, plain, name, price, changePct}，
    其中 code 与 sina 都是带前缀代码（保持与旧调用方兼容）。
    """
    cache = _load_cache()
    entry = cache.get("ashare2")
    if not force and _fresh(entry):
        return entry["items"]

    # ⚠️ 缓存过期但还在 → **先用旧数据返回，不阻塞**。
    #
    #    为什么：重新枚举要 70 秒，而 API 超时是 30 秒。
    #    如果这里死等，请求会 500，页面卡在「加载中」（实测踩过：
    #    专业图表和模拟盘都因此打不开）。
    #    旧的股票列表（几天前的）对看图/选股完全够用 ——
    #    A 股一周新增几只新股，不影响任何现有代码的查询。
    #
    #    想强制刷新：fetch_ashare_list(force=True)，
    #    或删 quant/state/symbols-cache.json 后重启。
    if (not force and entry and entry.get("items")
            and time.time() - entry.get("at", 0) < CACHE_STALE_MAX_SEC):
        age_d = (time.time() - entry.get("at", 0)) / 86400
        print(f"  ⚠ 股票列表缓存已 {age_d:.1f} 天"
              f"（TTL {CACHE_TTL_SEC/86400:.0f} 天），先用旧数据"
              f"（{len(entry['items'])} 只）", file=sys.stderr)
        return entry["items"]

    print("  正在枚举全量 A 股（约 70 秒，只需一次）…", file=sys.stderr)

    def prog(done, total, n):
        print(f"    {done}/{total}  已找到 {n} 只", file=sys.stderr)

    items = enumerate_ashare(progress=prog)
    if items:
        norm = [{
            "code": i["code"], "sina": i["code"], "plain": i["plain"],
            "name": i["name"], "price": i["price"], "changePct": i.get("changePct"),
        } for i in items]
        cache["ashare2"] = {"at": time.time(), "items": norm}
        _save_cache(cache)
        return norm
    return FALLBACK["ashare"]


# ============================================================
#  加密：Binance exchangeInfo（511 个 USDT 对，17MB / 10.4s）
# ============================================================
BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")

# 常见币种中文名（全量列表里没有中文，这里给常用的一些）
COIN_NAMES = {
    "BTC": "比特币", "ETH": "以太坊", "BNB": "BNB", "SOL": "Solana",
    "XRP": "瑞波币", "ADA": "艾达币", "DOGE": "狗狗币", "TRX": "波场",
    "DOT": "波卡", "MATIC": "Polygon", "LTC": "莱特币", "SHIB": "柴犬币",
    "AVAX": "雪崩", "LINK": "Chainlink", "ATOM": " cosmos", "UNI": "Uniswap",
    "XLM": "恒星币", "ETC": "以太经典", "FIL": "Filecoin", "APT": "Aptos",
    "ARB": "Arbitrum", "OP": "Optimism", "SUI": "Sui", "NEAR": "NEAR",
    "ICP": "ICP", "INJ": "Injective", "TIA": "Celestia", "SEI": "Sei",
    "PEPE": "Pepe", "WIF": "dogwifhat", "BONK": "Bonk", "ORDI": "Ordinals",
    "TON": "Toncoin", "BCH": "比特现金", "EOS": "柚子币", "NEO": "小蚁",
    "VET": "唯链", "ALGO": "Algorand", "FTM": "Fantom", "GRT": "The Graph",
}


def fetch_crypto_list(force: bool = False) -> list[dict]:
    """
    拉 Binance 全量 USDT 现货对。
    ⚠️ 响应 17MB / 10.4 秒 —— 必须缓存，绝不能每次请求都拉。
    """
    cache = _load_cache()
    centry = cache.get("crypto")
    if not force and _fresh(centry):
        return centry["items"]
    # 同 A 股：过期缓存先用着，不阻塞（加密只有 511 个交易对，变更更少）
    if (not force and centry and centry.get("items")
            and time.time() - centry.get("at", 0) < CACHE_STALE_MAX_SEC):
        return centry["items"]

    items: list[dict] = []
    last_err: Exception | None = None
    for host in BINANCE_HOSTS:
        try:
            d = json.loads(_get(f"{host}/api/v3/exchangeInfo?permissions=SPOT",
                                timeout=40))
            for s in d.get("symbols", []):
                if s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                    continue
                base = s.get("baseAsset", "")
                items.append({
                    "symbol": s.get("symbol"),
                    "base": base,
                    "name": COIN_NAMES.get(base, base),
                })
            break
        except Exception as e:
            last_err = e
            continue

    if not items and last_err:
        raise RuntimeError(f"Binance 全量列表失败: {last_err}")

    if items:
        cache["crypto"] = {"at": time.time(), "items": items}
        _save_cache(cache)
    return items


# ============================================================
#  仅供展示的名字查询 —— **绝不联网**
# ============================================================
def cached_name(market: str, value: str) -> str | None:
    """
    只从**缓存**里查名字，查不到返回 None。**不会触发任何网络请求。**

    ═══════════════════════════════════════════════════════════════
      为什么必须单独有这么一个函数
    ═══════════════════════════════════════════════════════════════
    `display_name()` 走 `resolve()` → `_ashare_index()`，而后者在缓存缺失时
    会**枚举 51997 个代码、耗时约 70 秒**。

    对「取个名字显示一下」这种需求，这是灾难性的：
      · 它把纯展示变成了阻塞网络调用
      · 干净检出（CI、新克隆的仓库）必然没有缓存 → 必然触发
      · 实测就是这么把 CI 拖挂的：一个 self-test 调了 paper_live.symbol_name()，
        本地有缓存 0.2 秒过，CI 上无缓存 → 70 秒 → 120 秒超时

    调用方要的是「能查到就显示中文名，查不到就显示代码」——
    这**不需要**全量列表。缓存里有就直接用，没有就拉倒。
    需要全量列表的场景（搜索、下拉）自己走 resolve()。
    """
    try:
        cache = _load_cache()
    except Exception:                                   # noqa: BLE001
        return None
    if not cache:
        return None

    v = (value or "").strip()
    if not v:
        return None

    try:
        if market == "ashare":
            entry = cache.get("ashare2") or cache.get("ashare")
            items = entry.get("items") if isinstance(entry, dict) else entry
            if not items:
                return None
            sina = _to_sina_code(v)
            plain = v.lstrip("shszbj")
            for i in items:
                if i.get("sina") == sina or i.get("plain") == plain:
                    n = i.get("name")
                    # 兜底列表里的 name 就是代码本身，那不算"查到名字"
                    return n if n and n != sina else None
            return None

        entry = cache.get("crypto")
        items = entry.get("items") if isinstance(entry, dict) else entry
        if not items:
            return None
        sym = v.upper()
        for i in items:
            if i.get("symbol") == sym:
                n = i.get("name")
                return n if n and n != sym else None
    except Exception:                                   # noqa: BLE001
        return None
    return None


# ============================================================
#  统一目录
# ============================================================
# 各市场在「快速下拉」里优先展示的常用标的。
# 全量列表留给搜索用 —— 把 5923 项塞进 <select> 会卡死浏览器。
ASHARE_POPULAR = [
    # 指数
    "sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sz399905", "sh000688",
    # 权重蓝筹
    "sh600519", "sz300750", "sh601318", "sh600036", "sz000858", "sh601899",
    "sh600900", "sz002594", "sh688981", "sz000001", "sh601166", "sh600030",
    "sh601012", "sz002415", "sh600276", "sz300059", "sh601888", "sh600887",
    "sz002304", "sh601398", "sh601288", "sh601857", "sh600028", "sh601088",
    # ETF
    "sh510300", "sh510500", "sz159915", "sh588000", "sh512880",
]

CRYPTO_POPULAR = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "MATICUSDT",
    "LTCUSDT", "BCHUSDT", "ATOMUSDT", "UNIUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "NEARUSDT", "INJUSDT", "TIAUSDT", "SEIUSDT",
    "PEPEUSDT", "WIFUSDT", "TONUSDT", "FILUSDT", "ETCUSDT", "XLMUSDT",
]

# 内置兜底：缓存为空且网络不通时至少还能用
FALLBACK = {
    "ashare": [{"code": s, "sina": s, "plain": s[2:], "name": s,
                "price": None, "changePct": None} for s in ASHARE_POPULAR],
    "crypto": [{"symbol": s, "base": s[:-4], "name": COIN_NAMES.get(s[:-4], s[:-4])}
               for s in CRYPTO_POPULAR],
}


def _ashare_index() -> list[dict]:
    try:
        items = fetch_ashare_list()
        return items or FALLBACK["ashare"]
    except Exception as e:
        print(f"  A股全量列表不可用，用内置兜底: {e}", file=sys.stderr)
        return FALLBACK["ashare"]


def _crypto_index() -> list[dict]:
    try:
        items = fetch_crypto_list()
        return items or FALLBACK["crypto"]
    except Exception as e:
        print(f"  加密全量列表不可用，用内置兜底: {e}", file=sys.stderr)
        return FALLBACK["crypto"]


def meta_sets() -> dict:
    """
    给前端 init 用的数据：常用标的（快速下拉）+ 全量数量。
    刻意**不返回全量**（A 股 5923 项会让 <select> 卡住），
    全量交给 search()。
    """
    out = {"ashare": {"popular": [], "total": 0}, "crypto": {"popular": [], "total": 0}}

    a = {}
    for i in _ashare_index():
        a[i["sina"]] = i
        a.setdefault(i.get("plain") or i["sina"][2:], i)
    out["ashare"]["total"] = len({i["sina"] for i in a.values()})
    for s in ASHARE_POPULAR:
        i = a.get(s)
        out["ashare"]["popular"].append({
            "value": s, "name": (i["name"] if i else s), "code": s[2:],
        })

    c = {i["symbol"]: i for i in _crypto_index()}
    out["crypto"]["total"] = len(c)
    for s in CRYPTO_POPULAR:
        i = c.get(s)
        out["crypto"]["popular"].append({
            "value": s, "name": (i["name"] if i else s),
        })
    return out


def search(market: str, query: str, limit: int = 40) -> list[dict]:
    """
    搜索标的。支持：
      · 中文名模糊      「宁德」→ 宁德时代
      · 代码片段        「300750」「3007」
      · 带前缀代码      「sz300750」
      · 币种            「ada」→ ADAUSDT
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    limit = max(1, min(int(limit), 200))

    if market == "ashare":
        items = _ashare_index()
        hits = []
        for i in items:
            code = i.get("plain") or i["code"][2:]
            sina = i["sina"]
            name = i["name"]
            qs = q.lstrip("shszbj") if q[:2] in ("sh", "sz", "bj") else q
            if (qs in code.lower() or q in sina.lower() or q in name.lower()):
                # 排序权重：代码精确 > 代码前缀 > 名称前缀 > 名称包含 > 代码包含
                score = 4
                if code == qs or sina == q:
                    score = 0
                elif code.startswith(qs) or sina.startswith(q):
                    score = 1
                elif name.lower().startswith(q):
                    score = 2
                elif q in name.lower():
                    score = 3
                hits.append((score, i))
        hits.sort(key=lambda x: (x[0], x[1]["code"]))
        return [{
            "value": i["sina"], "code": i.get("plain") or i["sina"][2:],
            "name": i["name"], "price": i.get("price"),
            "changePct": i.get("changePct"), "market": "ashare",
        } for _, i in hits[:limit]]

    items = _crypto_index()
    hits = []
    for i in items:
        sym = i["symbol"]
        base = i["base"]
        name = i["name"]
        if q in sym.lower() or q in base.lower() or q in name.lower():
            score = 3
            if base.lower() == q or sym.lower() == q:
                score = 0
            elif base.lower().startswith(q):
                score = 1
            elif q in name.lower():
                score = 2
            hits.append((score, i))
    hits.sort(key=lambda x: (x[0], x[1]["symbol"]))
    return [{
        "value": i["symbol"], "code": i["symbol"], "name": i["name"],
        "market": "crypto",
    } for _, i in hits[:limit]]


def resolve(market: str, value: str) -> dict | None:
    """把一个标的标识解析成 {value, code, name}。找不到返回 None。"""
    v = (value or "").strip()
    if not v:
        return None
    if market == "ashare":
        sina = _to_sina_code(v)
        for i in _ashare_index():
            if i["sina"] == sina or i.get("plain") == v.lstrip("shszbj"):
                return {"value": i["sina"],
                        "code": i.get("plain") or i["sina"][2:],
                        "name": i["name"]}
        # 未知代码也放行 —— 新浪对任意代码都能取，用户可能知道我们没枚举到的
        return {"value": sina, "code": sina[2:], "name": sina}
    sym = v.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    for i in _crypto_index():
        if i["symbol"] == sym:
            return {"value": i["symbol"], "code": i["symbol"], "name": i["name"]}
    return {"value": sym, "code": sym, "name": sym}


def display_name(market: str, value: str) -> str:
    """取显示名。查不到就返回原值 —— 不要编造名字。"""
    r = resolve(market, value)
    return r["name"] if r else value


def counts() -> dict:
    """各市场标的数量（可能触发一次全量拉取，之后走缓存）。"""
    return {
        "ashare": len(_ashare_index()),
        "crypto": len(_crypto_index()),
    }


# ============================================================
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

    print("=" * 74)
    print("  标的目录自检（会访问真实接口）")
    print("=" * 74)

    print("\n[1] 代码前缀映射")
    check("600519 -> sh", _to_sina_code("600519") == "sh600519")
    check("300750 -> sz", _to_sina_code("300750") == "sz300750")
    check("000001 -> sz", _to_sina_code("000001") == "sz000001")
    check("688981 -> sh", _to_sina_code("688981") == "sh688981")
    check("430047 -> bj", _to_sina_code("430047") == "bj430047")
    check("已带前缀原样返回", _to_sina_code("sh600519") == "sh600519")

    print("\n[2] A 股全量列表")
    try:
        a = fetch_ashare_list()
        check("数量 > 5000", len(a) > 5000, f"{len(a)} 只")
        check("含中文名", any("\u4e00" <= ch <= "\u9fff" for ch in (a[0]["name"] or "")),
              a[0]["name"] if a else "—")
        check("每项有 code 与 sina",
              all(i["code"] and i["sina"] for i in a[:50]))
        mout = next((i for i in a if (i.get("plain") or i["code"][2:]) == "600519"), None)
        check("能查到 600519", mout is not None,
              f"{mout['name']} -> {mout['sina']}" if mout else "未找到")
    except Exception as e:
        check("A 股列表可用", False, f"{type(e).__name__}: {e}")

    print("\n[3] 加密全量列表")
    try:
        c = fetch_crypto_list()
        check("数量 > 400", len(c) > 400, f"{len(c)} 个 USDT 对")
        check("都是 USDT 计价", all(i["symbol"].endswith("USDT") for i in c[:50]))
        check("能查到 BTCUSDT", any(i["symbol"] == "BTCUSDT" for i in c))
    except Exception as e:
        check("加密列表可用", False, f"{type(e).__name__}: {e}")

    print("\n[4] 搜索")
    r = search("ashare", "宁德")
    check("中文名搜索命中", len(r) > 0 and "宁德" in r[0]["name"],
          r[0]["name"] if r else "无结果")
    r = search("ashare", "300750")
    check("代码搜索命中", len(r) > 0 and r[0]["code"] == "300750",
          f"{r[0]['name']} ({r[0]['code']})" if r else "无结果")
    r = search("ashare", "茅台")
    check("茅台能搜到", len(r) > 0 and "茅台" in r[0]["name"],
          r[0]["name"] if r else "无结果")
    r = search("crypto", "ada")
    check("币种搜索命中", len(r) > 0 and r[0]["value"] == "ADAUSDT",
          r[0]["value"] if r else "无结果")
    r = search("crypto", "比特币")
    check("币种中文名命中 BTC", len(r) > 0 and r[0]["value"] == "BTCUSDT",
          r[0]["value"] if r else "无结果")

    print("\n[5] 解析与显示名")
    r = resolve("ashare", "sz300750")
    check("resolve 给出中文名", r and r["name"] and r["name"] != "sz300750",
          f"{r['name']}" if r else "—")
    r = resolve("crypto", "btc")
    check("crypto 自动补 USDT", r and r["value"] == "BTCUSDT", r["value"] if r else "—")
    check("未知代码不编造名字", resolve("ashare", "sz999999")["name"] == "sz999999")

    print("\n[6] meta_sets 只返回常用项")
    m = meta_sets()
    check("ashare popular 有内容", len(m["ashare"]["popular"]) > 20,
          f"{len(m['ashare']['popular'])} 项")
    check("ashare total 是全量", m["ashare"]["total"] > 5000, f"{m['ashare']['total']}")
    check("popular 远小于 total（不塞爆下拉）",
          len(m["ashare"]["popular"]) < m["ashare"]["total"] / 50)

    print("\n[7] 缓存生效（第二次应很快）")
    t0 = time.time()
    fetch_ashare_list()
    dt = (time.time() - t0) * 1000
    check("第二次 < 50ms（走缓存）", dt < 50, f"{dt:.0f}ms")

    print("\n" + "=" * 74)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 74)
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="标的目录")
    ap.add_argument("--list", choices=["ashare", "crypto"])
    ap.add_argument("--all", action="store_true", help="列出全部（默认只列 50）")
    ap.add_argument("--search", default=None)
    ap.add_argument("--limit", type=int, default=40, help="搜索结果上限")
    ap.add_argument("--market", default="ashare", choices=["ashare", "crypto"])
    ap.add_argument("--resolve", default=None)
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    ap.add_argument("--meta", action="store_true")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供 Node 调用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.meta:
        print(json.dumps(meta_sets(), ensure_ascii=False, indent=2))
        return 0

    if args.counts:
        print(json.dumps(counts(), ensure_ascii=False, indent=2))
        return 0

    if args.resolve:
        print(json.dumps(resolve(args.market, args.resolve), ensure_ascii=False, indent=2))
        return 0

    if args.search:
        hits = search(args.market, args.search, limit=args.limit)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False))
            return 0
        for r in hits:
            extra = f"  {r.get('price')}  {r.get('changePct')}%" if r.get('price') else ""
            print(f"  {r['value']:12} {r['name']}{extra}")
        return 0

    if args.list:
        if args.list == "ashare":
            items = fetch_ashare_list(force=args.refresh)
            rows = [{
                "value": i["sina"], "code": i.get("plain") or i["sina"][2:],
                "name": i["name"], "price": i.get("price"),
                "changePct": i.get("changePct"), "market": "ashare",
            } for i in items]
        else:
            items = fetch_crypto_list(force=args.refresh)
            rows = [{
                "value": i["symbol"], "code": i["symbol"],
                "name": i["name"], "market": "crypto",
            } for i in items]
        if args.json:
            print(json.dumps(rows if args.all else rows[:args.limit], ensure_ascii=False))
            return 0
        print(f"共 {len(rows)} 项")
        for r in (rows if args.all else rows[:50]):
            extra = f"  {r.get('price')}  {r.get('changePct')}%" if r.get("price") is not None else ""
            print(f"  {r['value']:12} {r['name']}{extra}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
