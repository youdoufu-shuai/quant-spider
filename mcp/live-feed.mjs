#!/usr/bin/env node
/**
 * Prompt Spider ← DSH 步骤级实时喂养器
 *
 * 与上一版的区别：粒度从「一轮 = 一个任务」降到「一次工具调用 = 一个任务」。
 *
 * 为什么：一个 turn 平均有 29.6 次工具调用（最多 92 次）。
 *   用 turn 做单位时画布上只有几十个点，看不出"Agent 正在干什么"。
 *   降到步骤级后你能看到每一次读文件、跑命令、写代码 —— 这才是演示过程。
 *
 * ── 数据源（踩坑记录）────────────────────────────────────────────
 * ~/.dsh/sessions/<...>/<id>/session.v4.jsonl.zstd
 *   ★ 多帧追加式 zstd：必须按魔数 28 B5 2F FD 拆帧逐帧解压。
 *     整体解压只得到第一帧（244 字节会话头），会让人误判"日志没内容"。
 *     逐帧解得：tool/call(含工具名与参数) / tool/result / step/* / turn/* /
 *     assistant/message(含叙述) / approval/asked / workspace/changes
 *
 *   同目录的 storages/session_projcache/*.json 是 UI 状态缓存，
 *   其 turnOutline.response 是截断预览（约 120 字），做语义分类信号极弱，不用。
 *
 * ── 分流依据（硬信号）────────────────────────────────────────────
 *   you   —— 工具本身要人（ask_user_question/exit_plan_mode/update_goal…），
 *            或该步骤触发了 approval/asked
 *   model —— 委派类工具（subagent/workflow/spawn_teammate/team_*）
 *   code  —— 其余全部（read/grep/pwsh/edit/write…）
 *
 * ── 用法 ────────────────────────────────────────────────────────
 *   node mcp/live-feed.mjs                  持续运行（Ctrl+C 结束）
 *   node mcp/live-feed.mjs --max 240        画布窗口内最多几步（默认 180）
 *   node mcp/live-feed.mjs --interval 1.2   轮询间隔秒
 *   node mcp/live-feed.mjs --session <id>   指定会话（默认最近修改）
 *   node mcp/live-feed.mjs --verbose        打印更多细节
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { zstdDecompressSync } from 'node:zlib';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { publishSession, unpublishSession } from './discovery.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const getOpt = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const INTERVAL = Number(getOpt('--interval', '1.5')) * 1000;
const MAX_STEPS = Number(getOpt('--max', '180'));
const SESSION_ARG = getOpt('--session', null);
const VERBOSE = args.includes('--verbose');
const NO_DISCOVER = args.includes('--no-discover');

const DSH = path.join(homedir(), '.dsh');
const SESS_ROOT = path.join(DSH, 'sessions');

// ============================================================
//  工具名 -> 中文动作，以及参数摘要
// ============================================================
const TOOL_LABEL = {
  read: '读取文件', write: '写入文件', edit: '修改文件',
  grep: '搜索内容', glob: '查找文件',
  pwsh: '执行命令', present: '交付产物', skill: '加载技能',
  todo_write: '更新任务清单', read_image: '读取图像',
  subagent: '派出子代理', subagent_fork: '派生代理',
  workflow: '编排工作流', spawn_teammate: '创建队友',
  send_message: '发送消息', wait_agent: '等待队友',
  list_agents: '查看队友', interrupt_agent: '中断队友',
  team_task_create: '建团队任务', team_task_list: '列团队任务',
  team_task_get: '读团队任务', team_task_update: '改团队任务',
  ask_user_question: '请求人工确认', exit_plan_mode: '提交计划待批',
  update_goal: '更新目标', create_goal: '创建目标', get_goal: '读取目标',
  job_output: '收取后台结果', job_kill: '结束后台任务', job_list: '查看后台任务',
  load_workspace_dependencies: '加载工具链',
  web_search: '联网检索', web_fetch: '抓取网页',
  schedule_create: '创建定时', schedule_list: '查看定时',
  schedule_update: '修改定时', schedule_delete: '删除定时',
  list_mcp_resources: '列出 MCP 资源', read_mcp_resource: '读取 MCP 资源',
  list_mcp_resource_templates: '列出 MCP 模板',
};

const MODEL_TOOLS = new Set([
  'subagent', 'subagent_fork', 'task', 'workflow', 'spawn_teammate',
  'team_task_create', 'team_task_list', 'team_task_get', 'team_task_update',
  'send_message', 'wait_agent', 'list_agents', 'interrupt_agent',
]);
const YOU_TOOLS = new Set([
  'ask_user_question', 'exit_plan_mode', 'update_goal', 'create_goal',
  'job_kill', 'schedule_create', 'schedule_update', 'schedule_delete',
]);

function classifyTool(name) {
  if (YOU_TOOLS.has(name)) return 'you';
  if (MODEL_TOOLS.has(name)) return 'model';
  return 'code';
}

function summarizeArgs(name, rawArgs) {
  if (!rawArgs) return '';
  let a;
  try { a = JSON.parse(rawArgs); } catch { return String(rawArgs).replace(/\s+/g, ' ').slice(0, 70); }
  const clip = (s, n = 70) => {
    const t = String(s ?? '').replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '…' : t;
  };
  switch (name) {
    case 'read': case 'write': case 'edit': case 'read_image':
      return clip(a.file_path || a.path || '');
    case 'grep': case 'glob':
      return clip(a.pattern || '');
    case 'pwsh':
      return clip(a.command || '', 96);
    case 'web_search':
      return clip((a.queries || []).join(' / '));
    case 'web_fetch':
      return clip(a.url || '');
    case 'present':
      return clip((a.files || []).map((f) => f.path).join(', '));
    case 'subagent': case 'subagent_fork':
      return clip(a.description || a.prompt || '');
    case 'job_output': case 'job_kill':
      return clip(a.job_id || '');
    case 'update_goal': case 'create_goal': case 'get_goal':
      return clip(a.action ? `action=${a.action}` : '');
    default: {
      const first = Object.values(a)[0];
      return clip(typeof first === 'string' ? first : JSON.stringify(first ?? ''));
    }
  }
}

// ============================================================
//  多帧 zstd
// ============================================================
function findFrames(buf, from = 0) {
  const out = [];
  for (let i = from; i + 4 <= buf.length; i++) {
    if (buf[i] === 0x28 && buf[i + 1] === 0xb5 && buf[i + 2] === 0x2f && buf[i + 3] === 0xfd) out.push(i);
  }
  return out;
}

function findLogPath(sessionArg) {
  const cands = [];
  let dirs = [];
  try { dirs = readdirSync(SESS_ROOT, { withFileTypes: true }); } catch { return null; }
  for (const d of dirs) {
    if (!d.isDirectory()) continue;
    const sub = path.join(SESS_ROOT, d.name);
    let subs = [];
    try { subs = readdirSync(sub, { withFileTypes: true }); } catch { continue; }
    for (const s of subs) {
      if (!s.isDirectory()) continue;
      const log = path.join(sub, s.name, 'session.v4.jsonl.zstd');
      try {
        const st = statSync(log);
        cands.push({ log, id: s.name.replace(/^session-/, ''), mtime: st.mtimeMs, size: st.size });
      } catch { /* 无 */ }
    }
  }
  cands.sort((a, b) => b.mtime - a.mtime);
  if (sessionArg) {
    const hit = cands.find((c) => c.id === sessionArg || c.id.endsWith(sessionArg));
    if (hit) return hit;
  }
  return cands[0] ?? null;
}

