// 今日：先回答“下一步做什么、闭环走到哪、哪些结论还不能说”。
// 市场结构保留为二级证据；所有面板独立取数、独立降级，不用冻结快照冒充实时数据。

import { api } from '../api.js';
import {
  h, panel, kpis, chip, table, empty, meterRow, slot, kv, seg, drawer, toast,
} from '../ui.js';
import { gatesBlock } from '../gates.js';
import { zh } from '../zh.js';
import {
  money, pct, ratio, cny, ymd, hhmmss, tone, toneStyle, num, esc, fmtByUnits,
} from '../fmt.js';
import { outcomeText, executionRowModel } from './kernel-short-alpha.js';

let root = null;

export async function mount(ctx) {
  root = h('div', { class: 'stack qd-today' });
  const kpiHost = h('div', {});
  const hero = h('section', { class: 'qd-hero', 'aria-label': '今日结论与下一步' });
  const heroMain = h('div', { class: 'qd-hero-main' });
  const nextHost = h('div', { class: 'qd-next-host' });
  hero.append(heroMain, nextHost);

  const loopPanel = panel({
    title: '工作闭环', note: '对象计数，不是转化率',
    body: h('div', { id: 'qd-loop' }),
    footer: 'pending、排队未知、tracking_only 与 null 都不是 0 收益或成交；只有真实状态推进计数。',
  });
  const workbench = h('div', { class: 'qd-workbench' });
  const judgementPanel = panel({
    title: '冻结判断', note: 'candidate / watch 不可事后改写',
    body: h('div', { id: 'qd-judgements' }),
    footer: '点击一行核对 thesis、invalidation、证据 id、执行边界与 T+1/T+2 状态；本页不创建票据。',
  });
  judgementPanel.id = 'qd-judgement-panel';
  const side = h('div', { class: 'stack qd-side' },
    panel({ title: '样本外结算', note: '账面 NAV 差 ≠ 可判定 edge', body: h('div', { id: 'qd-edge' }) }),
    panel({ title: '账户约束', note: 'paper 模拟账本', body: h('div', { id: 'qd-account' }) }),
    panel({ title: '数据可信边界', note: '覆盖、质量与不可说的结论', body: h('div', { id: 'qd-quality' }) }),
  );
  workbench.append(judgementPanel, side);

  const secondaryBody = h('div', { class: 'qd-secondary-body' });
  const secondary = h('details', { class: 'qd-secondary' },
    h('summary', {},
      h('span', { class: 'strong' }, '二级证据：市场情绪、行业、规则候选与今日时间线'),
      h('span', { class: 'micro faint' }, '展开后才取数，不让信息陈列抢占首屏')),
    secondaryBody);
  let secondaryLoaded = false;
  secondary.addEventListener('toggle', () => {
    if (!secondary.open || secondaryLoaded) return;
    secondaryLoaded = true;
    mountSecondary(secondaryBody, ctx, dashP);
  });

  root.append(kpiHost, hero, loopPanel, workbench, secondary);

  const dashP = api('/dashboard.json?slim=1', { ttl: 10000 });
  const trackingP = api('/short-alpha/tracking?scope=prospective&limit=6', { ttl: 15000 });
  const executionP = api('/short-alpha/execution?limit=6', { ttl: 15000 });

  slot(kpiHost, async () => kpiRow(await dashP), { lines: 2 });
  slot(heroMain, async () => {
    const [dash, tracking, execution] = await Promise.all([dashP, trackingP, executionP]);
    return heroBlock(dash, tracking, execution, ctx);
  }, { lines: 4 });
  slot(nextHost, async () => {
    const [dash, tracking] = await Promise.all([dashP, trackingP.catch(() => null)]);
    return nextActionsBlock(dash, tracking, ctx);
  }, { lines: 4 });
  slot(root.querySelector('#qd-loop'), async () => {
    const [tracking, execution] = await Promise.all([trackingP, executionP]);
    return loopBlock(tracking, execution);
  }, { lines: 3 });
  slot(root.querySelector('#qd-judgements'), async () => {
    const [tracking, execution] = await Promise.all([trackingP, executionP]);
    return judgementLedger(tracking, execution, ctx);
  }, { lines: 7 });
  slot(root.querySelector('#qd-edge'), async () => {
    const [dash, tracking] = await Promise.all([dashP, trackingP]);
    return edgeBlock(dash.arena, tracking);
  }, { lines: 5 });
  slot(root.querySelector('#qd-account'), async () => accountBlock(await dashP), { lines: 5 });
  slot(root.querySelector('#qd-quality'), async () => {
    const info = await api('/data/information', { ttl: 60000 });
    return qualityBlock(info, ctx);
  }, { lines: 6 });

  return root;
}

export function unmount() {
  root = null;
}

export async function onRefresh() {
  if (root) root.dispatchEvent(new CustomEvent('qd:refresh'));
}

