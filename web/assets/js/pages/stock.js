// 个股：一只票的全部事实。K 线在左边画，档案在右边读。
// 这一页不做"评分"：内核没有 score，界面也不许自己造一个。

import { api, qs } from '../api.js';
import {
  h, panel, chip, table, empty, errorBox, slot, kv, seg, sectionHeader, toast, ratioWith,
} from '../ui.js';
import {
  money, pct, pctPts, ratio, cny, ymd, num, tone, toneStyle, price as fp, signed, esc,
} from '../fmt.js';
import { KLineChart } from '../chart.js';
import { IntradayChart } from '../intraday.js';
import { barSeries, macd, kdj, rsi, boll, sma, rangeStats } from '../indicators.js';
import { zh } from '../zh.js';
import { isLiveWindow } from '../live-window.js';

const state = {
  symbol: null,
  raw: [],
  bars: [],
  days: 120,
  adjust: 'none',
  chart: null,
  intra: null,
  profile: null,
  quote: null,
  equity: 0,
  intraSession: 0,
  intraLoading: false,
  intraHasData: false,
  intraTradingDay: false,
  intraLastBar: null,
};

export async function mount(ctx) {
  const symbol = (ctx.params.symbol || '').toUpperCase();
  if (!symbol) return picker(ctx);
  state.symbol = symbol;
  state.days = Number(ctx.params.days) || 120;

  const root = h('div', { class: 'stack' });
  const head = h('div', { class: 'stock-head' }, h('div', { class: 'stack', style: { gap: '4px' } },
    h('div', { class: 'skeleton', style: { width: '180px', height: '18px' } })));
  // 图在上、轨道在右：K 线在 300px 栏里没法看（UX §2 已改）。
  // 图的有效宽度下限 880px；窄于 1600 时轨道落到图下，先保证图。
  const body = h('div', { class: 'stock-split' });
  const main = h('div', { class: 'stack', style: { minWidth: '0' } });
  const side = h('div', { class: 'stack stock-rail' });
  body.append(main, side);
  const below = h('div', { class: 'archive-grid' });
  // 换票是这只票上最高频的动作（看完 A 要看 B），所以它必须有一个不依赖
  // 回退按钮的入口：最近看过的列表 + `[` `]` 前后翻。
  remember(symbol);
  root.append(head, recentRow(symbol, ctx), body, below);

  // 不设 min-width：窄窗要让画布真的变窄，引擎才会走"收副图"的窄态分支。
  // 高度交给 CSS（.kline-fill）：原来写死 380px，1721×1033 的窗口下面会剩 500px 死区。
  const chartHost = h('div', { class: 'kline-host kline-fill' });
  const statLine = h('div', { class: 'range-stats' });
  const chartPanel = panel({
    title: '日 K',
    note: '滚轮缩放 · 拖动平移 · 双击复位',
    actions: h('div', { class: 'row-tight', id: 'chart-tools' }),
    body: chartHost,
    footer: statLine,
  });
  main.append(chartPanel);

  const intraHost = h('div', { class: 'kline-host', style: { height: '190px' } });
  const intraPanel = panel({ title: '5 分 K', note: h('span', { id: 'intra-mode' }, '5 分钟 · 加载中'), body: intraHost, footer: h('div', { class: 'micro faint', id: 'intra-note' }, '—') });
  const indHost = h('div', { class: 'grid g2', style: { gap: '1px' } });
  const indPanel = panel({ title: '指标读数', note: '本地按前复权收盘计算', body: indHost });
  intraPanel.classList.add('span2');
  indPanel.classList.add('span2');
  below.append(intraPanel, indPanel);

  // 轨道只放"现在要不要动手"：可交易性 + 测算。档案九块放到图下面。
  const sideSlots = {
    trade: panel({ title: '可交易性', body: h('div', { id: 'p-trade' }) }),
    calc: panel({ title: '成交测算', note: '/execution/estimate', body: h('div', { id: 'p-calc' }) }),
    valuation: panel({ title: '估值分位', note: '750 日窗口', body: h('div', { id: 'p-val' }) }),
    tech: panel({ title: '技术面', note: '前复权', body: h('div', { id: 'p-tech' }) }),
    rs: panel({ title: '相对强弱', body: h('div', { id: 'p-rs' }) }),
    fin: panel({ title: '财务', body: h('div', { id: 'p-fin' }) }),
    hold: panel({ title: '筹码与事件', body: h('div', { id: 'p-hold' }) }),
  };
  side.append(sideSlots.trade, sideSlots.calc);
  sideSlots.fin.classList.add('span2');
  below.append(sideSlots.valuation, sideSlots.tech, sideSlots.rs, sideSlots.fin, sideSlots.hold);

  // ---- 取数
  const barsP = api(`/market/bars${qs({ symbol, days: 250 })}`, { ttl: 60000 });
  const profileP = api(`/symbol/${encodeURIComponent(symbol)}/profile`, { ttl: 120000 });
  const quoteP = api(`/market/quote${qs({ symbols: symbol, live: true, depth: true })}`, { ttl: 15000, silent: true }).catch(() => null);
  const portP = api('/portfolio', { ttl: 20000, silent: true }).catch(() => null);

  const [barsData, profile, quote, port] = await Promise.all([barsP, profileP, quoteP, portP]).catch((err) => {
    throw err;
  });
  state.profile = profile;
  state.quote = quote?.quotes?.[0] || null;
  // 不是 `Number(equity || 0)`：净值缺失时不能变成 0，否则"占净值"会走
  // 一个假的除零分支，看起来像"这只票买得起"。
  state.equity = port?.equity === null || port?.equity === undefined ? null : Number(port.equity);
  state.raw = barsData.bars || [];
  head.replaceChildren(...headNodes(barsData, profile, state.quote, state.equity, ctx));

  // ---- 图表
  state.chart = new KLineChart(chartHost, {
    onRange: (stats) => statLine.replaceChildren(...rangeNodes(stats)),
  });
  state.chart.resize();
  applyBars();

  state.intra = new IntradayChart(intraHost);
  state.intraHasData = false;
  state.intraTradingDay = false;
  state.intraLastBar = null;
  const intraSession = ++state.intraSession;
  loadIntraday(symbol, profile, { session: intraSession });

  // ---- 工具条
  const tools = root.querySelector('#chart-tools');
  const put = (s) => tools.append(s?.el || s);
  put(seg([{ label: '60', value: 60 }, { label: '120', value: 120 }, { label: '250', value: 250 }],
    state.days, (v) => { state.days = v; applyBars(); }, { label: 'K 线区间' }));
  put(seg([{ label: '不复权', value: 'none' }, { label: '前复权', value: 'qfq' }],
    state.adjust, (v) => { state.adjust = v; applyBars(); }, { label: '复权方式' }));

  // 叠加是开关，不是单选：MA 和 BOLL 可以同时开。用 radiogroup 表达会撒谎。
  const toggle = (label, key, initial) => {
    let on = initial;
    const btn = h('button', {
      class: 'btn btn-sm', type: 'button', 'aria-pressed': String(on),
      style: { background: on ? 'var(--bg-raised)' : 'transparent', color: on ? 'var(--ink)' : 'var(--ink-dim)' },
      onclick: () => {
        on = !on;
        btn.setAttribute('aria-pressed', String(on));
        btn.style.background = on ? 'var(--bg-raised)' : 'transparent';
        btn.style.color = on ? 'var(--ink)' : 'var(--ink-dim)';
        state.chart.setOverlays({ [key]: on });
      },
    }, label);
    return btn;
  };
  tools.append(toggle('MA', 'ma', true), toggle('BOLL', 'boll', false));
  put(seg([
    { label: 'MACD', value: 'MACD' }, { label: 'KDJ', value: 'KDJ' },
    { label: 'RSI', value: 'RSI' }, { label: '关', value: 'NONE' },
  ], 'MACD', (v) => state.chart.setIndicator(v), { label: 'K 线指标' }));

  // ---- 侧栏
  slot(root.querySelector('#p-trade'), () => tradeBlock(profile, state.quote, state.equity, ctx), { lines: 6 });
  slot(root.querySelector('#p-calc'), () => calcBlock(profile, state.equity, ctx), { lines: 4 });
  slot(root.querySelector('#p-val'), () => valBlock(profile), { lines: 5 });
  slot(root.querySelector('#p-tech'), () => techBlock(profile), { lines: 5 });
  slot(root.querySelector('#p-rs'), () => rsBlock(profile), { lines: 4 });
  slot(root.querySelector('#p-fin'), () => finBlock(profile), { lines: 5 });
  slot(root.querySelector('#p-hold'), () => holdBlock(profile), { lines: 6 });
  slot(indHost, () => indicatorCards(state.raw, profile), { lines: 5 });

  root.append(stickyCta(profile, state.equity, ctx));

  // `[` / `]` 在最近看过的票之间前后翻。不用 ←/→：那个键属于画布上的光标。
  const onKey = (ev) => {
    if (ev.key !== '[' && ev.key !== ']') return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.target instanceof Element && ev.target.closest('input, textarea, select, [contenteditable]')) return;
    const list = recent();
    const i = list.indexOf(symbol);
    if (i < 0) return;
    const next = list[ev.key === ']' ? i + 1 : i - 1];
    if (!next) return;
    ev.preventDefault();
    ctx.navigate(`s/${next}`);
  };
  window.addEventListener('keydown', onKey);
  ctx.onCleanup?.(() => window.removeEventListener('keydown', onKey));
  return root;
}

