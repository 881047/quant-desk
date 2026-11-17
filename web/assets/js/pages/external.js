// 实盘（账外）：真钱的账本。与模拟盘完全分离，读的是 data/external.sqlite。
//
// 这一页只在桌面窗口里有数据 —— 它走的是 pywebview 的 Bridge（Python 直接调用
// service/trading/manual.py），不是 HTTP。浏览器打开时会明确说明"只在桌面窗口
// 可用"，不假装成加载失败。
//
// 两条写路径（记一笔 / 同步快照）都先预检再落库：预检失败面板上会列出每一条问题，
// 因为一次写错会把止损锚到错的价格上。

import { h, panel, kpis, chip, table, empty, errorBox, slot, kv, toast, confirmAction } from '../ui.js';
import { money, ratio, num, toneStyle, fp2, ymd, hhmmss, todayYmd } from '../fmt.js';
import { zh } from '../zh.js';

const bridge = () => window.pywebview?.api || null;

async function ready(timeout = 4000) {
  if (bridge()) return true;
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    await new Promise((r) => setTimeout(r, 120));
    if (bridge()) return true;
  }
  return Boolean(bridge());
}

export async function mount(ctx) {
  if (!(await ready())) return notDesktop();

  const root = h('div', { class: 'stack' });
  const head = h('div', {});
  const grid = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  const col1 = h('div', { class: 'stack' });
  const col2 = h('div', { class: 'stack' });
  grid.append(col1, col2);
  root.append(head, grid);

  col1.append(
    panel({ title: '持仓', note: '来自快照；价格到了会自动补', body: h('div', { id: 'ext-positions' }), flush: true }),
    panel({ title: '今日动作', note: '记一笔 / 同步都会留痕', body: h('div', { id: 'ext-events' }) }),
  );
  col2.append(
    panel({ title: '记一笔', note: '佣金、印花税、过户费自动算', body: h('div', { id: 'ext-trade' }) }),
    panel({ title: '同步快照', note: '整表覆盖；先预检再写入', body: h('div', { id: 'ext-sync' }) }),
    panel({
      title: '粘贴导入',
      note: '成交流水或持仓表，先解析再决定写不写',
      body: h('div', { id: 'ext-paste' }),
      footer: '解析不写库。认出是成交就当成交流水，认出是持仓就填进上面的同步表单 —— 写不写由你点。',
    }),
  );

  loadAccount(col1, head);
  slot(col1.querySelector('#ext-events'), () => eventsBlock(), { lines: 4 });
  slot(col2.querySelector('#ext-trade'), () => tradeBlock(col1, head), { lines: 6 });
  slot(col2.querySelector('#ext-sync'), () => syncBlock(col1, head), { lines: 6 });
  slot(col2.querySelector('#ext-paste'), () => pasteBlock(col1, head), { lines: 6 });

  return root;
}

export function unmount() {}

function notDesktop() {
  return panel({
    title: '实盘（账外）',
    body: h('div', { class: 'empty' },
      h('b', {}, '这一页只在桌面窗口里有数据'),
      h('div', {}, '它读的是本机的 ', h('code', {}, 'data/external.sqlite'),
        '，走的是桌面窗口里的 Python 桥，不是 HTTP 接口。'),
      h('div', { class: 'mt-2' }, '双击桌面上的 ', h('code', {}, 'quant-desk'), ' 打开窗口即可录入。'),
      h('div', { class: 'micro faint mt-2' }, '浏览器里其余页面照常可用，只有这一页需要窗口。')),
  });
}

// ------------------------------------------------------------------ 账户
async function loadAccount(col1, head) {
  const api = bridge();
  try {
    // 先读快照（毫秒级），再补实时价 —— 与旧面板同一个策略：
    // 等厂商报价先回来的话，开盘第一屏会是八秒灰块。
    const quick = await api.account(false);
    paint(head, col1, quick);
    // 报价这一步单独兜：快照已经画出来了，厂商挂了不该把它整屏换成错误框
    // ——先出快照再补价，本来就是这一页存在的理由。
    let full = null;
    try {
      full = await api.account(true);
    } catch (quoteErr) {
      toast(`实时价没取到，先按快照显示：${quoteErr?.message || quoteErr}`);
    }
    if (full) paint(head, col1, full);
  } catch (err) {
    head.replaceChildren(errorBox({ code: zh.code('BRIDGE'), message: String(err?.message || err), next: '窗口的 Python 侧出错了；看 data/panel.log。' }));
  }
}

