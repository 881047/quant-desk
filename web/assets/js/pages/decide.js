// 决策：把判断写进票据，再把票据变成钱。两步必须分开。
//
// 提案（propose）只是生成票据，不动任何资金；审批（approve）才会。界面用词必须
// 把这条差别说清楚 —— 一个写着"买入"的按钮在 propose 上就是错的。

import { api, post, qs } from '../api.js';
import {
  h, panel, chip, table, empty, errorBox, slot, kv, confirmAction, toast,
  drawer, judge, seg,
} from '../ui.js';
import { money, ratio, ymd, hhmmss, num, tone, fp2, firstLine } from '../fmt.js';
import { zh } from '../zh.js';

const state = { symbol: '', tickets: [], root: null };
let pageRoot = null;

export async function mount(ctx) {
  state.symbol = (ctx.params.symbol || '').toUpperCase();
  const root = h('div', { class: 'stack' });
  state.root = root;
  pageRoot = root;

  const body = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  const left = h('div', { class: 'stack' });
  const right = h('div', { class: 'stack' });
  body.append(left, right);
  root.append(body);

  const formHost = h('div', {});
  left.append(panel({
    title: '提案',
    note: '生成票据 · 不动钱',
    body: formHost,
    footer: '这一步只生成票据，不冻结、不扣减任何资金；钱只有当你在票据上点「批准并下单」后才会动。字段由服务端校验，前端只做第一道提示。',
  }));
  right.append(
    panel({ title: '票据', note: '状态以服务端返回为准', body: h('div', { id: 'tickets' }), flush: true }),
    panel({ title: '观察层', note: '压着账户风险的事实', body: h('div', { id: 'obs' }) }),
    panel({ title: '今日拒绝记录', note: '为什么不买，也要留痕', body: h('div', { id: 'declined' }) }),
    panel({ title: '决策历史', note: '这台台子做过的每个决定，含当时的理由', body: h('div', { id: 'history' }) }),
  );

  buildForm(formHost, ctx);
  slot(pageRoot?.querySelector('#tickets'), () => ticketsBlock(ctx), { lines: 4 });

  // `#/decide?id=t-xxx` 直达某张票：路由表承诺过这个参数，页面得接住。
  // 等票据列表画出第一屏再开抽屉，否则抽屉里只有"正在取票据"。
  const wantId = ctx.params.id || null;
  if (wantId) {
    let tries = 0;
    const wait = setInterval(() => {
      tries += 1;
      const host = pageRoot?.querySelector('#tickets');
      const ready = host && host.isConnected && host.children.length > 0;
      if (ready || tries > 40) {
        clearInterval(wait);
        if (ready) openTicket(wantId);
      }
    }, 200);
    ctx.onCleanup?.(() => clearInterval(wait));
  }
  slot(root.querySelector('#obs'), () => obsBlock(), { lines: 3 });
  slot(root.querySelector('#declined'), () => declinedBlock(), { lines: 3 });
  slot(root.querySelector('#history'), () => historyBlock(), { lines: 4 });

  return root;
}

export function unmount() { state.root = null; }
export async function onRefresh() {
  const host = pageRoot?.querySelector('#tickets');
  if (host) slot(host, () => ticketsBlock({ refresh: () => {} }), { lines: 4 });
}

