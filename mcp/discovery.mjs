#!/usr/bin/env node
/**
 * 实时会话发现 —— 解决「预览链接一刷新就失效」的问题。
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么需要这个
 * ═══════════════════════════════════════════════════════════════
 * MCP hub 的实现是「每个进程一个随机端口 + 一个私有 token」：
 *
 *     const token = randomBytes(32).toString('hex');
 *     server.listen(0, '127.0.0.1', ...)   // 端口 0 = 随机
 *
 * 所以每次重启喂养器，预览 URL 都会变：
 *     49909 → 62664 → 65221 → 53198 …
 *
 * 后果：**浏览器里打开的旧链接指向已经死掉的会话**，
 * 页面停在最后一帧不再变化 —— 看起来就像"没有新动画"。
 * 实测确认过：链接是活的（HTTP 200），但那个会话已经没有生产者了。
 *
 * 解法：喂养器把当前有效链接写入一个**固定路径**的发现文件，
 * 再提供一个稳定的入口页去读它。这样用户永远访问同一个地址。
 *
 * 发现文件位置：lib/live-sessions.json
 *   —— 放在 lib/ 是因为主服务器对 lib/ 有白名单静态路由，
 *      这样不需要改动原项目的路由逻辑就能被读到。
 *
 * ⚠️ 这是个**信息暴露**点：token 写进文件意味着本机任何进程都能读到预览权限。
 *    但这本来就是本地单机演示工具（服务只监听 127.0.0.1），
 *    且 token 只能看不能改数据，所以风险可接受。
 *    如果不希望落盘，可以传 --no-discover 关闭。
 */

import { writeFileSync, readFileSync, existsSync, renameSync, unlinkSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const DISCOVERY_PATH = path.resolve(here, '..', 'lib', 'live-sessions.json');

const MAX_ENTRIES = 12;
const STALE_MS = 10 * 60 * 1000;   // 10 分钟没心跳就视为失效

function readAll() {
  try {
    const raw = readFileSync(DISCOVERY_PATH, 'utf8');
    const d = JSON.parse(raw);
    return Array.isArray(d.sessions) ? d : { sessions: [] };
  } catch {
    return { sessions: [] };
  }
}

/** 原子写：先写临时文件再 rename，避免读到写了一半的 JSON */
function writeAll(data) {
  const tmp = DISCOVERY_PATH + '.tmp';
  writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
  renameSync(tmp, DISCOVERY_PATH);
}

export function publishSession(info) {
  const now = Date.now();
  const all = readAll();

  // 清掉过期条目（进程崩了不会自己清理，靠这里兜底）
  let sessions = all.sessions.filter(s => now - (s.updatedAt || 0) < STALE_MS);
  // 同一个 pid 的旧记录去掉（重启后 pid 会变，但同一进程重复发布要幂等）
  sessions = sessions.filter(s => s.pid !== process.pid);

  sessions.unshift({
    ...info,
    pid: process.pid,
    updatedAt: now,
    startedAt: info.startedAt || now,
  });

  writeAll({
    updatedAt: now,
    note: '由 live-feed.mjs 自动写入。此文件用于稳定入口页发现当前有效会话。',
    sessions: sessions.slice(0, MAX_ENTRIES),
  });
}

export function unpublishSession() {
  const all = readAll();
  const sessions = all.sessions.filter(s => s.pid !== process.pid);
  if (sessions.length === all.sessions.length) return;
  writeAll({ updatedAt: Date.now(), note: all.note, sessions });
}

export function listSessions() {
  const now = Date.now();
  const all = readAll();
  const live = all.sessions.filter(s => now - (s.updatedAt || 0) < STALE_MS);
  return { ...all, sessions: live };
}
