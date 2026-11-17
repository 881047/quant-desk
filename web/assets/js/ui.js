// 组件原语。没有框架：一个 h() 加几个函数，够用且没有构建步骤。
// 所有用户可见文本都走 textContent 或 esc()，不接受未转义的拼接。

import { esc, num } from './fmt.js';
import { zh } from './zh.js';

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(4)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** 从 HTML 字符串建临时 DOM —— 供 icon() 等静态片段使用。
    只喂写死的标记，动态值一律走 h()（innerHTML 是 XSS 的门，不对外部数据开放）。 */
function html(markup) {
  const t = document.createElement('template');
  t.innerHTML = String(markup).trim();
  return t.content.firstElementChild;
}

export function icon(name, size = 12) {
  const paths = {
    refresh: 'M12 4a6 6 0 1 0 5.2 3',
    plus: 'M8 3v10M3 8h10',
    close: 'M4 4l8 8M12 4l-8 8',
    chevron: 'M5 3l5 5-5 5',
    down: 'M4 6l4 4 4-4',
    up: 'M4 10l4-4 4 4',
    search: 'M7 2a5 5 0 1 0 0 10A5 5 0 0 0 7 2zM11 11l3 3',
    alert: 'M8 2l6 12H2L8 2zM8 6v4M8 12v.5',
    check: 'M3 8l3.5 3.5L13 5',
  };
  const p = paths[name];
  if (!p) return null;
  return html(`<svg viewBox="0 0 16 16" width="${size}" height="${size}" fill="none"
    stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="${p}"/></svg>`);
}

export function chip(text, kind = '') {
  return h('span', { class: `chip ${kind}`.trim() }, text);
}

// ------------------------------------------------------- 三态数字：值 / 0 / —
// `0` 是干净数字（无框无底）；`—` 是带框空位，视觉重量必须 ≥ 0。
// 把 — 做成淡灰会让"管道没跑"读成"今天没涨停"，这是本产品最致命的错觉。
export function miss(reason, { fix = null, endpoint = null } = {}) {
  const tip = [`未取到${reason ? `：${reason}` : ''}`, endpoint ? `${endpoint} 未调用` : null,
    '不参与任何判断', fix ? `补齐：${fix}` : null].filter(Boolean).join(' · ');
  return h('span', {
    class: 'cell-missing',
    'data-tip': tip,
    title: tip,
    'aria-label': `未取到${reason ? `，${reason}` : ''}`,
  }, '—');
}

/**
 * 判定：两个填充 + 一个空位，零色相。
 *   过   = 1px 空心方 8×8  + 400
 *   不过 = 实心方 8×8     + 600
 *   无结论/未运行 = 不画方，只占住槽位 + 一句原因
 * 填充量随严重度递增（25 格里 23 格是"过"，所以正常态最轻）。
 * `text` 必须 ≤4 字：长句塞进可见文本会在 420px 列里换行，整列基线错开。
 * 调用：judge(ok, { text:'不过', aria:'不通过，35.96 高于闸门 35' })
 */
export function judge(ok, spec) {
  const o = typeof spec === 'string' || spec == null ? { text: spec ?? undefined } : spec;
  const known = ok === true || ok === false;
  const text = o.text || (ok === true ? '过' : ok === false ? '不过' : '未运行');
  const aria = o.aria || text;
  const el = h('span', {
    class: `judge ${known ? (ok ? 'judge-yes' : 'judge-no') : 'judge-empty'}`,
    role: 'img',
    'aria-label': aria,
    title: aria,
  });
  el.appendChild(h('span', { class: 'judge-mark', 'aria-hidden': 'true' }));
  el.appendChild(h('span', { class: 'judge-label' }, text));
  if (!known && o.reason) el.dataset.reason = o.reason;
  return el;
}

