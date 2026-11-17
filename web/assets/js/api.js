// 唯一的数据入口。所有数字都从这里来，界面不自己编数字。
//
// 四条约定（对应内核的三条红线 + 本地时钟）：
// 1. token 只存在 localStorage，从不写进 HTML —— 服务端也不把口令注入页面。
// 2. 失败必须带 code 与 next，界面照原样显示，不把 500 说成"暂无数据"。
// 3. GET 结果做短缓存：切页签不重复打上游（上游有熔断，见 /market/quote/health）。
// 4. 缓存命中要告诉调用方"这是几点的数" —— 任何数字都必须能回答 as_of，
//    所以 api(..., {meta:true}) 返回 {data, at, fromCache}，at 是响应到达时刻。

const TOKEN_KEY = 'qd.token';
const CACHE = new Map();      // key -> { at, value, refreshing, retryAfter }
const INFLIGHT = new Map();   // key -> Promise<{data, at}>
const MAX_ENTRIES = 400;

export class ApiError extends Error {
  constructor(status, code, message, next, body, handled = false) {
    super(message || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.next = next;
    this.body = body;
    // 401 已经由外壳（口令闸门）统一处理过；caller 不要重复渲染/toast
    this.handled = handled;
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(value) {
  if (value) localStorage.setItem(TOKEN_KEY, value.trim());
  else localStorage.removeItem(TOKEN_KEY);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function authLost() {
  window.dispatchEvent(new CustomEvent('qd:auth-lost'));
}

/** 缓存键的规范化：`?sections=a,b` 与 `?sections=b,a` 必须是同一个键，
    否则个股档案每次换个顺序都会重新拉一遍（13 块 × 9–16KB）。 */
function cacheKey(path) {
  const [p, query] = String(path).split('?');
  if (!query) return p;
  const usp = new URLSearchParams(query);
  const sections = usp.get('sections');
  if (sections) {
    usp.set('sections', sections.split(',').map((s) => s.trim()).filter(Boolean).sort().join(','));
  }
  const s = usp.toString();
  return s ? `${p}?${s}` : p;
}

/** 会把上游打到熔断的查询，前端硬拦 —— 不靠调用方自觉。
    /market/pools?refresh=true 会真的去抓全市场；新鲜度是 snapshot_intraday.py 的职责。 */
function refuseDangerous(path) {
  if (!/^\/market\/pools(\?|$)/.test(path)) return;
  const usp = new URLSearchParams(String(path).split('?')[1] || '');
  const v = usp.get('refresh') || usp.get('force_refresh');
  if (v && v !== '0' && v !== 'false') {
    throw new ApiError(0, 'REFRESH_REFUSED',
      '前端不允许触发全市场抓取：会打到上游熔断。',
      '刷新是数据管道的职责（scripts/snapshot_intraday.py --loop 60）；界面只显示 age_seconds。',
      null, true);
  }
}

// 服务死活只有两种判据：HTTP 有响应（任何状态码）就是活着；传输失败才是死了。
// 只在翻转时派事件——离线时每个请求都失败，逐条冒泡会把界面刷成瀑布。
let serviceOnline = true;
function noteAlive(alive) {
  if (alive === serviceOnline) return;
  serviceOnline = alive;
  window.dispatchEvent(new CustomEvent(alive ? 'qd:online' : 'qd:offline'));
}

function netError(err) {
  const aborted = err?.name === 'AbortError' || err?.name === 'TimeoutError';
  return new ApiError(0, 'NETWORK', aborted ? '请求超时' : '服务未响应',
    '确认服务在 127.0.0.1:8811（计划任务 quant-desk-service）。');
}

/** 单次请求。返回 {data, at}；at 是响应到达时刻，不是请求发出时刻。 */
async function raw(path, { method = 'GET', body, timeout = 20000, silent = false, idempotencyKey = null } = {}) {
  refuseDangerous(path);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  const headers = { 'X-Quant-Token': getToken() };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;

  try {
    const res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
    });
    noteAlive(true);   // 哪怕 401/500，服务至少还在这儿
    // body 读取必须在同一个 try 里：超时 abort 发生在读 body 阶段时，
    // 会把 AbortError 抛成一个裸 TypeError，页面只会显示 undefined。
    let text;
    try {
      text = await res.text();
    } catch (err) {
      if (silent) return null;
      throw netError(err);
    }
    const at = Date.now();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

    if (!res.ok) {
      if (res.status === 401 || res.status === 403) {
        // handled: 外壳会挂口令闸门；caller 拿到它只应中止，不应再渲染或 toast
        authLost();
        throw new ApiError(res.status, 'UNAUTHORIZED', '口令无效或缺失（X-Quant-Token）',
          '改 .env 里的 QUANT_TOKEN 后重启服务，不要重试。', data, true);
      }
      const d = data?.detail;
      const code = (d && typeof d === 'object' ? d.code : null) || data?.code || `HTTP_${res.status}`;
      const message = (d && typeof d === 'object' ? d.message || d.msg : d) || data?.error || String(text).slice(0, 300);
      throw new ApiError(res.status, code, typeof message === 'string' ? message : JSON.stringify(message),
        data?.next || null, data, false);
    }
    return { data, at };
  } catch (err) {
    if (!(err instanceof ApiError)) noteAlive(false);   // 超时/连接被拒，不是服务在报错
    if (silent) return null;
    throw err instanceof ApiError ? err : netError(err);
  } finally {
    clearTimeout(timer);
  }
}

function remember(key, r) {
  if (!r) return;
  CACHE.set(key, { at: r.at, value: r.data, refreshing: false, retryAfter: 0 });
  if (CACHE.size > MAX_ENTRIES) {
    let oldestKey = null;
    let oldestAt = Infinity;
    for (const [k, v] of CACHE) if (v.at < oldestAt) { oldestAt = v.at; oldestKey = k; }
    if (oldestKey) CACHE.delete(oldestKey);
  }
}

function shape(value, at, fromCache, meta) {
  // 叫 fromCache 不叫 stale：业务 staleness（帧 >5 分钟 / 日线非当日 / profile 非当日）
  // 由各面板自己按阈值算，这里只回答"这次给的是不是缓存里的值"。
  return meta ? { data: value, at, fromCache } : value;
}

/**
 * GET with a short TTL. `ttl=0` bypasses the cache.
 * `meta:true` → {data, at, fromCache}，用于渲染 as_of。
 */
export async function api(path, { ttl = 15000, force = false, silent = false, meta = false } = {}) {
  refuseDangerous(path);
  const key = cacheKey(path);
  const now = Date.now();
  const hit = CACHE.get(key);

  if (!force && hit && now - hit.at < ttl) {
    return shape(hit.value, hit.at, false, meta);
  }

  // 过期但还能用：先返回旧的，同时后台刷新。
  // 三个闸门缺一不可 —— 否则上游熔断期间会"每次调用都再打一次"，正是最不该做的。
  if (!force && hit && now - hit.at < ttl + 30000) {
    const cooling = hit.refreshing || hit.retryAfter > now;
    if (!cooling) {
      hit.refreshing = true;
      raw(path, { silent: true })
        .then((r) => { if (r) remember(key, r); else hit.retryAfter = Date.now() + ttl; })
        .catch(() => { hit.retryAfter = Date.now() + ttl; })
        .finally(() => { hit.refreshing = false; });
    }
    return shape(hit.value, hit.at, true, meta);
  }

  // 同一 path 并发去重：5 个面板同时要 /dashboard.json 只打一次上游
  const running = INFLIGHT.get(key);
  if (running && !force) {
    const r = await running.catch(() => null);
    if (r) return shape(r.data, r.at, false, meta);
  }

  const p = raw(path, { silent }).then((r) => { remember(key, r); return r; });
  INFLIGHT.set(key, p);
  try {
    const r = await p;
    return shape(r?.data, r?.at, false, meta);
  } finally {
    INFLIGHT.delete(key);
  }
}

/** 写操作。idempotencyKey 由调用方生成并存 sessionStorage：超时重试必须原样重发，
    否则 409 会变成两张票（指纹含 thesis 自由文本，换个说法就是第二张）。 */
const WRITE_AFFECTS = [
  [/^\/decision\/propose$/, ['/tickets', '/decision', '/portfolio', '/risk', '/dashboard.json']],
  [/^\/(decision|tickets)\/[^/]+\/(approve|reject)$/, ['/tickets', '/decision', '/portfolio', '/risk', '/dashboard.json']],
  [/^\/portfolio\/(close|sell)$/, ['/portfolio', '/tickets', '/risk', '/dashboard.json']],
  [/^\/journal/, ['/journal', '/dashboard.json']],
];

export async function post(path, body, { timeout = 20000, idempotencyKey = null } = {}) {
  const r = await raw(path, { method: 'POST', body, timeout, idempotencyKey });
  // 只清受影响的资源，不要把 /health、/data/status 一起清掉
  const hit = WRITE_AFFECTS.find(([re]) => re.test(path));
  for (const prefix of (hit ? hit[1] : [])) invalidate(prefix);
  INFLIGHT.clear();
  return r?.data;
}

export function invalidate(prefix = '') {
  for (const key of [...CACHE.keys()]) if (key.startsWith(prefix)) CACHE.delete(key);
}

export function probe(path) {
  return raw(path, { silent: true }).then((r) => (r ? r.data : null));
}

/**
 * 像 probe，但把失败的原因也带回来：`{ok, data, status, code, message}`。
 *
 * `probe` 把所有失败吞成 null，于是"服务还没起来"和"口令不对"在界面上长得一样。
 * 桌面窗口会自己拉起服务（5–15 秒），那段时间把用户推去改口令，是把他送到一个
 * 没错的配置面前。
 */
export async function probeWhy(path) {
  try {
    const r = await raw(path, { silent: false });
    return { ok: true, data: r ? r.data : null, status: 200, code: null, message: '' };
  } catch (err) {
    return {
      ok: false, data: null,
      status: Number(err && err.status) || 0,
      code: (err && err.code) || 'NETWORK',
      message: (err && err.message) || String(err),
    };
  }
}

/** 常用查询串拼装，空值不写进 URL（空串在 FastAPI 里是合法值，会改变语义）。 */
export function qs(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '' || Number.isNaN(v)) continue;
    if (k === 'sections' && Array.isArray(v)) usp.set(k, [...v].sort().join(','));
    else usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : '';
}