/** 单列（<1563，下带掉到图下面）时，提案按钮会被滚走 —— 那是任务链断了。
    这里给一条 sticky 底条：读完档案不需要往上滚去找按钮。并排时 CSS 隐藏它。 */
function stickyCta(profile, equity, ctx) {
  const lot = Number(profile?.tradability?.min_ticket_cny);
  const share = Number.isFinite(equity) && equity > 0 && Number.isFinite(lot) ? lot / equity : null;
  return h('div', { class: 'stock-cta' },
    h('div', { class: 'stack', style: { gap: '1px' } },
      h('div', { class: 'small strong' }, `${profile.identity?.name || profile.symbol}`),
      h('div', { class: 'micro num faint' },
        Number.isFinite(lot) ? `一手 ${money(lot)}` : '一手 —',
        share === null ? '' : ` · 占净值 ${ratio(share, 0)}`)),
    h('div', { class: 'spacer' }),
    h('button', {
      class: 'btn btn-primary', style: { height: '30px' },
      onclick: () => ctx.navigate('decide', { symbol: profile.symbol }),
    }, '用这只票提案'));
}

export function unmount() {
  state.intraSession += 1;
  state.intraLoading = false;
  state.intraTradingDay = false;
  state.intraHasData = false;
  state.intraLastBar = null;
  state.profile = null;
  state.chart?.destroy();
  state.intra?.destroy();
  state.chart = null;
  state.intra = null;
}

