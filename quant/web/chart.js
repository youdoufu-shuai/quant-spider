/**
 * 交互式图表引擎 —— 零依赖，Canvas 2D。
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么单独写一个模块
 * ═══════════════════════════════════════════════════════════════
 * 原来的图表是「画一次就不管了」：没有十字光标、没有 tooltip、
 * X 轴连日期刻度都没有。这是这个项目最大的体验短板 ——
 * 看盘时你没法知道某根 K 线是哪一天、什么价位。
 *
 * 所以把图表抽成通用引擎，主看板和专业图表页共用一套实现，
 * 避免两处各写一遍、各自有各自的 bug。
 *
 * ═══════════════════════════════════════════════════════════════
 *  设计要点
 * ═══════════════════════════════════════════════════════════════
 *
 * **1. 视口（viewport）是共享状态，不是每个面板各管各的**
 *    多面板（K线 / MACD / RSI）必须同步缩放平移，否则十字光标
 *    在三个面板里指向不同时间的 K 线，完全没法用。
 *    做法：所有面板共用一个 viewport 对象，变更时统一重绘。
 *
 * **2. 索引空间 vs 像素空间**
 *    viewport 里存的是**索引**（第几根 K 线），不是像素。
 *    这样缩放平移不会累积浮点误差，窗口尺寸变化也不影响视口。
 *    像素换算只在绘制时做。
 *
 * **3. Y 轴只按可见数据自适应**
 *    如果按全量数据算 Y 范围，放大到局部时价格线会挤成一条直线。
 *    所以每次重绘都从可见区间重新算 min/max，并留 padding。
 *
 * **4. 稀疏刻度**
 *    500 根 K 线全画日期会糊成一片。用「按可用宽度算步长」
 *    的算法挑刻度，保证文字之间至少留 60px。
 *
 * **5. 触摸支持**
 *    单指拖动 = 平移，双指捏合 = 缩放。用 pointer events 统一
 *    处理鼠标和触摸，避免两套代码。
 *
 * ═══════════════════════════════════════════════════════════════
 *  用法
 * ═══════════════════════════════════════════════════════════════
 *   const vp = new Viewport(1000, 120);        // 初始视口：1000 根，最多显示 120 根
 *   const chart = new Chart(canvas, { viewport: vp, height: 320 });
 *   chart.setBars(bars);                        // [{t, o, h, l, c, v}, ...]
 *   chart.addSeries({ key:'ema12', color:'#f0ca57', values:[...] });
 *   chart.on('hover', ({ index, bar, x, y }) => { ... });
 *   vp.on('change', () => chart.draw());
 */

// ══════════════════════════════════════════════════════════════
//  视口
// ══════════════════════════════════════════════════════════════
export class Viewport {
  /**
   * @param {number} total      总数据根数
   * @param {number} count      可见根数（窗口宽度）
   * @param {number} start      可见区间起始索引
   */
  constructor(total = 0, count = 120, start = null) {
    this.total = total;
    this.count = count;
    this.start = start === null ? Math.max(0, total - count) : start;
    this._listeners = new Set();
    this._clamp();
  }

  /**
   * 订阅变更。
   *
   * ⚠️ 签名必须是 (name, fn)，不是 (fn)。
   *    Viewport 只有一种事件（change），最初图省事写成 on(fn)，
   *    但调用方按 Chart 的风格写 `vp.on('change', cb)` ——
   *    于是 'change' 这个字符串被当成回调收下、真正的 cb 被丢掉，
   *    表现为「按钮改了视口但界面不刷新」。极难定位，
   *    因为不报错（我加的校验把它降级成了 console.error）。
   *    现在两种调用方式都支持，谁也别猜。
   */
  on(nameOrFn, maybeFn) {
    let fn = maybeFn;
    if (typeof nameOrFn === 'function') {
      fn = nameOrFn;                      // on(fn) 形式
    } else if (maybeFn === undefined) {
      fn = nameOrFn;                      // on('change') 这种误用也当 fn 处理
    }
    if (typeof fn !== 'function') {
      console.error('[Viewport.on] 没有拿到回调函数。'
        + '正确用法：vp.on("change", fn) 或 vp.on(fn)。收到：',
        typeof nameOrFn, typeof maybeFn);
      return () => {};
    }
    this._listeners.add(fn);
    return () => { this._listeners.delete(fn); };
  }
  emit() {
    for (const fn of this._listeners) {
      if (typeof fn !== 'function') { this._listeners.delete(fn); continue; }
      try { fn(this); } catch (e) { console.error('viewport 监听器出错:', e); }
    }
  }

