// 短线 α：内核证据与 WorkBuddy 判断是两个不同的对象，本页不把二者混成选票。
import { api } from '../api.js';
import { h, panel, chip, table, slot, kv } from '../ui.js';
import { pct, tone, num, ymd, ratio } from '../fmt.js';

export function mountShortAlpha(root) {
  const grid = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  grid.append(
    panel({
      title: '短线 α · 内核证据', note: '按代码稳定排序，不按收益排名',
      body: h('div', { id: 'short-alpha-evidence' }),
      footer: '历史净超额只是证据；candidate / watch 由 WorkBuddy 独立判断，本表不是内核选股名单。',
    }),
    panel({
      title: '短线 α · 判断与样本外', note: '冻结 WorkBuddy 当时判断',
      body: h('div', { id: 'short-alpha-tracking' }),
      footer: 'T 收盘仅作研究锚点；T+1 开盘是机会代理，不是成交回报。prospective 与 replay 永不混算。',
    }),
  );
  root.append(grid);
  root.append(panel({
    title: '短线 α · 模拟执行与复评', note: 'T+1 入场 · 动态复评 · 最迟 T+2 退出',
    body: h('div', { id: 'short-alpha-execution' }),
    footer: '仅显示 paper 模拟账户。proposed / queued 不是成交；复评与查询不创建订单，实际委托仍走 propose → approve。',
  }));
  slot(root.querySelector('#short-alpha-evidence'), evidenceBlock, { lines: 8 });
  slot(root.querySelector('#short-alpha-tracking'), trackingBlock, { lines: 10 });
  slot(root.querySelector('#short-alpha-execution'), executionBlock, { lines: 10 });
}

function pctCell(value) {
  return value === null || value === undefined
    ? '—' : h('span', { class: tone(value) }, pct(value));
}

export function evidenceUniverseCount(data) {
  return data?.universe?.matched_current_after_requested_filter ?? 0;
}

export function evidenceRowModel(row) {
  const evidence = row.evidence || {};
  const t1 = evidence.horizons?.t1_close || {};
  const execution = row.execution || {};
  return {
    id: row.evidence_id,
    symbol: row.symbol,
    name: row.name || row.symbol,
    boards: row.boards,
    sealRatio: row.seal_ratio,
    historyMean: t1.net_excess_mean,
    historyN: t1.n,
    historyDays: t1.n_days,
    costBps: execution.total_round_trip_cost_bps,
    boundary: row.fill_kind === 'one_word'
      ? '一字：无成交承诺'
      : execution.account_ok === true ? '整手可负担'
        : execution.account_ok === false ? '账户情景不可行' : '待核',
  };
}

function evidenceBlock() {
  return api('/short-alpha/evidence?limit=4', { ttl: 60000, silent: true })
    .catch((error) => ({ ok: false, error: error.message }))
    .then((data) => {
      if (!data?.ok) return h('div', { class: 'muted' }, `证据未取到：${data?.error || '接口不可用'}`);
      const rows = (data.rows || []).map((row) => {
        const model = evidenceRowModel(row);
        return {
          id: model.id,
          symbol: h('div', {}, h('div', { class: 'strong' }, model.name),
            h('div', { class: 'micro faint' }, model.symbol)),
          setup: `${model.boards ?? '—'} 板 · ${ratio(model.sealRatio, 2)}`,
          history: h('div', {}, pctCell(model.historyMean),
            h('div', { class: 'micro faint' }, `n=${model.historyN ?? '—'} / 日=${model.historyDays ?? '—'}`)),
          cost: model.costBps === null || model.costBps === undefined
            ? '—' : `${num(model.costBps, 1)} bps`,
          boundary: model.boundary,
        };
      });
      return h('div', { class: 'stack', style: { gap: '8px' } },
        h('div', { class: 'row micro faint' },
          chip(`${data.as_of || '—'}`), chip(`证据宇宙 ${evidenceUniverseCount(data)} 只`),
          h('span', {}, '模型必须自行写下 thesis / evidence / invalidation')),
        table({
          label: 'short-alpha-evidence', rowAction: 'none',
          cols: [
            { key: 'symbol', label: '股票' }, { key: 'setup', label: '结构' },
            { key: 'history', label: 'T+1 历史净超额', title: '历史分布，不是未来收益承诺' },
            { key: 'cost', label: '往返成本' }, { key: 'boundary', label: '事实边界' },
          ],
          rows,
          emptyText: '当前证据宇宙没有可展示的行；这不等于 WorkBuddy 已登记“无候选”。',
        }));
    });
}