// ------------------------------------------------------------------ 最近看过
// 存在 localStorage 里而不是 sessionStorage：一天看三次，最后一次和下一次之间
// 关过窗口。"我刚才看的那只"跨会话仍然有用，"刚才滚到哪"不跨。
const RECENT_KEY = 'qd.recentSymbols';
const RECENT_MAX = 8;

function recent() {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string').slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

function remember(symbol) {
  if (!symbol) return;
  const next = [symbol, ...recent().filter((s) => s !== symbol)].slice(0, RECENT_MAX);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)); } catch { /* 隐私模式：记不住就记不住 */ }
}

/** 顶栏下面那一条：最近看过。只有一只是它自己就不显示，避免噪音。 */
function recentRow(symbol, ctx) {
  const items = recent().filter((s) => s !== symbol);
  if (!items.length) return h('div', { class: 'recent-row', hidden: 'hidden' });
  return h('div', { class: 'recent-row' },
    h('span', { class: 'micro faint', style: { marginRight: '2px' } }, '最近'),
    ...items.map((s) => h('button', {
      class: 'btn btn-sm', type: 'button', title: `打开 ${s}（[ ] 前后翻）`,
      onclick: () => ctx.navigate(`s/${s}`),
    }, s.replace(/\.(SH|SZ|BJ)$/, ''))));
}

export async function onRefresh() {
  if (!state.symbol) return;
  const symbol = state.symbol;
  const [data] = await Promise.all([
    api(`/market/bars${qs({ symbol, days: 250 })}`, { ttl: 0, force: true }),
    state.profile
      ? loadIntraday(symbol, state.profile, { force: true })
      : Promise.resolve(),
  ]);
  if (symbol !== state.symbol) return;
  state.raw = data.bars || [];
  applyBars();
  toast('K 线已更新', 'ok');
}

function applyBars() {
  const raw = state.raw.slice(-Math.max(state.days, 40));
  state.bars = barSeries({ bars: raw }, { adjust: state.adjust });
  state.chart?.setData(state.bars);
}

export async function onTick() {
  if (!state.symbol || !state.profile || !state.intraTradingDay || !isLiveWindow()) return;
  await loadIntraday(state.symbol, state.profile, { force: true });
}

async function loadIntraday(symbol, profile, { force = false, session = state.intraSession } = {}) {
  // 30 秒 tick 与手动刷新允许入口重叠，但不允许同一页面叠两个上游请求。
  if (state.intraLoading) return;
  state.intraLoading = true;
  const note = document.getElementById('intra-note');
  const mode = document.getElementById('intra-mode');
  try {
    const data = await api(`/market/mins${qs({ symbol, limit: 48, live: true })}`, {
      ttl: force ? 0 : 60000, force, silent: true,
    });
    // 页面卸载或切到另一只票之后，慢响应只属于旧上下文。
    if (session !== state.intraSession || symbol !== state.symbol) return;
    state.intraTradingDay = data?.session?.trading_day === true;
    if (!data?.ok || !data.bars?.length) {
      if (!state.intraHasData) state.intra?.setData([], null);
      if (note) note.textContent = data?.note || '这一天没有 5 分钟线';
      if (mode) mode.textContent = data?.session?.label || '暂无数据';
      return;
    }
    state.intraHasData = true;
    state.intra?.setData(data.bars, profile?.tradability?.pre_close);
    const live = data.is_realtime === true;
    const last = String(data.last_bar || '').replace('T', ' ').slice(11, 16)
      || data.bars[data.bars.length - 1]?.t || '';
    state.intraLastBar = `${data.date} ${last}`.trim();
    const label = data.session?.label || (live ? '实时' : '历史');
    if (mode) {
      mode.textContent = live && isLiveWindow() && !['午休', '已收盘'].includes(label)
        ? '实时 · 30 秒'
        : label;
    }
    if (note) {
      note.textContent = [
        `${state.intraLastBar} · ${live ? '实时源' : '非实时'} · ${data.source ? zh.vendor(data.source) : data.from}`,
        `${data.count} 根 · VWAP ${fp(data.vwap)}`,
        `日内高 ${fp(profile?.intraday?.session_high)} / 低 ${fp(profile?.intraday?.session_low)}`,
        `前 30 分钟量占 ${ratio(profile?.intraday?.first_30min_volume_share, 1)}`,
      ].filter(Boolean).join(' · ');
    }
  } catch (err) {
    if (session !== state.intraSession || symbol !== state.symbol) return;
    if (note) {
      note.textContent = `${err.code || 'ERR'}：${err.message}`
        + (state.intraHasData ? ` · 保留 ${state.intraLastBar}` : '');
    }
    if (mode) mode.textContent = '刷新失败';
  } finally {
    if (session === state.intraSession) state.intraLoading = false;
  }
}