/** 增量读取器：只解新帧，且保留最后一帧（可能正在写入） */
class StepLogReader {
  constructor(logPath) {
    this.logPath = logPath;
    this.bytePos = 0;
    this.steps = [];          // 每个元素 = 一次工具调用
    this.turns = new Map();   // turn -> { prompt, ended }
    this.currentTurn = 0;
    this.lastNarration = '';
    this._pendingByCallId = new Map();
  }

  poll() {
    let buf;
    try { buf = readFileSync(this.logPath); } catch { return false; }
    if (buf.length <= this.bytePos) return false;
    const frames = findFrames(buf, this.bytePos);
    if (frames.length < 2) return false;
    const usable = frames.slice(0, -1);

    let text = '';
    for (let k = 0; k < usable.length; k++) {
      const start = usable[k];
      const end = (k + 1 < usable.length) ? usable[k + 1] : (frames[usable.length] ?? buf.length);
      try { text += zstdDecompressSync(buf.subarray(start, end)).toString('utf8'); } catch { /* 半个帧 */ }
    }
    this.bytePos = usable[usable.length - 1];
    if (!text) return false;

    let added = 0;
    for (const line of text.split('\n')) {
      if (!line) continue;
      let o; try { o = JSON.parse(line); } catch { continue; }
      added += this._ingest(o);
    }
    return added > 0;
  }

