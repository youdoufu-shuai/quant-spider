# AI 决策配置

自动交易支持把决策交给外部大模型。**默认用 DeepSeek**，配置在页面上改，不用重启服务。

---

## 快速开始

1. 打开模拟盘页面 → 找到 **「AI 决策配置」** 卡片 → 点「展开」
2. 供应商已经默认选中 **DeepSeek（默认）**
3. 去 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 建一个 Key，填进 **API Key** 框
4. 勾上 **「允许使用 AI 模式」**
5. 点 **「测试连接」** —— 会真发一次请求（消耗几十 token）
6. 点 **「保存」**
7. 回到「自动交易」卡片，模式选 **AI 决策**，点「启用」

要不要先看它怎么判断？点 **「试算一次决策」** —— 不推进行情、不下单，只看它这次会怎么决定。

---

## 界面上每个字段

| 字段 | 说明 |
|---|---|
| **供应商** | DeepSeek / OpenAI / Ollama（本地）/ 自定义（OpenAI 兼容） |
| **模型** | 留空用供应商默认。下拉里是常用项，也可以手输任意模型名 |
| **API Key** | **留空 = 不修改已存的**。保存后只显示 `sk-xxx…abcd`，原值不回显 |
| **超时** | 单次请求超时，3~300 秒，默认 30 |
| **温度** | 0~2，默认 0.2。这类决策任务不需要发散，建议保持低温 |
| **接口地址** | 留空用供应商默认。自定义供应商必须填完整路径（`.../chat/completions`） |
| **系统提示词** | 留空用内置的。内置那份要求模型只输出 JSON 并说明风控优先 |
| **允许使用 AI 模式** | 总开关。不开的话选了 AI 模式也会报错 |

---

## 供应商对比

| 供应商 | 端点 | 默认模型 | 要 Key | 备注 |
|---|---|---|---|---|
| **DeepSeek** | `api.deepseek.com/chat/completions` | `deepseek-chat` | 是 | 国内直连，便宜。`deepseek-reasoner` 推理更强但更慢（会先输出思维链） |
| OpenAI | `api.openai.com/v1/chat/completions` | `gpt-4o-mini` | 是 | 国内需要代理 |
| Ollama | `127.0.0.1:11434/v1/chat/completions` | `qwen2.5:14b` | 否 | 完全本地，免费、数据不出机器。要先 `ollama pull qwen2.5:14b` |
| 自定义 | 自己填 | 自己填 | 视情况 | 任何 OpenAI 兼容服务：vLLM、LM Studio、公司网关等 |

**想省钱又要效果**：DeepSeek 的 `deepseek-chat` 足够。这个任务输入只有约 1KB 的结构化数据、输出只有几十 token，单次成本可以忽略。

**完全不想花钱**：Ollama + `qwen2.5:14b`。本地模型对 JSON 格式的遵守不如 DeepSeek 稳，但兜底逻辑能处理格式错误。

---

## 安全设计

### 1. 密钥不回显

`GET /api/ai/settings` **永远只回**：

```json
{ "apiKeySet": true, "apiKeyHint": "sk-brow…1234" }
```

原值只存在于本机 `quant/state/ai-config.json`，该路径已在 `.gitignore` 里。

**为什么留空 = 不修改**：因为前端拿不到原值。如果空值被当成"清空"，
用户只要改一下模型名就会把密钥抹掉。要真清除，传 `clearApiKey: true`。

页面上的 `API Key` 输入框是 `type="password"`，保存后立即清空，
所以也不会残留在 DOM 里（这一点写进了浏览器端测试）。

### 2. AI 的输出只是建议

这是最重要的一条。风控层**独立于 AI 生效**：

- AI 说 `target: 5.0`（满仓）→ 被夹到 `max_fraction`
- AI 说 `hold`（不卖）→ 但触发止损时照样强制平仓
- AI 返回垃圾格式 → 回落内置打分，事件日志里写明原因

一个胡说的模型不能把账户打爆。

### 3. 配置坏了不会让服务挂掉

`ai-config.json` 损坏时回落到默认值并关闭 AI 模式，
同时在页面上显示 `loadError`。不会静默用一个半坏的配置。

---

## 环境变量（可选）

优先级**高于**配置文件，方便临时覆盖和 CI：

```bash
export QTRADER_AI_URL=https://api.deepseek.com/chat/completions
export QTRADER_AI_MODEL=deepseek-chat
export QTRADER_AI_KEY=sk-xxx          # 设了这个会自动启用 AI 模式
```

---

## 命令行

```bash
python quant/ai_settings.py            # 看当前配置（脱敏）
python quant/ai_settings.py test       # 真发一次请求
python quant/ai_settings.py --selftest # 43 项自检
```

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `HTTP 401` | Key 无效/没填。检查有没有多余空格 |
| `HTTP 402` | 余额不足，去充值 |
| `HTTP 404` | 接口地址不对。必须包含完整路径 `/chat/completions`；Ollama 要带 `/v1` |
| `HTTP 429` | 请求太频繁或超配额 |
| 徽标显示「未就绪」 | 把鼠标移上去看 `problems` 里的具体原因 |
| 自动交易开了但不下单 | 看「事件明细」里的 `决策失败` 记录 —— 会写明具体原因 |
| 连接超时 | 国内直连 OpenAI 不通，换 DeepSeek，或在 `接口地址` 里填自己的代理 |

---

## 它给模型看什么

AI 收到的是**结构化快照**，不是原始 K 线数组：

```json
{
  "标的": "贵州茅台 (sh600519)",
  "周期": "1d",
  "当前价": 1329.0,
  "指标": { "EMA12":..., "RSI14":..., "MACD_DIF":..., "ADX":...,
            "布林上轨":..., "ATR占价格比":... },
  "账户": { "总权益":..., "可用现金":..., "是否持仓":..., "持仓浮动盈亏":... },
  "近期成交": [...]
}
```

为什么这样做：

- **数值带单位和解释**，模型不容易误读
- **体积小**，省 token
- **关键指标预先算好** —— 不让模型自己做算术，它算不准

---

## 实测记录

用假密钥测的（真发了请求，DeepSeek 返回 401）：

```
✗ HTTP 401 Authorization Required （API Key 无效或没填）
{"error":{"message":"Authentication Fails, Your api key: ****1234 is invalid
 (request_id: 1b9f4d97-89fe-4c6e-9f...)}}
```

这条**恰好证明了链路是对的**：端点、模型名、鉴权头格式全对，
只是密钥无效。DeepSeek 自己也把 key 脱敏成了 `****1234`。

⚠️ **但真模型下的决策质量没有验证过** —— 我这边没有可用的 Key，
所以「AI 会不会给出合理决策」这件事是未知的。
接口、兜底、输出夹紧逻辑都测了，模型的行为没测。
