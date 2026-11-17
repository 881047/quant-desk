// 复盘：这套系统到底有没有优势。净值不是证据，决策超额才是。
// 每一块都带 n 与区间；没有 n 的比较不放进表格。

import { api, post } from '../api.js';
import {
  h, panel, kpis, chip, table, empty, errorBox, slot, sectionHeader, confirmAction, toast, drawer,
} from '../ui.js';
import { money, pct, ymd, hhmmss, num, tone, toneStyle, esc, firstLine } from '../fmt.js';
import { zh } from '../zh.js';

export async function mount(ctx) {
  const root = h('div', { class: 'stack' });
  const head = h('div', {});
  root.append(head);

  // 复盘是"对照"动作：擂台和归因要并置看。所以不做页内 Tab（Tab 会藏掉一半），
  // 只给一条 sticky 目录 —— 长页滚动时不必回顶找入口。
  const tocItems = [
    ['sec-arena', '擂台与归因'],
    ['sec-decay', '衰减'],
    ['sec-journal', '复盘日志'],
    ['sec-settle', '结算与备忘'],
    ['sec-declined', '拒绝记录'],
  ];
  root.append(h('nav', { class: 'toc', 'aria-label': '复盘小节' },
    tocItems.map(([id, label]) => h('button', {
      class: 'btn btn-sm', type: 'button',
      onclick: () => root.querySelector(`#${id}`)?.scrollIntoView({ block: 'start' }),
    }, label))));

  const grid = h('div', { class: 'grid g2', id: 'sec-arena', style: { alignItems: 'start' } });
  grid.append(
    panel({ title: '擂台', note: '模型 vs 等权基准', body: h('div', { id: 'arena' }) }),
    panel({ title: '归因', note: '按策略', body: h('div', { id: 'attr' }), flush: true }),
  );
  root.append(grid);
  const decayPanel = panel({ title: '衰减', note: 'SPRT / CUSUM，需要 ≥20 笔', body: h('div', { id: 'decay' }) });
  decayPanel.id = 'sec-decay';
  const journalPanel = panel({
    title: '复盘日志', note: '数字写入时当场核，unmatched_detail 给出待修数字所在的原句',
    actions: h('button', { class: 'btn btn-sm', onclick: () => journalComposer(ctx) }, '写一条'),
    body: h('div', { id: 'journal' }),
  });
  journalPanel.id = 'sec-journal';
  root.append(decayPanel, journalPanel);

  const low = h('div', { class: 'grid g2', id: 'sec-settle', style: { alignItems: 'start' } });
  low.append(
    panel({ title: '平仓记录', note: '判断的结算', body: h('div', { id: 'cases' }) }),
    panel({
      title: '备忘录', note: '事前写下、到期结算',
      actions: h('button', { class: 'btn btn-sm', onclick: () => scoreAction({ path: '/memo/score', label: '备忘录', ctx }) }, '结算到期'),
      body: h('div', { id: 'memo' }),
    }),
  );
  root.append(low);
  const declinedPanel = panel({
    title: '拒绝记录', note: '为什么不买也要留痕',
    actions: h('button', { class: 'btn btn-sm', onclick: () => scoreAction({ path: '/declined/score', label: '拒绝记录', ctx }) }, '结算到期'),
    body: h('div', { id: 'declined' }),
  });
  declinedPanel.id = 'sec-declined';
  root.append(declinedPanel);

  slot(head, () => arenaKpi(), { lines: 2 });
  slot(root.querySelector('#arena'), () => arenaBlock(), { lines: 5 });
  slot(root.querySelector('#attr'), () => attrBlock(), { lines: 5 });
  slot(root.querySelector('#decay'), () => decayBlock(), { lines: 4 });
  slot(root.querySelector('#journal'), () => journalBlock(), { lines: 5 });
  slot(root.querySelector('#cases'), () => casesBlock(ctx), { lines: 4 });
  slot(root.querySelector('#memo'), () => memoBlock(), { lines: 4 });
  slot(root.querySelector('#declined'), () => declinedBlock(), { lines: 4 });
  return root;
}

export function unmount() {}