  _ingest(o) {
    const d = o.data || {};
    switch (o.type) {
      case 'turn/start':
        this.currentTurn = d.turn;
        if (!this.turns.has(d.turn)) this.turns.set(d.turn, { prompt: '', ended: false });
        return 1;
      case 'user/message': {
        const t = this.turns.get(this.currentTurn);
        if (t) {
          const txt = (d.content || []).filter((c) => c.type === 'text').map((c) => c.text).join(' ');
          if (txt) t.prompt = (t.prompt ? t.prompt + ' ' : '') + txt;
        }
        return 1;
      }
      case 'tool/call': {
        this.currentTurn = d.turn;
        const name = d.name || '?';
        const rec = {
          turn: d.turn, step: d.step, name,
          label: TOOL_LABEL[name] || name,
          summary: summarizeArgs(name, d.arguments),
          route: classifyTool(name),
          done: false,
          ok: null,
          requiresApproval: false,
        };
        this.steps.push(rec);
        if (d.callId) this._pendingByCallId.set(d.callId, rec);
        return 1;
      }
      case 'tool/result': {
        const cid = d.toolCallId || d.message?.toolCallId;
        let rec = cid ? this._pendingByCallId.get(cid) : null;
        if (!rec) {
          // 兜底：找最后一个未完成的
          for (let i = this.steps.length - 1; i >= 0; i--) {
            if (!this.steps[i].done) { rec = this.steps[i]; break; }
          }
        }
        if (rec) {
          rec.done = true;
          const txt = JSON.stringify(d).slice(0, 200);
          rec.ok = !/\b(error|failed|拒绝|错误)\b/i.test(txt);
          if (cid) this._pendingByCallId.delete(cid);
        }
        return 1;
      }
      case 'approval/asked': {
        const rec = d.callId ? this._pendingByCallId.get(d.callId) : null;
        const target = rec || this.steps[this.steps.length - 1];
        if (target) {
          target.route = 'you';
          target.requiresApproval = true;
          target.approvalReason = String(d.reason || '').slice(0, 120);
        }
        return 1;
      }
      case 'assistant/message': {
        const txt = (d.message?.content || [])
          .filter((c) => c.type === 'text').map((c) => c.text).join(' ').trim();
        if (txt) this.lastNarration = txt.replace(/\s+/g, ' ');
        return 1;
      }
      case 'turn/end': {
        const t = this.turns.get(d.turn);
        if (t) t.ended = true;
        return 1;
      }
      default:
        return 0;
    }
  }

  /** 当前正在执行的那一步 */
  currentStep() {
    for (let i = this.steps.length - 1; i >= 0; i--) if (!this.steps[i].done) return this.steps[i];
    return this.steps[this.steps.length - 1] ?? null;
  }
}

// ============================================================
//  主流程
// ============================================================
const target = findLogPath(SESSION_ARG);
if (!target) { console.error('找不到 DSH 会话日志'); process.exit(1); }

