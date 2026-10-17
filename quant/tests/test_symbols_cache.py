#!/usr/bin/env python3
"""
股票列表缓存测试 —— 全部离线。

═══════════════════════════════════════════════════════════════
  ⚠️ 为什么单独一个文件
═══════════════════════════════════════════════════════════════
这里测的是一个**真实造成事故的 bug**：

  缓存 TTL 原来是 12 小时，过期后 `fetch_ashare_list()` 会**阻塞枚举**
  51997 个代码（70 秒）。而 HTTP 请求超时是 30 秒 → 返回 500 →
  依赖 `/api/paper/meta` 的页面（专业图表、模拟盘）全部卡在「加载中」。

  实测证据：`/api/paper/meta` 返回
    {"error":"正在枚举全量 A 股（约 70 秒，只需一次）…"}
  pro.html 的 canvas 停在浏览器默认的 300×150，一像素没画。

修复：
  1. TTL 12 小时 → 7 天（A 股一周新增几只新股，对查询无影响）
  2. 缓存过期但还在（90 天内）→ **先用旧数据返回，不阻塞**

这些测试锁住修复，防止有人「优化」回阻塞行为。

⚠️ 离线：不联网。缓存路径通过临时目录注入。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
REPO = QUANT.parent
for p in (HERE, REPO, QUANT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("QUANT_OFFLINE", "1")

from harness import REGISTRY, TestCase, declare_counts, run_all, module_summary  # noqa: E402
import symbols as SYM                                       # noqa: E402


def _fresh_cache(items: list | None = None) -> dict:
    """一份「刚存」的缓存结构。"""
    it = items if items is not None else [
        {"code": "sh600519", "sina": "sh600519", "plain": "600519",
         "name": "贵州茅台", "price": 1329.0, "changePct": 1.2},
    ]
    return {"ashare2": {"at": time.time(), "items": it},
            "crypto": {"at": time.time(), "items": []}}


# ══════════════════════════════════════════════════════════════
def test_ttl_is_long_enough():
    """
    ⚠️ TTL 必须显著长于「枚举耗时」。

    枚举要 70 秒，如果 TTL 只有 12 小时，那么每天至少两次
    「过期期间的第一个请求」会阻塞 70 秒 → 超时。
    TTL 7 天意味着平均每 7 天才遇到一次，而且还有 stale 兜底。
    """
    assert SYM.CACHE_TTL_SEC >= 7 * 24 * 3600, \
        f"TTL 太短（{SYM.CACHE_TTL_SEC/3600:.0f} 小时），建议 ≥7 天"
    assert SYM.CACHE_STALE_MAX_SEC > SYM.CACHE_TTL_SEC, \
        "stale 上限必须大于 TTL，否则过期缓存立刻就不可用了"


def test_fresh_cache_is_used():
    """新鲜缓存直接返回，不枚举。"""
    tmp = Path(tempfile.mkdtemp(prefix="sym-")) / "c.json"
    tmp.write_text(json.dumps(_fresh_cache()), encoding="utf8")
    orig = SYM.CACHE_FILE
    SYM.CACHE_FILE = tmp
    try:
        items = SYM.fetch_ashare_list()
        assert len(items) == 1
        assert items[0]["name"] == "贵州茅台"
    finally:
        SYM.CACHE_FILE = orig
        tmp.unlink(missing_ok=True)


def test_stale_cache_is_still_used_not_blocking():
    """
    ⚠️ 核心修复：缓存**过期但还在**时必须返回旧数据，不能去枚举。

    这条是本文件存在的主要理由。旧行为会阻塞 70 秒然后超时。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sym-")) / "c.json"
    stale = _fresh_cache()
    # 把时间调到 2 天前（超过旧 TTL 12 小时，但远小于新 TTL 7 天）
    stale["ashare2"]["at"] = time.time() - 2 * 86400
    tmp.write_text(json.dumps(stale), encoding="utf8")
    orig = SYM.CACHE_FILE
    SYM.CACHE_FILE = tmp
    try:
        t0 = time.time()
        items = SYM.fetch_ashare_list()
        el = time.time() - t0
        assert len(items) == 1, "过期缓存必须仍可用"
        assert items[0]["name"] == "贵州茅台"
        # 关键：不能阻塞。枚举 51997 个代码要 70 秒，这里必须毫秒级。
        assert el < 1.0, \
            f"返回过期缓存用了 {el:.2f}s —— 说明去枚举了，会导致 HTTP 超时"
    finally:
        SYM.CACHE_FILE = orig
        tmp.unlink(missing_ok=True)