// ------------------------------------------------------------------ 头部
function headNodes(barsData, profile, quote, equity, ctx) {
  const t = profile.tradability || {};
  const id = profile.identity || {};
  const last = Number(t.last);
  const pre = Number(t.pre_close);
  const chg = numberOr(quote?.pct_chg, pre ? (last / pre - 1) * 100 : null);
  const cls = tone(chg);
  const live = quote?.is_realtime;
  const quoteInfo = quote
    ? h('div', { class: 'row-tight' },
      chip(live ? `实时 · ${zh.vendor(quote.source)}` : `日线 · ${zh.vendor(quote.source || 'stored')}`, live ? 'ok' : 'off'),
      quote.age_seconds !== null && quote.age_seconds !== undefined ? chip(`${quote.age_seconds}s`, '') : null)
    : chip('无报价', 'off');

  return [
    h('div', { class: 'stack', style: { gap: '5px' } },
      h('div', { class: 'stock-id' },
        h('span', { class: 'stock-name' }, id.name || profile.symbol),
        h('span', { class: 'stock-code' }, profile.symbol),
        // 同一个名字贴两遍没有信息量：000001.SZ 的 industry 与申万一级都是"银行"。
        // 只有当它们真的不同（茅台：白酒 / 食品饮料）才都显示。
        id.industry && id.industry !== id.sw?.l1 ? chip(id.industry) : null,
        id.sw?.l1 ? chip(id.sw.l1) : null,
        id.is_st ? chip('ST', 'warn') : null,
        id.is_etf ? chip('ETF', '') : null,
        id.board ? chip(zh.board(id.board), 'off') : null),
      h('div', { class: 'row', style: { gap: '10px', alignItems: 'baseline' } },
        h('span', { class: `stock-price ${cls}` }, fp(last)),
        h('span', { class: `stock-delta ${cls}` }, `${signed(chg, 2)}%`),
        h('span', { class: `small ${cls} num` }, `${signed(last - pre, 2)}`),
        h('div', { class: 'spacer' }),
        quoteInfo),
      h('div', { class: 'stock-quote-meta' },
        h('span', {}, `开 ${fp(t.open)}`),
        h('span', {}, `高 ${fp(t.high)}`),
        h('span', {}, `低 ${fp(t.low)}`),
        h('span', {}, `昨收 ${fp(pre)}`),
        h('span', {}, `额 ${cny(t.amount_cny)}`),
        h('span', {}, `涨停 ${fp(t.up_limit)}`),
        h('span', {}, `跌停 ${fp(t.down_limit)}`),
        h('span', { class: 'faint' }, `as_of ${ymd(barsData.as_of)} · last_bar ${ymd(barsData.last_bar)}`))),
    h('div', { class: 'row-tight' },
      h('button', { class: 'btn', onclick: () => ctx.navigate('decide', { symbol: profile.symbol }) }, '用它提案'),
      h('button', { class: 'btn btn-ghost', onclick: () => ctx.navigate('market', { industry: id.sw?.l1 || '' }) }, '看行业')),
  ];
}

function numberOr(a, b) {
  const v = a === null || a === undefined ? b : a;
  return v === null || v === undefined ? null : Number(v);
}

// ------------------------------------------------------------------ 区间统计
function rangeNodes(stats) {
  if (!stats) return [h('span', { class: 'faint' }, '—')];
  return [
    h('span', {}, `${ymd(stats.from)} → ${ymd(stats.to)} · ${stats.n} 根`),
    h('span', {}, '区间 ', h('b', { class: tone(stats.change), style: toneStyle(stats.change) }, pct(stats.change))),
    h('span', {}, '振幅 ', h('b', {}, ratio(stats.amplitude, 1))),
    h('span', {}, '高 ', h('b', {}, fp(stats.high)), ' 低 ', h('b', {}, fp(stats.low))),
    h('span', {}, '成交额 ', h('b', {}, cny(stats.amount))),
    h('span', {}, '年化波动 ', h('b', {}, ratio(stats.volAnnual, 1))),
  ];
}

