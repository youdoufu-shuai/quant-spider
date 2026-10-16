#!/usr/bin/env node
/**
 * 量化看板服务 —— 静态托管 + 实时数据 API。
 *
 * 为什么单独一个服务，而不是塞进 server.mjs：
 *   server.mjs 是原项目的静态服务器，职责单一、已被 MCP hub 复用。
 *   量化模块需要的是一条**会调 Python 的 API**，塞进去会污染原结构。
 *   所以单独起一个，端口独立，互不影响。
 *
 * 端点：
 *   GET /                     看板页面
 *   GET /api/quant?market=&symbol=&interval=&limit=
 *                             跑一次完整管道，返回 JSON
 *   GET /api/meta             列出市场/标的/粒度/策略（不取行情，很快）
 *   GET /api/health           健康检查
 *
 * 缓存策略：同一组参数的请求在 TTL 内复用结果。
 *   为什么必须缓存：跑一次管道要 0.4–1.8 秒（含真实网络请求），
 *   前端如果每次刷新都打一遍，既慢又容易触发数据源限流
 *   （东方财富就是被我压测打封的）。
 */

import http from 'node:http';
import { execFile } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '..');

const PYTHON = process.env.QUANT_PYTHON
  || String.raw`C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`;
const PIPELINE = path.join(here, 'pipeline.py');
const PORT = Number(process.env.QUANT_PORT || 4400);
const CACHE_TTL_MS = Number(process.env.QUANT_TTL || 20000);

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

// ------------------------------------------------------------
function runPipeline(args, timeoutMs = 90000) {
  return new Promise((resolve, reject) => {
    execFile(PYTHON, [PIPELINE, ...args], {
      encoding: 'utf8',
      maxBuffer: 128 * 1024 * 1024,
      timeout: timeoutMs,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    }, (err, stdout, stderr) => {
      if (err && !stdout) {
        reject(new Error(stderr?.slice(0, 600) || err.message));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (e) {
        reject(new Error(`管道输出不是合法 JSON: ${e.message} | stderr=${stderr?.slice(0, 300)}`));
      }
    });
  });
}

// ------------------------------------------------------------
const cache = new Map();   // key -> { at, data }

async function getQuant(params) {
  const market = params.get('market') || 'ashare';
  const symbol = params.get('symbol') || '';
  const interval = params.get('interval') || '';
  const limit = String(Math.max(60, Math.min(1000, Number(params.get('limit') || 300))));
  const strategy = params.get('strategy') || 'all';
  const key = `${market}|${symbol}|${interval}|${limit}|${strategy}`;

  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) {
    return { ...hit.data, _cache: { hit: true, ageMs: Date.now() - hit.at } };
  }

  const args = ['--market', market, '--limit', limit, '--strategy', strategy];
  if (symbol) args.push('--symbol', symbol);
  if (interval) args.push('--interval', interval);

  const data = await runPipeline(args);
  if (data && !data.error) cache.set(key, { at: Date.now(), data });
  return { ...data, _cache: { hit: false, ageMs: 0 } };
}

// ------------------------------------------------------------
async function serveStatic(req, res, pathname) {
  // /quant/*         -> quant/ 目录
  // /spider.js       -> 主项目的 app/spider.js（量化模块复用蜘蛛绘制代码）
  // 其余              -> quant/web/
  let rel;
  if (pathname === '/spider.js') {
    rel = path.join('app', 'spider.js');
  } else if (pathname.startsWith('/quant/')) {
    rel = pathname.slice(1);
  } else {
    rel = path.join('quant', 'web', pathname === '/' ? 'index.html' : pathname.slice(1));
  }

  const filename = path.resolve(projectRoot, rel);
  if (!filename.startsWith(projectRoot)) { res.writeHead(403).end('Forbidden'); return; }

  try {
    const st = await stat(filename);
    if (!st.isFile()) throw new Error('not a file');
    const ext = path.extname(filename);
    res.setHeader('Content-Type', TYPES[ext] || 'application/octet-stream');
    res.setHeader('Cache-Control', 'no-store');
    res.end(await readFile(filename));
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('Not found: ' + rel);
  }
}