def test_very_old_cache_triggers_refresh_path():
    """
    超过 CACHE_STALE_MAX_SEC（90 天）的缓存不可信，应该走重新枚举路径。
    这里只验证「不再当成可用缓存」——不真的联网。

    做法：把枚举函数换成一个返回固定值的桩，验证它被调用了。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sym-")) / "c.json"
    ancient = _fresh_cache()
    ancient["ashare2"]["at"] = time.time() - (SYM.CACHE_STALE_MAX_SEC + 86400)
    tmp.write_text(json.dumps(ancient), encoding="utf8")

    called = {"n": 0}
    orig_cache = SYM.CACHE_FILE
    orig_enum = SYM.enumerate_ashare
    orig_save = SYM._save_cache

    def fake_enum(progress=None):
        called["n"] += 1
        return [{"code": "sh600000", "plain": "600000", "name": "浦发银行",
                 "price": 8.0, "changePct": 0.5}]

    SYM.CACHE_FILE = tmp
    SYM.enumerate_ashare = fake_enum
    SYM._save_cache = lambda c: None          # 别写真文件
    try:
        items = SYM.fetch_ashare_list()
        assert called["n"] == 1, "90 天以上的缓存应触发重新枚举"
        assert items and items[0]["name"] == "浦发银行"
    finally:
        SYM.CACHE_FILE = orig_cache
        SYM.enumerate_ashare = orig_enum
        SYM._save_cache = orig_save
        tmp.unlink(missing_ok=True)


def test_force_bypasses_cache():
    """force=True 必须绕过缓存（无论是新鲜还是过期）。"""
    tmp = Path(tempfile.mkdtemp(prefix="sym-")) / "c.json"
    tmp.write_text(json.dumps(_fresh_cache()), encoding="utf8")

    called = {"n": 0}
    orig_cache = SYM.CACHE_FILE
    orig_enum = SYM.enumerate_ashare
    orig_save = SYM._save_cache

    def fake_enum(progress=None):
        called["n"] += 1
        return [{"code": "sh600000", "plain": "600000", "name": "浦发银行",
                 "price": 8.0, "changePct": 0.5}]

    SYM.CACHE_FILE = tmp
    SYM.enumerate_ashare = fake_enum
    SYM._save_cache = lambda c: None
    try:
        items = SYM.fetch_ashare_list(force=True)
        assert called["n"] == 1, "force=True 应强制枚举"
        assert items[0]["name"] == "浦发银行"
    finally:
        SYM.CACHE_FILE = orig_cache
        SYM.enumerate_ashare = orig_enum
        SYM._save_cache = orig_save
        tmp.unlink(missing_ok=True)


def test_empty_cache_does_not_count_as_usable():
    """items 为空的缓存不算可用 —— 否则会一直返回空列表。"""
    tmp = Path(tempfile.mkdtemp(prefix="sym-")) / "c.json"
    tmp.write_text(json.dumps({"ashare2": {"at": time.time(), "items": []}}),
                   encoding="utf8")

    called = {"n": 0}
    orig_cache = SYM.CACHE_FILE
    orig_enum = SYM.enumerate_ashare
    orig_save = SYM._save_cache

    def fake_enum(progress=None):
        called["n"] += 1
        return [{"code": "sh600000", "plain": "600000", "name": "浦发银行",
                 "price": 8.0, "changePct": 0.5}]

    SYM.CACHE_FILE = tmp
    SYM.enumerate_ashare = fake_enum
    SYM._save_cache = lambda c: None
    try:
        items = SYM.fetch_ashare_list()
        assert called["n"] == 1, "空缓存应触发枚举"
        assert len(items) == 1
    finally:
        SYM.CACHE_FILE = orig_cache
        SYM.enumerate_ashare = orig_enum
        SYM._save_cache = orig_save
        tmp.unlink(missing_ok=True)


def test_cached_name_never_networks():
    """
    ⚠️ cached_name() 必须只读缓存，绝不联网。

    为什么：它被 paper_live.symbol_name() 调用，在每次下单/查账户时都跑。
    如果它触发枚举，一次请求就 70 秒 —— CI 里直接超时。
    （这条测试最初是为 CI 超时加的，现在继续守着。）
    """
    import socket
    calls = []
    orig = (socket.socket, socket.create_connection, socket.getaddrinfo)

    def spy(*a, **k):
        calls.append(a)
        raise AssertionError("cached_name 不该联网")

    socket.socket = spy
    socket.create_connection = spy
    socket.getaddrinfo = spy
    try:
        # 无论有没有缓存，都不许联网
        name = SYM.cached_name("ashare", "sh600519")
        assert name is None or isinstance(name, str)
        assert SYM.cached_name("不存在的市场", "xxx") is None
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo = orig
    assert calls == [], f"cached_name 发起了网络调用：{calls}"


# ══════════════════════════════════════════════════════════════
_CASES = [(n, f) for n, f in sorted(globals().items())
          if n.startswith("test_") and callable(f)]


def _make_case(fn):
    def run_case(self):
        try:
            fn()
        except AssertionError as e:
            self.eq(f"FAIL: {e}", "PASS", f"{fn.__name__} 断言失败")
            return
        except Exception as e:                          # noqa: BLE001
            self.eq(f"{type(e).__name__}: {e}", "PASS",
                    f"{fn.__name__} 抛异常")
            return
        self.eq("PASS", "PASS", f"{fn.__name__} 全部断言通过")
    run_case.__name__ = fn.__name__
    run_case.__doc__ = fn.__doc__
    return run_case


class TestSymbolsCache(TestCase):
    """股票列表缓存：TTL、过期兜底、强制刷新、不联网。"""
    name = "symbols.股票列表缓存"


for _n, _f in _CASES:
    setattr(TestSymbolsCache, _n, _make_case(_f))

REGISTRY.append(TestSymbolsCache)

declare_counts({"TestSymbolsCache": {n: (1, 1) for n, _ in _CASES}})


if __name__ == "__main__":
    run_all(verbose=True)
    raise SystemExit(module_summary("test_symbols_cache.py"))