// ------------------------------------------------------------------ 指标
function indicatorCards(raw, profile) {
  const bars = barSeries({ bars: raw }, { adjust: 'qfq' });
  if (bars.length < 30) return empty('K 线不足以计算指标');
  const closes = bars.map((b) => b.c);
  const highs = bars.map((b) => b.h);
  const lows = bars.map((b) => b.l);
  const i = bars.length - 1;
  const m = macd(closes);
  const k = kdj(highs, lows, closes);
  const r6 = rsi(closes, 6);
  const r12 = rsi(closes, 12);
  const r24 = rsi(closes, 24);
  const bd = boll(closes, 20, 2);
  const ma5 = sma(closes, 5);
  const ma20 = sma(closes, 20);
  const ma60 = sma(closes, 60);
  const pctB = bd.upper[i] && bd.lower[i] ? (closes[i] - bd.lower[i]) / (bd.upper[i] - bd.lower[i]) : null;
  const lastCross = (() => {
    for (let j = i; j > Math.max(1, i - 20); j -= 1) {
      const a = m.dif[j - 1] - m.dea[j - 1];
      const b = m.dif[j] - m.dea[j];
      if (a === null || b === null) break;
      if (a <= 0 && b > 0) return { kind: '金叉', at: bars[j].t, ago: i - j };
      if (a >= 0 && b < 0) return { kind: '死叉', at: bars[j].t, ago: i - j };
    }
    return null;
  })();
  const t = profile?.technicals || {};
  const vol20 = t.volatility_annual_pct?.['20d'];
  const stat = rangeStats(bars, Math.max(0, bars.length - 60), i);

  const tile = (label, value, note, cls = '') => h('div', { class: 'kpi', style: { border: '1px solid var(--line-soft)' } },
    h('div', { class: 'kpi-l' }, label),
    h('div', { class: `kpi-v ${cls}`.trim(), style: { fontSize: '13px' } }, value),
    h('div', { class: 'kpi-s' }, note));

  const maAlign = ma5[i] > ma20[i] && ma20[i] > ma60[i] ? '多头排列' : ma5[i] < ma20[i] && ma20[i] < ma60[i] ? '空头排列' : '缠绕';
  return [
    tile('均线', `MA5 ${fp(ma5[i])}`, `MA20 ${fp(ma20[i])} · MA60 ${fp(ma60[i])}`, '' ),
    tile('均线形态', maAlign, `收盘 ${fp(closes[i])} 相对 MA20 ${pctPts(ma20[i] ? (closes[i] / ma20[i] - 1) * 100 : null)}`, maAlign === '多头排列' ? 'up' : maAlign === '空头排列' ? 'down' : ''),
    tile('MACD', `DIF ${num2(m.dif[i])} / DEA ${num2(m.dea[i])}`, lastCross ? `${lastCross.kind} @ ${ymd(lastCross.at)}（${lastCross.ago} 根前）` : '近 20 根无交叉', m.hist[i] > 0 ? 'up' : 'down'),
    tile('KDJ', `K ${num2(k.k[i])} D ${num2(k.d[i])} J ${num2(k.j[i])}`, k.j[i] > 100 ? 'J>100 超买区' : k.j[i] < 0 ? 'J<0 超卖区' : '中性区'),
    tile('RSI', `${num2(r6[i])} / ${num2(r12[i])} / ${num2(r24[i])}`, '周期 6 / 12 / 24；>80 超买 <20 超卖'),
    tile('BOLL %B', ratio(pctB, 2), `上 ${fp(bd.upper[i])} · 中 ${fp(bd.mid[i])} · 下 ${fp(bd.lower[i])}`),
    tile('ATR20', `${num2(t.atr20)} / ${pctPts(t.atr20_pct)}`, '价格单位与占比分开：别把两个口径混着减'),
    tile('区间位置', ratio(stat ? (closes[i] - stat.low) / (stat.high - stat.low) : null, 2), `60 日 高 ${fp(stat?.high)} 低 ${fp(stat?.low)} · 年化波动 ${ratio(vol20, 1)}`),
  ];
}

function num2(v) { return Number.isFinite(v) ? v.toFixed(2) : '—'; }