  _clamp() {
    this.count = Math.max(this.minCount, Math.min(this.count, this.total || 1));
    const maxStart = Math.max(0, (this.total || 1) - this.count);
    this.start = Math.max(0, Math.min(this.start, maxStart));
  }

  get minCount() { return 10; }
  get end() { return Math.min(this.start + this.count, this.total); }
  get visibleCount() { return this.end - this.start; }

  setTotal(n) {
    const wasAtRight = this.start + this.count >= this.total - 1;
    this.total = n;
    // 如果原本贴着右边缘（看最新），数据更新后继续贴右边缘
    if (wasAtRight) this.start = Math.max(0, n - this.count);
    this._clamp();
    this.emit();
  }

  /** 缩放。factor > 1 放大（可见根数变少），< 1 缩小 */
  zoom(factor, anchorRatio = 0.5) {
    const anchor = this.start + this.count * anchorRatio;
    const next = Math.round(this.count / factor);
    this.count = Math.max(this.minCount, Math.min(next, this.total || 1));
    this.start = Math.round(anchor - this.count * anchorRatio);
    this._clamp();
    this.emit();
  }

  /** 按像素平移。dxPx 为正表示向右拖（看更早的数据） */
  pan(dxPx, plotWidth) {
    const perBar = plotWidth / Math.max(1, this.count);
    this.start = Math.round(this.start - dxPx / perBar);
    this._clamp();
    this.emit();
  }

  showAll() { this.count = this.total || 1; this.start = 0; this._clamp(); this.emit(); }
  toLatest() { this.start = Math.max(0, this.total - this.count); this._clamp(); this.emit(); }
  seekTo(index) {
    this.start = Math.round(index - this.count / 2);
    this._clamp();
    this.emit();
  }
}

