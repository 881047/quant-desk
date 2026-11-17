// 盘面：今天市场的结构，不是"荐股"。
// 三池 + 闸门（四态）、申万一级、宽度，以及可筛选的 5,379 只宇宙。

import { api, qs } from '../api.js';
import {
  h, panel, chip, table, empty, errorBox, slot, kv, sectionHeader,
} from '../ui.js';
import {
  money, pct, pctPts, ratio, cny, ymd, num, tone, toneStyle, fp2, esc, fmtByUnits,
} from '../fmt.js';
import { zh } from '../zh.js';
import { gatesBlock } from '../gates.js';

const state = {
  industry: '',
  query: '',
  order: 'amount',
  root: null,
};

export async function mount(ctx) {
  state.industry = ctx.params.industry || '';
  // 顶栏搜索框输中文名会落到这里（app.js 只认 6 位代码），q 必须进筛选框，
  // 否则"落到盘面页但什么都没发生"——用户会以为搜索坏了。
  state.query = ctx.params.q || '';
  const root = h('div', { class: 'stack' });
  state.root = root;

  const head = h('div', { class: 'grid g2' });
  const pools = panel({
    title: '三池与闸门',
    note: '闸门是四态：过 / 不过 / 无法判断',
    body: h('div', { id: 'pools' }),
    footer: '家数取派生池（close == up_limit，逐只可核对）；封单、首封时间只能用东财。',
  });
  const breadth = panel({ title: '宽度与状态', note: 'regime / 事实', body: h('div', { id: 'breadth' }) });
  head.append(pools, breadth);
  root.append(head);

  const sector = panel({
    title: '申万一级 · 31',
    note: '市值加权 / 等权 / 主力净流入',
    body: h('div', { id: 'sector-all' }),
    footer: '口径：涨跌幅按流通市值加权；pct_chg_eq 是等权，两者差得远说明是大票拉着指数走。',
  });
  root.append(sector);

  const browseHost = h('div', {});
  const browse = panel({
    title: '个股浏览',
    note: '筛选不是推荐，排序只为显示',
    actions: h('div', { class: 'row-tight' }, h('span', { class: 'micro faint', id: 'uni-count' }, '')),
    body: browseHost,
  });
  root.append(browse);
  buildBrowser(browseHost, ctx);

  slot(root.querySelector('#pools'), () => poolsBlock(ctx), { lines: 6 });
  slot(root.querySelector('#breadth'), () => breadthBlock(), { lines: 5 });
  slot(root.querySelector('#sector-all'), () => sectorAll(ctx), { lines: 12 });

  return root;
}

export function unmount() { state.root = null; }

