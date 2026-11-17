// 应用外壳：口令闸门 → 导航轨 → 页面。
// 路由走 hash，静态托管不需要任何服务端 rewrite（uvicorn 只做 StaticFiles）。
//
// 两条与"时间"有关的约定：
// 1. 落地页按时段决定，但只在 hash 为空时生效 —— 收藏和回退永远优先。
// 2. 状态条 28px，常驻回答"这是哪一刻的数"。底边 3px 就是数据新鲜度尺。

import { api, getToken, setToken, clearToken, probe, probeWhy } from './api.js';
import { h, icon, toast, toastHost, chip, skeleton, miss, judge } from './ui.js';
import { hhmmssOf, ymd, duration } from './fmt.js';
import { zh } from './zh.js';
import { tickerBar, refreshTape } from './tape.js';

const PAGES = [
  { id: 'today', label: '今日', glyph: '01', title: '今日台面', hint: '今天做不做、做哪只' },
  { id: 'market', label: '盘面', glyph: '02', title: '盘面结构', hint: '钱在往哪走、有没有我要的形状' },
  { id: 'risk', label: '持仓与风险', glyph: '03', title: '持仓与风险', hint: '手上这几只现在安不安全' },
  { id: 'decide', label: '决策', glyph: '04', title: '决策与票据', hint: '哪些票在等我、为什么被拒' },
  { id: 'review', label: '复盘', glyph: '05', title: '复盘', hint: '今天哪条判断被数据推翻了' },
  { id: 'kernel', label: '内核', glyph: '06', title: '内核解剖', hint: '这个台子现在还值得信吗' },
];

const SECONDARY = {
  stock: { id: 'stock', label: '个股', title: '个股研究', hint: 'K 线、指标与九块档案' },
  external: { id: 'external', label: '实盘（账外）', title: '实盘（账外）', hint: '在桌面面板里录；这里只读' },
};

const state = {
  page: null,
  status: null,
  health: null,
  dataStatus: null,
  readiness: null,
  pools: null,
  tickets: null,
};

// 页面模块的当前实例 + 它注册的清理函数
let pageMod = null;
let disposers = [];
let routeSeq = 0;

const root = document.getElementById('root');
let viewEl = null;
let navEl = null;
let statusEl = null;
let searchInput = null;

let shellReady = false;
let bindingsDone = false;
let tickTimer = null;
let pendingScroll = null;
// 当前已经画进 viewEl 的那条 hash。滚动位置按它存 —— hashchange 之后
// location.hash 已经是新页，拿它当 key 会把上一页的位置写到新页名下。
let renderedHash = '';
const SCROLL_KEY = 'qd.scroll.';

const SCROLL_KEEP = 20;

/** 滚动位置按 hash 存，每个个股 symbol 一条 —— 会话内只增不减。
 *  留着最近 20 条：回退一次只可能用到上一条，其余是泄漏。 */
function saveScroll() {
  if (!viewEl || !renderedHash) return;
  try {
    const key = SCROLL_KEY + renderedHash;
    sessionStorage.removeItem(key);          // 重新插入 = 挪到队尾
    sessionStorage.setItem(key, String(viewEl.scrollTop));
    const mine = Object.keys(sessionStorage).filter((k) => k.startsWith(SCROLL_KEY));
    for (const stale of mine.slice(0, Math.max(0, mine.length - SCROLL_KEEP))) {
      sessionStorage.removeItem(stale);
    }
  } catch { /* 隐私模式：存不下就算了，不该让切页失败 */ }
}

function restoredScroll() {
  try { return Number(sessionStorage.getItem(SCROLL_KEY + location.hash)) || 0; } catch { return 0; }
}

function disposePage() {
  for (const fn of disposers.reverse()) {
    try { fn(); } catch (err) { console.warn('[cleanup]', err); }
  }
  disposers = [];
  if (pageMod?.unmount) {
    try { pageMod.unmount(); } catch { /* 页面卸载不该阻断导航 */ }
  }
  pageMod = null;
}

// ------------------------------------------------------------------ 时段
function landingPage() {
  const now = new Date();
  const hh = now.getHours() * 100 + now.getMinutes();
  if (hh < 915) return 'today';
  if (hh < 930) return 'today';
  if (hh >= 930 && hh <= 1130) return 'market';
  if (hh >= 1300 && hh <= 1500) return 'market';
  if (hh > 1505) return 'review';
  return 'today';
}