// ══════════════════════════════════════════════════════════════
//  工具
// ══════════════════════════════════════════════════════════════
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** 「漂亮」的刻度步长：1 / 2 / 5 × 10^n */
function niceStep(raw) {
  if (!(raw > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * mag;
}

/** 按可用宽度挑时间刻度步长，保证文字不重叠 */
function timeTickStep(count, plotWidth, minGapPx = 68) {
  const maxTicks = Math.max(2, Math.floor(plotWidth / minGapPx));
  return Math.max(1, Math.ceil(count / maxTicks));
}

function fmtNum(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
}

// ══════════════════════════════════════════════════════════════
//  图表
// ══════════════════════════════════════════════════════════════
export class Chart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts
   *   viewport   共享的 Viewport 实例
   *   height     画布高度（CSS 像素）
   *   type       'candle' | 'line' | 'histogram'
   *   valueKey   主数值字段（histogram/line 用）
   *   pad        内边距 {l, r, t, b}
   *   yFormat    价格格式化的小数位
   *   showVolume 是否在底部画成交量
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.viewport = opts.viewport || new Viewport();
    this.height = opts.height || 300;
    this.type = opts.type || 'candle';
    this.valueKey = opts.valueKey || 'c';
    this.pad = Object.assign({ l: 8, r: 64, t: 10, b: 20 }, opts.pad);
    this.yFormat = opts.yFormat ?? 2;
    this.showVolume = opts.showVolume ?? false;
    this.showCrosshair = opts.showCrosshair !== false;
    this.gridY = opts.gridY ?? 4;
    this.baseline = opts.baseline ?? null;      // 水平参考线（如初始资金）
    this.theme = Object.assign({
      // 与 theme.css v3 对齐：只有涨跌是彩色，其余全是灰阶。
      // 网格要极淡 —— 图表里的信息是 K 线，不是网格。
      bg: '#ffffff', grid: '#f2f2f0', gridStrong: '#e4e4e1',
      text: '#7c848a', textStrong: '#171a1e',
      up: '#a02a24', down: '#2e6152',
      crosshair: '#a9afb4', crosshairBg: 'rgba(255,255,255,.97)',
      border: '#d9d9d5',
    }, opts.theme || {});

    this.bars = [];
    this.series = [];        // [{key, color, values, width, dash}]
    this.histogram = null;   // {values, colorPos, colorNeg}
    this.markers = [];       // [{index, side:'buy'|'sell', price, label}]
    this.hlines = [];        // [{value, color, label, dash}]
    this._hover = null;      // {index, x, y, inside}
    this._listeners = new Map();
    this._ro = null;

    this._bindEvents();
    this._observeResize();
  }

  // ── 事件 ──────────────────────────────────────────────────
  on(name, fn) {
    if (typeof fn !== 'function') return () => {};
    if (!this._listeners.has(name)) this._listeners.set(name, new Set());
    this._listeners.get(name).add(fn);
    return () => { this._listeners.get(name)?.delete(fn); };
  }
  _emit(name, payload) {
    for (const fn of this._listeners.get(name) || []) {
      try { fn(payload); } catch (e) { console.error(`chart 事件 ${name} 出错:`, e); }
    }
  }

  // ── 数据 ──────────────────────────────────────────────────
  setBars(bars) {
    this.bars = bars || [];
    this.viewport.setTotal(this.bars.length);
    return this;
  }
  addSeries(s) { this.series.push(s); return this; }
  clearSeries() { this.series = []; return this; }
  setHistogram(h) { this.histogram = h; return this; }
  setMarkers(m) { this.markers = m || []; return this; }
  setHlines(h) { this.hlines = h || []; return this; }

  // ── 尺寸 ──────────────────────────────────────────────────
  get plotWidth() {
    return Math.max(1, this.canvas.clientWidth - this.pad.l - this.pad.r);
  }
  get plotHeight() {
    return Math.max(1, this.height - this.pad.t - this.pad.b);
  }

  _observeResize() {
    if (typeof ResizeObserver === 'undefined') return;
    // 用 ResizeObserver 而不是 window.resize：容器宽度变化时
    // window.resize 不会触发，而它恰恰是回测表填满后画布被撑宽的原因。
    //
    // ⚠️ 回调里必须**避免同步改变被观察元素的尺寸**。
    //    这里只重绘 canvas 的 width/height 属性，不动容器布局，
    //    所以不会触发 "ResizeObserver loop" 警告。
    //    真正的循环警告是浏览器抛给 window.onerror 的，属于噪声，
    //    页面里已经把它过滤掉了（见 pro.html 的错误处理）。
    this._ro = new ResizeObserver(() => {
      if (this._rafPending) return;
      this._rafPending = true;
      requestAnimationFrame(() => { this._rafPending = false; this.draw(); });
    });
    this._ro.observe(this.canvas.parentElement || this.canvas);
  }

  dispose() { this._ro?.disconnect(); this._listeners.clear(); }

  // ── 交互 ──────────────────────────────────────────────────
  _bindEvents() {
    const cv = this.canvas;
    let dragging = false, lastX = 0, moved = false;
    let pinch = null;

    const localPos = e => {
      const r = cv.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };

    cv.addEventListener('pointerdown', e => {
      cv.setPointerCapture?.(e.pointerId);
      if (e.pointerType === 'touch' && pinch) return;
      dragging = true; moved = false; lastX = e.clientX;
      cv.style.cursor = 'grabbing';
    });

    cv.addEventListener('pointermove', e => {
      const p = localPos(e);

      // 双指捏合
      if (pinch && e.pointerId !== pinch.idA && e.pointerId !== pinch.idB) return;
      if (pinch) {
        const pts = pinch.points;
        pts[e.pointerId] = p.x;
        const ids = Object.keys(pts);
        if (ids.length >= 2) {
          const dist = Math.abs(pts[ids[0]] - pts[ids[1]]);
          if (pinch.lastDist > 0) {
            const f = dist / pinch.lastDist;
            if (Math.abs(f - 1) > 0.01) this.viewport.zoom(f, 0.5);
          }
          pinch.lastDist = dist;
        }
        return;
      }

      if (dragging) {
        const dx = e.clientX - lastX;
        if (Math.abs(dx) > 0) { moved = true; this.viewport.pan(dx, this.plotWidth); lastX = e.clientX; }
      }

      // 十字光标
      const inside = p.x >= this.pad.l && p.x <= this.pad.l + this.plotWidth
                  && p.y >= this.pad.t && p.y <= this.pad.t + this.plotHeight;
      const idx = inside ? this.xToIndex(p.x) : null;
      const changed = (idx !== this._hover?.index) || (inside !== this._hover?.inside);
      this._hover = { index: idx, x: p.x, y: p.y, inside };
      if (changed || inside) {
        this.draw();
        this._emit('hover', {
          index: idx, bar: idx !== null ? this.bars[idx] : null,
          x: p.x, y: p.y, inside,
        });
      }
    });

    const endDrag = e => {
      dragging = false;
      cv.style.cursor = 'crosshair';
      if (pinch) {
        delete pinch.points[e.pointerId];
        if (Object.keys(pinch.points).length < 2) pinch = null;
      }
    };
    cv.addEventListener('pointerup', endDrag);
    cv.addEventListener('pointercancel', endDrag);
    cv.addEventListener('pointerleave', () => {
      if (!dragging) {
        this._hover = null;
        this.draw();
        this._emit('hover', { index: null, bar: null, inside: false });
      }
    });

    // 触摸：双指开始捏合
    cv.addEventListener('touchstart', e => {
      if (e.touches.length === 2) {
        dragging = false;
        pinch = {
          points: {}, lastDist: 0,
          idA: e.touches[0].identifier, idB: e.touches[1].identifier,
        };
      }
    }, { passive: true });

    // 滚轮缩放（以光标为锚点）
    cv.addEventListener('wheel', e => {
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      const ratio = clamp((e.clientX - r.left - this.pad.l) / this.plotWidth, 0, 1);
      this.viewport.zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, ratio);
    }, { passive: false });

    cv.style.cursor = 'crosshair';
    cv.style.touchAction = 'none';   // 让 pointer 事件接管，避免浏览器滚动干扰
  }

  indexToX(i) {
    const vp = this.viewport;
    const n = Math.max(1, vp.visibleCount);
    // 用 (i - start + 0.5) 让 K 线落在格子的中心
    return this.pad.l + ((i - vp.start + 0.5) / n) * this.plotWidth;
  }
  xToIndex(x) {
    const vp = this.viewport;
    const n = Math.max(1, vp.visibleCount);
    const raw = vp.start + ((x - this.pad.l) / this.plotWidth) * n - 0.5;
    return clamp(Math.round(raw), 0, Math.max(0, this.bars.length - 1));
  }

  /**
   * 计算 Y 轴范围。
   *
   * ⚠️ 只统计**这个面板真正要画的东西**。
   *    最初这里无条件统计了 bars 的 high/low —— 对 K 线图是对的，
   *    但对 MACD / RSI 面板是灾难：那些面板把 bars 只当时间轴用，
   *    数值走 series 和 histogram。结果 RSI（0~100）的 Y 轴被
   *    价格（~4000）撑成 4597 ~ -216，曲线压成一条直线，完全没法看。
   *
   *    所以按 chart.type 决定是否纳入 bars 的价幅：
   *      candle  → 需要（影线决定范围）
   *      其它    → 不需要，只看 series 与 histogram
   */
  visibleRange() {
    const vp = this.viewport;
    let lo = Infinity, hi = -Infinity;

    if (this.type === 'candle') {
      for (let i = vp.start; i < vp.end && i < this.bars.length; i++) {
        const b = this.bars[i];
        if (!b) continue;
        const h = b.h ?? b.c, l = b.l ?? b.c;
        if (Number.isFinite(h)) hi = Math.max(hi, h);
        if (Number.isFinite(l)) lo = Math.min(lo, l);
      }
    }

    for (const s of this.series) {
      const vals = s.values || [];
      for (let i = vp.start; i < vp.end && i < vals.length; i++) {
        const v = vals[i];
        if (v === null || v === undefined || Number.isNaN(v)) continue;
        hi = Math.max(hi, v); lo = Math.min(lo, v);
      }
    }

    if (this.histogram?.values) {
      const hv = this.histogram.values;
      for (let i = vp.start; i < vp.end && i < hv.length; i++) {
        const v = hv[i];
        if (v === null || v === undefined || Number.isNaN(v)) continue;
        hi = Math.max(hi, v); lo = Math.min(lo, v);
      }
    }

    for (const h of this.hlines) {
      if (Number.isFinite(h.value)) { hi = Math.max(hi, h.value); lo = Math.min(lo, h.value); }
    }

    if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
    if (hi === lo) { hi += Math.abs(hi) * 0.01 + 1; lo -= Math.abs(lo) * 0.01 + 1; }
    const padY = (hi - lo) * 0.08;
    return { lo: lo - padY, hi: hi + padY };
  }

  yToPx(v, range) {
    return this.pad.t + (range.hi - v) / (range.hi - range.lo) * this.plotHeight;
  }
  pxToY(y, range) {
    return range.hi - (y - this.pad.t) / this.plotHeight * (range.hi - range.lo);
  }

  // ── 绘制 ──────────────────────────────────────────────────
  draw() {
    const cv = this.canvas;
    const dpr = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    const w = cv.clientWidth || 600;
    const h = this.height;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    cv.style.height = h + 'px';
    const c = this.ctx;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    c.fillStyle = this.theme.bg;
    c.fillRect(0, 0, w, h);

    if (!this.bars.length) {
      c.fillStyle = this.theme.text;
      c.font = '11px ';
      c.fillText('暂无数据', this.pad.l + 8, h / 2);
      return;
    }

    const range = this.visibleRange();
    this._drawGrid(c, range);
    this._drawHlines(c, range);

    if (this.type === 'candle') this._drawCandles(c, range);
    else if (this.type === 'histogram') this._drawHistogram(c, range);
    else this._drawLineSeries(c, range, this.valueKey);

    for (const s of this.series) this._drawLine(c, s, range);
    this._drawMarkers(c, range);
    if (this.showVolume) this._drawVolume(c, range);
    this._drawPriceAxis(c, range);
    this._drawTimeAxis(c);
    if (this.showCrosshair && this._hover?.inside && this._hover.index !== null) {
      this._drawCrosshair(c, range);
    }
  }

  _drawGrid(c, range) {
    const { l, r, t, b } = this.pad;
    const pw = this.plotWidth, ph = this.plotHeight;
    c.strokeStyle = this.theme.grid;
    c.lineWidth = 1;
    c.beginPath();
    for (let g = 0; g <= this.gridY; g++) {
      const y = Math.round(t + ph * g / this.gridY) + 0.5;
      c.moveTo(l, y); c.lineTo(l + pw, y);
    }
    c.stroke();
  }

  _drawHlines(c, range) {
    const { l, t, b } = this.pad;
    const pw = this.plotWidth, ph = this.plotHeight;
    for (const h of this.hlines) {
      if (!Number.isFinite(h.value)) continue;
      const y = Math.round(this.yToPx(h.value, range)) + 0.5;
      if (y < t || y > t + ph) continue;
      c.save();
      c.setLineDash(h.dash || [3, 4]);
      c.strokeStyle = h.color || this.theme.gridStrong;
      c.beginPath(); c.moveTo(l, y); c.lineTo(l + pw, y); c.stroke();
      c.restore();
      if (h.label) {
        c.fillStyle = h.color || this.theme.text;
        c.font = '9px ';
        c.textAlign = 'left';
        c.fillText(h.label, l + 4, y - 3);
      }
    }
  }

  _drawCandles(c, range) {
    const vp = this.viewport;
    const n = Math.max(1, vp.visibleCount);
    const slot = this.plotWidth / n;
    const bw = Math.max(1, Math.min(slot * 0.72, 18));
    const { up, down } = this.theme;

    for (let i = vp.start; i < vp.end; i++) {
      const bar = this.bars[i];
      if (!bar) continue;
      const x = this.indexToX(i);
      const o = bar.o ?? bar.c, hh = bar.h ?? bar.c,
            ll = bar.l ?? bar.c, cl = bar.c;
      const yO = this.yToPx(o, range), yC = this.yToPx(cl, range);
      const yH = this.yToPx(hh, range), yL = this.yToPx(ll, range);
      const isUp = cl >= o;
      const color = isUp ? up : down;

      // 影线
      c.strokeStyle = color;
      c.lineWidth = 1;
      c.beginPath();
      const xc = Math.round(x) + 0.5;
      c.moveTo(xc, yH); c.lineTo(xc, yL);
      c.stroke();

      // 实体
      const top = Math.min(yO, yC);
      const bh = Math.max(1, Math.abs(yC - yO));
      c.fillStyle = color;
      if (bw <= 2) {
        c.fillRect(xc - 0.5, top, 1.5, bh);
      } else {
        c.fillRect(Math.round(x - bw / 2), Math.round(top), Math.round(bw), Math.round(bh));
      }
    }
  }

  _drawHistogram(c, range) {
    const hg = this.histogram;
    if (!hg) return;
    const vp = this.viewport;
    const n = Math.max(1, vp.visibleCount);
    const slot = this.plotWidth / n;
    const bw = Math.max(1, Math.min(slot * 0.6, 12));
    // 直方图以 0 为基准，所以 Y 范围要含 0
    const zero = this.yToPx(0, range);
    c.save();
    for (let i = vp.start; i < vp.end; i++) {
      const v = hg.values[i];
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      const x = this.indexToX(i);
      const y = this.yToPx(v, range);
      c.fillStyle = v >= 0 ? (hg.colorPos || this.theme.up) : (hg.colorNeg || this.theme.down);
      c.globalAlpha = 0.85;
      const top = Math.min(y, zero);
      const bh = Math.max(1, Math.abs(zero - y));
      c.fillRect(Math.round(x - bw / 2), Math.round(top), Math.round(bw), Math.round(bh));
    }
    c.restore();
    // 零轴
    c.strokeStyle = this.theme.gridStrong;
    c.beginPath();
    c.moveTo(this.pad.l, Math.round(zero) + 0.5);
    c.lineTo(this.pad.l + this.plotWidth, Math.round(zero) + 0.5);
    c.stroke();
  }

  _drawLineSeries(c, range, key) {
    const vals = this.bars.map(b => b[key]);
    this._drawLine(c, { values: vals, color: this.theme.textStrong, width: 1.6 }, range);
  }

  _drawLine(c, s, range) {
    const vp = this.viewport;
    c.save();
    c.strokeStyle = s.color || '#1a1d21';
    c.lineWidth = s.width || 1.4;
    if (s.dash) c.setLineDash(s.dash);
    c.beginPath();
    let started = false;
    for (let i = vp.start; i < Math.min(vp.end, s.values.length); i++) {
      const v = s.values[i];
      if (v === null || v === undefined || Number.isNaN(v)) { started = false; continue; }
      const x = this.indexToX(i), y = this.yToPx(v, range);
      if (!started) { c.moveTo(x, y); started = true; } else c.lineTo(x, y);
    }
    c.stroke();
    c.restore();
  }

  _drawMarkers(c, range) {
    const vp = this.viewport;
    for (const m of this.markers) {
      if (m.index < vp.start || m.index >= vp.end) continue;
      const x = this.indexToX(m.index);
      const v = Number.isFinite(m.price) ? m.price : this.bars[m.index]?.l;
      if (!Number.isFinite(v)) continue;
      const y = this.yToPx(v, range);
      const isBuy = m.side === 'buy';
      c.fillStyle = isBuy ? this.theme.up : this.theme.down;
      c.beginPath();
      if (isBuy) {
        c.moveTo(x, y + 9); c.lineTo(x - 4.5, y + 1); c.lineTo(x + 4.5, y + 1);
      } else {
        c.moveTo(x, y - 9); c.lineTo(x - 4.5, y - 1); c.lineTo(x + 4.5, y - 1);
      }
      c.closePath(); c.fill();
    }
  }

  _drawVolume(c, range) {
    const vp = this.viewport;
    let vmax = 0;
    for (let i = vp.start; i < vp.end; i++) {
      const v = this.bars[i]?.v;
      if (Number.isFinite(v)) vmax = Math.max(vmax, v);
    }
    if (vmax <= 0) return;
    const n = Math.max(1, vp.visibleCount);
    const slot = this.plotWidth / n;
    const bw = Math.max(1, Math.min(slot * 0.6, 12));
    const boxH = this.plotHeight * 0.18;
    const baseY = this.pad.t + this.plotHeight;
    c.save();
    c.globalAlpha = 0.42;
    for (let i = vp.start; i < vp.end; i++) {
      const bar = this.bars[i];
      if (!bar || !Number.isFinite(bar.v)) continue;
      const x = this.indexToX(i);
      const bh = Math.max(1, (bar.v / vmax) * boxH);
      c.fillStyle = (bar.c >= (bar.o ?? bar.c)) ? this.theme.up : this.theme.down;
      c.fillRect(Math.round(x - bw / 2), Math.round(baseY - bh), Math.round(bw), Math.round(bh));
    }
    c.restore();
  }

  _drawPriceAxis(c, range) {
    const x = this.pad.l + this.plotWidth;
    c.save();
    c.fillStyle = this.theme.text;
    c.font = '10px ';
    c.textAlign = 'left';
    c.textBaseline = 'middle';
    for (let g = 0; g <= this.gridY; g++) {
      const y = this.pad.t + this.plotHeight * g / this.gridY;
      const v = range.hi - (range.hi - range.lo) * g / this.gridY;
      c.fillText(fmtNum(v, this.yFormat), x + 5, y);
    }
    c.restore();
  }

  /** X 轴时间刻度 —— 这是原来看板完全缺失的部分 */
  _drawTimeAxis(c) {
    const vp = this.viewport;
    const n = vp.visibleCount;
    const step = timeTickStep(n, this.plotWidth);
    const { l, t } = this.pad;
    const yBase = t + this.plotHeight;
    c.save();
    c.font = '9px ';
    c.textAlign = 'center';
    c.textBaseline = 'top';
    c.strokeStyle = this.theme.gridStrong;
    c.fillStyle = this.theme.text;

    for (let i = vp.start; i < vp.end; i += step) {
      const bar = this.bars[i];
      if (!bar) continue;
      const x = Math.round(this.indexToX(i)) + 0.5;
      c.beginPath(); c.moveTo(x, yBase); c.lineTo(x, yBase + 4); c.stroke();
      const label = this._tickLabel(bar.t, n);
      c.fillText(label, x, yBase + 5);
    }
    c.restore();
  }

  /** 根据跨度决定显示日期还是时间 */
  _tickLabel(t, visibleCount) {
    if (!t) return '';
    const s = String(t);
    // 跨度大（>200 根）只显示日期，否则显示日期+时间
    if (visibleCount > 200) return s.slice(5, 10);
    if (s.length >= 16) return s.slice(5, 16);
    return s.slice(5);
  }

  _drawCrosshair(c, range) {
    const { index, x, y } = this._hover;
    const { l, t } = this.pad;
    const pw = this.plotWidth, ph = this.plotHeight;
    const cx = Math.round(this.indexToX(index)) + 0.5;

    c.save();
    c.setLineDash([3, 3]);
    c.strokeStyle = this.theme.crosshair;
    c.lineWidth = 1;
    // 竖线
    c.beginPath(); c.moveTo(cx, t); c.lineTo(cx, t + ph); c.stroke();
    // 横线（只在 y 落在绘图区内时画）
    const insideY = y >= t && y <= t + ph;
    if (insideY) {
      const hy = Math.round(y) + 0.5;
      c.beginPath(); c.moveTo(l, hy); c.lineTo(l + pw, hy); c.stroke();
    }
    c.restore();

    // 右侧价格标签
    if (insideY) {
      const v = this.pxToY(y, range);
      const label = fmtNum(v, this.yFormat);
      c.font = '10px ';
      const tw = c.measureText(label).width + 8;
      const bx = l + pw + 2;
      c.fillStyle = this.theme.crosshairBg;
      c.strokeStyle = this.theme.crosshair;
      c.lineWidth = 1;
      const by = clamp(y - 8, t, t + ph - 16);
      c.beginPath(); c.rect(bx, by, tw, 16); c.fill(); c.stroke();
      c.fillStyle = this.theme.textStrong;
      c.textAlign = 'left'; c.textBaseline = 'middle';
      c.fillText(label, bx + 4, by + 8);
    }

    // 底部时间标签
    const bar = this.bars[index];
    if (bar?.t) {
      const label = String(bar.t);
      c.font = '10px ';
      const tw = c.measureText(label).width + 10;
      const bx = clamp(cx - tw / 2, l, l + pw - tw);
      const by = t + ph + 1;
      c.fillStyle = this.theme.crosshairBg;
      c.strokeStyle = this.theme.crosshair;
      c.beginPath(); c.rect(bx, by, tw, 15); c.fill(); c.stroke();
      c.fillStyle = this.theme.textStrong;
      c.textAlign = 'center'; c.textBaseline = 'middle';
      c.fillText(label, bx + tw / 2, by + 8);
    }
  }
}

