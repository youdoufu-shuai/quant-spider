#!/usr/bin/env node
/**
 * MCP 接入验证 —— 独立于项目自带的 protocol.test.mjs
 *
 * 为什么单独写：项目自带的测试有一项会调用 `npm --prefix mcp ci` 去验证
 * 「导出的独立项目能自举」，但本机 Node 是精简发行版（没有 npm），
 * 那一项必然失败。其余协议验证与 npm 无关，值得单独跑一遍。
 *
 * 本脚本验证：
 *   1. 官方 MCP client 能通过 stdio 连上 server
 *   2. 工具 / 资源 / prompt 清单齐全（11 / 3 / 1）
 *   3. 创建会话返回可用的 previewUrl
 *   4. 上报事件能被接受，非法事件被拒绝
 *   5. 预览页确实能打开（HTTP 200）且带 token
 *
 * 用法：node mcp/verify-mcp.mjs
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const entry = path.join(here, 'server.mjs');

let pass = 0;
let fail = 0;
const check = (name, cond, detail = '') => {
  if (cond) {
    pass++;
    console.log(`  [PASS] ${name}${detail ? ' -- ' + detail : ''}`);
  } else {
    fail++;
    console.log(`  [FAIL] ${name}${detail ? ' -- ' + detail : ''}`);
  }
};

console.log('=== MCP 接入验证 ===');
console.log(`  server: ${entry}`);

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [entry],
  stderr: 'pipe',
});
const client = new Client(
  { name: 'spider-verify', version: '1.0.0' },
  { versionNegotiation: { mode: 'legacy' } },
);

let stderrBuf = '';
transport.stderr?.on('data', (d) => (stderrBuf += String(d)));

try {
  await client.connect(transport);
  check('stdio 连接成功', true);

  // ---- 清单 ----
  const tools = await client.listTools();
  check('工具数量 = 11', tools.tools.length === 11, `实际 ${tools.tools.length}`);
  const toolNames = tools.tools.map((t) => t.name);
  for (const want of ['spider_create_session', 'spider_update_events', 'spider_export_project']) {
    check(`包含工具 ${want}`, toolNames.includes(want));
  }

  const resources = await client.listResources();
  check('资源数量 = 3', resources.resources.length === 3, `实际 ${resources.resources.length}`);

  const prompts = await client.listPrompts();
  check('prompt 数量 = 1', prompts.prompts.length === 1, `实际 ${prompts.prompts.length}`);

  const guide = await client.readResource({ uri: 'spider://guide' });
  check('可读取 spider://guide', /spider_update_events/.test(guide.contents[0].text));

  // ---- 创建会话 ----
  const session = await client.callTool({
    name: 'spider_create_session',
    arguments: { config: { title: '本地部署验证', layout: 'landscape' } },
  });
  const s = session.structuredContent;
  check('创建会话成功', !!s && !session.isError, s ? `id=${s.id}` : '无 structuredContent');
  check('返回 previewUrl', !!(s && s.previewUrl), s?.previewUrl ?? '');
  check('previewUrl 带 token', !!(s && s.previewUrl && s.previewUrl.includes('#token=')));

  // ---- 预览页可访问 ----
  if (s?.previewUrl) {
    const u = new URL(s.previewUrl);
    const token = new URLSearchParams(u.hash.slice(1)).get('token');
    const pageRes = await fetch(`${u.origin}${u.pathname}?token=${token}`);
    check('预览页 HTTP 200', pageRes.status === 200, `${u.origin}${u.pathname}`);
    const body = await pageRes.text();
    check('预览页是 UI 而非错误', body.includes('<canvas') || body.includes('spider'), `${body.length} 字节`);
  }

  // ---- 上报事件 ----
  const upd = await client.callTool({
    name: 'spider_update_events',
    arguments: {
      sessionId: s.id,
      events: [
        { id: 'a1', label: '检查输入格式', route: 'code', status: 'completed' },
        { id: 'a2', label: '生成草稿', route: 'model', status: 'running' },
        { id: 'a3', label: '等待人工确认', route: 'you', status: 'pending' },
      ],
    },
  });
  check('上报事件被接受', !upd.isError);

  // ---- 非法事件应被拒绝 ----
  const bad = await client.callTool({
    name: 'spider_update_events',
    arguments: { sessionId: s.id, events: [{ id: 'bad', label: 'X', route: 'not-a-route' }] },
  });
  check('非法 route 被拒绝', bad.isError === true);

  // ---- 同一 id 更新而非重复 ----
  const upd2 = await client.callTool({
    name: 'spider_update_events',
    arguments: { sessionId: s.id, events: [{ id: 'a2', status: 'completed' }] },
  });
  check('同 id 更新状态', !upd2.isError);

  // ---- 收尾 ----
  const done = await client.callTool({
    name: 'spider_complete_session',
    arguments: { sessionId: s.id, status: 'completed' },
  });
  check('完成会话', !done.isError);

  await client.close();
} catch (err) {
  fail++;
  console.log(`  [FAIL] 异常: ${err.message}`);
  if (stderrBuf) console.log(`  server stderr: ${stderrBuf.slice(0, 400)}`);
}

console.log('');
if (stderrBuf && /error|Error/.test(stderrBuf)) {
  console.log('server stderr 片段:');
  console.log('  ' + stderrBuf.slice(0, 500).replace(/\n/g, '\n  '));
}
console.log(`结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