// ------------------------------------------------------------------ 口令闸门
// 401 时闸门盖在整页上，不销毁当前页面：重输口令后回到原处继续（滚动位置保留）。
function renderGate(message = '') {
  const input = h('input', {
    class: 'input', type: 'password', placeholder: 'X-Quant-Token', autocomplete: 'off',
    'aria-label': '访问口令',
  });
  const err = h('div', { class: 'gate-err', role: 'alert' }, message);
  const btn = h('button', { class: 'btn btn-primary', style: { height: '32px' } }, '进入');

  const submit = async () => {
    const value = input.value.trim();
    if (!value) { err.textContent = '先填口令'; return; }
    setToken(value);
    err.textContent = '';
    btn.disabled = true;
    btn.textContent = '校验中…';
    const res = await probeWhy('/portfolio');
    btn.disabled = false;
    btn.textContent = '进入';
    if (res.ok && res.data && res.data.equity !== undefined) {
      boot();
      return;
    }
    if (!res.ok && res.status !== 401 && res.status !== 403) {
      // 服务没响应 ≠ 口令错。桌面窗口自己拉起服务要 5–15 秒，这段时间把口令
      // 清掉会让人去改一个没错的 .env。
      err.textContent = `服务没有响应（${res.code || res.status || '网络'}）。等一下再点进入，或看 data/panel.log。`;
      return;
    }
    clearToken();
    err.textContent = '口令无效或缺失（X-Quant-Token）。改 .env 后重启服务，不要重试。';
    input.select();
  };
  btn.addEventListener('click', submit);
  input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') submit(); });

  root.replaceChildren(h('div', { class: 'gate' },
    h('div', { class: 'gate-card', role: 'dialog', 'aria-modal': 'true', 'aria-label': '访问口令' },
      h('div', { class: 'gate-brand' }, h('h1', {}, 'quant-desk'), h('span', {}, 'A 股模拟盘 · 本地')),
      h('p', {}, '这是你自己机器上的交易台。口令在项目根目录的 ', h('code', {}, '.env'), ' 里，键名 ', h('code', {}, 'QUANT_TOKEN'), '。'),
      h('p', { class: 'faint small' }, '口令只存在这个浏览器的 localStorage，不写进页面源码，也不发给任何第三方。'),
      input, err, btn)));
  setTimeout(() => input.focus(), 30);
}

// 桌面窗口会自己拉起服务（panel.pyw 的 _start_service）。那 5–15 秒里
// `boot()` 拿不到 /portfolio，旧代码把它读成"口令没有通过"——用户会去改一个
// 没错的配置。这两屏只在这条路上出现，浏览器里几乎碰不到。
function renderWaiting(tries) {
  root.replaceChildren(h('div', { class: 'gate' },
    h('div', { class: 'gate-card', role: 'status', 'aria-live': 'polite' },
      h('div', { class: 'gate-brand' }, h('h1', {}, 'quant-desk'), h('span', {}, '正在连接本地服务')),
      h('p', {}, `第 ${tries} 次尝试…`),
      h('p', { class: 'faint small' }, '窗口会自己把服务拉起来，通常 5–15 秒。这不是口令问题，不用改 .env。'),
      h('button', { class: 'btn', style: { height: '32px' }, onclick: () => boot() }, '立即重试'))));
}

function renderUnavailable(detail) {
  root.replaceChildren(h('div', { class: 'gate' },
    h('div', { class: 'gate-card', role: 'alert' },
      h('div', { class: 'gate-brand' }, h('h1', {}, '本地服务没有响应'), h('span', {}, detail || '')),
      h('p', {}, '窗口会自己启动服务，但这次没等到。多半是端口被占用，或服务起来后崩了。'),
      h('p', { class: 'faint small' }, '看 ', h('code', {}, 'data/panel.log'), '；手动启动可用 ',
        h('code', {}, '.venv\Scripts\python.exe -m uvicorn service.api.app:app --port 8811'), '。'),
      h('button', { class: 'btn btn-primary', style: { height: '32px' }, onclick: () => boot() }, '重新连接'))));
}

// ------------------------------------------------------------------ 外壳
function renderShell() {
  // toast 容器常驻：与首条消息同帧创建的话，role=status 不会播报任何东西。
  // 建在 body 上、与页面无关，所以放在外壳这一层而不是某个页面里。
  toastHost();

  navEl = h('nav', { class: 'nav', 'aria-label': '主导航' });
  for (const page of PAGES) {
    navEl.appendChild(h('button', {
      class: 'nav-item', 'data-page': page.id, title: page.hint,
      onclick: () => navigate(page.id),
    }, h('span', { class: 'nav-glyph' }, page.glyph), h('span', {}, page.label)));
  }
  // 不编号的入口：个股是二级页，但它是这个台子的第一诉求，不能只藏在列表行里
  navEl.appendChild(h('div', { class: 'nav-group' }, '二级页'));
  navEl.appendChild(h('button', {
    class: 'nav-item', 'data-page': 'stock', title: '打开上次看过的票（s）',
    onclick: () => openStock(),
  }, h('span', { class: 'nav-glyph' }, '·'), h('span', {}, '个股')));
  navEl.appendChild(h('button', {
    class: 'nav-item', 'data-page': 'external', title: '实盘（账外）在桌面面板里录；这里只读',
    onclick: () => navigate('external'),
  }, h('span', { class: 'nav-glyph' }, '·'), h('span', {}, '实盘（账外）')));

  searchInput = h('input', {
    class: 'input', placeholder: '代码 / 名称  Ctrl+K', 'aria-label': '搜索标的',
    autocomplete: 'off', spellcheck: 'false',
  });
  searchInput.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const raw = searchInput.value.trim().toUpperCase();
    if (!raw) return;
    const code = /^\d{6}\.(SH|SZ|BJ)$/.test(raw) ? raw : /^\d{6}$/.test(raw) ? guessMarket(raw) : null;
    searchInput.value = '';
    if (code) navigate(`s/${code}`);
    else navigate('market', { q: raw });
  });

  const railFoot = h('div', { class: 'rail-foot', id: 'rail-foot' });
  const rail = h('aside', { class: 'rail' },
    h('div', { class: 'brand' },
      h('div', { class: 'brand-mark' }), h('div', { class: 'brand-name' }, 'quant·desk'),
      h('div', { class: 'brand-sub' }, 'v0.3')),
    navEl, railFoot);

  const topbar = h('header', { class: 'topbar' },
    h('h1', { id: 'page-title' }, '—'),
    h('div', { class: 'sub', id: 'page-sub' }, ''),
    h('div', { class: 'topbar-spacer' }),
    h('div', { class: 'search' }, searchInput),
    h('div', { class: 'row-tight', id: 'top-actions' }),
    h('button', {
      class: 'btn btn-ghost btn-icon', title: '重新取数（R）', 'aria-label': '刷新',
      onclick: () => hardRefresh(),
    }, icon('refresh')));

  viewEl = h('div', { class: 'view', id: 'view' });
  statusEl = h('footer', { class: 'statusbar', id: 'statusbar' });

  offlineEl = h('div', { class: 'offline-bar', id: 'offline-bar', role: 'status', hidden: true },
    h('span', { class: 'dot bad' }), h('b', {}, '服务已断开'),
    // 全带唯一一句操作禁令：单独一个类，用 --ink-2（8.2:1），不靠 .faint 的最低档
    h('span', { class: 'offline-note' }, '正在重连…（数据停在上一次成功取到的时刻，别照着下单）'));
  const tape = tickerBar();
  root.replaceChildren(h('div', { class: 'app' }, rail,
    h('div', { class: 'main' }, topbar, tape, offlineEl, viewEl, statusEl)));
}

