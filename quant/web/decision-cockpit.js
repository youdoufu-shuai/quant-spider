/**
 * 决策驾驶舱 —— 回答四个问题，不做装饰。
 *
 * ═══════════════════════════════════════════════════════════════
 *  为什么重做（上一版被评价为「意义不明」，是准确的）
 * ═══════════════════════════════════════════════════════════════
 * 上一版画了一张「蛛网」：六根丝从中心放射，蜘蛛趴在中枢。
 * 五个问题：
 *
 *   1. 没有结论
 *      最重要的问题「该做什么」答案在最右边一个小框里（不动 / 0%），
 *      而 60% 的屏幕给了一张抽象网。眼睛先看到的是装饰。
 *
 *   2. 蛛网不表达数学
 *      六个因子是**相加**的，却画成从中心**放射**。隐喻本身就是错的。
 *
 *   3. 蜘蛛位置没含义
 *      蜘蛛离中心近，不是因为"没方向"，只是因为分数小。
 *      这个编码要解释才懂，不直观 = 没价值。
 *
 *   4. 「已达标（1.74×）」有歧义
 *      分数 -0.4357、动作「不动」，但张力弧是满的、写着"已达标"。
 *      达标了怎么还不动？因为达标的是**做空**方向，而策略只做多。
 *      页面上完全没解释这件事。
 *
 *   5. 首屏只有一张网
 *      左边一列字和下方表格完全重复，右边小框。眼睛没有落点。
 *
 * ═══════════════════════════════════════════════════════════════
 *  现在画什么
 * ═══════════════════════════════════════════════════════════════
 * 按用户真正会问的顺序，四个问题：
 *
 *   ① 结论是什么？        → 大号结论 + 一句话原因（后端 why 字段）
 *   ② 为什么？            → 贡献瀑布：每根条的长度 = 对分数的实际贡献
 *   ③ 什么情况下会变？    → 离阈值还差多少，换算成"主导因子还要动几档"
 *   ④ 什么会否决它？      → 风控闸门
 *
 * ③ 是最有行动价值的一节，也是原来完全没有的。
 * 它回答的是「我现在该盯着什么」—— 比如
 *   「超买超卖再涨 0.45 档就会开仓」
 * 比一个 -0.4357 有用得多。
 *
 * 配色：亮色主题（白底），红=看多、绿=看空（A 股习惯）。
 */

const TAU = Math.PI * 2;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const easeOut = t => 1 - Math.pow(1 - t, 3);

const C = {
  // 亮色主题：canvas 底色是白卡片，文字/线条都要在白底上够深
  bg: '#ffffff', panel: '#ffffff', border: '#e6e6e3',
  muted: '#7c848a', text: '#171a1e', dim: '#a9afb4',
  bull: '#a02a24',      // 看多（酒红）
  bear: '#2e6152',      // 看空（松绿）
  flat: '#a9afb4',
  accent: '#171a1e',    // 阈值线用墨色 —— 红色是数据，不能拿来当标尺
  ok: '#2e6152',
  warn: '#7a5c1e',
  rule: 'rgba(23,26,30,.16)',   // 零线/合计线
};

// 动画阶段（秒）。总长约 2.6s，比上一版的 4s 短 ——
// 结论应该立刻可见，动画只是辅助理解。
const STAGE = 0.35;
const NSTAGE = 6;