function paint(head, col1, data) {
  const p = data?.panel || {};
  head.replaceChildren(kpis([
    { label: '总资产', value: p.ok ? money(p.total) : '—', sub: p.ok ? (p.source === 'manual' ? '手动录入' : '读自快照') : '还没填过' },
    { label: '可用资金', value: p.ok ? money(p.cash) : '—', sub: '不含冻结' },
    { label: '持仓市值', value: p.ok ? money(p.market_value) : '—', sub: `${(data?.positions || []).length} 只` },
    { label: '现金占比', value: p.ok ? ratio(p.cash_ratio, 1) : '—', sub: p.ok ? (Number(p.cash_ratio) > 0.8 ? '仓位很轻' : '仓位较重') : '—' },
  ]));

  const host = col1.querySelector('#ext-positions');
  if (!host) return;
  const rows = data?.positions || [];
  const drift = data?.drift || {};
  if (!p.ok) {
    host.replaceChildren(h('div', { class: 'empty' },
      h('b', {}, '账户数字还没填'),
      h('div', {}, p.reason ? `（${p.reason}）先填一次总资产与可用资金，之后每笔成交会自动扣加。` : '先填一次总资产与可用资金。')));
    return;
  }
  host.replaceChildren(
    rows.length
      ? table({
        cols: [
          { key: 'symbol', label: '代码', render: (r) => `<span class="sym">${r.symbol}</span>` },
          { key: 'name', label: '名称' },
          { key: 'qty', label: '数量', cls: 'r', render: (r) => num(r.qty) },
          { key: 'cost', label: '成本', cls: 'r', render: (r) => fp2(r.cost) },
          { key: 'last', label: '现价', cls: 'r', render: (r) => (r.last ? fp2(r.last) : '—') },
          { key: 'mv', label: '市值', cls: 'r', render: (r) => money(r.market_value, { unit: false }) },
          { key: 'pnl', label: '盈亏', cls: 'r', render: (r) => `<span style="${toneStyle(r.pnl_total)}">${money(r.pnl_total)}</span>` },
          { key: 'stop', label: '止损', cls: 'r', render: (r) => (r.stop ? fp2(r.stop) : '—') },
          { key: 'state', label: '状态', cls: 'r', render: (r) => (r.breach_stop ? chip('破止损', 'bad') : r.breach_auction ? chip('破竞价线', 'warn') : chip('持有', 'off')) },
        ],
        rows,
      })
      : empty('空仓', '账本里没有持仓。'),
    h('div', { class: 'panel-ft' },
      drift.verdict === 'match' ? chip('与账户一致', 'ok') : drift.verdict ? chip('与账户不一致', 'bad') : chip('未比对', 'off'),
      h('span', { class: 'micro faint', style: { marginLeft: '8px' } }, drift.message || drift.reason || (data.snapshot_day ? `快照 ${ymd(data.snapshot_day)}` : '还没有快照'))),
  );
}