export function panel({ title, note, actions = null, body = null, footer = null, cls = '', flush = false, scroll = false, src = null }) {
  const el = h('section', { class: `panel ${cls}`.trim() });
  if (title || note || actions) {
    const hd = h('div', { class: 'panel-hd' });
    if (title) hd.appendChild(h('h2', {}, title));
    if (note) hd.appendChild(h('span', { class: 'hd-note', title: typeof note === 'string' ? note : '' }, note));
    if (src) {
      const bits = [src.endpoint, src.asOf ? `as_of ${src.asOf}` : null].filter(Boolean).join(' · ');
      hd.appendChild(h('span', { class: 'hd-src', title: bits }, bits));
    }
    if (actions) {
      const box = h('div', { class: 'hd-actions' });
      append(box, [actions]);
      hd.appendChild(box);
    }
    el.appendChild(hd);
  }
  const bd = h('div', { class: `panel-bd ${flush ? 'flush' : ''} ${scroll ? 'scroll' : ''}`.trim() });
  if (body) append(bd, [body]);
  el.appendChild(bd);
  if (footer) el.appendChild(h('div', { class: 'panel-ft' }, footer));
  return el;
}

export function kpi({ label, value, sub, hero = false, cls = '', title = null }) {
  return h('div', { class: 'kpi', title },
    h('div', { class: 'kpi-l' }, label),
    h('div', { class: `kpi-v ${hero ? 'kpi-hero' : ''} ${cls}`.trim() }, value),
    sub ? h('div', { class: 'kpi-s' }, sub) : null);
}

export function kpis(items) {
  return h('div', { class: 'kpis' }, items.map((it) => kpi(it)));
}

export function empty(title, hint = null) {
  const el = h('div', { class: 'empty' });
  el.appendChild(h('b', {}, title));
  if (hint) el.appendChild(h('div', { html: hint }));
  return el;
}

export function errorBox(err) {
  if (err?.handled) return empty('口令失效', '已切到口令闸门，这一块先停在这里。');
  if (err?.status === 409) return conflictBox(err);
  const code = err?.code || `HTTP ${err?.status || '?'}`;
  return h('div', { class: 'err' },
    h('div', { class: 'code' }, code),
    h('div', {}, err?.message || '请求失败'),
    err?.next ? h('div', { class: 'next' }, err.next) : null);
}

/** 409 不是网络错误：它是"这条提案被规则挡住了"。
    必须给 guard.checks 全表，红行 = 失败项，detail 原样显示。 */
export function conflictBox(err) {
  const body = err?.body || {};
  const checks = body?.guard?.checks || body?.checks || [];
  return h('div', { class: 'err err-conflict' },
    h('div', { class: 'code' }, err?.code || 'CONFLICT'),
    h('div', { class: 'conflict-fixed' }, '改 body 不会解决这一条，请先处理它。'),
    h('div', {}, err?.message || ''),
    checks.length ? h('table', { class: 'tbl tbl-checks' },
      h('thead', {}, h('tr', {}, h('th', {}, '检查项'), h('th', {}, '结果'), h('th', {}, '说明'))),
      h('tbody', {}, checks.map((c) => h('tr', { class: c.ok ? 'is-pass' : 'is-fail' },
        h('td', { class: 'gc-name', title: c.name || '' }, judge(c.ok, zh.check(c.name || ''))),
        h('td', { class: 'num' }, c.ok ? '过' : '不过'),
        // 后端给了 detail_zh 与 structured；英文原句留在 title，改写不丢定位。
        h('td', { class: 'gc-detail', title: c.detail || '' }, c.detail_zh || c.detail || '—'))))) : null);
}

export function skeleton(lines = 3) {
  const box = h('div', { class: 'stack', style: { gap: '7px', padding: '4px 0' } });
  for (let i = 0; i < lines; i += 1) {
    box.appendChild(h('div', { class: 'skeleton', style: { width: `${100 - (i % 3) * 14}%` } }));
  }
  return box;
}

export function meterRow({ label, value, max, text, tone = '', proof = null }) {
  const p = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const el = h('div', { class: 'meter-row' },
    h('div', { class: 'meter-label', title: label }, label),
    h('div', { class: 'meter-track' }, h('div', { class: `meter-fill ${tone}`.trim(), style: { width: `${p * 100}%` } })),
    h('div', { class: 'meter-val' }, text));
  if (proof) el.title = proof;
  return el;
}

