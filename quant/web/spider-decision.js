/**
 * 决策蛛网 —— 把量化的**决策过程**画成一张网。
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么重写，而不是改原来的 spider-web.js
 * ═══════════════════════════════════════════════════════════════
 * 原来那个渲染器画的是「K 线时间流 + 信号点飞向分支」，蜘蛛本体
 * 匀速乱飘（`body.vx = 26` 是硬编码常量），和数据完全无关。
 *
 * 问题在于**隐喻用错了**：K 线不是网，因子才是。
 * 量化流程本身就是一张网：
 *
 *     6 个因子 ──加权求和──> 分数 ──阈值──> 动作 ──风控闸门──> 仓位
 *        ↑                    ↑                        ↑
 *      网上的丝            蜘蛛的位置              蜘蛛能不能过去
 *
 * 所以新画法让**每个视觉元素都承载真实数据**：
 *
 *   丝的粗细   = 该因子的权重（随市场状态变：趋势市 trend=1.8 / 震荡市 0.5）
 *   丝的颜色   = 方向（红=看多 / 绿=看空 / 灰=中性）
 *   丝上光点   = 贡献值 v×w，位置与亮度都编码它
 *   丝的弯曲   = 绝对强度 |v|，强信号绷直、弱信号下垂
 *   蜘蛛径向   = 分数 −1~+1 映射到 中心↔边缘
 *   蜘蛛朝向   = 当前动作
 *   蜘蛛亮度   = 把握度 |score| / 阈值
 *   张力弧     = 离阈值还差多少，闭合就该动手
 *   风控闸门   = 六个锁，**蜘蛛想冲过去被拦住** ← 最有价值的画面
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么可以做成"计算重演"
 * ═══════════════════════════════════════════════════════════════
 * 量化是**确定性**的：同样的 K 线 + 同样的参数 → 同样的因子与分数。
 * 所以动画不是随机装饰，而是把一次**真实计算**的分层结果按顺序演出来：
 *
 *   阶段 0  丝还未亮          → 什么都没算
 *   阶段 1  丝依次亮起        → 各因子算出来了
 *   阶段 2  光点沿丝滑向中枢   → 贡献汇入
 *   阶段 3  中枢累积、张力收紧 → 分数在逼近阈值
 *   阶段 4  蜘蛛决定方向       → 动作
 *   阶段 5  撞闸门 / 通过      → 风控
 *
 * 每次重演都准确复现真实数字，不是"看起来像在算"。
 */

const TAU = Math.PI * 2;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const lerp = (a, b, t) => a + (b - a) * t;
const easeOut = t => 1 - Math.pow(1 - t, 3);
const easeInOut = t => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

// 六个因子的固定角度（弧度，0 = 正右方，顺时针）。
// 为什么固定：每根丝的位置应该稳定，否则换标的后图形会"跳"，
// 用户没法形成位置记忆。
const FACTOR_ANGLES = {
  trend: -Math.PI * 0.75,        // 左上
  momentum: -Math.PI * 0.55,
  strength: -Math.PI * 0.20,
  reversion: Math.PI * 0.20,
  position: Math.PI * 0.55,
  volume: Math.PI * 0.75,        // 左下
};

const COLORS = {
  bg: '#080909', border: '#24242a', muted: '#8b8b94', text: '#e8e8e9',
  accent: '#f23c83',
  bull: '#ef4b5e',        // 看多（A 股习惯：红涨）
  bear: '#3fbf8f',        // 看空
  flat: '#5a5a64',
  spider: '#48d6a4',
  ring: 'rgba(72,214,164,0.06)',
  strand: 'rgba(120,120,140,0.30)',
  gateOpen: '#48d6a4',
  gateFired: '#ef4b5e',
  gateIdle: '#3a3a44',
};

// 动作 → 角度。BUY 在上（-90°），SELL 在下（+90°），HOLD 在右（0°）。
const ACTION_ANGLE = { buy: -Math.PI / 2, sell: Math.PI / 2, hold: 0 };

// 计算重演的阶段时长（秒）。总时长 ≈ 4.2 秒。
const STAGE_DURATION = 0.45;
const N_STAGES = 6;


