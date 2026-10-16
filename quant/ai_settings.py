#!/usr/bin/env python3
"""
AI 决策设置 —— 持久化、脱敏、可测试。

═══════════════════════════════════════════════════════════════
  为什么单独一个模块
═══════════════════════════════════════════════════════════════
原来 AI 配置只能靠环境变量（QTRADER_AI_URL / MODEL / KEY）。
问题是：
  · 环境变量改一次要重启服务，页面上没法配
  · 密钥散落在启动脚本里，容易误提交
  · 页面上看不到当前用的是哪个模型

所以做成配置文件 + 页面可编辑。

═══════════════════════════════════════════════════════════════
  安全设计（重要）
═══════════════════════════════════════════════════════════════
1. **密钥永不出现在 API 响应里**。
   `public_view()` 只回 `apiKeySet: true/false` 和一个脱敏提示
   （`sk-…abcd`），绝不回原值。前端的输入框留空 =
   不修改已存的密钥，而不是清空。

2. **密钥不写入仓库**。
   配置文件落在 `quant/state/ai-config.json`，
   已在 .gitignore 里（这个目录整个被忽略）。

3. **默认就是 DeepSeek**，符合大多数人的用法；
   但**不预置任何密钥** —— 没有密钥时 AI 模式会明确报错并兜底，
   不会静默用假数据糊弄。

4. **AI 输出仍然只是建议**：风控层独立生效，
   target 会被夹到 [0, max_fraction]。这一层不因为换了模型而改变。

═══════════════════════════════════════════════════════════════
  用法
═══════════════════════════════════════════════════════════════
  from ai_settings import AIStore
  st = AIStore()                     # 默认读 quant/state/ai-config.json
  st.load()
  d = st.build_decider()             # 造一个按配置的 AIDecider
  st.save({...})                     # 部分更新
  st.public_view()                   # 脱敏后的视图，可以安全返回给前端
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
CONFIG_PATH = STATE_DIR / "ai-config.json"

for _s in (__import__("sys").stdout, __import__("sys").stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ============================================================
#  预置供应商
# ============================================================
# 都是 OpenAI 兼容的 /chat/completions 端点，所以一套代码通吃。
# DeepSeek 放在第一位 —— 它是默认值，也是大多数人的选择。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek（默认）",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "keyUrl": "https://platform.deepseek.com/api_keys",
        "note": "国内直连，价格低。deepseek-chat 适合本用途；"
                "deepseek-reasoner 会先输出思维链，更慢但推理更强。",
        "needsKey": True,
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "keyUrl": "https://platform.openai.com/api-keys",
        "note": "国内需要代理。",
        "needsKey": True,
    },
    "ollama": {
        "label": "Ollama（本地）",
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "qwen2.5:14b",
        "models": ["qwen2.5:14b", "qwen2.5:7b", "llama3.1:8b", "deepseek-r1:14b"],
        "keyUrl": "",
        "note": "完全本地，不花钱、数据不出机器，但需要先 ollama pull。",
        "needsKey": False,
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "url": "",
        "model": "",
        "models": [],
        "keyUrl": "",
        "note": "任何兼容 /chat/completions 的服务：vLLM、LM Studio、"
                "公司内网网关等。",
        "needsKey": False,
    },
}

DEFAULT_PROVIDER = "deepseek"


# ============================================================
@dataclass
class AISettings:
    """AI 决策的配置。字段名与 PROVIDERS 对齐。"""
    provider: str = DEFAULT_PROVIDER
    url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 30.0
    temperature: float = 0.2
    # 提示词可自定义 —— 不同模型对指令的敏感度差很多
    system_prompt: str = ""
    enabled: bool = False          # 是否允许 AI 模式（默认关，避免误用没配的 AI）
    updated_at: float = 0.0

    def resolved_url(self) -> str:
        """实际要请求的 URL：显式填了就用，否则取供应商默认。"""
        if self.url.strip():
            return self.url.strip()
        return PROVIDERS.get(self.provider, {}).get("url", "")

    def resolved_model(self) -> str:
        if self.model.strip():
            return self.model.strip()
        return PROVIDERS.get(self.provider, {}).get("model", "")

    def to_dict(self) -> dict:
        return asdict(self)

    def public_view(self) -> dict:
        """
        脱敏视图 —— **可以安全返回给前端**。

        ⚠️ 这里绝不能回 api_key 原值。只回「有没有设」和一个尾巴，
        足够用户确认"我填的是哪个 key"，又不足以泄露。
        """
        return {
            "provider": self.provider,
            "url": self.url,
            "model": self.model,
            "effectiveUrl": self.resolved_url(),
            "effectiveModel": self.resolved_model(),
            "timeout": self.timeout,
            "temperature": self.temperature,
            "systemPrompt": self.system_prompt,
            "enabled": self.enabled,
            "apiKeySet": bool(self.api_key),
            "apiKeyHint": _mask_key(self.api_key),
            "updatedAt": self.updated_at,
        }


def _mask_key(key: str) -> str:
    """
    把密钥脱敏成可展示的提示。

    保留前缀与后 4 位：`sk-1234…cdef`。
    太短的（<12 字符）只显示位数，避免"脱敏后反而泄露"。
    """
    if not key:
        return ""
    k = key.strip()
    if len(k) < 12:
        return f"（已设置，{len(k)} 位）"
    return f"{k[:7]}…{k[-4:]}"


DEFAULT_SYSTEM_PROMPT = """你是一个量化交易决策助手。你会收到一份结构化的市场快照，
包含标的信息、技术指标和账户状态。