// ------------------------------------------------------------------ 记一笔
function tradeBlock(col1, head) {
  const date = h('input', { class: 'input input-num', style: { width: '110px' }, value: todayYmd() });
  const symbol = h('input', { class: 'input input-num', placeholder: '600519.SH', style: { width: '128px' } });
  const side = h('select', { class: 'select', style: { width: '92px' } },
    h('option', { value: 'BUY' }, '买入'), h('option', { value: 'SELL' }, '卖出'));
  const qty = h('input', { class: 'input input-num', placeholder: '100', style: { width: '84px' } });
  const price = h('input', { class: 'input input-num', placeholder: '9.20', style: { width: '84px' } });
  const fee = h('div', { class: 'micro faint' }, '填价格与数量后自动算费用');
  const hint = h('div', { class: 'micro' }, '');

  const refreshFee = async () => {
    const api = bridge();
    if (!api || !price.value || !qty.value) { fee.textContent = '填价格与数量后自动算费用'; return; }
    try {
      const f = await api.quote_fee(side.value, Number(price.value), Number(qty.value));
      if (!f || f.total === undefined) { fee.textContent = '费用算不出来（口径不支持）'; return; }
      fee.textContent = `佣金 ${money(f.commission)} · 印花税 ${money(f.stamp)} · 过户费 ${money(f.transfer)} · 合计 ${money(f.total)}`;
    } catch { fee.textContent = '费用算不出来'; }
  };
  price.addEventListener('change', refreshFee);
  qty.addEventListener('change', refreshFee);
  side.addEventListener('change', refreshFee);

  const submit = async () => {
    const row = {
      trade_date: date.value.trim(), symbol: symbol.value.trim(), side: side.value,
      qty: qty.value.trim(), price: price.value.trim(), fee: '',
    };
    if (!row.symbol || !row.qty || !row.price) { hint.textContent = '代码、数量、价格都要填'; hint.style.color = 'var(--al-missing-ink)'; return; }
    const ok = await confirmAction({
      title: '写入这笔成交',
      message: kv([
        ['标的', `${row.symbol} ${row.side}`],
        ['数量 / 价格', `${row.qty} 股 @ ${row.price}`],
        ['日期', ymd(row.trade_date)],
      ]) + '<div class="micro faint" style="margin-top:8px">写入后会更新持仓与可用资金，并出现在今日动作里。</div>',
      confirmText: '写入',
    });
    if (!ok) return;
    try {
      const res = await bridge().add_trade(row);
      hint.textContent = res?.message || '';
      hint.style.color = res?.ok ? 'var(--px-down)' : 'var(--px-up)';
      toast(res?.message || (res?.ok ? '已写入' : '没有写入'), res?.ok ? 'ok' : 'bad');
      if (res?.ok) {
        symbol.value = ''; qty.value = ''; price.value = ''; fee.textContent = '填价格与数量后自动算费用';
        loadAccount(col1, head);
        const ev = col1.querySelector('#ext-events');
        if (ev) slot(ev, () => eventsBlock(), { lines: 4 });
      }
    } catch (err) {
      hint.textContent = String(err?.message || err);
      hint.style.color = 'var(--px-up)';
    }
  };

  return h('div', { class: 'stack', style: { gap: '9px' } },
    h('div', { class: 'row', style: { flexWrap: 'wrap' } },
      h('div', { class: 'field' }, h('label', {}, '成交日'), date),
      h('div', { class: 'field' }, h('label', {}, '代码'), symbol),
      h('div', { class: 'field' }, h('label', {}, '方向'), side)),
    h('div', { class: 'row', style: { flexWrap: 'wrap' } },
      h('div', { class: 'field' }, h('label', {}, '数量（股）'), qty),
      h('div', { class: 'field' }, h('label', {}, '成交价'), price)),
    fee,
    h('div', { class: 'row' }, h('button', { class: 'btn btn-primary', onclick: submit }, '写入成交')),
    hint,
    h('div', { class: 'micro faint' }, '费用由内核按当前费率算，不问你——记错费率会让成本（以及止损）偏。'));
}

