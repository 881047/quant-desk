// K 线引擎 —— 手写 Canvas，不引图表库。
//
// 为什么不用现成的：这张图要回答的是"这根 K 线和那根之间发生了什么"，
// 需要的是可核对的坐标轴、真实成交额、复权切换与十字光标逐根读数；
// 通用图表库把这些放在配置项后面，而配置项比手写更容易说谎。
//
// 颜色全部从 CSS 变量读一次再缓存（tokens.css 是唯一来源），
// 所以主题只有一份定义；每帧 13 次 getComputedStyle 会强制样式重算，那是拖拽掉帧的主因。
//
// 两层画布：static 层（网格/蜡烛/均线/量/副图/坐标轴）画到离屏 canvas，
// 只有 hover 层（十字光标 + 图例）每帧重画。250 根上限下这是必要的最小改动。

import { sma, boll, macd, kdj, rsi, rangeStats } from './indicators.js';
import { price as fprice, cny, pct, md } from './fmt.js';

const MA_DEFS = [
  { n: 5, key: 'ma5', label: 'MA5' },
  { n: 10, key: 'ma10', label: 'MA10' },
  { n: 20, key: 'ma20', label: 'MA20' },
  { n: 60, key: 'ma60', label: 'MA60' },
];

const FONT_MICRO = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
const FONT_BODY = '11px ui-monospace, SFMono-Regular, Menlo, monospace';

function cssVar(el, name, fallback) {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || fallback;
}

/** 一次读全，之后只查表。theme 只在 resize / 主题变化时失效。 */
function readTheme(el) {
  const g = (n, f) => cssVar(el, n, f);
  return {
    bg: g('--bg-panel', '#12151A'),
    page: g('--bg-page', '#0B0D10'),
    grid: g('--chart-grid', '#171C23'),
    axis: g('--chart-axis', '#1A2027'),
    divider: g('--chart-divider', '#2E3742'),
    label: g('--chart-label', '#74808F'),
    text: g('--ink-mid', '#AEB7C4'),
    up: g('--px-up', '#FF5A63'),
    down: g('--px-down', '#21C97C'),
    flat: g('--px-flat', '#8A93A3'),
    acc: g('--accent', '#58A6FF'),
    ma5: g('--ma-5', '#E8EDF5'),
    ma10: g('--ma-10', '#FFC24B'),
    ma20: g('--ma-20', '#58A6FF'),
    ma60: g('--ma-60', '#9AA7B8'),
    cross: g('--chart-cross', '#6E7A8C'),
    volUp: g('--vol-up', 'rgba(255,90,99,.42)'),
    volDown: g('--vol-down', 'rgba(33,201,124,.42)'),
    macdUp: g('--macd-up', 'rgba(255,90,99,.55)'),
    macdDown: g('--macd-down', 'rgba(33,201,124,.55)'),
    bollLine: g('--boll-line', '#5C6B80'),
    bollFill: g('--boll-fill', 'rgba(92,107,128,.10)'),
    onSolid: g('--on-solid', '#0B0D10'),
    crossBg: g('--chart-cross-bg', '#0B0D10'),
    strong: g('--line-strong', '#37414F'),
    up2: g('--px-up', '#FF5A63'),
    down2: g('--px-down', '#21C97C'),
    live: g('--signal-live', '#3ED3B8'),
  };
}

/** 图表几何：宽态 6+228+1+58+1+66+20 = 380；窄态 6+295+1+58+20 = 380。
    只有这一处定义，别在 layout 里散算数字。 */
const GEO = { top: 6, main: 228, sep: 1, vol: 58, sub: 66, axis: 20 };

export class KLineChart {
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = { onHover: null, onRange: null, ...opts };
    this.bars = [];
    this.start = 0;
    this.end = 0;
    this.indicator = 'MACD';
    this.overlays = { ma: true, boll: false, vol: true };
    this.hover = null;
    this.drag = null;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this._dirty = true;
    this._themeDirty = true;

    this.canvas = document.createElement('canvas');
    this.canvas.className = 'kline-canvas';
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('role', 'img');
    this.host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this.off = document.createElement('canvas');
    this.octx = this.off.getContext('2d');

    this._onWheel = this._wheel.bind(this);
    this._onDown = this._down.bind(this);
    this._onMove = this._move.bind(this);
    this._onUp = this._up.bind(this);
    this._onLeave = this._leave.bind(this);
    this._onDbl = this._dbl.bind(this);
    this._onKey = this._key.bind(this);