// ══════════════════════════════════════════════════════════════
//  Tooltip —— DOM 覆盖层
// ══════════════════════════════════════════════════════════════
/**
 * 为什么用 DOM 而不是画在 canvas 上：
 *   · 文字排版不用自己算换行/对齐
 *   · 可以用 CSS 做样式，改起来快
 *   · 不会被 canvas 的裁剪区域限制
 * 代价是需要跟着鼠标移动，注意不要遮住光标位置。
 */
export class Tooltip {
  constructor(container, opts = {}) {
    this.el = document.createElement('div');
    this.el.className = 'chart-tooltip';
    Object.assign(this.el.style, {
      position: 'absolute', pointerEvents: 'none', zIndex: 20,
      background: 'rgba(255,255,255,.98)', border: '1px solid #e6e6e3',
      borderRadius: '4px', padding: '7px 10px', fontSize: '11px',
      lineHeight: '1.7', color: '#171a1e', whiteSpace: 'nowrap',
      boxShadow: '0 16px 48px rgba(23,26,30,.12)', display: 'none',
      fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
      transition: 'opacity .08s',
    }, opts.style || {});
    container.style.position = container.style.position || 'relative';
    container.appendChild(this.el);
    this.container = container;
    this.offset = opts.offset || 14;
  }

  show(html, x, y) {
    this.el.innerHTML = html;
    this.el.style.display = 'block';
    const cw = this.container.clientWidth;
    const ch = this.container.clientHeight;
    const w = this.el.offsetWidth;
    const h = this.el.offsetHeight;
    // 靠近右边缘时翻到左侧，避免被裁掉
    let left = x + this.offset;
    if (left + w > cw - 4) left = x - w - this.offset;
    left = Math.max(4, Math.min(left, cw - w - 4));
    let top = y + this.offset;
    if (top + h > ch - 4) top = y - h - this.offset;
    top = Math.max(4, Math.min(top, ch - h - 4));
    this.el.style.left = left + 'px';
    this.el.style.top = top + 'px';
  }

  hide() { this.el.style.display = 'none'; }
  dispose() { this.el.remove(); }
}

// ══════════════════════════════════════════════════════════════
//  辅助：把数据整理成图表要的格式
// ══════════════════════════════════════════════════════════════
/** 从 pipeline 的 JSON 里抽序列 */
export function seriesFrom(data, key, color, opts = {}) {
  const line = data.indicators?.[key];
  return {
    key, color,
    values: line?.values || line || [],
    width: opts.width || 1.4,
    dash: opts.dash,
  };
}

/** 从回测成交点构造标记 */
export function markersFrom(fills, bars) {
  const out = [];
  for (const f of fills || []) {
    let idx = bars.findIndex(b => b.t === f.at);
    if (idx < 0 && Number.isFinite(f.bar)) idx = f.bar;
    if (idx < 0) continue;
    out.push({ index: idx, side: f.side, price: f.price });
  }
  return out;
}

export { fmtNum, niceStep, timeTickStep };