// ------------------------------------------------------------
/** 通用 Python 调用：跑指定脚本，返回解析后的 JSON */
function runPython(script, args, timeoutMs = 90000) {
  return new Promise((resolve, reject) => {
    execFile(PYTHON, [script, ...args], {
      encoding: 'utf8',
      maxBuffer: 128 * 1024 * 1024,
      timeout: timeoutMs,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    }, (err, stdout, stderr) => {
      // 注意：CLI 会用非零退出码表示"业务性失败"（会话不存在等），
      // 但 stdout 里仍有合法 JSON。所以优先尝试解析 stdout。
      const text = (stdout || '').trim();
      if (text) {
        try {
          const parsed = JSON.parse(text);
          resolve(parsed);
          return;
        } catch { /* 落到下面的错误分支 */ }
      }
      reject(new Error((stderr || err?.message || 'unknown').slice(0, 800)));
    });
  });
}

async function readBody(req, limit = 64 * 1024) {
  let size = 0;
  const parts = [];
  for await (const part of req) {
    size += part.length;
    if (size > limit) throw new Error('请求体过大');
    parts.push(part);
  }
  const text = Buffer.concat(parts).toString('utf8');
  return text ? JSON.parse(text) : {};
}

/**
 * 模拟盘 API 路由。
 *
 *   GET  /api/paper/meta              市场/策略元信息（毫秒级）
 *   GET  /api/paper/sessions          会话列表
 *   POST /api/paper/create            建会话（会拉一次真实行情）
 *   GET  /api/paper/:id               账户快照
 *   POST /api/paper/:id/refresh       拉行情并推进账户
 *   POST /api/paper/:id/order         下单
 *   POST /api/paper/:id/cancel        撤单（可只撤一笔）
 *   POST /api/paper/:id/auto          开关/配置自动交易 {on,mode,strategy,config}
 *   POST /api/paper/:id/decide        手动跑一次自动决策（调试/演示）
 *   POST /api/paper/:id/risk          查看/修改风控参数 {config}
 *   POST /api/paper/:id/reset         重置账户
 *   DELETE /api/paper/:id             删除会话
 */
