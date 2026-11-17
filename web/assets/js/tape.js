// 行情带：顶栏下的一条指数横带。
//
// 为什么要有：这是交易台"像不像终端"的第一眼信号——彭博、路透、任何交易席位的
// 顶上都有一条这样的带子。这里只放真的数：沪深四条主指数，其它什么都不放。
//
// 三条约定：
// 1. 不转圈、不闪：盘后就是收盘值，涨跌用全站唯一那对红绿，不做动效。
// 2. 取不到就整条隐藏，不留一条空带子占 26px。
// 3. 60 秒缓存 —— 它和个股页共用 /market/quote，切页签不重复打上游。
// 4. 不自己起定时器：跟着外壳那 30 秒一拍走（refreshTape），否则每次重建外壳
//    都会多出一个 setInterval。

import { api } from './api.js';
import { h } from './ui.js';
import { price, pctPts, tone } from './fmt.js';

const INDICES = [
  ['000001.SH', '上证'],
  ['399001.SZ', '深成'],
  ['399006.SZ', '创业板'],
  ['000300.SH', '沪深300'],
];

let current = null;   // 当前这条带的 reload 函数

export function tickerBar() {
  const el = h('div', { class: 'tape', role: 'list', 'aria-label': '指数行情' });

  const load = async () => {
    let data = null;
    try {
      data = await api(`/market/quote?symbols=${INDICES.map((i) => i[0]).join(',')}`, {
        ttl: 60000, silent: true,
      });
    } catch {
      data = null;   // 上游挂了不是新闻，下一拍再试；带子保持原样
    }
    const rows = data?.quotes || [];
    if (!rows.length) {
      if (!el.childElementCount) el.hidden = true;   // 从没成功过才整条收起
      return;
    }

    const byCode = new Map(rows.map((q) => [q.symbol, q]));
    const cells = [];
    for (const [code, label] of INDICES) {
      const q = byCode.get(code);
      if (!q || q.last === null || q.last === undefined) continue;
      const t = tone(q.pct_chg);
      cells.push(h('div', { class: 'tape-item', role: 'listitem', title: `${q.name || label} · ${code}` },
        h('span', { class: 'tape-name' }, label),
        h('span', { class: 'tape-last num' }, price(q.last, 2)),
        h('span', { class: `tape-chg num ${t}` }, pctPts(q.pct_chg, 2))));
    }
    if (!cells.length) return;

    const session = data.session || {};
    const tag = session.in_session ? '盘中' : session.trading_day ? '已收盘' : (session.label || '休市');
    const day = String(data.as_of || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3');
    el.replaceChildren(
      h('span', { class: 'tape-cap' }, '指数'),
      ...cells,
      h('span', { class: 'spacer' }),
      h('span', { class: 'tape-session' }, `${tag} · ${day}`));
    el.hidden = false;
  };

  current = load;
  load();
  return el;
}

/** 外壳每次重取状态时顺手刷一次；没有带子时是空操作。 */
export function refreshTape() {
  if (current) current();
}
