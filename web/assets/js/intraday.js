// 分时图：5 分钟线 + 均价线 + 昨收基准。
// 它不是"缩小版日 K"：分时回答的是"今天这 240 分钟里，谁在什么价位成交"，
// 所以基准线是昨收，而不是第一根的开盘价。

function cssVar(el, name, fallback) {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || fallback;
}

export class IntradayChart {
  constructor(host) {
    this.host = host;
    this.bars = [];
    this.prevClose = null;
    this.hover = null;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'kline-canvas';
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this._move = this._onMove.bind(this);
    this._leave = this._onLeave.bind(this);
    this.canvas.addEventListener('pointermove', this._move);
    this.canvas.addEventListener('pointerleave', this._leave);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
  }

  destroy() {
    this.ro.disconnect();
    this.canvas.removeEventListener('pointermove', this._move);
    this.canvas.removeEventListener('pointerleave', this._leave);
    this.canvas.remove();
  }

  setData(bars, prevClose) {
    this.bars = bars || [];
    this.prevClose = Number(prevClose) || (this.bars[0]?.o ?? null);
    this.resize();
  }

  resize() {
    const rect = this.host.getBoundingClientRect();
    this.w = Math.max(240, Math.floor(rect.width));
    this.h = Math.max(140, Math.floor(rect.height));
    this.canvas.width = this.w * this.dpr;
    this.canvas.height = this.h * this.dpr;
    this.canvas.style.width = `${this.w}px`;
    this.canvas.style.height = `${this.h}px`;
    this.draw();
  }

  draw() {
    const { ctx, w, h } = this;
    if (!w || !h) return;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const col = {
      grid: cssVar(this.host, '--chart-grid', '#171C23'),
      label: cssVar(this.host, '--chart-label', '#7C8899'),
      up: cssVar(this.host, '--px-up', '#FF5A63'),
      down: cssVar(this.host, '--px-down', '#21C97C'),
      cross: cssVar(this.host, '--chart-cross', '#5E6B80'),
    };
    if (!this.bars.length) {
      ctx.fillStyle = col.label;
      ctx.font = '12px ui-monospace, monospace';
      ctx.fillText('这一天没有 5 分钟线', 14, 24);
      return;
    }
    const pad = { l: 8, r: 54, t: 10, b: 18 };
    const plotW = w - pad.l - pad.r;
    const volH = Math.round((h - pad.t - pad.b) * 0.18);
    const gapH = 8;
    const priceH = h - pad.t - pad.b - volH - gapH;
    const bars = this.bars;
    const n = bars.length;
    let lo = Math.min(...bars.map((b) => b.l));
    let hi = Math.max(...bars.map((b) => b.h));
    if (this.prevClose) { lo = Math.min(lo, this.prevClose); hi = Math.max(hi, this.prevClose); }
    const padY = (hi - lo) * 0.1 || 1;
    lo -= padY; hi += padY;
    const x = (i) => pad.l + (i + 0.5) * (plotW / n);
    const y = (v) => pad.t + priceH - ((v - lo) / (hi - lo)) * priceH;

    ctx.strokeStyle = col.grid;
    ctx.lineWidth = 1;
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = col.label;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let k = 0; k <= 4; k += 1) {
      const v = lo + ((hi - lo) * k) / 4;
      const yy = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(pad.l + plotW, yy); ctx.stroke();
      ctx.fillText(v.toFixed(2), pad.l + plotW + 5, yy);
    }
    if (this.prevClose) {
      const p = this.prevClose;
      ctx.strokeStyle = 'rgba(255,255,255,.22)';
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(pad.l, y(p)); ctx.lineTo(pad.l + plotW, y(p)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(255,255,255,.45)';
      ctx.fillText(p.toFixed(2), pad.l + plotW + 5, y(p));
    }

    const rising = bars[n - 1].c >= this.prevClose;
    const stroke = rising ? col.up : col.down;
    ctx.beginPath();
    bars.forEach((b, i) => (i ? ctx.lineTo(x(i), y(b.c)) : ctx.moveTo(x(i), y(b.c))));
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.3;
    ctx.stroke();
    ctx.lineTo(x(n - 1), pad.t + priceH);
    ctx.lineTo(x(0), pad.t + priceH);
    ctx.closePath();
    const g = ctx.createLinearGradient(0, pad.t, 0, pad.t + priceH);
    g.addColorStop(0, rising ? 'rgba(255,90,99,.18)' : 'rgba(33,201,124,.18)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fill();

    let cumAmt = 0; let cumVol = 0;
    ctx.beginPath();
    let started = false;
    bars.forEach((b, i) => {
      cumAmt += b.amt || 0;
      cumVol += (b.vol || 0) * 100;
      if (!cumVol) return;
      const vwap = cumAmt / cumVol;
      if (!started) { ctx.moveTo(x(i), y(vwap)); started = true; } else ctx.lineTo(x(i), y(vwap));
    });
    ctx.strokeStyle = 'rgba(255,194,75,.75)';
    ctx.lineWidth = 1;
    ctx.stroke();

    const maxV = Math.max(...bars.map((b) => b.vol || 0), 1);
    const volTop = pad.t + priceH + gapH;
    bars.forEach((b, i) => {
      const bh = Math.max(1, ((b.vol || 0) / maxV) * (volH - 1));
      ctx.fillStyle = b.c >= b.o ? 'rgba(255,90,99,.45)' : 'rgba(33,201,124,.45)';
      ctx.fillRect(x(i) - 2, volTop + volH - bh, 3.5, bh);
    });

    ctx.fillStyle = col.label;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const marks = [0, Math.floor(n / 4), Math.floor(n / 2), Math.floor((3 * n) / 4), n - 1];
    for (const i of marks) ctx.fillText(bars[i].t, x(i), h - 15);

    if (this.hover) {
      const i = this.hover.index;
      ctx.setLineDash([1, 2]);
      ctx.strokeStyle = col.cross;
      ctx.beginPath(); ctx.moveTo(x(i), pad.t); ctx.lineTo(x(i), h - 18); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(pad.l, this.hover.y); ctx.lineTo(pad.l + plotW, this.hover.y); ctx.stroke();
      ctx.setLineDash([]);
      const b = bars[i];
      ctx.fillStyle = 'rgba(74,84,104,.95)';
      const txt = `${b.t}  ${b.c.toFixed(2)}`;
      ctx.font = '10px ui-monospace, monospace';
      const tw = ctx.measureText(txt).width + 10;
      const bx = Math.min(Math.max(pad.l, x(i) - tw / 2), w - tw - 4);
      ctx.beginPath(); ctx.roundRect(bx, 2, tw, 14, 2); ctx.fill();
      ctx.fillStyle = '#E6E9EE'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(txt, bx + tw / 2, 9);
    }
    this._geo = { x, pad, plotW };
  }

  _onMove(ev) {
    if (!this.bars.length || !this._geo) return;
    const rect = this.canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const { pad, plotW } = this._geo;
    const n = this.bars.length;
    const i = Math.max(0, Math.min(n - 1, Math.floor(((px - pad.l) / plotW) * n)));
    this.hover = { index: i, y: ev.clientY - rect.top };
    this.draw();
  }

  _onLeave() { this.hover = null; this.draw(); }
}