/**
 * 带原式的比率读数：悬停显示「分子 … / 分母 …」。
 *
 * 这不是装饰。`Number(equity) || 1` 这一类兜底会让分母静默变成 1，
 * 算出来的 `100%` 完全合法、不触发任何告警、看起来和真值一模一样。
 * 唯一能让它露出来的办法，就是把分子分母原文摆在手边。
 */
export function ratioWith(value, { num, den, digits = 1, unit = '元' } = {}) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return h('span', { class: 'num-wrap' }, miss('分母不可算', { fix: '先修上一步的净值' }));
  }
  const text = `${(Number(value) * 100).toFixed(digits)}%`;
  const fmtNum = (v) => (v === null || v === undefined || !Number.isFinite(Number(v))
    ? '—' : `${Number(v).toFixed(2)}${unit}`);
  const known = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
  const tip = known(num) && known(den) ? `分子 ${fmtNum(num)} / 分母 ${fmtNum(den)}` : null;
  const el = h('span', { class: 'num', title: tip || '', 'data-proof': tip ? '1' : '0' }, text);
  if (tip) el.setAttribute('aria-label', `${text}（${tip}）`);
  return el;
}

/**
 * 表格。三态约定：
 *   - 单元格值 null / undefined / '' → 渲染 miss()，原因从 `c.miss(row)` 取
 *   - 绝不写 `value ?? 0`：后端给 null 时显示 0 就是骗人
 *
 * 可访问性：
 *   - `rowAction` 缺省由 onRow 推断：有 onRow → 'open'，没有 → 'none'
 *   - 可操作行（open/filter/select）= roving tabindex：同屏只有当前行 tabindex=0，
 *     ↑/↓ 移行（到边界继续滚容器）、PgUp/PgDn 滚一屏、滚轮不劫持
 *   - 纯读行（none）= 行不可聚焦，容器 tabindex=0 是唯一停靠点
 *   - 容器与被聚焦行**不得同时有 tabindex**
 *   - fit 打开内滚：12px 渐隐 + 「还有 N 行」+ thin 滚动条，到底三者同时消失
 */
// 必须与 tokens.css 的 --row-h 同值（28 = td 上下 5+5 + 12px 字号的行盒）。
// 它决定「还有 N 行」的估算和 ↑/↓ 到边界时的滚动步长。
const ROW_H = 28;
const FIT_STORE = "qd.fit.anchor.";

// 焦点恢复只解决"同一会话内、DOM 重建"，**不持久化**：
// 存进 sessionStorage 会让用户 Ctrl+R 后焦点自己跳进票据表 —— 浏览器做了用户没要求的事。
// 首行锚定可以持久化（它跟"我在看哪一行"有关），焦点不行（它跟"我此刻在操作"有关）。
const FOCUS_KEY = new Map();
let lastInput = 'mouse';      // 'key' | 'mouse'
let inputBound = false;
function bindInputModality() {
  if (inputBound) return;
  inputBound = true;
  window.addEventListener('keydown', () => { lastInput = 'key'; }, true);
  window.addEventListener('mousedown', () => { lastInput = 'mouse'; }, true);
}