// ------------------------------------------------------------------ 首屏
function kpiRow(dash) {
  const s = dash.summary;
  const eq = Number(s.equity);
  const ret = Number(s.total_return || 0);
  const dd = Number(s.drawdown || 0);
  return kpis([
    { label: '净值', value: money(eq), sub: `峰值 ${money(s.peak_equity)} · 回撤 ${ratio(Math.abs(dd), 2)}` },
    { label: '可用现金', value: money(s.cash_free), sub: `冻结 ${money(s.cash_frozen)} · 承诺 ${money(s.cash_committed)}` },
    { label: '持仓市值', value: money(s.market_value), sub: `${s.position_count} 只 / 上限 3` },
    {
      label: '当日盈亏',
      value: fmtByUnits(dash.units, 'daily_pnl_pct', s.daily_pnl_pct, 2, { endpoint: '/portfolio' }),
      sub: `累计 ${pct(ret)} · 已付费用 ${money(s.fees_paid)}`,
      cls: tone(s.daily_pnl_pct),
    },
    { label: '今日成交', value: `${s.fills}`, sub: '上限 4 笔/日（卖出豁免）' },
  ]);
}

function heroBlock(dash, tracking, execution, ctx) {
  const edge = dash.arena?.edge || {};
  const minimum = dash.arena?.min_decisions_for_an_interval ?? 20;
  const n = edge.n ?? 0;
  const items = mergedJudgements(tracking, execution);
  const summary = tracking.summary?.prospective || {};
  const firstDay = execution.days?.[0] || tracking.days?.[0] || {};
  const enough = n >= minimum;
  const title = enough ? '继续核对下一笔可结算判断' : '能力尚未判定，先完成可结算工作';
  const explanation = enough
    ? `已有 ${n} 个可判定决策；仍需逐笔核对区间、基准与执行口径，不用单日盈亏替代能力判断。`
    : `短线 α 已冻结 ${summary.signals ?? items.length} 条判断；可判定 edge 为 n=${n}、n_days=${edge.n_days ?? 0}，至少 ${minimum} 个可判定决策才给区间。今天最有价值的动作是清理待办并等待合法后续日线。`;

  return h('div', { class: 'stack', style: { gap: '10px' } },
    h('div', { class: 'chips' },
      chip('paper 模拟账户', 'acc'),
      chip(`信号日 ${ymd(tracking.as_of || dash.day)}`),
      firstDay.entry_window_day ? chip(`T+1 入场窗口 ${ymd(firstDay.entry_window_day)}`) : null,
      firstDay.exit_deadline_day ? chip(`T+2 截止 ${ymd(firstDay.exit_deadline_day)}`) : null),
    h('h1', { class: 'qd-hero-title' }, title),
    h('div', { class: 'qd-hero-copy' }, explanation),
    h('div', { class: 'row' },
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => showHandoff(dash, tracking, execution),
      }, '查看交班摘要'),
      h('span', { class: 'micro faint' }, '摘要由当前接口生成；本页只读，不自动下单或运行命令。')));
}

function nextActionsBlock(dash, tracking, ctx) {
  const duties = (dash.duty?.items || []).filter((item) => item.kind !== 'regime_note');
  const actions = duties.map((item) => {
    if (item.kind === 'memo_due') {
      return {
        kind: zh.duty(item.kind), detail: item.detail, label: '去复盘结算',
        run: () => ctx.navigate('review'),
        note: '在复盘页“备忘录”卡片确认后点击“结算到期”。',
      };
    }
    if (item.kind === 'hypothesis_open') {
      return {
        kind: zh.duty(item.kind), detail: item.detail, label: '查看执行说明',
        run: () => showHypothesisInstruction(item),
        note: '只展示命令，不会在本页自动运行。',
      };
    }
    return {
      kind: zh.duty(item.kind), detail: item.detail, label: '去复盘核对',
      run: () => ctx.navigate('review'), note: '先核对对象与口径，再决定是否处理。',
    };
  });

  const pending = (tracking?.days || []).flatMap((day) => day.items || [])
    .filter((item) => item.open_entry?.status === 'pending').length;
  if (tracking) {
    actions.push({
      kind: '样本外', label: pending ? `查看 ${pending} 条判断` : '查看判断',
      detail: pending
        ? `${pending} 条冻结判断仍在等待 T+1/T+2 合法后续；pending 不是失败，也不能记成 0 收益。`
        : '冻结判断已有后续状态，逐条核对研究轨道、可执行轨道与实际票据是否一致。',
      run: () => {
        document.getElementById('qd-judgement-panel')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        document.querySelector('.qd-judgement-row')?.focus({ preventScroll: true });
      },
      note: '只查看证据与状态，不补票、不改写判断。',
    });
  } else {
    actions.push({ kind: '接口缺口', detail: '短线 tracking 接口未取到，无法判断样本外待办；这里不显示 0。', label: null });
  }

  return h('div', { class: 'qd-next' },
    h('div', { class: 'qd-next-title' }, '现在值得做的事'),
    actions.length ? h('div', { class: 'qd-next-list' }, actions.slice(0, 4).map((action, index) => h('div', { class: 'qd-next-row' },
      h('span', { class: 'qd-next-index num' }, String(index + 1).padStart(2, '0')),
      h('div', { class: 'qd-next-copy' },
        h('div', { class: 'small strong' }, action.kind),
        h('div', { class: 'micro faint' }, action.detail)),
      action.label ? h('button', { class: 'btn btn-sm', type: 'button', onclick: action.run, title: action.note }, action.label) : null,
    ))) : empty('没有未处理事项', '待办为空不等于应该交易。'));
}