// ------------------------------------------------------------------ 侧栏块
function tradeBlock(profile, quote, equity, ctx) {
  const t = profile.tradability || {};
  // 不写 `Number(min_ticket_cny || 0)`：缺一手成本时会算出"占净值 0.0%"，
  // 与"这只票便宜到可以忽略"无法区分。缺就是缺。
  const lotRaw = t.min_ticket_cny;
  const lotCost = lotRaw === null || lotRaw === undefined ? null : Number(lotRaw);
  const share = Number.isFinite(lotCost) && equity > 0 ? lotCost / equity : null;
  const depth = quote?.depth;
  return h('div', { class: 'stack', style: { gap: '9px' } },
    kv([
      ['最新', fp(t.last)],
      ['昨收', fp(t.pre_close)],
      ['涨停 / 跌停', `${fp(t.up_limit)} / ${fp(t.down_limit)}`],
      ['距涨停', pctPts(t.to_up_limit_pct)],
      ['一手（100 股）', Number.isFinite(lotCost) ? money(lotCost) : '—'],
      ['占净值', ratioWith(share, { num: lotCost, den: equity, digits: 1 })],
      ['成交额', cny(t.amount_cny)],
      ['状态', t.suspended ? chip('停牌', 'bad') : chip('正常', 'ok')],
    ]),
    depth
      ? h('div', { class: 'section' },
        sectionHeader('五档'),
        kv([
          ['买一', `${fp(depth.bid1)} × ${num(depth.bid1_volume)} 手`],
          ['卖一', `${fp(depth.ask1)} × ${num(depth.ask1_volume)} 手`],
          ['委比', pctPts(depth.wb_ratio_pct)],
          ['封板', zh.limit(depth.limit_state || 'none')],
        ]))
      : null,
    share !== null && share > 0.5
      ? h('div', { class: 'micro', style: { color: 'var(--warning)', lineHeight: '1.6' } },
        `一手就占净值 ${ratio(share, 0)}，超过单票上限 50% —— 这只票在这个账户规模上买不到合规仓位。`)
      : null);
}

function valBlock(profile) {
  const v = profile.valuation || {};
  if (!v.available) return empty('这只票没有估值数据', v.note || '');
  const cur = v.current || {};
  const pct_ = v.percentile_in_window || {};
  const med = v.median_in_window || {};
  const rows = [
    ['PE(TTM)', cur.pe_ttm, pct_.pe_ttm, med.pe_ttm],
    ['PB', cur.pb, pct_.pb, med.pb],
    ['PS(TTM)', cur.ps_ttm, pct_.ps_ttm, med.ps_ttm],
    ['股息率 %', cur.dividend_yield_ttm_pct, pct_.dv_ttm, med.dv_ttm],
    ['换手率 %', cur.turnover_rate_pct, null, null],
  ];
  return h('div', { class: 'stack', style: { gap: '8px' } },
    rows.map(([label, value, p, median]) => h('div', { class: 'gauge' },
      h('div', { class: 'row small' },
        h('span', { class: 'muted' }, label),
        h('span', { class: 'spacer' }),
        h('span', { class: 'num strong' }, value === null || value === undefined ? '—' : `${num2(value)}`),
        median !== null && median !== undefined ? h('span', { class: 'num faint micro' }, `中位 ${num2(median)}`) : null),
      p !== null && p !== undefined
        ? h('div', { class: 'gauge-track' },
          h('div', { class: 'gauge-dot', style: { left: `${Math.max(0, Math.min(1, p)) * 100}%` } }),
          h('div', { class: 'gauge-scale' }, h('span', {}, '低'), h('span', {}, `${(p * 100).toFixed(0)}% 分位`), h('span', {}, '高')))
        : h('div', { class: 'micro faint' }, '无分位样本'))),
    h('div', { class: 'micro faint' }, `窗口 ${v.window_sessions} 日（自 ${ymd(v.window_start)}）· ${v.direction?.pe_ttm || ''}`));
}

function techBlock(profile) {
  const t = profile.technicals || {};
  if (!t.available) return empty('没有技术面数据');
  const ret = t.return_pct || {};
  const ma = t.ma || {};
  return h('div', { class: 'stack', style: { gap: '8px' } },
    kv([
      ['5 日', pctPts(ret['5d'])],
      ['20 日', pctPts(ret['20d'])],
      ['60 日', pctPts(ret['60d'])],
      ['250 日', pctPts(ret['250d'])],
    ]),
    h('div', { class: 'section' },
      sectionHeader('均线（价格偏离）'),
      kv([
        ['MA20', `${fp(ma.ma20?.value)} · ${pctPts(ma.ma20?.price_vs_pct)}`],
        ['MA60', `${fp(ma.ma60?.value)} · ${pctPts(ma.ma60?.price_vs_pct)}`],
        ['MA200', `${fp(ma.ma200?.value)} · ${pctPts(ma.ma200?.price_vs_pct)}`],
      ])),
    h('div', { class: 'section' },
      sectionHeader('波动'),
      kv([
        ['年化 20d', t.volatility_annual_pct?.['20d'] == null ? '—' : ratio(t.volatility_annual_pct['20d'] / 100, 1)],
        ['年化 60d', t.volatility_annual_pct?.['60d'] == null ? '—' : ratio(t.volatility_annual_pct['60d'] / 100, 1)],
        ['ATR20', `${num2(t.atr20)} 元`],
        ['250 日位置', ratio(t.range_250d?.position, 2)],
      ])));
}