export function table({
  cols, rows, onRow = null, emptyText = '没有行', cls = '',
  fit = false, rowAction = null, label = null, anchorKey = null,
}) {
  const action = rowAction || (onRow ? 'open' : 'none');
  if (!rowAction && !onRow) {
    // 显式标注优先；两边都没给时按"纯读表"处理，但留一条日志，因为
    // "这张表能不能点"必须由 IA 决定，不该在这里被静默默认。
    console.debug('[ui.table] 未声明 data-row-action，按 none 处理');
  }
  const keyOf = anchorKey || label || 'table';
  const storeKey = FIT_STORE + keyOf;

  const wrap = h('div', { class: fit ? 'fit-wrap' : 'tbl-wrap' });
  const scroller = fit
    ? h('div', {
      class: 'fit',
      role: 'region',
      'aria-label': label || '数据表',
      'data-row-action': action,
      // 与行 tabindex 二选一：可操作表的停靠点是行，纯读表的停靠点是容器
      tabindex: action === 'none' ? '0' : null,
    })
    : h('div', { class: 'tbl-wrap' });

  const focusId = keyOf;
  bindInputModality();
  const t = h('table', { class: `tbl ${cls} ${onRow ? 'tbl-click' : ''}`.trim() });
  t.setAttribute('aria-rowcount', String(rows.length));
  t.appendChild(h('thead', {}, h('tr', {}, cols.map((c) => h('th', {
    class: c.cls || '', title: c.title || '', style: c.width ? { width: c.width } : undefined,
  }, c.label)))));

  const tbody = h('tbody');
  if (!rows.length) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: cols.length, class: 'tbl-empty' }, emptyText)));
  } else {
    rows.forEach((row, ri) => {
      const tr = h('tr', {});
      tr.dataset.rowKey = String(row.id ?? row.symbol ?? row.code ?? row.dataset ?? ri);
      for (const c of cols) {
        const td = h('td', { class: c.cls || '' });
        const raw = c.render ? c.render(row) : row[c.key];
        if (raw instanceof Node) td.appendChild(raw);
        else if (raw === null || raw === undefined || raw === '') {
          const why = typeof c.miss === 'function' ? c.miss(row) : null;
          td.appendChild(miss(why?.reason ?? why ?? null, why?.fix ? { fix: why.fix } : {}));
        } else td.innerHTML = String(raw);
        tr.appendChild(td);
      }
      const actionable = action !== 'none';
      if (actionable) {
        tr.tabIndex = -1;                     // roving：下面按 key 挑一个置 0
        tr.setAttribute('aria-rowindex', String(ri + 1));
      }
      if (onRow) {
        tr.addEventListener('click', () => onRow(row));
        tr.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter') { ev.preventDefault(); onRow(row); }
        });
      }
      tbody.appendChild(tr);
    });
  }

  // ↑/↓ 移行；到边界继续滚容器（不卡住）；PgUp/PgDn 滚一屏；滚轮不劫持
  if (action !== 'none') {
    tbody.addEventListener('keydown', (ev) => {
      const trs = [...tbody.querySelectorAll('tr[tabindex]')];
      const i = trs.indexOf(document.activeElement);
      if (i < 0) return;
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        const next = trs[i + (ev.key === 'ArrowDown' ? 1 : -1)];
        if (next) {
          trs.forEach((r) => { r.tabIndex = r === next ? 0 : -1; });
          FOCUS_KEY.set(focusId, next.dataset.rowKey || '');
          next.focus();
          next.scrollIntoView({ block: 'nearest', behavior: 'auto' });
        } else {
          scroller.scrollBy({ top: ev.key === 'ArrowDown' ? ROW_H * 3 : -ROW_H * 3, behavior: 'auto' });
        }
      } else if (ev.key === 'PageDown' || ev.key === 'PageUp') {
        ev.preventDefault();
        scroller.scrollBy({ top: (ev.key === 'PageDown' ? 1 : -1) * scroller.clientHeight, behavior: 'auto' });
      }
    });
  }

  if (action !== 'none') {
    const trs = [...tbody.querySelectorAll('tr')];
    const want = FOCUS_KEY.get(focusId) || null;
    // 行数一变索引全错位，所以停靠点按 key 记；被删掉的行退到原位置上一行，再退第一行
    let cur = want ? trs.find((tr) => tr.dataset.rowKey === want) : null;
    if (!cur && want) {
      const idx = trs.findIndex((tr) => tr.dataset.rowKey === want);
      cur = trs[Math.max(0, idx - 1)] || null;
    }
    if (!cur) cur = trs[0] || null;
    if (cur) cur.tabIndex = 0;
    // 刷新会吃掉焦点：记在内存里，重渲染后按 key 还原。
    // 三道护栏：① 只有键盘模态才还原（鼠标用户不该看到凭空出现的焦点环）
    //           ② 焦点仍"属于我们"（用户正在输入框里打字时绝不抢）
    //           ③ preventScroll，避免与首行锚定互抢、肉眼跳两下
    let hadFocus = false;
    tbody.addEventListener('focusin', (ev) => {
      const tr = ev.target.closest?.('tr[data-row-key]');
      if (!tr) return;
      FOCUS_KEY.set(focusId, tr.dataset.rowKey || '');
      hadFocus = true;
    });
    tbody.addEventListener('focusout', () => { hadFocus = false; });
    requestAnimationFrame(() => {
      const ae = document.activeElement;
      const ours = ae === document.body || ae === null || scroller.contains(ae);
      if (!hadFocus && !ours) return;          // 用户在别处操作，别抢
      if (lastInput !== 'key') return;         // 鼠标模态不还原焦点
      const again = [...tbody.querySelectorAll('tr')].find((tr) => tr.dataset.rowKey === cur.dataset.rowKey);
      again?.focus({ preventScroll: true });
    });
  }

  t.appendChild(tbody);
  scroller.appendChild(t);
  wrap.appendChild(scroller);

  if (fit) {
    const fade = h('div', { class: 'fit-fade', 'aria-hidden': 'true' });
    const more = h('div', { class: 'fit-more', 'aria-live': 'off' });
    wrap.append(fade, more);
    // 刷新前后保持首行锚定：票据表每几秒刷一次，不锚定会把用户正看的那行顶掉
    scroller.addEventListener('scroll', () => {
      recordAnchor(scroller, storeKey);
      paintFit(wrap, scroller, fade, more);
    }, { passive: true });
    requestAnimationFrame(() => {
      restoreAnchor(scroller, storeKey);
      paintFit(wrap, scroller, fade, more);
      observeFit();
    });
  }
  return wrap;
}