function guessMarket(code) {
  if (/^(6|9)/.test(code)) return `${code}.SH`;
  if (/^(0|2|3)/.test(code)) return `${code}.SZ`;
  if (/^(4|8)/.test(code)) return `${code}.BJ`;
  return `${code}.SH`;
}

const LAST_SYMBOL = 'qd.lastSymbol';
function openStock(symbol = null) {
  const target = symbol || sessionStorage.getItem(LAST_SYMBOL);
  if (target) { navigate(`s/${target}`); return; }
  searchInput?.focus();
  searchInput?.select();
}

// ------------------------------------------------------------------ 路由
export function navigate(page, params = {}) {
  // 个股是二级页：#/s/600000.SH。页面里仍有人写 navigate('stock', {symbol})，
  // 在这里统一翻译，而不是让 7 个页面各自记住路由形状。
  if (page === 'stock' || page === 's') {
    const { symbol, ...rest } = params;
    const query = new URLSearchParams(rest).toString();
    if (symbol) {
      location.hash = `#/s/${encodeURIComponent(symbol)}${query ? `?${query}` : ''}`;
      return;
    }
  }
  const query = new URLSearchParams(params).toString();
  location.hash = `#/${page}${query ? `?${query}` : ''}`;
}

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  const params = Object.fromEntries(new URLSearchParams(query || ''));
  const segs = path.split('/').filter(Boolean);
  // 二级页：#/s/600000.SH —— 可收藏、可回退
  if (segs[0] === 's' && segs[1]) {
    return { key: 'stock', params: { ...params, symbol: decodeURIComponent(segs.slice(1).join('/')) } };
  }
  // 旧链接 #/stock?symbol=… 重定向一次，不留死链
  if (segs[0] === 'stock') return { key: 'stock-legacy', params };
  // 二级入口（个股 / 实盘）也走这里：它们不在 PAGES 里，但必须能被 hash 直达。
  const hit = PAGES.find((p) => p.id === segs[0]) || SECONDARY[segs[0]];
  return { key: hit ? hit.id : null, params };
}

function metaOf(key) {
  return PAGES.find((p) => p.id === key) || SECONDARY[key] || PAGES[0];
}

