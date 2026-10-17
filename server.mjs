import http from 'node:http';
import { createReadStream } from 'node:fs';
import { readFile, realpath, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const projectRoot = path.dirname(fileURLToPath(import.meta.url));
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2' };
const sharedModules = new Set(['config.js', 'session-store.js', 'replay.js', 'layout.js', 'live-sessions.json']);
const within = (base, filename) => { const relative = path.relative(base, filename); return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative); };

function json(res, value) {
  res.setHeader('Content-Type', types['.json']);
  res.end(JSON.stringify(value));
}

/** Shared zero-dependency static handler, used by the demo and local MCP hub. */
export async function serveStatic(req, res, { root = projectRoot } = {}) {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Cache-Control', 'no-cache');
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    if (pathname.includes('\\') || pathname.includes('\0') || pathname.split('/').some(part => part === '..')) throw new Error('Invalid path');
  } catch { res.writeHead(400).end('Invalid path'); return; }
  if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405, { Allow: 'GET, HEAD' }).end(); return; }
  if (pathname === '/status') { json(res, { application: 'prompt-spider-ui' }); return; }

  // ── /api/live-status ──────────────────────────────────────────
  // 实时会话的**真实**状态。
  //
  // 为什么需要：lib/live-sessions.json 里的 updatedAt 只是「最后一次
  // 写入时间」。live-feed 进程被 kill 后条目会一直留着，时间戳看起来
  // 还很新 —— 页面显示「活跃」，但链接指向已经死掉的会话。
  // 实测踩过：发现文件里有 pid 4676 的死条目，端口 53507 早已关闭。
  //
  // 这里用 process.kill(pid, 0) 检查进程**是否真的还活着**
  // （信号 0 不发信号，只做存在性检查），给页面一个可信状态。
  if (pathname === '/api/live-status') {
    let raw = { sessions: [] };
    try {
      raw = JSON.parse(await readFile(path.join(root, 'lib', 'live-sessions.json'), 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') { res.writeHead(500).end('Read error'); return; }
    }
    const now = Date.now();
    const sessions = (Array.isArray(raw.sessions) ? raw.sessions : []).map((s) => {
      let alive = false;
      let probe = 'no-pid';
      if (Number.isInteger(s.pid) && s.pid > 0) {
        try {
          process.kill(s.pid, 0);        // 信号 0：只探测，不发信号
          alive = true;
          probe = 'alive';
        } catch (error) {
          // ESRCH = 进程不存在；EPERM = 存在但无权限（本机同用户不会发生）
          probe = error.code === 'EPERM' ? 'no-permission' : 'gone';
        }
      }
      return {
        ...s,
        alive,
        probe,
        ageMs: Number.isFinite(s.updatedAt) ? now - s.updatedAt : null,
      };
    });
    res.setHeader('Cache-Control', 'no-store');
    json(res, { now, updatedAt: raw.updatedAt ?? null, sessions });
    return;
  }

  let filename;
  let base;
  if (pathname === '/project-config.json' || pathname === '/replay.json') {
    base = root;
    filename = path.join(root, pathname.slice(1));
    if (pathname === '/project-config.json') {
      try { await stat(filename); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        json(res, {}); return;
      }
    }
  } else if (pathname.startsWith('/lib/')) {
    const name = pathname.slice(5);
    if (!sharedModules.has(name)) { res.writeHead(404).end('Not found'); return; }
    base = path.join(root, 'lib');
    filename = path.join(base, name);
  } else {
    base = path.join(root, 'app');
    // /live 是稳定入口页的短地址，映射到 app/live.html。
    // 不映射的话会被当文件名找 "live"，没有扩展名直接被 404。
    const rel = pathname === '/' ? '/index.html'
      : pathname === '/live' ? '/live.html'
      : pathname;
    filename = path.resolve(base, '.' + rel);
    if (!within(base, filename) || !types[path.extname(filename)] || pathname.split('/').some(part => part.startsWith('.'))) {
      res.writeHead(404).end('Not found'); return;
    }
  }
  try {
    const [realBase, realFile] = await Promise.all([realpath(base), realpath(filename)]);
    if (!within(realBase, realFile) || !(await stat(realFile)).isFile()) { res.writeHead(404).end('Not found'); return; }
    res.setHeader('Content-Type', types[path.extname(filename)] || 'application/octet-stream');
    if (req.method === 'HEAD') { res.end(); return; }
    const stream = createReadStream(realFile);
    res.once('close', () => stream.destroy());
    stream.once('error', () => res.destroy());
    stream.pipe(res);
  } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ENOTDIR') res.writeHead(404).end('Not found');
    else throw error;
  }
}

export function createDemoServer({ root = projectRoot } = {}) {
  return http.createServer((req, res) => {
    serveStatic(req, res, { root }).catch(() => { if (!res.headersSent) res.writeHead(500); res.end('Server error'); });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4317);
  // 监听地址可配置。默认只监听回环 —— 本地演示工具不该默认暴露到局域网。
  // 容器里需要 0.0.0.0 才能让 -p 端口转发生效，所以允许覆盖并给出警告。
  const host = process.env.HOST || '127.0.0.1';
  const server = createDemoServer();
  server.on('error', error => {
    console.error(error.code === 'EADDRINUSE' ? `Port ${port} is busy. Set PORT to another port and run npm start.` : error.message);
    process.exitCode = 1;
  });
  server.listen(port, host, () => {
    const shown = host === '0.0.0.0' ? '127.0.0.1' : host;
    console.log(`Prompt Spider UI → http://${shown}:${server.address().port}`);
    console.log('Local Canvas demo. Ctrl+C to stop.');
    if (host !== '127.0.0.1' && host !== 'localhost') {
      console.log(`  ⚠ Listening on ${host} — reachable from your local network. No auth.`);
    }
  });
}