function recordAnchor(scroller, storeKey) {
  const rows = scroller.querySelectorAll('tbody tr');
  const box = scroller.getBoundingClientRect();
  for (const tr of rows) {
    if (tr.getBoundingClientRect().bottom > box.top + 2) {
      try { sessionStorage.setItem(storeKey, tr.dataset.rowKey || ''); } catch { /* 隐私模式 */ }
      return;
    }
  }
}

function restoreAnchor(scroller, storeKey) {
  let key = null;
  try { key = sessionStorage.getItem(storeKey); } catch { /* 隐私模式 */ }
  if (!key) return;
  const row = [...scroller.querySelectorAll('tbody tr')].find((tr) => tr.dataset.rowKey === key);
  if (row) scroller.scrollTop = Math.max(0, row.offsetTop - scroller.offsetTop - 1);
}

/** 三件套同进同退；判到底用「内容高差」，不用 scrollTop（行数变少时会被夹紧）。 */
function paintFit(wrap, scroller, fade, more) {
  if (!scroller.isConnected) return;
  const hidden = scroller.scrollHeight - scroller.clientHeight;
  const atEnd = hidden <= 4;
  // 低于 --fit-min-h 不内滚，改整块折叠更易用
  const tooShort = scroller.clientHeight < 220;
  const n = hidden > 4 ? Math.ceil(hidden / ROW_H) : 0;
  fade.hidden = atEnd;
  more.hidden = atEnd || n === 0;
  if (!more.hidden) more.textContent = `还有 ${n} 行`;
  scroller.style.overflowY = (atEnd && hidden <= 4) || tooShort ? 'auto' : 'auto';
  // 同屏最多 1 个内滚区：后来的自动降级为不限高
  const all = [...document.querySelectorAll('.fit-wrap')];
  const firstActive = all.find((w) => w.querySelector('.fit')?.dataset.fitActive === '1');
  if (firstActive && firstActive !== wrap) {
    const box = wrap.querySelector('.fit');
    if (box) box.style.maxHeight = 'none';
  } else {
    scroller.dataset.fitActive = '1';
  }
}

let fitRO = null;
function observeFit() {
  if (fitRO) return;
  // 一个全局 RO，避免每个表一个 observer 反向持有已卸载 DOM
  fitRO = new ResizeObserver(() => {
    for (const wrap of document.querySelectorAll('.fit-wrap')) {
      const scroller = wrap.querySelector('.fit');
      if (!scroller) continue;
      paintFit(wrap, scroller, wrap.querySelector('.fit-fade'), wrap.querySelector('.fit-more'));
    }
  });
  for (const wrap of document.querySelectorAll('.fit-wrap')) {
    const scroller = wrap.querySelector('.fit');
    if (scroller) fitRO.observe(scroller);
  }
}