async function route() {
  // 空 hash 才按时段落地；本次会话手动切过页签的，优先回上次那一页（防抢焦点）
  const first = parseRoute();
  if (!first.key) {
    const last = sessionStorage.getItem('qd.manualNav') ? sessionStorage.getItem('qd.lastPage') : null;
    location.replace(`#/${last || landingPage()}`);
    return;
  }
  const { key, params } = first;
  if (key === 'stock-legacy') {
    location.replace(`#/s/${encodeURIComponent(params.symbol || '')}${params.q ? `?q=${encodeURIComponent(params.q)}` : ''}`);
    return;
  }
  sessionStorage.setItem('qd.lastPage', key === 'stock' && params.symbol ? `s/${params.symbol}` : key);
  const seq = ++routeSeq;
  const meta = metaOf(key);
  state.page = key;

  for (const btn of navEl.querySelectorAll('.nav-item')) {
    if (btn.dataset.page === key) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  document.getElementById('page-title').textContent = key === 'stock' && params.symbol ? params.symbol : meta.title;
  document.getElementById('page-sub').textContent = meta.hint;
  document.title = `${key === 'stock' && params.symbol ? params.symbol : meta.label} · quant-desk`;
  if (key === 'stock' && params.symbol) sessionStorage.setItem(LAST_SYMBOL, params.symbol);

  // 换页前把"上一页滚到哪儿"按上一页的 hash 存起来，mount 后原样还回去
  saveScroll();
  disposePage();
  // 骨架要说话：个股页的 /symbol/profile 实测 0.88s，纯灰条会让人以为卡住了。
  // 缩进交给 .view 的 padding（12px 14px 20px），骨架自己不再加 —— 加过一次
  // 20px，于是每次切页横向抖 20px。
  viewEl.replaceChildren(h('div', { class: 'stack', style: { gap: '12px' } },
    h('div', { class: 'micro faint' }, `正在取「${meta.label}」的数…`),
    skeleton(6)));

  try {
    const moduleVersion = encodeURIComponent(state.health?.build?.code_fingerprint || 'dev');
    const mod = await import(`./pages/${key}.js?v=${moduleVersion}`);
    if (seq !== routeSeq) return;           // 连点导航：慢的那次不许覆盖新页
    pageMod = mod;
    const ctx = {
      params, page: meta, navigate, refresh: hardRefresh,
      onCleanup(fn) { if (typeof fn === 'function') disposers.push(fn); },
    };
    const node = await mod.mount(ctx);
    if (seq !== routeSeq) { disposePage(); return; }
    viewEl.replaceChildren(node);
    // 恢复顺序：内存里的 pendingScroll（口令失效后重进）优先，其次是本页上次的位置
    viewEl.scrollTop = pendingScroll ?? restoredScroll();
    pendingScroll = null;
    renderedHash = location.hash;
  } catch (err) {
    if (seq !== routeSeq) return;
    console.error(err);
    try {
      window.pywebview?.api?.report_error?.(err?.stack || String(err));
    } catch { /* diagnostics must never replace the original page error */ }
    viewEl.replaceChildren(h('div', { class: 'panel' },
      h('div', { class: 'panel-bd' },
        h('div', { class: 'err' },
          h('div', { class: 'code' }, 'PAGE_FAILED'),
          h('div', {}, `${meta.label} 页渲染失败：${err?.message || err}`),
          h('div', { class: 'next' }, '这一页的接口可能变了；刷新一次，仍失败就看 data/service.log。'),
          h('details', {},
            h('summary', {}, '技术细节'),
            h('pre', { class: 'micro' }, err?.stack || String(err)))))));
  }
}

// ------------------------------------------------------------------ 状态
/** 状态条的数据源是分开的，这是故意的：一个数字只有一个来源。
    三池/闸门只认 /market/pools（含 age_seconds），readiness 只认 /readiness，
    待批票只认 /tickets?status=open —— 不从 /facts 或 /dashboard 里"顺手取一个"。 */
async function loadStatus({ fresh = false } = {}) {
  const t = (ttl) => (fresh ? 0 : ttl);
  const [dash, health, dataStatus, readiness, pools, tickets] = await Promise.all([
    api('/dashboard.json?slim=1', { ttl: t(5000), silent: true, meta: true }).catch(() => null),
    api('/health', { ttl: 10000, silent: true }).catch(() => null),
    api('/data/status', { ttl: 120000, silent: true }).catch(() => null),
    api('/readiness', { ttl: t(60000), silent: true, meta: true }).catch(() => null),
    api('/market/pools', { ttl: t(60000), silent: true, meta: true }).catch(() => null),
    api('/tickets?status=open&limit=20', { ttl: t(15000), silent: true, meta: true }).catch(() => null),
  ]);
  state.status = dash?.data ?? null;
  state.health = health;
  state.dataStatus = dataStatus;
  state.readiness = readiness;
  state.pools = pools;
  state.tickets = tickets;
  state.dashboardAt = dash?.at ?? null;
  renderStatus();
  renderRailFoot();
  refreshTape();
}

function ageText(at) {
  if (!at) return '';
  const sec = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (sec < 60) return `${sec}s 前`;
  return `${Math.round(sec / 60)}m${String(sec % 60).padStart(2, '0')}s 前`;
}

function datasetFreshness(ds) {
  if (!ds?.cover_end) return { kind: 'cold', text: '无覆盖' };
  // 只有 coverage_boundary=true 的逐日面板，cover_end 才代表"数据抓到哪天"。
  // 交易日历排到 2027、参考表按最新实体日期，都不是滞后也不是未来数据。
  if (ds.coverage_boundary === false) {
    return { kind: '', text: `${zh.datasetKind(ds.kind)} · 覆盖日不作新鲜度判据` };
  }
  const y = Number(ds.cover_end.slice(0, 4));
  const m = Number(ds.cover_end.slice(4, 6)) - 1;
  const d = Number(ds.cover_end.slice(6, 8));
  const lagDays = Math.round((Date.now() - new Date(y, m, d)) / 86400000);
  if (lagDays <= 1) return { kind: 'fresh', text: `${ymd(ds.cover_end)}（T+${lagDays}）` };
  if (lagDays <= 4) return { kind: 'warm', text: `${ymd(ds.cover_end)}（T+${lagDays}）` };
  return { kind: 'cold', text: `${ymd(ds.cover_end)}（T+${lagDays}）` };
}

/** 状态条的钟：1 秒独立 tick。
 *  它显示到秒，而全仓只有 30 秒的数据拍与 3 秒的探活 —— 原先两次数据拍之间
 *  时钟是冻住的。一块专门回答"这是哪一刻"的区域，停住比不显示更糟。
 *  aria-hidden：每秒变化的文本不该被读屏播报，日期与 as_of 另有常驻项。 */
let clockEl = null;
let clockTimer = null;

function ensureClock() {
  if (!clockEl) {
    clockEl = h('span', { class: 'sb-item sb-clock num', 'aria-hidden': 'true' });
    clockEl.textContent = hhmmssOf(new Date());
  }
  return clockEl;
}

function stopClock() {
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
}

function startClock() {
  stopClock();
  const tick = () => { if (clockEl) clockEl.textContent = hhmmssOf(new Date()); };
  tick();
  clockTimer = setInterval(tick, 1000);
}

/** 状态条：28px，8 项。缺数一律走 miss()，不做 Number(null) 兜底。
 *
 * 三条与「重绘频率」有关的约定（这一格是全站唯一被 30 秒轮询刷新的常驻控件）：
 *   1. 时钟不在这里算 —— 见 ensureClock()/startClock()，它自己 1 秒一拍。
 *   2. **按格子复用节点**：每个格子有 key 与内容签名，签名没变就返回上一轮的同一个
 *      节点；重建只发生在「这一格的内容真的变了」。
 *   3. 整条只在「格子数量或顺序变了」时才动结构（例如 readiness 从缺到有）。
 *
 * 为什么不再整条 replaceChildren：这条上有可聚焦元素（待批 N 是按钮），
 * replaceChildren 会把节点全部换掉 —— 键盘用户 Tab 到「待批 3」正要按 Enter，
 * 轮询刷过，Enter 就落在 body 上。签名短路只挡得住「没变化」，挡不住「值变了」。
 */
const sbNodes = new Map();   // key -> { sig, node }
const sbSpacer = h('span', { class: 'sb-item sb-grow' });

/** 取一个格子：签名相同就复用上一轮的节点，否则新建并登记。 */
function sbCell(key, sig, build) {
  const prev = sbNodes.get(key);
  if (prev && prev.sig === sig) return prev.node;
  const node = build();
  node.dataset.sbKey = key;
  sbNodes.set(key, { sig, node });
  return node;
}

/** 把想要的孩子逐个对齐到当前 DOM：能复用就复用，只换真正变了的那一格。
 *  替换前后若焦点在这一格里（待批按钮），按 data-sb 还回去。 */
function reconcileStatus(desired) {
  if (!statusEl) return;
  const kids = Array.from(statusEl.children);
  if (kids.length === desired.length && kids.every((n, i) => n === desired[i])) return;

  const active = document.activeElement;
  const pinned = active && statusEl.contains(active) ? active.dataset.sb : null;

  for (let i = 0; i < desired.length; i += 1) {
    const want = desired[i];
    const have = statusEl.children[i];
    if (have === want) continue;
    if (have) have.replaceWith(want);
    else statusEl.appendChild(want);
  }
  while (statusEl.children.length > desired.length) {
    statusEl.lastElementChild.remove();
  }
  if (pinned) {
    statusEl.querySelector(`[data-sb="${pinned}"]`)?.focus({ preventScroll: true });
  }
}

function renderStatus() {
  if (!statusEl) return;
  const s = state.status || {};
  const summary = s.summary || {};
  const session = s.brief?.session || s.session || {};
  const today = s.persona?.today || {};

  // 1 交易日
  const day = s.day || state.dataStatus?.cover_end;
  const dayNode = sbCell('day', day || '—',
    () => h('span', { class: 'sb-item sb-strong' }, day ? ymd(day) : '—'));

  // 2 此刻：常驻节点，文本由 1 秒定时器独立更新（不进签名，也不在这里重建）
  const clock = ensureClock();

  // 3 数据时刻：收盘包 / 帧 是两件事，不许混成一个「实时」
  const commit = state.health?.build?.commit;
  const dashAge = ageText(state.dashboardAt);
  const priced = session.priced_through;
  const closeTitle = `日线类面板的 as_of${dashAge ? ` · 取数 ${dashAge}` : ''}` +
    `${commit ? ` · build ${commit.slice(0, 7)}${state.health.build.dirty ? '*' : ''}` : ''}`;
  // 签名只认日期：「取数 X 前」每拍都变，放进签名会让这一格 30 秒重建一次。
  // 易变文案走 title，原地赋值即可。
  const closeNode = priced
    ? (() => {
      const n = sbCell('close', priced,
        () => h('span', { class: 'sb-item' }, `收盘 ${ymd(priced)}`));
      n.title = closeTitle;
      return n;
    })()
    : sbCell('close', 'none',
      () => miss('无收盘包', { endpoint: '/dashboard.json' }));

  // 4 readiness（进 popover，但一旦不是 READY 就升回常驻位）
  const ready = state.readiness?.data;
  let readyNode;
  if (ready?.code) {
    // 「未就绪」三个字不够用：NON_TRADING_DAY / THIN_COVERAGE / AWAITING_PUBLISH
    // 是三件完全不同的事，对应三种不同的下一步。状态条窄，所以显示中文码，
    // 原始枚举留在 title 与 aria 里——界面不替内核改写口径。
    // 盘中不是异常：SESSION_OPEN 只说「今天这根 bar 还没生成」，不禁止下单。
    // 把它画成实心方，每天 9:30–15:00 都会误报一次「不通过」。
    const isReady = ready.code === 'READY' || ready.code === 'SESSION_OPEN';
    const label = ready.code === 'READY' ? '就绪' : (zh.readiness(ready.code) || '未就绪');
    const why = Array.isArray(ready.blocks) && ready.blocks.length
      ? `，禁止 ${ready.blocks.length} 类写入` : '';
    const title = `${ready.code}${why}${ready.as_of ? ` · as_of ${ready.as_of}` : ''}`;
    const aria = isReady ? `数据${label}，${ready.code}` : `数据${label}，${ready.code}${why}`;
    readyNode = sbCell('ready', `${isReady}|${label}|${aria}|${title}`, () => {
      const node = judge(isReady, { text: label, aria });
      node.title = title;
      return node;
    });
  } else {
    readyNode = sbCell('ready', 'missing', () => miss('未取到 readiness', { endpoint: '/readiness' }));
  }

  // 5 闸门：只认 /market/pools；读屏报数字，不只是状态
  const pool = state.pools?.data;
  const gates = pool?.gates;
  const sent = s.sentiment || {};
  let gatesNode;
  if (gates && gates.checks && Object.keys(gates.checks).length) {
    // 这两个表必须分开：`shown` 进可见文字，`spoken` 进 aria。
    // 原来只有 `spoken`，可见文字是光秃秃的「语气」「炸板」「高度」三个标签——
    // 2026-11-14 在真窗口截图里才看出来，状态栏三格是死的。
    const shown = {
      tone_ok: sent.tone ? String(sent.tone) : '',
      break_rate_ok: Number.isFinite(sent.break_rate) ? `${(Number(sent.break_rate) * 100).toFixed(2)}%` : '',
      height_ok: Number.isFinite(sent.max_boards) ? `${sent.max_boards} 板` : '',
    };
    const spoken = {
      tone_ok: shown.tone_ok ? `，语气 ${shown.tone_ok}` : '',
      break_rate_ok: shown.break_rate_ok ? `，炸板率 ${shown.break_rate_ok}` : '',
      height_ok: shown.height_ok ? `，最高 ${shown.height_ok}` : '',
    };
    // 闸门判的是**某一天**的三池，不是「此刻」。收盘终值不会随时间变质
    //（后端 stale=false 是对的），但屏幕上必须说清是哪一天 —— 否则
    // 11-07 看到「闸门全过」，会以为今天有得做。
    const asOf = pool?.as_of
      ? `，as_of ${ymd(pool.as_of)}${gates.phase === 'settled' ? ' 收盘终值' : ' 盘中'}` : '';
    const aged = Number.isFinite(pool?.age_seconds) ? `，数据 ${duration(pool.age_seconds)} 前` : '';
    const title = [gates.note, asOf.replace(/^，/, ''), aged.replace(/^，/, '')]
      .filter(Boolean).join(' · ') || '三个闸门';
    // 同上：`数据 X 前` 与 title 不进签名，否则每拍重建三个 judge。
    gatesNode = sbCell('gates',
      JSON.stringify([gates.checks, shown, spoken, asOf, gates.indeterminate]),
      () => {
        const box = h('span', { class: 'sb-item sb-gates' });
        for (const [k, label] of [['tone_ok', '语气'], ['break_rate_ok', '炸板'], ['height_ok', '高度']]) {
          const v = gates.checks[k];
          const tail = spoken[k] || '';
          const face = shown[k] ? `${label} ${shown[k]}` : label;
          box.appendChild(judge(v ?? null, {
            text: face,
            aria: v === true ? `通过${tail}${asOf}` : v === false ? `不通过${tail}${asOf}`
              : `未运行${asOf}${gates.indeterminate?.length ? `，${gates.indeterminate.join('、')}` : ''}`,
            reason: v === null || v === undefined ? (gates.indeterminate || []).join('、') : null,
          }));
        }
        return box;
      });
    gatesNode.title = title;
  } else {
    const note = gates?.note || '三池未入库，闸门无法判断';
    gatesNode = sbCell('gates', `missing|${note}`,
      () => miss(note, { endpoint: '/market/pools', fix: 'snapshot_intraday.py' }));
  }

  // 6 kill switch / safe mode —— 常态系统状态，不给「错误」观感，也不着色：
  // 这两个是用户自己设的开关，紫（故障级别）与琥珀（数据陈旧）都不对。
  // 「需要注意」靠字重 + 满墨表达，见 .chip.state-on。
  const killOn = Boolean(summary.kill_switch);
  const safeOn = Boolean(summary.safe_mode);
  const killNode = sbCell('kill', String(killOn),
    () => chip(killOn ? '总开关 已拉下' : '总开关 正常', killOn ? 'state-on' : 'off'));
  const safeNode = sbCell('safe', String(safeOn),
    () => chip(safeOn ? '安全模式 开' : '安全模式 关', safeOn ? 'state-on' : 'off'));

  // 7 人格 + 今日可否下单
  const persona = s.persona?.current?.name;
  const canPlace = Boolean(today.can_place_orders);
  const personaNode = sbCell('persona', `${persona || '—'}|${canPlace}`, () => h('span', { class: 'sb-item' },
    persona || '—', ' · ', canPlace ? '可下单' : '不可下单'));

  // 8 待批票 —— 只认 /tickets?status=open。有票时这一项就是行动入口：
  // 状态条上唯一的「有事等你去处理」，不给按钮等于让人自己去找。
  const tk = state.tickets?.data;
  const pending = tk && Number.isFinite(tk.count) ? tk.count : null;
  const tkTitle = tk?.note || '待批票据（/tickets?status=open）';
  const tkBody = ['待批 ', pending === null
    ? miss('未取到', { endpoint: '/tickets?status=open' })
    : h('b', {}, String(pending))];
  // 0 张时保持纯文本：不给点了没反应的按钮。
  // 有票时是 <button>，是整条上唯一可聚焦的元素 —— 值变了（3→4）只替换这一格，
  // 其余八个节点原地不动，焦点由 reconcileStatus 按 data-sb 复还。
  const ticketsNode = pending > 0
    ? sbCell('tickets', `act|${pending}|${tkTitle}`, () => h('button', {
      class: 'sb-item sb-act', type: 'button', 'data-sb': 'tickets',
      title: `${tkTitle} · 点击去决策页处理`,
      onclick: () => navigate('decide'),
    }, ...tkBody))
    : sbCell('tickets', `idle|${pending}|${tkTitle}`,
      () => h('span', { class: 'sb-item', title: tkTitle }, ...tkBody));

  const fresh = sbCell('fresh', freshnessSig(), freshnessStrip);
  reconcileStatus([dayNode, clock, closeNode, readyNode, gatesNode, killNode, safeNode,
    personaNode, ticketsNode, sbSpacer, fresh]);
}

/** 新鲜度尺的内容签名：段数与每段的类型/标题。变了才重建整条尺。 */
function freshnessSig() {
  const sets = state.dataStatus?.datasets || [];
  return [
    state.dataStatus?.cover_end || '',
    sets.map((ds) => `${ds.dataset}:${datasetFreshness(ds).kind}:${datasetFreshness(ds).text}`).join('|'),
  ].join('#');
}

/** 底边 3px 就是新鲜度尺，不单独占一行。三态"通"用中性灰：青和跌绿在 3px 上分不出。 */
function freshnessStrip() {
  const sets = state.dataStatus?.datasets || [];
  if (!sets.length) return h('div', { class: 'fresh-strip', 'aria-hidden': 'true' });
  const bar = h('div', {
    class: 'fresh-strip',
    role: 'img',
    'aria-label': `数据新鲜度：${sets.length} 个数据集，最新覆盖 ${state.dataStatus?.cover_end || '未知'}`,
  });
  for (const ds of sets) {
    const f = datasetFreshness(ds);
    bar.appendChild(h('div', {
      class: `fresh-seg ${f.kind}`,
      title: `${ds.dataset} · 截至 ${f.text} · ${ds.rows === null || ds.rows === undefined ? '行数未取到' : `${Number(ds.rows).toLocaleString('zh-CN')} 行`} · ${ds.trust_tier || ''}`,
    }));
  }
  return bar;
}

function renderRailFoot() {
  const box = document.getElementById('rail-foot');
  if (!box) return;
  const s = state.status || {};
  const summary = s.summary || {};
  const rows = [];
  if (summary.equity !== null && summary.equity !== undefined) {
    rows.push(['净值', `¥${Number(summary.equity).toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`]);
  } else rows.push(['净值', '—']);
  if (summary.position_count !== null && summary.position_count !== undefined) {
    rows.push(['仓位', `${summary.position_count} / 3`]);
  }
  if (s.regime) rows.push(['状态', zh.regime(s.regime)]);
  if (s.day) rows.push(['数据日', ymd(s.day)]);
  box.replaceChildren(...rows.map(([k, v]) => h('div', { class: 'rail-stat' }, k, h('b', {}, v))));
}

async function hardRefresh() {
  const btn = document.querySelector('.topbar .btn-icon');
  btn?.classList.add('spin');
  // 刷新会重建 DOM（route 分支），滚动位置先存后还 —— 不然按一次 R 就回到页顶
  const keepTop = viewEl?.scrollTop ?? 0;
  await loadStatus({ fresh: true });
  if (pageMod?.onRefresh) {
    try { await pageMod.onRefresh(); } catch (err) { console.warn(err); }
  } else {
    await route();
  }
  if (viewEl) viewEl.scrollTop = keepTop;
  btn?.classList.remove('spin');
  toast('已重新取数', 'ok');
}

// ------------------------------------------------------------------ 启动
function bindGlobal() {
  if (bindingsDone) return;
  bindingsDone = true;

  window.addEventListener('hashchange', () => {
    if (parseRoute().key) sessionStorage.setItem('qd.manualNav', '1');
    route();
  });

  window.addEventListener('qd:auth-lost', () => {
    clearToken();
    // 停掉一切会往已卸载 DOM 写东西的东西
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    stopClock();
    pendingScroll = viewEl?.scrollTop ?? null;
    disposePage();
    shellReady = false;
    if (navEl) navEl = null;
    renderGate('口令失效了。重输后回到原处。');
  });

  document.addEventListener('keydown', (ev) => {
    const el = document.activeElement;
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el?.tagName) || el?.isContentEditable;
    if (document.documentElement.classList.contains('overlay-open')) return;  // 浮层里只管浮层
    // 搜索框写着 ⌘K，但这是 Windows 窗口——既没有 ⌘ 键，Ctrl+K 也从没绑过。
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'k' || ev.key === 'K')) {
      ev.preventDefault(); searchInput?.focus(); return;
    }
    if (ev.key === '/' && !typing) { ev.preventDefault(); searchInput?.focus(); return; }
    if (typing) return;
    if (ev.key === 'r' || ev.key === 'R') { ev.preventDefault(); hardRefresh(); return; }
    if (ev.key === 's' || ev.key === 'S') { ev.preventDefault(); openStock(); return; }
    if (ev.key === '?') { toast('1-6 切页 · s 个股 · / 搜索 · R 刷新 · Esc 关浮层', '', '快捷键'); return; }
    const idx = Number(ev.key);
    if (Number.isInteger(idx) && idx >= 1 && idx <= PAGES.length) navigate(PAGES[idx - 1].id);
  });
}

