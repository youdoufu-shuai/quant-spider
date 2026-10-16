#!/usr/bin/env node
/**
 * 极简 npm 安装器（带超时 / 重试 / 并发）—— 替代 `npm ci`
 *
 * 为什么需要：本机 Node 是精简发行版（只有 node.exe，没有 npm），
 * 无法执行 README 要求的 `npm --prefix mcp ci`。
 *
 * 做法：package-lock.json 已记录每个包的精确 resolved URL 与 integrity，
 * 因此无需依赖解析：下载 tarball → 校验 sha512 → 解压到 node_modules。
 * 内置 gunzip + tar 解析，零外部依赖。
 *
 * 第一版教训：单包下载没有超时，遇到慢 registry 会无限挂起
 * （实测 `@modelcontextprotocol/server` 卡了 10 分钟）。现在：
 *   - 每包 AbortSignal.timeout 超时
 *   - 失败退避重试
 *   - 并发下载（默认 6）
 *   - 已装好的包跳过（可断点续装）
 *
 * 用法：node install-from-lock.mjs [项目目录] [--concurrency N] [--timeout S]
 */
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const rootArg = args.find((a) => !a.startsWith('--')) || process.cwd();
const root = path.resolve(rootArg);
const getOpt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? Number(args[i + 1]) : dflt;
};
const CONCURRENCY = getOpt('--concurrency', 6);
const TIMEOUT_MS = getOpt('--timeout', 90) * 1000;
const MAX_RETRY = 4;

const lockPath = path.join(root, 'package-lock.json');
if (!existsSync(lockPath)) {
  console.error(`找不到 ${lockPath}`);
  process.exit(1);
}

const lock = JSON.parse(await readFile(lockPath, 'utf8'));
const packages = lock.packages || {};
const entries = Object.keys(packages)
  .filter((k) => k !== '' && packages[k].resolved)
  .map((k) => ({
    name: k.replace(/^node_modules\//, ''),
    version: packages[k].version,
    resolved: packages[k].resolved,
    integrity: packages[k].integrity,
  }));

console.log(`从 lock 读到 ${entries.length} 个包；并发 ${CONCURRENCY}，单包超时 ${TIMEOUT_MS / 1000}s`);

function extractTar(buffer, stripComponents = 1) {
  const files = [];
  let offset = 0;
  while (offset + 512 <= buffer.length) {
    const header = buffer.subarray(offset, offset + 512);
    if (header.every((b) => b === 0)) break;
    const readStr = (s, l) => header.subarray(s, s + l).toString('utf8').replace(/\0.*$/, '');
    const name = readStr(0, 100);
    const prefix = readStr(345, 155);
    const fullName = prefix ? `${prefix}/${name}` : name;
    const sizeOctal = readStr(124, 12).trim();
    const size = sizeOctal ? parseInt(sizeOctal, 8) : 0;
    const typeFlag = String.fromCharCode(header[156]);
    const dataStart = offset + 512;
    if (typeFlag === '0' || typeFlag === '') {
      const parts = fullName.split('/').filter(Boolean);
      const stripped = parts.slice(stripComponents).join('/');
      if (stripped) files.push({ path: stripped, data: buffer.subarray(dataStart, dataStart + size) });
    }
    offset = dataStart + Math.ceil(size / 512) * 512;
  }
  return files;
}

async function fetchWithRetry(url) {
  let lastErr;
  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(TIMEOUT_MS) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return Buffer.from(await res.arrayBuffer());
    } catch (err) {
      lastErr = err;
      if (attempt < MAX_RETRY) {
        const wait = 1500 * attempt;
        await new Promise((r) => setTimeout(r, wait));
      }
    }
  }
  throw lastErr;
}

/**
 * 取得 tarball：优先用缓存目录里预下载好的文件。
 *
 * 为什么：Node 的 fetch 对个别 registry URL 不稳定（实测
 * @modelcontextprotocol/server 连续 4 次 fetch failed），
 * 同一个 URL 用 PowerShell / curl 却能秒下。所以支持外部预下载：
 * 把 <package>-<version>.tgz 放进 .tarball-cache/ 即可。
 */
async function getTarball(entry) {
  const cacheName = `${entry.name.replace(/\//g, '__')}-${entry.version}.tgz`;
  const cachePath = path.join(root, '.tarball-cache', cacheName);
  if (existsSync(cachePath)) {
    console.log(`  ↺ ${entry.name}@${entry.version} 用预下载缓存`);
    return await readFile(cachePath);
  }
  return await fetchWithRetry(entry.resolved);
}

let ok = 0;
const failures = [];

async function installOne(entry) {
  const dest = path.join(root, 'node_modules', entry.name.replace(/\//g, path.sep));
  if (existsSync(path.join(dest, 'package.json'))) {
    ok++;
    return;
  }
  try {
    const buf = await getTarball(entry);

    if (entry.integrity) {
      const [algo, expected] = entry.integrity.split('-');
      const actual = createHash(algo === 'sha1' ? 'sha1' : 'sha512').update(buf).digest('base64');
      if (actual !== expected) {
        throw new Error(`integrity 校验失败 (${algo})`);
      }
    }

    const files = extractTar(gunzipSync(buf), 1);
    if (files.length === 0) throw new Error('解出 0 个文件');

    await rm(dest, { recursive: true, force: true });
    for (const f of files) {
      const target = path.join(dest, f.path);
      await mkdir(path.dirname(target), { recursive: true });
      await writeFile(target, f.data);
    }
    ok++;
    console.log(`  ✓ ${entry.name}@${entry.version}  (${files.length} 文件, ${(buf.length / 1024).toFixed(0)} KB)`);
  } catch (err) {
    failures.push(`${entry.name}@${entry.version}: ${err.message}`);
    console.log(`  ✗ ${entry.name}@${entry.version}  ${err.message}`);
  }
}

// 并发池
const queue = [...entries];
const workers = Array.from({ length: Math.min(CONCURRENCY, queue.length) }, async () => {
  while (queue.length) {
    const e = queue.shift();
    if (e) await installOne(e);
  }
});
await Promise.all(workers);

console.log('');
console.log(`完成：${ok} 成功 / ${failures.length} 失败`);
if (failures.length) {
  console.log('失败清单：');
  for (const f of failures) console.log('  - ' + f);
}

const serverPkg = path.join(root, 'node_modules', '@modelcontextprotocol', 'server', 'package.json');
if (existsSync(serverPkg)) {
  const pkg = JSON.parse(await readFile(serverPkg, 'utf8'));
  console.log(`关键依赖 @modelcontextprotocol/server@${pkg.version} 就位`);
}
process.exit(failures.length === 0 ? 0 : 1);