console.log('');
console.log('╔══════════════════════════════════════════════════════════════╗');
console.log('║  Prompt Spider ← DSH   步骤级实时喂养器                       ║');
console.log('╚══════════════════════════════════════════════════════════════╝');
console.log('');
console.log('  会话 id : ' + target.id);
console.log('  日志    : ' + (target.size / 1024 / 1024).toFixed(2) + ' MB');
console.log('  粒度    : 步骤级（一次工具调用 = 一个任务）');
console.log('  窗口上限: ' + MAX_STEPS + ' 步');
console.log('');

const reader = new StepLogReader(target.log);
reader.poll();

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(here, 'server.mjs')],
  stderr: 'pipe',
});
const client = new Client({ name: 'dsh-step-feed', version: '3.0.0' },
  { versionNegotiation: { mode: 'legacy' } });
transport.stderr?.on('data', () => {});
await client.connect(transport);

const firstTurn = reader.turns.get(1);
const titleSrc = String(firstTurn?.prompt || '').replace(/\s+/g, ' ').trim();
const title = titleSrc
  ? (titleSrc.length > 52 ? titleSrc.slice(0, 52) + '…' : titleSrc)
  : 'DSH 实时会话';

const created = await client.callTool({
  name: 'spider_create_session',
  arguments: {
    config: {
      title,
      subtitle: 'DSH STEP-LEVEL LIVE STREAM',
      modelName: 'DeepSeek Harness',
      layout: 'landscape',
      unit: 'task',
      prompt: [
        '本面板按步骤级跟随 DSH Agent：每一个点 = 一次真实工具调用。',
        '',
        '数据源：session.v4.jsonl.zstd（多帧追加式 zstd，按魔数拆帧解压）',
        '',
        '分流依据：',
        '  code  —— 读文件 / 搜索 / 执行命令 / 写文件',
        '  model —— 派出子代理 / 编排工作流 / 团队协作',
        '  you   —— 请求人工确认 / 需要授权',
      ].join('\n'),
    },
  },
});
const SESSION_ID = created.structuredContent?.id;
const PREVIEW = created.structuredContent?.previewUrl;
if (!SESSION_ID) { console.error('创建会话失败:', JSON.stringify(created).slice(0, 400)); process.exit(1); }

/**
 * 发布到发现文件 —— 让稳定入口页 http://127.0.0.1:4317/live 总能找到当前会话。
 *
 * 为什么需要：MCP hub 每个进程用随机端口（listen(0)）+ 随机 token，
 * 所以每次重启喂养器预览 URL 都会变（实测 49909→62664→65221→53198…）。
 * 浏览器里打开的旧链接会指向已死的会话，画面停住不动 —— 看起来像"没动画"。
 */
const startedAt = Date.now();
function publish(extra = {}) {
  if (NO_DISCOVER) return;
  try {
    publishSession({
      sessionId: SESSION_ID,
      previewUrl: PREVIEW,
      title: title.slice(0, 60),
      market: 'agent',
      startedAt,
      steps: reader.steps.length,
      turns: reader.turns.size,
      current: reader.currentStep()?.label || '',
      ...extra,
    });
  } catch (e) {
    console.error('  ⚠ 写发现文件失败（不影响主流程）:', e.message);
  }
}
publish();

console.log('  预览链接（打开看实时过程）:');
console.log('');
console.log('    ' + PREVIEW);
console.log('');
if (!NO_DISCOVER) {
  console.log('  ★ 稳定入口（推荐收藏这个，永远指向当前会话）:');
  console.log('      http://127.0.0.1:4317/live');
  console.log('');
}

// ---------- 推送 ----------
let pushedUpto = 0;    // 已推送到 all 的哪个绝对下标
let lastSub = '';
let lastStateKey = '';