function arenaKpi() {
  return api('/arena', { ttl: 30000 }).then((a) => {
    const llm = a.sleeves?.llm || {};
    const mkt = a.sleeves?.market || {};
    // 双方 return 都在才算差值：一边缺数就差值 = 0，是"合法且完全错误"的典型。
    const gap = (llm.return === null || llm.return === undefined || mkt.return === null || mkt.return === undefined)
      ? null
      : llm.return - mkt.return;
    // 未计分不再只有一个计数：not_scored_detail 给出 case_ids / ticket_ids，
    // 告警能落成"去修哪张票"的入口，而不是让人对着数字猜。
    const ns = a.not_scored_detail || {};
    const nsTotal = Object.values(ns).reduce((sum, x) => sum + (Number(x.count) || 0), 0);
    const nsSub = Object.entries(ns).map(([key, x]) => {
      const ids = (x.ticket_ids?.length ? x.ticket_ids : x.case_ids) || [];
      const shown = ids.slice(0, 2).join('、');
      return `${zh.notScored(key) || key} ${x.count}${shown ? `（${shown}${ids.length > 2 ? '…' : ''}）` : ''}`;
    }).join(' · ') || '无';
    return kpis([
      { label: '模型盘', value: money(llm.nav), sub: `收益 ${pct(llm.return)}`, cls: tone(llm.return) },
      { label: '等权基准', value: money(mkt.nav), sub: `收益 ${pct(mkt.return)}`, cls: tone(mkt.return) },
      { label: '净值差', value: pct(gap), sub: `跑 ${a.days_running} 天`, cls: tone(gap) },
      { label: '已判定决策', value: `${a.edge?.n ?? 0}`, sub: a.edge?.mean_excess === null ? '样本不足' : `均值超额 ${pct(a.edge.mean_excess)}` },
      { label: '置信区间', value: a.ci_position === 'sample_below_20' ? '样本不足' : (a.ci_position || '—'), sub: `需要 ${a.min_decisions_for_an_interval} 笔` },
      { label: '未计分', value: `${nsTotal}`, sub: nsSub },
    ]);
  });
}

