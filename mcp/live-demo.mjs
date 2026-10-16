#!/usr/bin/env node
/**
 * 实时模式演示 —— 把「固定动画」变成「真实事件流」
 *
 * 为什么需要这个：
 *   项目默认打开的是「参考演示」，那是从视频采样重建的固定 20.92 秒动画，
 *   每次都一样 —— 看起来就像一段视频。
 *   真正的产品形态是 live.js 的「实时会话」：由外部持续上报事件，
 *   UI 跟着真实进度变化。但它需要 MCP 或回放 JSON 才能进入。
 *
 *   本脚本不需要任何 Agent，直接用官方 MCP client 连本地 server，
 *   模拟一个真实的开发任务流（代码检查 → 模型处理 → 人工确认），
 *   在终端打印预览链接。打开链接即可看到实时模式。
 *
 * 用法：node mcp/live-demo.mjs [--speed 1.0] [--keep]
 *   --speed N  事件间隔倍率（默认 1.0；2.0 = 快一倍）
 *   --keep     跑完保持会话不关闭（默认保持，方便你回看）
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

const args = process.argv.slice(2);
const speedArg = args.indexOf('--speed');
const SPEED = speedArg >= 0 ? Number(args[speedArg + 1]) || 1 : 1;

/**
 * 一个真实的开发任务流程。刻意做成混合形态：
 *   code   —— 确定性步骤（读文件、跑测试、构建）
 *   model  —— 需要模型判断的步骤（设计、审查、改写）
 *   you    —— 需要人确认的步骤（批准、发布）
 * 这样三条分支都会亮起来，能看出分流机制。
 */
const TASKS = [
  // ---- 第一阶段：确定性检查 ----
  { id: 'scan',      label: '扫描仓库结构',           route: 'code',  status: 'running',   ms: 1400 },
  { id: 'scan',      label: '扫描仓库结构',           route: 'code',  status: 'completed', ms: 900 },
  { id: 'deps',      label: '检查依赖版本',           route: 'code',  status: 'running',   ms: 1100 },
  { id: 'deps',      label: '检查依赖版本',           route: 'code',  status: 'completed', ms: 700 },
  { id: 'lint',      label: '静态检查',               route: 'code',  status: 'running',   ms: 1300 },
  { id: 'lint',      label: '静态检查',               route: 'code',  status: 'completed', ms: 800 },

  // ---- 第二阶段：模型处理 ----
  { id: 'read',      label: '阅读模块与调用关系',      route: 'model', status: 'running',   ms: 1800 },
  { id: 'read',      label: '阅读模块与调用关系',      route: 'model', status: 'completed', ms: 600 },
  { id: 'design',    label: '设计重构方案',           route: 'model', status: 'running',   ms: 2200 },
  { id: 'design',    label: '设计重构方案',           route: 'model', status: 'completed', ms: 700 },
  { id: 'draft',     label: '生成补丁草稿',           route: 'model', status: 'running',   ms: 2000 },
  { id: 'draft',     label: '生成补丁草稿',           route: 'model', status: 'completed', ms: 600 },
  { id: 'review',    label: '自审：边界与错误处理',    route: 'model', status: 'running',   ms: 1600 },
  { id: 'review',    label: '自审：边界与错误处理',    route: 'model', status: 'completed', ms: 500 },

  // ---- 第三阶段：确定性验证 ----
  { id: 'test',      label: '运行测试套件',           route: 'code',  status: 'running',   ms: 1500 },
  { id: 'test',      label: '运行测试套件',           route: 'code',  status: 'completed', ms: 800 },
  { id: 'build',     label: '构建产物',               route: 'code',  status: 'running',   ms: 1200 },
  { id: 'build',     label: '构建产物',               route: 'code',  status: 'completed', ms: 900 },

  // ---- 第四阶段：需要人确认 ----
  { id: 'approve',   label: '等待批准合并',           route: 'you',   status: 'pending',   ms: 2600 },
  { id: 'approve',   label: '等待批准合并',           route: 'you',   status: 'completed', ms: 800 },
  { id: 'publish',   label: '提交并推送',             route: 'you',   status: 'running',   ms: 1400 },
  { id: 'publish',   label: '提交并推送',             route: 'you',   status: 'completed', ms: 700 },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms / SPEED)));

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(here, 'server.mjs')],
  stderr: 'pipe',
});
const client = new Client(
  { name: 'spider-live-demo', version: '1.0.0' },
  { versionNegotiation: { mode: 'legacy' } },
);

let stderrBuf = '';
transport.stderr?.on('data', (d) => (stderrBuf += String(d)));

try {
  await client.connect(transport);

  const session = await client.callTool({
    name: 'spider_create_session',
    arguments: {
      config: {
        title: '重构 prompt-spider-ui 的渲染层',
        subtitle: 'EVERY TASK, ONE FORK',
        modelName: '本地演示 Agent',
        layout: 'landscape',
        unit: 'task',
        expectedTotal: TASKS.length,
        prompt: [
          '目标：重构渲染层，降低单帧开销。',
          '',
          '约束：',
          '1. 不改变现有视觉表现，只优化内部实现。',
          '2. 保持确定性回放：同一时间点必须得到同一状态。',
          '3. 改动必须通过现有 44 项测试。',
          '',
          '流程：先做确定性检查，再让模型设计并生成草稿，',
          '最后运行测试与构建，等人工批准后提交。',
        ].join('\n'),
      },
    },
  });

  const s = session.structuredContent;
  if (!s?.previewUrl) {
    console.error('创建会话失败:', JSON.stringify(session));
    process.exit(1);
  }

  console.log('');
  console.log('╔══════════════════════════════════════════════════════════════╗');
  console.log('║  实时模式已开启 —— 打开下面的链接                             ║');
  console.log('╚══════════════════════════════════════════════════════════════╝');
  console.log('');
  console.log('  ' + s.previewUrl);
  console.log('');
  console.log(`  共 ${TASKS.length} 次上报，速度 ${SPEED}×`);
  console.log('  现在会持续上报事件 —— 页面会跟着真实进度变化，');
  console.log('  而不是播放那段固定动画。');
  console.log('');

  // 把事件逐个上报，每次只更新变化的字段
  for (let i = 0; i < TASKS.length; i++) {
    const t = TASKS[i];
    await client.callTool({
      name: 'spider_update_events',
      arguments: {
        sessionId: s.id,
        events: [{ id: t.id, label: t.label, route: t.route, status: t.status }],
      },
    });
    process.stdout.write(`  [${String(i + 1).padStart(2)}/${TASKS.length}] ${t.route.padEnd(5)} ${t.status.padEnd(9)} ${t.label}\n`);
    await sleep(t.ms);
  }

  console.log('');
  console.log('  全部上报完成。会话保持开启，你可以继续在页面里回看/拖动时间轴。');
  console.log('  按 Ctrl+C 结束（会关闭会话并释放内存记录）。');
  console.log('');

  // 保持会话存活，让用户有时间查看
  await new Promise((resolve) => {
    process.on('SIGINT', resolve);
    process.on('SIGTERM', resolve);
    if (!args.includes('--keep')) setTimeout(resolve, 300000);
  });

  try {
    await client.callTool({
      name: 'spider_complete_session',
      arguments: { sessionId: s.id, status: 'completed' },
    });
  } catch { /* 已断开则忽略 */ }
  await client.close();
  console.log('  会话已关闭。');
} catch (err) {
  console.error('出错:', err.message);
  if (stderrBuf) console.error('server stderr:', stderrBuf.slice(0, 600));
  process.exit(1);
}