/** 把一条步骤转成 MCP 事件 */
function toEvent(absIdx, s, isCurrent) {
  const status = s.done
    ? (s.ok === false ? 'failed' : 'completed')
    : 'running';
  const detailParts = [`turn ${s.turn} / step ${s.step}`, s.name];
  if (s.requiresApproval) detailParts.push('已请求人工授权');
  if (s.approvalReason) detailParts.push(s.approvalReason);
  if (s.summary) detailParts.push(s.summary);
  return {
    id: `s${absIdx}`,
    label: `${s.label}${s.summary ? ' · ' + s.summary : ''}`.slice(0, 190),
    route: s.route,
    status: isCurrent && !s.done ? 'running' : status,
    detail: detailParts.join(' · ').slice(0, 4000),
  };
}

async function sync() {
  const changed = reader.poll();
  const all = reader.steps;
  if (!all.length) return;

  const base = Math.max(0, all.length - MAX_STEPS);
  const cur = reader.currentStep();
  const curIdx = cur ? all.indexOf(cur) : -1;

  // 需要推送的：新步骤 + 当前步骤的状态变化
  const events = [];
  for (let i = Math.max(pushedUpto, base); i < all.length; i++) {
    events.push(toEvent(i, all[i], i === curIdx));
  }
  // 当前步的状态可能只是"完成"了，也要刷新
  const stateKey = cur ? `${curIdx}:${cur.done}:${cur.requiresApproval}` : '';
  if (cur && curIdx >= 0 && curIdx < pushedUpto && stateKey !== lastStateKey) {
    events.push(toEvent(curIdx, cur, true));
  }
  lastStateKey = stateKey;

  if (!events.length && !changed) return;
  pushedUpto = all.length;

  // 状态行只在真的推进时打印，避免空闲时每 2 秒刷一行 "+0" 噪音
  if (events.length === 0) return;

  for (let i = 0; i < events.length; i += 100) {
    try {
      await client.callTool({
        name: 'spider_update_events',
        arguments: { sessionId: SESSION_ID, events: events.slice(i, i + 100) },
      });
    } catch (e) {
      console.log('  推送失败: ' + e.message);
      return;
    }
  }

  // 实时状态行
  const sub = cur
    ? `${reader.turns.size} 轮 · ${all.length} 步 · 正在进行「${cur.label}」`
    : `${all.length} 步`;
  if (sub !== lastSub) {
    lastSub = sub;
    try {
      await client.callTool({
        name: 'spider_configure_session',
        arguments: { sessionId: SESSION_ID, config: { subtitle: sub.slice(0, 160) } },
      });
    } catch { /* 忽略 */ }
  }

  const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  console.log(`  [${ts}] ${reader.turns.size} 轮 / ${all.length} 步（+${events.length}）` +
    (cur ? ` · 正在「${cur.label}」${cur.summary ? ' → ' + cur.summary.slice(0, 46) : ''}` : ''));
  if (VERBOSE && reader.lastNarration) {
    console.log(`         叙述: ${reader.lastNarration.slice(0, 90)}`);
  }
}

/** 心跳：即使没有新事件也要刷新发现文件的时间戳，否则会被判为过期 */
function heartbeat() {
  publish({ steps: reader.steps.length, turns: reader.turns.size,
            current: reader.currentStep()?.label || '' });
}

await sync();
console.log('');
console.log('  已开始持续跟随。Agent 每调用一次工具，画布就多一个点。');
console.log('  按 Ctrl+C 结束。');
console.log('');

let stop = false;
const shutdown = async () => {
  if (stop) return;
  stop = true;
  console.log('\n  正在收尾…');
  try { unpublishSession(); } catch { /* ignore */ }
  try {
    await client.callTool({ name: 'spider_complete_session', arguments: { sessionId: SESSION_ID, status: 'completed' } });
    console.log('  已标记完成，历史保留可循环回看。');
  } catch { /* ignore */ }
  try { await client.close(); } catch { /* ignore */ }
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
// 进程被杀（SIGKILL 之外的异常退出）也要清理，避免发现文件里留死链接
process.on('exit', () => { try { unpublishSession(); } catch { /* ignore */ } });

while (!stop) {
  await new Promise((r) => setTimeout(r, INTERVAL));
  try {
    await sync();
    heartbeat();
  } catch (e) { console.log('  同步出错（重试中）: ' + e.message); }
}