async function handlePaper(req, res, pathname, url) {
  const json = (code, obj) => {
    res.writeHead(code, { 'Content-Type': TYPES['.json'], 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(obj));
  };
  const CLI = path.join(here, 'paper_cli.py');
  const SYMBOLS_CLI = path.join(here, 'symbols.py');
  const method = req.method || 'GET';

  try {
    // --- 无会话参数的路由 ---
    if (pathname === '/api/paper/meta') {
      json(200, await runPython(CLI, ['meta'], 20000));
      return;
    }
    if (pathname === '/api/paper/sessions' && method === 'GET') {
      json(200, await runPython(CLI, ['list'], 30000));
      return;
    }
    if (pathname === '/api/paper/create' && method === 'POST') {
      const b = await readBody(req);
      // 先在 Node 侧校验，避免把 argparse 的 usage 文本当错误返回给前端
      const market = String(b.market || 'ashare');
      if (!/^[a-z]+$/.test(market)) {
        json(400, { error: `非法市场：${market}` });
        return;
      }
      const args = ['create', '--market', market];
      if (b.symbol) args.push('--symbol', String(b.symbol));
      if (b.interval) args.push('--interval', String(b.interval));
      if (b.initial) {
        const v = Number(b.initial);
        if (!Number.isFinite(v) || v <= 0) { json(400, { error: '初始资金必须是正数' }); return; }
        args.push('--initial', String(v));
      }
      if (b.label) args.push('--label', String(b.label).slice(0, 60));
      const r = await runPython(CLI, args, 120000);
      if (r.error) {
        // 未知市场这类错误来自 argparse（写了 stderr），这里给出可读的替代
        const msg = /usage:/i.test(r.error)
          ? `创建失败：市场或参数不合法（market=${market} symbol=${b.symbol || '默认'} interval=${b.interval || '默认'}）`
          : r.error;
        json(400, { error: msg });
        return;
      }
      json(200, r);
      return;
    }

    // --- 带 :id 的路由 ---
    const m = /^\/api\/paper\/([a-f0-9-]{8,40})(?:\/(refresh|order|cancel|auto|reset|decide|risk))?$/i.exec(pathname);
    if (!m) { json(404, { error: '未知的模拟盘路由: ' + pathname }); return; }
    const id = m[1];
    const action = m[2] || null;

    if (!action && method === 'GET') {
      const r = await runPython(CLI, ['get', '--id', id], 30000);
      json(r.error ? 404 : 200, r);
      return;
    }
    if (!action && method === 'DELETE') {
      json(200, await runPython(CLI, ['delete', '--id', id], 20000));
      return;
    }
    if (action === 'refresh' && method === 'POST') {
      const b = await readBody(req).catch(() => ({}));
      const args = ['refresh', '--id', id];
      if (b.limit) args.push('--limit', String(Math.max(60, Math.min(1000, Number(b.limit)))));
      const r = await runPython(CLI, args, 120000);
      json(r.error ? 400 : 200, r);
      return;
    }
    if (action === 'order' && method === 'POST') {
      const b = await readBody(req);
      if (!b.side) { json(400, { error: '缺少 side（buy / sell）' }); return; }
      const args = ['order', '--id', id, '--side', String(b.side)];
      if (b.type) args.push('--type', String(b.type));
      if (b.qty !== undefined && b.qty !== null && b.qty !== '') args.push('--qty', String(Number(b.qty)));
      if (b.reason) args.push('--reason', String(b.reason).slice(0, 120));
      const r = await runPython(CLI, args, 120000);
      json(r.error ? 400 : 200, r);
      return;
    }
    if (action === 'cancel' && method === 'POST') {
      const b = await readBody(req).catch(() => ({}));
      const args = ['cancel', '--id', id];
      if (b.order) args.push('--order', String(b.order));
      const r = await runPython(CLI, args, 30000);
      json(r.error ? 400 : 200, r);
      return;
    }
    if (action === 'auto' && method === 'POST') {
      const b = await readBody(req);
      // 先在 Node 侧校验，避免把 argparse 的 usage 文本当错误返回给前端
      const MODES = ['off', 'score', 'confirm', 'signal', 'ai'];
      if (b.mode && !MODES.includes(String(b.mode))) {
        json(400, { error: `非法模式 ${b.mode}（可选：${MODES.join(' / ')}）` });
        return;
      }
      const args = ['auto', '--id', id, b.on ? '--on' : '--off'];
      if (b.strategy) args.push('--strategy', String(b.strategy));
      if (b.mode) args.push('--mode', String(b.mode));
      if (b.config && typeof b.config === 'object') {
        args.push('--config', JSON.stringify(b.config));
      }
      const r = await runPython(CLI, args, 90000);
      if (r.error) {
        const msg = /usage:/i.test(r.error)
          ? `自动交易配置不合法（mode=${b.mode || '默认'} strategy=${b.strategy || '默认'}）`
          : r.error;
        json(400, { error: msg });
        return;
      }
      json(200, r);
      return;
    }
    if (action === 'decide' && method === 'POST') {
      const r = await runPython(CLI, ['decide', '--id', id], 120000);
      json(r.error ? 400 : 200, r);
      return;
    }
    if (action === 'risk' && method === 'POST') {
      const b = await readBody(req).catch(() => ({}));
      const args = ['risk', '--id', id];
      if (b.config && typeof b.config === 'object') {
        args.push('--config', JSON.stringify(b.config));
      }
      const r = await runPython(CLI, args, 60000);
      json(r.error ? 400 : 200, r);
      return;
    }
    if (action === 'reset' && method === 'POST') {
      const b = await readBody(req).catch(() => ({}));
      const args = ['reset', '--id', id];
      if (b.initial) args.push('--initial', String(Number(b.initial)));
      const r = await runPython(CLI, args, 30000);
      json(r.error ? 400 : 200, r);
      return;
    }

    json(405, { error: `${method} 不支持该路由` });
  } catch (err) {
    json(500, { error: String(err.message || err) });
  }
}

// ------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const pathname = decodeURIComponent(url.pathname);

  try {
    if (pathname === '/api/health') {
      res.setHeader('Content-Type', TYPES['.json']);
      res.end(JSON.stringify({ ok: true, python: PYTHON, cached: cache.size }));
      return;
    }

    if (pathname === '/api/meta') {
      // 走 --meta：不取行情，毫秒级返回，供前端初始化下拉框
      const data = await runPipeline(['--meta'], 20000);
      res.setHeader('Content-Type', TYPES['.json']);
      res.setHeader('Cache-Control', 'no-store');
      res.end(JSON.stringify(data));
      return;
    }

    if (pathname === '/api/quant') {
      const t0 = Date.now();
      const data = await getQuant(url.searchParams);
      res.setHeader('Content-Type', TYPES['.json']);
      res.setHeader('X-Quant-Ms', String(Date.now() - t0));
      if (data.error) res.statusCode = 502;
      res.end(JSON.stringify(data));
      return;
    }

    // ── 模拟盘 API ──────────────────────────────────────────
    // 全部转发给 paper_cli.py。状态存在 Python 侧的 JSON 文件里，
    // Node 不持有账户状态 —— 所以重启这个服务不会丢账户。
    if (pathname.startsWith('/api/paper')) {
      await handlePaper(req, res, pathname, url);
      return;
    }

    // ── 标的搜索 ────────────────────────────────────────────
    // A 股 6741 只、加密 511 对，不可能全塞进 <select>（会卡死浏览器）。
    // 所以下拉只给常用项，其余走搜索。
    // ⚠️ 这些路由必须在 serveStatic 之前，否则会被当成静态文件路径 404。
    if (pathname.startsWith('/api/symbols/')) {
      const SYMBOLS_CLI = path.join(here, 'symbols.py');
      const market = String(url.searchParams.get('market') || 'ashare');
      const q = String(url.searchParams.get('q') || '').trim();
      const limit = Math.max(1, Math.min(500, Number(url.searchParams.get('limit')) || 40));
      const reply = r => {
        res.writeHead(200, { 'Content-Type': TYPES['.json'], 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(r));
      };

      if (pathname === '/api/symbols/counts') {
        reply(await runPython(SYMBOLS_CLI, ['--counts'], 240000));
        return;
      }
      if (pathname === '/api/symbols/search' || pathname === '/api/symbols/list') {
        if (!q && pathname === '/api/symbols/search') { reply({ results: [] }); return; }
        const args = q
          ? ['--search', q, '--market', market, '--limit', String(limit), '--json']
          : ['--list', market, '--limit', String(limit), '--json'];
        const r = await runPython(SYMBOLS_CLI, args, q ? 90000 : 300000);
        reply({ results: Array.isArray(r) ? r : [] });
        return;
      }
      res.writeHead(404, { 'Content-Type': TYPES['.json'] });
      res.end(JSON.stringify({ error: '未知的标的接口: ' + pathname }));
      return;
    }

    await serveStatic(req, res, pathname);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': TYPES['.json'] });
    res.end(JSON.stringify({ error: String(err.message || err) }));
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`量化看板 → http://127.0.0.1:${PORT}/`);
  console.log(`  模拟盘   http://127.0.0.1:${PORT}/paper.html`);
  console.log(`  Python   ${PYTHON}`);
  console.log(`  管道     ${PIPELINE}`);
  console.log(`  缓存 TTL ${CACHE_TTL_MS} ms`);
  console.log(`  ⚠ 模拟盘：只读行情，无交易接口，不产生任何真实委托`);
});