/**
 * 桌面窗口（pywebview）把口令放在 fragment 里注入：`#/today?t=...`。
 * 立刻从地址栏抹掉——口令不该留在历史里，也不该被截图带走。
 */
function consumeInjectedToken() {
  const raw = location.hash || '';
  const at = raw.indexOf('?');
  if (at < 0) return;
  const params = new URLSearchParams(raw.slice(at + 1));
  const token = params.get('t');
  if (!token) return;
  setToken(token);
  // 只摘掉口令，其余参数（`#/decide?id=t-xxx` 的 id）必须留着 ——
  // 整段丢弃会让"直达某张票"变成"回到列表"。
  params.delete('t');
  const base = raw.slice(0, at) || '#/today';
  const rest = params.toString();
  history.replaceState(null, '', location.pathname + location.search + (rest ? `${base}?${rest}` : base));
}

// 服务断开/恢复。桌面窗口在交易时段被重启、或机器休眠醒来，都会走到这里：
// 30 秒的软刷新太慢，断开时改为每 3 秒探一次，探通立刻重画当前页。
let offlineEl = null;
let reconnectTimer = null;
let offlineSince = 0;
let lastRefresh = 0;

window.addEventListener('qd:offline', () => {
  if (offlineEl) offlineEl.hidden = false;
  if (!offlineSince) offlineSince = Date.now();
  if (reconnectTimer) return;
  // 探 /health 而不是 /portfolio：后者一旦单独坏掉（某个数据集没落库之类），
  // 就会出现"探通 → 重画 → 又断"的 3 秒抖动，把人晃晕。
  reconnectTimer = setInterval(() => { probe('/health'); }, 3000);
});