// ------------------------------------------------------------------ 同步快照
function syncBlock(col1, head) {
  const day = h('input', { class: 'input input-num', style: { width: '110px' }, value: todayYmd() });
  const total = h('input', { class: 'input input-num', style: { width: '110px' } });
  const cash = h('input', { class: 'input input-num', style: { width: '110px' } });
  const mv = h('input', { class: 'input input-num', style: { width: '110px' } });
  const rowsBox = h('div', { class: 'stack', style: { gap: '4px' } });
  const planBox = h('div', {});

  const addRow = (row = {}) => {
    const tr = h('div', { class: 'row-tight', style: { gap: '4px' } },
      h('input', { class: 'input input-num', placeholder: '代码', value: row.symbol || '', style: { width: '110px' } }),
      h('input', { class: 'input input-num', placeholder: '数量', value: row.qty || '', style: { width: '72px' } }),
      h('input', { class: 'input input-num', placeholder: '成本', value: row.cost || '', style: { width: '72px' } }),
      h('input', { class: 'input input-num', placeholder: '现价', value: row.last || '', style: { width: '72px' } }),
      h('button', { class: 'btn btn-sm btn-ghost', onclick: () => tr.remove() }, '删'));
    tr.dataset.row = '1';
    rowsBox.appendChild(tr);
  };

  const read = () => ({
    as_of: day.value.trim(),
    total: total.value.trim(),
    cash: cash.value.trim(),
    market_value: mv.value.trim(),
    positions: [...rowsBox.querySelectorAll('[data-row]')].map((tr) => {
      const v = [...tr.querySelectorAll('input')].map((i) => i.value.trim());
      return { symbol: v[0], qty: v[1], cost: v[2], last: v[3] };
    }).filter((r) => r.symbol),
  });

  const preview = async () => {
    planBox.replaceChildren(h('div', { class: 'micro faint' }, '预检中…'));
    try {
      const plan = await bridge().preview(read());
      if (!plan) { planBox.replaceChildren(empty('没有结果')); return; }
      planBox.replaceChildren(h('div', { class: 'stack', style: { gap: '4px' } },
        plan.ok ? chip('预检通过', 'ok') : chip('预检不通过', 'bad'),
        ...(plan.problems || []).map((m) => h('div', { class: 'micro', style: { color: 'var(--px-up)' } }, m)),
        ...(plan.warnings || []).map((m) => h('div', { class: 'micro', style: { color: 'var(--al-stale)' } }, m)),
        plan.ok ? h('div', { class: 'micro faint' }, '确认无误后点「保存快照」写入。') : null));
    } catch (err) {
      planBox.replaceChildren(errorBox({ code: zh.code('PREVIEW'), message: String(err?.message || err) }));
    }
  };

  const save = async () => {
    try {
      const res = await bridge().commit(read());
      if (res?.ok) {
        toast(res.message || '已保存', 'ok');
        loadAccount(col1, head);
        const ev = col1.querySelector('#ext-events');
        if (ev) slot(ev, () => eventsBlock(), { lines: 4 });
      } else {
        planBox.replaceChildren(h('div', { class: 'err' },
          h('div', { class: 'code' }, zh.code('COMMIT_REFUSED')),
          h('div', {}, res?.message || '没有写入'),
          ...(res?.problems || []).map((m) => h('div', { class: 'next' }, m))));
      }
    } catch (err) {
      planBox.replaceChildren(errorBox({ code: zh.code('COMMIT'), message: String(err?.message || err) }));
    }
  };

  // 用当前账户预填一次，避免从空白开始手打
  (async () => {
    try {
      const data = await bridge().account(false);
      const p = data?.panel || {};
      if (p.ok) { total.value = p.total ?? ''; cash.value = p.cash ?? ''; mv.value = p.market_value ?? ''; }
      for (const row of data?.positions || []) addRow(row);
      if (!(data?.positions || []).length) addRow();
    } catch { addRow(); }
  })();

  return h('div', { class: 'stack', style: { gap: '9px' } },
    h('div', { class: 'row', style: { flexWrap: 'wrap' } },
      h('div', { class: 'field' }, h('label', {}, '快照日'), day),
      h('div', { class: 'field' }, h('label', {}, '总资产'), total),
      h('div', { class: 'field' }, h('label', {}, '可用资金'), cash),
      h('div', { class: 'field' }, h('label', {}, '持仓市值'), mv)),
    h('div', { class: 'stack', style: { gap: '4px' } },
      h('div', { class: 'micro faint' }, '持仓（代码 / 数量 / 成本 / 现价）'),
      rowsBox,
      h('button', { class: 'btn btn-sm', onclick: () => addRow(), style: { justifySelf: 'start' } }, '加一行')),
    h('div', { class: 'row-tight' },
      h('button', { class: 'btn btn-sm', onclick: preview }, '预检'),
      h('button', { class: 'btn btn-sm btn-primary', onclick: save }, '保存快照')),
    planBox,
    h('div', { class: 'micro faint', style: { lineHeight: '1.6' } },
      '整表覆盖而不是增量：快照是"这一刻账户长什么样"，与逐笔流水是两件事。'));
}

// ------------------------------------------------------------------ 今日动作
function eventsBlock() {
  return bridge().events().then((rows) => {
    const list = (rows || []).slice(-14).reverse();
    if (!list.length) return empty('今天还没有动作', '记一笔或同步之后，这里会出现。');
    return h('div', { class: 'timeline' }, list.map((e) => h('div', { class: `tl-item ${e.kind || ''}` },
      h('div', { class: 'tl-time' }, hhmmss(e.ts)),
      h('div', { class: 'tl-dot' }),
      h('div', { class: 'tl-body' },
        h('div', { class: 'tl-kind' }, e.label || e.kind || ''),
        e.detail ? h('div', { class: 'tl-detail' }, e.detail) : null))));
  }).catch((err) => errorBox({ code: zh.code('EVENTS'), message: String(err?.message || err) }));
}