function poolsBlock(ctx) {
  return api(`/market/pools${qs({ limit: 12 })}`, { ttl: 30000 }).then((d) => {
    // 成功响应里没有 ok 字段（只有失败分支才带 ok=false）。用 !d.ok 会把有数报成没数。
    if (d.ok === false || !d.sentiment) {
      // ok=false 不是"今天没有涨停"：要么三池没入库，要么管道没跑。原文照抄。
      return h('div', { class: 'stack', style: { gap: '8px' } },
        h('div', { class: 'err' },
          h('div', { class: 'code' }, zh.code('POOLS_MISSING')),
          h('div', {}, d.error || '三池没有数据'),
          d.alternative ? h('div', { class: 'next' }, d.alternative) : null),
        gatesBlock(d.gates));
    }
    const s = d.sentiment || {};
    const g = d.gates || {};
    const counts = d.counts || {};
    // 三柱的数字跟 counts 走；counts 缺了才退回 sentiment 的同名字段。
    const basis = counts.source || s.counts_source || '';
    const max = Math.max(s.limit_up || 0, s.broken_board || 0, s.limit_down || 0, 1);
    return h('div', { class: 'stack', style: { gap: '10px' } },
      // 高度全部交给 .triad / .triad-bar（app.css），与今日页同一套几何
      h('div', { class: 'triad' },
        triCol('涨停', counts.limit_up ?? s.limit_up, max, 'up'),
        triCol('炸板', counts.broken_board ?? s.broken_board, max, 'mid'),
        triCol('跌停', counts.limit_down ?? s.limit_down, max, 'down')),
      h('div', { class: 'chips' },
        chip(`最高 ${s.max_boards ?? '—'} 板`),
        chip(`封单 ${cny(s.seal_amount_cny)}`),
        chip(`炸板率 ${fmtByUnits(d.units, 'break_rate', s.break_rate, 2, { signed: false, endpoint: '/market/pools' })}`, s.break_rate > 0.4 ? 'warn' : ''),
        chip(`语气 ${s.tone || '—'}`, s.tone === '退潮' ? 'warn' : ''),
        chip(d.pool_state?.settled ? '收盘终值' : '盘中快照', d.pool_state?.settled ? 'ok' : 'warn'),
        // 三柱与炸板率来自派生池（counts.source），三池明细来自厂商。标签必须
        // 跟着数字走：写"东方财富"在 33 旁边，会和总览的 32 看起来像数据打架。
        chip(basis ? `口径 ${zh.vendor(basis)}` : '口径 —', 'off'),
        counts.eastmoney
          ? h('span', {
            class: 'chip off',
            title: '同一交易日的厂商收盘数。与派生池的差是计数方法：派生池把盘中触及涨停价的都算炸板，厂商按收盘封板状态算',
          }, `东财 ${counts.eastmoney.limit_up} / ${counts.eastmoney.broken_board} / ${counts.eastmoney.limit_down}`)
          : null),
      gatesBlock(g),
      poolsTable(d.pools?.limit_up || [], ctx, d.units),
      h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
        `as_of ${ymd(d.as_of)} · ${d.age_seconds === null || d.age_seconds === undefined ? '池子龄 —' : `池子龄 ${Math.round(d.age_seconds / 60)} 分钟`} · `,
        d.stale ? h('span', { style: { color: 'var(--al-stale)' } }, '已过期') : h('span', {}, '新鲜')));
  });
}

function triCol(label, n, max, kind) {
  return h('div', { class: 'triad-col' },
    h('div', { class: `triad-n ${kind === 'up' ? 'up' : kind === 'down' ? 'down' : 'flat'}` }, num(n ?? '—')),
    // 56 是 .triad 的可用柱高（84 − 数字行 − 标签行），与今日页共用同一个刻度
    h('div', { class: `triad-bar ${kind}`, style: { height: `${Math.max(3, ((n || 0) / max) * 56)}px` } }),
    h('div', { class: 'triad-l' }, label));
}

// 闸门渲染统一走 ../gates.js 的 gatesBlock()：8px 实心方 + 填充量，零色相。
// 这里原来有一套本地实现，用 chip 的 'bad'（故障紫）表示"不过"—— 紫是系统故障通道，
// 闸门不过不是故障，而且和今日页对同一个 gates 对象画出了两种样子。

function poolsTable(rows, ctx, units) {
  if (!rows.length) return empty('涨停池为空', '要么今天没有涨停，要么池子没入库 —— 看上面的错误说明判断是哪一种。');
  return table({
    cls: 'tbl-click',
    cols: [
      { key: 'code', label: '代码', render: (r) => `<span class="sym">${esc(r.code || r.symbol || '')}</span>` },
      { key: 'name', label: '名称' },
      { key: 'boards', label: '板', cls: 'r', render: (r) => `${r.boards ?? r.consecutive ?? '—'}` },
      // 量纲由 d.units 单点声明，fmtByUnits 决定要不要 ×100；不在页面里记字段口径。
      { key: 'seal_ratio_pct', label: '封单/流通', cls: 'r', render: (r) => fmtByUnits(units, 'seal_ratio_pct', r.seal_ratio_pct, 3, { signed: false, endpoint: '/market/pools' }) },
      { key: 'seal_window_text', label: '首封', cls: 'r', render: (r) => esc(r.seal_window_text || '—') },
      { key: 'lot_cost', label: '一手', cls: 'r', render: (r) => money(r.lot_cost_cny) },
      { key: 'pct', label: '占净值', cls: 'r', render: (r) => fmtByUnits(units, 'pct_of_equity', r.pct_of_equity, 1, { endpoint: '/market/pools' }) },
    ],
    rows: rows.slice(0, 12),
    onRow: (r) => ctx.navigate(`s/${r.code || r.symbol}`),
  });
}