// ------------------------------------------------------------------ 表单
function buildForm(host, ctx) {
  const symbolInput = h('input', {
    class: 'input input-num', placeholder: '600519.SH', value: state.symbol,
    style: { width: '132px' }, 'aria-label': '代码',
  });
  const side = h('select', { class: 'select', style: { width: '86px' } },
    h('option', { value: 'BUY' }, '买入'), h('option', { value: 'SELL' }, '卖出'));
  const sleeve = h('select', { class: 'select', style: { width: '120px' } },
    h('option', { value: 'CORE' }, '核心'), h('option', { value: 'AGGRESSIVE' }, '进攻'));
  const qty = h('input', { class: 'input input-num', type: 'number', min: '1', step: '100', value: '100', style: { width: '92px' }, 'aria-label': '股数' });
  // 两种意图都合法，但不能同时表达：一个说买几股，一个说占多少仓位。
  // 服务端本来就支持 target_weight（gt=0, le=1），界面不该只给股数一种说法。
  const weight = h('input', {
    class: 'input input-num', type: 'number', min: '1', max: '100', step: '5', value: '20',
    style: { width: '92px' }, 'aria-label': '目标仓位（百分比）', hidden: 'hidden',
  });
  let sizeMode = 'qty';
  const strategy = h('select', { class: 'select', style: { width: '120px' } },
    h('option', { value: 'llm' }, '模型决策'), h('option', { value: 'core_etf' }, '核心 ETF'));
  const thesis = h('textarea', { class: 'textarea', rows: '4', placeholder: '为什么是这只、这个位置、这个时点。至少 20 字。' });
  const evidence = h('textarea', { class: 'textarea', rows: '3', placeholder: '一条一行。至少 1 条，最多 8 条。只写能复核的事实。' });
  const invalidation = h('textarea', { class: 'textarea', rows: '2', placeholder: '什么情况算判断错了（至少 5 字）。' });
  const directed = h('select', { class: 'select', style: { width: '130px' } },
    h('option', { value: 'agent' }, '模型自己的判断'), h('option', { value: 'user' }, '用户明确指令'));
  const reqId = h('input', { class: 'input input-num', value: newId(), style: { width: '100%' }, 'aria-label': '幂等句柄' });
  const err = h('div', {});
  const result = h('div', {});

  const estimate = h('div', { class: 'micro faint' }, '填代码与股数后自动测算');
  // 定义在 estimate 之后：切换回调会改它的文案，提前引用会踩 TDZ。
  const sizeToggle = seg(
    [{ label: '股数', value: 'qty' }, { label: '仓位 %', value: 'weight' }],
    'qty',
    (v) => {
      sizeMode = v;
      qty.hidden = v !== 'qty';
      weight.hidden = v !== 'weight';
      estimate.textContent = v === 'weight'
        ? '按仓位提案时不做冲击测算：那个数按股数算。股数由服务端按净值折算。'
        : '填代码与股数后自动测算';
      if (v === 'qty') estimates();
    },
    { label: '下单量怎么表达' },
  );
  const estimates = async () => {
    const s = symbolInput.value.trim().toUpperCase();
    const q = Number(qty.value);
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(s) || !q) return;
    try {
      const d = await api(`/execution/estimate${qs({ symbol: s, side: side.value, qty: q })}`, { ttl: 0, silent: true });
      if (!d) return;
      estimate.replaceChildren(
        h('span', {}, `参考价 ${fp2(d.price)}（${d.price_source === 'server_last' ? '最近收盘' : d.price_source}）`),
        h('span', { style: { marginLeft: '10px' } }, `参与率 ${ratio(d.participation, 2)}`),
        h('span', { style: { marginLeft: '10px' } }, `冲击 ${num(d.impact_bps, 1)} bps`),
        h('span', { style: { marginLeft: '10px' } }, `费用 ${money(d.cost_cny)}`));
    } catch { /* 测算失败不该挡住提案 */ }
  };
  symbolInput.addEventListener('change', estimates);
  qty.addEventListener('change', estimates);

  const submit = async () => {
    err.replaceChildren();
    const qtyRaw = qty.value.trim();
    const body = {
      symbol: symbolInput.value.trim().toUpperCase(),
      side: side.value,
      sleeve: sleeve.value,
      ...(sizeMode === 'qty'
        ? { qty: qtyRaw === '' ? null : Number(qtyRaw) }
        : { target_weight: weight.value.trim() === '' ? null : Number(weight.value) / 100 }),
      strategy: strategy.value,
      thesis: thesis.value.trim(),
      evidence: evidence.value.split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 8),
      invalidation: invalidation.value.trim(),
      directed_by: directed.value,
      client_request_id: reqId.value.trim(),
    };
    const problems = [];
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(body.symbol)) problems.push('代码要写成 600519.SH 这种带后缀的');
    if (body.thesis.length < 20) problems.push(`判断依据只有 ${body.thesis.length} 字，至少要 20 字`);
    if (!body.evidence.length) problems.push('证据至少要写 1 条');
    if (body.invalidation.length < 5) problems.push('失效条件至少要 5 字');
    if (body.client_request_id.length < 8) problems.push('幂等句柄至少 8 位');
    if (sizeMode === 'qty') {
      if (!Number.isInteger(body.qty) || body.qty < 1) problems.push('股数必须是正整数（空着不会按 100 股下单）');
    } else if (!(body.target_weight > 0 && body.target_weight <= 1)) {
      problems.push('目标仓位要在 1% 到 100% 之间');
    }
    if (problems.length) {
      err.replaceChildren(h('div', { class: 'err' }, h('div', { class: 'code' }, zh.code('CHECK_FORM')),
        ...problems.map((p) => h('div', {}, p))));
      return;
    }
    btn.disabled = true;
    btn.textContent = '提交中…';
    try {
      const ticket = await post('/decision/propose', body);
      result.replaceChildren(ticketCard(ticket, { fresh: true }));
      reqId.value = newId();
      toast('票据已生成，资金未动', 'ok', ticket.ticket_id || ticket.id || 'propose');
      const host = pageRoot?.querySelector('#tickets');
      if (host) slot(host, () => ticketsBlock(ctx), { lines: 4 });
    } catch (e) {
      err.replaceChildren(proposeError(e, body));
    } finally {
      btn.disabled = false;
      btn.textContent = '生成票据（不出钱）';
    }
  };
  const btn = h('button', { class: 'btn btn-primary', onclick: submit }, '生成票据（不出钱）');

  host.replaceChildren(h('div', { class: 'stack', style: { gap: '10px' } },
    h('div', { class: 'row', style: { flexWrap: 'wrap' } },
      h('div', { class: 'field' }, h('label', {}, '代码'), symbolInput),
      h('div', { class: 'field' }, h('label', {}, '方向'), side),
      h('div', { class: 'field' }, h('label', {}, '仓位类型'), sleeve),
      h('div', { class: 'field' }, h('label', {}, '下单量'), h('div', { class: 'row-tight' }, sizeToggle?.el || sizeToggle, qty, weight)),
      h('div', { class: 'field' }, h('label', {}, '策略'), strategy)),
    h('div', { class: 'field' }, h('label', {}, '判断依据（为什么是它）'), thesis),
    h('div', { class: 'field' }, h('label', {}, '证据（可复核的事实）'), evidence, h('div', { class: 'hint' }, '一条一行；只写能对上数的事实，不写感受。')),
    h('div', { class: 'field' }, h('label', {}, '失效条件（什么算看错）'), invalidation),
    h('div', { class: 'row', style: { flexWrap: 'wrap' } },
      h('div', { class: 'field' }, h('label', {}, '谁下的决定'), directed),
      h('div', { class: 'field', style: { flex: '1', minWidth: '220px' } },
        h('label', { title: 'client_request_id' }, '幂等句柄（重试不会变成两张票）'),
        h('div', { class: 'row-tight' }, reqId,
          h('button', { class: 'btn btn-sm', onclick: () => { reqId.value = newId(); } }, '换一个')))),
    h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
      '超时重试必须原样带同一个句柄：同一个 id 永远返回同一张票，不会扣两次钱。换一个 id 就是另一张票。'),
    estimate,
    err,
    h('div', { class: 'row' }, btn),
    result));
  setTimeout(estimates, 60);
}