const STATUS = {
  pending: '等待后续日线', settled: '已结算', partial: '部分结算',
  no_candidate: '明确无候选', invalid: '无效，不计 0',
  no_fill: '无成交代理', no_fill_open_one_word: '一字，无成交代理',
  no_fill_missing_open_bar: '缺开盘 bar', no_fill_suspended: '停牌',
  no_fill_no_account_scenario: '无账户成本情景',
  no_fill_account_scenario: '无账户成本情景',
  no_fill_account_unaffordable: '账户预算不足',
  no_fill_account_budget_exceeded: '账户预算不足',
  invalid_open_price: '开盘价无效',
  missing_or_noncontiguous_or_corporate_action_label: '非连续/除权/缺出口，不计0',
  invalid_frozen_cost_inputs: '冻结成本输入无效',
  queue_required_fill_unknown: '排队位置未知',
  open_auction_opportunity_proxy: '开盘机会代理',
  awaiting_entry: '等待 T+1 入场', tracking_only: '仅样本外跟踪',
  proposed: '已提案，未批准', queued: '已排队，未成交', filled: '已成交',
  open: '持仓中', closed: '已退出', rejected: '风控/执行拒绝',
  cancelled: '已撤单', expired: '已过期', armed: '条件待触发',
};

function status(value) {
  return STATUS[value] || value || '—';
}

export function outcomeText(outcome) {
  if (!outcome) return '—';
  if (outcome.status === 'settled') return null;
  if (outcome.status === 'pending') return status('pending');
  if (outcome.status === 'no_fill') return status(outcome.reason || 'no_fill');
  if (outcome.status === 'invalid') return status(outcome.reason || 'invalid');
  return status(outcome.status);
}

export function trackingView(data) {
  const summary = data?.summary?.prospective || {};
  return {
    asOf: data?.as_of,
    registered: data?.latest_bar_judgment_registered === true,
    signalDays: summary.signal_days,
    candidateSignals: summary.candidate_signals,
    watchSignals: summary.watch_signals,
    candidateT1: summary.groups?.candidate?.executable_t1_open?.t1_close || {},
    watchT1: summary.groups?.watch?.executable_t1_open?.t1_close || {},
  };
}

function outcomeCell(outcome) {
  if (!outcome) return '—';
  if (outcome.status === 'settled') return pctCell(outcome.net_excess);
  return h('span', { class: 'micro faint', title: outcome.reason || '' }, outcomeText(outcome));
}

export function executionRowModel(item) {
  const reviewLabels = { hold: '继续持有', exit_next_open: '下一开盘退出', exit_now: '本窗口退出' };
  return {
    symbol: item.symbol,
    name: item.name || item.symbol,
    role: item.role,
    weight: item.target_weight,
    entryStatus: item.entry_ticket?.status || item.state,
    qty: item.position?.qty,
    availableQty: item.position?.available_qty,
    review: reviewLabels[item.latest_review?.decision] || '尚未复评',
    reviewReason: item.latest_review?.reason || '',
    exitStatus: item.exit_ticket?.status || null,
    deadline: item.exit_deadline_day,
  };
}

function executionBlock() {
  return api('/short-alpha/execution?limit=6', { ttl: 15000, silent: true })
    .catch((error) => ({ ok: false, error: error.message }))
    .then((data) => {
      if (!data?.ok) return h('div', { class: 'muted' }, `执行状态未取到：${data?.error || '接口不可用'}`);
      const children = [h('div', { class: 'row micro faint' },
        chip(`paper 模拟账户`), chip(`${num(data.summary?.signal_days)} 个信号日`),
        h('span', {}, '账外实盘：不触碰；本面板：只读'))];
      for (const day of data.days || []) {
        const rows = (day.items || []).map((item) => {
          const model = executionRowModel(item);
          return {
            id: `${day.signal_day}:${item.symbol}`,
            symbol: h('div', {}, h('div', { class: 'strong' }, model.name),
              h('div', { class: 'micro faint' }, model.symbol)),
            role: model.role,
            weight: model.weight == null ? '只跟踪' : ratio(model.weight, 1),
            entry: status(model.entryStatus),
            position: model.qty == null ? '—' : `${model.qty} 股 / 可卖 ${model.availableQty ?? '—'}`,
            review: h('div', { title: model.reviewReason }, model.review),
            exit: model.exitStatus ? status(model.exitStatus) : '—',
            deadline: ymd(model.deadline),
          };
        });
        children.push(h('div', { class: 'stack', style: { gap: '5px', borderTop: '1px solid var(--line)', paddingTop: '8px' } },
          h('div', { class: 'row' }, chip(`信号 ${ymd(day.signal_day)}`),
            chip(`T+1 ${ymd(day.entry_window_day)}`), chip(`最迟退出 ${ymd(day.exit_deadline_day)}`),
            day.execution_intent ? chip('含权重意图') : chip('旧记录仅跟踪')),
          table({
            label: `short-alpha-execution-${day.signal_day}`, rowAction: 'none', fit: true,
            cols: [
              { key: 'symbol', label: '股票' }, { key: 'role', label: '角色' },
              { key: 'weight', label: '目标权重' }, { key: 'entry', label: '入场票据' },
              { key: 'position', label: '当前持仓' }, { key: 'review', label: '最新复评理由' },
              { key: 'exit', label: '退出票据' }, { key: 'deadline', label: '退出边界' },
            ], rows,
            emptyText: '这个信号日没有个股判断。',
          })));
      }
      if (!(data.days || []).length) {
        children.push(h('div', { class: 'muted' }, '暂无 prospective 执行记录；未登记或旧无权重记录都不会自动生成订单。'));
      }
      return h('div', { class: 'stack', style: { gap: '8px' } }, children);
    });
}

