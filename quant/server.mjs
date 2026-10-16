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
import { execFile, spawnSync } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '..');

// ── Python 解释器探测 ────────────────────────────────────────
// ⚠️ 不要写死开发机的绝对路径。
//    最初这里是 String.raw`C:\Users\24942\...\python.exe` —— 只在那台机器上能跑，
//    别人 clone 下来直接起不来，Docker 里也得靠 ENV 覆盖。
//    现在按顺序探测：环境变量 → 当前运行的 Python → 平台常规命令名。
const PYTHON_CANDIDATES = [
  process.env.QUANT_PYTHON,
  process.env.PYTHON,
  // Windows 上通常叫 python，类 Unix 上通常叫 python3
  process.platform === 'win32' ? 'python' : 'python3',
  'python3',
  'python',
].filter(Boolean);

/** 探测第一个可用的 Python。返回 null 表示都没找到。 */
function detectPython() {
  for (const cand of PYTHON_CANDIDATES) {
    try {
      // 只做一次极轻量的探测，避免启动时卡住
      const r = spawnSync(cand, ['-c', 'import sys;print(sys.version_info[0])'], {
        encoding: 'utf8', timeout: 5000,
      });
      if (r.status === 0 && String(r.stdout).trim().startsWith('3')) return cand;
    } catch { /* 试下一个 */ }
  }
  return null;
}

const PYTHON = detectPython();
if (!PYTHON) {
  console.error('❌ 找不到可用的 Python 3。');
  console.error('   请安装 Python 3.10+，或用环境变量指定：');
  console.error('     QUANT_PYTHON=/usr/bin/python3   （或 Windows 上的完整路径）');
  process.exit(1);
}

const PIPELINE = path.join(here, 'pipeline.py');
const PORT = Number(process.env.QUANT_PORT || 4400);