function showHypothesisInstruction(item) {
  const id = String(item.detail || '').match(/#(\d+)/)?.[1] || '';
  const command = id
    ? `cd /d D:\\quant-desk && .venv\\Scripts\\python.exe scripts\\run_research.py --hypothesis ${id}`
    : '未从待办中解析出假设 id；请先到研究记录核对，不要猜参数。';
  drawer({
    title: '假设研究 · 手动执行说明',
    body: h('div', { class: 'stack', style: { gap: '12px' } },
      h('div', { class: 'small' }, item.detail),
      h('p', { class: 'small muted' }, 'draft 表示假设尚未由统计裁判结论回填。下面命令只供你核对后手动运行；本页面没有执行它，也不会修改假设状态。'),
      h('pre', { class: 'qd-code' }, command),
      h('p', { class: 'micro faint' }, '运行前应确认 test_spec、数据日期与样本口径；结论只能由研究结果回填，不能把 draft 当有效规则。')),
  });
}

// ------------------------------------------------------------------ 六环闭环
function loopBlock(tracking, execution) {
  const stats = loopStats(tracking, execution);
  const summary = tracking.summary?.prospective || {};
  const stages = [
    ['证据', stats.evidence, '已读 evidence id'],
    ['判断', stats.judgements, `${summary.candidate_signals ?? '—'} candidate · ${summary.watch_signals ?? '—'} watch`],
    ['票据', stats.tickets, 'entry ticket；不含 tracking_only'],
    ['成交', stats.fills, '只认真实成交回报'],
    ['复评', stats.reviews, '冻结 hold / exit 判断'],
    ['结算', stats.settled, '任一合法 T+1/T+2 轨道'],
  ];
  const nextIndex = stages.findIndex(([_, count]) => count === 0);
  return h('div', { class: 'qd-loop' }, stages.map(([label, count, note], index) => h('div', {
    class: `qd-loop-stage ${count > 0 ? 'has-progress' : ''} ${index === nextIndex ? 'is-next' : ''}`,
    dataset: { stage: label, count: String(count) },
  },
    h('div', { class: 'qd-loop-label' }, `${String(index + 1).padStart(2, '0')} · ${label}`),
    h('div', { class: 'qd-loop-count num' }, num(count)),
    h('div', { class: 'micro faint' }, note),
    index === nextIndex ? h('div', { class: 'qd-loop-next' }, '下一断点') : null)));
}

function loopStats(tracking, execution) {
  const items = mergedJudgements(tracking, execution);
  const evidence = new Set();
  for (const day of tracking.days || []) {
    for (const id of day.reviewed_evidence || []) evidence.add(id);
  }
  const tickets = items.filter((item) => item.execution?.entry_ticket).length;
  const fills = items.filter((item) => item.execution?.entry_ticket?.status === 'filled'
    || item.execution?.position).length;
  const reviews = (execution.days || []).reduce((total, day) => {
    const listed = Array.isArray(day.reviews) ? day.reviews.length : 0;
    const current = (day.items || []).filter((item) => item.latest_review).length;
    return total + Math.max(listed, current);
  }, 0);
  const settled = items.filter((item) => Object.values(item.outcomes || {})
    .some((horizon) => Object.values(horizon || {}).some((outcome) => outcome?.status === 'settled'))).length;
  return {
    evidence: evidence.size, judgements: items.length, tickets, fills, reviews, settled,
  };
}

// ------------------------------------------------------------------ 冻结判断
function mergedJudgements(tracking, execution) {
  const execByKey = new Map();
  const dayByKey = new Map();
  for (const day of execution?.days || []) {
    dayByKey.set(String(day.signal_day), day);
    for (const item of day.items || []) execByKey.set(`${day.signal_day}:${item.symbol}`, item);
  }
  return (tracking?.days || []).flatMap((day) => (day.items || []).map((item) => {
    const execDay = dayByKey.get(String(day.signal_day)) || {};
    return {
      ...item,
      signal_day: day.signal_day,
      captured_at: day.captured_at,
      execution: execByKey.get(`${day.signal_day}:${item.symbol}`) || null,
      entry_window_day: execDay.entry_window_day || null,
      exit_deadline_day: execDay.exit_deadline_day || null,
    };
  }));
}

function judgementLedger(tracking, execution, ctx) {
  const items = mergedJudgements(tracking, execution);
  if (!items.length) return empty('还没有冻结判断', '这与“明确无候选”不同；先确认是否已读证据并登记模型判断。');
  let filter = 'all';
  const list = h('div', { class: 'qd-judgement-list' });
  const counts = {
    all: items.length,
    candidate: items.filter((item) => item.role === 'candidate').length,
    watch: items.filter((item) => item.role === 'watch').length,
  };
  const filters = seg({
    label: '冻结判断筛选',
    items: [
      { value: 'all', label: `全部 ${counts.all}` },
      { value: 'candidate', label: `candidate ${counts.candidate}` },
      { value: 'watch', label: `watch ${counts.watch}` },
    ],
  }, filter, (value) => { filter = value; render(); });

  function render() {
    const shown = items.filter((item) => filter === 'all' || item.role === filter);
    list.replaceChildren(...shown.map((item) => judgementRow(item, ctx)));
  }
  render();
  return h('div', { class: 'stack', style: { gap: '10px' } }, filters, list);
}

function judgementRow(item, ctx) {
  const execution = item.execution;
  const model = execution ? executionRowModel(execution) : null;
  const state = model?.entryStatus || 'execution_unavailable';
  const stateText = executionState(state);
  return h('button', {
    class: 'qd-judgement-row', type: 'button',
    dataset: { symbol: item.symbol, role: item.role, state },
    'aria-label': `${item.name || item.symbol}，${item.role}，${stateText}，查看冻结理由与结算状态`,
    onclick: () => showJudgement(item, ctx),
  },
    h('span', {}, chip(item.role, item.role === 'candidate' ? 'acc' : 'off')),
    h('span', { class: 'qd-judgement-name' },
      h('span', { class: 'strong' }, item.name || item.symbol),
      h('span', { class: 'micro faint num' }, item.symbol)),
    h('span', { class: 'qd-judgement-thesis micro' }, item.thesis || '—'),
    h('span', { class: 'qd-judgement-state' },
      h('span', { class: 'small' }, stateText),
      h('span', { class: 'micro faint' }, item.open_entry?.status === 'pending' ? '等待后续日线' : outcomeText(item.outcomes?.t2_close?.executable_t1_open) || '查看结算')));
}

const EXECUTION_STATE = {
  tracking_only: '仅跟踪 · 无票据', awaiting_entry: '等待 T+1 入场', proposed: '已提案 · 未批准',
  queued: '排队中 · 未成交', filled: '已成交', open: '持仓中', closed: '已退出',
  rejected: '已拒绝', cancelled: '已撤单', expired: '已过期', armed: '条件待触发',
  execution_unavailable: '执行状态未关联',
};
function executionState(value) {
  return EXECUTION_STATE[value] || value || '—';
}

function showJudgement(item, ctx) {
  const execution = item.execution;
  const evidence = item.evidence || [];
  const outcomes = [
    ['T+1 · 研究 T 收盘', item.outcomes?.t1_close?.research_t_close],
    ['T+1 · 可执行 T+1 开盘', item.outcomes?.t1_close?.executable_t1_open],
    ['T+2 · 研究 T 收盘', item.outcomes?.t2_close?.research_t_close],
    ['T+2 · 可执行 T+1 开盘', item.outcomes?.t2_close?.executable_t1_open],
  ];
  let currentDrawer;
  const footer = h('div', { class: 'row' },
    h('button', {
      class: 'btn btn-primary', type: 'button',
      onclick: () => { currentDrawer.close(); ctx.navigate(`s/${item.symbol}`); },
    }, '打开个股证据'),
    h('span', { class: 'micro faint' }, '只查看行情与资料，不自动生成订单'));
  currentDrawer = drawer({
    title: `${item.name || item.symbol} · ${item.role}`,
    footer,
    body: h('div', { class: 'stack qd-detail', style: { gap: '14px' } },
      h('div', { class: 'chips' },
        chip(item.role, item.role === 'candidate' ? 'acc' : 'off'),
        chip(executionState(execution?.state || 'execution_unavailable')),
        chip(`信号日 ${ymd(item.signal_day)}`)),
      safeKv([
        ['目标权重', !execution ? '执行状态未关联' : execution.target_weight === null || execution.target_weight === undefined ? '未冻结（不是 0%）' : ratio(execution.target_weight, 1)],
        ['T+1 入场窗口', item.entry_window_day ? ymd(item.entry_window_day) : '未提供'],
        ['T+2 退出截止', item.exit_deadline_day ? ymd(item.exit_deadline_day) : '未提供'],
        ['entry ticket', execution?.entry_ticket?.status || '无'],
        ['当前持仓', execution?.position ? `${execution.position.qty ?? '—'} 股` : '无'],
        ['最新复评', execution?.latest_review?.decision || '无'],
      ]),
      detailSection('冻结 thesis', item.thesis),
      detailSection('失效条件 invalidation', item.invalidation),
      h('section', { class: 'qd-detail-section' },
        h('h3', {}, '证据 id'),
        evidence.length ? h('ul', { class: 'qd-evidence-list' }, evidence.map((id) => h('li', { class: 'num' }, id))) : h('p', {}, '未提供')),
      h('section', { class: 'qd-detail-section' },
        h('h3', {}, '样本外轨道'),
        h('div', { class: 'stack', style: { gap: '8px' } }, outcomes.map(([label, outcome]) => h('div', { class: 'qd-outcome' },
          h('div', { class: 'small strong' }, label),
          h('div', { class: 'micro faint' }, outcomeText(outcome) || `已结算 · 净超额 ${pct(outcome?.net_excess)}`),
          outcome?.reason ? h('div', { class: 'micro faint', title: outcome.reason }, outcomeReason(outcome.reason)) : null,
          outcome?.status !== 'settled' ? h('div', { class: 'micro faint' }, 'null / pending 不显示为 0 收益') : null)))),
      h('p', { class: 'micro faint' }, 'T+1 开盘是可执行机会代理，不是成交回报；实际成交只认 entry ticket / position / ledger。')),
  });
}

function outcomeReason(reason) {
  return ({
    'T+1 daily bar not available': 'T+1 日线尚未出现',
    'T+2 daily bar not available': 'T+2 日线尚未出现',
  })[reason] || reason;
}

function detailSection(title, text) {
  return h('section', { class: 'qd-detail-section' }, h('h3', {}, title), h('p', {}, text || '—'));
}

// ------------------------------------------------------------------ 样本外与账户
function edgeBlock(arena, tracking) {
  if (!arena?.sleeves) return empty('擂台还没有数据');
  const llm = arena.sleeves.llm || {};
  const mkt = arena.sleeves.market || {};
  const gap = Number.isFinite(Number(llm.nav)) && Number.isFinite(Number(mkt.nav))
    ? Number(llm.nav) - Number(mkt.nav) : null;
  const edge = arena.edge || {};
  const minimum = arena.min_decisions_for_an_interval ?? 20;
  const pending = tracking.summary?.prospective?.open_entry_states?.pending;
  const groupRows = [];
  const groups = tracking.summary?.prospective?.groups || {};
  for (const role of ['candidate', 'watch']) {
    for (const track of ['research_t_close', 'executable_t1_open']) {
      const stat = groups[role]?.[track]?.t2_close || {};
      groupRows.push({
        id: `${role}:${track}`,
        group: role,
        track: track === 'research_t_close' ? '研究 T 收盘' : '可执行 T+1 开盘',
        n: stat.n ?? '—', days: stat.n_days ?? '—',
        ci: stat.ci_status === 'no_sample' ? '无样本' : (stat.ci_status || '—'),
      });
    }
  }
  return h('div', { class: 'stack', style: { gap: '10px' } },
    safeKv([
      ['模型 NAV', money(llm.nav)],
      ['等权基准 NAV', money(mkt.nav)],
      ['账面 NAV 差', h('span', { class: `num ${tone(gap)}` }, gap === null ? '—' : money(gap, { sign: true }))],
      ['可判定 edge', `n=${edge.n ?? '—'} · n_days=${edge.n_days ?? '—'}`],
      ['区间门槛', `至少 ${minimum} 个可判定决策`],
      ['等待入场的判断', pending === undefined ? '—' : `${pending} 条（不是 0 收益）`],
      ['用户指令决策', `${edge.excluded_user_directed ?? 0} 笔排除，不冒充模型 edge`],
    ]),
    h('div', { class: 'qd-truth-note' }, 'NAV 差只是两个账本序列的账面差；能力判断只看带基准、带样本数与区间的已平仓决策。'),
    h('details', { class: 'qd-inline-detail' },
      h('summary', {}, '查看 candidate / watch × 研究 / 可执行四组 T+2 口径'),
      table({
        label: '今日样本外四组 T+2 结算', rowAction: 'none',
        cols: [
          { key: 'group', label: '组' }, { key: 'track', label: '轨道' },
          { key: 'n', label: 'n', cls: 'r' }, { key: 'days', label: '独立日', cls: 'r' },
          { key: 'ci', label: '区间' },
        ],
        rows: groupRows,
      })));
}

function accountBlock(dash) {
  const s = dash.summary || {};
  const persona = dash.persona?.current || {};
  const sentiment = dash.sentiment || {};
  const regimeNote = (dash.duty?.items || []).find((item) => item.kind === 'regime_note');
  return h('div', { class: 'stack', style: { gap: '10px' } },
    h('div', { class: 'chips' },
      chip(`人格 ${persona.name || '—'}`, 'acc'),
      chip(`市场 ${dash.regime ? zh.regime(dash.regime) : '—'}`, dash.regime === 'stressed' ? 'warn' : ''),
      chip(`情绪 ${sentiment.tone || '—'}`, sentiment.tone === '退潮' ? 'warn' : '')),
    safeKv([
      ['可用现金 / 净值', `${money(s.cash_free)} / ${money(s.equity)}`],
      ['持仓 / 今日成交', `${s.position_count ?? '—'} 只 / ${s.fills ?? '—'} 笔`],
      ['人格可下单', persona.tradable === true ? '当前人格与活袖允许提案；仍需逐票 guard' : persona.tradable === false ? '当前人格不可下单' : '—'],
      ['最高板 / 炸板率', `${sentiment.max_boards ?? '—'} 板 / ${fmtByUnits(null, 'break_rate', sentiment.break_rate, 2, { signed: false, endpoint: '/market/pools' })}`],
      ['定价 / 信号日', `${ymd(dash.brief?.session?.priced_through || dash.day)} / ${ymd(dash.day)}`],
      ['账户边界', '仅 paper 模拟账本，不代表 external 实盘镜像'],
    ]),
    regimeNote ? h('div', { class: 'micro faint', style: { lineHeight: '1.6' } }, regimeNote.detail) : null);
}

// ------------------------------------------------------------------ 数据可信
function qualityBlock(info, ctx) {
  const qualityScore = info.quality_adjusted_score;
  const quality = info.quality;
  const weak = info.weak_dimensions || [];
  const conflicts = (info.conflicts || []).filter((item) => item.agree === false);
  const unknown = (value) => value === null || value === undefined ? '未返回' : num(value);
  return h('div', { class: 'stack', style: { gap: '10px' } },
    h('div', { class: 'qd-quality-scores' },
      qualityMetric('覆盖分', `${num(info.score, 1)} / 100`, '最近 250 个交易日覆盖与深度'),
      qualityMetric('可信覆盖分', qualityScore === null || qualityScore === undefined ? '—' : `${num(qualityScore, 1)} / 100`, qualityScore === null || qualityScore === undefined ? '当前服务未返回，不能拿覆盖分冒充' : `质量惩罚 ${num(info.quality_penalty, 1)} 分`)),
    safeKv([
      ['质量错误', quality ? `${unknown(quality.error_issues)} 条` : '当前服务未返回'],
      ['警告分区', quality ? `${unknown(quality.warn_partitions)} 个` : '当前服务未返回'],
      ['当前隔离', quality ? `${unknown(quality.quarantine_partitions)} 个` : '当前服务未返回'],
      ['源间冲突', Array.isArray(info.conflicts) ? `${conflicts.length} 项不能作为已核对事实引用` : '当前服务未返回'],
      ['报价状态', `${info.quotes?.session_label || '—'}；通道可达不等于有实时价`],
    ]),
    weak.length ? h('div', { class: 'stack', style: { gap: '6px' } },
      h('div', { class: 'small strong' }, '当前最弱维度'),
      weak.slice(0, 2).map((item) => h('div', { class: 'qd-weak-row' },
        h('div', { class: 'row' }, h('span', { class: 'small strong' }, item.label), h('span', { class: 'micro num faint' }, `${num(Number(item.score) * 100, 1)}%`)),
        h('div', { class: 'micro faint' }, item.question || '—'),
        (item.components || []).length ? h('div', { class: 'qd-weak-components' },
          item.components.map(weakComponentBlock)) : null))) : null,
    h('details', { class: 'qd-inline-detail' },
      h('summary', {}, '查看不可说的结论与修复口径'),
      h('div', { class: 'stack', style: { gap: '8px', marginTop: '8px' } },
        weak.slice(0, 2).map((item) => h('div', {},
          h('div', { class: 'small strong' }, item.label),
          h('p', { class: 'micro faint' }, item.blind || '—'),
          item.fix ? h('p', { class: 'micro faint' }, `修复口径：${item.fix}`) : null)),
        conflicts.map((item) => h('p', { class: 'micro faint' }, `${item.fact}：${item.note}`)))),
    h('button', { class: 'btn btn-sm', type: 'button', onclick: () => ctx.navigate('kernel') }, '去内核核对数据血缘'));
}

function weakComponentBlock(part) {
  const depth = (part.depth === null || part.depth === undefined) ? '—' : String(part.depth);
  const breadth = (part.universe_coverage === null || part.universe_coverage === undefined)
    ? '' : ` · 股票覆盖 ${num(Number(part.universe_coverage) * 100, 1)}%`;
  const recovery = part.recoverable ? '可回补真实历史' : '不可回补，只能逐日冷存';
  return h('div', { class: 'qd-weak-component' },
    h('div', { class: 'row' },
      h('span', { class: 'micro strong' }, part.label),
      h('span', { class: 'micro num faint' }, `${num(Number(part.score) * 100, 1)}%`)),
    h('div', { class: 'micro faint' },
      `${depth} / ${part.min_history} 日${breadth} · ${recovery}`));
}

function qualityMetric(label, value, note) {
  return h('div', { class: 'qd-quality-metric' },
    h('div', { class: 'micro faint' }, label),
    h('div', { class: 'qd-quality-value num' }, value),
    h('div', { class: 'micro faint' }, note));
}

// ------------------------------------------------------------------ 交班摘要
function showHandoff(dash, tracking, execution) {
  const text = handoffText(dash, tracking, execution);
  drawer({
    title: '交班摘要 · 当前只读快照',
    body: h('div', { class: 'stack', style: { gap: '10px' } },
      h('p', { class: 'micro faint' }, '内容来自本次 dashboard / tracking / execution 响应；不会自动写入 journal 或账本。'),
      h('pre', { class: 'qd-handoff' }, text)),
    footer: h('button', {
      class: 'btn btn-primary', type: 'button',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast('交班摘要已复制，可粘贴到备忘录');
        } catch {
          toast('浏览器未允许剪贴板访问，请手动选择摘要文本复制', 'bad');
        }
      },
    }, '复制摘要'),
  });
}