function breadthBlock() {
  return Promise.all([
    api('/facts', { ttl: 30000 }),
    api('/regime', { ttl: 60000 }),
  ]).then(([f, r]) => {
    const b = f.breadth || {};
    const total = (b.advancers || 0) + (b.decliners || 0) + (b.flat || 0);
    const share = (n) => (total ? n / total : 0);
    return h('div', { class: 'stack', style: { gap: '10px' } },
      // 宽度条：涨/平/跌 三段，宽度按家数
      h('div', { style: { display: 'flex', height: '10px', borderRadius: '1px', overflow: 'hidden' } },
        h('div', { style: { width: `${share(b.advancers) * 100}%`, background: 'var(--px-up)' }, title: `涨 ${b.advancers}` }),
        h('div', { style: { width: `${share(b.flat) * 100}%`, background: 'var(--px-flat)' }, title: `平 ${b.flat}` }),
        h('div', { style: { width: `${share(b.decliners) * 100}%`, background: 'var(--px-down)' }, title: `跌 ${b.decliners}` })),
      h('div', { class: 'row small num' },
        h('span', { class: 'up' }, `涨 ${num(b.advancers)}`),
        h('span', { class: 'flat' }, `平 ${num(b.flat)}`),
        h('span', { class: 'down' }, `跌 ${num(b.decliners)}`),
        h('div', { class: 'spacer' }),
        h('span', { class: 'faint' }, `等权 ${fmtByUnits(f.units, 'equal_weight_return', b.equal_weight_return, 2, { endpoint: '/facts' })} · 中位 ${fmtByUnits(f.units, 'median_return', b.median_return, 2, { endpoint: '/facts' })}`)),
      h('div', { class: 'section' },
        sectionHeader('状态'),
        kv([
          ['市场状态', chip(r.label ? zh.regime(r.label) : '—', r.label === 'stressed' ? 'warn' : '')],
          ['趋势', zh.trend(r.trend)],
          ['波动状态', zh.vol(r.vol_state)],
          ['20日波动分位', fmtByUnits(r.units, 'vol_percentile_1y', r.vol_percentile_1y, 0, { signed: false, endpoint: '/regime' })],
          ['MA20 上方占比', fmtByUnits(r.units, 'breadth_above_ma20', r.breadth_above_ma20, 1, { signed: false, endpoint: '/regime' })],
        ])),
      r.reasons?.length
        ? h('div', { class: 'micro faint', style: { lineHeight: '1.6' } }, r.reasons.join(' · '))
        : null,
      h('div', { class: 'micro faint' }, '「市场状态」是三项阈值的组合，是约定，不是市场事实。'));
  });
}

function sectorAll(ctx) {
  return api('/market/sector?limit=31', { ttl: 60000 }).then((d) => {
    const rows = d.industries || [];
    if (!rows.length) return empty('行业榜为空');
    const max = Math.max(...rows.map((x) => Math.abs(x.pct_chg || 0)), 0.01);
    return h('div', { class: 'heat-rows' }, rows.map((r) => {
      const v = Number(r.pct_chg || 0);
      const w = (Math.abs(v) / max) * 50;
      const active = state.industry && state.industry === r.sw_l1;
      return h('div', {
        class: 'heat-row',
        style: active ? { background: 'var(--accent-soft)' } : undefined,
        onclick: () => { state.industry = r.sw_l1; ctx.navigate('market', { industry: r.sw_l1 }); },
        title: `${r.sw_l1} · ${r.members} 只（成交 ${r.traded}）`,
      },
        h('div', { class: 'heat-name' }, r.sw_l1),
        h('div', { class: 'heat-track' },
          h('div', { class: 'heat-zero' }),
          h('div', {
            class: 'heat-fill',
            style: v >= 0 ? { left: '50%', width: `${w}%`, background: 'rgba(255,90,99,.55)' }
              : { right: '50%', width: `${w}%`, background: 'rgba(33,201,124,.55)' },
          })),
        h('div', { class: `heat-val ${tone(v)}` }, pctPts(v)),
        h('div', { class: 'heat-flow', title: '主力净流入' }, cny(r.main_net_inflow_cny)));
    }));
  });
}