function arenaBlock() {
  return api('/arena', { ttl: 30000 }).then((a) => {
    const llm = a.sleeves?.llm || {};
    const mkt = a.sleeves?.market || {};
    const rows = a.decisions || [];
    return h('div', { class: 'stack', style: { gap: '9px' } },
      h('div', { class: 'row small' },
        h('span', { class: 'muted' }, '模型'), h('div', { class: 'spacer' }),
        h('span', { class: 'num' }, money(llm.nav)),
        h('span', { class: `num ${tone(llm.return)}`, style: { width: '68px', textAlign: 'right' } }, pct(llm.return))),
      h('div', { class: 'row small' },
        h('span', { class: 'muted' }, '基准'), h('div', { class: 'spacer' }),
        h('span', { class: 'num' }, money(mkt.nav)),
        h('span', { class: `num ${tone(mkt.return)}`, style: { width: '68px', textAlign: 'right' } }, pct(mkt.return))),
      h('div', { class: 'section' },
        sectionHeader(`已平仓决策 ${rows.length} 笔`),
        rows.length
          ? table({
            cols: [
              { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${esc(r.symbol)}</span>` },
              { key: 'opened', label: '开', cls: 'r', render: (r) => ymd(r.opened) },
              { key: 'closed', label: '平', cls: 'r', render: (r) => ymd(r.closed) },
              { key: 'pnl_pct', label: '收益', cls: 'r', render: (r) => `<span style="${toneStyle(r.pnl_pct)}">${pct(r.pnl_pct)}</span>` },
              { key: 'excess', label: '超额', cls: 'r', render: (r) => `<span style="${toneStyle(r.excess)}">${pct(r.excess)}</span>` },
            ],
            rows,
          })
          : empty('还没有结算过的决策')),
      a.core_retired ? h('div', { class: 'micro faint', style: { lineHeight: '1.6' } }, a.core_note) : null,
      a.edge?.note ? h('div', { class: 'micro faint' }, a.edge.note) : null);
  });
}

function attrBlock() {
  return api('/attribution', { ttl: 60000 }).then((d) => {
    const rows = (d.strategies || []).filter((s) => s.closed > 0 || s.status === 'live');
    return table({
      cols: [
        { key: 'strategy', label: '策略', render: (r) => `<span title="${esc(r.strategy)}">${esc(zh.strategy(r.strategy))}</span>` },
        { key: 'closed', label: '笔数', cls: 'r', render: (r) => `${r.closed}` },
        { key: 'wins', label: '胜', cls: 'r', render: (r) => `${r.wins}` },
        { key: 'pnl', label: '总盈亏', cls: 'r', render: (r) => `<span style="${toneStyle(r.total_pnl)}">${money(r.total_pnl)}</span>` },
        { key: 'mean', label: '均值', cls: 'r', render: (r) => r.mean_pnl_pct === null ? '—' : `<span style="${toneStyle(r.mean_pnl_pct)}">${pct(r.mean_pnl_pct)}</span>` },
        { key: 'excess', label: '超额', cls: 'r', render: (r) => r.excess_n ? `<span style="${toneStyle(r.mean_excess)}">${pct(r.mean_excess)}</span>` : chip('样本 0', 'off') },
          { key: 'status', label: '状态', cls: 'r', render: (r) => chip(r.status === 'live' ? '在跑' : '已退役', r.status === 'live' ? 'ok' : 'off') },
      ],
      rows,
      emptyText: '没有可归因的平仓记录',
    });
  });
}

function decayBlock() {
  return api('/decay', { ttl: 60000 }).then((d) => {
    const rows = d.strategies || [];
    return h('div', { class: 'stack', style: { gap: '6px' } },
      table({
        cols: [
          { key: 'key', label: '策略', render: (r) => `<span class="sym" title="${esc(r.key)}">${esc(zh.strategy(r.key) || r.key)}</span>` },
          { key: 'state', label: '判定', render: (r) => chip(zh.decay(r.state), r.state === 'decaying' ? 'bad' : r.state === 'insufficient' ? 'warn' : 'off') },
          { key: 'n', label: 'n', cls: 'r', render: (r) => `${r.n}` },
          { key: 'window', label: '窗口', cls: 'r', render: (r) => r.window ?? '—' },
          { key: 'note', label: '说明', render: (r) => `<span class="muted">${esc(decayNote(r))}</span>` },
        ],
        rows,
      }),
      h('div', { class: 'micro faint' }, d.rule || ''));
  });
}

// 日志列表一次显示多少条。库里有 77 条，只给 12 条且没有翻页，等于把历史藏起来。
let journalLimit = 12;

function journalBlock() {
  // 一次取满（接口上限 100），本地分页：`count` 是"这次返回了几条"，不是总数，
  // 想显示"还有 N 条"就得多打一次请求 —— 不值得，摘要一共几十 KB。
  return api('/journal?limit=100', { ttl: 30000 }).then((d) => {
    const all = d.entries || [];
    if (!all.length) return empty('还没有复盘记录');
    const rows = all.slice(0, journalLimit);
    return h('div', { class: 'stack', style: { gap: '10px' } }, rows.map((e, i) => {
      const v = e.verified;
      const badge = v === null || v === undefined
        ? chip('未核', 'warn')
        : v.unmatched_count
          // 不写"错"：核对器自己的注释写着，实测约三成数字本来就不在 facts
          //（自评条数、单笔金额、别的工具的口径）。这里只说"没对上"，
          // 判定留给读的人 —— 把存疑写成"错"，等于替他把结论下了。
          ? chip(`核 ${v.checked} · 存疑 ${v.unmatched_count}`, 'warn')
          : chip(`核 ${v.checked} · 全对`, 'ok');
      return h('div', { class: 'stack', style: { gap: '3px', borderLeft: '2px solid var(--line-strong)', paddingLeft: '9px' } },
        h('div', { class: 'row small' },
          h('span', { class: 'num faint' }, hhmmss(e.ts)),
          h('span', { class: 'sym' }, zh.journal(e.kind)),
          e.symbol ? h('span', { class: 'num muted' }, e.symbol) : null,
          badge,
          h('div', { class: 'spacer' }),
          h('span', { class: 'micro faint' }, `${e.chars} 字`)),
        // 日志正文常以空行开头。直接取第一行会得到一个空 summary，
        // 屏幕上只剩一个没有文字的光标 —— 取第一行**非空**文本。
        //
        // 列表接口默认只给最新 3 条全文，其余只有 summary（首 100 + 末 160 字）。
        // 早先这里只读 e.text，于是 12 条里 9 条显示「（没有正文）」、点开也是空的。
        // 现在：先用 summary 顶上，展开时按 id 取全文（/journal?id=N 会给全文）。
        journalEntry(e, i === 0),
        unmatchedList(v));
    }), moreRow(all.length, rows.length));
  });
}

/** 一条日志：标题行 + 可展开正文。展开时才去取全文（列表里只有摘要）。 */
function journalEntry(e, open) {
  const body = e.text || e.summary || '';
  const pre = h('pre', {
    class: 'small',
    style: { whiteSpace: 'pre-wrap', color: 'var(--ink-dim)', fontFamily: 'var(--font-ui)', lineHeight: '1.7', margin: '4px 0 0' },
  }, body);
  const details = h('details', open ? { open: '' } : {});
  details.addEventListener('toggle', () => {
    if (!details.open || e.text) return;   // 已有全文，或还没展开
    api(`/journal?id=${encodeURIComponent(e.id)}`, { ttl: 0, silent: true })
      .then((one) => {
        const full = (one?.entries || [])[0];
        if (full?.text) pre.textContent = full.text;
      })
      .catch(() => { /* 取不到就留着摘要，不要把已经显示的字擦掉 */ });
  });
  details.append(
    h('summary', { class: 'small', style: { cursor: 'pointer', color: 'var(--ink-mid)' } },
      firstLine(body).slice(0, 90) || '（没有正文）'),
    pre);
  return details;
}

/** 「共 N 条 · 显示 M 条」+ 载入更多。藏起来的历史等于没有。 */
function moreRow(total, shown) {
  const more = Number(total) > shown;
  const btn = more
    ? h('button', {
        class: 'btn btn-sm',
        onclick: () => {
          journalLimit = Math.min(100, journalLimit + 24);
          slot(document.querySelector('#journal'), journalBlock, { lines: 5 });
        },
      }, `载入更多（还有 ${Number(total) - shown} 条）`)
    : null;
  return h('div', { class: 'row micro faint', style: { gap: '8px' } },
    h('span', {}, `共 ${total ?? shown} 条 · 显示 ${shown} 条`),
    h('div', { class: 'spacer' }), btn);
}

function casesBlock(ctx) {
  return api('/cases?limit=20', { ttl: 30000 }).then((d) => {
    const rows = d.cases || [];
    if (!rows.length) return empty('还没有平仓', '第一笔平仓之后，这里才有可结算的判断。');
    return h('div', { class: 'stack', style: { gap: '10px' } }, rows.map((c) => h('div', {
      class: 'stack', style: { gap: '3px', borderLeft: `2px solid ${c.pnl >= 0 ? 'var(--px-up)' : 'var(--px-down)'}`, paddingLeft: '9px' },
    },
      h('div', { class: 'row small' },
        h('span', { class: 'sym' }, c.symbol),
        h('span', { class: `num ${tone(c.pnl_pct)}` }, pct(c.pnl_pct)),
        h('span', { class: 'num muted' }, money(c.pnl)),
        h('div', { class: 'spacer' }),
        h('span', { class: 'micro faint num' }, `${ymd(c.opened_at)} → ${ymd(c.closed_at)} · ${c.qty} 股`)),
      h('div', { class: 'micro', style: { color: 'var(--ink-dim)', lineHeight: '1.6' } }, c.thesis || '—'),
      c.invalidation
        ? h('div', { class: 'micro faint', style: { lineHeight: '1.6' } }, `失效：${c.invalidation}`)
        : null,
      h('button', {
        class: 'btn btn-sm btn-ghost', style: { justifySelf: 'start', marginTop: '2px' },
        onclick: () => ctx.navigate(`s/${c.symbol}`),
      }, '看这只票现在的样子'))));
  });
}

function memoBlock() {
  return api('/memo', { ttl: 60000 }).then((d) => {
    const rows = d.memos || [];
    if (!rows.length) return empty('没有备忘录');
    return h('div', { class: 'stack', style: { gap: '9px' } }, rows.slice(0, 6).map((m) => h('div', {
      class: 'stack', style: { gap: '2px' },
    },
      h('div', { class: 'row small' },
        chip(m.status === 'scored' ? '已结算' : '未结算', m.status === 'scored' ? 'ok' : 'off'),
        h('span', { class: 'strong' }, m.topic || ''),
        h('div', { class: 'spacer' }),
        h('span', { class: 'micro faint num' }, ymd(m.created_at))),
      h('div', { class: 'micro', style: { color: 'var(--ink-dim)', lineHeight: '1.6' } }, m.thesis || ''),
      m.falsifier ? h('div', { class: 'micro faint' }, `证伪：${m.falsifier}`) : null)));
  });
}

function declinedBlock() {
  return api('/declined?limit=5', { ttl: 60000 }).then((d) => {
    const rows = d.rows || [];
    if (!rows.length) return empty('没有拒绝记录');
    return h('div', { class: 'stack', style: { gap: '10px' } }, rows.map((r) => h('div', {
      style: { borderLeft: '2px solid var(--line-strong)', paddingLeft: '9px' },
    },
      h('div', { class: 'row small' },
        h('span', { class: 'num faint' }, hhmmss(r.ts)),
        h('span', { class: 'sym' }, (r.symbols || []).join(' ')),
        h('div', { class: 'spacer' }),
        h('span', { class: 'micro faint' }, ymd(r.day))),
      h('details', {},
        h('summary', { class: 'small', style: { cursor: 'pointer', color: 'var(--ink-mid)' } }, '为什么不买'),
        h('pre', { class: 'small', style: { whiteSpace: 'pre-wrap', color: 'var(--ink-dim)', fontFamily: 'var(--font-ui)', lineHeight: '1.75', margin: '4px 0 0' } }, r.reason || ''),
        r.invalidation ? h('div', { class: 'micro faint', style: { marginTop: '4px' } }, `失效条件：${r.invalidation}`) : null))));
  });
}

/**
 * 后端已把英文句型拆成 note_meta（reason / need / have）。
 * 不再用正则从 "need at least N closed trades, have M" 里抓可变参数。
 */
function decayNote(r) {
  const meta = r?.note_meta;
  if (!meta || meta.reason === 'none') return '—';
  if (meta.reason === 'sample_below_min') {
    return `至少要 ${meta.need} 笔已平仓，现在 ${meta.have} 笔`;
  }
  return zh.decayReason(meta.reason) || '—';
}

/**
 * 核对不再只报"有几个数对不上"：verified.unmatched_detail 带 value / excerpt /
 * start-end，把"待修"落成能定位到原句的坐标清单。缺 detail 时明确报版本问题，
 * 不静默退回裸数字 —— 静默退回会让不完整的迁移一直不被发现。
 */
function unmatchedList(v) {
  const items = v?.unmatched_detail || [];
  if (!items.length) {
    return v?.unmatched?.length
      ? h('div', { class: 'micro', style: { color: 'var(--al-stale)' } },
        `待修 ${v.unmatched_count} 个：后端未给坐标（请重启服务）`)
      : null;
  }
  return h('div', { class: 'stack', style: { gap: '2px' } },
    h('div', { class: 'micro', style: { color: 'var(--al-stale)' } },
      `待修 ${v.unmatched_count ?? items.length} 个 · 显示前 ${items.length} 个`),
    items.map((it) => h('div', {
      class: 'micro faint',
      style: { paddingLeft: '8px', whiteSpace: 'pre-wrap' },
      title: it.start === undefined
        ? '' : `字符 ${it.start}–${it.end} · ${it.field} · 出现 ${it.occurrences} 次`,
    }, `${it.value} → ${it.excerpt || '（无上下文）'}`)));
}


// ------------------------------------------------------------------ 复盘的三个写动作
// 复盘不是只读页：写日志、结算备忘录、结算"不做"，是复盘页存在的理由。
// 结算类动作统一两步：dry_run 预检（不写库）→ 人确认 → 真执行。

/** 结算动作：预检展示判定，确认才落库。没有到期的记录不是失败，是还没到能判的时候。 */
async function scoreAction({ path, label, ctx }) {
  let pre;
  try {
    pre = await post(`${path}?dry_run=true`, null, { timeout: 60000 });
  } catch (e) {
    toast(`${label}预检失败：${e.message || e.code}`, 'bad');
    return;
  }
  const scored = pre.scored || [];
  if (!scored.length) {
    toast(pre.note || `没有到期的${label}（未到期 ${pre.pending ?? 0} 条）——这不算失败`);
    return;
  }
  // 预检明细拼进确认文案：confirmAction 只收文本，明细一行一条，最多 6 条。
  const detail = scored.slice(0, 6).map((x) => {
    const what = x.topic || (x.symbols || []).join(' ') || x.id || '';
    const verdict = x.verdict || (x.outcome === 1 ? '验证' : x.outcome === 0 ? '证伪' : '');
    const tail = x.brier !== undefined && x.brier !== null ? `（brier ${Number(x.brier).toFixed(3)}）` : '';
    return `· ${String(what).slice(0, 24)} → ${verdict}${tail}`;
  }).join('\n');
  const ok = await confirmAction({
    title: `结算${label}（${scored.length} 条到期）`,
    message: `预检判定（预检不写库，确认后才落库）：\n${detail}`,
    confirmText: '结算',
  });
  if (!ok) return;
  try {
    const res = await post(path, null, { timeout: 60000 });
    toast(`${label}已结算 ${res.scored?.length ?? scored.length} 条`, 'ok');
    ctx.refresh?.();
  } catch (e) {
    toast(`结算失败：${e.message || e.code}`, 'bad');
  }
}

/** 写复盘日志。后端会当场核对正文里的数字——结果必须显示出来，否则核对白做。 */
function journalComposer(ctx) {
  const text = h('textarea', {
    class: 'input', rows: '7', spellcheck: 'false', placeholder:
      '今天验证了什么、被什么数据推翻了。\n正文里的数字会被当场和那天的事实核对：核对不过的条目标"待修"，不会拒收。',
    style: { width: '100%', resize: 'vertical' },
  });
  const kind = h('select', { class: 'select' },
    h('option', { value: 'review' }, '复盘'),
    h('option', { value: 'plan' }, '计划'),
    h('option', { value: 'note' }, '笔记'));
  const symbol = h('input', { class: 'input input-num', placeholder: '可选，如 600519.SH', style: { width: '150px' } });
  const out = h('div', {});
  const submit = h('button', {
    class: 'btn btn-primary', style: { marginTop: '8px' },
    onclick: async () => {
      if (!text.value.trim()) { toast('先写正文', 'warn'); return; }
      submit.disabled = true;
      try {
        const res = await post('/journal', {
          text: text.value, kind: kind.value,
          symbol: symbol.value.trim() || null, tags: '',
        }, { timeout: 60000 });
        const v = res.verification;
        out.replaceChildren(h('div', { class: 'stack', style: { gap: '3px', marginTop: '8px' } },
          v ? chip(`核 ${v.checked} · ${v.unmatched_count ? `存疑 ${v.unmatched_count}` : '全对'}`, v.unmatched_count ? 'warn' : 'ok')
            : chip('已写入（未核对）', 'off'),
          unmatchedList(v)));
        if (!v?.unmatched_count) {
          toast('日志已写入', 'ok');
          text.value = '';
          ctx.refresh?.();
        }
      } catch (e) {
        out.replaceChildren(errorBox(e));
      } finally {
        submit.disabled = false;
      }
    },
  }, '写入（当场核对数字）');

  drawer({
    title: '写复盘日志',
    label: '写日志',
    body: h('div', { class: 'stack', style: { gap: '8px' } },
      h('div', { class: 'row', style: { gap: '8px' } },
        h('div', { class: 'field' }, h('label', {}, '类型'), kind),
        h('div', { class: 'field' }, h('label', {}, '相关代码'), symbol)),
      text, submit, out),
  });
}
