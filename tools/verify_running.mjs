/**
 * 启动后全面自检 —— 每个页面都真的渲染，且没有 JS 错误。
 *
 * 用法：node tools/verify_running.mjs [输出目录]
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const outDir = process.argv[2] || null;
if (outDir && !existsSync(outDir)) mkdirSync(outDir, { recursive: true });

const PAGES = [
  { name: '量化看板', url: 'http://127.0.0.1:4400/', canvas: 'cvMain' },
  { name: '专业图表', url: 'http://127.0.0.1:4400/pro.html', canvas: 'cvMain' },
  { name: '决策驾驶舱', url: 'http://127.0.0.1:4400/decision.html', canvas: 'cvCockpit' },
  { name: '选股', url: 'http://127.0.0.1:4400/screen.html', canvas: null },
  { name: '模拟盘', url: 'http://127.0.0.1:4400/paper.html', canvas: null },
];

const port = 9300 + Math.floor(Math.random() * 500);
const prof = mkdtempSync(path.join(tmpdir(), 'verify-'));
const chrome = process.env.CHROME_PATH
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const proc = spawn(chrome, ['--headless=new', '--disable-gpu-sandbox',
  '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader',
  '--hide-scrollbars', '--window-size=1560,1200', '--user-data-dir=' + prof,
  '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${port}`, 'about:blank'], { stdio: 'ignore' });

async function list() {
  for (let i = 0; i < 60; i++) {
    try { return await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); }
    catch { await new Promise(r => setTimeout(r, 400)); }
  }
  throw new Error('Chrome 没起来');
}
await list();

function conn(wsUrl) {
  return new Promise((res, rej) => {
    const s = new WebSocket(wsUrl);
    s.onopen = () => res(s);
    s.onerror = rej;
  });
}

let pass = 0, fail = 0;
for (const p of PAGES) {
  // 每个页面用一个新的 target，避免互相污染
  const created = await (await fetch(
    `http://127.0.0.1:${port}/json/new?${encodeURIComponent(p.url)}`,
    { method: 'PUT' })).json().catch(() => null);
  const targets = await list();
  const t = targets.find(x => x.type === 'page' && x.url.startsWith(p.url.split('?')[0]))
    || targets.find(x => x.type === 'page' && x.url !== 'about:blank')
    || created;
  if (!t || !t.webSocketDebuggerUrl) { console.log(`  ✗ ${p.name}: 拿不到 target`); fail++; continue; }

  const s = await conn(t.webSocketDebuggerUrl);
  let id = 0; const pend = new Map(); const errs = []; const cerr = [];
  s.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      errs.push((d.exception?.description || d.text || '').split('\n')[0].slice(0, 160));
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      cerr.push(m.params.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 160));
    }
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  };
  const send = (me, pa = {}) => new Promise(r => {
    const i = ++id; pend.set(i, r); s.send(JSON.stringify({ id: i, method: me, params: pa }));
  });
  const ev = async x => {
    const r = await send('Runtime.evaluate',
      { expression: x, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) return 'EXC: ' + (r.result.exceptionDetails.exception?.description || '').slice(0, 150);
    return r.result?.result?.value;
  };
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride',
    { width: 1560, height: 1200, deviceScaleFactor: 1, mobile: false });

  // 等数据
  for (let i = 0; i < 40; i++) {
    const st = await ev(`document.getElementById('status')?.textContent || ''`);
    if (st && !/加载中|计算中/.test(st) && st !== '—') break;
    await new Promise(r => setTimeout(r, 500));
  }
  await new Promise(r => setTimeout(r, 4000));

  const title = await ev('document.title');
  let detail = '';
  if (p.canvas) {
    const cv = await ev(`(()=>{const c=document.getElementById(${JSON.stringify(p.canvas)});
      if(!c) return null;
      const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      let non=0; for(let i=0;i<d.length;i+=4){ if(d[i]>16||d[i+1]>16||d[i+2]>16) non++; }
      return {w:c.width,h:c.height,non};})()`);
    if (cv && typeof cv === 'object') {
      detail = `canvas ${cv.w}x${cv.h}, 已绘制 ${cv.non} 像素`;
    } else detail = 'canvas 取不到';
  } else {
    const txt = await ev(`document.body.innerText.replace(/\\s+/g,' ').slice(0,60)`);
    detail = `正文 ${JSON.stringify(String(txt).slice(0, 50))}`;
  }

  const realErrs = errs.filter(e => !/ResizeObserver loop/i.test(e));
  const ok = realErrs.length === 0 && cerr.length === 0;
  if (ok) pass++; else fail++;
  console.log(`  ${ok ? '✓' : '✗'} ${p.name.padEnd(8)} ${String(title).slice(0, 24).padEnd(26)} ${detail}`);
  if (realErrs.length) realErrs.slice(0, 2).forEach(e => console.log(`      JS 异常: ${e}`));
  if (cerr.length) cerr.slice(0, 2).forEach(e => console.log(`      console.error: ${e}`));

  if (outDir) {
    await ev(`scrollTo(0,0)`);
    await new Promise(r => setTimeout(r, 600));
    const shot = await send('Page.captureScreenshot', { format: 'png' });
    if (shot.result?.data) {
      const f = path.join(outDir, `${p.name}.png`);
      writeFileSync(f, Buffer.from(shot.result.data, 'base64'));
    }
  }
  s.close();
}

console.log(`\n  ${pass} 个页面正常 / ${fail} 个有问题`);
proc.kill();
try { rmSync(prof, { recursive: true, force: true }); } catch {}
process.exit(fail ? 1 : 0);