export function kv(pairs) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of pairs) {
    if (v === null || v === undefined) continue;
    dl.appendChild(h('dt', {}, k));
    dl.appendChild(v instanceof Node ? h('dd', {}, v) : h('dd', { html: v }));
  }
  return dl;
}

/**
 * 单选分段控件。语义是 radiogroup，不是 toggle —— 所以用 aria-checked，不用 aria-pressed。
 * 只有选中项 tabindex=0，←/→ 在组内移动并立即生效。程序化改值一律走返回节点上的 set()。
 */
export function seg(options, value, onChange, { label = '' } = {}) {
  const items = Array.isArray(options) ? options : (options.items || []);
  const explicit = Array.isArray(options) ? label : (options.label || label);
  const derived = items.map((o) => o.label).filter(Boolean).join(' / ');
  const dev = /^(localhost|127\.0\.0\.1)$/.test(location.hostname);
  if (!explicit) {
    // 缺标签不能只 warn：生产里永远不会被修，读屏听到的永远是"选择"，
    // 而"选择"这个词在这个台子里每天出现十几次，完全无法区分。
    const msg = '[ui.seg] 缺少 aria-label；用 seg({label, items}, …) 或第 4 个参数 {label} 补齐';
    if (dev) throw new Error(msg);
    console.error(msg);
  }
  const name = explicit || derived;
  const box = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': name });
  let current = value;
  const btns = [];

  const paint = (v) => {
    for (const b of btns) {
      const on = b.dataset.value === String(v);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    }
  };

  for (const opt of items) {
    const btn = h('button', {
      type: 'button',
      role: 'radio',
      'aria-checked': 'false',
      'data-value': String(opt.value),
      title: opt.title || '',
      tabindex: '-1',
    }, opt.label);
    btn.addEventListener('click', () => {
      if (current === opt.value) return;
      current = opt.value;
      paint(current);
      onChange(opt.value);
      box.dispatchEvent(new CustomEvent('segchange', { detail: current, bubbles: true }));
    });
    btn.addEventListener('keydown', (ev) => {
      const i = btns.indexOf(btn);
      const step = ev.key === 'ArrowRight' || ev.key === 'ArrowDown' ? 1
        : ev.key === 'ArrowLeft' || ev.key === 'ArrowUp' ? -1 : 0;
      let next = null;
      if (step) next = btns[(i + step + btns.length) % btns.length];
      else if (ev.key === 'Home') next = btns[0];
      else if (ev.key === 'End') next = btns[btns.length - 1];
      if (!next) return;
      ev.preventDefault();
      next.focus();
      next.click();
    });
    btns.push(btn);
    box.appendChild(btn);
  }
  paint(current);

  // 外部（图表指标、周期切换等）改值：禁止直接改 classList / aria-checked
  box.set = (v, { silent = true } = {}) => {
    current = v;
    paint(v);
    if (!silent) onChange(v);
  };
  box.value = current;
  return box;
}

export function sectionHeader(text, right = null) {
  const el = h('div', { class: 'section-hd' }, text);
  if (right) el.appendChild(right);
  return el;
}

// ------------------------------------------------------------------ toast
// 容器必须**先存在于 DOM、后追加内容**，否则读屏不会播报：懒创建的 role=status
// 与首条消息同帧插入，辅助技术把那一段当成"一开始就在那儿的内容"。
// 所以 app.js 的 renderShell() 会先调一次 toastHost()，这里保留同样逻辑兜底。
const TOAST_MS = 5200;
const TOAST_MS_BAD = 8000;   // 故障条要念完：标题 + 一句原因，5 秒不够

export function toastHost() {
  let host = document.querySelector('.toasts');
  if (!host) {
    host = h('div', {
      class: 'toasts', role: 'status', 'aria-live': 'polite', 'aria-relevant': 'additions text',
    });
    document.body.appendChild(host);
  }
  return host;
}