function trackingBlock() {
  return api('/short-alpha/tracking?scope=prospective&limit=6', { ttl: 30000, silent: true })
    .catch((error) => ({ ok: false, error: error.message }))
    .then((data) => {
      if (!data?.ok) return h('div', { class: 'muted' }, `跟踪未取到：${data?.error || '接口不可用'}`);
      const view = trackingView(data);
      const candidateT1 = view.candidateT1;
      const watchT1 = view.watchT1;
      const children = [
        kv([
          ['最新完整日线', ymd(view.asOf)],
          ['登记状态', view.registered ? 'WorkBuddy 已登记' : '模型尚未登记'],
          ['前瞻信号日', num(view.signalDays)],
          ['candidate / watch', `${num(view.candidateSignals)} / ${num(view.watchSignals)}`],
        ]),
        h('div', { class: 'micro faint' },
          `可执行 T+1：candidate n=${candidateT1.n ?? 0}，均值 ${candidateT1.net_excess_mean == null ? '—' : pct(candidateT1.net_excess_mean)}；`
          + `watch n=${watchT1.n ?? 0}，均值 ${watchT1.net_excess_mean == null ? '—' : pct(watchT1.net_excess_mean)}。`
          + 'n<30 只作观察，区间不足 8 个独立日不画 CI。'),
      ];
      for (const day of data.days || []) {
        const rows = (day.items || []).map((item) => ({
          id: `${day.signal_day}:${item.symbol}`,
          symbol: h('div', {}, h('span', { class: 'strong' }, item.name || item.symbol),
            h('div', { class: 'micro faint' }, item.symbol)),
          role: item.role === 'candidate' ? 'candidate' : 'watch',
          weight: item.target_weight == null ? '只跟踪' : ratio(item.target_weight, 1),
          thesis: h('div', { title: item.invalidation || '' }, item.thesis || '—'),
          research1: outcomeCell(item.outcomes?.t1_close?.research_t_close),
          executable1: outcomeCell(item.outcomes?.t1_close?.executable_t1_open),
          executable2: outcomeCell(item.outcomes?.t2_close?.executable_t1_open),
        }));
        children.push(h('div', { class: 'stack', style: { gap: '5px', borderTop: '1px solid var(--line)', paddingTop: '8px' } },
          h('div', { class: 'row' }, chip(ymd(day.signal_day)),
            chip(day.judgment_status === 'explicit_no_candidate' ? '明确无候选' : '模型判断'),
            chip(status(day.status)),
            h('span', { class: 'micro faint', title: day.frozen_sha256 }, `冻结 ${String(day.frozen_sha256 || '').slice(0, 10)}`)),
          day.no_candidate_reason ? h('div', { class: 'small muted' }, day.no_candidate_reason) : null,
          rows.length ? table({
            label: `short-alpha-${day.signal_day}`, rowAction: 'none', fit: true,
            cols: [
              { key: 'symbol', label: '股票' }, { key: 'role', label: '模型角色' },
              { key: 'weight', label: '冻结目标权重' },
              { key: 'thesis', label: '冻结理由 / 失效条件' },
              { key: 'research1', label: 'T+1 研究净超额' },
              { key: 'executable1', label: 'T+1 可执行净超额' },
              { key: 'executable2', label: 'T+2 可执行净超额' },
            ], rows,
          }) : null));
      }
      if (!(data.days || []).length) {
        children.push(h('div', { class: 'muted' },
          data.latest_bar_judgment_registered
            ? '当前窗口没有 prospective 记录。'
            : '最新日线尚无 WorkBuddy 判断。未登记 ≠ 明确无候选；请由 WorkBuddy 阅读证据后登记。'));
      }
      return h('div', { class: 'stack', style: { gap: '8px' } }, children);
    });
}