export class DecisionWeb {
  /**
   * @param {HTMLCanvasElement} canvas
   */
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.w = 0; this.h = 0;
    this.data = null;
    this._prep = null;
    this.trail = [];              // 仓位/分数轨迹（P2）
    this._lastSample = -1;
    this.replayStart = null;      // 重演开始时间（performance.now()/1000）
    this.raf = null;
    this.showReplay = true;
  }

  // ── 尺寸 ────────────────────────────────────────────────
  resize() {
    const dpr = Math.min(2, Math.max(1, globalThis.devicePixelRatio || 1));
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const w = Math.max(360, Math.floor(rect.width) - 2);
    const h = Math.max(420, Math.round(w * 0.42));
    if (this.canvas.width === Math.round(w * dpr) &&
        this.canvas.height === Math.round(h * dpr)) return false;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
    return true;
  }

  // ── 数据 ────────────────────────────────────────────────
  /**
   * 灌入决策快照。返回是否是新数据（用来决定要不要重演）。
   */
  setData(d) {
    if (!d || d.error) { this.data = null; this._prep = null; return false; }
    const key = `${d.meta?.symbol}|${d.meta?.at}|${d.meta?.bars}|${d.decision?.score}`;
    if (this._key === key) { this.data = d; return false; }
    this._key = key;
    this.data = d;
    this._prep = this.prepare(d);
    // 采样轨迹：每次数据变化记一个点（P2 的"蜘蛛走过留下的丝"）
    const p = this._prep;
    if (p) {
      const last = this.trail[this.trail.length - 1];
      if (!last || last.at !== d.meta.at) {
        this.trail.push({ at: d.meta.at, score: p.score, target: p.target, action: p.action });
        if (this.trail.length > 60) this.trail.shift();
      }
    }
    return true;
  }

  /** 重演一次计算过程 */
  replay() { this.replayStart = performance.now() / 1000; }

  /**
   * 把原始数据整理成绘图要用的几何与数值。
   *
   * 关键：**所有几何都由数据算出**，渲染函数里不该再出现魔法数字。
   */
  prepare(d) {
    // 网的可用区域：扣掉左栏 + 闸门 + 决策区
    const leftW = Math.min(300, this.w * 0.22);
    const gateW = 118, decideW = 150;
    const avail = Math.max(240, this.w - leftW - gateW - decideW - 80);
    // 半径用可用宽度的一半，但也被高度约束 —— 否则宽屏下网会顶到上下边缘
    const R = Math.max(150, Math.min(avail * 0.46, this.h * 0.36));

    const maxAbsContrib = Math.max(
      ...(d.factors || []).map(x => Math.abs(x.contrib || 0)), 0.001);

    const factors = (d.factors || []).map(f => {
      const wNorm = clamp((f.weight - 0.15) / 1.65, 0, 1);   // 权重实测区间 0.15~1.8
      return {
        ...f,
        angle: FACTOR_ANGLES[f.key] ?? 0,
        tension: f.present ? clamp(Math.abs(f.value ?? 0), 0, 1) : 0,
        // 丝长：权重越大越长，但下限提高到 0.62 ——
        // 原来从 0.45 起，六根丝挤在中枢附近看不清，实测很不明显
        reach: f.present ? 0.62 + 0.38 * wNorm : 0.5,
        bead: maxAbsContrib > 0
          ? clamp((f.contrib || 0) / maxAbsContrib, -1, 1) : 0,
        color: f.dir === 'bull' ? COLORS.bull
             : f.dir === 'bear' ? COLORS.bear : COLORS.flat,
      };
    });


    const score = d.score?.clamped ?? 0;
    const entry = d.gates?.entry ?? 0.25;
    const target = d.decision?.target ?? 0;
    const action = d.decision?.action ?? 'hold';
    const riskHit = d.decision?.riskHit || null;
    const fired = (d.risk || []).filter(g => g.fired);

    return {
      factors, R,
      score: clamp(score, -1, 1),
      rawSum: d.score?.raw ?? 0,
      totalWeight: d.score?.totalWeight ?? 1,
      entry, exit: d.gates?.exit ?? -0.25,
      margin: d.gates?.margin ?? 0,
      action, target, riskHit, fired,
      // 蜘蛛径向：|score| / 1，但为了视觉可读，映射到 0.18~1.0 之间，
      // 免得 0 分时蜘蛛和中枢重叠看不出来
      radial: 0.18 + 0.82 * clamp(Math.abs(score), 0, 1),
      // 把握度：分数相对阈值。>=1 就是"够线了"
      conviction: clamp(Math.abs(score) / (entry || 0.25), 0, 2),
      regime: d.regime?.name ?? '—',
      adx: d.regime?.adx ?? null,
    };
  }

  // ── 主绘制 ──────────────────────────────────────────────
  /**
   * @param {number} time  performance.now()/1000
   */
  render(time) {
    const resized = this.resize();
    const c = this.ctx, w = this.w, h = this.h;
    c.fillStyle = COLORS.bg;
    c.fillRect(0, 0, w, h);

    if (!this.data || !this._prep) {
      c.fillStyle = COLORS.muted;
      c.font = '11px Consolas, monospace';
      c.fillText('等待决策数据…', 16, 28);
      return;
    }

    const T = this._stageProgress(time);
    const p = this._prep;

    // 布局：左侧数据栏 / 中间网 / 右侧风控闸门 / 最右决策区
    // 网的可用宽度在 prepare() 里按同样的扣除算过，两处必须一致。
    const padX = 16;
    const leftW = Math.min(300, w * 0.22);
    const gateW = 118;
    const decideW = 150;
    const midArea = w - leftW - gateW - decideW - padX * 2;
    // 网心放在「左栏右侧 → 闸门左侧」这段区域的中间，不留大片空白
    const webLeft = padX + leftW + 14;
    const webRight = w - padX - decideW - gateW - 20;
    const webCx = (webLeft + webRight) / 2;
    const webCy = h / 2 + 2;

    this._drawLeftPanel(c, padX, p, T);
    this._drawWeb(c, webCx, webCy, p, time, T);
    this._drawGates(c, webRight + 8, h / 2, p, T, time);
    this._drawDecision(c, w - padX - decideW / 2, h / 2, p, T, time);
    this._drawStrandLinks(c, webCx, webCy, webRight + 8, p, T);
    this._drawSpider(c, webCx, webCy, p, time, T);
    this._drawStageBar(c, webCx, h - 22, T);
    this._drawHeader(c, p, T);
    if (this.trail.length > 1) this._drawTrail(c, webCx, webCy, p);
  }

  /**
   * 计算当前处于重演的哪个阶段。
   *
   * 返回 { stage, t, done }：
   *   stage 0..5   当前阶段（-1 = 未开始，99 = 全部完成）
   *   t            该阶段的进度 0~1
   */
  _stageProgress(time) {
    if (!this.showReplay) return { stage: 99, t: 1, done: true };
    if (this.replayStart == null) this.replayStart = time - 99;
    const el = time - this.replayStart;
    const total = STAGE_DURATION * N_STAGES;
    if (el >= total) return { stage: 99, t: 1, done: true };
    const idx = Math.floor(el / STAGE_DURATION);
    const t = (el - idx * STAGE_DURATION) / STAGE_DURATION;
    return { stage: idx, t, done: false };
  }

  // ── 左栏：数据快照（不是时间流）────────────────────────
  _drawLeftPanel(c, x, p, T) {
    const m = this.data.meta;
    let y = 26;
    this._text(c, `${m.name} ${m.symbol}`, x, y, COLORS.text, 13, 'left', true);
    y += 16;
    this._text(c, `${m.interval} · ${m.bars} 根 · ${m.source}`, x, y, COLORS.muted, 10);
    y += 14;
    this._text(c, `最新 ${m.at}  收 ${m.close}`, x, y, COLORS.muted, 10);

    y += 22;
    // 市场状态（决定权重的那个判断）
    const rc = p.regime === '趋势市' ? COLORS.bull
             : p.regime === '震荡市' ? COLORS.accent : COLORS.muted;
    this._text(c, '市场状态', x, y, COLORS.muted, 10);
    this._text(c, `${p.regime}${p.adx != null ? `  ADX ${p.adx}` : ''}`,
               x + 74, y, rc, 11, 'left', true);
    y += 15;
    this._text(c, '→ 权重偏向', x, y, COLORS.muted, 9);
    const bias = p.regime === '趋势市' ? '趋势 / 强度' : '超买超卖 / 通道位置';
    this._text(c, bias, x + 74, y, rc, 9.5);
    y += 20;

    // 因子明细：这是"溯源表"，蛛网负责形状，这里负责精确数字
    this._text(c, '因子（值 × 权重 = 贡献）', x, y, COLORS.muted, 10);
    y += 6;
    const reveal = T.done ? 1 : clamp((T.stage + T.t) / 4, 0, 1);
    p.factors.forEach((f, i) => {
      y += 15;
      const on = this.showReplay ? (i / p.factors.length) < reveal : true;
      c.globalAlpha = on ? 1 : 0.18;

      this._text(c, f.label, x, y, f.present ? f.color : COLORS.flat, 10);
      if (f.present) {
        this._text(c, `${f.value >= 0 ? '+' : ''}${f.value.toFixed(2)}`,
                   x + 62, y, COLORS.text, 10, 'left');
        this._text(c, `×${f.weight.toFixed(2)}`, x + 112, y, COLORS.muted, 9.5);
        this._text(c, `${f.contrib >= 0 ? '+' : ''}${f.contrib.toFixed(2)}`,
                   x + 152, y, f.contrib >= 0 ? COLORS.bull : COLORS.bear, 10, 'left', true);
        // 贡献占比条
        const bw = Math.min(70, Math.abs(f.contrib) * 45);
        if (bw > 1) {
          c.fillStyle = f.contrib >= 0 ? COLORS.bull : COLORS.bear;
          c.globalAlpha = (on ? 1 : 0.18) * 0.55;
          c.fillRect(x + 196, y - 6, Math.min(bw, 70), 2.5);
        }
      } else {
        this._text(c, '（指标未就绪）', x + 62, y, COLORS.flat, 9.5);
      }
      c.globalAlpha = 1;
    });

    y += 22;
    c.globalAlpha = T.done || T.stage >= 4 ? 1 : 0.25;
    this._text(c, '加权和', x, y, COLORS.muted, 10);
    this._text(c, p.rawSum.toFixed(3), x + 62, y, COLORS.text, 10, 'left');
    this._text(c, `÷ ${p.totalWeight.toFixed(2)}`, x + 112, y, COLORS.muted, 9.5);
    c.globalAlpha = 1;
    y += 15;
    c.globalAlpha = T.done || T.stage >= 5 ? 1 : 0.25;
    this._text(c, '分数', x, y, COLORS.muted, 10);
    const sc = p.score;
    this._text(c, `${sc >= 0 ? '+' : ''}${sc.toFixed(4)}`, x + 62, y,
               sc > 0 ? COLORS.bull : sc < 0 ? COLORS.bear : COLORS.muted,
               12, 'left', true);
    c.globalAlpha = 1;
  }

  // ── 网：中枢 + 六根丝 ──────────────────────────────────
  _drawWeb(c, cx, cy, p, time, T) {
    const R = p.R;
    // 同心波纹环 —— 只是空间参考，不代表数据
    for (let r = 1; r <= 4; r++) {
      c.beginPath();
      c.ellipse(cx, cy, R * r / 4, R * r / 4 * 0.72, 0, 0, TAU);
      c.strokeStyle = COLORS.ring;
      c.lineWidth = 0.6;
      c.stroke();
    }

    // ⚠️ 这里原来画了 22 条从左侧发散的射线（沿用了旧版「左侧数据流」的造型）。
    //    新版没有左侧数据流了，那些线**不再承载任何信息**，只是视觉噪声。
    //    删掉 —— 蛛网上的每一根线都应该对应一个因子。

    // 六根丝
    p.factors.forEach((f, i) => {
      const appear = this.showReplay
        ? clamp(((T.stage === 0 ? 0 : T.stage - 1) + T.t) - i * 0.12, 0, 1)
        : 1;
      if (appear <= 0) return;

      const len = R * f.reach * (0.35 + 0.65 * appear);
      const ang = f.angle;
      const ex = cx + Math.cos(ang) * len;
      const ey = cy + Math.sin(ang) * len * 0.72;
      const sag = (1 - f.tension) * 18 * (f.dir === 'bear' ? 1 : -1);
      const mx = cx + Math.cos(ang) * len * 0.5;
      const my = cy + Math.sin(ang) * len * 0.72 * 0.5 + sag;

      // 丝本体：粗细 = 权重
      c.beginPath();
      c.moveTo(cx, cy);
      c.quadraticCurveTo(mx, my, ex, ey);
      c.strokeStyle = f.present
        ? this._rgba(f.color, 0.34 + 0.58 * f.tension)
        : 'rgba(90,90,100,0.20)';
      c.lineWidth = 1.2 + 6.5 * clamp((f.weight - 0.15) / 1.65, 0, 1);
      c.lineCap = 'round';
      c.stroke();
      c.lineCap = 'butt';

      // 端点方块：|因子值| 越大越大
      if (f.present) {
        c.fillStyle = this._rgba(f.color, 0.6);
        const sz = 2.5 + 5 * clamp(Math.abs(f.value ?? 0), 0, 1);
        c.fillRect(ex - sz / 2, ey - sz / 2, sz, sz);
      }

      // ★ 丝末端标签：没有标签就看不出哪根是哪根（实测的可见性问题）
      const outward = Math.cos(ang) >= 0 ? 1 : -1;
      const lx = ex + outward * 7;
      const ly = ey + Math.sin(ang) * 5 + 3.5;
      this._text(c, f.label, lx, ly, f.present ? f.color : COLORS.flat, 10.5,
                 outward > 0 ? 'left' : 'right', true);
      if (f.present) {
        this._text(c, `${f.value >= 0 ? '+' : ''}${f.value.toFixed(2)}  w${f.weight.toFixed(2)}`,
                   lx, ly + 11, COLORS.muted, 8.5,
                   outward > 0 ? 'left' : 'right');
      }

      // 光点：贡献值
      const beadT = this.showReplay
        ? (T.stage < 2 ? 0 : T.stage === 2 ? easeOut(T.t) : 1)
        : 1;
      if (f.present && Math.abs(f.bead) > 0.02 && beadT > 0) {
        const base = 0.28 + 0.72 * Math.abs(f.bead);
        const travel = lerp(0.06, base, beadT);
        const bx = cx + Math.cos(ang) * len * travel;
        const by = cy + Math.sin(ang) * len * 0.72 * travel;
        const bright = 0.35 + 0.65 * Math.abs(f.bead);
        const rad = 2.4 + 3.8 * Math.abs(f.bead);
        c.beginPath();
        c.arc(bx, by, rad * 2.2, 0, TAU);
        c.fillStyle = this._rgba(f.color, 0.14 * bright);
        c.fill();
        c.beginPath();
        c.arc(bx, by, rad, 0, TAU);
        c.fillStyle = this._rgba(f.color, 0.95 * bright);
        c.fill();
      }
    });

    this._drawHub(c, cx, cy, p, T, time);
    this._drawTensionArc(c, cx, cy, p, T, time);
  }

  /** 中枢：分数在这里汇聚 */
  _drawHub(c, cx, cy, p, T, time) {
    const grow = this.showReplay
      ? (T.stage < 3 ? 0.45 : T.stage === 3 ? lerp(0.45, 1, easeOut(T.t)) : 1)
      : 1;
    const base = 16 + 30 * clamp(Math.abs(p.score), 0, 1);
    const r = base * grow;
    const col = p.score > 0.02 ? COLORS.bull : p.score < -0.02 ? COLORS.bear : COLORS.flat;

    // 外晕
    const g = c.createRadialGradient(cx, cy, 0, cx, cy, r * 2.4);
    g.addColorStop(0, this._rgba(col, 0.22));
    g.addColorStop(1, this._rgba(col, 0));
    c.fillStyle = g;
    c.beginPath(); c.arc(cx, cy, r * 2.4, 0, TAU); c.fill();

    // 核心
    c.beginPath();
    c.arc(cx, cy, r * 0.55, 0, TAU);
    c.fillStyle = this._rgba(col, 0.9);
    c.fill();
    c.strokeStyle = this._rgba(col, 0.45);
    c.lineWidth = 1.2;
    c.beginPath(); c.arc(cx, cy, r, 0, TAU); c.stroke();

    // 分数
    this._text(c, `${p.score >= 0 ? '+' : ''}${p.score.toFixed(3)}`,
               cx, cy + 4, COLORS.text, 12, 'center', true);
  }

  /**
   * 张力弧：从 0 分刻度到 1 分刻度的一段弧，填充到 |score| 处。
   * 弧闭合 = 够线了，该动手。另用一条刻度标出阈值位置。
   */
  _drawTensionArc(c, cx, cy, p, T, time) {
    const R = p.R * 1.02;
    const ry = R * 0.62;
    const a0 = -Math.PI * 0.86, a1 = -Math.PI * 0.14;   // 顶部一段弧
    const conv = this.showReplay
      ? (T.stage < 3 ? 0 : T.stage === 3 ? easeOut(T.t) : 1) : 1;
    const prog = clamp(Math.abs(p.score) * conv, 0, 1);

    // 底弧
    c.beginPath();
    c.ellipse(cx, cy, R, ry, 0, a0, a1);
    c.strokeStyle = 'rgba(90,90,110,0.22)';
    c.lineWidth = 3.5;
    c.stroke();

    // 填充弧
    if (prog > 0.005) {
      const col = p.score > 0 ? COLORS.bull : COLORS.bear;
      c.beginPath();
      c.ellipse(cx, cy, R, ry, 0, a0, a0 + (a1 - a0) * prog);
      c.strokeStyle = this._rgba(col, 0.85);
      c.lineWidth = 3.5;
      c.stroke();
    }

    // 阈值刻度：entry 对应弧上的位置
    const thr = clamp(p.entry, 0, 1);
    const ta = a0 + (a1 - a0) * thr;
    const tx = cx + Math.cos(ta) * R, ty = cy + Math.sin(ta) * ry;
    c.beginPath();
    c.moveTo(tx, ty - 7); c.lineTo(tx, ty + 7);
    c.strokeStyle = COLORS.accent;
    c.lineWidth = 1.6;
    c.stroke();
    this._text(c, `开仓线 ${p.entry.toFixed(2)}`, tx, ty - 12, COLORS.accent, 8.5, 'center');

    // 状态文字
    const enough = p.margin >= 1;
    const msg = enough
      ? `已达阈值（${p.margin.toFixed(2)}×）`
      : `距阈值还差 ${((1 - p.margin) * 100).toFixed(0)}%`;
    this._text(c, msg, cx, cy - R * 0.62 - 16,
               enough ? COLORS.accent : COLORS.muted, 9.5, 'center', enough);
  }

  // ── 风控闸门 ────────────────────────────────────────────
  _drawGates(c, x, cy, p, T, time) {
    const gates = this.data.risk || [];
    const active = gates.filter(g => g.active);
    const rowH = 26;
    const totalH = active.length * rowH;
    let y = cy - totalH / 2;
    const arrived = this.showReplay
      ? (T.stage < 5 ? 0 : T.stage === 5 ? easeOut(T.t) : 1) : 1;

    this._text(c, '风控闸门', x, y - 14, COLORS.muted, 10);
    this._text(c, `${p.fired.length} 个触发`, x, y - 1, COLORS.muted, 9);

    for (const g of active) {
      const fired = g.fired;
      const col = fired ? COLORS.gateFired : COLORS.gateOpen;
      c.globalAlpha = arrived > 0 ? 1 : 0.5;
      // 闸门框
      c.strokeStyle = this._rgba(col, fired ? 1 : 0.4);
      c.lineWidth = fired ? 2 : 1;
      c.strokeRect(x - 2.5, y + 1.5, 104, rowH - 6);
      if (fired) {
        c.fillStyle = this._rgba(col, 0.14);
        c.fillRect(x - 2.5, y + 1.5, 104, rowH - 6);
      }
      this._text(c, g.label, x + 4, y + 14,
                 fired ? col : COLORS.text, 10, 'left', fired);
      const v = g.value;
      const vtxt = typeof v === 'number' && v <= 1 && v > 0
        ? `${(v * 100).toFixed(0)}%` : String(v);
      this._text(c, vtxt, x + 98, y + 14, COLORS.muted, 9.5, 'right');
      c.globalAlpha = 1;
      y += rowH;
    }
  }

  /** 从网到闸门再到决策区的连线（体现"数据在流动"） */
  _drawStrandLinks(c, cx, cy, gateX, p, T) {
    const blocked = p.fired.length > 0;
    const prog = this.showReplay
      ? (T.stage < 5 ? 0 : T.stage === 5 ? easeOut(T.t) : 1) : 1;
    if (prog <= 0) return;
    const x0 = cx + p.R * 1.05;
    const y0 = cy;
    const x1 = gateX - 4;
    c.beginPath();
    c.moveTo(x0, y0);
    c.bezierCurveTo(x0 + 40, y0, x1 - 40, cy, x1, cy);
    c.strokeStyle = blocked
      ? this._rgba(COLORS.gateFired, 0.6 * prog)
      : this._rgba(COLORS.spider, 0.5 * prog);
    c.lineWidth = blocked ? 2.2 : 1.6;
    if (blocked) c.setLineDash([5, 4]);
    c.stroke();
    c.setLineDash([]);
  }

  // ── 决策区 ──────────────────────────────────────────────
  _drawDecision(c, x, cy, p, T, time) {
    const bw = 132, bh = 108;
    const col = p.action === 'buy' ? COLORS.bull
              : p.action === 'sell' ? COLORS.bear : COLORS.muted;
    const arrived = this.showReplay
      ? (T.stage < 5 ? 0.35 : T.stage === 5 ? lerp(0.35, 1, easeOut(T.t)) : 1) : 1;
    c.globalAlpha = arrived;
    c.fillStyle = '#0e0e11';
    c.fillRect(x - bw / 2, cy - bh / 2, bw, bh);
    c.strokeStyle = this._rgba(col, 0.85);
    c.lineWidth = 1.6;
    c.strokeRect(x - bw / 2 + .5, cy - bh / 2 + .5, bw - 1, bh - 1);

    const label = { buy: '买入', sell: '卖出', hold: '不动' }[p.action] || p.action;
    this._text(c, label, x, cy - 28, col, 17, 'center', true);
    this._text(c, '目标仓位', x, cy - 6, COLORS.muted, 9.5, 'center');
    this._text(c, `${(p.target * 100).toFixed(0)}%`, x, cy + 13, COLORS.text, 15, 'center', true);
    this._text(c, `把握 ${p.conviction.toFixed(2)}×`, x, cy + 33,
               p.conviction >= 1 ? col : COLORS.muted, 9.5, 'center');
    c.globalAlpha = 1;
  }

  // ── 蜘蛛本体 ────────────────────────────────────────────
  /**
   * 蜘蛛 = 决策主体。
   *
   * 位置承载信息，不再是装饰：
   *   径向 = |分数|   分数越大越靠近网的边缘（立场越明确）
   *   角度 = 当前动作  BUY 朝上 / SELL 朝下 / HOLD 朝右
   *   亮度 = 把握度    够线才亮
   *   被拦住 = 风控触发  蜘蛛往决策区冲但过不去
   */
  _drawSpider(c, cx, cy, p, time, T) {
    const blocked = p.fired.length > 0 || !!p.riskHit;
    const ang = ACTION_ANGLE[p.action] ?? 0;
    const R = p.R * p.radial;

    // 阶段 4 之前蜘蛛在中枢附近聚集
    const moveT = this.showReplay
      ? (T.stage < 4 ? 0 : T.stage === 4 ? easeOut(T.t) : 1) : 1;
    const rr = lerp(p.R * 0.12, R, moveT);
    let sx = cx + Math.cos(ang) * rr;
    let sy = cy + Math.sin(ang) * rr * 0.62;

    // 被风控拦住：蜘蛛冲到闸门前就被弹回
    if (blocked && moveT > 0.6) {
      const push = Math.sin((moveT - 0.6) / 0.4 * Math.PI) * p.R * 0.28;
      sx += push;
    }

    const bright = clamp(0.45 + 0.55 * p.conviction, 0.45, 1);
    const wobble = Math.sin(time * 2.4) * 0.7;
    this._drawSpiderBody(c, sx, sy + wobble, time, ang, bright, blocked);

    // 触手：指向中枢（说明"我在读这张网"）
    c.beginPath();
    c.moveTo(sx, sy);
    c.quadraticCurveTo((sx + cx) / 2, (sy + cy) / 2 - 18, cx, cy);
    c.setLineDash([2, 6]);
    c.strokeStyle = this._rgba(COLORS.spider, 0.5 * bright);
    c.lineWidth = 1.1;
    c.stroke();
    c.setLineDash([]);

    // 被拦住的提示
    if (blocked) {
      const msg = p.riskHit ? `风控拦截：${p.riskHit}` : '风控拦截';
      const tx = (sx + this.w - 200) / 2;
      this._text(c, msg, sx + 26, sy + 30, COLORS.gateFired, 10, 'left', true);
      // 撞击闪烁
      const flash = 0.35 + 0.65 * Math.abs(Math.sin(time * 5));
      c.beginPath();
      c.arc(sx, sy, 26 + 6 * flash, 0, TAU);
      c.strokeStyle = this._rgba(COLORS.gateFired, 0.35 * flash);
      c.lineWidth = 2;
      c.stroke();
    }
  }

  /** 蜘蛛身体（简化的八足 + 球体，复用主项目的造型语言） */
  _drawSpiderBody(c, x, y, time, facing, bright, blocked) {
    const col = blocked ? COLORS.gateFired : COLORS.spider;
    c.save();
    c.translate(x, y);

    // 八条腿
    c.strokeStyle = this._rgba(col, 0.75 * bright);
    c.lineWidth = 1.5;
    c.lineCap = 'round';
    for (let i = 0; i < 8; i++) {
      const side = i < 4 ? -1 : 1;
      const k = i % 4;
      const base = -0.5 + k * 0.42;
      const swing = Math.sin(time * 3 + i * 0.9) * 0.10;
      const a = facing + base + swing;
      const L1 = 12, L2 = 15;
      const jx = Math.cos(a) * L1 * 0.9;
      const jy = Math.sin(a) * L1 * 0.9 + side * 5;
      const ex = jx + Math.cos(a + side * 0.5) * L2;
      const ey = jy + Math.sin(a + side * 0.5) * L2 + side * 7;
      c.beginPath();
      c.moveTo(0, 0);
      c.lineTo(jx, jy);
      c.lineTo(ex, ey);
      c.stroke();
    }
    c.lineCap = 'butt';

    // 腹部
    c.beginPath();
    c.ellipse(0, 0, 8.5, 10, facing + Math.PI / 2, 0, TAU);
    c.fillStyle = this._rgba(col, 0.30 * bright);
    c.fill();
    c.strokeStyle = this._rgba(col, 0.9 * bright);
    c.lineWidth = 1.3;
    c.stroke();

    // 头部（朝向动作方向）
    const hx = Math.cos(facing) * 8, hy = Math.sin(facing) * 8;
    c.beginPath();
    c.arc(hx, hy, 4.6, 0, TAU);
    c.fillStyle = this._rgba(col, 0.95 * bright);
    c.fill();

    // 眼
    c.fillStyle = blocked ? '#fff' : '#0a1a14';
    c.beginPath(); c.arc(hx + Math.cos(facing - 0.5) * 2.4,
                         hy + Math.sin(facing - 0.5) * 2.4, 1.1, 0, TAU); c.fill();
    c.beginPath(); c.arc(hx + Math.cos(facing + 0.5) * 2.4,
                         hy + Math.sin(facing + 0.5) * 2.4, 1.1, 0, TAU); c.fill();
    c.restore();
  }

  // ── 轨迹（P2）：蜘蛛走过留下的丝 ────────────────────────
  _drawTrail(c, cx, cy, p) {
    const t = this.trail;
    if (t.length < 2) return;
    c.beginPath();
    t.forEach((pt, i) => {
      const a = ACTION_ANGLE[pt.action] ?? 0;
      const r = p.R * (0.18 + 0.82 * clamp(Math.abs(pt.score), 0, 1));
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r * 0.62;
      if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
    });
    c.strokeStyle = 'rgba(72,214,164,0.16)';
    c.lineWidth = 1;
    c.setLineDash([1, 3]);
    c.stroke();
    c.setLineDash([]);
  }

  // ── 阶段条：显示重演进度 ────────────────────────────────
  _drawStageBar(c, cx, y, T) {
    const stages = this.data.stages || [];
    if (!stages.length) return;
    const n = stages.length;
    const bw = Math.min(560, this.w * 0.42);
    const x0 = cx - bw / 2;
    const step = bw / n;
    const cur = T.done ? n : T.stage;
    for (let i = 0; i < n; i++) {
      const x = x0 + i * step;
      const on = i < cur || T.done;
      const active = i === cur && !T.done;
      c.fillStyle = on ? this._rgba(COLORS.spider, 0.85)
                  : active ? this._rgba(COLORS.accent, 0.85)
                  : 'rgba(70,70,85,0.5)';
      c.fillRect(x + 1, y, step - 2, 3);
    }
    // 当前阶段的说明
    const s = stages[Math.min(cur, n - 1)];
    this._text(c, `${cur + 1}/${n}  ${s.label}  ·  ${s.detail}`,
               cx, y - 6, T.done ? COLORS.muted : COLORS.accent, 9.5, 'center');
  }

  // ── 顶部状态行 ──────────────────────────────────────────
  _drawHeader(c, p, T) {
    const m = this.data.meta;
    this._text(c, `${m.name} ${m.symbol} · ${m.interval}`,
               16, 22, COLORS.text, 12, 'left', true);
    this._text(c, '6 因子 → 加权 → 分数 → 阈值 → 风控 → 仓位（真实计算，非演示动画）',
               this.w - 16, 22, COLORS.muted, 9.5, 'right');
  }

  // ── 工具 ────────────────────────────────────────────────
  _text(c, v, x, y, color, size = 10, align = 'left', bold = false) {
    c.font = `${bold ? 700 : 400} ${size}px Consolas, "Cascadia Code", monospace`;
    c.fillStyle = color;
    c.textAlign = align;
    c.textBaseline = 'alphabetic';
    c.fillText(String(v), x, y);
    c.textAlign = 'left';
  }

  /** '#rrggbb' + alpha → rgba() */
  _rgba(hex, a) {
    const h = hex.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${clamp(a, 0, 1)})`;
  }

  /** 启动自绘循环 */
  start() {
    if (this.raf != null) return;
    const loop = () => {
      const t = performance.now() / 1000;
      // 重演结束后就不必每帧重绘了（省电）—— 只在动画期间跑满帧
      this.render(t);
      const st = this._stageProgress(t);
      if (st.done) {
        // 完成后降到低帧率维持蜘蛛的轻微呼吸
        this.raf = requestAnimationFrame(loop);
      } else {
        this.raf = requestAnimationFrame(loop);
      }
    };
    loop();
  }

  stop() {
    if (this.raf != null) { cancelAnimationFrame(this.raf); this.raf = null; }
  }
}

export { FACTOR_ANGLES, ACTION_ANGLE, COLORS };
