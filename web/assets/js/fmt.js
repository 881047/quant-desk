import { unitsFor } from './contract.units.js';

// 数字的写法只有一份。中文语境 + A 股口径：
//   涨=红、跌=绿（与欧美相反）；金额用 ¥ 与 万/亿；百分比永远带符号。
// 表格里的数字一律 tabular-nums，右对齐，不要千分位与空格混排。

const nf = new Intl.NumberFormat('zh-CN');
const nf2 = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** HTML 转义：所有 innerHTML 模板里的动态值必须过一遍 ——
    行业名、公司名里出现 `<` 或 `&` 不是假想，是数据。 */
export function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const v = Number(value);
  return digits === 0 ? nf.format(v) : v.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

export function price(value, digits) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  const d = digits ?? (v >= 1000 ? 2 : 2);
  return nf2.format(v).replace(/\.00$/, d === 0 ? '' : '.00');
}

/** 固定两位的价格（表格与 K 线读数用）。null/'' 一律 '—'，不做 Number(null)=0 兜底。 */
export function fp2(value) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(2);
}

export function money(value, { sign = false } = {}) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  const s = Math.abs(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const mark = v < 0 ? '−' : (sign && v > 0 ? '+' : '');
  return `${mark}¥${s}`;
}

/** 大额人民币：一位小数，单位换到 万 / 亿。0 与 null 是两件事。 */
export function cny(value, { unit = true } = {}) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  const sign = v < 0 ? '−' : '';
  if (!unit) return `${sign}${nf.format(Math.round(a))}`;
  if (a >= 1e12) return `${sign}${(a / 1e12).toFixed(2)} 万亿`;
  if (a >= 1e8) return `${sign}${(a / 1e8).toFixed(2)} 亿`;
  if (a >= 1e4) return `${sign}${(a / 1e4).toFixed(1)} 万`;
  return `${sign}${nf.format(Math.round(a))}`;
}

/** ratio 入（0.0146 → +1.46%）。 */
export function pct(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(digits)}%`;
}

/** 已经是百分点（1.4 → +1.40%）。 */
export function pctPts(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}%`;
}

/**
 * 后端 ``*_pct`` 字段专用：值已经是百分点（5.11 → "5.110%"），不加符号、不再 ×100。
 * 量纲由接口的 ``units`` 声明；前端不必再为每个字段记住要不要换算。
 */
export function pctPlain(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return `${v.toFixed(digits)}%`;
}

export function ratio(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

export function signed(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—';
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}`;
}

/** A 股涨跌色（CSS class）。0 是平盘，不是涨。 */
export function tone(value) {
  const v = Number(value);
  if (!Number.isFinite(v) || v === 0) return 'flat';
  return v > 0 ? 'up' : 'down';
}

export function toneStyle(value) {
  // 用令牌不用常量：浅色皮肤/令牌调整时这里自动跟随。
  // 原来引用的 --fg-dim 是旧令牌名，早就不存在了——中性色一直没生效过。
  const t = tone(value);
  return t === 'up' ? 'color:var(--px-up)' : t === 'down' ? 'color:var(--px-down)' : 'color:var(--ink-dim)';
}

export function ymd(value) {
  const s = String(value ?? '');
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  return s.slice(0, 10);
}

export function md(value) {
  const s = ymd(value);
  return s.length >= 10 ? s.slice(5) : s;
}

export function hhmmss(iso) {
  if (!iso) return '—';
  const s = String(iso);
  const m = s.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : s.slice(11, 19);
}

/** 本机时钟 HH:MM:SS。状态条上的「此刻」是本机时间，不是 UTC。

    不要用 `hhmmss(new Date().toISOString())`：那个函数只截字符串里的 T 段，
    toISOString() 给的是 UTC，北京时间会整整差 8 小时（12:16 显示成 04:16）。
    服务端时间戳自带 +08:00 偏移，T 段就是本地时间，所以只有「现在几点」需要这个。
 */
export function hhmmssOf(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 本机日期 YYYYMMDD。账外页签的默认交易日用它。

    `new Date().toISOString().slice(0, 10)` 取的是 UTC 日：北京时间 00:00–08:00
    会默认成**昨天**，早盘录单最容易踩。
 */
export function todayYmd(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}`;
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const s = Math.abs(Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

/** 第一行非空文本 —— 日志/论点正文常以空行或缩进开头，直接取首行会得到空串。 */
export function firstLine(text) {
  return String(text || '').split('\n').map((t) => t.trim()).find(Boolean) || '';
}

// ------------------------------------------------------------ 量纲驱动的格式
const warnedUnits = new Set();

/**
 * 数值显示的唯一入口：由后端响应的 `units` 决定要不要 ×100。
 *
 * 曾经每个页面各自记住"这个字段是小数还是百分数"，一次换错函数就是 100 倍
 * 的数（docs/14 A1 的 1000%）。units 来自接口顶层；老接口没带时按 endpoint
 * 从生成物 unitsFor() 取同一份声明。`daily_pnl_pct` 这类名字带 _pct 却是
 * 小数的例外也在表里 —— 按名字猜必然出错。
 *
 * 字段没声明时不猜换算：返回破折号并只警告一次。宁可少显示一个数，也不要
 * 显示一个看起来完全合理的错数。
 */
export function fmtByUnits(units, field, value, digits = 2, opts = {}) {
  const { endpoint = null, signed = true } = opts;
  const declared = units || (endpoint ? unitsFor(endpoint) : null);
  const scale = declared?.[field];
  if (!scale) {
    if (!warnedUnits.has(`${endpoint || '?'}:${field}`)) {
      warnedUnits.add(`${endpoint || '?'}:${field}`);
      console.warn(`[units] ${endpoint || '?'} 未声明 ${field} 的量纲，拒绝猜换算`);
    }
    return '—';
  }
  if (scale.startsWith('fraction')) return signed ? pct(value, digits) : ratio(value, digits);
  if (scale.startsWith('percent')) return signed ? pctPts(value, digits) : pctPlain(value, digits);
  if (scale === 'cny') return cny(value);
  if (scale.startsWith('count') || scale.startsWith('ordinal') || scale.startsWith('index')) {
    return value === null || value === undefined ? '—' : String(value);
  }
  return '—';
}
