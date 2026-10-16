/**
 * 量化过程可视化 —— 用蜘蛛网表现量化的决策过程。
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么用蜘蛛这个隐喻，以及每个元素对应什么
 * ═══════════════════════════════════════════════════════════════
 *  蛛网丝        K 线时间序列（每根 K 线一条射线，从左侧数据栏发散出来）
 *  网上的节点    某根 K 线上产生的**信号点**（买/卖），颜色区分方向
 *  蜘蛛（本体）  策略引擎本身 —— 它在网的中央处理信息
 *  触手          策略正在"抓取"的那根 K 线（正在评估的输入）
 *  心跳核心      当前市场状态（涨/跌/震荡），脉动频率随波动率变化
 *  三条分支      BUY / SELL / HOLD —— 策略给出的目标动作
 *
 * ═══════════════════════════════════════════════════════════════
 *  绘制代码复用
 * ═══════════════════════════════════════════════════════════════
 *  蜘蛛本体直接 import 主面板的 drawSpiderBody（同源 /spider.js），
 *  所以这里的蜘蛛和 `spider-viewer.html`、实时面板里的**是同一份代码**，
 *  美术调整会同步生效，不会出现两套画风。
 *
 *  但 legs/knee 的姿势计算在主项目里是模块私有函数（未 export），
 *  所以这里按同样的算法重写一份 —— 算法只有 10 行，重复的成本
 *  低于改动主项目导出接口的成本。如果哪天主项目 export 了 legPose，
 *  把这里换成 import 即可。
 */

import { drawSpiderBody } from '/spider.js';

const TAU = Math.PI * 2;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fraction = v => v - Math.floor(v);
const smooth = v => v * v * (3 - 2 * v);

/**
 * 与主项目 app/spider.js 的 legPose 同算法（那里是模块私有，未 export）。
 * 保持一致的参数含义，改一处要同步另一处。
 */
export function legPose({ time = 0, phase = 0, speed = 0, localVelocity = { x: 0, y: 1 },
                          reducedMotion = false, side = -1, index = 0 } = {}) {
  const stride = reducedMotion ? 0 : 13 + Math.min(speed * 0.11, 13);
  const frequency = 1.8 + Math.min(speed / 200, 0.65);
  const row = (index - 3.5) / 3.8;
  const spread = Math.sin((index + 0.75) / 8.5 * Math.PI);
  const cycle = reducedMotion ? 0
    : time * frequency + phase + (index % 2) * 0.5 + (side > 0 ? 0.5 : 0) + index * 0.035;

  const ph = fraction(cycle);
  let along, lift;
  if (ph < 0.64) { along = (0.5 - ph / 0.64) * stride; lift = 0; }
  else { const sw = (ph - 0.64) / 0.36; along = (smooth(sw) - 0.5) * stride; lift = Math.sin(sw * Math.PI); }

  const curl = reducedMotion ? 0 : Math.sin(cycle * TAU) * 2.8;
  const root = { x: side * (22.2 * Math.sqrt(1 - row * row)), y: row * 27 };
  const foot = {
    x: side * (39 + spread * 17) + localVelocity.x * along - side * lift * 7.5 + side * curl,
    y: row * 52 + localVelocity.y * along - lift * 10 + curl * 0.65,
  };
  const knee = {
    x: side * (30 + spread * 9) - side * lift * 4,
    y: row * 34 + (index < 4 ? -5.5 : 5.5) - lift * 5 + curl * 0.4,
  };
  const ankle = { x: foot.x - side * 3.8, y: foot.y + (index < 4 ? 1.2 : -1.2) };
  return { root, knee, ankle, foot, lift };
}

const ROUTES = [
  { key: 'buy',  label: 'BUY',  zh: '买入', color: '#ef4b5e' },
  { key: 'hold', label: 'HOLD', zh: '观望', color: '#8b8b94' },
  { key: 'sell', label: 'SELL', zh: '卖出', color: '#3fbf8f' },
];

const STATUS = { completed: '已完成', running: '进行中', pending: '待触发', failed: '失败' };