    this.canvas.addEventListener('wheel', this._onWheel, { passive: false });
    this.canvas.addEventListener('pointerdown', this._onDown);
    this.canvas.addEventListener('pointermove', this._onMove);
    this.canvas.addEventListener('pointerup', this._onUp);
    this.canvas.addEventListener('pointercancel', this._onUp);
    this.canvas.addEventListener('pointerleave', this._onLeave);
    this.canvas.addEventListener('dblclick', this._onDbl);
    this.canvas.addEventListener('keydown', this._onKey);

    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.host);
  }

  destroy() {
    this.ro.disconnect();
    this.canvas.removeEventListener('wheel', this._onWheel);
    this.canvas.removeEventListener('pointerdown', this._onDown);
    this.canvas.removeEventListener('pointermove', this._onMove);
    this.canvas.removeEventListener('pointerup', this._onUp);
    this.canvas.removeEventListener('pointercancel', this._onUp);
    this.canvas.removeEventListener('pointerleave', this._onLeave);
    this.canvas.removeEventListener('dblclick', this._onDbl);
    this.canvas.removeEventListener('keydown', this._onKey);
    this.bars = [];
    this._ser = null;
    this.canvas.remove();
  }

  setData(bars, { keepView = false } = {}) {
    this.bars = Array.isArray(bars) ? bars : [];
    this.hover = null;                     // 数据换了，旧索引不再有意义
    this._ser = null;
    this._dirty = true;
    if (!keepView || !this.bars.length) this.resetView();
    else this._clampView();
    this.draw();
  }

  setIndicator(name) {
    this.indicator = name;
    this._ser = null;
    this._dirty = true;
    this.draw();
  }

  setOverlays(patch) {
    Object.assign(this.overlays, patch);
    this._ser = null;
    this._dirty = true;
    this.draw();
  }

  resetView() {
    const n = this.bars.length;
    const want = Math.min(n, 120);
    this.start = Math.max(0, n - want);
    this.end = Math.max(0, n - 1);
    this._emitRange();
  }

  /** 任何改变 start/end 的路径都要过这里：end 永远落在 [start, len-1]。 */
  _clampView() {
    const n = this.bars.length;
    if (!n) { this.start = 0; this.end = 0; return; }
    this.end = Math.max(0, Math.min(this.end, n - 1));
    this.start = Math.max(0, Math.min(this.start, this.end));
    if (this.hover && this.hover.index > this.end) this.hover = null;
  }

  resize() {
    const rect = this.host.getBoundingClientRect();
    this.w = Math.max(240, Math.floor(rect.width));
    this.h = Math.max(200, Math.floor(rect.height));
    this.canvas.width = Math.floor(this.w * this.dpr);
    this.canvas.height = Math.floor(this.h * this.dpr);
    this.canvas.style.width = `${this.w}px`;
    this.canvas.style.height = `${this.h}px`;
    this.off.width = this.canvas.width;
    this.off.height = this.canvas.height;
    this._themeDirty = true;
    this._dirty = true;
    this.draw();
  }

  // ---------------------------------------------------------------- geometry
  /** 右侧价格区：58px 起，三位数以上的价格给 64px。
      永不换行、永不省略号 —— 价格不能显示成 "12…"。 */
  _gutter() {
    // 宽度是全局参数：按**整段序列**算一次，绑定在 bars 上。
    // 按可见区间算会让轴在 58/64 之间抖动，蜡烛整体横向位移——缩放时最刺眼的那种跳。
    if (this._gutFor === this.bars && this._gut != null) return this._gut;
    let widest = 0;
    for (const b of this.bars) {
      const s = this._fmtPrice(b.c);
      if (s.length > widest) widest = s.length;
    }
    this._gutFor = this.bars;
    this._gut = widest > 6 ? 64 : 58;
    return this._gut;
  }

  get pad() {
    // 有效绘图宽 < 880 时收掉副图（README 里的"窄态"）。
    const withInd = this.indicator && this.indicator !== 'NONE' && this.w >= 880;
    return { l: 10, r: this._gutter(), t: GEO.top, b: GEO.axis, gap: GEO.sep, withInd };
  }

  layout() {
    // 定值分配，不按比例。两条理由：
    //   1. 主图是这张图的全部用途，200px 下 ATR 自适应刻度里的蜡烛实体只剩 1–2px；
    //   2. 比例分配下"关副图"会让主图与量**同时**变大，pitch 重算、蜡烛整体横移。
    // 几何常量在 GEO 里只有一份，下面按"总高 380"校验，不散算。
    const p = this.pad;
    const plotW = Math.max(40, this.w - p.l - p.r);
    const bodyH = this.h - p.t - p.b;
    const n = Math.max(1, this.end - this.start + 1);
    const gap = plotW / n;
    const volH = this.overlays.vol ? GEO.vol : 0;
    const indH = p.withInd ? GEO.sub : 0;
    // 高度不够时只压主图，量/副图给最小值 —— 压量的后果是"看不到量"，压主图是"蜡烛变矮"，后者可读。
    const fixed = p.t + p.b + volH + indH + (volH ? GEO.sep : 0) + (indH ? GEO.sep : 0);
    const mainH = Math.max(48, this.h - fixed);
    const main = { x: p.l, y: p.t, w: plotW, h: mainH };
    let y = p.t + mainH + (volH ? GEO.sep : 0);
    const vol = volH ? { x: p.l, y, w: plotW, h: volH } : null;
    y += volH + (indH ? GEO.sep : 0);
    const ind = indH ? { x: p.l, y, w: plotW, h: indH } : null;
    return { plotW, gap, n, main, vol, ind, r: p.r, narrow: !p.withInd };
  }

  visible() { return this.bars.slice(this.start, this.end + 1); }

  _emitRange() {
    if (!this.opts.onRange) return;
    const stats = rangeStats(this.bars, this.start, this.end);
    this.opts.onRange(stats);
  }

  // ---------------------------------------------------------------- 指标缓存
  /** 指标只随 (bars, indicator, overlays) 变化，与 hover/滚动无关。 */
  _series() {
    if (this._ser && this._serBars === this.bars
      && this._serInd === this.indicator
      && this._serMa === this.overlays.ma
      && this._serBoll === this.overlays.boll) {
      return this._ser;
    }
    const closes = this.bars.map((b) => b.c);
    const highs = this.bars.map((b) => b.h);
    const lows = this.bars.map((b) => b.l);
    const s = { closes, highs, lows };
    if (this.overlays.ma) for (const def of MA_DEFS) s[def.key] = sma(closes, def.n);
    if (this.overlays.boll) {
      const b = boll(closes, 20, 2);
      s.bollMid = b.mid; s.bollUp = b.upper; s.bollLow = b.lower;
    }
    if (this.indicator === 'MACD') s.macd = macd(closes);
    if (this.indicator === 'KDJ') s.kdj = kdj(highs, lows, closes);
    if (this.indicator === 'RSI') { s.rsi6 = rsi(closes, 6); s.rsi12 = rsi(closes, 12); s.rsi24 = rsi(closes, 24); }
    Object.assign(this, { _ser: s, _serBars: this.bars, _serInd: this.indicator, _serMa: this.overlays.ma, _serBoll: this.overlays.boll });
    return s;
  }

  // ---------------------------------------------------------------- drawing
  draw() {
    if (!this.w || !this.h) return;
    if (this._themeDirty) { this.col = readTheme(this.host); this._themeDirty = false; this._dirty = true; }
    const col = this.col;
    if (this._dirty || this._serBars !== this.bars) { this._renderStatic(col); this._dirty = false; }

    const { ctx } = this;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    if (!this.bars.length) {
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.fillStyle = col.text;
      ctx.font = FONT_BODY;
      ctx.fillText('没有 K 线', 16, 28);
      return;
    }
    ctx.drawImage(this.off, 0, 0);
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    if (this.hover) {
      this._legend(this.L, col);
      this._crosshair(col);
    }
  }

  _renderStatic(col) {
    const octx = this.octx;
    const { w, h } = this;
    octx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    octx.clearRect(0, 0, w, h);
    octx.fillStyle = col.bg;
    octx.fillRect(0, 0, w, h);
    if (!this.bars.length) {
      octx.fillStyle = col.text;
      octx.font = FONT_BODY;
      octx.fillText('没有 K 线', 16, 28);
      this._dirty = false;
      return;
    }

    this._clampView();
    const L = this.layout();
    // 有效绘图宽 < 880 的四件事共用这一个开关：关副图、均线只留 MA5/MA20、
    // 刻度 6 档降 3 档、指标叠加关闭。layout() 谁写的都要有这个字段。
    if (L.narrow === undefined) L.narrow = L.plotW < 880;
    const s = this._series();
    const closes = s.closes;

    let lo = Infinity;
    let hi = -Infinity;
    for (let i = this.start; i <= this.end; i += 1) {
      lo = Math.min(lo, this.bars[i].l);
      hi = Math.max(hi, this.bars[i].h);
      for (const def of MA_DEFS) {
        const v = s[def.key]?.[i];
        if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
      for (const k of ['bollUp', 'bollLow']) {
        const v = s[k]?.[i];
        if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return;
    const padY = (hi - lo) * 0.08 || hi * 0.02 || 1;
    lo -= padY; hi += padY;
    const yOf = (v) => L.main.y + L.main.h - ((v - lo) / (hi - lo)) * L.main.h;
    const xCenter = (i) => L.main.x + (i - this.start) * L.gap + L.gap / 2;
    Object.assign(this, { yOf, xCenter, lo, hi, L, gap: L.gap });

    this._grid(octx, L, col, lo, hi, yOf);
    this._candles(octx, L, col, xCenter, yOf);
    this._overlays(octx, col, s, xCenter, yOf);
    if (L.vol) this._volume(octx, L.vol, col, xCenter);
    if (L.ind) this._indicatorPane(octx, L.ind, col, s, xCenter);
    this._separators(octx, L, col);
    this._timeAxis(octx, L, col, xCenter);

    // 最新价标签（静态层，不随 hover 变）
    const lastIdx = this.end;
    const last = this.bars[lastIdx];
    if (last) {
      const prevC = lastIdx > 0 ? this.bars[lastIdx - 1].c : last.c;
      this._priceTag(octx, L, col, last.c, yOf(last.c), last.c >= prevC ? col.up : col.down);
    }

    this.canvas.setAttribute('aria-label',
      `K 线 ${this.bars[this.start]?.t || ''} 至 ${this.bars[this.end]?.t || ''}，`
      + `区间最高 ${this._fmtPrice(hi)}、最低 ${this._fmtPrice(lo)}。方向键逐根读数，滚轮缩放，双击复位。`);
  }

  _grid(ctx, L, col, lo, hi, yOf) {
    ctx.save();
    ctx.strokeStyle = col.grid;
    ctx.lineWidth = 1;
    ctx.font = FONT_MICRO;
    ctx.fillStyle = col.label;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const steps = L.narrow ? 3 : 6;
    let lastLabelY = -99;
    // 最新价那枚色块也在同一列里。刻度挨得太近会让两个数字叠成一片，
    // 所以先把它的位置算出来，落在它附近（±9px）的刻度不画字。
    const lastBar = this.bars[this.end];
    const lastY = lastBar ? yOf(lastBar.c) : null;
    for (let i = 0; i <= steps; i += 1) {
      const v = lo + ((hi - lo) * i) / steps;
      const y = Math.round(yOf(v)) + 0.5;
      if (y < L.main.y - 1 || y > L.main.y + L.main.h + 1) continue;
      ctx.beginPath();
      ctx.moveTo(L.main.x, y);
      ctx.lineTo(L.main.x + L.main.w, y);
      ctx.stroke();
      // 两个标签纵向距离 < 16px 就只留上面那个：重叠的价格比没有价格更难读。
      if (lastY !== null && Math.abs(y - lastY) < 9) continue;
      if (Math.abs(y - lastLabelY) >= 16) {
        ctx.fillText(this._fmtPrice(v), this.w - 6, y);
        lastLabelY = y;
      }
    }
    ctx.restore();
  }

  _fmtPrice(v) {
    if (!Number.isFinite(v)) return '—';
    return v.toFixed(2);
  }

  /** 右侧价格标签：clamp 在 main 区内侧，不许压到成交量/副图，也不许越出画布。 */
  /** 价格轴标签：2px 色条 + 6px 间隙 + 50px 右对齐数字（58 = 2 + 6 + 50）。
      只放价格，永不放涨跌幅（它在 sticky 顶栏；同屏重复数字是缺陷）。
      y 夹在 [10, volTop - 10]，与 VOL 之间留 10px 净空。 */
  _priceTag(ctx, L, col, value, y, color) {
    const text = this._fmtPrice(value);
    const axisX = L.main.x + L.main.w;
    const volTop = L.vol ? L.vol.y : this.h - (L.xaxisH || 18);
    const by = Math.max(10, Math.min(y, volTop - 10));
    ctx.save();
    ctx.font = FONT_MICRO;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    // 2px 色条：涨停/跌停 +2px 高，其余 12px
    const barH = (color === col.up || color === col.down) ? 14 : 12;
    ctx.fillStyle = color;
    ctx.fillRect(axisX, Math.round(by - barH / 2), 2, barH);
    ctx.fillStyle = color === col.cross ? col.label : color;
    ctx.fillText(text, this.w - 4, by);
    ctx.restore();
  }

  _candles(ctx, L, col, xCenter, yOf) {
    const bw = Math.max(1, Math.min(13, L.gap * 0.66));
    ctx.save();
    ctx.lineWidth = 1;
    for (let i = this.start; i <= this.end; i += 1) {
      const b = this.bars[i];
      if (!b) break;
      const x = Math.round(xCenter(i)) + 0.5;
      const rising = b.c > b.o;
      const flat = b.c === b.o;
      const color = flat ? col.flat : rising ? col.up : col.down;
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      const bodyTop = yOf(Math.max(b.o, b.c));
      const bodyBot = yOf(Math.min(b.o, b.c));
      const bh = Math.max(1, bodyBot - bodyTop);
      ctx.beginPath();
      ctx.moveTo(x, Math.round(yOf(b.h)) + 0.5);
      ctx.lineTo(x, Math.round(yOf(b.l)) + 0.5);
      ctx.stroke();
      const lx = Math.round(xCenter(i) - bw / 2);
      const bwR = Math.max(1, Math.round(bw));
      if (flat && b.h === b.l) {
        ctx.beginPath();
        ctx.moveTo(lx, Math.round(yOf(b.c)) + 0.5);
        ctx.lineTo(lx + bwR, Math.round(yOf(b.c)) + 0.5);
        ctx.stroke();
      } else if (rising) {
        // 阳线空心：先用面板底色盖住网格，再描边。空心才看得见后面的网格与均线。
        ctx.fillStyle = col.bg;
        ctx.fillRect(lx, Math.round(bodyTop), bwR, bh);
        ctx.strokeStyle = color;
        ctx.strokeRect(lx + 0.5, Math.round(bodyTop) + 0.5, Math.max(1, bwR - 1), Math.max(1, Math.round(bh) - 1));
      } else {
        ctx.fillRect(lx, Math.round(bodyTop), bwR, bh);
      }
    }
    ctx.restore();
  }

  _overlays(ctx, col, s, xCenter, yOf) {
    // 布林带先画成一条带子，再补上下轨：紫已被"坏"这个通道占用，不要借色
    if (s.bollUp && s.bollLow) {
      ctx.save();
      ctx.beginPath();
      let started = false;
      for (let i = this.start; i <= this.end; i += 1) {
        const v = s.bollUp[i];
        if (!Number.isFinite(v)) { started = false; continue; }
        const x = xCenter(i);
        if (!started) { ctx.moveTo(x, yOf(v)); started = true; } else ctx.lineTo(x, yOf(v));
      }
      for (let i = this.end; i >= this.start; i -= 1) {
        const v = s.bollLow[i];
        if (!Number.isFinite(v)) continue;
        ctx.lineTo(xCenter(i), yOf(v));
      }
      ctx.closePath();
      ctx.fillStyle = col.bollFill;
      ctx.fill();
      ctx.restore();
      this._line(ctx, s.bollUp, col.bollLine, 1, [1, 3], xCenter, yOf);
      this._line(ctx, s.bollLow, col.bollLine, 1, [1, 3], xCenter, yOf);
    }
    if (s.ma5) {
      const narrow = this.L?.narrow;
      this._line(ctx, s.ma5, col.ma5, 1.2, null, xCenter, yOf);
      if (!narrow) this._line(ctx, s.ma10, col.ma10, 1.2, null, xCenter, yOf);
      this._line(ctx, s.ma20, col.ma20, 1.2, null, xCenter, yOf);
      if (!narrow) this._line(ctx, s.ma60, col.ma60, 1.1, [4, 3], xCenter, yOf);
    }
  }

  _line(ctx, arr, color, width, dash, xCenter, yOf) {
    if (!arr) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    for (let i = this.start; i <= this.end; i += 1) {
      const v = arr[i];
      if (!Number.isFinite(v)) { started = false; continue; }
      const x = xCenter(i);
      if (!started) { ctx.moveTo(x, yOf(v)); started = true; } else ctx.lineTo(x, yOf(v));
    }
    ctx.stroke();
    ctx.restore();
  }

  /** 分区边界：6px 空隙 + 1px 线。仪表的边界用线表达，不用留白。 */
  _separators(ctx, L, col) {
    ctx.save();
    ctx.strokeStyle = col.divider;
    ctx.lineWidth = 1;
    for (const box of [L.vol, L.ind]) {
      if (!box) continue;
      const y = Math.round(box.y - 4) + 0.5;
      ctx.beginPath();
      ctx.moveTo(L.main.x, y);
      ctx.lineTo(L.main.x + L.main.w, y);
      ctx.stroke();
    }
    ctx.restore();
  }

  _volume(ctx, box, col, xCenter) {
    const bw = Math.max(1, Math.min(13, this.L.gap * 0.66));
    const maxV = Math.max(...this.bars.slice(this.start, this.end + 1).map((b) => b.vol || 0), 1);
    ctx.save();
    for (let i = this.start; i <= this.end; i += 1) {
      const b = this.bars[i];
      if (!b) break;
      const x0 = Math.round(xCenter(i) - bw / 2);
      const bwR = Math.max(1, Math.round(bw));
      // 留 3px 净空：量柱不许顶到那条 1px 边界线上。
      const hgt = Math.max(1, ((b.vol || 0) / maxV) * (box.h - 4));
      const y0 = Math.round(box.y + box.h - hgt);
      // 量柱的主编码是高度，颜色只做冗余 —— 60px 高、5px 宽上空心会糊成一片
      ctx.fillStyle = b.c >= b.o ? col.volUp : col.volDown;
      ctx.fillRect(x0, y0, bwR, hgt);
    }
    ctx.fillStyle = col.label;
    ctx.font = FONT_MICRO;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(`VOL ${cny(maxV * 100, { unit: true })}`, box.x + 2, box.y - 3);
    ctx.restore();
  }

  _indicatorPane(ctx, box, col, s, xCenter) {
    const kind = this.indicator;
    ctx.save();
    // 副图边界用 --chart-axis：与 grid 必须能分辨，否则看不出副图从哪开始
    ctx.strokeStyle = col.axis;
    ctx.lineWidth = 1;
    ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);
    ctx.font = FONT_MICRO;

    if (kind === 'MACD' && s.macd) {
      const { dif, dea, hist } = s.macd;
      let lo = Infinity;
      let hi = -Infinity;
      for (let i = this.start; i <= this.end; i += 1) {
        for (const v of [dif[i], dea[i], hist[i]]) {
          if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
        }
      }
      if (!Number.isFinite(lo)) { ctx.restore(); return; }
      const pad = (hi - lo) * 0.15 || 1;
      lo -= pad; hi += pad;
      const y = (v) => box.y + box.h - ((v - lo) / (hi - lo)) * box.h;
      const zero = y(0);
      ctx.strokeStyle = col.strong;
      ctx.beginPath(); ctx.moveTo(box.x, zero); ctx.lineTo(box.x + box.w, zero); ctx.stroke();
      const bw = Math.max(1, Math.min(6, this.L.gap * 0.4));
      for (let i = this.start; i <= this.end; i += 1) {
        const v = hist[i];
        if (!Number.isFinite(v)) continue;
        ctx.fillStyle = v >= 0 ? col.macdUp : col.macdDown;
        const x = xCenter(i);
        const y0 = Math.min(y(v), zero);
        ctx.fillRect(Math.round(x - bw / 2), Math.round(y0), Math.max(1, Math.round(bw)), Math.max(1, Math.abs(y(v) - zero)));
      }
      this._line(ctx, dif, col.ma5, 1.1, null, xCenter, y);
      this._line(ctx, dea, col.ma10, 1.1, null, xCenter, y);
      const i = this.hover?.index ?? this.end;
      ctx.fillStyle = col.text;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText(`MACD(12,26,9)  DIF ${this._n(dif[i])}  DEA ${this._n(dea[i])}  MACD ${this._n(hist[i])}`,
        box.x + 4, box.y + 3);
    } else if (kind === 'KDJ' && s.kdj) {
      const { k, d, j } = s.kdj;
      const lo = 0; const hi = 100;
      const y = (v) => box.y + box.h - ((v - lo) / (hi - lo)) * box.h;
      ctx.textBaseline = 'top';
      for (const level of [20, 50, 80]) {
        ctx.strokeStyle = level === 50 ? col.grid : col.axis;
        ctx.beginPath(); ctx.moveTo(box.x, y(level)); ctx.lineTo(box.x + box.w, y(level)); ctx.stroke();
        ctx.fillStyle = col.label; ctx.textAlign = 'right';
        ctx.fillText(String(level), box.x + box.w - 3, y(level) - 11);
      }
      this._line(ctx, k, col.ma5, 1.1, null, xCenter, y);
      this._line(ctx, d, col.ma10, 1.1, null, xCenter, y);
      this._line(ctx, j, col.ma20, 1.1, null, xCenter, y);
      const i = this.hover?.index ?? this.end;
      ctx.fillStyle = col.text;
      ctx.textAlign = 'left';
      ctx.fillText(`KDJ(9,3,3)  K ${this._n(k[i])}  D ${this._n(d[i])}  J ${this._n(j[i])}`, box.x + 4, box.y + 3);
    } else if (kind === 'RSI' && s.rsi6) {
      const y = (v) => box.y + box.h - (v / 100) * box.h;
      ctx.textBaseline = 'top';
      for (const level of [30, 50, 70]) {
        ctx.strokeStyle = level === 50 ? col.grid : col.axis;
        ctx.beginPath(); ctx.moveTo(box.x, y(level)); ctx.lineTo(box.x + box.w, y(level)); ctx.stroke();
        ctx.fillStyle = col.label; ctx.textAlign = 'right';
        ctx.fillText(String(level), box.x + box.w - 3, y(level) - 11);
      }
      this._line(ctx, s.rsi6, col.ma5, 1.1, null, xCenter, y);
      this._line(ctx, s.rsi12, col.ma10, 1.1, null, xCenter, y);
      this._line(ctx, s.rsi24, col.ma20, 1.1, null, xCenter, y);
      const i = this.hover?.index ?? this.end;
      ctx.fillStyle = col.text;
      ctx.textAlign = 'left';
      ctx.fillText(`RSI  RSI6 ${this._n(s.rsi6[i])}  RSI12 ${this._n(s.rsi12[i])}  RSI24 ${this._n(s.rsi24[i])}`, box.x + 4, box.y + 3);
    }
    ctx.restore();
  }

  _n(v) { return Number.isFinite(v) ? v.toFixed(2) : '—'; }

  _legend(L, col) {
    if (!L) return;
    const { ctx } = this;
    const s = this._ser || {};
    const i = this.hover?.index ?? this.end;
    const b = this.bars[i];
    if (!b) return;
    const prev = i > 0 ? this.bars[i - 1].c : b.o;
    ctx.save();
    ctx.font = FONT_BODY;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    let x = L.main.x + 2;
    const y = 13;
    const chunk = (text, color) => {
      ctx.fillStyle = color;
      ctx.fillText(text, x, y);
      x += ctx.measureText(text).width + 10;
    };
    chunk(md(b.t), col.text);
    chunk(`开 ${fprice(b.o)}`, col.text);
    chunk(`高 ${fprice(b.h)}`, col.text);
    chunk(`低 ${fprice(b.l)}`, col.text);
    chunk(`收 ${fprice(b.c)}`, b.c >= prev ? col.up : col.down);
    chunk(pct(b.c / prev - 1), b.c >= prev ? col.up : col.down);
    if (this.overlays.ma) {
      for (const def of MA_DEFS) {
        if (L.narrow && def.n !== 5 && def.n !== 20) continue;
        const v = s[def.key]?.[i];
        if (Number.isFinite(v)) chunk(`${def.label} ${fprice(v)}`, col[def.key]);
      }
    }
    ctx.restore();
  }

  _timeAxis(ctx, L, col, xCenter) {
    ctx.save();
    ctx.font = FONT_MICRO;
    ctx.fillStyle = col.label;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const n = this.end - this.start + 1;
    const want = Math.max(2, Math.floor(L.plotW / 78));
    const step = Math.max(1, Math.round(n / want));
    for (let i = this.start; i <= this.end; i += step) {
      const b = this.bars[i];
      if (!b) break;
      const x = xCenter(i);
      // 首尾标签收进来，避免被画布边缘裁掉半个字
      const tx = Math.max(L.main.x + 18, Math.min(x, L.main.x + L.main.w - 18));
      ctx.fillText(md(b.t), tx, this.h - 15);
    }
    ctx.restore();
  }

  _crosshair(col) {
    const { ctx, hover, L } = this;
    if (!hover || !L) return;
    const idx = Math.max(this.start, Math.min(this.end, hover.index));
    const x = this.xCenter(idx);
    const y = hover.y;
    ctx.save();
    ctx.setLineDash([1, 2]);
    ctx.strokeStyle = col.cross;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, L.main.y);
    ctx.lineTo(Math.round(x) + 0.5, this.h - (L.xaxisH || 18));
    ctx.stroke();
    if (y > L.main.y && y < L.main.y + L.main.h) {
      ctx.beginPath();
      ctx.moveTo(L.main.x, Math.round(y) + 0.5);
      ctx.lineTo(L.main.x + L.main.w, Math.round(y) + 0.5);
      ctx.stroke();
      const v = this.hi - ((y - L.main.y) / L.main.h) * (this.hi - this.lo);
      this._priceTag(ctx, L, col, v, y, col.cross);
      ctx.fillStyle = col.bg;
      ctx.fillRect(Math.round(x) - 3.5, Math.round(y) - 3.5, 7, 7);
      ctx.strokeStyle = col.cross;
      ctx.strokeRect(Math.round(x) - 3.5, Math.round(y) - 3.5, 7, 7);
    }
    ctx.setLineDash([]);
    const b = this.bars[idx];
    if (b) {
      const label = md(b.t);
      ctx.font = FONT_MICRO;
      const tw = ctx.measureText(label).width + 12;
      const bx = Math.max(tw / 2 + 1, Math.min(x, this.w - tw / 2 - 1));
      ctx.fillStyle = col.crossBg;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx - tw / 2, this.h - 18, tw, 15, 2);
      else ctx.rect(bx - tw / 2, this.h - 18, tw, 15);
      ctx.fill();
      ctx.strokeStyle = col.strong;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = col.text;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, bx, this.h - 10);
    }
    ctx.restore();
  }

  // ---------------------------------------------------------------- events
  /** 用上一帧的 L（this.L）反解，不要重算 layout()：
      resize 之后 this.w/h 与 this.L 不同步，重算会差一格。 */
  _indexAt(clientX) {
    const L = this.L;
    if (!L) return this.start;
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const idx = Math.round((x - L.main.x) / L.gap - 0.5) + this.start;
    return Math.max(this.start, Math.min(this.end, idx));
  }

  _emitHover(index) {
    this.hover = index === null ? null : { ...this.hover, index };
    this.draw();
    if (this.opts.onHover) this.opts.onHover(index === null ? null : this.bars[index], index);
  }

  _move(ev) {
    const rect = this.canvas.getBoundingClientRect();
    if (this.drag) {
      const dx = ev.clientX - this.drag.x;
      const step = Math.round(dx / Math.max(1, this.L?.gap || 1));
      if (step !== 0) {
        const span = this.drag.end - this.drag.start;
        const len = this.bars.length;
        let start = this.drag.start - step;
        let end = this.drag.end - step;
        if (start < 0) { start = 0; end = Math.min(len - 1, span); }
        if (end > len - 1) { end = len - 1; start = Math.max(0, end - span); }
        // 贴边后必须重置锚点：否则手指往回走 1px 视图会跳一整段
        if (start !== this.drag.start) { this.drag.start = start; this.drag.end = end; this.drag.x = ev.clientX; }
        this.start = start;
        this.end = Math.max(start, Math.min(end, len - 1));
        this._dirty = true;
        this._emitRange();
      }
    }
    const index = this._indexAt(ev.clientX);
    const y = ev.clientY - rect.top;
    this.hover = { index, x: ev.clientX - rect.left, y };
    this.draw();
    if (this.opts.onHover) this.opts.onHover(this.bars[index], index);
  }

  _leave() {
    this.hover = null;
    this.drag = null;
    this.draw();
    if (this.opts.onHover) this.opts.onHover(null, null);
  }

  _down(ev) {
    if (!this.bars.length) return;
    this.drag = { x: ev.clientX, start: this.start, end: this.end };
    this.canvas.setPointerCapture(ev.pointerId);
    this.canvas.classList.add('dragging');
  }

  _up(ev) {
    this.drag = null;
    this.canvas.classList.remove('dragging');
    try { this.canvas.releasePointerCapture(ev.pointerId); } catch { /* 指针已释放 */ }
  }

  _dbl() { this.resetView(); this._dirty = true; this.draw(); }

  /** 滚轮缩放：锚点按像素比例算，不按 index 比例 —— 后者每轮吃 ±0.5 格会缓慢漂移。 */
  _wheel(ev) {
    ev.preventDefault();
    if (!this.bars.length || !this.L) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const frac = Math.max(0, Math.min(1, (x - this.L.main.x) / this.L.plotW));
    const dir = ev.deltaY > 0 ? 1 : -1;
    const span = this.end - this.start;
    const len = this.bars.length;
    const plotW = this.L.plotW;
    // pitch 钳在 4-14px：不出现半根蜡烛，也不让一根 K 缩成一条线
    const minSpan = Math.max(1, Math.ceil(plotW / 14));
    const maxSpan = Math.max(minSpan, Math.min(len - 1, Math.floor(plotW / 4)));
    const nextSpan = Math.max(minSpan, Math.min(maxSpan, Math.round(span * (dir > 0 ? 1.18 : 0.85))));
    const anchor = this.start + frac * span;
    let start = Math.round(anchor - nextSpan * frac);
    let end = start + nextSpan;
    if (start < 0) { start = 0; end = nextSpan; }
    if (end > len - 1) { end = len - 1; start = Math.max(0, end - nextSpan); }
    this.start = start;
    this.end = end;
    this.hover = null;
    this._dirty = true;
    this._emitRange();
    this.draw();
  }

  _key(ev) {
    if (!this.bars.length) return;
    const step = ev.shiftKey ? 10 : 1;
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {
      ev.preventDefault();
      const cur = this.hover?.index ?? this.end;
      const next = Math.max(this.start, Math.min(this.end, cur + (ev.key === 'ArrowRight' ? step : -step)));
      this.hover = { index: next, x: this.xCenter(next), y: this.L.main.y + this.L.main.h / 2 };
      this.draw();
      if (this.opts.onHover) this.opts.onHover(this.bars[next], next);
    } else if (ev.key === 'Escape') {
      this.hover = null;
      this.draw();
      if (this.opts.onHover) this.opts.onHover(null, null);
    }
  }
}

