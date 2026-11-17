/* quant-desk 桌面窗口的前端。
 *
 * 只做两件事：把 Python 给的数画出来，把用户填的数原样送回去。所有判断在
 * `service/trading/deskview.py` 与 `manual.py` 里，这里不重复算一遍——两处
 * 各算一次，第一次对不上就没人说得清哪个对。
 *
 * 在浏览器里直接打开这个文件时会进入演示模式（没有 pywebview），方便改样式。
 */

'use strict';

const state = {
  meta: null,
  account: null,
  events: [],
  system: null,
  plan: null,
  demo: false,
};

const $ = (id) => document.getElementById(id);

// Server payloads are data, never markup. Build elements and assign text/value
// properties instead of relying on every interpolation remembering to escape.
function el(tag, className = '', text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text ?? '');
  return node;
}

function appendValue(parent, value) {
  if (value && typeof value === 'object' && value.nodeType) parent.append(value);
  else parent.textContent = String(value ?? '');
  return parent;
}

function tag(text, tone = 'dim') {
  return el('span', `tag ${tone}`, text);
}

const money = (value, digits = 2) => {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {
    return '—';
  }
  return Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
};

const signed = (value, digits = 0) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const n = Number(value);
  return (n > 0 ? '+' : '') + n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
};

const pct = (value, digits = 1) => (value === null || value === undefined
  ? '—' : `${(Number(value) * 100).toFixed(digits)}%`);

const price = (value) => (value === null || value === undefined || Number(value) === 0
  ? '—' : Number(value).toFixed(2));

const toneClass = (value) => (Number(value) > 0 ? 'up' : Number(value) < 0 ? 'down' : 'flat');

/* ------------------------------------------------------------- transport */
const api = () => window.pywebview?.api ?? null;

async function call(name, ...args) {
  const bridge = api();
  if (!bridge) throw new Error('窗口桥没接上');
  const fn = bridge[name];
  if (typeof fn !== 'function') throw new Error(`窗口不提供 ${name}()`);
  return fn(...args);
}

/* ------------------------------------------------------------------ demo */
const demoAccount = () => ({
  panel: { ok: true, total: 4795.56, cash: 4793.56, market_value: 0, cash_ratio: 0.9996,
           source: 'screen:ths' },
  positions: [],
  source: 'snapshot',
  snapshot_day: '20261027',
  book_value: 0,
  drift: { verdict: 'match', message: '与账户一致：市值 0.00，按持仓算是 0.00（差 +0.00）' },
  reconcile: { ok: true, snapshot_missing: false, mismatches: [] },
  last_sync: { imported_at: '2026-10-27T13:11:00', file_name: 'sync-20261027', rows: 0 },
  alert: null,
});

/* ---------------------------------------------------------------- busy */
let busyDepth = 0;
function busy(on, text = '读取中…') {
  busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
  const box = $('busy');
  $('busy-text').textContent = text;
  box.hidden = busyDepth === 0;
}

