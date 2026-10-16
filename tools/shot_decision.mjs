/**
 * 给决策驾驶舱页面截图。
 *
 * ⚠️ 踩过的坑：`Page.captureScreenshot` 的 `clip` 参数在这个 Chrome 版本上
 *    会让 result.data 为空（不报错，就是没数据）。所以改成：
 *      1) 设视口尺寸
 *      2) 把目标元素 scrollIntoView 到视口顶部
 *      3) 直接整屏截图（不带 clip）
 *    多截一点总比截不出来强。
 *
 * 用法：node tools/shot_decision.mjs <输出路径> [url] [宽] [高]
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const out = process.argv[2] || 'decision-web.png';
const url = process.argv[3] || 'http://127.0.0.1:4400/decision.html';
const VW = Number(process.argv[4]) || 1560;
const VH = Number(process.argv[5]) || 1200;

const port = 8100 + Math.floor(Math.random() * 800);
const prof = mkdtempSync(path.join(tmpdir(), 'shot-'));
const chrome = process.env.CHROME_PATH
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const proc = spawn(chrome, [
  '--headless=new', '--disable-gpu-sandbox', '--enable-unsafe-swiftshader',
  '--use-gl=angle', '--use-angle=swiftshader', '--hide-scrollbars',
  '--user-data-dir=' + prof, '--no-first-run', '--no-default-browser-check',
  `--window-size=${VW},${VH}`, `--remote-debugging-port=${port}`, url,
], { stdio: 'ignore' });

async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const p = l.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (p) return p.webSocketDebuggerUrl;
    } catch { /* 还没起来 */ }
    await new Promise(r => setTimeout(r, 400));
  }
  throw new Error('找不到 CDP target');
}

const s = new WebSocket(await wsUrl());
await new Promise((res, rej) => { s.onopen = res; s.onerror = rej; });
let id = 0; const pend = new Map(); const errs = [];
s.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') {
    errs.push((m.params.exceptionDetails.exception?.description || '').slice(0, 240));
  }
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
const send = (me, p = {}) => new Promise(r => {
  const i = ++id; pend.set(i, r); s.send(JSON.stringify({ id: i, method: me, params: p }));
});
const ev = async x => {
  const r = await send('Runtime.evaluate',
    { expression: x, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) {
    return 'EXC: ' + (r.result.exceptionDetails.exception?.description || '').slice(0, 200);
  }
  return r.result?.result?.value;
};

await send('Emulation.setDeviceMetricsOverride',
  { width: VW, height: VH, deviceScaleFactor: 1, mobile: false });

// 等页面算出数据
for (let i = 0; i < 100; i++) {
  const st = await ev(`document.getElementById('status')?.textContent || ''`);
  if (st && !st.includes('加载中') && st !== '—') break;
  await new Promise(r => setTimeout(r, 500));
}
// 等重演动画跑完（9 阶段 × 0.45s），再加余量
await new Promise(r => setTimeout(r, 8000));

console.log('status :', await ev(`document.getElementById('status')?.textContent`));
console.log('meta   :', await ev(`document.getElementById('webMeta')?.textContent`));

// 把目标滚到视口顶部（视口截图只能拍当前视口）
await ev(`document.getElementById('cvWeb')?.scrollIntoView({block:'start'})`);
await new Promise(r => setTimeout(r, 1500));
console.log('scrollY:', await ev(`scrollY`));

const shot = await send('Page.captureScreenshot', { format: 'png' });
if (shot.result?.data) {
  writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  console.log('saved  :', out);
} else {
  console.log('❌ 截图失败：', JSON.stringify(shot).slice(0, 200));
}
console.log('异常   :', errs.length ? errs.slice(0, 2) : '（无）');

s.close(); proc.kill();
try { rmSync(prof, { recursive: true, force: true }); } catch { /* 已删 */ }
process.exit(0);