/** 迷你走势：账户、擂台、榜单行里的小图。空数据画 baseline，不画假的起伏。 */
export class Sparkline {
  constructor(canvas, { color = null, fill = true, baseline = null } = {}) {
    this.canvas = canvas;
    this.opts = { color, fill, baseline };
    this.draw([]);
  }

  draw(values) {
    const canvas = this.canvas;
    if (!canvas.isConnected) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(40, Math.floor(rect.width || canvas.clientWidth || 120));
    const h = Math.max(20, Math.floor(rect.height || canvas.clientHeight || 36));
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const color = this.opts.color || cssVar(document.documentElement, '--ma-10', '#FFC24B');
    const data = (values || []).filter((v) => Number.isFinite(v));
    if (data.length < 2) {
      ctx.strokeStyle = cssVar(document.documentElement, '--line', '#21262E');
      ctx.beginPath(); ctx.moveTo(0, h - 1); ctx.lineTo(w, h - 1); ctx.stroke();
      return;
    }
    const lo = Math.min(...data); const hi = Math.max(...data);
    const pad = (hi - lo) * 0.12 || 0.5;
    const x = (i) => (i / (data.length - 1)) * (w - 1);
    const y = (v) => h - 2 - ((v - (lo - pad)) / ((hi + pad) - (lo - pad))) * (h - 4);
    ctx.beginPath();
    data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    if (this.opts.fill) {
      ctx.lineTo(x(data.length - 1), h);
      ctx.lineTo(x(0), h);
      ctx.closePath();
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, `${color}33`);
      g.addColorStop(1, `${color}00`);
      ctx.fillStyle = g;
      ctx.fill();
    }
  }
}