export function toast(message, kind = '', title = null) {
  const host = toastHost();
  const bad = kind === 'bad';
  const item = h('div', {
    class: `toast ${kind}`.trim(),
    // 故障级用 alert 打断播报：这一条是"刚才那件事没成"
    role: bad ? 'alert' : null,
  });
  host.appendChild(item);
  // 空节点先进 DOM，下一次任务再填字 —— 同帧"插入 + 填字"会被当成已存在内容
  setTimeout(() => {
    if (title) item.appendChild(h('b', {}, title));
    item.appendChild(document.createTextNode(String(message)));
  }, 0);

  let left = bad ? TOAST_MS_BAD : TOAST_MS;
  let since = Date.now();
  let timer = setTimeout(() => item.remove(), left);
  // 悬停/聚焦时暂停消失（鼠标停在它上面还读得完）
  const pause = () => { clearTimeout(timer); left = Math.max(700, left - (Date.now() - since)); };
  const resume = () => { since = Date.now(); timer = setTimeout(() => item.remove(), left); };
  item.addEventListener('mouseenter', pause);
  item.addEventListener('mouseleave', resume);
  item.addEventListener('focusin', pause);
  item.addEventListener('focusout', resume);
}

// ---------------------------------------------------------------- 浮层基座
// 三个必须同时成立的东西：Esc/点遮罩/取消 都 resolve、焦点进得去出得来、背景滚动锁住。
let lockDepth = 0;

function lockViewScroll() {
  lockDepth += 1;
  if (lockDepth > 1) return;
  const view = document.querySelector('.view');
  if (view) view.style.overflow = 'hidden';
  // 键盘 PageDown/空格 也要挡住
  document.documentElement.classList.add('overlay-open');
}

function unlockViewScroll() {
  lockDepth = Math.max(0, lockDepth - 1);
  if (lockDepth > 0) return;
  const view = document.querySelector('.view');
  if (view) view.style.overflow = '';
  document.documentElement.classList.remove('overlay-open');
}

function focusables(root) {
  return [...root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function restoreFocus(prev) {
  if (prev && prev.isConnected && typeof prev.focus === 'function') { prev.focus(); return; }
  // 触发元素被列表重排销毁了：退到当前页签，绝不 focus 到 body
  const fallback = document.querySelector('.nav-item[aria-current="page"]') || document.querySelector('.view');
  fallback?.focus?.();
}

function trapTab(root) {
  return (ev) => {
    if (ev.key !== 'Tab') return;
    const f = focusables(root);
    if (!f.length) { ev.preventDefault(); return; }
    const first = f[0];
    const last = f[f.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  };
}

export function drawer({ title, body, footer = null, label = null }) {
  const prev = document.activeElement;
  const backdrop = h('div', { class: 'drawer-backdrop' });
  const panelEl = h('div', {
    class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'drawer-title',
  });
  const panelFooter = footer ? (() => { const ft = h('div', { class: 'drawer-ft' }); append(ft, [footer]); return ft; })() : null;
  let closed = false;

  const onKey = (ev) => {
    if (ev.key === 'Escape') { ev.stopPropagation(); close(); }
  };
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);   // 三条关闭路径都必须走到这里
    backdrop.removeEventListener('keydown', trap);
    unlockViewScroll();
    backdrop.remove();
    restoreFocus(prev);
  };
  const trap = trapTab(panelEl);

  panelEl.appendChild(h('div', { class: 'drawer-hd' },
    h('h3', { id: 'drawer-title', tabindex: '-1' }, title), h('div', { class: 'spacer' }),
    h('button', { class: 'btn btn-ghost btn-sm btn-icon', onclick: close, 'aria-label': '关闭' }, icon('close'))));
  const bd = h('div', { class: 'drawer-bd' });
  append(bd, [body]);
  panelEl.appendChild(bd);
  if (panelFooter) panelEl.appendChild(panelFooter);
  backdrop.appendChild(panelEl);
  backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) close(); });
  backdrop.addEventListener('keydown', trap);
  document.body.appendChild(backdrop);
  document.addEventListener('keydown', onKey);
  lockViewScroll();

  // drawer 的初始焦点是标题：先让读屏读出"这是什么抽屉"，再读内容
  requestAnimationFrame(() => panelEl.querySelector('.drawer-hd h3')?.focus());
  if (label) panelEl.setAttribute('aria-label', label);
  return { close, body: bd };
}