function toast(text, bad = false) {
  const box = $('toast');
  box.textContent = text;
  box.className = `toast on${bad ? ' bad' : ''}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.className = 'toast'; }, 4200);
}

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('on', tab.dataset.tab === name);
  });
  document.querySelectorAll('.pane').forEach((pane) => {
    pane.classList.toggle('on', pane.id === `pane-${name}`);
  });
  if (name === 'sync' && !$('edit-body').children.length) fillFormFromAccount();
}

const TABS = ['live', 'sync', 'acts', 'system'];
function initialTab() {
  // Query string, not the hash: WebView2 restores the last URL (hash included)
  // when the window reopens, so a hash left over from a screenshot made the app
  // open on 系统 instead of 实盘.
  const wanted = new URLSearchParams(location.search || '').get('tab') || '';
  return TABS.includes(wanted) ? wanted : 'live';
}

/* ------------------------------------------------------------- render */
function renderMeta() {
  const day = state.meta?.day || '';
  $('day-pill').textContent = day
    ? `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}`
    : '—';
  const stamp = state.meta?.now || '';
  $('stamp').textContent = stamp ? stamp.replace('T', ' ').slice(0, 19) : '';
}

function renderAccount() {
  const data = state.account;
  if (!data) {
    $('verdict-line').textContent = '实盘账本读不到（data/external.sqlite）';
    $('verdict').className = 'verdict bad';
    return;
  }
  const panel = data.panel || {};
  const drift = data.drift || {};
  const gap = data.reconcile || {};

  if (panel.ok) {
    $('kpi-total').textContent = money(panel.total);
    $('kpi-cash').textContent = money(panel.cash);
    $('kpi-mv').textContent = money(panel.market_value);
    $('kpi-ratio').textContent = pct(panel.cash_ratio);
    $('kpi-total-foot').textContent = panel.source === 'manual' || panel.note
      ? '手动录入' : '实时读自客户端';
    $('kpi-cash-foot').textContent = `${panel.note ? '' : '资金面板'}只读`;
    $('kpi-mv-foot').textContent = data.positions?.length
      ? `${data.positions.length} 只持仓` : '空仓';
    $('kpi-ratio-foot').textContent = Number(panel.cash_ratio) > 0.8
      ? '仓位很轻' : '仓位较重';
  } else {
    ['kpi-total', 'kpi-cash', 'kpi-mv', 'kpi-ratio'].forEach((id) => { $(id).textContent = '—'; });
    $('kpi-total-foot').textContent = '客户端读不到';
    $('kpi-cash-foot').textContent = '';
    $('kpi-mv-foot').textContent = '';
    $('kpi-ratio-foot').textContent = '';
  }

  const verdict = $('verdict');
  const line = $('verdict-line');
  const sub = $('verdict-sub');
  verdict.onclick = null;
  verdict.classList.remove('clickable');
  if (data.quotes_pending) {
    verdict.className = 'verdict';
    line.textContent = '正在取实时价…';
    sub.textContent = '持仓与账户数字已就绪，价格到了会自动补上。';
  } else if (!panel.ok) {
    verdict.className = 'verdict warn clickable';
    line.textContent = '账户数字还没填 —— 点这里录入总资产与可用资金';
    sub.textContent = panel.reason
      ? `（联不上客户端：${panel.reason}）先填一次，之后每笔成交会自动扣加现金。`
      : '先填一次，之后每笔成交会自动扣加现金。';
    verdict.onclick = () => showTab('sync');
  } else if (drift.verdict === 'match') {
    verdict.className = 'verdict ok';
    line.textContent = drift.message || '与账户一致';
    sub.textContent = '';
  } else if (drift.verdict) {
    verdict.className = 'verdict bad';
    line.textContent = '与账户不一致';
    sub.textContent = drift.message || drift.reason || '';
  } else {
    verdict.className = 'verdict';
    line.textContent = '未比对';
    sub.textContent = drift.reason || '';
  }

  renderPositions(data);
  renderLedger(data, gap);
  renderAlerts(data, drift, gap);
}

function renderPositions(data) {
  const body = $('position-body');
  const rows = data.positions || [];
  const cell = (value, className = 'num') => el('td', className, value);
  body.replaceChildren(...rows.map((p) => {
    const breach = p.breach_stop || p.breach_auction;
    const status = p.breach_stop ? tag('破止损', 'bad')
      : p.breach_auction ? tag('破竞价线', 'warn') : tag('持有');
    const tr = el('tr', breach ? 'breach' : '');
    tr.append(
      cell(p.symbol, 'l sym'), cell(p.name || '', 'l'), cell(money(p.qty, 0)),
      cell(money(p.cost, 3)), cell(price(p.last)), cell(money(p.market_value, 0)),
      cell(signed(p.pnl_total, 0), `num ${toneClass(p.pnl_total)}`),
      cell(price(p.stop)), cell(price(p.auction_line)), appendValue(el('td', 'l'), status),
    );
    return tr;
  }));
  $('position-empty').hidden = rows.length > 0;
  $('position-table').querySelector('thead').hidden = rows.length === 0;
  $('position-source').textContent = data.snapshot_day
    ? `快照 ${data.snapshot_day}${data.source === 'ledger' ? '（账本兜底）' : ''}`
    : '账本（还没有快照）';
}

function renderLedger(data, gap) {
  const last = data.last_sync || {};
  const items = [];
  items.push(['快照日期', data.snapshot_day || '还没有']);
  items.push(['上次同步', last.imported_at ? String(last.imported_at).slice(0, 16).replace('T', ' ') : '—']);
  items.push(['来源', last.file_name || '—']);
  items.push(['流水行数', last.rows ?? '—']);
  if (gap.snapshot_missing) {
    items.push(['流水 vs 快照', tag('还没有快照', 'warn')]);
  } else if (gap.ok) {
    items.push(['流水 vs 快照', tag('一致', 'good')]);
  } else {
    const n = (gap.mismatches || []).length;
    items.push(['流水 vs 快照', tag(`${n} 处不一致`, 'bad')]);
  }
  items.push(['按持仓算市值', money(data.book_value)]);
  const children = [];
  items.forEach(([key, value]) => {
    children.push(el('dt', '', key), appendValue(el('dd'), value));
  });
  $('ledger-kv').replaceChildren(...children);
}

function renderAlerts(data, drift, gap) {
  const box = $('alerts-box');
  const items = [];
  if (data.alert) items.push({ cls: 'bad', text: data.alert });
  if (drift.verdict && drift.verdict !== 'match' && drift.message) {
    items.push({ cls: 'bad', text: drift.message });
  }
  (gap.mismatches || []).slice(0, 6).forEach((row) => {
    items.push({
      cls: 'bad',
      text: `${row.symbol}：账本 ${row.ledger_qty} 股 vs 账户 ${row.broker_qty} 股（差 ${signed(row.delta, 0)}）——缺成交流水`,
    });
  });
  if (!data.snapshot_day) {
    items.push({ cls: '', text: '还没有持仓快照。下一次同步之后，止损与竞价线才算得出来。' });
  }
  if (!items.length) {
    items.push({ cls: 'info', text: '没有需要处理的事。' });
  }
  box.replaceChildren(...items.map((item) => el('div', `alert ${item.cls}`, item.text)));
}

function renderEvents() {
  const rows = state.events || [];
  const tones = ['bad', 'good', 'warn', 'note', 'dim', 'info'];
  const item = (ev) => {
    const clock = String(ev.ts || '').slice(11, 16) || '—';
    const tone = tones.includes(ev.tone) ? ev.tone : 'dim';
    const what = el('div', 'what');
    if (ev.title) what.append(el('b', '', ev.title));
    if (ev.detail) what.append(el('div', 'why', ev.detail));
    const li = el('li');
    li.append(el('time', '', clock), tag(ev.label || ev.kind, tone), what);
    return li;
  };
  $('acts-list').replaceChildren(...rows.map(item));
  $('acts-empty').hidden = rows.length > 0;
  $('acts-hint').textContent = rows.length ? `${rows.length} 条` : '';

  // 实盘页只放最近几条：这里回答的是"它今天动过吗"，不是"完整复盘"。
  const recent = rows.slice(-5);
  $('live-acts').replaceChildren(...recent.map(item));
  $('live-acts-empty').hidden = recent.length > 0;
  $('live-acts-hint').textContent = rows.length > recent.length
    ? `最近 ${recent.length} 条，共 ${rows.length} 条` : '';
}

function renderSystem() {
  const sys = state.system;
  if (!sys) return;
  const paper = sys.paper || {};
  const pnl = el('span', toneClass(paper.unrealized_pnl), money(paper.unrealized_pnl));
  const rows = [
    ['权益', money(paper.equity), 'big'],
    ['现金', money(paper.cash_available)],
    ['持仓市值', money(paper.market_value)],
    ['浮动盈亏', pnl],
    ['累计收益', `${((paper.total_return || 0) * 100).toFixed(2)}%`],
    ['回撤', `${((paper.drawdown || 0) * 100).toFixed(2)}%`],
    ['已付费用', money(paper.fees_paid)],
    ['市场状态', sys.regime || '—'],
  ];
  const children = [];
  rows.forEach(([key, value, className]) => {
    children.push(el('dt', '', key), appendValue(el('dd', className || ''), value));
  });
  $('paper-kv').replaceChildren(...children);

  renderPersona(sys);
  renderDuty(sys);
  renderArena(sys);
  renderData(sys);
}

function renderPersona(sys) {
  const box = $('persona-box');
  const persona = sys.persona;
  if (!persona) {
    box.replaceChildren(el('div', 'empty', '人格状态不可用'));
    return;
  }
  const cur = persona.current || {};
  const gates = sys.sentiment || {};
  const chips = el('div', 'chips persona-chips');
  (persona.available || []).forEach((item) => {
    const ok = item.ready;
    const missing = (item.missing_labels || item.missing_requirements || []).join('、');
    const text = `${item.name || ''}${ok ? '' : ` · 缺 ${missing}`}`;
    chips.append(el('span', `chip ${ok ? 'ok' : ''}`, text));
  });
  const kv = el('dl', 'kv');
  kv.append(
    el('dt', '', '当前'), el('dd', 'big', cur.name || '—'),
    el('dt', '', '今日切换'), el('dd', '', `${cur.switches_today ?? 0}/2`),
  );
  const children = [kv, el('p', 'section-note persona-style', cur.style || '—'), chips];
  if (gates.tone) {
    children.push(el('p', 'section-note',
      `情绪 ${gates.tone} · 炸板率 ${pct(gates.break_rate)} · 最高 ${gates.max_boards} 板`));
  }
  box.replaceChildren(...children);
}

function renderDuty(sys) {
  const items = sys.duty?.items || [];
  if (!items.length) {
    const li = el('li');
    li.append(el('span', 'hint', '没有待办。'));
    $('duty-list').replaceChildren(li);
    return;
  }
  $('duty-list').replaceChildren(...items.map((item) => {
    const li = el('li');
    li.append(el('span', 'pri', `P${item.priority}`), el('span', '', item.detail));
    return li;
  }));
}

function renderArena(sys) {
  const arena = sys.arena || {};
  const edge = arena.edge || {};
  const rawN = Number(edge.n || 0);
  const n = Number.isFinite(rawN) ? rawN : 0;
  const sleeves = arena.sleeves || {};
  const kv = el('dl', 'kv');
  const entries = Object.entries(sleeves);
  if (!entries.length) kv.append(el('dt', '', '袖'), el('dd', '', '—'));
  entries.forEach(([key, value]) => {
    const ret = Number(value?.return || 0) * 100;
    const dd = el('dd');
    dd.append(
      document.createTextNode(`¥${money(value?.nav)} `),
      el('span', ret >= 0 ? 'up' : 'down', `${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%`),
    );
    kv.append(el('dt', '', key), dd);
  });
  const bar = el('div', 'bar');
  const fill = el('i');
  fill.style.width = `${Math.max(0, Math.min(100, n / 20 * 100))}%`;
  bar.append(fill);
  $('arena-box').replaceChildren(
    kv, bar,
    el('p', 'section-note', `平仓样本 ${n}/20 · 判决 ${arena.verdict || '—'}`),
    el('p', 'section-note', arena.why || ''),
  );
}

function renderData(sys) {
  const brief = sys.brief || {};
  const core = brief.core?.datasets || {};
  const aggressive = brief.aggressive?.datasets || {};
  const aux = brief.aux?.datasets || {};
  const group = (title, items, hint) => {
    const entries = Object.entries(items);
    if (!entries.length) return null;
    const box = el('div', 'dataset-group');
    const heading = el('h3', '', `${title} `);
    heading.append(el('span', '', hint));
    const chips = el('div', 'chips');
    entries.forEach(([name, item]) => {
      const lag = item?.lag_trading_days;
      const quarantined = Number(item?.quarantined_partitions || 0);
      const cls = quarantined || lag === null || lag === undefined ? 'bad'
        : lag === 0 ? 'ok' : lag <= 2 ? 'warn' : 'bad';
      const note = quarantined ? `隔离${quarantined}` : lag === null || lag === undefined
        ? '无数据' : lag === 0 ? '' : `滞后${lag}日`;
      chips.append(el('span', `chip ${cls}`, `${name}${note ? ` · ${note}` : ''}`));
    });
    box.append(heading, chips);
    return box;
  };
  const okCount = (items) => Object.values(items)
    .filter((item) => item?.lag_trading_days === 0 && !Number(item?.quarantined_partitions || 0)).length;
  const blocked = sys.aggressive_blocked || [];
  const gate = brief.aggressive?.usable ? tag('游资袖可用', 'good')
    : tag(`游资袖不可用${blocked.length ? `：${blocked.join('；')}` : ''}`, 'warn');
  const quote = sys.probe?.rt_quote || {};
  const quoteText = quote.tradable ? '盘中实时，可作为成交价'
    : quote.feed_available ? '行情源可用，盘后取到的是收盘价，不能作为成交价'
      : '行情源不可用（网络关闭或密钥缺失）';
  $('data-hint').textContent = `核心 ${okCount(core)}/${Object.keys(core).length} · 盘面 ${okCount(aggressive)}/${Object.keys(aggressive).length} · 辅助 ${okCount(aux)}/${Object.keys(aux).length}`;
  const children = [el('div', 'chips'), group('核心', core, '缺任一 → 无法下单'),
    group('盘面', aggressive, '缺任一 → 游资袖不可用'), group('辅助', aux, '参考，不阻断')];
  children[0].append(gate, el('span', 'chip', quoteText));
  $('data-box').replaceChildren(...children.filter(Boolean));
}

/* ------------------------------------------------------------ 同步表单 */
function addRow(row = {}) {
  const body = $('edit-body');
  const tr = el('tr');
  const field = (className, value, placeholder) => {
    const input = el('input', className);
    input.value = String(value ?? '');
    input.placeholder = placeholder;
    const td = el('td', className === 'f-symbol' || className === 'f-name' ? 'l' : '');
    td.append(input);
    return td;
  };
  const remove = el('button', 'del', '×');
  remove.title = '删除';
  remove.onclick = () => tr.remove();
  const action = el('td');
  action.append(remove);
  tr.append(
    field('f-symbol', row.symbol, '000590'), field('f-name', row.name, '名称'),
    field('f-qty', row.qty, '0'), field('f-cost', row.cost, '0.000'),
    field('f-last', row.last, '现价'), action,
  );
  body.append(tr);
  return tr;
}

function fillFormFromAccount() {
  const data = state.account;
  $('edit-body').replaceChildren();
  if (!data) return;
  const panel = data.panel || {};
  if (panel.ok) {
    $('in-total').value = Number(panel.total || 0).toFixed(2);
    $('in-cash').value = Number(panel.cash || 0).toFixed(2);
    $('in-mv').value = Number(panel.market_value || 0).toFixed(2);
  }
  $('in-day').value = state.meta?.today || state.meta?.day || '';
  (data.positions || []).forEach((p) => addRow({
    symbol: p.symbol, name: p.name, qty: p.qty, cost: p.cost, last: p.last,
  }));
  if (!(data.positions || []).length) addRow();
  state.plan = null;
  $('plan-box').replaceChildren(el('p', 'hint', '已按当前持仓填好，改动后点「校验」。'));
}

function readForm() {
  const positions = [...$('edit-body').children].map((tr) => ({
    symbol: tr.querySelector('.f-symbol').value.trim(),
    name: tr.querySelector('.f-name').value.trim(),
    qty: tr.querySelector('.f-qty').value.trim(),
    cost: tr.querySelector('.f-cost').value.trim(),
    last: tr.querySelector('.f-last').value.trim(),
  })).filter((row) => row.symbol);
  return {
    as_of: $('in-day').value.trim(),
    total: $('in-total').value.trim(),
    cash: $('in-cash').value.trim(),
    market_value: $('in-mv').value.trim(),
    positions,
  };
}

function renderPlan(plan) {
  const box = $('plan-box');
  if (!plan) {
    box.replaceChildren(el('p', 'hint', '没有结果。'));
    return;
  }
  const summary = el('div', 'plan-summary');
  const item = (label, value, suffix = '', className = '') => {
    const span = el('span');
    span.append(document.createTextNode(label), el('b', className, value));
    if (suffix) span.append(document.createTextNode(suffix));
    return span;
  };
  summary.append(
    item('日期 ', plan.as_of || '—'), item('持仓 ', plan.count ?? 0, ' 只'),
    item('按现价算市值 ', money(plan.computed_market_value)),
    item('面板市值 ', money(plan.market_value)),
    item('差额 ', signed(plan.diff, 2), '', Number(plan.diff) === 0 ? '' : (plan.mismatch ? 'down' : '')),
  );
  const issues = [summary];
  (plan.problems || []).forEach((text) => issues.push(el('div', 'issue bad', text)));
  (plan.warnings || []).forEach((text) => issues.push(el('div', 'issue warn', text)));
  if (issues.length === 1 && plan.ok) {
    issues.push(el('div', 'issue ok', '校验通过，可以保存。'));
  }
  box.replaceChildren(...issues);
}

/* ------------------------------------------------------------- actions */
async function loadAccount() {
  busy(true, '读取账户…');
  try {
    // 第一遍不取行情：快照里已经有券商自己的市值，几毫秒就能画出来。
    const fast = await call('account', false);
    state.account = fast;
    renderAccount();
  } catch (err) {
    $('verdict-line').textContent = String(err.message || err);
    $('verdict').className = 'verdict bad';
  } finally {
    busy(false);
  }

  // 第二遍补实时价。行情源慢的时候（首次调用要 8 秒）这里失败也不影响上面的画面。
  try {
    const full = await call('account', true);
    if (full) {
      state.account = full;
      renderAccount();
    }
  } catch (err) {
    /* 保留快照视图 */
  }
}

async function loadEvents() {
  try {
    state.events = await call('events');
    renderEvents();
  } catch (err) { /* 时间线坏了不该挡住账户 */ }
}

async function loadSystem() {
  $('system-loading').hidden = false;
  $('system-loading').textContent = '正在读取系统状态…（首次要读数据集健康度，约 5–8 秒）';
  try {
    state.system = await call('system');
    renderSystem();
    $('system-loading').hidden = true;
  } catch (err) {
    $('system-loading').textContent = `系统块读不到：${err.message || err}`;
    $('system-loading').className = 'empty bad';
  }
}

async function refreshAll() {
  const button = $('btn-reload');
  button.classList.add('spin');
  await Promise.all([loadAccount(), loadEvents(), loadSystem()]);
  button.classList.remove('spin');
  const now = new Date();
  $('stamp').textContent = `刷新于 ${now.toTimeString().slice(0, 8)}`;
}

function quickTrade() {
  showTab('sync');
  $('t-date').value = $('t-date').value || state.meta?.today || '';
  $('t-symbol').focus();
  window.scrollTo({ top: 0 });
}

async function updateFeeHint() {
  const price = Number($('t-price').value || 0);
  const qty = Number($('t-qty').value || 0);
  if (!price || !qty) { $('fee-hint').textContent = ''; return; }
  try {
    const fee = await call('quote_fee', $('t-side').value, price, qty);
    $('fee-hint').textContent = fee && fee.total !== undefined
      ? `费用约 ${Number(fee.total).toFixed(2)}（${fee.rate_note || ''}）` : '';
  } catch (err) { $('fee-hint').textContent = ''; }
}

async function parsePaste() {
  const text = $('paste-box').value;
  if (!text.trim()) { toast('先粘贴内容', true); return; }
  busy(true, '解析…');
  $('btn-import').hidden = true;
  try {
    const result = await call('parse_text', text);
    if (!result.ok) {
      $('parse-hint').textContent = result.message || '解析失败';
      $('parse-hint').className = 'hint err';
      return;
    }
    if (result.kind === 'positions') {
      $('edit-body').replaceChildren();
      let mv = 0;
      result.rows.forEach((row) => {
        addRow(row);
        mv += Number(row.market_value || 0);
      });
      if (mv > 0) $('in-mv').value = mv.toFixed(2);
      if (!$('in-day').value) $('in-day').value = result.as_of || state.meta?.today || '';
      $('parse-hint').textContent = `认出 ${result.rows.length} 只，表头来自 ${result.profile}`;
      $('parse-hint').className = 'hint ok';
    } else {
      $('btn-import').hidden = false;
      $('btn-import').textContent = `写入这 ${result.rows.length} 笔成交`;
      $('parse-hint').textContent = `这是成交流水（${result.rows.length} 行），不是持仓`;
      $('parse-hint').className = 'hint';
    }
  } catch (err) {
    $('parse-hint').textContent = String(err.message || err);
    $('parse-hint').className = 'hint err';
  } finally { busy(false); }
}

async function importTrades() {
  busy(true, '写入成交…');
  try {
    const result = await call('import_text', $('paste-box').value);
    $('parse-hint').textContent = result.message || '';
    $('parse-hint').className = `hint ${result.ok ? 'ok' : 'err'}`;
    toast(result.message || (result.ok ? '已写入' : '没有写入'), !result.ok);
    if (result.ok) {
      $('btn-import').hidden = true;
      await loadAccount();
      await loadEvents();
    }
  } catch (err) {
    toast(String(err.message || err), true);
  } finally { busy(false); }
}

async function previewPlan() {
  busy(true, '校验…');
  try {
    const plan = await call('preview', readForm());
    state.plan = plan;
    renderPlan(plan);
  } catch (err) {
    toast(String(err.message || err), true);
  } finally { busy(false); }
}

async function savePlan() {
  const payload = readForm();
  busy(true, '写入快照…');
  try {
    const result = await call('commit', payload);
    if (result.ok) {
      toast(result.message || '已保存');
      state.plan = null;
      await loadAccount();
      await loadEvents();
      $('plan-box').replaceChildren(el('div', 'issue ok', result.message || '已保存'));
      showTab('live');
    } else {
      renderPlan({ ok: false, problems: result.problems || [result.message || '保存失败'], warnings: [] });
      toast(result.message || '没有保存', true);
    }
  } catch (err) {
    toast(String(err.message || err), true);
  } finally { busy(false); }
}

async function addTrade() {
  const row = {
    trade_date: $('t-date').value.trim() || state.meta?.today || '',
    symbol: $('t-symbol').value.trim(),
    side: $('t-side').value,
    qty: $('t-qty').value.trim(),
    price: $('t-price').value.trim(),
    fee: $('t-fee').value.trim(),
  };
  if (!row.symbol || !row.qty || !row.price) { toast('代码、数量、价格都要填', true); return; }
  busy(true, '写入成交…');
  try {
    const result = await call('add_trade', row);
    $('trade-hint').textContent = result.message || '';
    $('trade-hint').className = `hint ${result.ok ? 'ok' : 'err'}`;
    if (result.ok) {
      toast(result.message || '已写入');
      ['t-symbol', 't-qty', 't-price', 't-fee'].forEach((id) => { $(id).value = ''; });
      $('fee-hint').textContent = '';
      await loadAccount();
      await loadEvents();
      fillFormFromAccount();
    } else {
      toast(result.message || '没有写入', true);
    }
  } catch (err) {
    toast(String(err.message || err), true);
  } finally { busy(false); }
}

/* ------------------------------------------------------------------ boot */
function wire() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => showTab(tab.dataset.tab);
  });
  $('btn-reload').onclick = refreshAll;
  $('btn-quick-trade').onclick = quickTrade;
  $('btn-add-row').onclick = () => addRow();
  $('btn-clear-rows').onclick = () => { $('edit-body').replaceChildren(); addRow(); };
  $('btn-parse').onclick = parsePaste;
  $('btn-import').onclick = importTrades;
  $('btn-preview').onclick = previewPlan;
  $('btn-save').onclick = savePlan;
  $('btn-add-trade').onclick = addTrade;
  ['t-price', 't-qty', 't-side'].forEach((id) => {
    $(id).addEventListener('input', updateFeeHint);
    $(id).addEventListener('change', updateFeeHint);
  });
  $('btn-all-acts').onclick = () => showTab('acts');

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'r') {
      event.preventDefault();
      refreshAll();
    }
  });
}

function resetTradeForm() {
  ['t-symbol', 't-qty', 't-price', 't-fee'].forEach((id) => { $(id).value = ''; });
  $('t-date').value = '';
  $('fee-hint').textContent = '';
  $('trade-hint').textContent = '';
}

async function boot() {
  wire();
  resetTradeForm();
  try {
    state.meta = await call('bootstrap');
  } catch (err) {
    state.meta = { day: '', now: new Date().toISOString() };
  }
  renderMeta();
  await Promise.all([loadAccount(), loadEvents(), loadSystem()]);
  // 深链接（#system / #sync）只在数据到齐后切，否则同步页的表格是空的。
  const tab = initialTab();
  if (tab !== 'live') showTab(tab);
}

function bootDemo() {
  state.demo = true;
  state.meta = { day: '20261026', today: '20261027', now: new Date().toISOString().slice(0, 19) };
  state.account = demoAccount();
  state.events = [
    { ts: '2026-10-27T09:31:00', kind: 'proposal_blocked', label: '被拒', tone: 'bad',
      title: 'PAPER_ONLY', detail: '实盘袖未开启；模拟账户照常运行' },
    { ts: '2026-10-27T13:05:00', kind: 'fill', label: '成交', tone: 'good',
      title: '000590.SZ SELL 200 股 @ 8.12', detail: '费用 5.00' },
    { ts: '2026-10-27T17:35:00', kind: 'review', label: '复盘', tone: 'note',
      title: '', detail: '清仓，空仓过夜；明天看竞价线再决定是否接回。' },
  ];
  state.system = {
    paper: { equity: 5001.2, cash_available: 5001.2, market_value: 0, unrealized_pnl: 0,
             total_return: 0.0002, drawdown: 0, fees_paid: 12.4 },
    regime: '修复',
    persona: { current: { name: '游资', style: '打板与接力，持股一到三天', switches_today: 1 },
               available: [{ name: '核心', ready: true }, { name: '游资', ready: false, missing_requirements: ['rt_quote'] }] },
    sentiment: { tone: '冰点', break_rate: 0.42, max_boards: 3 },
    duty: { items: [{ priority: 1, detail: '补 000590 的成交流水' }] },
    arena: { edge: { n: 3 }, verdict: 'insufficient', why: '平仓样本不足 20 笔', sleeves: { core: { nav: 5001.2, return: 0.0002 } } },
    brief: { core: { datasets: { daily: { lag_trading_days: 0 } } },
             aggressive: { datasets: { limit_pool_t0: { lag_trading_days: 0 } }, usable: true, missing: [] },
             aux: { datasets: {} } },
    probe: { rt_quote: { tradable: false, feed_available: true } },
  };
  resetTradeForm();
  renderMeta(); renderAccount(); renderEvents(); renderSystem(); fillFormFromAccount();
  $('t-date').value = state.meta.today || state.meta.day;
  const tab = initialTab();
  if (tab !== 'live') showTab(tab);
  toast('演示模式：没有连上 Python 后端', false);
}
if (window.pywebview) {
  window.addEventListener('pywebviewready', boot);
  // 有些版本会在脚本执行前就绪，两个入口都留，boot 自身是幂等的。
  setTimeout(() => { if (!state.meta) boot(); }, 120);
} else {
  window.addEventListener('pywebviewready', boot);
  setTimeout(() => { if (!state.meta) bootDemo(); }, 500);
}