function handoffText(dash, tracking, execution) {
  const stats = loopStats(tracking, execution);
  const items = mergedJudgements(tracking, execution);
  const duties = (dash.duty?.items || []).filter((item) => item.kind !== 'regime_note');
  const edge = dash.arena?.edge || {};
  const firstDay = execution.days?.[0] || {};
  const lines = [
    '# 量化台交班摘要',
    '',
    `账户快照：${dash.summary?.as_of || '—'}；定价/信号日：${ymd(dash.day)}`,
    `paper 模拟账户：净值 ${money(dash.summary?.equity)}，现金 ${money(dash.summary?.cash_free)}，持仓 ${dash.summary?.position_count ?? '—'}，今日成交 ${dash.summary?.fills ?? '—'}`,
    `闭环：证据 ${stats.evidence} / 判断 ${stats.judgements} / 票据 ${stats.tickets} / 成交 ${stats.fills} / 复评 ${stats.reviews} / 结算 ${stats.settled}`,
    `可判定 edge：n=${edge.n ?? '—'}，n_days=${edge.n_days ?? '—'}；NAV 差不是能力证据。`,
    `执行边界：T+1 窗口 ${ymd(firstDay.entry_window_day || '—')}，T+2 截止 ${ymd(firstDay.exit_deadline_day || '—')}；tracking_only 不补票。`,
    '',
    '## 下一步',
  ];
  if (!duties.length) lines.push('- 没有非状态类待办；这不等于应该交易。');
  for (const duty of duties) lines.push(`- [${zh.duty(duty.kind)}] ${duty.detail}`);
  lines.push(`- 核对 ${stats.judgements} 条冻结判断的后续日线；pending / null 不记 0 收益。`, '', '## 冻结判断');
  for (const item of items) {
    lines.push(`- ${item.symbol} ${item.name || ''}｜${item.role}｜${executionState(item.execution?.state || 'execution_unavailable')}`);
    lines.push(`  thesis：${item.thesis || '—'}`);
    lines.push(`  invalidation：${item.invalidation || '—'}`);
  }
  lines.push('', '边界：本摘要仅用于交班，不创建订单、不执行研究命令、不修改账本。');
  return lines.join('\n');
}