/**
 * 模态。Esc / 点遮罩 / 取消 三条路径都必须 resolve，不存在"永远 pending"。
 * initialFocus: 'cancel'（危险动作，连按 Enter 也不会成交）| 'input' | 'first'
 */
export function modal({ title, body, footer, initialFocus = 'first', alert = false }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const backdrop = h('div', { class: 'modal-backdrop' });
    let done = false;
    let trap = null;
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(false); }
    };
    const finish = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey);
      if (trap) backdrop.removeEventListener('keydown', trap);
      unlockViewScroll();
      backdrop.remove();
      restoreFocus(prev);
      resolve(value);
    };
    const box = h('div', {
      class: 'modal',
      role: alert ? 'alertdialog' : 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': 'modal-title',
    });
    box.appendChild(h('div', { class: 'modal-hd' }, h('h3', { id: 'modal-title', tabindex: '-1' }, title)));
    const bd = h('div', { class: 'modal-bd' });
    append(bd, [body]);
    box.appendChild(bd);
    const ft = h('div', { class: 'modal-ft' });
    append(ft, [footer(finish)]);
    box.appendChild(ft);
    backdrop.appendChild(box);
    backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) finish(false); });
    trap = trapTab(box);
    backdrop.addEventListener('keydown', trap);
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onKey);
    lockViewScroll();

    requestAnimationFrame(() => {
      const target = initialFocus === 'cancel' ? box.querySelector('.modal-ft button')
        : initialFocus === 'input' ? box.querySelector('input, textarea')
          : box.querySelector('.modal-ft button') || box.querySelector('.modal-bd input');
      (target || box.querySelector('h3'))?.focus();
    });
  });
}

export function confirmAction({ title = '确认操作', message, confirmText = '确认', danger = false, typeWord = null }) {
  return modal({
    title,
    alert: true,
    // 危险动作把焦点放在"取消"上：连按 Enter 不会成交
    initialFocus: typeWord ? 'input' : 'cancel',
    body: h('div', { class: 'stack', style: { gap: '10px' } },
      typeof message === 'string' ? h('div', { html: message, style: { lineHeight: '1.7', color: 'var(--ink-mid)' } }) : message,
      typeWord ? h('div', { class: 'field' },
        h('label', { for: 'confirm-word' }, `输入 ${typeWord} 以继续`),
        h('input', { class: 'input', id: 'confirm-word', autocomplete: 'off' })) : null),
    footer: (finish) => [
      h('button', { class: 'btn', onclick: () => finish(false) }, '取消'),
      h('button', {
        class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`,
        id: 'confirm-ok',
        onclick: () => {
          if (typeWord) {
            const v = document.getElementById('confirm-word')?.value.trim();
            if (v !== typeWord) { toast(`要输入 ${typeWord} 才能继续`, 'bad'); return; }
          }
          finish(true);
        },
      }, confirmText),
    ],
  });
}

/**
 * 渐进填充：面板先出骨架，数据到了再换内容。
 * 一个面板的接口挂了不影响别的面板 —— 与 /dashboard.json 的"每块各自降级"同一条原则。
 * 签名保持 slot(target, loader, {lines})。
 */
export function slot(target, loader, { lines = 3 } = {}) {
  const node = typeof target === 'string' ? document.querySelector(target) : target;
  if (!node) return;
  node.replaceChildren(skeleton(lines));
  Promise.resolve()
    .then(loader)
    .then((content) => {
      if (!node.isConnected) return;
      if (content === null || content === undefined) node.replaceChildren(empty('没有内容'));
      else if (content instanceof Node) node.replaceChildren(content);
      else node.replaceChildren(h('div', { html: String(content) }));
    })
    .catch((err) => {
      if (!node.isConnected) return;
      // 401 已经由外壳接管；这块不该再画一个"请求失败"
      if (err?.handled) { node.replaceChildren(); return; }
      node.replaceChildren(errorBox(err));
    });
}

export { esc };
