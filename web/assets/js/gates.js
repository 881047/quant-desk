// 闸门：四态，只用形状，不用颜色。
//
// 视觉组第二轮的口径：结果通道（过/不过/无结论）与数据可信度（有/旧/缺）是两条
// 正交通道，**不许用颜色区分结论**——否则"闸门不过"会和"股价下跌"抢同一个绿。
// 过   = 1px 空心方（灰）：正常态不占墨，25 格里 23 格是"过"，空心才看得见实心；
// 不过 = 实心方 + 600 字重（8px 格子里 1px 描边抗锯齿后会消失，不能靠描边分等级）；
// 无结论 = 虚线方，且不给底色；
// 缺数据 = 灰点线方（并入数据可信度一族，见 --al-missing-ink）。
// 尺寸：8×8 = 判定标记；6×6 留给涨停/跌停，两个通道不混用。

import { h, chip } from './ui.js';
import { zh } from './zh.js';

function verdictSquare(kind) {
  return h('span', { class: `verdict ${kind}`, 'aria-hidden': 'true' });
}

/** 一条闸门。kind: pass | fail | unknown | none。 */
function verdictRow(label, kind, note = null) {
  const k = kind === 'unknown' ? 'none' : kind;
  // 词表统一成一个词：「未运行」。它报的是"检查没有执行"这个事实 ——
  // 本台报事实不报结论，也不替读者下判断（另一个说法在语感上离「不过」太近，
  // 容易被读成"判了但不算"，已废弃）。
  const map = { pass: '过', fail: '不过', none: '未运行' };
  return h('div', { class: `verdict-row ${k}`, title: note || '' },
    k === 'none' ? null : verdictSquare(k),
    h('span', { class: 'verdict-label' }, label),
    h('span', { class: 'faint micro' }, map[k] || ''),
    note ? h('span', { class: 'micro faint', style: { marginLeft: 'auto' } }, note) : null);
}

/**
 * 三池闸门块。ok 是三态：true / false / null。
 * null 不许渲染成"不过"——那是"没得判"，补救办法不同（缺数要补管道，不过要等结构）。
 */
export function gatesBlock(g) {
  if (!g) return h('div', { class: 'micro faint' }, '没有闸门数据');
  const checks = g.checks || {};
  const indeterminate = new Set(g.indeterminate || []);
  const failed = new Set(g.failed || []);
  const rows = Object.entries(checks).map(([key, value]) => {
    const kind = indeterminate.has(key) ? 'unknown'
      : failed.has(key) || value === false ? 'fail'
        : value === null || value === undefined ? 'none' : 'pass';
    // 原始键名只进 title（对代码用），不上屏 —— 右侧那列是给眼睛看的，不是给 grep 看的。
    return verdictRow(zh.gate(key), kind);
  });

  const stateKind = g.ok === true ? 'pass' : g.ok === false ? 'fail' : 'unknown';
  const stateText = g.ok === true ? '闸门通过' : g.ok === false ? '闸门不过' : '无法判断（缺数，不是不过）';

  return h('div', { class: 'stack', style: { gap: '6px' } },
    h('div', { class: 'verdict-row' },
      stateKind === 'unknown' ? null : verdictSquare(stateKind),
      h('span', { class: 'verdict-label', style: { fontWeight: 600, color: 'var(--ink)' } }, stateText),
      g.phase ? chip(`phase ${g.phase}`, 'off') : null),
    h('div', { class: 'stack', style: { gap: '2px', paddingLeft: '15px' } }, rows),
    g.note && g.ok === null
      ? h('div', { class: 'micro', style: { color: 'var(--al-missing-ink)', lineHeight: '1.6' } }, g.note)
      : null,
    // borderline：同一条闸门两套口径给出两个答案。并列两行，判定留空（虚线方），
    // 界面不替用户仲裁 —— 仲裁是人的职责，不是渲染层的。
    Array.isArray(g.borderline) && g.borderline.length
      ? h('div', { class: 'stack', style: { gap: '2px', borderTop: '1px solid var(--line-soft)', paddingTop: '5px', marginTop: '3px' } },
        h('div', { class: 'micro', style: { color: 'var(--al-stale)' } }, '同一闸门两套口径 · 不做仲裁'),
        g.borderline.map((b) => h('div', { class: 'verdict-row none' },
          null,
          h('span', { class: 'verdict-label' }, `${b.metric || b.name || '—'}：派生 ${b.derived ?? '—'} / 厂商 ${b.vendor ?? '—'}`),
          h('span', { class: 'micro faint', style: { marginLeft: 'auto' } }, b.verdict || b.note || '两说'))))
      : null);
}

/**
 * 数据可信度：有 / 旧 / 缺 / 坏。与结论无关，单独一条通道。
 * 用 class 表达，不拼内联 style —— 这样这四种状态能进 CSS 被审，
 * 也不会出现"变量改了名字、内联样式还留着旧写法"。
 * stale / fault 复用已有的 .chip.warn / .chip.bad，不新开语义重复的名字。
 */
const BADGE_CLASS = { live: 'live', stale: 'warn', missing: 'missing', fault: 'bad' };

export function dataBadge(state, text) {
  const map = {
    live: '有',
    stale: '旧',
    missing: '缺',
    fault: '坏',
  };
  const key = BADGE_CLASS[state] ? state : 'missing';
  const label = map[key];
  return h('span', {
    class: `chip ${BADGE_CLASS[key]}`,
    title: text || label,
  }, `${label}${text ? ` · ${text}` : ''}`);
}