function safeKv(pairs) {
  return kv(pairs.map(([key, value]) => [key, value instanceof Node ? value : h('span', {}, String(value ?? '—'))]));
}

// ------------------------------------------------------------------ 二级证据（展开后才取数）
function mountSecondary(host, ctx, dashP) {
  const board = h('div', { class: 'grid g-desk' });
  const c1 = h('div', { class: 'stack' });
  const c2 = h('div', { class: 'stack' });
  const c3 = h('div', { class: 'stack' });
  board.append(c1, c2, c3);
  host.append(board);
  c1.append(
    panel({ title: '情绪三柱', note: '收盘口径', body: h('div', { id: 'triad' }) }),
    panel({ title: '三池闸门', note: '过 / 不过 / 无结论 / 缺数', body: h('div', { id: 'gates' }) }),
    panel({ title: '申万一级', note: '按涨跌幅排序', actions: h('span', { class: 'micro faint' }, '点击进盘面'), body: h('div', { id: 'sector' }) }),
  );
  c2.append(
    panel({ title: '额度与风控', note: '上限来自 /risk', body: h('div', { id: 'limits' }) }),
    panel({ title: '历史规则候选', note: '仅作旧规则对照，不是 WorkBuddy 短线判断', body: h('div', { id: 'screen' }), flush: true }),
  );
  c3.append(panel({
    title: '今天做过什么', note: '提案/成交/复盘', body: h('div', { id: 'events' }),
    footer: '时间线来自 decisions、persona_switches、journal；没有动作不等于应该补一笔交易。',
  }));

  slot(host.querySelector('#triad'), async () => triadBlock((await dashP).sentiment, (await dashP).day), { lines: 2 });
  slot(host.querySelector('#sector'), async () => sectorBlock(await api('/market/sector?limit=31', { ttl: 60000 }), ctx), { lines: 10 });
  slot(host.querySelector('#gates'), async () => gatesBlock((await api('/market/pools?limit=1', { ttl: 60000 })).gates), { lines: 3 });
  slot(host.querySelector('#limits'), async () => limitsBlock((await dashP).summary), { lines: 6 });
  slot(host.querySelector('#screen'), async () => screenBlock(await api('/research/screen?limit=5', { ttl: 60000 }), ctx), { lines: 5 });
  slot(host.querySelector('#events'), async () => {
    const dash = await dashP;
    return eventBlock(dash.events, dash.events_total);
  }, { lines: 4 });
}

