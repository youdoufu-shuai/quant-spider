"""
内存占用测量 —— 父进程测子进程，避开进程内 API 的坑。

═══ 为什么这么绕 ═══
进程内用 GetProcessMemoryInfo 自测在 Windows + ctypes 下反复返回 0
（试了两种结构体定义），不可靠。而**父进程查子进程**是稳的：
子进程退出前，父进程读它的 PeakWorkingSetSize 即可。

═══ 怎么测 ═══
  父进程：起一个子进程跑指定场景，40ms 采样一次内存，记峰值。
  子进程：造合成数据、跑计算、把「数据规模」打到 stdout。

用合成数据而不是真实行情：内存取决于「根数 × 组合数」，与数据来源无关。
合成数据秒级完成，真实行情每次要联网等几秒。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent / "quant"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def win_mem(pid: int):
    """返回 (WorkingSet, PeakWorkingSet) 字节；非 Windows 或失败返回 None。"""
    if sys.platform != "win32":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, pid)          # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        p = PMC()
        p.cb = wt.DWORD(ctypes.sizeof(PMC))
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb):
            return None
        return int(p.WorkingSetSize), int(p.PeakWorkingSetSize)
    finally:
        k32.CloseHandle(h)


def linux_mem(pid: int):
    try:
        rss = hwm = 0
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
            elif line.startswith("VmHWM:"):
                hwm = int(line.split()[1]) * 1024
        return (rss, hwm) if rss else None
    except OSError:
        return None


def measure(script: Path, args: list[str], sample_ms: int = 35):
    p = subprocess.Popen([sys.executable, str(script), *args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf8", errors="replace")
    peak_ws = peak_pk = 0
    samples = 0
    t0 = time.time()
    while p.poll() is None:
        m = win_mem(p.pid) or linux_mem(p.pid)
        if m:
            peak_ws = max(peak_ws, m[0])
            peak_pk = max(peak_pk, m[1])
            samples += 1
        time.sleep(sample_ms / 1000.0)
    out, err = p.communicate()
    return {
        "peak_mb": round(max(peak_ws, peak_pk) / 1048576, 1),
        "samples": samples,
        "seconds": round(time.time() - t0, 2),
        "exit": p.returncode,
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip().splitlines()[-1][:200] if err else "",
    }


# ══════════════════════════════════════════════════════════════
#  子进程侧：每个场景一个函数
# ══════════════════════════════════════════════════════════════
CHILD = r'''
import sys, json, gc
from pathlib import Path
QUANT = Path(r"{quant}")
sys.path.insert(0, str(QUANT))
import numpy as np
from datasource import Bar, Series
from indicators import compute
from strategy import run as run_strategy
from backtest import Costs, run_backtest

def mk(n, market="crypto"):
    rng = np.random.default_rng(42)
    px, bars = 100.0, []
    for i in range(n):
        px *= 1 + rng.normal(0.0004, 0.012)
        o = px * (1 + rng.normal(0, 0.003))
        h = max(o, px) * (1 + abs(rng.normal(0, 0.004)))
        l = min(o, px) * (1 - abs(rng.normal(0, 0.004)))
        bars.append(Bar(1_700_000_000_000 + i*3_600_000, o, h, l, px,
                        1000 + rng.random()*500))
    return Series("synth", market, "SYNTH", "合成", "1h", tuple(bars), 0.0)

def scenario(name, **kw):
    n = int(kw.get("n", 500))
    s = mk(n)
    if name == "idle":
        pass
    elif name == "compute":
        ind = compute(s)
    elif name == "backtest":
        ind = compute(s); sig = run_strategy("ema_cross", ind)
        run_backtest(s, sig, costs=Costs(0.001, 0.0005, 0.0))
    elif name == "optimize":
        sys.path.insert(0, str(QUANT))
        from optimizer import Optimizer
        o = Optimizer(series=s, strategy="ema_cross",
                      costs=Costs(0.001, 0.0005, 0.0))
        o.grid(max_combos=int(kw.get("combos", 20)))
    elif name == "json":
        ind = compute(s); sig = run_strategy("ema_cross", ind)
        run_backtest(s, sig, costs=Costs(0.001, 0.0005, 0.0))
        payload = {{"bars": [b.as_dict() for b in s.bars],
                    "indicators": {{k: list(v) for k, v in list(ind.values.items())[:17]}}}}
        txt = json.dumps(payload, ensure_ascii=False)
        print("KEYS=" + str(len(txt)))
    elif name == "pipeline_like":
        # 模拟 pipeline.py 的完整工作量：指标 + 5 策略 + 回测 + JSON
        ind = compute(s)
        sigs = {{k: run_strategy(k, ind) for k in
                 ("ema_cross","rsi_reversion","bollinger_break","macd_trend","donchian_break")}}
        for sig in sigs.values():
            run_backtest(s, sig, costs=Costs(0.001, 0.0005, 0.0))
        payload = {{"bars": [b.as_dict() for b in s.bars],
                    "indicators": {{k: list(v) for k, v in list(ind.values.items())[:17]}}}}
        txt = json.dumps(payload, ensure_ascii=False)
        print("KEYS=" + str(len(txt)))
    gc.collect()
    print("DONE n=" + str(n))

if __name__ == "__main__":
    scenario(sys.argv[1], **json.loads(sys.argv[2] if len(sys.argv) > 2 else "{{}}"))
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-ms", type=int, default=35)
    a = ap.parse_args()

    child = Path(sys.argv[0]).with_name("_mem_child.py")
    child.write_text(CHILD.replace("{quant}", str(QUANT)).replace("{{", "{").replace("}}", "}"),
                     encoding="utf8")

    print("=" * 88)
    print("  内存测量（合成数据，父进程测子进程的峰值工作集）")
    print("=" * 88)

    baseline = measure(child, ["idle", '{"n":10}'], a.sample_ms)
    b = baseline["peak_mb"]
    print(f"\n  基线（解释器 + numpy，不算东西）          峰值 {b:>6.1f} MB")

    print(f"\n── 1. compute() 指标计算随数据量 ──")
    print(f"  {'根数':>7}  {'峰值':>9}  {'净增':>9}  {'每根':>9}")
    for n in (500, 2000, 5000, 10000):
        r = measure(child, ["compute", json.dumps({"n": n})], a.sample_ms)
        per = (r["peak_mb"] - b) * 1024 / n
        print(f"  {n:>7}  {r['peak_mb']:>7.1f}MB  {r['peak_mb']-b:>+7.1f}MB  {per:>7.1f}KB")

    print(f"\n── 2. 完整 pipeline 工作量（指标 + 5 策略 + 回测 + JSON）──")
    print(f"  {'根数':>7}  {'峰值':>9}  {'净增':>9}  {'JSON 大小':>12}")
    for n in (500, 2000, 5000):
        r = measure(child, ["pipeline_like", json.dumps({"n": n})], a.sample_ms)
        keys = next((l.split("=")[1] for l in r["stdout"].splitlines()
                     if l.startswith("KEYS=")), "?")
        ks = f"{int(keys)/1024:.0f} KB" if keys.isdigit() else "?"
        print(f"  {n:>7}  {r['peak_mb']:>7.1f}MB  {r['peak_mb']-b:>+7.1f}MB  {ks:>12}")

    print(f"\n── 3. 参数优化（内存大头：每组合重算指标 + 回测）──")
    print(f"  {'根数':>7}  {'组合':>5}  {'峰值':>9}  {'净增':>9}")
    for n, c in ((500, 20), (1000, 60), (2000, 100), (2000, 300)):
        r = measure(child, ["optimize", json.dumps({"n": n, "combos": c})], a.sample_ms)
        print(f"  {n:>7}  {c:>5}  {r['peak_mb']:>7.1f}MB  {r['peak_mb']-b:>+7.1f}MB")

    print(f"\n── 4. 只算指标（对照）──")
    for n in (500, 2000, 5000):
        r = measure(child, ["compute", json.dumps({"n": n})], a.sample_ms)
        print(f"  {n:>7}  {r['peak_mb']:>7.1f}MB  {r['peak_mb']-b:>+7.1f}MB")

    child.unlink(missing_ok=True)
    print("\n" + "=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