function rsBlock(profile) {
  const rs = profile.relative_strength || {};
  if (!rs.available) return empty('没有同业对比', '行业快照缺失时这一块不给数。');
  const w = rs.window || {};
  const bar = (label, row) => h('div', { class: 'stack', style: { gap: '3px' } },
    h('div', { class: 'row small' },
      h('span', { class: 'muted' }, label),
      h('span', { class: 'spacer' }),
      h('span', { class: `num ${tone(row.vs_industry_pp)}` }, `vs 行业 ${pctPts(row.vs_industry_pp)}`),
      h('span', { class: `num ${tone(row.vs_market_pp)}`, style: { marginLeft: '8px' } }, `vs 市场 ${pctPts(row.vs_market_pp)}`)),
    h('div', { class: 'micro faint' }, `本票 ${pctPts(row.stock_pct)} · ${rs.industry} ${pctPts(row.industry_pct)} · 市场 ${pctPts(row.market_pct)}`));
  return h('div', { class: 'stack', style: { gap: '9px' } },
    Object.entries(w).map(([k, row]) => bar(k, row)),
    h('div', { class: 'micro faint' }, `对比组 ${rs.level} · ${rs.peer_count} 只`));
}

function finBlock(profile) {
  const f = profile.fundamentals || {};
  if (!f.available) return empty('没有财务数据', f.note || '');
  const m = f.metrics || {};
  const labels = f.labels || {};
  const pick = ['roe_waa', 'grossprofit_margin', 'netprofit_margin', 'or_yoy', 'netprofit_yoy', 'debt_to_assets'];
  const trend = (f.trend || []).slice(-6);
  return h('div', { class: 'stack', style: { gap: '8px' } },
    h('div', { class: 'row small' },
      h('span', { class: 'muted' }, `${ymd(f.latest_period)} 报告期`),
      h('span', { class: 'spacer' }),
      h('span', { class: 'micro faint' }, `披露 ${ymd(f.announced_on)} · ${f.days_since_announcement} 天前`)),
    kv(pick.map((k) => [labels[k] || k, m[k] === null || m[k] === undefined ? '—' : num2(m[k])])),
    trend.length
      ? h('div', { class: 'section' },
        sectionHeader('ROE 趋势（YTD %）'),
        h('div', { class: 'row-tight', style: { alignItems: 'flex-end', height: '34px', gap: '3px' } },
          trend.map((p) => {
            const v = Number(p.roe_ytd || 0);
            const max = Math.max(...trend.map((x) => Number(x.roe_ytd || 0)), 1);
            return h('div', {
              title: `${ymd(p.period)} ROE ${num2(p.roe_ytd)}%`,
              style: {
                flex: '1', height: `${Math.max(3, (v / max) * 32)}px`,
                background: 'rgba(88,166,255,.35)', borderTop: '1px solid var(--accent)',
              },
            });
          })),
        h('div', { class: 'micro faint' }, f.basis || ''))
      : null);
}

function holdBlock(profile) {
  const o = profile.ownership || {};
  const ev = profile.events || {};
  const holders = o.holders || {};
  const top10 = o.top10_float || {};
  const pledge = o.pledge || {};
  const unlocks = o.unlocks || {};
  const actions = ev.corporate_actions || [];
  const anns = ev.announcements || [];
  return h('div', { class: 'stack', style: { gap: '9px' } },
    h('div', { class: 'section' }, sectionHeader('股东'),
      kv([
        ['户数', holders.available ? `${num(holders.holder_count)}（${pct(holders.change_pct)}）` : '—'],
        ['前十大流通', top10.available ? ratio(top10.combined_float_pct / 100, 2) : '—'],
        ['北向', top10.northbound_holder ? ratio(top10.northbound_holder.float_pct / 100, 2) : '—'],
        ['质押', pledge.available ? ratio(pledge.pledge_ratio_pct / 100, 2) : '—'],
        ['解禁', unlocks.available ? '有记录' : '无记录'],
      ])),
    h('div', { class: 'section' }, sectionHeader('分红'),
      actions.length
        ? kv(actions.slice(0, 2).map((a) => [ymd(a.ex_date), `每股 ${num2(a.cash_per_share)} 元`]))
        : h('div', { class: 'micro faint' }, '区间内没有分红记录')),
    h('div', { class: 'section' }, sectionHeader('公告'),
      anns.length
        ? h('div', { class: 'stack', style: { gap: '5px' } },
          anns.slice(0, 3).map((a) => h('a', {
            href: a.pdf_url || '#', target: '_blank', rel: 'noopener',
            class: 'small', style: { color: 'var(--ink-mid)', lineHeight: '1.5' },
            title: a.title,
          }, `${ymd(a.pub_date)} · ${truncate(a.title, 28)}`)))
        : h('div', { class: 'micro faint' }, '没有公告记录')));
}