function triadBlock(s, day) {
  if (!s) return empty('没有情绪快照', '先跑 snapshot_intraday 或等 17:30 的日更。');
  const max = Math.max(s.limit_up || 0, s.broken_board || 0, s.limit_down || 0, 1);
  const col = (n, label, kind, cls) => h('div', { class: 'triad-col' },
    h('div', { class: `triad-n ${cls}` }, num(n)),
    h('div', { class: `triad-bar ${kind}`, style: { height: `${Math.max(3, (n / max) * 56)}px` } }),
    h('div', { class: 'triad-l' }, label));
  return h('div', { class: 'stack', style: { gap: '10px' } },
    h('div', { class: 'triad' },
      col(s.limit_up, '涨停', 'up', 'up'),
      col(s.broken_board, '炸板', 'mid', 'flat'),
      col(s.limit_down, '跌停', 'down', 'down')),
    h('div', { class: 'row', style: { gap: '10px', flexWrap: 'wrap' } },
      chip(`最高 ${s.max_boards} 板`),
      chip(`炸板率 ${fmtByUnits(null, 'break_rate', s.break_rate, 2, { signed: false, endpoint: '/market/pools' })}`, s.break_rate > 0.4 ? 'warn' : ''),
      chip(`封单 ${cny(s.seal_amount_cny)}`),
      chip(`语气 ${s.tone || '—'}`, s.tone === '退潮' ? 'warn' : ''),
      s.break_rate_basis ? chip(zh.basis(s.break_rate_basis), 'off') : null),
    h('div', { class: 'micro faint' }, `as_of ${ymd(day)}`));
}