export class DecisionCockpit {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.w = 0; this.h = 0;
    this.data = null;
    this.prep = null;
    this._key = null;
    this.startAt = null;
    this.raf = null;
    this.showReplay = true;
  }

  resize() {
    const dpr = Math.min(2, Math.max(1, globalThis.devicePixelRatio || 1));
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const w = Math.max(420, Math.floor(rect.width) - 2);
    // 高度按内容算，不能写死。
    // ⚠️ 第一版写死 430px，但六行贡献 + 六行闸门要 ~300px 的内容
    //    加上标题与合计线，结果文本压成一团（实测截图确认）。
    // 行数随标的变（指标未就绪的因子不画），所以动态算。
    const nContrib = this.prep ? this.prep.contrib.rows.length : 6;
    const nGate = this.prep ? this.prep.gate.rows.length : 6;
    const TITLE_H = 4;                           // 不画标题了，只留一点上边距
    const contribH = TITLE_H + nContrib * 30 + 26;   // 标题 + 行 + 合计线
    const gateH = TITLE_H + nGate * 24 + 24;
    const h = Math.max(160, Math.round(10 + Math.max(contribH, gateH) + 10));

    if (this.canvas.width === Math.round(w * dpr) &&
        this.canvas.height === Math.round(h * dpr)) return false;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
    return true;
  }

  /**
   * 灌入数据。返回是否是新数据（决定要不要重放动画）。
   *
   * ⚠️ 这里**不**调 _prepare —— 布局要用 this.w，
   *    而 setData 通常在 canvas 还没量过尺寸时被调用（this.w 还是 0）。
   *    布局统一在 render() 里算，那时 resize() 已经跑过。
   */
  setData(d) {
    if (!d || d.error) { this.data = null; this.prep = null; this._key = null; return false; }
    const key = `${d.meta?.symbol}|${d.meta?.at}|${d.decision?.score}|${d.assumption?.position}`;
    if (this._key === key) { this.data = d; return false; }
    this._key = key;
    this.data = d;
    return true;
  }

  replay() { this.startAt = null; }

  /**
   * 把所有几何与数值预先算好。
   * 渲染函数里不该再出现魔法数字 —— 这样调布局只改这一处。
   *
   * ⚠️ 这里**不画结论区**。结论已经由页面上的 HTML 卡片负责
   *    （`#fallback`），因为：
   *      · HTML 文字更清晰、可选中、可访问
   *      · canvas 出问题时答案仍然可见
   *      · 第一版两边都画，结果重复且重叠（实测截图确认）
   *    canvas 只负责 HTML 做不好的部分：贡献瀑布 + 闸门状态。
   */
  _prepare(d) {
    const padX = 18;
    const w = this.w;
    const topY = 10;

    // ── 贡献瀑布 ──
    // 每根条的长度 = 该因子对分数的**实际贡献**（v×w ÷ 总权重）。
    // 用「占最大贡献的比例」定长度，这样最长的总是满格，好比较。
    const present = (d.factors || []).filter(f => f.present);
    const share = f => (f.contrib || 0) / (d.score?.totalWeight || 1);
    const maxAbs = Math.max(...present.map(f => Math.abs(share(f))), 0.001);
    const sorted = [...present].sort((a, b) => Math.abs(share(b)) - Math.abs(share(a)));

    // 瀑布占 56%，留够间距给右边的风控闸门 —— 62% 时两者会挤在一起
    const colW = (w - padX * 2) * 0.56;
    const gap = 56;
    const rowH = 30;
    const TITLE_H = 4;                        // 不画标题了，只留上边距
    const contrib = {
      x: padX, y: topY, w: colW, rowH,
      rows: sorted.map((f, i) => {
        const s = share(f);
        return {
          ...f, share: s,
          norm: Math.abs(s) / maxAbs,
          y: topY + TITLE_H + i * rowH,
          color: s > 0.002 ? C.bull : s < -0.002 ? C.bear : C.flat,
        };
      }),
    };

    // ── 风控闸门（右侧） ──
    const active = (d.risk || []).filter(g => g.active);
    const gate = {
      x: padX + colW + gap,
      y: topY,
      w: w - padX - (padX + colW + gap),
      rows: active.map((g, i) => ({
        ...g, y: topY + TITLE_H + i * 24,
        color: g.fired ? C.bull : C.ok,
      })),
    };

    return {
      contrib, gate,
      score: d.score?.clamped ?? 0,
      totalWeight: d.score?.totalWeight ?? 1,
      rawSum: d.score?.raw ?? 0,
      dominant: d.dominant,
    };
  }

  /** 当前动画阶段。返回 { stage, t, done } */
  _prog(time) {
    if (!this.showReplay) return { stage: 99, t: 1, done: true };
    if (this.startAt == null) this.startAt = time;
    const el = time - this.startAt;
    const total = STAGE * NSTAGE;
    if (el >= total) return { stage: 99, t: 1, done: true };
    const i = Math.floor(el / STAGE);
    return { stage: i, t: (el - i * STAGE) / STAGE, done: false };
  }

  render(time) {
    this.resize();
    const c = this.ctx, w = this.w, h = this.h;
    c.fillStyle = C.bg;
    c.fillRect(0, 0, w, h);

    if (!this.data) {
      c.fillStyle = C.muted;
      c.font = '11px ';
      c.fillText('等待决策数据…', 18, 30);
      return;
    }

    // ⚠️ 顺序要紧：先 _prepare（它按实际行数算布局），
    //    再 resize（它读 prep 决定 canvas 高度），最后画。
    //    第一版是先 resize 再 prepare，导致高度用了错误的行数。
    const T = this._prog(time);
    this.prep = this._prepare(this.data);
    if (this.resize()) {
      c.fillStyle = C.bg;
      c.fillRect(0, 0, this.w, this.h);       // 高度变了要重铺底色
    }
    const p = this.prep;
    const W = this.w;
    void w; void h;

    // 结论区不在这里画 —— HTML 卡片负责（见 _prepare 的说明）
    this._drawContrib(c, p, T, W);
    this._drawGates(c, p, T);
  }

  // ══════════════════════════════════════════════════════════
  //  贡献瀑布 —— 为什么是这个分数
  // ══════════════════════════════════════════════════════════
  _drawContrib(c, p, T, W) {
    const g = p.contrib;
    // 不画标题 —— 外层 HTML 区块已经写了「为什么是这个结论」。
    // 画布里重复一遍，等于同一句话说两遍，还白占一行高度。

    // 零线：所有条从这里向左右长
    const labelW = 74, valW = 104;
    const barX0 = g.x + labelW;
    const barMax = Math.max(60, g.w - labelW - valW);
    const zero = barX0 + barMax * 0.5;

    c.strokeStyle = C.rule;
    c.setLineDash([2, 4]);
    c.beginPath();
    const rowTop = g.rows.length ? g.rows[0].y : g.y + 22;
    c.moveTo(zero, rowTop);
    c.lineTo(zero, g.rows.length ? g.rows[g.rows.length - 1].y + 24 : g.y + 60);
    c.stroke();
    c.setLineDash([]);

    g.rows.forEach((r, i) => {
      const grow = this.showReplay
        ? clamp(((T.stage === 0 ? 0 : T.stage) + T.t) - i * 0.10, 0, 1)
        : 1;
      if (grow <= 0) return;
      const len = r.norm * (barMax * 0.48) * easeOut(grow);
      const bh = 13;
      const y = r.y + 5;

      this._text(c, r.label, g.x, y + 10, C.text, 11);

      const bx = r.share >= 0 ? zero : zero - len;
      c.fillStyle = r.color;
      c.globalAlpha = 0.85;
      c.fillRect(bx, y, Math.max(1.5, len), bh);
      c.globalAlpha = 1;
      if (len > 3) {
        c.fillStyle = r.color;
        c.fillRect(r.share >= 0 ? bx + len - 2 : bx, y, 2, bh);
      }

      const vx = g.x + g.w;
      this._text(c, `${r.share >= 0 ? '+' : ''}${r.share.toFixed(4)}`,
                 vx - 46, y + 10, r.color, 11, 'right', true);
      this._text(c, `w${r.weight.toFixed(2)}`, vx, y + 10, C.dim, 9.5, 'right');
    });

    // 合计
    const sumY = (g.rows.length ? g.rows[g.rows.length - 1].y + 30 : g.y + 46) + 4;
    c.strokeStyle = C.rule;
    c.beginPath(); c.moveTo(g.x, sumY); c.lineTo(g.x + g.w, sumY); c.stroke();
    this._text(c, '加权和 ÷ 总权重 =', g.x, sumY + 16, C.muted, 10.5);
    this._text(c, `${p.rawSum >= 0 ? '+' : ''}${p.rawSum.toFixed(3)} ÷ ${p.totalWeight.toFixed(2)}`,
               g.x + 158, sumY + 16, C.muted, 10.5);
    this._text(c, `= ${p.score >= 0 ? '+' : ''}${p.score.toFixed(4)}`,
               g.x + g.w, sumY + 16,
               p.score > 0.002 ? C.bull : p.score < -0.002 ? C.bear : C.muted,
               12.5, 'right', true);
  }

  // ══════════════════════════════════════════════════════════
  //  ④ 风控闸门
  // ══════════════════════════════════════════════════════════
  _drawGates(c, p, T) {
    const g = p.gate;
    if (g.w < 120) return;                    // 太窄就不画，免得挤压瀑布
    const appear = this.showReplay
      ? clamp(((T.stage < 3 ? 0 : T.stage - 3) + T.t) * 1.5, 0, 1) : 1;
    if (appear <= 0.01) return;
    c.globalAlpha = appear;

    // 同样不画标题（外层区块已经写了「风控闸门」）
    const fired = g.rows.filter(r => r.fired).length;

    g.rows.forEach(r => {
      const col = r.fired ? C.bull : C.ok;
      c.fillStyle = col;
      c.fillRect(g.x, r.y + 4, 2.5, 14);
      this._text(c, r.label, g.x + 10, r.y + 15, r.fired ? col : C.text, 11);
      const vtxt = typeof r.value === 'number' && r.value > 0 && r.value <= 1
        ? `${(r.value * 100).toFixed(0)}%` : String(r.value ?? '—');
      this._text(c, vtxt, g.x + g.w, r.y + 15, C.muted, 10.5, 'right');
    });

    const noteY = (g.rows.length ? g.rows[g.rows.length - 1].y + 26 : g.y + 40) + 8;
    this._text(c, '风控优先于信号：触发时强制平仓，不看分数',
               g.x, noteY, C.muted, 9.5);
    c.globalAlpha = 1;
  }

  // ── 工具 ──────────────────────────────────────────────
  _text(c, v, x, y, color, size = 10, align = 'left', bold = false) {
    c.font = `${bold ? 700 : 400} ${size}px `;
    c.fillStyle = color;
    c.textAlign = align;
    c.textBaseline = 'alphabetic';
    c.fillText(String(v), x, y);
    c.textAlign = 'left';
  }

  _width(c, v, size, bold) {
    c.font = `${bold ? 700 : 400} ${size}px `;
    return c.measureText(String(v)).width;
  }

  start() {
    if (this.raf != null) return;
    const loop = () => { this.render(performance.now() / 1000); this.raf = requestAnimationFrame(loop); };
    loop();
  }

  stop() { if (this.raf != null) { cancelAnimationFrame(this.raf); this.raf = null; } }
}

export { C as COCKPIT_COLORS };