请只输出一个 JSON 对象，不要任何其他文字、不要 Markdown 代码块。格式：
{"action": "buy|sell|hold", "target": 0.0, "reason": "简短理由"}

规则：
- action 是 buy 时 target 表示目标仓位比例（0.0~1.0）；sell 时 target 必须是 0.0
- hold 表示不动，target 填当前仓位比例
- reason 用中文，30 字以内，说清依据（例如"ADX 32 趋势强，MACD 金叉"）
- 不确定就 hold。宁可不动，不要勉强给信号

注意：你的输出只被当作建议，风控层会独立生效并夹紧仓位。
不要试图绕过风控，也不要因为"想满仓"而给 buy。"""


# ============================================================
class AIStore:
    """
    配置的读写。

    写入用**原子替换**（临时文件 + os.replace），
    避免写到一半崩溃留下半个 JSON —— 那会让服务起不来。
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.settings = AISettings()
        self.loaded = False

    # --------------------------------------------------------
    def load(self) -> AISettings:
        """读配置。文件不存在/损坏时回落到默认值，并说明情况。"""
        s = AISettings()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf8"))
                for k, v in raw.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except (json.JSONDecodeError, OSError) as e:
                # 配置坏了不该让整个服务挂掉 —— 用默认值继续，但要说清
                s = AISettings()
                s.enabled = False
                self._load_error = f"{type(e).__name__}: {e}"
            else:
                self._load_error = ""
        else:
            self._load_error = ""

        # 环境变量优先级**高于**文件 —— 方便临时覆盖与 CI
        if os.environ.get("QTRADER_AI_URL"):
            s.url = os.environ["QTRADER_AI_URL"]
        if os.environ.get("QTRADER_AI_MODEL"):
            s.model = os.environ["QTRADER_AI_MODEL"]
        if os.environ.get("QTRADER_AI_KEY"):
            s.api_key = os.environ["QTRADER_AI_KEY"]
            s.enabled = True

        self.settings = s
        self.loaded = True
        return s

    # --------------------------------------------------------
    def save(self, patch: dict) -> AISettings:
        """
        部分更新并落盘。

        ⚠️ api_key 的特殊处理：**空字符串 = 不修改**，不是清空。
           因为前端拿不到原值（脱敏），如果它把空值提交上来就清空，
           用户只要改一下模型就会把密钥抹掉。
           要真正清除密钥，传 `clearApiKey: true`。
        """
        s = self.settings
        clear_key = bool(patch.pop("clearApiKey", False))

        for k, v in patch.items():
            if k == "api_key":
                continue                      # 单独处理，见下
            if not hasattr(s, k):
                continue
            if k in ("timeout", "temperature"):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if k == "timeout":
                    v = max(3.0, min(300.0, v))
                else:
                    v = max(0.0, min(2.0, v))
            elif k == "enabled":
                v = bool(v)
            elif isinstance(v, str):
                v = v.strip()
            setattr(s, k, v)

        # 显式传入非空 key 才更新；空字符串视为"不动"
        new_key = str(patch.get("api_key", "") or "").strip()
        if clear_key:
            s.api_key = ""
        elif new_key:
            s.api_key = new_key

        # 换供应商时，若没显式给 url/model，就跟着换默认值
        prov = PROVIDERS.get(s.provider)
        if prov:
            if not s.url.strip() and prov["url"]:
                s.url = ""
            if patch.get("provider") and not patch.get("model"):
                s.model = ""

        s.updated_at = time.time()
        self.settings = s
        self._write()
        return s

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.settings.to_dict(), ensure_ascii=False,
                                  indent=2), encoding="utf8")
        os.replace(tmp, self.path)            # 原子替换

    # --------------------------------------------------------
    def status(self) -> dict:
        """
        可用性判断 —— 页面要拿它决定"能不能开 AI 模式"。
        """
        s = self.settings
        url = s.resolved_url()
        model = s.resolved_model()
        prov = PROVIDERS.get(s.provider, {})
        problems = []
        if not url:
            problems.append("没有配置接口地址")
        if not model:
            problems.append("没有配置模型名")
        if prov.get("needsKey") and not s.api_key:
            problems.append(f"{prov.get('label', s.provider)} 需要 API Key，"
                            f"还没填")
        ready = not problems and s.enabled
        if not s.enabled and not problems:
            problems.append("AI 模式未启用（在页面上打开开关）")
        return {
            "ready": ready,
            "enabled": s.enabled,
            "provider": s.provider,
            "providerLabel": prov.get("label", s.provider),
            "url": url,
            "model": model,
            "apiKeySet": bool(s.api_key),
            "problems": problems,
            "loadError": getattr(self, "_load_error", ""),
        }

    # --------------------------------------------------------
    def build_decider(self, config=None):
        """
        造一个按当前配置的 AIDecider。

        未配置好时抛 ValueError（而不是造一个必然失败的决策器）——
        调用方应该先查 status()，把问题展示给用户。
        """
        from autotrader import AIDecider, AutoConfig

        st = self.status()
        if st["problems"]:
            raise ValueError("AI 未配置好：" + "；".join(st["problems"]))
        s = self.settings
        return AIDecider(
            config or AutoConfig(mode="ai"),
            url=s.resolved_url(),
            model=s.resolved_model(),
            api_key=s.api_key,
            timeout=s.timeout,
        )

    # --------------------------------------------------------
    def test(self) -> dict:
        """
        真的发一次请求，看配置能不能用。

        这是**唯一**会用到密钥的对外方法，而且只回
        "成功/失败 + 模型回复的摘要"，不回密钥。
        """
        st = self.status()
        if st["problems"]:
            return {"ok": False, "error": "；".join(st["problems"]), "status": st}
        s = self.settings
        url = s.resolved_url()
        body = json.dumps({
            "model": s.resolved_model(),
            "messages": [
                {"role": "system",
                 "content": "只回复一个 JSON：{\"action\":\"hold\",\"target\":0,\"reason\":\"连通性测试\"}"},
                {"role": "user", "content": "连通性测试，请按要求回复 JSON。"},
            ],
            "temperature": 0.0,
            "max_tokens": 100,
        }, ensure_ascii=False).encode("utf8")

        headers = {"Content-Type": "application/json"}
        if s.api_key:
            headers["Authorization"] = f"Bearer {s.api_key}"

        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=s.timeout) as r:
                raw = r.read().decode("utf8")
            ms = int((time.time() - t0) * 1000)
            d = json.loads(raw)
            text = d["choices"][0]["message"]["content"]
            usage = d.get("usage") or {}
            return {
                "ok": True, "ms": ms, "status": st,
                "reply": text[:300],
                "model": d.get("model", s.resolved_model()),
                "usage": {"prompt": usage.get("prompt_tokens"),
                          "completion": usage.get("completion_tokens"),
                          "total": usage.get("total_tokens")},
            }
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf8")[:400]
            except Exception:
                pass
            hint = ""
            if e.code == 401:
                hint = "（API Key 无效或没填）"
            elif e.code == 402:
                hint = "（余额不足）"
            elif e.code == 404:
                hint = "（接口地址不对，检查是不是完整路径 .../chat/completions）"
            elif e.code == 429:
                hint = "（请求太频繁或超配额）"
            return {"ok": False, "ms": int((time.time() - t0) * 1000),
                    "error": f"HTTP {e.code} {e.reason} {hint}", "detail": detail,
                    "status": st}
        except Exception as e:
            return {"ok": False, "ms": int((time.time() - t0) * 1000),
                    "error": f"{type(e).__name__}: {e}", "status": st}