function sectorBlock(data, ctx) {
  const rows = data.industries || [];
  if (!rows.length) return empty('行业榜为空');
  const max = Math.max(...rows.map((r) => Math.abs(r.pct_chg || 0)), 0.01);
  const top = rows.slice(0, 8);
  const bottom = rows.slice(-4).reverse();
  const render = (r) => {
    const v = Number(r.pct_chg || 0);
    const w = (Math.abs(v) / max) * 50;
    return h('div', {
      class: 'heat-row', onclick: () => ctx.navigate('market', { industry: r.sw_l1 }),
      title: `${r.sw_l1}：${r.members} 只，市值加权 ${fmtByUnits(data.units, 'pct_chg', v, 2, { endpoint: '/market/sector' })}，等权 ${fmtByUnits(data.units, 'pct_chg_eq', r.pct_chg_eq, 2, { endpoint: '/market/sector' })}，主力净流入 ${cny(r.main_net_inflow_cny)}`,
    },
      h('div', { class: 'heat-name' }, r.sw_l1),
      h('div', { class: 'heat-track' },
        h('div', { class: 'heat-zero' }),
        h('div', { class: 'heat-fill', style: v >= 0
          ? { left: '50%', width: `${w}%`, background: 'rgba(255,90,99,.55)' }
          : { right: '50%', width: `${w}%`, background: 'rgba(33,201,124,.55)' } })),
      h('div', { class: `heat-val ${tone(v)}` }, fmtByUnits(data.units, 'pct_chg', v, 2, { endpoint: '/market/sector' })),
      h('div', { class: 'heat-flow' }, cny(r.main_net_inflow_cny)));
  };
  return h('div', { class: 'heat-rows' },
    top.map(render),
    h('div', { class: 'micro faint', style: { padding: '3px 0' } }, `… 共 ${data.count} 个行业；行业层面 涨 ${data.breadth?.up ?? '—'} / 跌 ${data.breadth?.down ?? '—'}`),
    bottom.map(render));
}

