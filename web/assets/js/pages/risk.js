// 持仓与风险：这个账户现在暴露在什么之下。
// 没有持仓时它也必须有用 —— 额度、止损闸门、开关，都是"空仓也可能违约"的东西。

import { api, post } from '../api.js';
import {
  h, panel, kpis, chip, table, empty, slot, kv, meterRow, confirmAction, toast,
  ratioWith,
} from '../ui.js';
import { money, pct, ratio, num, tone, toneStyle, fp2, esc, fmtByUnits } from '../fmt.js';
import { zh } from '../zh.js';

export async function mount(ctx) {
  const root = h('div', { class: 'stack' });
  const head = h('div', {});
  const body = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  const col1 = h('div', { class: 'stack' });
  const col2 = h('div', { class: 'stack' });
  body.append(col1, col2);
  root.append(head, body);

  const portP = api('/portfolio', { ttl: 8000 });
  const riskP = api('/risk', { ttl: 8000 });

  slot(head, async () => {
    const [p, r] = await Promise.all([portP, riskP]);
    return kpis([
      { label: '净值', value: money(p.equity), sub: `峰值 ${money(p.peak_equity)}` },
      { label: '可用现金', value: money(p.cash_free), sub: `冻结 ${money(p.cash_frozen)}` },
      { label: '持仓市值', value: money(p.market_value), sub: `${p.position_count} 只` },
      { label: '浮动盈亏', value: money(p.unrealized_pnl), sub: `已实现 ${money(p.realized_pnl)}`, cls: tone(p.unrealized_pnl) },
      { label: '回撤', value: ratio(Math.abs(p.drawdown), 2), sub: `半仓线 ${ratio(0.08, 0)} · 停手线 ${ratio(0.12, 0)}`, cls: p.drawdown <= -0.08 ? 'down' : '' },
      { label: '当日', value: fmtByUnits(p.units, 'daily_pnl_pct', p.daily_pnl_pct, 2, { endpoint: '/portfolio' }), sub: `成交 ${p.fills} 笔`, cls: tone(p.daily_pnl_pct) },
    ]);
  }, { lines: 2 });

  // 止损面板留个引用：空仓时它只会说"没有持仓，所以没有止损"，一张卡换一句话不值得。
  const stopsPanel = panel({ title: '止损', note: '/risk/stops', body: h('div', { id: 'stops' }) });
  col1.append(
    panel({ title: '持仓', note: 'T+1 可卖数量单独一列', body: h('div', { id: 'positions' }), flush: true }),
    stopsPanel,
    panel({ title: '暴露', note: '权重分母含现金', body: h('div', { id: 'exposure' }) }),
  );
  col2.append(
    panel({ title: '额度使用', note: '限额来自 /risk', body: h('div', { id: 'limits' }) }),
    panel({ title: '开关', note: '会写账本，需要二次确认', body: h('div', { id: 'switches' }) }),
    panel({ title: '观察层', note: '约束与偏离，不是建议', body: h('div', { id: 'obs' }) }),
  );

  slot(root.querySelector('#positions'), async () => {
    const p = await portP;
    if (!p.positions?.length) {
      return h('div', { class: 'empty' },
        h('b', {}, '空仓 100% 现金'),
        h('div', {}, `净值 ${money(p.equity)}，可用 ${money(p.cash_free)}。空仓是合法答案，不是待办。`));
    }
    return table({
      cols: [
        { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
        { key: 'name', label: '名称' },
        { key: 'qty', label: '持仓', cls: 'r', render: (r) => num(r.total_qty) },
        { key: 'avail', label: '可卖', cls: 'r', render: (r) => `${num(r.available_qty)}${r.available_qty < r.total_qty ? ' 🔒' : ''}` },
        { key: 'cost', label: '成本', cls: 'r', render: (r) => fp2(r.avg_cost) },
        { key: 'last', label: '现价', cls: 'r', render: (r) => fp2(r.last) },
        { key: 'pnl', label: '浮盈', cls: 'r', render: (r) => `<span style="${toneStyle(r.unrealized_pnl)}">${money(r.unrealized_pnl)}</span>` },
        { key: 'weight', label: '权重', cls: 'r', render: (r) => ratio(r.weight, 1) },
      ],
      rows: p.positions,
    });
  }, { lines: 3 });

  slot(root.querySelector('#stops'), async () => {
    const s = await api('/risk/stops', { ttl: 8000 });
    const rows = s.stops || [];
    if (!rows.length && !s.blocked_t1?.length) {
      // 整块收起来，而不是留一张写着"没有止损"的空卡。有 T+1 锁定时必须留着。
      stopsPanel.hidden = true;
      return h('div');   // slot() 会把 null 画成「没有内容」，藏起来的卡里不能留这句
    }
    if (!rows.length) {
      return h('div', { class: 'stack', style: { gap: '6px' } },
        empty(s.note || '没有持仓，所以没有止损'),
        s.blocked_t1?.length ? h('div', { class: 'small', style: { color: 'var(--al-stale)' } }, `T+1 锁定：${s.blocked_t1.join('、')}`) : null);
    }
    return h('div', { class: 'stack', style: { gap: '8px' } },
      table({
        cols: [
          { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
          { key: 'stop', label: '止损', cls: 'r', render: (r) => fp2(r.stop_price ?? r.stop) },
          { key: 'last', label: '收盘', cls: 'r', render: (r) => fp2(r.last) },
          { key: 'dist', label: '距离', cls: 'r', render: (r) => `<span style="${toneStyle(r.distance_pct)}">${pct(r.distance_pct)}</span>` },
          { key: 'below', label: '连续破线', cls: 'r', render: (r) => `${r.sessions_below ?? 0} 日` },
        ],
        rows,
      }),
      s.breached?.length
        ? h('div', { class: 'err', style: { marginTop: '6px' } },
          h('div', { class: 'code', title: 'OPEN_VIOLATION' }, '破线闸门'),
          h('div', {}, `这些票收盘价低于止损：${s.breached.join('、')}`),
          h('div', { class: 'next' }, '内核会拒绝新开仓（409），直到破线解除或持仓退出。'))
        : null);
  }, { lines: 3 });

  slot(root.querySelector('#exposure'), async () => {
    const e = await api('/risk/exposure', { ttl: 8000 });
    const ind = Object.entries(e.by_industry || {});
    return h('div', { class: 'stack', style: { gap: '8px' } },
      // 三项权重的分母都是 equity（后端 notes: "weights are market value / equity"）。
      kv([
        ['已投资', ratioWith(e.invested_weight, {
          num: e.invested_weight === null || e.invested_weight === undefined ? null : e.invested_weight * e.equity,
          den: e.equity, digits: 1 })],
        ['现金', ratioWith(e.cash_weight, {
          num: e.cash_weight === null || e.cash_weight === undefined ? null : e.cash_weight * e.equity,
          den: e.equity, digits: 1 })],
        ['Top1 / Top3', `${ratio(e.concentration?.top1, 1)} / ${ratio(e.concentration?.top3, 1)}`],
        ['HHI', e.concentration?.hhi === null || e.concentration?.hhi === undefined ? '—' : num(e.concentration.hhi, 3)],
        ['beta（等权）', e.beta_vs_equal_weight === null ? h('span', { class: 'mv-missing' }, '不适用') : num(e.beta_vs_equal_weight, 2)],
      ]),
      ind.length
        ? h('div', { class: 'meter' }, ind.map(([k, v]) => meterRow({ label: k, value: v, max: 0.35, text: ratio(v, 1), tone: v > 0.35 ? 'bad' : '' })))
        : h('div', { class: 'micro faint' }, '空仓：没有行业暴露可加权。'));
  }, { lines: 3 });

  slot(root.querySelector('#limits'), async () => {
    const [r, p] = await Promise.all([riskP, portP]);
    const lim = r.limits || {};
    const eq = Number(p.equity);
    const cashW = Number.isFinite(eq) && eq > 0 ? Number(p.cash_free) / eq : null;
    return h('div', { class: 'stack', style: { gap: '10px' } },
      h('div', { class: 'meter' },
        meterRow({ label: '持仓数', value: p.position_count, max: lim.max_positions, text: `${p.position_count}/${lim.max_positions}` }),
        meterRow({
          label: '现金占比', value: cashW ?? 0, max: 1,
          text: ratioWith(cashW, { num: p.cash_free, den: p.equity, digits: 1 }),
          proof: cashW === null ? '净值缺失，占比不可算' : `分子 现金 ${Number(p.cash_free).toFixed(2)} 元 / 分母 净值 ${Number(p.equity).toFixed(2)} 元`,
          tone: cashW === null ? '' : cashW >= lim.min_cash_weight ? 'ok' : 'bad',
        }),
        meterRow({ label: '回撤', value: Math.abs(p.drawdown), max: Math.abs(lim.drawdown_stop), text: ratio(Math.abs(p.drawdown), 2), tone: Math.abs(p.drawdown) >= Math.abs(lim.drawdown_half) ? 'warn' : 'ok' }),
        meterRow({ label: '今日成交', value: p.fills, max: lim.max_actions_per_day, text: `${p.fills}/${lim.max_actions_per_day}` })),
      kv([
        ['单票（核心）', ratio(lim.max_core_single, 0)],
        ['单票（AGG）', ratio(lim.max_aggressive_single, 0)],
        ['行业上限', ratio(lim.industry_cap, 0)],
        ['单笔风险', ratio(lim.max_trade_risk, 0)],
        ['ADV 占比', ratio(lim.max_adv_ratio, 2)],
      ]),
      h('div', { class: 'micro faint' }, '这些是风控拒绝的线，不是建议池。'));
  }, { lines: 4 });

  slot(root.querySelector('#switches'), () => switchBlock(ctx), { lines: 3 });

  slot(root.querySelector('#obs'), async () => {
    const o = await api('/observations', { ttl: 8000 });
    const items = [...(o.constraints || []), ...(o.deviations || [])];
    if (!items.length) return empty('没有约束与偏离', '规则与账本之间没有可执行的差距。');

    const subjectOf = (s) => (s === 'core' ? '核心盘' : s === 'llm' ? '模型盘' : (s || ''));

    const one = (it) => {
      const rail = it.attention === 'high' ? 'var(--al-fault)' : 'var(--line-strong)';
      const head = h('div', { class: 'row small' },
        chip(zh.obs(it.kind), it.attention === 'high' ? 'bad' : 'off'),
        h('span', { class: 'muted' }, subjectOf(it.subject)));
      const rule = h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
        (it.constraints || []).join(' · ') || '—');
      const note = it.note ? h('div', { class: 'micro faint' }, it.note) : null;
      return h('div', {
        class: 'stack',
        style: { gap: '2px', borderLeft: `2px solid ${rail}`, paddingLeft: '8px' },
      }, head, rule, note);
    };

    const budget = o.budget?.regime
      ? zh.regime(o.budget.regime) || o.budget.regime
      : '—';
    const footer = h('div', { class: 'micro faint' },
      `预算：${ratio(o.budget?.allocation?.llm, 0)} 可用（市场状态 ${budget}）`);

    return h('div', { class: 'stack', style: { gap: '8px' } }, ...items.map(one), footer);
  }, { lines: 4 });

  return root;
}

function switchBlock(ctx) {
  return api('/portfolio', { ttl: 4000 }).then((p) => {
    const make = (label, value, key, danger) => {
      const btn = h('button', {
        class: `btn ${value ? (danger ? 'btn-danger' : '') : ''}`.trim(),
        onclick: async () => {
          const ok = await confirmAction({
            title: value ? `关闭 ${label}` : `打开 ${label}`,
            message: value
              ? `关闭后 ${label} 不再拦截交易。这一条会写进账本（actor=human）。`
              : `打开 ${label} 后${key === 'kill_switch' ? '所有新开仓都会被拒绝' : '所有新买入都会被拒绝'}，卖出仍然放行。会写进账本。`,
            confirmText: value ? '关闭' : '打开',
            danger: !value,
          });
          if (!ok) return;
          try {
            await post(`/control/${key.replace('_', '-')}`, { value: !value });
            toast(`${label} 已${value ? '关闭' : '打开'}`, 'ok');
            ctx.refresh?.();
          } catch (err) {
            toast(err.message || '操作失败', 'bad', err.code);
          }
        },
      }, `${label}：${value ? '已拉下' : '正常'}`);
      return btn;
    };
    return h('div', { class: 'stack', style: { gap: '8px' } },
      h('div', { style: { borderLeft: '2px solid var(--al-fault)', paddingLeft: '9px' }, class: 'stack' },
        h('div', { class: 'row' }, h('span', { class: 'small strong' }, '安全态'), h('div', { class: 'spacer' }),
          chip(p.kill_switch ? '总开关 已拉下' : '总开关 正常', p.kill_switch ? 'bad' : 'off')),
        h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
          p.safe_mode ? '安全模式生效中：新买入被拒，卖出放行。' : '安全模式未开启。'),
        h('div', { class: 'row-tight', style: { marginTop: '6px' } },
          make('总开关', p.kill_switch, 'kill_switch', true),
          make('安全模式', p.safe_mode, 'safe_mode', true))));
  });
}