# ============================================================
def _selftest() -> int:
    import tempfile

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
    print("  AI 设置自检（不联网、不碰真实配置）")
    print("=" * 76)

    tmpdir = Path(tempfile.mkdtemp(prefix="aistore-"))
    cfg = tmpdir / "ai-config.json"

    # --- 1. 默认值 ---
    print("\n[1] 默认值")
    st = AIStore(cfg)
    s = st.load()
    check("默认供应商是 DeepSeek", s.provider == "deepseek", s.provider)
    check("默认模型是 deepseek-chat", s.resolved_model() == "deepseek-chat")
    check("默认 URL 指向 deepseek", "api.deepseek.com" in s.resolved_url(),
          s.resolved_url())
    check("默认没有密钥", s.api_key == "")
    check("默认不启用（避免误用没配的 AI）", s.enabled is False)

    # --- 2. 脱敏：最关键的一条 ---
    print("\n[2] 密钥脱敏（安全关键）")
    st.save({"api_key": "sk-abcdefghijklmnopqrstuvwxyz1234"})
    pv = st.settings.public_view()
    blob = json.dumps(pv, ensure_ascii=False)
    check("public_view 不含密钥原值",
          "abcdefghijklmnopqrstuvwxyz" not in blob)
    check("public_view 不含完整密钥", "sk-abcdefghijklmnopqrstuvwxyz1234" not in blob)
    check("apiKeySet 为 True", pv["apiKeySet"] is True)
    check("有脱敏提示", pv["apiKeyHint"] != "", pv["apiKeyHint"])
    check("提示保留了尾部 4 位", pv["apiKeyHint"].endswith("1234"), pv["apiKeyHint"])
    check("提示不含中间部分", "ghijklmnop" not in pv["apiKeyHint"])

    st2 = AIStore(tmpdir / "b.json")
    st2.save({"api_key": "short"})
    check("短密钥只显示位数", "位" in st2.settings.public_view()["apiKeyHint"],
          st2.settings.public_view()["apiKeyHint"])

    # --- 3. 空字符串 = 不修改 ---
    print("\n[3] 空密钥不覆盖已存密钥")
    st.save({"api_key": "sk-original-key-1234567890"})
    st.save({"model": "deepseek-reasoner", "api_key": ""})
    check("改模型不会清掉密钥", st.settings.api_key == "sk-original-key-1234567890",
          st.settings.api_key[:10] + "…")
    check("模型确实改了", st.settings.model == "deepseek-reasoner")
    st.save({"clearApiKey": True})
    check("clearApiKey 能真正清空", st.settings.api_key == "")

    # --- 4. 供应商切换 ---
    print("\n[4] 供应商切换")
    st.save({"provider": "ollama", "url": "", "model": ""})
    check("切到 ollama 用它的默认 URL",
          "11434" in st.settings.resolved_url(), st.settings.resolved_url())
    check("切到 ollama 用它的默认模型",
          st.settings.resolved_model() == "qwen2.5:14b")
    st.save({"provider": "custom",
             "url": "http://192.168.1.9:8000/v1/chat/completions",
             "model": "my-model"})
    check("自定义 URL 生效",
          st.settings.resolved_url().endswith("/v1/chat/completions"))
    check("自定义模型生效", st.settings.resolved_model() == "my-model")

    # --- 5. 持久化 ---
    print("\n[5] 落盘与重读")
    st.save({"provider": "deepseek", "enabled": True})
    st3 = AIStore(cfg)
    s3 = st3.load()
    check("重读配置一致", s3.provider == "deepseek" and s3.enabled is True)
    check("updated_at 被写入", s3.updated_at > 0)
    check("配置文件是合法 JSON",
          isinstance(json.loads(cfg.read_text(encoding="utf8")), dict))

    # --- 6. 损坏配置不该让服务挂掉 ---
    print("\n[6] 损坏配置的容错")
    bad = tmpdir / "bad.json"
    bad.write_text("{ 这不是 JSON", encoding="utf8")
    st4 = AIStore(bad)
    s4 = st4.load()
    check("损坏时回落到默认值", s4.provider == "deepseek")
    check("损坏时不启用", s4.enabled is False)
    check("记录了加载错误", "JSONDecodeError" in getattr(st4, "_load_error", ""),
          getattr(st4, "_load_error", "")[:60])
    check("status 里能看到错误", st4.status()["loadError"] != "")

    # --- 7. 可用性判断 ---
    print("\n[7] status 的可用性判断")
    st5 = AIStore(tmpdir / "c.json")
    st5.load()
    st5.save({"provider": "deepseek", "api_key": "", "enabled": True})
    stt = st5.status()
    check("缺密钥 → 不 ready", stt["ready"] is False)
    check("说明了缺密钥", any("API Key" in p for p in stt["problems"]),
          str(stt["problems"]))
    st5.save({"api_key": "sk-test-key-1234567890"})
    stt2 = st5.status()
    check("补齐后 ready", stt2["ready"] is True, str(stt2["problems"]))
    st5.save({"enabled": False})
    check("关掉开关 → 不 ready", st5.status()["ready"] is False)
    check("说明未启用", any("未启用" in p for p in st5.status()["problems"]))

    # --- 8. 本地模型不需要密钥 ---
    print("\n[8] 本地模型不要求密钥")
    st6 = AIStore(tmpdir / "d.json")
    st6.load()
    st6.save({"provider": "ollama", "enabled": True})
    check("ollama 无密钥也 ready", st6.status()["ready"] is True,
          str(st6.status()["problems"]))

    # --- 9. 数值范围夹紧 ---
    print("\n[9] 数值范围")
    st7 = AIStore(tmpdir / "e.json")
    st7.load()
    st7.save({"timeout": 99999})
    check("timeout 被夹到上限", st7.settings.timeout == 300.0, st7.settings.timeout)
    st7.save({"timeout": 0.1})
    check("timeout 被夹到下限", st7.settings.timeout == 3.0, st7.settings.timeout)
    st7.save({"temperature": 9})
    check("temperature 被夹到 2", st7.settings.temperature == 2.0)
    st7.save({"temperature": -1})
    check("temperature 被夹到 0", st7.settings.temperature == 0.0)

    # --- 10. 未知字段被忽略 ---
    print("\n[10] 未知字段")
    st8 = AIStore(tmpdir / "f.json")
    st8.load()
    st8.save({"这不是字段": 1, "provider": "deepseek"})
    check("未知字段被忽略", not hasattr(st8.settings, "这不是字段"))

    # --- 11. 未配置时不造决策器 ---
    print("\n[11] 未配置时的行为")
    st9 = AIStore(tmpdir / "g.json")
    st9.load()
    try:
        st9.build_decider()
        check("未配置时 build_decider 应抛错", False, "没抛错")
    except ValueError as e:
        check("未配置时抛 ValueError", True, str(e)[:50])
    except Exception as e:
        check("未配置时抛 ValueError", False, f"{type(e).__name__}: {e}")

    # --- 12. 提示词有默认值 ---
    print("\n[12] 系统提示词")
    check("默认提示词非空", len(DEFAULT_SYSTEM_PROMPT) > 50)
    check("提示词要求 JSON 输出", "JSON" in DEFAULT_SYSTEM_PROMPT)
    check("提示词说明了风控优先", "风控" in DEFAULT_SYSTEM_PROMPT)
    st10 = AIStore(tmpdir / "h.json")
    st10.load()
    st10.save({"system_prompt": "自定义提示"})
    check("可自定义提示词", st10.settings.system_prompt == "自定义提示")

    # --- 13. 环境变量覆盖 ---
    print("\n[13] 环境变量优先")
    old = os.environ.get("QTRADER_AI_URL")
    os.environ["QTRADER_AI_URL"] = "http://env-override.test/v1/chat/completions"
    st11 = AIStore(tmpdir / "i.json")
    st11.load()
    check("环境变量覆盖 URL", "env-override" in st11.settings.resolved_url(),
          st11.settings.resolved_url())
    if old is None:
        del os.environ["QTRADER_AI_URL"]
    else:
        os.environ["QTRADER_AI_URL"] = old

    # 清理
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 76)
    print(f"  自检: {ok} 通过 / {fail} 失败")
    print("=" * 76)
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    ap_args = sys.argv[1:]
    if not ap_args or ap_args[0] == "show":
        st = AIStore()
        st.load()
        print(json.dumps({"settings": st.settings.public_view(),
                          "status": st.status()},
                         ensure_ascii=False, indent=2))
    elif ap_args[0] == "test":
        st = AIStore()
        st.load()
        print(json.dumps(st.test(), ensure_ascii=False, indent=2))
    else:
        print("用法: python ai_settings.py [show|test|--selftest]")
        raise SystemExit(2)