function truncate(s, n) {
  const t = String(s || '');
  return t.length > n ? `${t.slice(0, n)}…` : t;
}

/** 成交测算：内核的估计，不是承诺。价格来源必须写明。 */
function calcBlock(profile, equity, ctx) {
  const qtyInput = h('input', {
    class: 'input input-num', value: '100', style: { width: '78px' },
    'aria-label': '数量（股）',
  });
  const out = h('div', {});
  const run = async () => {
    const raw = qtyInput.value.trim();
    const qty = Math.round(Number(raw));
    if (!raw || !Number.isInteger(qty) || qty < 1) {
      out.replaceChildren(h('div', { class: 'micro', style: { color: 'var(--al-missing-ink)' } }, '先填股数（必须是正整数）。'));
      return;
    }
    out.replaceChildren(h('div', { class: 'micro faint' }, '测算中…'));
    try {
      const d = await api(`/execution/estimate${qs({ symbol: profile.symbol, side: 'BUY', qty })}`, { ttl: 0 });
      out.replaceChildren(kv([
        ['参考价', `${fp(d.price)}（${d.price_source === 'server_last' ? '最近收盘' : d.price_source}）`],
        ['参与率', ratio(d.participation, 2)],
        ['单边冲击', `${num2(d.impact_bps)} bps`],
        // cost_cny 是**冲击成本**（participation × impact），不是手续费：
        // 100 股在 ADV 里几乎为零，算出来是 ¥0.00 完全正确。标成"费用"
        // 会让人以为这笔买入零成本，而实际还有佣金（最低 5 元）与过户费。
        ['冲击成本', money(d.cost_cny)],
        ['需要', `${d.days_needed} 天`],
      ]));
    } catch (err) {
      out.replaceChildren(errorBox(err));
    }
  };
  qtyInput.addEventListener('change', run);
  setTimeout(run, 0);
  return h('div', { class: 'stack', style: { gap: '9px' } },
    h('div', { class: 'row' }, h('span', { class: 'small muted' }, '股数'), qtyInput,
      h('button', { class: 'btn btn-sm', onclick: run }, '测算')),
    out,
    h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
      profile.tradability?.min_lot ? `${profile.tradability.min_lot} 股整手 · ` : '',
      '参与率 > 30% 时内核拆成多日 TWAP —— 不要把一日成交当成可行。'),
    h('button', {
      class: 'btn btn-primary', style: { width: '100%' },
      onclick: () => ctx.navigate('decide', { symbol: profile.symbol }),
    }, '用这只票提案'));
}



// ------------------------------------------------------------------ 无代码时的选择器
async function picker(ctx) {
  const root = h('div', { class: 'stack' });
  const input = h('input', { class: 'input', placeholder: '输入代码或名称，回车搜索', style: { width: '320px' } });
  const out = h('div', {});
  const doSearch = async () => {
    const q = input.value.trim();
    if (!q) return;
    out.replaceChildren(h('div', { class: 'empty' }, '查询中…'));
    try {
      const data = await api(`/universe${qs({ query: q, limit: 20, order: 'amount' })}`, { ttl: 0 });
      const rows = data.rows || [];
      out.replaceChildren(rows.length
        ? table({
          cls: 'tbl-click',
          cols: [
            { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
            { key: 'name', label: '名称' },
            { key: 'industry', label: '行业' },
            { key: 'close', label: '收盘', cls: 'r', render: (r) => fp(r.close) },
            { key: 'ret_20d', label: '20日', cls: 'r', render: (r) => `<span style="${toneStyle(r.ret_20d)}">${pct(r.ret_20d)}</span>` },
            { key: 'amount_cny', label: '成交额', cls: 'r', render: (r) => cny(r.amount_cny) },
          ],
          rows,
          onRow: (r) => ctx.navigate(`s/${r.symbol}`),
        })
        : empty('没有匹配的标的', '换一个代码或名称；北交所可以看盘但不能开新仓。'));
    } catch (err) {
      out.replaceChildren(errorBox(err));
    }
  };
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') doSearch(); });
  root.append(
    panel({
      title: '选一只票',
      note: '本地 5,379 只宇宙',
      body: h('div', { class: 'stack', style: { gap: '10px' } },
        h('div', { class: 'row' }, input, h('button', { class: 'btn', onclick: doSearch }, '搜索')),
        h('div', { class: 'micro faint' }, '也可以直接访问 #/stock/600519.SH；或在盘面页点任意一行。')),
    }),
    panel({ title: '结果', body: out }),
  );
  setTimeout(() => input.focus(), 40);
  return root;
}