// ------------------------------------------------------------------ 粘贴导入
/**
 * 从券商 App 复制出来的文本：可能是成交流水，也可能是持仓表。
 *
 * 两步：`parse_text` 只解析（不写库、不动钱），`import_text` 才落库。
 * 中间那一步是给人看的 —— 认出几行、认出的是什么，都摆出来再由他决定写不写。
 * 旧面板（panel.pyw legacy）一直是这个流程，换界面不该把它简化成一步。
 */
function pasteBlock(col1, head) {
  const box = h('textarea', {
    class: 'input', rows: '6', spellcheck: 'false',
    placeholder: '把券商的成交记录或持仓表整段粘进来（制表符/多空格分隔都行）',
    style: { width: '100%', resize: 'vertical', fontFamily: 'var(--font-num)', fontSize: 'var(--fs-label)' },
    'aria-label': '粘贴成交流水或持仓表',
  });
  const hint = h('div', { class: 'micro faint' });
  const out = h('div', {});
  const writeBtn = h('button', { class: 'btn btn-sm btn-primary', hidden: 'hidden' }, '写入');

  const doParse = async () => {
    const text = box.value;
    if (!text.trim()) { toast('先粘贴内容', 'warn'); return; }
    writeBtn.hidden = true;
    out.replaceChildren();
    hint.textContent = '解析中…';
    hint.className = 'micro faint';
    let r;
    try {
      r = await bridge().parse_text(text);
    } catch (e) {
      hint.textContent = `解析失败：${e?.message || e}`;
      hint.className = 'micro';
      hint.style.color = 'var(--al-fault)';
      return;
    }
    if (!r?.ok) {
      // 解析失败的原话照抄：它通常会说清"哪一行第几列不认识"，改写就丢了定位。
      hint.textContent = r?.message || '解析失败';
      hint.className = 'micro';
      hint.style.color = 'var(--al-fault)';
      return;
    }
    const rows = r.rows || [];
    hint.textContent = r.kind === 'positions'
      ? `认出 ${rows.length} 只持仓（表头来自「${r.profile || '未知格式'}」）`
      : `认出 ${rows.length} 笔成交（${r.profile || '未知格式'}）`;
    hint.className = 'micro';
    hint.style.color = 'var(--ink-2)';

    // 先看清楚再加确认：把解析结果逐行摆出来，字段名也用真实值不猜。
    out.appendChild(h('table', { class: 'tbl', style: { marginTop: '4px' } },
      h('thead', {}, h('tr', {}, Object.keys(rows[0] || {}).slice(0, 6).map((k) => h('th', {}, k)))),
      h('tbody', {}, rows.slice(0, 40).map((row) => h('tr', {},
        Object.keys(rows[0] || {}).slice(0, 6).map((k) => h('td', { class: 'num' }, row[k] ?? '—')))))));

    if (r.kind === 'positions') {
      out.appendChild(h('div', { class: 'micro faint', style: { marginTop: '6px' } },
        '这是持仓表，不是成交流水。请用上面的「同步快照」写入 —— 那条路径会先预检每一条。'));
      return;
    }
    writeBtn.textContent = `写入这 ${rows.length} 笔成交`;
    writeBtn.hidden = false;
    writeBtn.onclick = async () => {
      const ok = await confirmAction({
        title: '写入成交',
        message: `这 ${rows.length} 笔会写进实盘账本，并改变持仓成本与止损锚点。解析不写库，写入才写。`,
        confirmText: '写入',
      });
      if (!ok) return;
      writeBtn.disabled = true;
      let res;
      try {
        res = await bridge().import_text(text);
      } catch (e) {
        hint.textContent = `写入失败：${e?.message || e}`;
        hint.style.color = 'var(--al-fault)';
        writeBtn.disabled = false;
        return;
      }
      hint.textContent = res?.message || (res?.ok ? '已写入' : '没有写入');
      hint.style.color = res?.ok ? 'var(--ink-2)' : 'var(--al-fault)';
      writeBtn.disabled = false;
      if (res?.ok) {
        writeBtn.hidden = true;
        box.value = '';
        await loadAccount(col1, head);
      }
    };
  };

  return h('div', { class: 'stack', style: { gap: '6px' } },
    box,
    h('div', { class: 'row-tight' },
      h('button', { class: 'btn btn-sm', onclick: doParse }, '解析'),
      writeBtn),
    hint,
    out);
}