export class QuantSpiderRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.w = 0; this.h = 0;
    this.regions = [];
    // 缓存：数据不变时不重复做 prepare（每帧都算纯属浪费）
    this._cacheKey = '';
    this._prep = null;
  }

  /** 数据或选项变化时调用，缓存转换结果 */
  update(data, opts = {}) {
    // ⚠️ 缓存键必须包含**数据内容指纹**。
    //    之前只用 symbol|interval|count|strategy|window ——
    //    默认 30 秒自动刷新时这五项全都不变，于是 prepare() 不再执行，
    //    蜘蛛面板就冻结在首次抓取的数据上，而其他面板都在更新（实测踩过）。
    //    generatedAt 每次管道运行都会变，用它做指纹最省事。
    const key = [
      data?.meta?.symbol, data?.meta?.interval, data?.meta?.count,
      opts.strategy, opts.window,
      data?.generatedAt,
    ].join('|');
    if (key !== this._cacheKey) {
      this._cacheKey = key;
      this._prep = this.prepare(data, opts);
    }
    this._data = data;
    this._opts = opts;
    return this._prep;
  }

  resize() {
    const dpr = Math.min(2, Math.max(1, globalThis.devicePixelRatio || 1));
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const w = Math.max(320, Math.floor(rect.width) - 2);
    const h = 560;
    if (this.canvas.width === Math.round(w * dpr) && this.canvas.height === Math.round(h * dpr)) return;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
  }

  text(v, x, y, color, size = 11, align = 'left', bold = false) {
    const c = this.ctx;
    c.font = `${bold ? 700 : 400} ${size}px Consolas, "Cascadia Code", monospace`;
    c.fillStyle = color;
    c.textAlign = align;
    c.textBaseline = 'alphabetic';
    c.fillText(String(v), x, y);
    c.textAlign = 'left';
  }

  /**
   * 把量化数据转成可视化需要的元素。
   *
   * 关键设计：**只展示时间轴的尾部窗口**。
   * 500 根 K 线画不下也没必要 —— 取最近 N 根，让每个点都看得清。
   * N 由调用方按数据量决定（见 index.html 的 spiderView），
   * 因为不同粒度的信号密度差很多：1 分钟数据 44 根常常一个信号都没有。
   */
  prepare(data, opts = {}) {
    const bars = data.bars;
    // 信号统计用**完整窗口**（这样计数有意义），
    // 但左侧文字流只画最后 MAX_ROWS 根 —— 再多就糊成一堵文字墙，
    // 每行不到 10px 谁也读不清。实测 138 行时会完全看不清。
    const MAX_ROWS = 38;
    const windowSize = Math.max(20, Math.min(opts.window ?? 60, bars.length));
    const offset = bars.length - windowSize;

    const sig = data.signals[opts.strategy] || Object.values(data.signals)[0];

    const events = [];
    const totalByRoute = { buy: 0, sell: 0, hold: 0 };
    for (const i of (sig?.entries || [])) {
      totalByRoute.buy++;
      if (i >= offset) events.push({ index: i - offset, kind: 'buy', price: bars[i].c, time: bars[i].t });
    }
    for (const i of (sig?.exits || [])) {
      totalByRoute.sell++;
      if (i >= offset) events.push({ index: i - offset, kind: 'sell', price: bars[i].c, time: bars[i].t });
    }
    events.sort((a, b) => a.index - b.index);
    totalByRoute.hold = Math.max(0, bars.length - totalByRoute.buy - totalByRoute.sell);

    // 只画尾部 MAX_ROWS 根
    const shownCount = Math.min(MAX_ROWS, windowSize);
    const hiddenCount = windowSize - shownCount;
    const shownOffset = bars.length - shownCount;
    const visible = bars.slice(shownOffset);

    const rows = visible.map((b, i) => {
      const gi = shownOffset + i;                 // 全序列下标
      const prev = gi > 0 ? bars[gi - 1].c : b.o;
      const chg = prev ? (b.c - prev) / prev * 100 : 0;
      return {
        i, globalIndex: gi, time: b.t, close: b.c, chg,
        up: b.c >= b.o,
        signal: events.find(e => e.index + offset === gi)?.kind || null,
        label: `${b.t.slice(5, 16)}  ${b.c.toFixed(2)}  ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`,
      };
    });

    return {
      bars: visible, rows, events, sig, windowSize,
      totalByRoute, hiddenCount, shownCount,
      // 两个偏移量含义不同，别混：
      //   globalStart  左侧实际画出的第一根在**全序列**里的下标
      //   windowOffset 统计窗口的第一根在全序列里的下标（>= globalStart）
      globalStart: shownOffset,
      windowOffset: offset,
    };
  }

  /** 目标动作：由当前策略的持仓状态决定 */
  currentRoute(prep) {
    const cur = prep.sig?.current ?? 0;
    return cur === 1 ? 'buy' : 'hold';
  }

  render(data, opts = {}) {
    this.resize();
    const c = this.ctx, w = this.w, h = this.h;
    const time = opts.time ?? performance.now() / 1000;
    const reduced = opts.reducedMotion ?? false;

    const bg = '#080909', border = '#24242a', muted = '#8b8b94', text = '#e8e9e9';
    const accent = '#f23c83', spiderColor = '#48d6a4';

    c.fillStyle = bg; c.fillRect(0, 0, w, h);
    if (!data) return;

    const prep = this.update(data, opts);
    const { rows, events } = prep;

    const pad = 14;
    const leftW = Math.min(430, w * 0.34);
    const nodeR = h - 96;
    const cx = pad + leftW + (w - leftW - pad * 2) * 0.42;
    const cy = h / 2 + 6;

    // ── 网：从左侧数据栏发散的射线（= 时间序列） ──
    // webTop 必须避开顶部两行标题（y=26 / y=44），否则文字会叠在一起
    const webTop = 96, webBot = h - 62;
    for (let i = 0; i < 22; i++) {
      const y = webTop + (webBot - webTop) * i / 21;
      const sag = Math.sin(i * 1.7 + (reduced ? 0 : time * 0.6)) * 5;
      c.beginPath();
      c.moveTo(pad, y);
      c.bezierCurveTo(cx - 190, y + sag, cx - 90, cy - sag * 0.4, cx - 26, cy);
      c.strokeStyle = 'rgba(72,214,164,0.055)';
      c.lineWidth = 0.65; c.stroke();
    }
    // 同心弧：波动率环
    for (let r = 1; r <= 4; r++) {
      c.beginPath();
      c.ellipse(cx, cy, 44 * r, 30 * r, 0, 0, TAU);
      c.strokeStyle = `rgba(72,214,164,${0.05 - r * 0.008})`;
      c.lineWidth = 0.6; c.stroke();
    }

    // ── 左侧数据流（K 线行） ──
    // 行高按可用高度自适应，并设下限 —— 低于 9px 文字就挤在一起了。
    //
    // ⚠️ 必须把 startY **夹到 webTop 之内**。
    //    行数 × 行高可能超过可用高度（例如 38 行 × 13px = 494 > 402），
    //    这时 cy - rows*rowH/2 会算到 webTop 上方，于是：
    //      · 第 0 行压到顶部状态行（y44）
    //      · 截断提示压到标题行（y26）
    //    实测出现过三处文字叠印、完全不可读。
    const avail = webBot - webTop;
    const MAX_ROWS_FIT = Math.max(6, Math.floor(avail / 11));
    const fitRows = rows.slice(-Math.min(rows.length, MAX_ROWS_FIT));
    const rowH = Math.max(9, Math.min(22, avail / Math.max(1, fitRows.length)));
    const startY = Math.max(webTop + 6, cy - (fitRows.length * rowH) / 2);
    this.regions = [];
    fitRows.forEach((r, i) => {
      const y = startY + i * rowH + rowH / 2;
      const col = r.up ? '#ef4b5e' : '#3fbf8f';
      const isSig = !!r.signal;
      // 信号行高亮底色
      if (isSig) {
        const sc = r.signal === 'buy' ? '#ef4b5e' : '#3fbf8f';
        c.fillStyle = sc + '22';
        c.fillRect(pad - 2, y - rowH / 2, leftW - 6, rowH - 1);
        c.fillStyle = sc;
        c.fillRect(pad - 2, y - rowH / 2, 2, rowH - 1);
      }
      this.text(String(r.globalIndex).padStart(3, '0'), pad + 4, y + 3.5, isSig ? '#e8e8e9' : '#4a4a54', 9);
      if (rowH >= 15) {
        this.text(r.label, pad + 34, y + 3.5, isSig ? '#ffffff' : col, 10);
      } else {
        // 行太挤时只显示时间与价格，避免糊成一片
        this.text(`${r.time.slice(11, 16)}  ${r.close.toFixed(2)}`, pad + 34, y + 3.5, col, 9.5);
      }
      const strength = clamp(Math.abs(r.chg) / 3, 0, 1);
      c.fillStyle = col; c.globalAlpha = isSig ? 0.75 : 0.30;
      c.fillRect(pad + 34, y + 6.5, Math.max(2, strength * 140), 1.6);
      c.globalAlpha = 1;
    });

    // 窗口截断提示（贴在 startY 上方，但 startY 已夹到 webTop 内，
    // 所以这里最多画到 webTop-2，不会压到标题行）
    if (prep.hiddenCount > 0 || fitRows.length < rows.length) {
      const hidden = prep.hiddenCount + (rows.length - fitRows.length);
      const y = Math.max(webTop - 3, startY - 8);
      this.text(`↑ 另有 ${hidden} 根未显示（信号计数仍包含它们）`, pad + 4, y, '#5a5a64', 9);
    }

    // ── 信号点：蛛网上的节点，飞到对应分支 ──
    // 三个偏移量概念不同，别混：
    //   ev.index      信号在**统计窗口**内的下标
    //   windowOffset  统计窗口第一根在**全序列**里的下标
    //   shownStart    实际画出的第一根（fitRows[0]）在全序列里的下标
    // 行号 = 全序列下标 - shownStart
    const targets = this.routeTargets(w, h, pad);
    const shownStart = fitRows.length ? fitRows[0].globalIndex : 0;
    const rowByGlobal = new Map(fitRows.map((r, i) => [r.globalIndex, i]));
    for (const ev of events) {
      const globalIdx = ev.index + prep.windowOffset;
      const localRow = rowByGlobal.get(globalIdx);
      if (localRow === undefined) continue;      // 落在截断之外：只记数不画点
      const y = startY + localRow * rowH + rowH / 2;
      const route = ROUTES.find(rt => rt.key === ev.kind);
      const t = targets[route.key];
      c.beginPath();
      c.moveTo(pad + 34, y);
      c.bezierCurveTo(cx - 130, y, cx + 110, t.y, t.x - 78, t.y);
      c.strokeStyle = route.color + '66';
      c.lineWidth = 1.1; c.stroke();
      const nx = pad + 34 + (t.x - 78 - pad - 34) * 0.42;
      const ny = y + (t.y - y) * 0.42;
      c.beginPath(); c.arc(nx, ny, 7, 0, TAU); c.fillStyle = route.color + '2e'; c.fill();
      c.beginPath(); c.arc(nx, ny, 3.4, 0, TAU); c.fillStyle = route.color; c.fill();
    }

    // ── 蜘蛛本体（复用主项目绘制代码） ──
    const speed = 26;
    const body = { x: cx, y: cy, vx: speed, vy: 0 };
    drawSpiderBody(c, body, time, reduced, { spider: spiderColor, accent }, { scale: 0.92 });

    // ── 触手：指向正在评估的那根 K 线（时间轴上最新一根） ──
    const focusRow = fitRows.length - 1;
    if (focusRow >= 0) {
      const fy = startY + focusRow * rowH + rowH / 2;
      const midX = (cx + pad + 34) / 2, midY = (cy + fy) / 2 - 34;
      c.beginPath();
      c.moveTo(cx - 30, cy - 6);
      c.quadraticCurveTo(midX, midY, pad + 40, fy);
      c.setLineDash([2, 5]);
      c.strokeStyle = spiderColor + 'cc';
      c.lineWidth = 1.3; c.stroke();
      c.setLineDash([]);
      c.beginPath(); c.arc(pad + 40, fy, 3.2, 0, TAU);
      c.fillStyle = spiderColor; c.fill();
      // 「正在评估」画在当前行**下方**，避免压到上一行文字
      this.text('正在评估', pad + 48, fy + rowH - 1, spiderColor, 9);
    }

    // ── 三条分支 ──
    for (const route of ROUTES) {
      const t = targets[route.key];
      const isCur = this.currentRoute(prep) === route.key;
      const bw = 150, bh = 84;
      c.fillStyle = '#0e0e11';
      c.fillRect(t.x - bw / 2, t.y - bh / 2, bw, bh);
      const cap = c.createLinearGradient(t.x - bw / 2, 0, t.x + bw / 2, 0);
      cap.addColorStop(0, route.color + 'f2');
      cap.addColorStop(1, route.color + '59');
      c.fillStyle = cap;
      c.fillRect(t.x - bw / 2, t.y - bh / 2, bw, 4);
      c.strokeStyle = isCur ? route.color : route.color + 'aa';
      c.lineWidth = isCur ? 2 : 1;
      c.strokeRect(t.x - bw / 2 + .5, t.y - bh / 2 + .5, bw - 1, bh - 1);
      if (isCur) {
        c.strokeStyle = route.color + '22';
        c.strokeRect(t.x - bw / 2 + 4.5, t.y - bh / 2 + 4.5, bw - 9, bh - 9);
      }
      this.text(route.label, t.x - bw / 2 + 11, t.y - 12, route.color, 13, 'left', true);
      this.text(route.zh, t.x - bw / 2 + 11, t.y + 8, muted, 10);
      this.text(`当前${isCur ? ' ●' : ''}`, t.x + bw / 2 - 11, t.y - 12, isCur ? route.color : muted, 9, 'right');
      // HOLD 不是"信号"，而是"其余没有信号的根数"—— 不能和买卖信号同口径显示，
      // 否则同屏会看到「信号 2 个」和「HOLD 248」自相矛盾（实测踩过）。
      const total = prep.totalByRoute[route.key] ?? 0;
      const inWin = events.filter(e => e.kind === route.key).length;
      if (route.key === 'hold') {
        this.text(String(total), t.x + bw / 2 - 11, t.y + 24, muted, 17, 'right', true);
        this.text(`其余 ${total} 根无信号`, t.x - bw / 2 + 11, t.y + 28, muted, 9);
      } else {
        this.text(String(total), t.x + bw / 2 - 11, t.y + 24, route.color, 17, 'right', true);
        this.text(`全区间 ${total} · 窗口内 ${inWin}`, t.x - bw / 2 + 11, t.y + 28, muted, 9);
      }
    }

    // ── 顶部状态行 ──
    const m = data.meta || {};
    this.text(`${m.name || ''} ${m.symbol || ''} · ${m.interval || ''} · ${m.count || 0} 根`,
      pad, 26, text, 13, 'left', true);
    this.text(`源 ${m.source || '—'}   窗口 ${prep.windowSize} 根   信号 ${events.length} 个`,
      pad, 44, muted, 10);
    const sigLabel = data.signals?.[opts.strategy]?.label || '';
    this.text(`策略：${sigLabel}`, w - pad, 26, accent, 12, 'right');
    const cur = prep.sig?.current ?? 0;
    this.text(`目标持仓：${cur === 1 ? '持多' : '空仓'}`, w - pad, 44, muted, 10, 'right');

    this.text('K 线时间序列 → 策略引擎 → 目标动作（真实历史数据，非演示动画）',
      pad, h - 12, muted, 10);
  }

  routeTargets(w, h, pad) {
    const rx = w - pad - 82;
    return {
      buy:  { x: rx, y: h * 0.26 },
      hold: { x: rx, y: h * 0.50 },
      sell: { x: rx, y: h * 0.74 },
    };
  }
}