// ------------------------------------------------------------------ 浏览
function buildBrowser(host, ctx) {
  const search = h('input', { class: 'input', placeholder: '代码 / 名称', style: { width: '180px' }, value: state.query });
  const order = h('select', { class: 'select' },
    h('option', { value: 'amount' }, '按成交额'),
    h('option', { value: 'move' }, '按涨跌幅'),
    h('option', { value: 'code' }, '按代码'));
  const minAmt = h('select', { class: 'select' },
    h('option', { value: '0' }, '成交额不限'),
    h('option', { value: '100000000' }, '> 1 亿'),
    h('option', { value: '500000000' }, '> 5 亿'),
    h('option', { value: '2000000000' }, '> 20 亿'));
  const industry = h('input', { class: 'input', placeholder: '行业（申万一级）', style: { width: '140px' }, value: state.industry });
  const tableHost = h('div', {});
  const countEl = h('span', { class: 'micro faint' });

  const load = async () => {
    tableHost.replaceChildren(skeletonTable());
    try {
      const d = await api(`/universe${qs({
        query: search.value.trim(), industry: industry.value.trim(),
        order: order.value, min_amount: minAmt.value, limit: 100,
      })}`, { ttl: 30000 });
      countEl.textContent = `${num(d.returned)} / ${num(d.universe_size)} 只 · ${ymd(d.as_of)}`;
      tableHost.replaceChildren(table({
        cls: 'tbl-click',
        cols: [
          { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
          { key: 'name', label: '名称', render: (r) => esc(r.name) },
          { key: 'industry', label: '行业', render: (r) => esc(r.sw_l1 || r.industry || '—') },
          { key: 'close', label: '收盘', cls: 'r', render: (r) => fp2(r.close) },
          { key: 'ret_1d', label: '1日', cls: 'r', render: (r) => `<span style="${toneStyle(r.ret_1d)}">${pct(r.ret_1d)}</span>` },
          { key: 'ret_20d', label: '20日', cls: 'r', render: (r) => `<span style="${toneStyle(r.ret_20d)}">${pct(r.ret_20d)}</span>` },
          { key: 'amount_cny', label: '成交额', cls: 'r', render: (r) => cny(r.amount_cny) },
          { key: 'turnover', label: '换手', cls: 'r', render: (r) => r.turnover === null ? '—' : `${fp2(r.turnover)}%` },
          { key: 'range_pos_20d', label: '20日位置', cls: 'r', render: (r) => ratio(r.range_pos_20d, 2) },
          { key: 'tradeable', label: '可交易', cls: 'r', render: (r) => r.tradeable ? chip('是', 'ok') : chip('否', 'off') },
        ],
        rows: d.rows || [],
        onRow: (r) => ctx.navigate(`s/${r.symbol}`),
      }));
    } catch (err) {
      tableHost.replaceChildren(errorBox(err));
    }
  };

  for (const el of [search, industry]) {
    el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') load(); });
  }
  order.addEventListener('change', load);
  minAmt.addEventListener('change', load);

  host.replaceChildren(
    h('div', { class: 'row', style: { padding: '8px 10px', borderBottom: '1px solid var(--line)', flexWrap: 'wrap' } },
      search, industry, minAmt, order,
      h('button', { class: 'btn btn-sm', onclick: load }, '筛选'),
      h('div', { class: 'spacer' }), countEl),
    tableHost,
  );
  load();
}

function skeletonTable() {
  return h('div', { style: { padding: '10px' }, class: 'stack' },
    Array.from({ length: 8 }, () => h('div', { class: 'skeleton', style: { height: '14px' } })));
}