function limitsBlock(s) {
  const eq = Number(s.equity);
  const cashW = Number.isFinite(eq) && eq > 0 ? Number(s.cash_free) / eq : null;
  const pos = s.position_count === null || s.position_count === undefined ? null : Number(s.position_count);
  return h('div', { class: 'stack', style: { gap: '9px' } },
    h('div', { class: 'meter' },
      meterRow({ label: '持仓数', value: pos ?? 0, max: 3, text: pos === null ? '—' : `${pos}/3` }),
      meterRow({ label: '现金占比', value: cashW ?? 0, max: 1, text: cashW === null ? '—' : ratio(cashW, 1), tone: cashW === null ? '' : cashW >= 0.2 ? 'ok' : 'bad' }),
      meterRow({ label: '回撤', value: Math.abs(Number(s.drawdown || 0)), max: 0.12, text: ratio(Math.abs(Number(s.drawdown || 0)), 2), tone: Math.abs(Number(s.drawdown || 0)) > 0.08 ? 'warn' : 'ok' }),
      meterRow({ label: '今日成交', value: Number(s.fills || 0), max: 4, text: `${s.fills}/4` })),
    h('div', { class: 'chips' },
      chip(s.kill_switch ? '总开关 已拉下' : '总开关 正常', s.kill_switch ? 'state-on' : 'off'),
      chip(s.safe_mode ? '安全模式 开' : '安全模式 关', s.safe_mode ? 'state-on' : 'off'),
      chip(`成交口径 ${s.fill_mode === 'intraday' ? '盘中' : '次日开盘'}`, '')),
    h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
      '单票上限 50%（AGG）· 核心 30% · 行业 35% · 单笔风险 4% · 日亏 6% 停新仓 / 10% 只减仓'));
}

function screenBlock(data, ctx) {
  const rows = data.candidates || [];
  if (!rows.length) return empty('今天没有候选', `价格上限 ${money(data.max_price)}，按规则筛不出可买的名字。空仓是合法答案。`);
  return table({
    cls: 'tbl-click', label: '历史规则候选',
    cols: [
      { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
      { key: 'name', label: '名称', render: (r) => `<span class="name">${esc(r.name)}</span>` },
      { key: 'close', label: '收盘', cls: 'r', render: (r) => Number(r.close).toFixed(2) },
      { key: 'pct_chg', label: '当日', cls: 'r', render: (r) => `<span style="${toneStyle(r.pct_chg)}">${fmtByUnits(data.units, 'pct_chg', r.pct_chg, 2, { endpoint: '/research/screen' })}</span>` },
      { key: 'mom20', label: '20日动量', cls: 'r', render: (r) => `<span style="${toneStyle(r.mom20)}">${fmtByUnits(data.units, 'mom20', r.mom20, 2, { endpoint: '/research/screen' })}</span>` },
      { key: 'adv', label: '20日均额', cls: 'r', render: (r) => cny(r.adv20_cny) },
    ],
    rows, onRow: (r) => ctx.navigate(`s/${r.symbol}`),
  });
}

function eventBlock(events, total) {
  if (!Array.isArray(events)) return empty('时间线未返回', '接口没有给出 events 数组，不能当成“今天没有动作”。');
  if (!events.length) return empty('今天还没有动作', '没有提案、成交或记录。空仓是合法答案。');
  const shown = events.slice(-12).reverse();
  return h('div', { class: 'timeline' },
    shown.map((e) => h('div', { class: `tl-item ${e.kind}` },
      h('div', { class: 'tl-time' }, hhmmss(e.ts)),
      h('div', { class: 'tl-dot' }),
      h('div', { class: 'tl-body' },
        h('div', { class: 'tl-kind' }, e.label || e.kind, e.title ? h('span', { class: 'muted' }, ` ${e.title}`) : null),
        e.detail ? h('div', { class: 'tl-detail' }, truncate(e.detail, 120)) : null))),
    total && total > shown.length ? h('div', { class: 'micro faint', style: { paddingTop: '5px' } }, `共 ${total} 条，显示最近 ${shown.length} 条`) : null);
}

function truncate(text, n) {
  const s = String(text || '');
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
