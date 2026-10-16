# Prompt Spider MCP — 接入 DSH 使用说明

> 状态：**已注册并生效**。DSH 已加载 `prompt-spider` MCP server，11 个工具可用。
> 注册位置：`~/.dsh/profiles/desktop/cordis.patch.yml`

---

## 现在就能用

在 DSH 里直接说这句话，我就会调用这套工具展示任务：

> **"用 prompt-spider 展示这次任务"**

或者更具体：

> **"开一个 spider 会话，标题叫「重构登录模块」，然后按进度上报"**

我会：
1. `spider_create_session` 建会话 → 拿到带 token 的预览链接
2. 干活的过程中 `spider_update_events` 上报每一步
3. 结束时 `spider_complete_session`，然后给你链接回看

**注意**：UI **不会自动**监控我 —— 是**我主动上报**。所以你需要明确让我用这套工具。

---

## 11 个工具

| 工具 | 作用 |
|---|---|
| `spider_create_session` | 建会话，返回 `previewUrl` |
| `spider_update_events` | 上报 1–100 条真实事件 |
| `spider_configure_session` | 改标题 / 布局 / 分支名 / 配色 |
| `spider_control_playback` | 播放 / 暂停 / 跳转 / 跟随实时 |
| `spider_get_session` | 查会话元数据 + 分页事件 |
| `spider_list_sessions` | 列出所有会话 |
| `spider_complete_session` | 标记完成 / 失败 / 取消（不关闭） |
| `spider_close_session` | 释放会话和记录 |
| `spider_export_session` | 导出回放 JSON |
| `spider_import_session` | 导入回放 |
| `spider_export_project` | 导出独立可运行项目 |

外加 3 个资源（`spider://guide`、`spider://config`、`spider://replay-format`）和 1 个 prompt（`spider_report_task`）。

---

## 三条分支怎么分

上报时必须给每个任务指定 `route`：

| route | 含义 | 什么时候用 |
|---|---|---|
| `code` | **确定性**处理 | 读文件、跑测试、构建、静态检查、格式转换 |
| `model` | **模型**判断 | 设计、审查、改写、理解代码、做决策 |
| `you` | 需要**人工**动作 | 等批准、等确认、发布、合并、真实世界操作 |

> ⚠️ **`completed` 只表示"本次展示结束"，不等于人工已批准。**
> 真实需要批准的任务，应该在批准**真的完成后**才标 completed。

### 事件字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | **稳定 ID**。复用同一个 ID 是**更新**，不会重复计数 |
| `label` | 新 ID 必填 | 任务名（≤200 字符） |
| `route` | 新 ID 必填 | `code` / `model` / `you` |
| `status` | | `pending` / `running` / `completed` / `failed` |
| `detail` | | 补充说明（≤4000 字符） |
| `score` | | 0–1。**没把握就别传** —— 缺失会显示"未提供"，不会编造 |

**关键用法**：同一个任务先上报 `running`，完成后**用同一个 id** 再上报 `completed`：

```javascript
// 开始
spider_update_events({ sessionId, events: [
  { id: "test", label: "运行测试套件", route: "code", status: "running" }
]})

// 完成（同一个 id，只改 status）
spider_update_events({ sessionId, events: [
  { id: "test", status: "completed", detail: "44 passed / 0 failed" }
]})
```

这样计数仍然是 1 个任务，但状态会从 `running` 变成 `completed` —— 页面上能看到它"跑完了"。

---

## 三种布局

| layout | 画布 | 适合 |
|---|---|---|
| `portrait`（默认） | 1080 × 1350 | 竖屏、录屏 |
| `landscape` | 1600 × 900 | 桌面演示、工作流面板 |
| `square` | 1080 × 1080 | 方形展示 |

---

## 定制外观

`spider_configure_session` 可以改：

```json
{
  "sessionId": "...",
  "config": {
    "title": "我的资料助手",
    "subtitle": "EVERY TASK, ONE FORK",
    "modelName": "My Agent",
    "layout": "square",
    "routes": {
      "code":  { "label": "自动处理", "color": "#48D7A6" },
      "model": { "label": "模型判断", "color": "#E18C78" },
      "you":   { "label": "人工复核", "color": "#F4CF4D" }
    },
    "theme": { "accent": "#F34A83", "spider": "#48D7A6", "background": "#080909" }
  }
}
```

**限制**：提示词和任务单位（`unit`）在收到事件后会**锁定** —— 要换就建新会话。

---

## 重要限制（别踩）

| 限制 | 说明 |
|---|---|
| **会话只在内存里** | 进程退出就没了。**退出前必须导出** |
| 单进程最多 **20 个会话** | 超了会报错，已有记录保留 |
| 单会话最多 **10,000 事件 / 16 MiB** | 超了报错，不静默截断 |
| **没有模型调用次数字段** | 所以页面显示"模型调用 未提供"，不会编造 |
| **不执行任何操作** | `code` 分支不跑生成的代码，`model`/`you` 不真的调用或批准 |

---

## 已验证的用法（本次部署）

我刚用这套工具把**本次部署过程**可视化了出来，10 个真实任务：

```
code 6  ── 检查环境 / 下载仓库 / 下载 three / 启动服务器 / 跑测试 / MCP 验证
model 3 ── 分析依赖树 / 编写安装器 / 预下载被卡住的包
you  1 ── 注册到 DSH 并确认加载
```

会话标题「Prompt Spider UI 本地部署」，状态 `COMPLETED`，耗时 8.57 秒，
页面状态栏显示 `真实事件 · 由 Agent 主动上报`。

回放已导出：`exports/dsh-deploy-prompt-spider.json`（11.9 KB，本地产物，不入库）
可以用 `spider_import_session` 重新载入。

---

## 常见问题

**Q：为什么 UI 显示的是固定动画，不是我的任务？**
A：那是「参考演示」模式。要进入实时模式，必须通过 MCP 建会话并上报事件，
或者导入回放 JSON。直接打开 http://127.0.0.1:4317/ 看到的是演示。

**Q：预览链接打不开？**
A：链接里的 `#token=` 不能丢。另外会话关闭后链接就失效了。

**Q：能同时开多个会话吗？**
A：能。每个会话有独立的预览链接，最多 20 个。

**Q：REA 会不会被影响？**
A：不会。两个 MCP server 注册在**同一个 `insert` 列表**里，已实测共存。
配置文件备份在 `~/.dsh/profiles/desktop/cordis.patch.yml.spider.backup`。

---

## 配置改动记录（可回滚）

**改了什么**：在 `~/.dsh/profiles/desktop/cordis.patch.yml` 的**已有 `insert` 列表**里
追加了一个 `prompt-spider-mcp` 条目。

**为什么合并而不是新增一条 `insert`**：
cordis 的 `insert` 语义是"把列表插到目标路径"，两条针对同一路径的 `insert`
谁覆盖谁**没有明确保证**。为了不破坏已有的 REA 注册，必须合并进同一个列表。

**路径为什么用单引号**：
Windows 路径的反斜杠在 YAML 双引号里会被当转义序列 ——
`"C:\Users\249"` 会报 `Invalid escape sequence \U`。单引号才是字面量。

**回滚方法**：
```powershell
Copy-Item "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml.spider.backup" `
          "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml" -Force
```
然后重启 DSH。