window.addEventListener('qd:online', () => {
  if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
  const down = offlineSince ? Date.now() - offlineSince : 0;
  offlineSince = 0;
  if (!offlineEl || offlineEl.hidden) return;
  offlineEl.hidden = true;
  if (down > 4000) toast(`服务已恢复（断开约 ${Math.round(down / 1000)} 秒）`);
  // 重画也要有冷却：真抖动时不至于每 3 秒把页面重建一遍。
  if (Date.now() - lastRefresh > 10000) { lastRefresh = Date.now(); route(); }
});

let bootTries = 0;

async function boot() {
  consumeInjectedToken();
  if (!getToken()) { renderGate(); return; }
  const check = await probeWhy('/portfolio');
  if (!check.ok) {
    if (check.status === 401 || check.status === 403) { renderGate('口令没有通过。'); return; }
    bootTries += 1;
    if (bootTries <= 12) {
      renderWaiting(bootTries);
      setTimeout(() => { boot(); }, 1500);
      return;
    }
    renderUnavailable(check.code || String(check.status || ''));
    return;
  }
  bootTries = 0;
  const ok = check.data;
  if (!ok || ok.equity === undefined) { renderGate('口令没有通过。'); return; }

  if (!shellReady) { renderShell(); shellReady = true; }
  bindGlobal();
  startClock();

  await loadStatus();
  await route();

  // 30 秒一拍的软刷新：只更新状态栏与页面自己声明的 tick 钩子，不重排整页
  if (tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(async () => {
    // 浮层打开时不打断用户正在看的弹窗
    if (document.hidden || document.documentElement.classList.contains('overlay-open')) return;
    await loadStatus({ fresh: true });
    if (pageMod?.onTick) {
      try { await pageMod.onTick(); } catch (err) { console.warn(err); }
    }
  }, 30000);
}

boot();
