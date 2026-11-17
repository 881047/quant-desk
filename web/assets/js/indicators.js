// 指标一律在浏览器里从原始 K 线算，不用服务端给的口径 —— 服务端只给 OHLCV + adj_close。
// 这样"我看到的 MA20"和"图上画的 MA20"不可能不一致。所有函数返回与输入等长的数组，
// 数据不足的位置是 null（不是 0）：0 会画出一条落在底部的假线。

export function sma(values, n) {
  const out = Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    sum += v;
    if (i >= n) sum -= values[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

export function ema(values, n) {
  const out = Array(values.length).fill(null);
  if (!values.length) return out;
  const k = 2 / (n + 1);
  let prev = values[0];
  out[0] = prev;
  for (let i = 1; i < values.length; i += 1) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Wilder 平滑：RSI 与 ATR 的口径，和简单 EMA 不同，别混用。 */
function rma(values, n) {
  const out = Array(values.length).fill(null);
  let acc = 0;
  for (let i = 0; i < values.length; i += 1) {
    if (i < n) { acc += values[i]; if (i === n - 1) out[i] = acc / n; continue; }
    acc = out[i - 1] * (n - 1) + values[i];
    out[i] = acc / n;
  }
  return out;
}

export function boll(closes, n = 20, k = 2) {
  const mid = sma(closes, n);
  const upper = Array(closes.length).fill(null);
  const lower = Array(closes.length).fill(null);
  for (let i = n - 1; i < closes.length; i += 1) {
    let sq = 0;
    for (let j = i - n + 1; j <= i; j += 1) sq += (closes[j] - mid[i]) ** 2;
    const sd = Math.sqrt(sq / n);
    upper[i] = mid[i] + k * sd;
    lower[i] = mid[i] - k * sd;
  }
  return { mid, upper, lower };
}

export function macd(closes, fast = 12, slow = 26, signal = 9) {
  const ef = ema(closes, fast);
  const es = ema(closes, slow);
  const dif = closes.map((_, i) => ef[i] - es[i]);
  const dea = ema(dif, signal);
  const hist = dif.map((v, i) => (v - dea[i]) * 2);
  return { dif, dea, hist };
}

export function kdj(highs, lows, closes, n = 9, kPeriod = 3, dPeriod = 3) {
  const K = Array(closes.length).fill(null);
  const D = Array(closes.length).fill(null);
  const J = Array(closes.length).fill(null);
  let kPrev = 50;
  let dPrev = 50;
  for (let i = 0; i < closes.length; i += 1) {
    if (i < n - 1) continue;
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - n + 1; j <= i; j += 1) { hh = Math.max(hh, highs[j]); ll = Math.min(ll, lows[j]); }
    const rsv = hh === ll ? 50 : ((closes[i] - ll) / (hh - ll)) * 100;
    kPrev = ((kPeriod - 1) * kPrev + rsv) / kPeriod;
    dPrev = ((dPeriod - 1) * dPrev + kPrev) / dPeriod;
    K[i] = kPrev; D[i] = dPrev; J[i] = 3 * kPrev - 2 * dPrev;
  }
  return { k: K, d: D, j: J };
}

export function rsi(closes, n = 14) {
  const gains = [0];
  const losses = [0];
  for (let i = 1; i < closes.length; i += 1) {
    const ch = closes[i] - closes[i - 1];
    gains.push(Math.max(ch, 0));
    losses.push(Math.max(-ch, 0));
  }
  const ag = rma(gains, n);
  const al = rma(losses, n);
  return closes.map((_, i) => {
    if (ag[i] === null || al[i] === null) return null;
    if (al[i] === 0) return 100;
    return 100 - 100 / (1 + ag[i] / al[i]);
  });
}

/** 区间统计：给"这 20 根"一个可核对的摘要，而不是让人用眼睛量。 */
export function rangeStats(bars, from, to) {
  const part = bars.slice(Math.max(0, from), to + 1);
  if (!part.length) return null;
  const first = part[0];
  const last = part[part.length - 1];
  const high = Math.max(...part.map((b) => b.h));
  const low = Math.min(...part.map((b) => b.l));
  const vol = part.reduce((s, b) => s + (b.vol || 0), 0);
  const amount = part.reduce((s, b) => s + (b.amount || 0), 0);
  const rets = [];
  for (let i = 1; i < part.length; i += 1) rets.push(Math.log(part[i].c / part[i - 1].c));
  const mean = rets.reduce((s, r) => s + r, 0) / (rets.length || 1);
  const varSum = rets.reduce((s, r) => s + (r - mean) ** 2, 0);
  const sd = rets.length > 1 ? Math.sqrt(varSum / (rets.length - 1)) : 0;
  return {
    n: part.length,
    from: first.t, to: last.t,
    change: last.c / first.o - 1,
    high, low,
    amplitude: first.o > 0 ? (high - low) / first.o : null,
    vol, amount,
    volAnnual: sd * Math.sqrt(243),
    highAt: part.findIndex((b) => b.h === high),
    lowAt: part.findIndex((b) => b.l === low),
  };
}

/** 成交额单位：/market/bars 的 amount 是千元，统一转成元再进图。 */
export function barSeries(payload, { adjust = 'none' } = {}) {
  const bars = (payload?.bars || []).map((b) => ({
    t: String(b.trade_date),
    o: Number(b.open), h: Number(b.high), l: Number(b.low), c: Number(b.close),
    vol: Number(b.vol ?? 0),
    amount: Number(b.amount ?? 0) * 1000,
    adj: b.adj_close === null || b.adj_close === undefined ? Number(b.close) : Number(b.adj_close),
  }));
  if (adjust === 'qfq') {
    // 前复权：按 adj_close / close 的比例还原每一根的价格，末端等于原价。
    // 直接乘比例而不是用累计净值差，保证同一根的高低开收比例一致。
    for (const b of bars) {
      if (!b.c) continue;
      const k = b.adj / b.c;
      b.o *= k; b.h *= k; b.l *= k; b.c = b.adj;
    }
  }
  return bars;
}