// 监听地址可配置。
// ⚠️ 默认只监听回环地址 —— 这是**安全默认值**：本地工具不该默认暴露到局域网。
//    Docker 里需要 0.0.0.0 才能让 -p 端口转发生效，所以允许覆盖，
//    但覆盖时会在启动日志里明确警告。
const HOST = process.env.QUANT_HOST || '127.0.0.1';
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
    // ── 决策快照 API（供「决策蛛网」可视化）────────────────
    // 无状态：给定标的与参数，算出「此刻会怎么决策」的完整分解。
    // 与 /api/paper/:id/decide 的区别：那个依赖某个模拟盘会话的状态，
    // 这个不需要任何会话，纯粹是「如果现在按这套参数决策，结果是什么」。
    if (pathname === '/api/decision' && req.method === 'GET') {
      const u = new URL(req.url, 'http://x');
      const market = u.searchParams.get('market') || 'ashare';
      const args = [
        '--market', market,
        '--interval', u.searchParams.get('interval') || '1d',
        '--limit', String(Math.max(120, Math.min(2000,
          Number(u.searchParams.get('limit')) || 500))),
        '--compact',
      ];
      const sym = u.searchParams.get('symbol');
      if (sym) args.push('--symbol', sym);
      // 持仓假设：默认空仓。传 position 才能看到止损/止盈的拦截效果 ——
      // 空仓时风控闸门永远不会触发，那恰恰是蛛网最有价值的一帧。
      const pos = Number(u.searchParams.get('position'));
      if (Number.isFinite(pos) && pos > 0) {
        args.push('--position', String(Math.min(1, pos)));
        const ep = Number(u.searchParams.get('entry'));
        if (Number.isFinite(ep) && ep > 0) args.push('--entry', String(ep));
        const pk = Number(u.searchParams.get('peak'));
        if (Number.isFinite(pk) && pk > 0) args.push('--peak', String(pk));
      }
      const dd = Number(u.searchParams.get('drawdown'));
      if (Number.isFinite(dd) && dd > 0) {
        args.push('--drawdown', String(Math.min(1, dd)));
      }
      if (u.searchParams.get('halted') === '1') args.push('--halted');
      try {
        const r = await runPython(path.join(here, 'decision.py'), args, 90000);
        res.writeHead(r.error ? 400 : 200,
                      { 'Content-Type': TYPES['.json'], 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(r));
      } catch (e) {
        res.writeHead(500, { 'Content-Type': TYPES['.json'] });
        res.end(JSON.stringify({ error: String(e.message || e) }));
      }
      return;
    }

    // ── AI 设置 API ────────────────────────────────────────
    // 安全要点：GET 永远只回**脱敏**视图（apiKeySet + sk-…abcd），
    // 绝不回密钥原值；密钥只存在于 quant/state/ai-config.json（已 gitignore）。
    if (pathname.startsWith('/api/ai')) {
      const AIS = path.join(here, 'ai_settings.py');
      const reply = (code, obj) => {
        res.writeHead(code, { 'Content-Type': TYPES['.json'], 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(obj));
      };

      // GET /api/ai/settings —— 当前配置（脱敏）+ 可用性
      if (pathname === '/api/ai/settings' && req.method === 'GET') {
        try {
          const r = await runPython(AIS, ['show'], 30000);
          reply(200, r);
        } catch (e) {
          reply(500, { error: String(e.message || e) });
        }
        return;
      }

      // GET /api/ai/providers —— 预置供应商列表（前端下拉框用）
      if (pathname === '/api/ai/providers' && req.method === 'GET') {
        const probe = [
          'import sys, json',
          `sys.path.insert(0, r"${here}")`,
          'from ai_settings import PROVIDERS, DEFAULT_PROVIDER, DEFAULT_SYSTEM_PROMPT',
          'print(json.dumps({"providers": PROVIDERS,',
          '  "default": DEFAULT_PROVIDER, "systemPrompt": DEFAULT_SYSTEM_PROMPT},',
          '  ensure_ascii=False))',
        ].join('\n');
        const r = await runPython('-c', [probe], 30000)
          .catch(e => ({ error: String(e.message || e) }));
        reply(r.error ? 500 : 200, r);
        return;
      }

      // POST /api/ai/settings —— 保存（部分更新）
      if (pathname === '/api/ai/settings' && req.method === 'POST') {
        const b = await readBody(req);
        // 只透传白名单字段，避免前端塞任意键进去
        const allow = ['provider', 'url', 'model', 'api_key', 'timeout',
                       'temperature', 'system_prompt', 'enabled', 'clearApiKey'];
        const patch = {};
        for (const k of allow) {
          if (Object.prototype.hasOwnProperty.call(b, k)) patch[k] = b[k];
        }
        const script = [
          'import sys, json',
          `sys.path.insert(0, r"${here}")`,
          'from ai_settings import AIStore',
          'patch = json.loads(sys.argv[1])',
          'st = AIStore(); st.load()',
          'st.save(patch)',
          'print(json.dumps({"settings": st.settings.public_view(),',
          '                  "status": st.status()}, ensure_ascii=False))',
        ].join('\n');
        try {
          // patch 通过**命令行参数**传（execFile 不能直接喂 stdin）。
          // 字段有限、体积小，远不到命令行长度上限。
          const r = await runPython('-c', [script, JSON.stringify(patch)], 30000);
          reply(200, r);
        } catch (e) {
          reply(400, { error: String(e.message || e) });
        }
        return;
      }

      // POST /api/ai/test —— 真发一次请求验证配置
      if (pathname === '/api/ai/test' && req.method === 'POST') {
        try {
          const r = await runPython(AIS, ['test'], 90000);
          reply(r.ok ? 200 : 400, r);
        } catch (e) {
          reply(500, { error: String(e.message || e) });
        }
        return;
      }

      reply(404, { error: '未知的 AI 接口: ' + pathname });
      return;
    }

    // ── 参数优化 API ────────────────────────────────────────
    // 优化可能要跑几秒到几十秒（每个参数组合都要回测两次：
    // 训练集 + 验证集），所以单独走一条路由，不混进 /api/quant。
    if (pathname.startsWith('/api/optimize')) {
      const OPT = path.join(here, 'optimizer.py');
      const reply = (code, obj) => {
        res.writeHead(code, { 'Content-Type': TYPES['.json'], 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(obj));
      };

      if (pathname === '/api/optimize/meta') {
        // 哪些策略可优化、参数空间是什么 —— 毫秒级，不取行情
        const probe = [
          'import sys, json',
          `sys.path.insert(0, r"${here}")`,
          'from optimizer import PARAM_SPACE, default_params, tunable_params',
          'from strategy import STRATEGIES',
          'print(json.dumps({',
          '  "space": PARAM_SPACE,',
          '  "strategies": {k: v[0] for k, v in STRATEGIES.items()},',
          '  "tunable": {k: tunable_params(k) for k in STRATEGIES},',
          '  "defaults": {k: default_params(k) for k in STRATEGIES},',
          '  "optimizable": sorted(PARAM_SPACE.keys()),',
          '}, ensure_ascii=False))',
        ].join('\n');
        const r = await runPython('-c', [probe], 30000).catch(e => ({ error: String(e.message || e) }));
        reply(r.error ? 500 : 200, r);
        return;
      }

      if (pathname === '/api/optimize' && req.method === 'POST') {
        const b = await readBody(req);
        const args = [
          '--market', String(b.market || 'ashare'),
          '--interval', String(b.interval || '1d'),
          '--strategy', String(b.strategy || 'ema_cross'),
          '--method', String(b.method || 'grid'),
          '--max-combos', String(Math.max(4, Math.min(600, Number(b.maxCombos) || 80))),
          '--limit', String(Math.max(120, Math.min(2000, Number(b.limit) || 500))),
          '--is-ratio', String(Math.max(0.3, Math.min(0.9, Number(b.isRatio) || 0.7))),
          '--json', '--compact',
        ];
        if (b.symbol) args.push('--symbol', String(b.symbol));
        // 优化很慢：600 组 × 2 次回测可能超过 5 分钟，给足超时
        const r = await runPython(OPT, args, 600000);
        reply(r.error ? 400 : 200, r);
        return;
      }
      reply(404, { error: '未知的优化接口: ' + pathname });
      return;
    }

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

server.listen(PORT, HOST, () => {
  const shown = HOST === '0.0.0.0' ? '127.0.0.1' : HOST;
  console.log(`量化看板 → http://${shown}:${PORT}/`);
  console.log(`  专业图表 http://${shown}:${PORT}/pro.html`);
  console.log(`  模拟盘   http://${shown}:${PORT}/paper.html`);
  console.log(`  Python   ${PYTHON}`);
  console.log(`  监听     ${HOST}:${PORT}`);
  console.log(`  缓存 TTL ${CACHE_TTL_MS} ms`);
  if (HOST !== '127.0.0.1' && HOST !== 'localhost') {
    console.log(`  ⚠ 已监听 ${HOST} —— 局域网内其他设备可以访问本看板。`);
    console.log(`    本服务没有鉴权，仅在可信网络或容器里这样用。`);
  }
  console.log(`  ⚠ 模拟盘：只读行情，无交易接口，不产生任何真实委托`);
});