function newId() {
  const uuid = (crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`);
  return `web-${uuid}`.slice(0, 40);
}

/** 服务端拒绝的原文照抄：code 是给程序看的，message 是给人看的，next 是下一步。 */
function proposeError(e, request) {
  return h('div', { class: 'err' },
    h('div', { class: 'code', title: e.code || '' }, zh.code(e.code) || `服务返回 ${e.status}`),
    h('div', {}, e.message || '提案失败'),
    e.next ? h('div', { class: 'next' }, e.next) : null,
    h('details', { style: { marginTop: '4px' } },
      h('summary', { class: 'micro faint', style: { cursor: 'pointer' } }, '我提交了什么'),
      // 这段 JSON 在暗底 .err 里，更暗的那档只有 3.55，用 --ink-dim（4.54）
      h('pre', { class: 'micro', style: { whiteSpace: 'pre-wrap', color: 'var(--ink-dim)', margin: '4px 0 0' } },
        JSON.stringify(request, null, 2))));
}

function ticketCard(t, { fresh = false } = {}) {
  const id = t.ticket_id || t.id || '—';
  const status = t.status || 'proposed';
  const armed = status === 'armed';
  return h('div', { class: 'panel', style: { borderColor: fresh ? 'var(--accent)' : 'var(--line)' } },
    h('div', { class: 'panel-hd' },
      h('h3', {}, fresh ? '票据已生成' : id),
      chip(zh.status(status), armed ? 'warn' : status === 'filled' ? 'ok' : 'acc'),
      h('div', { class: 'hd-actions' },
        h('span', { class: 'micro faint num' }, t.created_at ? hhmmss(t.created_at) : '')),
      ),
    h('div', { class: 'panel-bd' },
      h('div', { class: 'stack', style: { gap: '6px' } },
        kv([
          ['代码', `${t.symbol || '—'} ${t.side ? zh.side(t.side) : ''} ${t.qty ? `${num(t.qty)} 股` : ''}`],
          ['限价', t.limit_price ? fp2(t.limit_price) : '—'],
          ['止损', t.stop_price ? fp2(t.stop_price) : '—'],
          ['仓位类型', t.sleeve ? zh.sleeve(t.sleeve) : '—'],
          ['票号', id],
        ]),
        armed
          ? h('div', { class: 'micro', style: { color: 'var(--al-stale)' } }, '已批未成交是可以撤销的中间态：撤销不会变成"被否决"，也不会动钱。')
          : null,
        fresh ? h('div', { class: 'micro faint' }, '资金没有任何变动。下一步是否审批，由你决定。') : null)));
}

// ------------------------------------------------------------------ 票据列表
function ticketsBlock(ctx) {
  return api('/tickets?limit=50', { ttl: 5000 }).then((d) => {
    const rows = d.tickets || [];
    state.tickets = rows;
    if (!rows.length) {
      return h('div', { class: 'empty' },
        h('b', {}, '账本里没有票据'),
        h('div', {}, '提案生成票据后出现在这里；批准才会进入下单。没有票据 = 今天没有可执行的决定。'));
    }
    return h('div', { class: 'stack', style: { gap: '0' } }, rows.map((t) => ticketRow(t, ctx)));
  });
}

function ticketRow(t, ctx) {
  const id = t.ticket_id || t.id;
  const status = t.status || '—';
  const kind = status === 'filled' ? 'ok' : status === 'armed' ? 'warn' : status === 'rejected' || status === 'cancelled' ? 'off' : 'acc';
  const actions = h('div', { class: 'row-tight' });
  if (status === 'proposed') {
    actions.append(
      h('button', { class: 'btn btn-sm btn-primary', onclick: () => approve(id, t, ctx) }, '批准'),
      h('button', { class: 'btn btn-sm', onclick: () => reject(id, ctx) }, '否决'));
  } else if (status === 'armed') {
    actions.append(h('button', { class: 'btn btn-sm', onclick: () => cancel(id, ctx) }, '撤销'));
  }
  return h('div', { style: { padding: '8px 10px', borderBottom: '1px solid var(--line-soft)' } },
    h('div', { class: 'row', style: { gap: '6px' } },
      h('span', { class: 'sym', style: { fontFamily: 'var(--font-num)' } }, t.symbol || '—'),
      h('span', { class: 'small' }, t.side ? zh.side(t.side) : ''),
      h('span', { class: 'micro faint num' }, t.qty ? `${num(t.qty)} 股` : ''),
      chip(zh.status(status), kind),
      h('div', { class: 'spacer' }),
      actions),
    // 被 guard 拒掉的票，列表摘要里原本只有一枚"已否决"的标记 —— 那等于告诉用户
    // "不行"却不告诉为什么。拒因就在 ticket.reasons 里，直接摊在行内。
    // 前缀（检查名）翻中文，正文原样；原始句子进 title，鼠标放上去能对回后端原文。
    status === 'rejected' && (t.reasons || []).length
      ? h('div', { class: 'micro', style: { marginTop: '2px', color: 'var(--ink-mid)' }, title: t.reasons.join('\n') },
        t.reasons.length > 1 ? `${zh.reason(t.reasons[0])} 等 ${t.reasons.length} 条` : zh.reason(t.reasons[0]))
      : null,
    h('div', { class: 'micro faint num', style: { marginTop: '2px' } },
      [id, t.limit_price ? `限价 ${fp2(t.limit_price)}` : null, t.stop_price ? `止损 ${fp2(t.stop_price)}` : null,
        t.created_at ? hhmmss(t.created_at) : null].filter(Boolean).join(' · ')),
    h('div', { class: 'row', style: { marginTop: '3px' } },
      h('button', { class: 'btn btn-sm', onclick: () => openTicket(id) }, '详情')));
}

/**
 * 票据全文。列表是索引（`ticket_brief` 故意不带 checks，20 张会顶到 56KB），
 * 单张才是记录 —— guard 的 25 项检查、快照、拒因都在这里。
 */
async function openTicket(id) {
  const box = h('div', { class: 'stack' }, h('div', { class: 'micro faint' }, '正在取票据…'));
  const d = drawer({ title: `票据 ${id}`, body: box, label: '票据全文' });
  let t;
  try {
    t = await api(`/tickets/${encodeURIComponent(id)}`, { ttl: 0 });
  } catch (e) {
    box.replaceChildren(errorBox(e));
    return;
  }
  box.replaceChildren();
  const checks = t.checks?.checks || [];
  const append = (node) => { if (node) box.appendChild(node); };

  append(kv([
    ['票号', t.ticket_id || id],
    ['状态', zh.status(t.status) || t.status || '—'],
    ['代码', `${t.symbol || '—'}${t.name ? ` ${t.name}` : ''}`],
    ['方向 / 数量', `${t.side ? zh.side(t.side) : '—'} · ${t.qty ? `${num(t.qty)} 股` : '—'}`],
    ['限价 / 止损', `${t.limit_price ? fp2(t.limit_price) : '—'} / ${t.stop_price ? fp2(t.stop_price) : '—'}`],
    ['参考价', t.price_ref ? fp2(t.price_ref) : '—'],
    ['仓位类型', t.sleeve ? zh.sleeve(t.sleeve) : '—'],
    ['交易日', t.trade_date ? ymd(t.trade_date) : '—'],
    ['生成 / 决策', [t.created_at ? hhmmss(t.created_at) : null,
      t.decision_ts ? hhmmss(t.decision_ts) : '未决策'].filter(Boolean).join(' → ')],
  ]));

  append(h('div', { class: 'micro faint', style: { marginTop: '6px' } }, '判断依据'));
  append(kv([
    ['逻辑', t.thesis || '—'],
    ['失效条件', t.invalidation || '—'],
    ['证据', (t.evidence || []).join('、') || '—'],
  ]));

  if ((t.reasons || []).length) {
    append(h('div', { class: 'micro', style: { marginTop: '8px', color: 'var(--ink)' } }, '被拒原因'));
    append(h('ul', { class: 'stack', style: { gap: '2px', margin: '2px 0 0', paddingLeft: '16px' } },
      // title 保留后端原句：中文前缀方便读，原始 `name: detail` 才是能拿去对代码的东西。
      t.reasons.map((r) => h('li', { class: 'micro', title: r }, zh.reason(r)))));
  }

  if (checks.length) {
    append(h('div', { class: 'micro', style: { marginTop: '8px' } },
      `风控检查 ${checks.length} 项 · ${checks.filter((c) => c.ok).length} 过 · ${checks.filter((c) => !c.ok).length} 不过`));
    append(h('table', { class: 'tbl tbl-checks' },
      h('thead', {}, h('tr', {}, h('th', {}, '检查项'), h('th', {}, '结果'), h('th', {}, '说明'))),
      h('tbody', {}, checks.map((c) => h('tr', { class: c.ok ? 'is-pass' : 'is-fail' },
        h('td', { class: 'gc-name', title: c.name || '' }, judge(c.ok, zh.check(c.name || ''))),
        h('td', { class: 'num' }, c.ok ? '过' : '不过'),
        // 后端给了 detail_zh 与 structured；英文原句留在 title，改写不丢定位。
        h('td', { class: 'gc-detail', title: c.detail || '' }, c.detail_zh || c.detail || '—'))))));
  } else if (t.status === 'rejected') {
    append(h('div', { class: 'micro faint', style: { marginTop: '8px' } },
      '这张票在 guard 跑之前就被拒了（字段或前置条件不合），所以没有检查明细。详见上面的拒因。'));
  }

  append(h('details', { style: { marginTop: '8px' } },
    h('summary', { class: 'micro faint', style: { cursor: 'pointer' } }, '原始记录'),
    // 票的原始记录是能对账的，不是装饰：更暗的那档在 raised 上只有 3.55，用 --ink-dim
    h('pre', { class: 'micro', style: { whiteSpace: 'pre-wrap', color: 'var(--ink-dim)', margin: '4px 0 0' } },
      JSON.stringify(t, null, 2))));
  return d;
}

async function approve(id, t, ctx) {
  const ok = await confirmAction({
    title: '批准这张票据',
    message: h('div', { class: 'stack', style: { gap: '6px' } },
      h('div', { class: 'small', style: { color: 'var(--ink-mid)', lineHeight: '1.7' } },
        '批准之后，这张票会进入下单；这是**唯一**会让资金变动的动作。'),
      kv([
        ['标的', `${t.symbol || ''} ${t.side ? zh.side(t.side) : ''}`],
        ['数量', t.qty ? `${num(t.qty)} 股` : '—'],
        ['限价', t.limit_price ? fp2(t.limit_price) : '市价/未定'],
        ['止损', t.stop_price ? fp2(t.stop_price) : '—'],
      ])),
    confirmText: '批准并下单',
    danger: false,
  });
  if (!ok) return;
  try {
    const res = await post(`/decision/${encodeURIComponent(id)}/approve`, { confirm: true });
    toast(`已批准：${res.status || 'ok'}`, 'ok', id);
    const host = pageRoot?.querySelector('#tickets');
    if (host) slot(host, () => ticketsBlock(ctx), { lines: 4 });
  } catch (e) {
    toast(e.message || '批准失败', 'bad', e.code);
  }
}

async function reject(id, ctx) {
  const reason = await promptReason('否决这张票据', '写清为什么不做 —— 这句话会留在审计记录里。');
  if (!reason) return;
  try {
    await post(`/decision/${encodeURIComponent(id)}/reject`, { reason });
    toast('已否决', 'ok', id);
    const host = pageRoot?.querySelector('#tickets');
    if (host) slot(host, () => ticketsBlock(ctx), { lines: 4 });
  } catch (e) {
    toast(e.message || '否决失败', 'bad', e.code);
  }
}

async function cancel(id, ctx) {
  const reason = await promptReason('撤销这张票据', '已批未成交的票可以撤销，撤销不会变成"被否决"。');
  // 取消时 promptReason resolve 空串 —— 不早退就会继续发 POST，Esc 变成撤单
  if (!reason) return;
  try {
    await post(`/decision/${encodeURIComponent(id)}/cancel`, { reason: reason || '' });
    toast('已撤销', 'ok', id);
    const host = pageRoot?.querySelector('#tickets');
    if (host) slot(host, () => ticketsBlock(ctx), { lines: 4 });
  } catch (e) {
    toast(e.message || '撤销失败', 'bad', e.code);
  }
}

function promptReason(title, hint) {
  return import('../ui.js').then(({ modal }) => modal({
    title,
    body: h('div', { class: 'stack', style: { gap: '8px' } },
      h('div', { class: 'small muted' }, hint),
      h('textarea', { class: 'textarea', rows: '3', id: 'reason-box' })),
    footer: (finish) => [
      h('button', { class: 'btn', onclick: () => finish('') }, '取消'),
      h('button', {
        class: 'btn btn-primary',
        onclick: () => {
          const v = document.getElementById('reason-box')?.value.trim() || '';
          if (v.length < 3) { toast('理由至少 3 个字', 'bad'); return; }
          finish(v);
        },
      }, '确认'),
    ],
  }));
}

// ------------------------------------------------------------------ 侧块
function obsBlock() {
  return api('/observations', { ttl: 8000 }).then((o) => {
    const items = [...(o.constraints || []), ...(o.deviations || [])];
    if (!items.length) return empty('没有约束与偏离');
    return h('div', { class: 'stack', style: { gap: '7px' } },
      items.slice(0, 6).map((it) => h('div', { class: 'stack', style: { gap: '1px' } },
        h('div', { class: 'row small' },
          chip(zh.obs(it.kind), it.attention === 'high' ? 'bad' : 'off'),
          h('span', { class: 'muted' }, it.subject === 'core' ? '核心盘' : it.subject === 'llm' ? '模型盘' : (it.subject || ''))),
        (it.constraints || []).length
          ? h('div', { class: 'micro faint', style: { lineHeight: '1.6' } }, it.constraints.join(' · '))
          : null)));
  });
}

function declinedBlock() {
  return api('/declined?limit=3', { ttl: 30000 }).then((d) => {
    const rows = d.rows || [];
    if (!rows.length) return empty('今天没有拒绝记录');
    return h('div', { class: 'stack', style: { gap: '8px' } },
      rows.map((r) => h('details', { style: { borderLeft: '2px solid var(--line-strong)', paddingLeft: '8px' } },
        h('summary', { class: 'small', style: { cursor: 'pointer', color: 'var(--ink-mid)' } },
          `${hhmmss(r.ts)} · ${(r.symbols || []).join(' ')}`),
        h('div', { class: 'micro', style: { color: 'var(--ink-dim)', lineHeight: '1.65', marginTop: '4px' } }, r.reason || ''))));
  });
}

// ------------------------------------------------------------------ 决策历史
/** /decisions 是决策的动作流水（提案/批准/否决/撤销），不是票据列表。
    票据回答"现在手上有什么"，这里回答"这台台子都做过什么决定、当时的理由是什么"。 */
function historyBlock() {
  return api('/decisions?limit=12', { ttl: 20000 }).then((d) => {
    const rows = d.decisions || [];
    if (!rows.length) return empty('还没有决策记录', '第一张票据生成后会出现在这里。');
    return h('div', { class: 'stack', style: { gap: '9px' } }, rows.map((r) => {
      const why = r.payload?.reason || r.payload?.request?.thesis || '';
      const tone = r.kind === 'approve' ? 'ok' : (r.kind === 'reject' || r.kind === 'manual_reject') ? 'bad' : 'off';
      return h('div', { class: 'stack', style: { gap: '1px', borderLeft: '2px solid var(--line-soft)', paddingLeft: '8px' } },
        h('div', { class: 'row small' },
          h('span', { class: 'num faint' }, hhmmss(r.ts)),
          chip(zh.decision(r.kind), tone),
          r.ticket_id ? h('span', { class: 'micro faint num', title: r.ticket_id }, r.ticket_id.slice(0, 20)) : null,
          h('div', { class: 'spacer' }),
          r.actor ? h('span', { class: 'micro faint' }, r.actor === 'agent' ? '模型' : r.actor === 'user' ? '用户' : r.actor) : null),
        why ? h('div', {
          class: 'micro', title: why, style: {
            color: 'var(--ink-dim)', overflow: 'hidden',
            display: '-webkit-box', WebkitLineClamp: '2', WebkitBoxOrient: 'vertical',
          },
        }, firstLine(why)) : null);
    }));
  });
}
