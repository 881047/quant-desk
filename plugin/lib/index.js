/**
 * quant-desk — host-agnostic tool library.
 *
 * 73 internal handlers are consumed through 4 public tools by two
 * hosts: the DSH plugin loader (`ctx.tools.register`) and the MCP server in
 * `../mcp/server.mjs`. Neither host owns trading logic — this file remains a
 * thin client over the local Python service, which owns the books and audit log.
 *
 * Tools are grouped by the question they answer:
 *   read  : context / data health / quote / bars / pools / screen / portfolio / risk
 *   act   : propose -> approve | reject
 *   audit : ticket / orders / journal / playbook / lineage
 */

import { randomUUID } from 'node:crypto'
import { buildFacadeTools, PUBLIC_TOOL_NAMES, resolveFacadeOperation } from './facade.js'
import { UNITS_TEXT } from './contract.units.js'

let defineTool
try {
  ({ defineTool } = await import('@deepseek-ai/dsh-tools'))
} catch (error) {
  // Allows the plugin to be loaded in a plain Node test harness. In DSH the
  // real SDK is always present; the warning makes a broken install visible.
  console.warn('[quant-desk] @deepseek-ai/dsh-tools unavailable, using pass-through defineTool')
  defineTool = (spec) => spec
}

export const name = 'quant-desk'
export const inject = ['systemPrompt', 'tools']

// The token the service ships with when `QUANT_TOKEN` is unset. Exported so the
// MCP adapter imports it instead of keeping a second literal: two copies means
// rotating one leaves the other silently 401-ing.
export const DEFAULT_TOKEN = 'change-me-local-only'

// Every number the plugin needs to hold in its head, in one place.
//
// These are not server-side policy (that lives in configs/params.yaml); they are
// transport limits of the DSH host and of the HTTP client. They are named here
// because a bare `16000` inside a clamp reads like a row count, and because the
// same ceiling was previously implied by three different literals.
const LIMITS = {
  // The host drops any tool result larger than this **whole** rather than
  // truncating it, so an oversized response is indistinguishable from a tool
  // that returned nothing. Measured against the host, not guessed.
  //
  // Enforced in **UTF-8 bytes** (Buffer.byteLength). It used to be compared to
  // `text.length`, which counts UTF-16 code units: a Chinese payload of 14,516
  // "characters" is 17,574 bytes, passed the check, and was then dropped by the
  // host while this file reported `truncated:false`. Measured on 2026-10-31
  // over 8 symbols, 7 of 8 `quant_profile` responses crossed the byte ceiling
  // while only 1 crossed the character one.
  maxOutputBytes: 16000,
  // Raise to 10 MB only if a body is really that big: it is buffered in memory.
  maxResponseBytes: 1024 * 1024,
  maxResponseCeiling: 10 * 1024 * 1024,
  // A request timeout, not a request budget. The service answers a heavy read
  // in ~1.4 s when the box is idle, but the endpoint is a synchronous CPU-bound
  // computation in a single-process GIL server: ten concurrent `/discover`
  // calls each took 21.7 s (measured 2026-11-02), i.e. the whole fleet serialises
  // behind the slowest one. Under CPU contention — a test run, the daily job —
  // a single `/discover` was measured at 17.6 s.
  //
  // A 20 s ceiling turned that into "quant service unreachable", which is a
  // lie: the service was up and answering. The skill tells the model that an
  // unreachable service means "stop and look at the service", so a false
  // unreachable sends it down the wrong path. The ceiling is now 60 s, and the
  // error text names a timeout as a timeout.
  timeoutMs: 60000,
  timeoutCeiling: 300000,
  // Per-tool row clamps. Row width differs by tool, so the ceiling that keeps a
  // body under maxOutputBytes differs too; the comments carry the measurement.
  rows: {
    // 20 rows of a pool ≈ 14 KB, 25 ≈ 17 KB — 20 is the last safe step.
    pools: { min: 1, max: 20, fallback: 15 },
    // Screen rows are narrower (one line of facts per name).
    screen: { min: 1, max: 20, fallback: 8 },
    // Universe rows carry turnover/valuation/industry/board/band: 40 rows =
    // 14,047 B measured 2026-11-01, against 13,527 B before board+band — 13 B
    // a row, which is why the ceiling stayed at 40 and not 45.
    universe: { min: 1, max: 40, fallback: 30 },
    // Memo rows are prose summaries; 20 is roughly 6 KB.
    memo: { min: 1, max: 50, fallback: 20 },
    // Audit rows. These four used to pass the caller's `limit` through to an
    // endpoint that accepts `le=200`; `/decisions?limit=200` alone is 18 KB, so
    // "ask for more rows" produced an error envelope instead of more rows.
    orders: { min: 1, max: 80, fallback: 50 },
    cases: { min: 1, max: 50, fallback: 20 },
    lhb: { min: 1, max: 40, fallback: 20 },
    knowledge: { min: 1, max: 40, fallback: 20 },
    // Brief ticket rows measured at 967 B each on 2026-11-04 (a full ticket is
    // 2.8–3.8 KB, which is why the route stops sending the guard evidence in a
    // list). 15 rows ≈ 14.5 KB — the last safe step before the 16 KB ceiling;
    // past it the rows-kind ladder starts cutting prose instead of dropping rows.
    tickets: { min: 1, max: 15, fallback: 10 },
    // Memory rows are journal summaries (head+tail, ≤300 chars) plus the newest
    // few in full: measured 2026-11-04 at 12 rows / recent=3 = 12,078 B, while
    // 20 rows / recent=0 = 16,825 B and 50 rows = 35,441 B. The old ceiling was
    // the route's `le=50`, i.e. asking for 20 — a page size the model had no
    // reason to think was special — returned nothing at all.
    memory: { min: 1, max: 12, fallback: 10 },
    // `full=true` keeps every row's whole text: 8 rows measured 9,739 B, and the
    // longest reviews are ~2 KB each, so 6 is the last step that stays under the
    // ceiling once entries are longer than today's average.
    memoryFull: { min: 1, max: 6, fallback: 6 },
    // Hypothesis rows carry a statement; `full` adds a ~2 KB test_spec and the
    // result blob. Measured 20 rows with full=true = 10,638 B (the route allows
    // 200, which is ~104 KB); summaries are ~130 B a row.
    hypotheses: { min: 1, max: 60, fallback: 20 },
    hypothesesFull: { min: 1, max: 20, fallback: 20 },
    // TCA rows are ten small fields, 212 B each by construction; the envelope
    // (by_basis / by_side / caveats) is ~455 B. 60 rows ≈ 13.2 KB. The route's
    // own default is 200 ≈ 42 KB, past the ceiling it does not know about.
    tca: { min: 1, max: 60, fallback: 60 },
    // Auction alerts: 40 rows per side measured 13,547 B (≈315 B a row) on
    // 2026-11-04; the route accepts `le=200` ≈ 68 KB.
    auction: { min: 1, max: 40, fallback: 10 },
    // Anomaly rows carry a machine-written Chinese paragraph: 300750.SZ on
    // 20261103 was 1,700 B for a single item (the whole-market page averages
    // ~700 B a row). 8 is the last step that stays under 16 KB in the worst case.
    anomaly: { min: 1, max: 8, fallback: 8 },
    shortAlphaEvidence: { min: 1, max: 4, fallback: 2 },
    shortAlphaTracking: { min: 1, max: 12, fallback: 4 },
  },
}
const DEFAULT_MAX_OUTPUT = LIMITS.maxOutputBytes

// Where the service listens. Overridable so a second desk (another port, another
// machine on the loopback alias) needs no code edit; allowRemote still gates a
// non-loopback host.
const DEFAULTS = {
  baseUrl: process.env.QUANT_BASE_URL || 'http://127.0.0.1:8811',
  token: '',
}

/**
 * Clamp a caller-supplied row count into a tool's safe range.
 *
 * The alternative — trusting the argument — produces an empty response, not an
 * error: the host discards an oversized body whole, so "asked for 200 rows" and
 * "the service has nothing" look identical from the model's side.
 */
export function clampRows(value, { min, max, fallback }) {
  const parsed = parseInt(value, 10)
  // 0 and "not a number" mean "not specified" and take the default. Clamping 0
  // up to 1 would answer a question about *this many rows* with one row, and the
  // model would read that as "the desk has one row" rather than "you asked
  // badly". Out-of-range positives are clamped instead: 200 rows of a pool is a
  // request the transport cannot carry, and saying so by returning the max is
  // better than returning nothing.
  if (!Number.isFinite(parsed) || parsed === 0) return fallback
  return Math.min(Math.max(parsed, min), max)
}

/**
 * Clamp a page size, send the request, and say when the page came back shorter.
 *
 * `clampRows` alone is silent, and a silent clamp reads as "this is the whole
 * list": `quant_auction(limit=200)` used to be answered with 40 alerts and
 * nothing said which 40. The note is one key on the envelope (`capped`), the
 * same shape `quant_journal_search` already uses, so a cut page is visible
 * without the model having to memorise every ceiling.
 */
export async function paged(askedRaw, spec, send, why) {
  const asked = Number(askedRaw)
  const limit = clampRows(askedRaw, spec)
  const data = await send(limit)
  if (Number.isFinite(asked) && asked > limit
      && data && typeof data === 'object' && !Array.isArray(data) && !data.capped) {
    data.capped = { asked_limit: asked, served_limit: limit, why }
  }
  return data
}

export function integerOption(value, fallback, { name, min, max }) {
  const parsed = value === undefined || value === null || value === ''
    ? fallback
    : Number(value)
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${name} must be a finite integer between ${min} and ${max}`)
  }
  return parsed
}

function validateServiceUrl(value, allowRemote) {
  const url = value instanceof URL ? value : new URL(value)
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error('quant service baseUrl must use http or https')
  }
  if (url.username || url.password) {
    throw new Error('quant service baseUrl must not contain userinfo')
  }
  const host = url.hostname.toLowerCase()
  const loopback = host === 'localhost' || host === '::1' || host === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(host)
  if (!loopback && !allowRemote) {
    throw new Error('remote quant service requires explicit allowRemote=true (or QUANT_ALLOW_REMOTE=1)')
  }
  return url
}

async function readResponseText(response, maxBytes) {
  const declared = Number(response.headers?.get?.('content-length'))
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body?.cancel().catch(() => {})
    throw new Error(`quant service response exceeds ${maxBytes} bytes`)
  }
  if (!response.body) return ''

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let bytes = 0
  let text = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      bytes += value.byteLength
      if (bytes > maxBytes) {
        await reader.cancel().catch(() => {})
        throw new Error(`quant service response exceeds ${maxBytes} bytes`)
      }
      text += decoder.decode(value, { stream: true })
    }
    return text + decoder.decode()
  } finally {
    reader.releaseLock()
  }
}

// ---------------------------------------------------------------- degradation
//
// A tool answer that does not fit used to be replaced whole by an error object.
// From the model's side that is indistinguishable from "this desk knows
// nothing", and it is the wrong trade: `quant_profile 300750.SZ` is 16,795
// compact bytes against a 16,000 ceiling — 5% over, and the reader lost 100% of
// the answer.
//
// So: shrink before giving up. Three deterministic levels, each strictly larger
// in what it cuts than the last. Every level keeps the **shape** (same keys,
// same types), so a caller that reads `valuation.pe` never has to care whether
// the answer was shrunk — it only gets fewer rows and shorter prose. The level
// and what it did are reported in `_shrink`, so "the excerpt stopped mid-word"
// is visible rather than inferred.
// 两套阶梯，区别只在**先牺牲什么**。
//
// 默认（文字类工具：journal / memo / filing）：行不是主体，先把长文本压短，
// 再考虑丢行。
//
// 表格类工具（pools / screen / limit_structure）：**行就是数据，文字是装饰**。
// 2026-11-04 实测 `quant_pools`：limit=20 的响应 16,946 B，旧的阶梯一上来就把
// 每池 20 行砍到 12 行——问 20 行只得 12 行，问 16 行却得 16 行，「问得越多给得
// 越少」。把字符串压到 120 字是 15,952 B，20 行全部留下。
export const SHRINK_LEVELS = {
  text: [
    { maxString: 240, maxArray: 12 },
    { maxString: 120, maxArray: 6 },
    { maxString: 60, maxArray: 2 },
  ],
  rows: [
    { maxString: 200, maxArray: 20 },   // 只压长文，行全留
    { maxString: 120, maxArray: 18 },   // 实测 limit=20：16,946 → 15,004 B
    { maxString: 120, maxArray: 12 },
    { maxString: 60, maxArray: 6 },
  ],
}

//: 判断一串对象是不是"按时间升序的序列"。只看首尾两行的同一个键，不做排序假设。
const SERIES_KEYS = ['trade_date', 'trade_time', 'ts', 'date', 'day', 'as_of']

function isAscendingSeries(rows) {
  if (!Array.isArray(rows) || rows.length < 2) return false
  const head = rows[0]
  const tail = rows[rows.length - 1]
  if (!head || !tail || typeof head !== 'object' || typeof tail !== 'object') return false
  for (const key of SERIES_KEYS) {
    const a = head[key]
    const b = tail[key]
    if (typeof a === 'string' && typeof b === 'string' && a !== b) {
      if (a.length >= 8 && b.length >= 8 && a < b && /^\d/.test(a) && /^\d/.test(b)) return true
    }
  }
  return false
}

function shrinkValue(value, level, report) {
  if (typeof value === 'string') {
    if (value.length <= level.maxString) return value
    report.strings += 1
    return `${value.slice(0, level.maxString)}…`
  }
  if (Array.isArray(value)) {
    // 时间序列留**末尾**：`quant_bars(days=250)` 被截到 12 行时，返回的是最早那 12 天，
    // 最近的行情全部消失，工具事实上不可用（2026-11-02 实测）。榜单类留头部才对
    // （涨停池按连板高度降序），所以按"这串是不是按时间升序"来分。
    const tail = isAscendingSeries(value)
    const kept = (tail ? value.slice(-level.maxArray) : value.slice(0, level.maxArray))
      .map((row) => shrinkValue(row, level, report))
    if (value.length > level.maxArray) {
      report.rows += value.length - level.maxArray
      if (tail) report.tail = true
    }
    return kept
  }
  if (value && typeof value === 'object') {
    const out = {}
    for (const [key, row] of Object.entries(value)) out[key] = shrinkValue(row, level, report)
    return out
  }
  return value
}

export function shrinkJson(value, levelIndex, ladder = SHRINK_LEVELS.text) {
  const level = ladder[Math.max(0, Math.min(levelIndex, ladder.length - 1))]
  const report = { strings: 0, rows: 0 }
  const body = shrinkValue(value, level, report)
  if (!report.strings && !report.rows) return null
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    body._shrink = {
      level: levelIndex + 1,
      strings_cut_at: level.maxString,
      rows_cut_at: level.maxArray,
      strings_shortened: report.strings,
      rows_dropped: report.rows,
    }
    if (report.tail) {
      // 说清楚砍的是头还是尾：时间序列砍头之后，第一行不是序列的起点。
      body._shrink.kept = 'tail'
      body._shrink.note = '时间序列按末尾保留（最近的在前一屏）；被丢掉的是更早的行'
    }
  }
  return body
}

export function formatJsonOutput(value, maxOutput = DEFAULT_MAX_OUTPUT,
                                ladder = SHRINK_LEVELS.text) {
  const limit = integerOption(maxOutput, DEFAULT_MAX_OUTPUT, {
    name: 'maxOutput', min: 128, max: 1000000,
  })
  // Compact, not `null, 1`. Two reasons, both measured on 2026-10-31:
  //   * `indent=1` costs 11–23% of the budget on the same payload
  //     (`quant_profile 600519.SH`: 20,095 B → 16,3xx B before any trimming),
  //     and the model parses JSON either way;
  //   * the ceiling is a **byte** budget. `text.length` counts UTF-16 code
  //     units, so a Chinese payload measuring 14,516 "characters" is 17,574
  //     bytes and was passing a check named `maxOutputBytes`. The host then
  //     dropped the whole body, and this function reported `truncated:false`.
  const text = JSON.stringify(value) ?? 'null'
  const bytes = Buffer.byteLength(text, 'utf8')
  if (bytes <= limit) return { text, truncated: false, bytes }

  for (let i = 0; i < ladder.length; i += 1) {
    const shrunk = shrinkJson(value, i, ladder)
    if (shrunk === null) break
    const candidate = JSON.stringify(shrunk)
    const size = Buffer.byteLength(candidate, 'utf8')
    if (size <= limit) {
      return { text: candidate, truncated: false, bytes: size, degraded: true }
    }
  }

  return {
    text: JSON.stringify({
      error: 'output too large',
      truncated: true,
      max_output: limit,
      bytes,
      next: 'retry with a smaller `limit`/`days`, or ask for one block at a time',
    }),
    truncated: true,
    bytes,
  }
}

export const PROMPT = `## 模拟交易台（quant-desk）
- 分工：内核是一个**信息源**，不是顾问。它只做三件事：给事实、给历史分布、给"如果你这么做会付多少"的测算。它没有 action 字段，不会说该买该卖，也不会替你做任何决定。买卖、仓位、要不要动核心盘，全是你的判断。
- 所有行情、持仓、数据状态只能来自 quant_* 工具；不得凭记忆或推测报价格。
- **被叫醒时先看这次要回答什么，再决定第一个调用**（分派表在技能的「被定时任务叫醒时」一节，本文件不背第二套）：巡检/告警先 quant_query(resource="watch") 并带 since；任务型（盘前、尾盘、复盘）直接进 quant_query(resource="context") 或 quant_query(resource="information")——**别为"看看有没有事"先调 watch**，它本来就要读全市场，那一次往返买不到信息。不带 since 的 watch 只答"有没有越过阈值"，带上才给 changes（宽度、池子家数、可买几只、账户、挪了 3 位以上的行业），并带 from/to/delta 与 material。findings 为空就写一行「无异常」收工，不要为凑内容去读一堆数据；报价源挂了给 quotes_unavailable，那不是"一切正常"。
- 叫醒后如果 quant_alert(action="list") 有待解读项，**先回答它**：那是规则层算完事实之后唯一悬着的问题，也是这封邮件能不能发出的唯一条件。读它给的 prompt（编号事实 + 计划条款 + 持仓），用三句话回答，每句引用一个出现过的数字；事实不够就直说缺什么，不要补。回答即发信，一条只答一次。
- 每次开工（非被叫醒时跳过 quant_query(resource="watch")）：quant_query(resource="context")（看 session.data_ready 与账户）→ quant_query(resource="observations")（约束状态与测算）→ quant_query(resource="duty")（待办清单）→ 再谈要不要交易。
- 止损是一条**状态**，不是一条命令：收盘跌破你自己写下的止损时，系统只报告距离、连续几日、以及次日开盘退出的成本。内核与接口不替你下**新判断**的单；但破线持续 autostop.confirm_seconds 之后，抓取循环会通过 /decision/propose + approve 代执行你建仓票上写好的那个止损（configs/params.yaml 的 autostop.enabled 可关，关掉就退回只记不卖）。除此之外要不要卖、什么时候卖仍是你的判断，日志会记住是谁决定的。
- **人格是显式的**：hot_money（游资，只能 AGGRESSIVE，需要实时报价与盘面结构）、retail（散户，只能 CORE，10–60 日）、trader（交易员，1–5 日）。当前人格决定你能用哪个袖、持多久。换风格先调 quant_workflow(operation="persona_switch")，必须带 reason / evidence / expected_edge_bps / invalidation，每天上限 2 次；提案里的 persona 必须等于当前人格，否则 409 PERSONA_MISMATCH。**切换不是重新开始，是留痕的决定**——quant_query(resource="persona") 的 scorecard 会把每次切换之后的收益和同期等权市场对上，所以"换个人格就好"这种话在这里会被记账。
- quant_query(resource="observations") 里的东西不是待办，是状态：止损被跌破、核心盘规则与实际权重的偏离、以及**两份要分开读的仓位数**——allocation（按 regime × 战绩算出的权重，是判断）与 enforced.strategy_max_weight（guard 真正会拒绝的静态上限）。两者之间有一段：超过分配但没超过注册上限时系统不会拒绝，走不走进那一段是你的判断，记下理由。它给你"若次日开盘退出要付多少手续费和冲击""这只票历史上类似下跌后 5 日的中位数与胜率"，不给你结论。要不要动，你说了算，但你要能解释为什么不动。
- 注意：只要有持仓收盘价低于它自己的止损，新开仓会被 409 OPEN_VIOLATION 拒绝（卖出不受限）。这不是建议，是风控规则：风险预算处在违约状态时不加仓。
- 完整决策链与「用户说一句外行话该调哪几个」的分派表在 quant-desk 技能正文（入口表 + 短线四步），**本文件不重复**：两套顺序并存时，模型会按读到的最后一套走，而它不一定是适合这个问题的那套。**逐只标的的问题必须带 symbol**；ledger / risk / regime / cards / tape_flow 是市场层面的，不带。做短线先看 quant_query(resource="knowledge", params={topic=tape_flow})：昨天涨停那批今天给不给溢价，决定今天该不该动手；再看 quant_query(resource="knowledge", params={topic=board, symbol=…}) 拿这一只的结构与同结构历史分布。
- **1–2日短线α必须由模型判断**：quant_query(resource="short_alpha_evidence") 只给稳定排序的证据卡，不给 candidate/watch；WorkBuddy 独立分析后用 quant_workflow(operation="short_alpha_judge") 冻结 thesis/evidence/invalidation/target_weight，candidate 与 watch 都可参与。T+1 开盘由 WorkBuddy 以 strategy=short_alpha、client_request_id=short-alpha:{T}:entry:{symbol} 走 quant_order(book="paper", action="propose")/quant_order(book="paper", action="approve")；持仓中用 quant_workflow(operation="short_alpha_review") 冻结动态理由，最迟 T+2 退出，quant_query(resource="short_alpha_execution") 只读关联票据与持仓。旧 quant_query(resource="short_alpha_legacy") 只是规则研究对照；无权重旧记录只跟踪，日更只结算，不自动选票或下单。
- 下单前先查 quant_query(resource="knowledge", params={topic=ledger})：那是一张**读数表**，不是判决表。每行给 spec（买了什么、卖了什么、什么池子、多少成本）与数字，gate_reading 是预注册阈值的机械读数——换一套阈值会得到另一个词，同一个数不变。**判断是你的**：把你打算做的和它的 spec 逐项比，entry / exit / 池子 / 成本任何一项不同就是另一个实验，这些数字不适用；四项都一样就该预期同样的结果。判断完写进 journal，带上你凭什么这么判。
- 拿不准一个形态，就 quant_query(resource="analog")：历史上最像的 300 次之后 1/2/3/4/5/10 日发生了什么（1–5 日连续，10 日作衰减参照），含胜率、超额、按交易日重采样的 95% 区间与最近邻距离分位。match.distance_band=far 表示最近邻比 60% 的随机标的都远——那组统计该降权甚至忽略，忽略与否由你判断。**net 块是扣掉你自己往返成本后的净超额**：survives_cost=false 意味着这段历史超额在你的规模上不存在，不要再拿 horizons 里的毛超额当理由。
- 判断"昨天强不强、今天该不该追"，先 quant_query(resource="limit_structure")：封板率、炸板率、连板高度、晋级率、涨停原因。封板率低而炸板率高时，追强的胜率会掉一个档；这是背景，不是禁止。
- 要在两只票之间做选择，用 quant_query(resource="compare")（同一次档案构建、同一估值窗口；报告期不同时财务三项标 comparable=false）——细节在工具描述里，这里不重复。
- 公告的**标题**在 profile.events.announcements 里，**正文**要 quant_query(resource="filing", params={announcement_id}) 读：一次一页，next_offset 续读，coverage 写着抽了多少页（12 / 187 意味着后面没有）。「减持计划」和「减持计划实施完毕」是同一个 category 里的相反事实，只有正文分得开。正文是 PDF 抽的，表格会塌——金额回 pdf_url 或 fundamentals 核。
- **要谈一只具体的票，先 quant_query(resource="profile")**：它一次给出身份、可交易性（涨跌停价/一手多少股/最小金额）、估值在窗口内的分位、已公告的最新财报、主力资金与两融余额、技术面（复权口径）、今日池子与 20 日龙虎榜、分红与公告、以及你的持仓和止损距离。缺的那一块会写成 null 并说明原因——null 不是 0，不要当 0 用。
- 价格有两套口径，别混：quant_query(resource="bars") 和 quant_query(resource="profile") 里的 close 是真实成交价（挂单、止损、成本用它），adj_close 是前复权价（算收益、均线、区间位置用它）。用未复权价算跨除权日的收益，会把分红记成亏损。
${UNITS_TEXT}
- 判断"这个量级能不能进出"，用 quant_query(resource="knowledge", params={topic=liquidity, symbol=…})：ADV、参与率、冲击 bps、1% ADV 容量。这个规模的账户撞不到容量（规模读 quant_query(resource="portfolio") 的 initial_cash/equity，不要假设它是多少），**但会撞最低佣金**：一笔 1,500 元的往返，5 元 × 2 就是 66bps，加印花税与双边滑点约 92bps——是 ledger 里判死那些结构时用的 20bps 的四倍多。规模越小，成本占比越高，这是你的主要对手而不是市场。
- 判断"这笔亏起来有多疼"，用 quant_query(resource="knowledge", params={topic=risk})：按当前权重回放过去 60 日的历史模拟 VaR、最差单日、集中度、持仓间相关性、距减半线的金额。
- 知识不是结论：每个答案都带 caveats 与 provenance，先读误差再决定权重。n 大不等于显著，看 excess_ci95 是否跨零。
- approve 返回 queued 表示"已排队到下一个交易日开盘执行"，不是成交；开盘跳空穿过限价会作废（cancel），此时不要立刻重报，先看原因。
- 已排队（queued）的单想撤，用 quant_order(book="paper", action="cancel", params={ticket_id, reason})，不是 quant_order(book="paper", action="reject")——reject 是「提案不做了」，cancel 是「这张单撤了」，后者守 can_cancel 窗口（ceiling 单 14:57–15:00 不可撤）。撤单不是卖出：要减仓用 quant_order(book="paper", action="propose", params={side=SELL})。
- 新仓前先 quant_query(resource="exposure")：同行业两只就是一次主题下注，不是分散；beta 与集中度会告诉你真实敞口。
- 提案写标的、方向、理由、证据、失效条件；数量可以给 qty 或 target_weight（二选一），价格不行：限价与止损永远由服务计算。strategy / persona 如填写会显式转发并由服务端校验。
- 如果是**用户让你下的单**（"清仓""买一点"），传 directed_by=user。它照常记账、照常过风控，但不计入 arena 的 edge——那是用户的择时，不是你的判断。不标就会算在你头上。
- data.core_usable=false 或 kill_switch=true 时不开新仓；aggressive_usable=false 时不做游资仓。**这两项说的是「支撑这个袖的数据在不在」，不是「准不准你交易」**；准入由 guard 的检查项决定，market.permission 只是状态读法（market.enforced=false：guard 不会因为它拒单）。
- **不确定自己缺什么，就 quant_query(resource="information")**：它把"能看见多少市场"打成分数（0–100），给出每个维度的覆盖率、历史深度、滞后天数，按品种探测实时报价，以及在同一天两个源互相矛盾的事实。blind_spots 里写着今天不能下的断言——没有盘中序列就不能谈分时承接，没有公告就不能说"没有消息"，沪市转债取不到实时报价就只能引用收盘价。**没有 grade，也不该按某个分数线决定能不能说某句话**：一个 85 分可能意味着一项 50 分被其余项平均掉了，所以要看 dimensions 里 score 最低的那几行，再看 blind_spots 列出的今天不能下的断言——能不能说某句话取决于你要说什么，那是你的判断。别用常识补空。**"不知道"是允许的答案；把 null 当 0 不是。**
- 单笔风险、最多几只、现金下限**一律读 quant_query(resource="context").risk.limits**（数值会改，这里不写死）；T+1 与 ST/停牌是内核规则。被 guard 拒绝时不要换参数重试，先解释原因。
- 问"钱在往哪个行业走"，用 quant_query(resource="knowledge", params={topic=industries})：31 个申万一级的 1/5/20 日涨跌、涨跌家数、MA20 上方占比、涨停家数、成交额占比、主力净流入。**涨幅必须和宽度一起读**——一个行业 +5% 若只有 3 只上涨、其余不动，那是抱团不是启动；反过来指数在跌而有 7 只涨停，那是局部炒作不是板块崩。这一层只有内核能算，它把日线、申万成员、涨停价、资金流按成员对齐。
- 盘前先 quant_query(resource="discover") 读事实，先问"哪条最反常"，不要先问"买什么"；quant_query(resource="screen") 只给规则候选，quant_query(resource="universe") 可以自由浏览全市场（换手/量比/市值/估值/行业），选什么由你决定。
- **内核不会替你下新判断的单，任何一笔新判断都不。** 没有自动再平衡、没有定时调仓：你能看到的每一种量化输出都只是信息和测算。唯一的例外是建仓票上写好的那个止损——破线持续 autostop.confirm_seconds 后，抓取循环通过 /decision/propose + approve 代执行它，那是兑现你已经写下的那句话而不是新的判断（configs/params.yaml 的 autostop.enabled 可关）。
- **核心盘（ETF）已于 2026-11-01 关闭**（configs/core.json 的 retired: true）：quant_query(resource="core") 与 quant_query(resource="core_preview") 返回 **409 CORE_RETIRED**，不要调用、也不要以为换个参数能绕过。原因：防守腿 511010 一手 14,112 元，是 5,007 元账户的 2.8 倍，每个防守日只能写一条 100% 现金的净值行。quant_query(resource="core") 仍可读（state=retired，executable 里有一手金额与需要多少净值），quant_query(resource="core_preview") 仍是纯试算可用。个股走 quant_order(book="paper", action="propose") / quant_order(book="paper", action="approve")。
- 每次决策后看 quant_query(resource="arena")：它把你的书、核心 ETF、市场放在一起，并给出每笔已平仓决策相对等权市场的超额与置信区间；少于 quant_query(resource="arena") 给的 min_decisions_for_an_interval 就不许声称有 edge（读它给的数，别背 20）。
- 形成观点时写 quant_query(resource="memo")：必须有机制、失效条件、带概率的预测（kind/horizon/probability），到期用 action=score 结算，Brier 会被记录。
- **决定不买也要留痕**：quant_workflow(operation="declined_add", params={}) 记下你差一点就买的那几只、理由与期限，到期 action=score 给出「如果你买了，跑赢还是跑输等权市场」。不记的话，克制永远不会被结算——11-01 到 11-03 三天零成交零记录，等于三天判断力没有任何证据。它不收价格，价格由结算自己读。
- 发现可检验的机制就 quant_query(resource="hypothesis", params={add})，统计裁判给出的是每个门槛的值与阈值（policy），不是结论——它叫 gate_reading 而不是 verdict，因为换一套预注册阈值会得到另一个词而数字不变。**要不要据此下注由你判断**，判断写进 journal。同一条 spec 被门槛拦过就不要原样重提：那不是勇气，是把同一个实验再做一遍。
- 盘后先 quant_query(resource="facts")，再 quant_workflow(operation="journal_append") 复盘：报告里每个关于"今天"的数字都要能在 facts 里查到——scripts/verify_report.py 会核，查不到即算编。**写入时服务也会核一遍**，返回的 verification 给出 matched/unmatched：unmatched 里的数不等于编的（可能是账户、时间、「我买了 3 只」这类别处来的数），但只要它是在描述今天，就该换成 facts 里查得到的那个；sign_from_words 表示数对上了但方向只在文字里（跌 3.04% 对 -0.030363），符号没核；**claims** 是带标签的那部分——misplaced 是「数是真的、贴错标签」（写 893 家跌停，跌停其实是 93），unbacked 是「这个标签下没这个数」（写可买 6 只，上限内 26、收盘可买 0）——这两种比"查不到"更该改，读者会顺手引用一个错的因果。in_cap / one_word / buyable_at_close 是三个不同的问题（上限内几只、其中开盘即封且全天没离开涨停价几只、收盘还能买几只），不能互相替代；「开过板」另读 board_opens.opened（东财池，分母不是 in_cap，不能相减或相除）；2026-10-29 的收盘报告把"我取过价的 6 只"写成"可买 6 只"，当日上限内 23 只、收盘可买 0 只。同一条 spec 被门槛拦过就别原样重提；要重提，先说清你的做法和它哪一项不同。shadow_candidate=true 才可进影子盘。
- 读旧结论先看 verified（quant_query(resource="memory_search") / quant_query(resource="journal_search") 每条都带）：null = **从没核过**，不是"核过没问题"；unmatched_count > 0 = 当时就有数对不上当天 facts。引用前先 quant_query(resource="facts") 重核，别把旧记忆当既成事实。取法：kind="review" 取复盘、id=N 取单条全文；别翻最新 20 条——note 是 review 的 4 倍，翻着翻着就全是巡检记录。复盘里"全文见 reports/…"的指针用 quant_query(resource="reports") 打开（分页，按 next_offset 取下一页）。
- 跑统计裁判需要在本机执行：cd D:/quant-desk && .venv/Scripts/python.exe scripts/run_research.py --hypothesis <id>（结论会自动回填）。
- 改规则只能 quant_workflow(operation="playbook_propose") 出草稿，激活由人决定。`

/**
 * Name a transport failure for what it actually is.
 *
 * `fetch` reports a timeout as `TimeoutError` (or `AbortError` when the caller
 * aborts). Both used to be dressed up as "quant service unreachable", which is
 * a different diagnosis with a different remedy: unreachable means "start the
 * service", a timeout means "the service is up and the answer is late".
 *
 * The desk proved the difference the hard way — on 2026-11-02 the four plugin
 * audits reported `quant_discover` as "threw / quant service unreachable"
 * while `/health` answered normally and a plain `curl` of the same path
 * returned 200 in 1.7 s. The audit then recorded that as a regression, which
 * is how a timeout became a false "the service is down" in the log.
 */
export function describeTransportFailure(error, baseUrl, timeoutMs) {
  const name = error?.name || 'fetch failed'
  const timedOut = name === 'TimeoutError' || name === 'AbortError'
  if (timedOut) {
    return `quant service timed out after ${timeoutMs}ms at ${baseUrl} (${name}). ` +
      `The service is still running — this is a slow answer, not a dead service. ` +
      `Check GET /health first, then retry once: a proposal retry reuses the same ` +
      `idempotency handle and an approval is idempotent by ticket_id, so neither can ` +
      `double-book. A journal/memo write is NOT idempotent — check before repeating it.`
  }
  let hint = `Start it with: .venv/Scripts/python.exe -m uvicorn service.api.app:app`
  try {
    const url = new URL(baseUrl)
    hint += ` --host ${url.hostname} --port ${url.port || '80'}`
  } catch {
    // A base URL that will not parse is its own problem; the caller already
    // validated it before the request, so this branch is defensive only.
  }
  return `quant service unreachable at ${baseUrl} (${name}). ${hint}`
}

/**
 * One service failure, with everything the caller needs to decide what next.
 *
 * `/decision/propose` returned 409 with `code: "EXECUTION_EXISTS"` and a `next`
 * saying "do not resubmit"; the client kept only `detail`, so the model read
 * "quant service 409: <one sentence>" and did the opposite. The 429's
 * `Retry-After` was dropped the same way. Every field the service took the
 * trouble to send is carried here and rendered by both hosts.
 */
export class ServiceError extends Error {
  constructor({ status, code = null, detail, next = null, fix = null,
                retryAfter = null, body = null }) {
    // The message carries `next` on purpose: the DSH host renders `error.message`
    // and nothing else, so a structured error that only lived in fields would
    // reach the model as "quant service 409: ..." again.
    const suffix = [
      next ? `下一步：${next}` : '',
      retryAfter ? `（${retryAfter} 秒后可重试）` : '',
      fix ? `修法：${fix}` : '',
    ].filter(Boolean).join(' ')
    super(`quant service ${status}${code ? ` [${code}]` : ''}: ${detail}` +
      (suffix ? ` — ${suffix}` : ''))
    this.name = 'ServiceError'
    this.status = status
    this.code = code
    this.detail = detail
    this.next = next
    this.fix = fix
    this.retryAfter = retryAfter
    this.body = body
  }

  toJSON() {
    return {
      error: 'service_error',
      status: this.status,
      code: this.code,
      detail: this.detail,
      // `next` and `fix` are the service's own words: it knows whether a write
      // landed. Dropping them is what turned "already booked, do not resubmit"
      // into "retry".
      next: this.next,
      fix: this.fix,
      retry_after_seconds: this.retryAfter,
    }
  }
}

/**
 * What a host should show for a failed tool call.
 *
 * MCP's `isError: true` carries a text block, not an object, so the structure
 * is serialised into that block. The message is kept for non-service errors
 * (guards, argument validation), which are already self-explanatory.
 */
export function errorText(error) {
  if (error instanceof ServiceError) return JSON.stringify(error.toJSON())
  return error?.message || String(error)
}

/** Fallback actions per code, for bodies that predate the `next` field. */
export const ERROR_ACTIONS = {
  STATE_CONFLICT: '没有产生写入；直接重试同一请求。',
  EXECUTION_EXISTS: '这一笔已经记账；用 quant_ticket 确认，不要重复提交。',
  ALREADY_FILLED: '票据已成交；读 quant_ticket 看成交，不要重提。',
  CORE_RETIRED: '核心盘已关闭；不要重试，也不要换参数绕过。',
  RATE_LIMITED: '被限流；按 retry_after_seconds 等待后再试，不要立刻重放。',
  INTERNAL: '服务端错误；先看 GET /health，再重试一次，仍失败查 data/service.log。',
}

export class QuantClient {
  constructor(config = {}) {
    this.allowRemote = config.allowRemote === true || (
      config.allowRemote === undefined && process.env.QUANT_ALLOW_REMOTE === '1'
    )
    this.baseUrlObj = validateServiceUrl(
      String(config.baseUrl || DEFAULTS.baseUrl).replace(/\/+$/, ''), this.allowRemote,
    )
    this.baseUrl = this.baseUrlObj.toString().replace(/\/+$/, '')
    this.token = config.token || process.env.QUANT_TOKEN || DEFAULT_TOKEN
    this.timeoutMs = integerOption(config.timeoutMs, LIMITS.timeoutMs, {
      name: 'timeoutMs', min: 1, max: LIMITS.timeoutCeiling,
    })
    this.maxResponseBytes = integerOption(config.maxResponseBytes, LIMITS.maxResponseBytes, {
      name: 'maxResponseBytes', min: 1, max: LIMITS.maxResponseCeiling,
    })
  }

  async request(path, { method = 'GET', body, query } = {}) {
    const url = validateServiceUrl(new URL(`${this.baseUrl}${path}`), this.allowRemote)
    for (const [key, value] of Object.entries(query || {})) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value))
      }
    }
    let response
    try {
      response = await fetch(url, {
        method,
        headers: {
          'X-Quant-Token': this.token,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error',
        signal: AbortSignal.timeout(this.timeoutMs),
      })
    } catch (error) {
      throw new Error(describeTransportFailure(error, this.baseUrl, this.timeoutMs))
    }
    const text = await readResponseText(response, this.maxResponseBytes)
    let data
    try {
      data = text ? JSON.parse(text) : {}
    } catch {
      if (response.ok) {
        throw new Error(`quant service ${response.status}: expected JSON response`)
      }
    }
    if (!response.ok) {
      const payload = data && typeof data === 'object' ? data : null
      // Two error shapes reach here: the app-level handlers send
      // `{code, detail, next}` and FastAPI's HTTPException sends
      // `{detail: {code, message}}` (that is what every `_fail(DecisionError)`
      // produces). Reading only the first made `BAD_SIZE` / `CORE_RETIRED`
      // invisible at the tool layer.
      const inner = payload?.detail && typeof payload.detail === 'object'
        ? payload.detail : null
      const code = payload?.code ?? inner?.code ?? null
      const detail = typeof payload?.detail === 'string'
        ? payload.detail
        : inner
          ? (inner.message ?? JSON.stringify(inner))
          : payload?.detail !== undefined
            ? JSON.stringify(payload.detail)
            : data === undefined
              ? (text.slice(0, 500) || response.statusText || 'non-JSON response')
              : JSON.stringify(payload ?? data)
      throw new ServiceError({
        status: response.status,
        code,
        detail,
        next: payload?.next ?? inner?.next ?? ERROR_ACTIONS[code] ?? null,
        fix: payload?.fix ?? inner?.fix ?? null,
        retryAfter: Number(response.headers.get('retry-after')) || null,
        body: payload,
      })
    }
    return data
  }
}

const jsonOutput = (max = DEFAULT_MAX_OUTPUT, ladder = SHRINK_LEVELS.text,
                    kind = 'text') => ({
  schema: { type: 'json' },
  //: What the answer *is*, not how big it is. The DSH host reads the ladder
  //: through ``render`` below; the MCP adapter has no ``output`` in its
  //: protocol, so it reads this instead. Without it the same tool shrank
  //: differently on the two hosts — `quant_pools(limit=20)` kept 20 rows under
  //: DSH and 12 under MCP, and nothing said the rows had been chosen.
  kind,
  render: (_args, value) => [{ type: 'text', text: formatJsonOutput(value, max, ladder).text }],
})

//: 表格类工具的输出：超预算时先压字、后丢行，见 SHRINK_LEVELS.rows 的注释。
const tabularOutput = () => jsonOutput(DEFAULT_MAX_OUTPUT, SHRINK_LEVELS.rows, 'rows')

/** The shrink ladder a host should use for a tool's answer shape. */
export function shrinkLadder(outputKind) {
  return outputKind === 'rows' ? SHRINK_LEVELS.rows : SHRINK_LEVELS.text
}

/**
 * 「逗号串或数组」→ 数组。参数名是复数、schema 只能声明一种类型时，两种写法
 * 模型都会写：实测 `quant_quote(symbols=[...])` 被宿主校验直接拒。这里都收下，
 * schema 用 oneOf 同时声明。
 */
export function asList(value) {
  const items = Array.isArray(value) ? value : typeof value === 'string' ? value.split(',') : []
  return items.map((item) => String(item).trim()).filter(Boolean)
}

/**
 * Refuse argument names the tool does not declare.
 *
 * Every `run` picks its fields by name, so an undeclared key used to vanish:
 * `quant_auction(limt=5)` ran with the default 10 and the answer looked normal;
 * `quant_core_plan(targtes={...})` answered "targets required" — a complaint
 * about a *different* name, which reads like a bug in the service. The service's
 * `extra="forbid"` never sees these fields because the tool rebuilds the body
 * from its whitelist before the request exists.
 *
 * MCP's spec asks for errors a caller can correct from; the legal list is that.
 */

export function unknownArguments(name, parameters, args) {
  const spec = parameters && parameters.properties ? parameters.properties : (parameters || {})
  const legal = Object.keys(spec)
  const unknown = Object.keys(args || {}).filter((key) => !legal.includes(key))
  if (!unknown.length) return undefined
  // 只在提案工具上补价格这句：读工具里 `price` 是合法参数（execution_estimate），
  // 泛泛地说"价格由服务端算"会变成一条假规则。
  const pricey = name.endsWith('propose') ? unknown.filter((key) => /price/i.test(key)) : []
  const hint = pricey.length
    ? ` ${pricey.join('/')} is refused on purpose: a proposal carries a quantity, the service computes the price.`
    : ''
  return `unknown argument(s) for ${name}: ${unknown.join(', ')}. legal: ${legal.join(', ') || '(none)'}.${hint}`
    + ' Nothing was sent: an undeclared field used to be dropped and the call ran with its default,'
    + ' so the answer quietly described a different question.'
}

/**
 * The response-level post-processing hook, applied by **both** hosts.
 *
 * Why it is not just code inside `run`: the MCP adapter does not execute the
 * tool against the service. It runs the tool against a *recording* client to
 * learn the request, sends that request itself, and renders the answer. A
 * mutation made inside `run` therefore lands on the `{__request}` placeholder
 * and is thrown away — `quant_bars(days=250)` told the DSH host "clamped to 60,
 * here is why" and told the MCP host nothing at all (measured 2026-11-08).
 *
 * A separate hook is the smallest shape that keeps one source: `execute`
 * applies it to a real answer, the adapter applies it to the answer it fetched,
 * and nothing is applied twice because `execute` skips it when the value is the
 * request placeholder. New post-processing belongs here, not in `run`.
 */
function define(name, description, parameters, run, output = null, after = null) {
  const resolved = output || jsonOutput()
  const tool = defineTool({
    name,
    description,
    parameters,
    output: resolved,
    execute: async (args, exec) => {
      const denied = unknownArguments(name, parameters, args)
      if (denied) throw new Error(denied)
      const value = await run(args || {}, exec)
      if (typeof after !== 'function' || (value && value.__request)) return value
      return after(value, args || {})
    },
  })
  // ``defineTool`` returns a fresh object built from a fixed field list, so the
  // output shape has to be attached after the fact. This is the one fact the
  // MCP adapter needs and the MCP protocol cannot carry: same tool, same
  // ladder, both hosts.
  tool.outputKind = resolved.kind || 'text'
  tool.after = typeof after === 'function' ? after : null
  return tool
}

/** ``quant_bars`` 的投递窗口。同一个算式给 run 与 after 用，避免两处各写一遍。 */
const barsWindow = (days) => {
  const asked = Number(days) || 60
  return { asked, served: Math.min(Math.max(Math.trunc(asked), 5), 60) }
}

// How long an auto-generated proposal handle is reused for the same intent.
// The client aborts at 60 s while the service keeps working, so a retry lands
// within a couple of minutes; five covers a slow model turn, and is short
// enough that tomorrow's deliberate order of the same size is a new request.
const REQUEST_ID_TTL_MS = 5 * 60 * 1000
const _requestIds = new Map()

/**
 * An idempotency handle that survives a retry whose prose changed.
 *
 * The service dedupes by intent fingerprint, which hashes `thesis` and
 * `evidence` verbatim — reword one sentence and the retry is a second ticket,
 * with the same money committed twice (measured: a 60 s timeout followed by the
 * documented "retry once" produced two `proposed` tickets). A caller-generated
 * id is the fix, but only if the retry sends the *same* id: a fresh UUID per
 * call is no better than the fingerprint. So the id is derived from the fields
 * that define the trade and reused for five minutes.
 *
 * Symbol, side, sleeve and size are in the key on purpose: 100 shares and then
 * 200 of the same name are two requests, not a retry. The contingency is in the
 * key for the same reason — changing the entry condition is a *different intent*,
 * and reusing the old id would hand back a ticket armed with the old condition.
 */
export function requestIdFor(args, now = Date.now()) {
  const explicit = typeof args?.client_request_id === 'string'
    ? args.client_request_id.trim() : ''
  if (explicit) return explicit

  const signature = JSON.stringify([
    args?.symbol ?? null, args?.side ?? null, args?.sleeve ?? null,
    args?.qty ?? null, args?.target_weight ?? null,
    args?.persona ?? null, args?.strategy ?? null,
    args?.contingency ?? null,
  ])
  const hit = _requestIds.get(signature)
  if (hit && now - hit.at < REQUEST_ID_TTL_MS) {
    hit.at = now
    return hit.id
  }
  const id = `propose-${randomUUID()}`
  _requestIds.set(signature, { id, at: now })
  if (_requestIds.size > 64) {
    // A stdio process lives for days; the map must not.
    for (const [key, value] of _requestIds) {
      if (now - value.at >= REQUEST_ID_TTL_MS) _requestIds.delete(key)
    }
  }
  return id
}

/**
 * Drop the handle that produced ``id``, so the next identical proposal is a
 * *new* request rather than a replay of a terminal answer.
 *
 * The service's ``proposal_requests`` contract is "same id, same answer" and it
 * keeps that promise for rejected/cancelled/expired tickets too. That is right
 * for a timeout (the caller cannot know whether the write landed) and wrong for
 * a rejection the caller has already read: re-proposing with a corrected thesis
 * used to hand back the old rejected ticket, and the model saw "still refused"
 * for a request it had fixed. The signature does not include the thesis on
 * purpose (a reworded retry inside five minutes should dedupe), so the terminal
 * status is the signal that the intent is over.
 */
export function forgetRequestId(id) {
  if (!id) return false
  for (const [key, value] of _requestIds) {
    if (value.id === id) {
      _requestIds.delete(key)
      return true
    }
  }
  return false
}

export function buildInternalTools(client) {
  return [
    define(
      'quant_context',
      'Compact trading context (<4KB): data health, account, risk budget, observations, regime, exposure, sentiment, top candidates, recent tickets. Call this at the start of any session that is not an alert-only wake-up — that one starts at quant_watch, and the dispatch table lives in the quant-desk skill. There is no `advice` field: the advice table was removed in P15 and nothing produces one. Read `session.tradable_now` before answering "what can I buy" — outside the session everything is a candidate for the next one. For short-term (1-2 day) questions the next call is quant_pools, not quant_screen: the pools carry the limit-up rows, and quant_screen deliberately excludes limit-ups.',
      {},
      () => client.request('/context_pack'),
    ),
    define(
      'quant_data_health',
      'Is today\'s data usable? Returns core/aggressive availability, missing datasets, quote age and lineage readiness.',
      { date: { type: 'string', description: 'YYYYMMDD; default latest trading day' } },
      (args) => client.request('/data/brief', { query: { day: args.date } }),
    ),
    define(
      'quant_information',
      'How much of the market you can actually see, as a score out of 100 with reasons. Twelve dimensions (price/volume, tradability, limit structure, flow, fundamentals, events, ETF+convertibles, live quotes, intraday series, dragon-tiger, auction, point-in-time), each scored on coverage, history depth and staleness. The live-quote dimension is probed per asset class — stocks ride THS, ETFs and Shenzhen convertibles ride Tencent, Shanghai convertibles have no quote path at all. Read score_floor / score_floor_keys / weak_dimensions alongside the headline: it is a weighted mean, so a comfortable headline can coexist with a dimension near zero; the weak-dimension list also carries the blind assertion and the fix command for each. Outside the session a stored close has age_seconds=null on purpose (a close is not "4 seconds old"), so that is a reachable channel, not a dead one — but there is still no realtime price. Returns blind_spots (claims the data cannot support today) and conflicts (two sources disagreeing about the same fact on the same day). Read it before asserting anything outside price and limit structure.',
      { date: { type: 'string', description: 'YYYYMMDD; default latest trading day' } },
      (args) => client.request('/data/information', { query: { day: args.date } }),
    ),
    define(
      'quant_facts',
      'The day\'s quotable numbers, under stable keys: breadth (advancers / decliners / flat, equal-weight and median return across ~5,500 names), pool counts with the vendor\'s own numbers beside the derived ones, what the price cap actually left buyable, the account (read from the book **now**, not from `day`: `account_as_of` says which session it belongs to, and a past-day read carries `account_note` — quote only the market half, breadth / pools / affordable, for a day that is not `account_as_of`), and today\'s tickets. Read this before writing a close report: every number a report states about "today" must be findable here, and `scripts/verify_report.py` fails a report whose numbers cannot be reproduced. in_cap / one_word / buyable_at_close answer three different questions and cannot stand in for each other (board_opens.opened answers a fourth, from a different vendor and a different denominator) — a report once said "6 names buyable" on a day when 23 sat inside the cap and 0 were buyable at the close.',
      { date: { type: 'string', description: 'YYYYMMDD; default the latest session with bars' } },
      (args) => client.request('/facts', { query: { as_of: args.date } }),
    ),
    define(
      'quant_live',
      'What the tape says RIGHT NOW: the latest archived capture (rt_snapshots, one batch every '
      + '30s–3min during a session) turned into the same keys as quant_facts — breadth, limit-up/'
      + 'limit-down counts, what the price cap leaves affordable, the two ends of the move list, '
      + 'and an account valued at these prices instead of the last close. Use this instead of '
      + 'quant_context/quant_facts whenever the question is about the current session: those two '
      + 'compute from `latest_bar_date()`, which at 10:30 is **yesterday** — their candidate list '
      + 'is priced at yesterday\'s close and cannot answer "what is happening now". Three blocks '
      + 'must be read together: (1) `frame_age_seconds` / `session.in_session` / `limits.available` '
      + '— how old this batch is, whether a new frame *should* exist, whether the counts could be '
      + 'computed; a large age outside the session is not staleness, the capture stops when the '
      + 'market does. (2) `delta` — the latest frame minus the one `lookback` minutes earlier. A '
      + 'single frame has no direction: "61 涨停" cannot tell you whether it fell from 82 or rose '
      + 'from 40, and those are opposite events. Read `delta.change` (a difference, not a percent: '
      + 'equal_weight_return 0.021→0.008 is -0.013) and `delta.short_of_lookback` (true when the '
      + 'day is younger than the window, so `elapsed_seconds` is what you actually got). (3) '
      + '`alerts` — what the 5-minute watcher fired today and when it last finished. Empty is '
      + 'ambiguous on its own: `alerts.watcher` tells "watched, nothing happened" from "nobody was '
      + 'watching", and `watcher.is_stale` marks the second one. Mind the scope: that log belongs to '
      + 'the **external** (real broker) book, as `alerts.scope` says — not to this simulated '
      + 'account. On 2026-11-05 the external book held 002185 while the paper book was flat, so a '
      + 'stop alert there is not your position. `book_watch` is the same shape for **your** paper '
      + 'book, written by the capture loop itself (tripwire on every frame): `book_watch.open` is '
      + 'what is true right now, `book_watch.items` is what happened while you were asleep — a line '
      + 'there may have been cleared two minutes later, so read `open` first. `book_watch.watcher` '
      + 'outcome says whether that loop is running (ok), between windows (idle), finished for the '
      + 'day (closed), or broke (error: ...). `quant_watch` runs the same checks on demand, against '
      + 'a vendor quote fetched at that moment — use it when you need the price to be this second. '
      + '`frame_id` is the batch filename (HHMMSS_%f). Since 2026-11-07 frames pruned from the '
      + 'hot directory are copied to rt_archive/<day>/ BEFORE deletion, so pass `as_of` '
      + '(ISO 8601 / HHMMSS / HH:MM:SS) or `frame_id` to replay a historical frame: only frames '
      + 'at or before that moment load, `historical=true`, and frame_age_seconds / frame_is_stale '
      + 'are null (a historical frame judged against today\'s clock would look stale by hours). '
      + 'Days before the cold archive started CANNOT be backfilled. `account` is still the '
      + 'CURRENT book, not a historical one. No signal, no score, no ranking: this is faster '
      + 'than quant_facts, not smarter.',
      {
        day: { type: 'string', description: 'YYYYMMDD; default today' },
        movers: { type: 'integer', description: '1-20 names on each end of the move list; default 5' },
        lookback: { type: 'integer', description: '1-240 minutes back for delta; default 15' },
        as_of: {
          type: 'string',
          description: [
            'Historical replay: load only frames at or before this moment (ISO 8601,',
            'HHMMSS or HH:MM:SS). A second-precision as_of includes the whole second,',
            'because the filename microseconds only prevent same-second collisions.',
            'Omit for the latest frame. Returns historical=true with age/stale null.',
          ].join(' '),
        },
        frame_id: {
          type: 'string',
          description: [
            'Historical replay: fetch one exact frame by HHMMSS_%f from the hot directory',
            'or rt_archive cold storage. Mutually exclusive with as_of; frame_id wins.',
            'Frames captured before 2026-11-07 (cold archive start) are unavailable.',
          ].join(' '),
        },
      },
      (args) => client.request('/market/frame', {
        query: { day: args.day, movers: args.movers, lookback: args.lookback,
                 as_of: args.as_of, frame_id: args.frame_id },
      }),
    ),
    define(
      'quant_quote',
      'Latest prices for up to 20 symbols: last/prev/open/high/low, limit band, pct change, and to_up_limit_pct/to_down_limit_pct (how far the last price sits from each band — the number a 打板 decision actually reads). Each quote carries source (ths/eastmoney/tencent/daily) and age_seconds — if source=daily it is the last stored close, not a live price. Read timestamp_basis before trusting age_seconds: `fetched` means quote_ts is when we asked the vendor (no vendor on this desk supplies a per-quote time), `daily_bar` means quote_ts is the bar\'s own trading date. Outside the session age_seconds is null on purpose — the last price is then the day\'s close and "4 seconds old" would be a lie. `limit_state` is one_word / sealed / opened / none and is **null when source=daily**: a stored bar\'s sealed state describes that bar\'s own session, not now (2026-11-03 read a 11-02 close of 7.86 as "sealed at 7.86" when the day\'s real limit was 8.65). When the source supplies five levels, `depth.bid1_volume` is the seal size in 手 while the board is sealed — the most direct answer to "can I still get in".',
      {
        symbols: {
          required: true,
          // 名字是复数：宿主按 schema 校验，写死 type:'string' 会让「传数组」直接
          // 被拒在门外（实测过一次）。两种写法都收，handler 统一成逗号串。
          oneOf: [
            { type: 'string', description: 'comma separated, e.g. 600000.SH,000001.SZ' },
            { type: 'array', items: { type: 'string' }, description: 'same codes as an array' },
          ],
        },
      },
      (args) => client.request('/market/quote', {
        query: { symbols: asList(args.symbols).join(',') },
      }),
    ),
    define(
      'quant_market_status',
      'Is the tape live? Session clock, which quote source is answering, cache age and last error. Call it when price freshness matters. `session.label` names one window at a time — 开盘集合竞价 (09:15–09:30) / 连续竞价 / 午休 / 收盘集合竞价 (14:57–15:00) / 已收盘 (15:00–15:05) / 盘前盘后 — and `in_session` stays true through 15:05 only so a 15:01 snapshot still counts as this session: it does not mean an order at that minute fills in continuous trading.',
      {},
      () => client.request('/market/status'),
    ),
    define(
      'quant_bars',
      'Recent daily bars for one symbol, 5-60 sessions. Use for trend/ATR/volume context. 60 is a delivery ceiling, not a data limit: 250 bars is about 27 KB and the host drops any body over 16 KB **whole**, so asking for 250 used to return 12 bars with no error — the ceiling now clamps and says so in `window_capped`. For longer windows use quant_profile (technicals: ma20/ma60/ma200, atr20, 区间位置) or quant_knowledge(topic=distribution).',
      {
        symbol: { type: 'string', required: true },
        days: { type: 'integer', description: '5-60 sessions; default 60' },
      },
      async (args) => {
        const { served } = barsWindow(args.days)
        return client.request('/market/bars', {
          query: { symbol: args.symbol, days: served },
        })
      },
      // 钳位说明必须两个宿主都看到：MCP 适配层走「录制 → 真发」，run 里的
      // 赋值会落在 __request 占位体上，所以它放在 after 而不是 run 里。
      null,
      (data, args) => {
        const { asked, served } = barsWindow(args.days)
        // A silent clamp would read as "this stock only has 60 sessions". Say it.
        if (served < asked && data && typeof data === 'object' && !Array.isArray(data)) {
          data.window_capped = {
            asked_for: asked,
            served,
            why: '250 根日线约 27 KB，超过宿主 16 KB 上限会被整包丢弃；'
              + '更长的窗口用 quant_profile 的 technicals 或 topic=distribution',
          }
        }
        return data
      },
    ),
    define(
      'quant_mins',
      '5-minute bars for one name on one session. Whole market is collected after the close, so a covered day is a pure read; '
      + 'an older day or a Beijing name is fetched once and says `from`. `t` is the bar end time, `vol` is in lots. '
      + 'Use it for "缩量回落 or 放量派发", the shape of a seal, or where the price sat versus VWAP — not for anything daily (use quant_bars).',
      {
        symbol: { type: 'string', required: true },
        date: { type: 'string', description: 'YYYYMMDD; default latest stored session' },
        last_n: { type: 'integer', description: 'keep the last N bars (8-240, default 48 = a whole session)' },
      },
      (args) => client.request('/market/mins', {
        // The server refuses limit < 8 (fewer bars cannot answer anything) and
        // clamps at 240; doing it here keeps that 422 out of a four-minute window.
        query: { symbol: args.symbol, date: args.date || undefined,
                 limit: Math.min(240, Math.max(8, args.last_n || 48)) },
      }),
    ),
    define(
      'quant_profile',
      [
        'Everything the desk knows about one name: identity, tradability (limit prices, lot size,',
        'minimum ticket), 申万 L1/L2/L3 alongside the vendor label with the comparison group it',
        'picked and why narrower buckets were skipped, valuation with in-window percentiles and',
        'direction, industry cross-section with 4 nearest comparables, latest reported fundamentals',
        '(year-to-date, with the basis',
        'stated), main-capital flow and margin balance, technicals on adjusted prices, relative',
        'strength against its industry and the market, today pool membership plus 20-session 龙虎榜',
        'with how many sessions could be read, corporate actions, filings from cninfo labelled four',
        'ways from the title (subject, document kind, stage, and whether the document carries a new',
        'fact) with the word behind each label, plus a 220-character excerpt — the body itself is',
        'quant_filing, paged — news tagged with its scope and the word that decided it, the shape of',
        "today's 5-minute session (fetched on demand when this name was never collected),",
        'your position if held. Every block carries the date it is true as of; a missing input is',
        'null, never 0. Use this before arguing a thesis about a single stock.',
      ].join(' '),
      {
        symbol: { type: 'string', required: true, description: 'e.g. 600519.SH' },
        day: { type: 'string', description: 'YYYYMMDD; default last session with bars' },
        valuation_window: {
          type: 'integer',
          description: '250-2500 sessions for valuation percentile; default 750 (about 3 years)',
        },
        sections: {
          oneOf: [
            { type: 'array', items: { type: 'string' } },
            { type: 'string', description: 'comma separated, e.g. events,technicals' },
          ],
          description: [
            'Ask for specific blocks when you do not need all thirteen: identity, tradability,',
            'valuation, peers, relative_strength, fundamentals, flow, technicals, sentiment,',
            'ownership, events, position, intraday. All thirteen are 16.7 KB — more than one',
            'response can carry — so the default returns every block that fits and lists the rest',
            'in omitted_sections with a size_note; a dropped block costs you one targeted call,',
            'while a host-truncated string looks like a short fact. Asking for one block returns',
            'that block in full: the contents do not change with the request, only what is',
            'included. Pass an array (or a comma-separated string), e.g. ["events"].',
          ].join(' '),
        },
      },
      (args) => client.request(`/symbol/${encodeURIComponent(args.symbol)}/profile`, {
        query: {
          day: args.day,
          valuation_window: args.valuation_window,
          sections: asList(args.sections).length ? asList(args.sections).join(',') : undefined,
        },
      }),
    ),
    define(
      'quant_filing',
      'Read one announcement body by announcement_id. The title says a filing exists; the body says whether it matters — 「减持计划」 and 「减持计划实施完毕」 are one category and opposite facts. Returns ONE window (about 3,500 chars, capped at 9 KB) plus `next_offset`, the only way to continue: a window closes on a sentence break, so `chars` is a request and `returned` is the truth. `coverage` says how much of the PDF was extracted — "12 / 187 页" means page 40 is absent, not missing. Text is PDF-extracted and tables collapse: amounts must be read from pdf_url or fundamentals. Undownloaded text is fetched once on demand; a scanned PDF says so instead of retrying.',
      {
        announcement_id: {
          type: 'string',
          required: true,
          description: 'digits only, from profile events.announcements[].announcement_id or knowledge(topic=filings)',
        },
        offset: { type: 'integer', description: 'character offset; use the previous response\u2019s next_offset, not offset+chars' },
        chars: { type: 'integer', description: 'window size 400-6000, default 4000; may come back shorter when the byte budget binds' },
        fetch: { type: 'boolean', description: 'download the PDF if the text was never cached; default true (first call can take a few seconds)' },
      },
      (args) => client.request(`/filing/${encodeURIComponent(args.announcement_id)}`, {
        query: { offset: args.offset, chars: args.chars, fetch: args.fetch },
      }),
    ),
    define(
      'quant_compare',
      'Two or three names side by side, on one basis. Both columns come from the same profile build with the same valuation window, so a difference in PE percentile cannot be an artefact of two different windows — that is the whole point, and doing it by calling quant_profile twice cannot give you it. Rows: price, lot cost, PE/PB with in-window percentiles, dividend yield, 20-day return and its excess over the name’s own industry and the market, 5-day main-capital flow, price vs MA20, ATR20, annualised vol, 250-day range position, plus three financials. Two guards are stated rather than assumed: `period_match=false` means the two names reported different quarters and the three financial rows are marked comparable=false (comparing Q2 ROE to Q3 ROE is not a comparison); `industries` says what each vs_industry_pp is measured against. About 5s for two names.',
      {
        symbols: {
          type: 'array',
          items: { type: 'string' },
          required: true,
          description: 'two or three symbols, e.g. ["600519.SH","000858.SZ"]',
        },
        day: { type: 'string', description: 'YYYYMMDD; default last session with bars' },
        valuation_window: {
          type: 'integer',
          description: '250-2500 sessions for the percentiles; default 750. Applied to every name',
        },
      },
      (args) => client.request('/compare', {
        query: {
          symbols: Array.isArray(args.symbols) ? args.symbols.join(',') : args.symbols,
          day: args.day,
          valuation_window: args.valuation_window,
        },
      }),
    ),
    define(
      'quant_short_alpha_evidence',
      '短线α的信息源入口：返回派生收盘涨停池与同花顺封板面板交集中的证据卡，按股票代码稳定排序，不按收益排名，不输出 candidate/watch。每行有 evidence_id、封板结构、条件桶历史分布、近期250日诊断、同身位基准、账户整手成本、分帧封单与反证。WorkBuddy 必须自行分析并登记判断；本工具不替模型选票，不下单，也不证明排队能成交。范围不含低吸、炸板反转或全市场非涨停排名。',
      {
        as_of: { type: 'string', description: '可选 YYYYMMDD；默认最新完整日线，历史日的账户情景不是PIT' },
        symbols: {
          oneOf: [
            { type: 'string', description: '可选，逗号分隔，最多12只' },
            { type: 'array', items: { type: 'string' }, description: '可选，股票代码数组' },
          ],
        },
        limit: { type: 'integer', description: '每次1–4只，默认2；用 offset 续读，避免整包超过宿主预算' },
        offset: { type: 'integer', description: '从0开始；用 pagination.next_offset 续读' },
      },
      (args) => client.request('/short-alpha/evidence', {
        query: {
          as_of: args.as_of,
          symbols: Array.isArray(args.symbols) ? args.symbols.join(',') : args.symbols,
          limit: clampRows(args.limit, LIMITS.rows.shortAlphaEvidence),
          offset: integerOption(args.offset, 0, { name: 'offset', min: 0, max: 10000 }),
        },
      }),
    ),
    define(
      'quant_short_alpha_judge',
      '登记并冻结 WorkBuddy 自己的短线α判断与可选 target_weight。必须先调用 quant_short_alpha_evidence，reviewed_evidence 填实际读过的 evidence_id；items 每只必须引用自身 evidence_id，并由模型写 thesis 与 invalidation。最多2个 candidate、3个 watch；无 candidate 时必须写 no_candidate_reason，items=[] 表示明确无候选，不等于未登记。candidate/watch 都可给 (0,1] 目标权重，总和不得超过 short_alpha 策略上限；旧记录无权重只跟踪，不追溯下单。服务端只校验证据归属、宇宙、文本、日期和预算，不按收益换票。每日只冻结一次，同请求幂等，不同判断409。本调用仍不创建票据、不改账本；T+1 由 WorkBuddy 另走 quant_propose/quant_approve。历史回填标 replay；最新完整日线才是 prospective。',
      {
        day: { type: 'string', description: '可选 YYYYMMDD；默认最新完整日线' },
        reviewed_evidence: {
          type: 'array', required: true,
          items: { type: 'string' }, description: '实际读过的1–12个 evidence_id，去重；服务端强制数量与唯一性',
        },
        items: {
          type: 'array', required: true,
          description: 'WorkBuddy 判断，最多5只；无候选且也不观察时传 []',
          items: {
            type: 'object', additionalProperties: false,
            properties: {
              symbol: { type: 'string', required: true },
              role: { type: 'string', enum: ['candidate', 'watch'], required: true },
              thesis: { type: 'string', required: true, description: '20–1200字，服务端强制' },
              evidence: {
                type: 'array', required: true, items: { type: 'string' },
                description: '1–8个 evidence_id，必须包含自身证据且在 reviewed_evidence 中',
              },
              invalidation: { type: 'string', required: true, description: '5–500字，服务端强制' },
              target_weight: {
                type: 'number',
                description: '可选 (0,1]；candidate/watch 均可参与，合计不得超过 short_alpha 策略上限；空值只跟踪',
              },
            },
          },
        },
        no_candidate_reason: { type: 'string', description: '无 candidate 时必填；有 candidate 时留空' },
        replay: { type: 'boolean', description: '历史回放强制 true；最新日正常登记省略或 false' },
        client_request_id: { type: 'string', description: '可选幂等句柄；重试必须沿用同一判断与同一句柄' },
      },
      (args) => client.request('/short-alpha/judgments', {
        method: 'POST',
        body: {
          day: args.day,
          reviewed_evidence: args.reviewed_evidence,
          items: args.items,
          no_candidate_reason: args.no_candidate_reason || '',
          replay: args.replay === true,
          client_request_id: args.client_request_id,
        },
      }),
    ),
    define(
      'quant_short_alpha_review',
      '冻结 WorkBuddy 对短线α持仓的一次动态复评。day 是原信号日 T，不是复评当天；items 每只给 hold / exit_next_open / exit_now、至少20字理由和实际读取的1–8个证据id。client_request_id 必须在同一复评窗口固定，例如 short-alpha-review:{T}:1910；同内容重试幂等，不同内容409。本工具只冻结判断，不创建订单；需要退出时必须另用 quant_propose(side=SELL,strategy=short_alpha) + quant_approve，client_request_id=short-alpha:{T}:exit:{symbol}。最迟 T+2 退出，不得用复评延长边界。',
      {
        day: { type: 'string', required: true, description: '原冻结信号日 YYYYMMDD' },
        items: {
          type: 'array', required: true,
          items: {
            type: 'object', additionalProperties: false,
            properties: {
              symbol: { type: 'string', required: true },
              decision: { type: 'string', enum: ['hold', 'exit_next_open', 'exit_now'], required: true },
              reason: { type: 'string', required: true, description: '20–1200字；退出或继续持有的独立理由' },
              evidence: { type: 'array', required: true, items: { type: 'string' } },
            },
          },
        },
        client_request_id: { type: 'string', required: true, description: '8–64字符；同一复评窗口固定' },
      },
      (args) => client.request('/short-alpha/reviews', {
        method: 'POST',
        body: { day: args.day, items: args.items, client_request_id: args.client_request_id },
      }),
    ),
    define(
      'quant_short_alpha_execution',
      '只读短线α模拟执行状态：按信号日关联 target_weight、T+1入场窗口、最迟T+2退出日、entry/exit固定请求号、真实票据状态、当前持仓与不可变复评。queued/proposed 不是成交，旧无权重记录显示 tracking_only。本工具不提案、不批准、不改任何账本；实际买卖仍走 quant_propose/quant_approve，绝不触碰账外实盘。',
      {
        day: { type: 'string', description: '可选原信号日 YYYYMMDD' },
        limit: { type: 'integer', description: '0–30个信号日，默认12' },
      },
      (args) => client.request('/short-alpha/execution', {
        query: { day: args.day, limit: args.limit === undefined ? 12 : args.limit },
      }),
    ),
    define(
      'quant_short_alpha_tracking',
      '只读 WorkBuddy 短线α判断的样本外跟踪。默认只看 prospective，历史 replay 永不合流。按 candidate/watch、T收盘研究锚点/T+1开盘可执行代理、T+1/T+2分层给 n、独立日、净超额均值/中位数/胜率与交易日块区间；n<30或独立日<8时明确不足。一字、排队未知、缺bar、停牌、预算不足不记0收益。latest_bar_judgment_registered 区分未登记与明确无候选。只结算已冻结判断，不自动选票，不创建订单。',
      {
        scope: { type: 'string', enum: ['prospective', 'replay', 'all'], description: '默认 prospective' },
        limit: { type: 'integer', description: '1–12个信号日，默认4；列表为紧凑记录' },
      },
      (args) => client.request('/short-alpha/tracking', {
        query: {
          scope: args.scope || 'prospective',
          limit: clampRows(args.limit, LIMITS.rows.shortAlphaTracking),
        },
      }),
    ),
    define(
      'quant_short_alpha',
      '【历史规则研究，非 WorkBuddy 判断】只读短线α候选合成：在派生收盘涨停池与同花顺封板名单交集中，按固定条件桶计算 T+1/T+2 相对全市场等权的收益，扣账户整手情景的完整费用与双边模型冲击。返回0–2个 research_candidate 和最多3个 watch；只有两个主期限净均值>0且交易日块95%区间下界都>0才是候选，不是收益承诺。每只含样本/独立交易日、三期限证据、成本、封单反证与失效条件。无 action、无下单、无价格指令；排队成交概率仍未知，历史as_of的账户不是PIT。scope不含低吸、炸板反转或全市场非涨停排名。',
      {
        as_of: { type: 'string', description: '可选 YYYYMMDD；信号按该日收盘回放，账户仍是当前快照' },
        limit: { type: 'integer', description: '0–2，默认2；设为0只返回watch与排除原因' },
      },
      (args) => client.request('/short-alpha/candidates', {
        query: {
          as_of: args.as_of,
          limit: integerOption(args.limit, 2, { name: 'limit', min: 0, max: 2 }),
        },
      }),
    ),
    define(
      'quant_pools',
      'START HERE FOR SHORT-TERM (1-2 day) questions: "can I play the tape today", "is chasing strength being rewarded". One call returns the sentiment gates (tone / break_rate / max_boards), the three pools with per-name rows, and the three-source cross-check. Each limit_up row carries boards (consecutive limit-ups), first_seal_time, seal_amount, open_times and — the number a 5,000 CNY account needs before anything else — lot_cost_cny (one 100-share lot) and pct_of_equity. Read gates.ok first: `false` means one of the pre-registered thresholds was crossed. That is a reading, not a command: it says the tape structure does not support opening new risk under those lines, not that the session is closed. If you stand down, record why; `null` means the panel never loaded and the gate could not be judged at all, which is a pipeline problem and must not be read as a market call. Then read pool_state before quoting counts: in the first 30 minutes the pools are still loading and tone comes back as 未成形. counts.limit_up comes from the derived pool (close == up_limit, checkable stock by stock) because both vendors miss names — 东财 averaged 2.1 missing limit-ups a day over Sep–Oct 2026, 同花顺 1.6. counts.eastmoney is the same day as that vendor published it; the gap is real, not a definition. limit_up_ex_bj excludes the 30%-band 北交所 names — counting them is a choice, missing them is not. NOT the tool for 低吸/趋势 candidates: those come from quant_screen, which excludes limit-ups on purpose.',
      {
        date: { type: 'string', description: 'YYYYMMDD; default latest. `as_of` is accepted as an alias' },
        as_of: { type: 'string', description: 'alias of `date`; exists only because the other date-scoped tools use this name. Pass one, not both' },
        limit: {
          type: 'integer',
          description: 'max rows per pool, default 15. Measured 2026-11-02 (after the one-lot columns were added): 15 rows ≈ 10.4 KB, 20 rows ≈ 12.5 KB. The host ceiling is 16 KB and an oversized body is dropped whole rather than truncated, so this is clamped to 1-20 — asking for 100 would return nothing at all',
        },
        group_by: {
          type: 'string',
          enum: ['none', 'industry'],
          description: 'industry answers "这个行业有几只连板" without counting rows by hand: limit_up / max_boards / broken / symbols per 东财 industry (104 buckets, not 申万 31 — see quant_sector for that)',
        },
        // 服务端一直支持六种排序，工具层没暴露，于是"按连板高度排"这件事只能
        // 由模型读 20 行自己排。默认 boards 与改造前一致，不改变既有调用。
        rank_by: {
          type: 'string',
          enum: ['boards', 'seal_ratio', 'first_seal', 'lot_cost', 'theme_size', 'none'],
          description: 'order inside each pool, default boards. seal_ratio = 封单/流通 (strength, not absolute money); first_seal = earliest seal first; lot_cost = one 100-share lot cost, the order a 5,000 CNY account should read; theme_size = how many limit-ups share the strongest theme',
        },
      },
      (args) => client.request('/market/pools', {
        // Clamped, not trusted. The service accepts up to 100 rows, which is
        // ~65 KB; the host drops a body over 16 KB whole, so "limit=100" would
        // come back as no data rather than partial data. Twenty rows is 14 KB
        // and still answers "what does the tape look like".
        query: {
          date: args.date ?? args.as_of,
          limit: clampRows(args.limit, LIMITS.rows.pools),
          group_by: args.group_by || 'none',
          rank_by: args.rank_by || 'boards',
        },
      }),
      // 表格工具：超预算时先压字、后丢行。见 SHRINK_LEVELS.rows 的实测注释。
      tabularOutput(),
    ),
    define(
      'quant_screen',
      'Deterministic candidate screen: tradable universe, price <= max, ADV >= 50M, not ST, not at limit, no 科创板/北交所; ranked by 20d momentum + liquidity. This is the 低吸/趋势 pool, NOT the short-term one: it drops limit-ups and anything near the ceiling, which is exactly what a 1-2 day trade looks at. For 打板/连板 read quant_pools and quant_limit_structure instead. `max_price` is an upper bound only: the effective cap is min(that, the highest price one 100-share lot can buy at the target weight), and the response\'s `max_price` is that effective number — on a 5,000 CNY account it comes back around 15, so asking for 45 does not widen the list. 北交所 is excluded here by policy: its data is part of the tape, its shares are not part of this book. '
      + 'Rows carry board and band: 创业板 is in scope, and a 300-series name trades a 0.20 band against 0.10 for a main-board one — the same "+8%" is 2 points from the ceiling on one and 12 on the other.',
      {
        limit: { type: 'integer', description: '1-20 candidates; default 8' },
        max_price: { type: 'number', description: '>1 and <1000; an upper bound (default 45), further capped by what one 100-share lot can afford at the account size' },
      },
      // `limit` 不填时**不能**替服务端填一个数：`/research/screen` 的默认值来自
      // `screen.max_candidates`（签名里是 None，就是为了让配置说话）。工具层硬塞
      // 8 会把 yaml 里的设置顶掉，改配置再重启也不生效。
      (args) => paged(args.limit, LIMITS.rows.screen, (limit) => client.request(
        '/research/screen', {
          query: {
            limit: args.limit === undefined || args.limit === null || args.limit === '' ? undefined : limit,
            max_price: args.max_price,
          },
        },
      ), '服务端自己也只收 20 行（le=20），再大会 422；20 行实测 5,629 B'),
      tabularOutput(),
    ),
    define(
      'quant_portfolio',
      'Paper account: equity, cash, positions with weights and PnL, fees paid, drawdown, and `pending` (tickets parked for an opening print). Read cash carefully: `cash_available` is the ledger balance, `cash_committed` is money already promised to queued buys, and `cash_free` is what the next order may actually spend — a queued buy freezes nothing until it settles. This is the simulated book only. A live broker mirror also exists (data/external.sqlite, read-only) and it is a different account: to see what it holds and how far each position is from its stop, call quant_stops with account="external", or read the `external` block of quant_dashboard. When the two disagree about "my positions", say which account you are answering for.',
      {},
      () => client.request('/portfolio'),
    ),
    define(
      'quant_risk',
      'Current risk state and hard limits: daily PnL, drawdown, kill switch, safe mode, position caps.',
      {},
      () => client.request('/risk'),
    ),
    define(
      'quant_propose',
      'Propose a trade as a ticket. Quantity is allowed as a positive integer qty OR target_weight, never both — target_weight is a TARGET (the service trades the distance from the current position to it, so asking for the weight you already hold buys nothing). Do NOT send price, limit_price or stop_price: the service computes those and enforces lot, position and risk caps. Returns status proposed or rejected with reasons. Read ticket.fillability before approving: queue.required=true means the price is sealed at the ceiling, so the order can only fill by queueing and fill_ratio.value is null because no subscribed feed knows the queue depth. That is a fact about the market, not a veto — but a ticket whose fill is a queue is not the same trade as one that clears at the touch. Also read `round_trip_cost_bps`, not `est_fees`: est_fees is the buy side only, and on a small ticket the ¥5 minimum commission lands twice. Measured 2026-11-02: a 311 CNY lot showed est_fees=5.00 but 346.7 bps round trip — 3.5% before the stock moves. If this call times out, call it again: the plugin reuses one idempotency handle for the same trade for five minutes, so you get the same ticket_id back instead of a second ticket holding the same cash.',
      {
        symbol: { type: 'string', required: true, description: '600000.SH' },
        side: { type: 'string', enum: ['BUY', 'SELL'], required: true },
        sleeve: { type: 'string', enum: ['CORE', 'AGGRESSIVE'], description: 'default CORE' },
        intent_type: { type: 'string', description: 'short label, e.g. core_rebalance / first_board' },
        thesis: { type: 'string', required: true, description: '20-1200 characters' },
        evidence: {
          type: 'array',
          items: { type: 'string' },
          required: true,
          description: '1-8 evidence ids you actually read, e.g. daily:20261023',
        },
        invalidation: { type: 'string', required: true, description: '5-500 characters' },
        target_weight: {
          type: 'number',
          description: 'desired weight >0 and <=1; mutually exclusive with qty',
        },
        qty: {
          type: 'integer',
          description: '1-1000000 shares for BUY/SELL; lot and risk caps apply; omit on SELL to close',
        },
        strategy: { type: 'string', description: 'registered strategy, max 30 chars; default llm' },
        persona: {
          type: 'string',
          description: 'must equal the active persona; otherwise the service rejects a BUY',
        },
        contingency: {
          type: 'object', additionalProperties: true,
          description: [
            'Optional entry condition (intent layer). Accepted when trigger.observe OR',
            'trigger.enabled is true; with both off the service returns TRIGGER_DISABLED',
            'and logs proposal_blocked. With ONLY observe on, the ticket is armed and',
            'evaluated frame by frame into reports/shadow, and a second approve is',
            'refused: no cash, no order, ever. Only enabled=true lets the capture loop',
            'approve the SAME ticket and spend money.',
            'Fields: kind(price|frame_time), op(lte|gte|eq), value, valid_from,',
            'valid_until, max_triggers=1, on_expiry=mark_only.',
            'With a condition, the FIRST approve only freezes the ticket as armed',
            '(no cash, no queue). Never re-propose on trigger.',
          ].join(' '),
        },
        directed_by: {
          type: 'string',
          enum: ['agent', 'user'],
          description: [
            'who decided: agent = your own judgement (default); user = the human told you to do this.',
            'A user-directed exit is booked and guarded the same way, but it is excluded',
            'from the arena edge statistic: it measures the instruction, not the judgement.',
          ].join(' '),
        },
        client_request_id: {
          type: 'string',
          description: [
            'Idempotency handle. Leave it out: the plugin fills it, and reuses the same',
            'value for the same trade (symbol/side/sleeve/size) for five minutes, so a',
            'timeout retry returns the SAME ticket even if the thesis was reworded.',
            'Pass it explicitly only to retry across sessions or processes.',
          ].join(' '),
        },
      },
      (args) => client.request('/decision/propose', {
        method: 'POST',
        body: {
          symbol: args.symbol,
          side: args.side,
          sleeve: args.sleeve,
          intent_type: args.intent_type,
          thesis: args.thesis,
          evidence: args.evidence,
          invalidation: args.invalidation,
          target_weight: args.target_weight,
          qty: args.qty,
          strategy: args.strategy,
          persona: args.persona,
          directed_by: args.directed_by,
          contingency: args.contingency,
          client_request_id: requestIdFor(args),
        },
      }),
      // A terminal answer retires the idempotency handle. The service promises
      // "same id, same answer" — including "rejected" — so reusing the handle
      // after a guard refusal returned the *old* rejected ticket even when the
      // caller had fixed the thesis. The handle exists to survive a timeout,
      // not to make a rejected proposal permanent: the next attempt with the
      // same shape gets a fresh id, and a genuine retry (nothing came back) still
      // hits the old one.
      null,
      (data) => {
        const status = String(data?.status || '')
        if (status === 'rejected' || status === 'cancelled' || status === 'expired') {
          forgetRequestId(data?.client_request_id)
        }
        return data
      },
    ),
    define(
      'quant_approve',
      'Execute a proposed ticket in the paper account. Re-runs the guard first; idempotent. Requires confirm=true. Returns filled | queued | rejected | already_filled | expired | armed. `armed` is the first approve of a ticket carrying a contingency: the entry condition is frozen, NO cash moved and nothing entered the queue. The capture loop approves the SAME ticket when a frame satisfies the condition, so do NOT re-propose. A SECOND approve by hand is now refused (returns `armed` with a note, no cash): only the capture loop\'s trigger identity can promote an armed intent, and only while the condition is met and still inside valid_until; an expired_untriggered / denied terminal or a kill-switch that is off is never revived. `queued` is a success, not a failure: the ticket is parked for an opening print (this session\'s for a 09:25–09:30 ticket, the next session\'s otherwise) and settles once the daily bar is published — read `note` for which session, and do not resubmit it. A ticket also dies with its session (`expires_at` 23:59:59 the day it was written): approving it on a later day returns `expired` rather than trading, so approve on the day you propose.',
      {
        ticket_id: { type: 'string', required: true, description: 'td-YYYYMMDD-xxxxxxxx' },
        confirm: { type: 'boolean', const: true, required: true, description: 'must be true' },
      },
      (args) => client.request(`/decision/${encodeURIComponent(args.ticket_id)}/approve`, {
        method: 'POST', body: { confirm: args.confirm },
      }),
    ),
    define(
      'quant_reject',
      'Reject a ticket with a reason. Use when the thesis no longer holds or the guard output is unacceptable.',
      {
        ticket_id: { type: 'string', required: true },
        reason: { type: 'string', required: true, description: 'why, >= 3 characters' },
      },
      (args) => client.request(`/decision/${encodeURIComponent(args.ticket_id)}/reject`, {
        method: 'POST',
        body: { reason: args.reason },
      }),
    ),
    define(
      'quant_cancel',
      'Cancel a QUEUED but not-yet-executed ticket (withdraw its remaining quantity), '
      + 'or withdraw an ARMED intent that has not been triggered yet — both set '
      + 'status="cancelled" with no order and no money spent. '
      + 'This is not a sell — to reduce a position use quant_propose(side=SELL). '
      + 'A `ceiling` order cannot be cancelled during the closing auction (14:57–15:00); '
      + 'an already-filled ticket reports status="filled" and is not undone.',
      {
        ticket_id: { type: 'string', required: true },
        reason: { type: 'string', description: 'why, optional' },
      },
      (args) => client.request(`/decision/${encodeURIComponent(args.ticket_id)}/cancel`, {
        method: 'POST',
        body: { reason: args.reason ?? '' },
      }),
    ),
    define(
      'quant_ticket',
      'Read one ticket with its computed qty/limit/stop, guard checks and fill.',
      { ticket_id: { type: 'string', required: true } },
      (args) => client.request(`/tickets/${encodeURIComponent(args.ticket_id)}`),
    ),
    define(
      'quant_tickets',
      [
        'List tickets newest first. Defaults to `status="open"` — proposed or queued, i.e. what you committed to',
        'and have not executed. This is the only place those appear: `quant_portfolio.pending` carries only',
        'tickets already parked for an opening print, and `quant_orders` carries only what reached the kernel,',
        'so a proposal you never approved used to be invisible to every tool. Use it to close a session:',
        'anything still open at 19:30 either gets executed, rejected with a reason, or explained in the journal.',
        'Rows are the brief form (no guard checks, no market snapshot). `quant_ticket` returns one full record —',
        'a full ticket is 2.8–3.8 KB, so a list of them would be shrunk to about five rows and still not answer',
        '"what did I leave open".',
      ].join(' '),
      {
        limit: { type: 'integer', description: '1-15 rows; default 10' },
        status: {
          type: 'string',
          enum: ['open', 'proposed', 'queued', 'filled', 'rejected', 'cancelled', 'expired'],
          description: 'default open (not terminal); a typo is rejected rather than returning an empty list',
        },
      },
      (args) => client.request('/tickets', {
        query: {
          limit: clampRows(args.limit, LIMITS.rows.tickets),
          status: args.status || 'open',
        },
      }),
      // 表格工具：超预算时先压字、后丢行。
      tabularOutput(),
    ),
    define(
      'quant_samples',
      [
        '研究样本账本：独立 2,000 本金、独立 sqlite、独立风控（1 只持仓、满仓、每天买一卖一）。',
        '返回账户、已结算笔数、距 20 笔还差多少、最近记录、限额。',
        '这个账本不并入主账户净值，主账户的 arena 判决也不读它：它的 KPI 是结算笔数，不是收益率。',
      ].join(' '),
      {},
      () => client.request('/samples'),
    ),
    define(
      'quant_samples_propose',
      [
        '在样本账本里提案。字段与 quant_propose 相同，但钱从独立的 2,000 里出，不碰主账户。',
        '一笔完整的「买入→卖出」才算 1 笔结算；每天最多 2 个动作（买一、卖一）。',
        '不要在这里写价格：限价与止损仍由服务算。',
      ].join(' '),
      {
        symbol: { type: 'string', required: true, description: '600000.SH' },
        side: { type: 'string', enum: ['BUY', 'SELL'], required: true },
        sleeve: { type: 'string', enum: ['CORE', 'AGGRESSIVE'], description: 'default CORE' },
        thesis: { type: 'string', required: true, description: '20-1200 characters' },
        evidence: {
          type: 'array',
          items: { type: 'string' },
          required: true,
          description: '1-8 evidence ids you actually read, e.g. daily:20261029',
        },
        invalidation: { type: 'string', required: true, description: '5-500 characters' },
        target_weight: { type: 'number', description: 'mutually exclusive with qty' },
        qty: { type: 'integer', description: 'omit on SELL to close' },
      },
      (args) => client.request('/samples/propose', {
        method: 'POST',
        body: {
          symbol: args.symbol,
          side: args.side,
          sleeve: args.sleeve,
          thesis: args.thesis,
          evidence: args.evidence,
          invalidation: args.invalidation,
          target_weight: args.target_weight,
          qty: args.qty,
        },
      }),
    ),
    define(
      'quant_samples_approve',
      '确认样本账本的票据。会重跑该账本自己的 guard（1 只持仓、满仓、每日 2 个动作），幂等。需 confirm=true。',
      {
        ticket_id: { type: 'string', required: true, description: 'td-YYYYMMDD-xxxxxxxx' },
        confirm: { type: 'boolean', const: true, required: true, description: 'must be true' },
      },
      (args) => client.request(`/samples/${encodeURIComponent(args.ticket_id)}/approve`, {
        method: 'POST', body: { confirm: args.confirm },
      }),
    ),
    define(
      'quant_samples_reject',
      '拒绝样本账本的票据并写明理由。',
      {
        ticket_id: { type: 'string', required: true },
        reason: { type: 'string', required: true, description: 'why, >= 3 characters' },
      },
      (args) => client.request(`/samples/${encodeURIComponent(args.ticket_id)}/reject`, {
        method: 'POST',
        body: { reason: args.reason },
      }),
    ),
    define(
      'quant_orders',
      'Recent paper orders and fills from the audit log.',
      { limit: { type: 'integer', description: 'rows, 1-80; default 50' } },
      (args) => client.request('/orders', {
        query: { limit: clampRows(args.limit, LIMITS.rows.orders) },
      }),
    ),
    define(
      'quant_journal_append',
      'Append a note to the trading journal (searchable later). Use for post-trade review and discipline notes. '
      + 'The response carries `verification`: every number in the text is matched against that day’s facts, '
      + 'and `unmatched` lists the ones the session cannot reproduce (percent and thousands-separator forms are '
      + 'both accepted; `sign_from_words` marks numbers whose direction only exists as a word). It never rejects — '
      + 'dropping numbers to pass a check would cost more than a wrong number — so read `unmatched` and fix the text. '
      + '`verification.claims` checks the counted ones against their label: `misplaced` is a real number under the '
      + 'wrong label (893 家跌停 — 跌停 was 93, 893 is advancers), `unbacked` is a number that label never had '
      + '(可买的 6 只 with in_cap 23 and buyable_at_close 0). Both are worth more than an `unmatched`: the first '
      + 'quotes a true number into a false sentence, the second invents one.',
      {
        text: { type: 'string', required: true },
        kind: { type: 'string', description: 'note / review / plan' },
        symbol: { type: 'string' },
        tags: { type: 'string', description: 'comma separated' },
      },
      (args) => client.request('/journal', {
        method: 'POST',
        body: { text: args.text, kind: args.kind, symbol: args.symbol, tags: args.tags },
      }),
    ),
    define(
      'quant_journal_search',
      'Search past journal entries by text, tag, symbol, kind or date. Compact rows: a head+tail summary plus the character count, with full text only for the newest few. The summary is head AND tail because these entries are written verdict-first and lesson-last — a review opens with the score and closes with "下次改一条：…"; reading only the head drops every action item. 300 chars or less comes back whole. To read one long entry, pass `id` alone: `full=true` returns the whole page at full length (about 5x, capped at 8 rows), so opening one 700-char review used to cost the page. Ceilings are measured: 20 summaries is 10 KB, 20 full texts is 34 KB — past the host ceiling, where the body is dropped whole rather than shortened; a clamped request says so in `capped`. Every row carries `verified` (the number check its text got when written); `null` means never checked.',
      {
        q: { type: 'string', description: 'keyword; empty returns latest' },
        kind: { type: 'string', enum: ['note', 'review', 'plan'], description: 'only this kind. "What did I conclude the last few evenings" is kind=review with limit=3 — filtering the latest 20 rows by hand stops working once notes outnumber reviews, which they do about 4:1' },
        since: { type: 'string', description: 'YYYYMMDD lower bound on the entry date (inclusive)' },
        until: { type: 'string', description: 'YYYYMMDD upper bound (inclusive); since=until=today answers "did I already review today"' },
        id: { type: 'integer', description: 'open exactly this entry, full text, nothing else. Cheapest way to read one long review' },
        limit: { type: 'integer', description: 'rows: 1-20 in summary mode, 1-8 with full=true; default 20' },
        recent: { type: 'integer', description: '0-5 newest entries with full text; default 3' },
        full: { type: 'boolean', description: 'full text for every returned row (about 5x larger, so the row cap drops to 8). Use id instead when you want one entry' },
      },
      async (args) => {
        const full = args.full === true
        const asked = Number(args.limit) || 20
        // 20 full entries measured 34,047 B; 8 is the last step under 16 KB.
        const limit = clampRows(asked, { min: 1, max: full ? 8 : 20, fallback: full ? 8 : 20 })
        // 0 是有效值（"不要全文"），clampRows 会把它当"没填"，所以单独处理。
        const askedRecent = args.recent === undefined || args.recent === null || args.recent === ''
          ? 3 : Number(args.recent)
        const recent = Number.isFinite(askedRecent)
          ? Math.min(Math.max(Math.trunc(askedRecent), 0), 5) : 3
        // 按 id 单取时，那一条就是要读的东西：不让"前 N 条带全文"的默认值把它
        // 变成摘要，否则一次 id 调用要配一次 full=true，白绕一圈。
        const single = args.id === undefined || args.id === null || args.id === ''
          ? null : Math.trunc(Number(args.id))
        const data = await client.request('/journal', {
          query: {
            q: args.q, limit, recent, full,
            kind: args.kind, since: args.since, until: args.until,
            id: Number.isFinite(single) ? single : undefined,
          },
        })
        if ((limit < asked || recent < askedRecent) && data && typeof data === 'object' && !Array.isArray(data)) {
          data.capped = {
            asked_limit: asked, served_limit: limit,
            asked_recent: askedRecent, served_recent: recent,
            why: '20 条全文实测 34,047 B，超过宿主 16 KB 上限会被整包丢弃；'
              + '要找更早的记录用 q 关键词检索，不要靠加大 limit',
          }
        }
        // 按 id 单取时服务端直接给全文，这里不需要再转一次。
        return data
      },
    ),
    define(
      'quant_reports',
      'Markdown under reports/ — the long form of the journal. Omit `path` to list (path/kind/title/bytes/modified); pass `path` to read one, paged. Journal entries say "全文见 reports/review/20261103.md"; until this tool existed those 119 files were write-only, so the detail a review points at was unreachable by the next session. Default page 6,000 chars: a review is 14.4 KB and the host drops a body over 16 KB whole, so a long report returns `truncated` + `next_offset`, never a silent cut. review/ carries the daily handoff; close/ open/ noon/ are session snapshots; daily/ is data quality; top-level reports/*.md are early one-off audits.',
      {
        path: { type: 'string', description: 'e.g. review/20261103.md — relative to reports/. Omit to list. Anything resolving outside reports/ is refused' },
        kind: { type: 'string', description: 'list only: filter by folder (review / close / open / noon / daily / external)' },
        offset: { type: 'integer', description: 'character offset for paging; pass next_offset from the previous page' },
        max_chars: { type: 'integer', description: 'page size 200-12000, default 6000' },
      },
      (args) => (args.path
        ? client.request('/reports/file', {
          query: { path: args.path, offset: args.offset, max_chars: args.max_chars },
        })
        : client.request('/reports', { query: { kind: args.kind } })),
    ),
    define(
      'quant_playbook',
      'Read the current trading playbook — this is the rule source of truth, and it '
        + 'overrides any paraphrase of the same rule in a prompt, a report or a '
        + 'memory. `version` is what is in effect right now, `text` is its body and '
        + '`sections` is the index of it: read `truncated` before quoting a rule — '
        + 'when it is true you only got up to `included`, and `next_section` names the '
        + 'chapter to fetch with section="9". A rule you did not read is not a rule '
        + 'you may cite. `drafts` are proposals that are NOT in effect: if '
        + '`draft_count` > 0 say so in the report, but never assume a draft is live, '
        + 'and check `is_fragment` before recommending that anyone activate one — a '
        + 'fragment draft replaces the rulebook with its own two chapters instead of '
        + 'extending it. `superseded` is the audit trail of drafts whose text already '
        + 'landed in a live version.',
      {
        section: {
          type: 'string',
          description: 'chapter numbers, comma separated, e.g. "0,2,9"; omit for the whole book (cut at the byte budget)',
        },
      },
      (args) => client.request('/playbook', { query: { section: args.section } }),
    ),
    define(
      'quant_playbook_propose',
      'Propose a new playbook version as a draft. Drafts never take effect until a human activates them.',
      {
        text: { type: 'string', required: true, description: 'full markdown, >= 50 chars' },
        note: { type: 'string', description: 'what changed and why' },
      },
      (args) => client.request('/playbook/propose', {
        method: 'POST',
        body: { text: args.text, note: args.note || '' },
      }),
    ),
    define(
      'quant_memory_search',
      'Search past journal notes and closed trade cases. Use before repeating a mistake or claiming "this time is different". Each note carries `verified` — the number check its text got when written (checked / matched / unmatched_count); `null` means never checked, which is not the same as checked-and-fine, so do not quote its figures as established. A page-level `unchecked` counts rows with no verdict.',
      {
        q: { type: 'string', description: 'keyword, Chinese substring ok' },
        limit: { type: 'integer', description: 'rows: 1-12 in summary mode, 1-6 with full=true; default 10' },
        recent: { type: 'integer', description: '0-10 newest journal entries with full text; default 2' },
        full: { type: 'boolean', description: 'full text for every row (about 3x larger, so the row cap drops to 6)' },
      },
      (args) => {
        const full = args.full === true
        const askedRecent = args.recent === undefined || args.recent === null || args.recent === ''
          ? null : Math.trunc(Number(args.recent))
        // 0 是有效值（"不要全文"），clampRows 会把它当"没填"，所以单独处理。
        const recent = Number.isFinite(askedRecent)
          ? Math.min(Math.max(askedRecent, 0), 10) : undefined
        return paged(args.limit, full ? LIMITS.rows.memoryFull : LIMITS.rows.memory, (limit) => (
          client.request('/memory/search', {
            query: { q: args.q, limit, recent, full },
          })
        ), '20 条摘要实测 16,825 B、50 条 35,441 B，超过宿主 16 KB 上限会被整包丢弃；'
          + '找更早的记录用 q 关键词，不要加大 limit')
      },
    ),
    define(
      'quant_cases',
      'Closed round trips with entry/exit, PnL, thesis and invalidation — the case library for review. With `attribution` each of the newest five rows also carries the seven parts that produced its return: entry timing, market, industry, idiosyncratic, dividend, exit timing, and cost. They sum exactly to pnl_pct, so "跑输 1.2%" becomes "选股 −1.4%、行业 +4.1%、成本 −0.6%". The aggregate is withheld until there are five cases — one trade’s component average is not a statistic.',
      {
        limit: { type: 'integer', description: 'rows, 1-50; default 20' },
        symbol: { type: 'string', description: 'filter by symbol' },
        attribution: { type: 'boolean', description: 'decompose the newest 5 rows; adds about 300 B each' },
      },
      (args) => client.request('/cases', {
        query: {
          limit: clampRows(args.limit, LIMITS.rows.cases), symbol: args.symbol,
          attribution: args.attribution,
        },
      }),
    ),
    define(
      'quant_hypothesis',
      'Register, list, or settle falsifiable hypotheses. A hypothesis needs a test spec; only statistics can promote it, not opinion — and action=result is the only path that changes its status. Leave it open and it stays open forever: there used to be no way to record an outcome at all, so every hypothesis the desk ever wrote was still "draft".',
      {
        action: { type: 'string', enum: ['list', 'add', 'result'], required: true },
        statement: { type: 'string', description: 'for add: one falsifiable sentence' },
        rationale: { type: 'string', description: 'for add: why you believe it' },
        test_spec: { type: 'string', description: 'for add: how to test it on stored data' },
        hyp_id: { type: 'integer', description: 'for result: the id from action=list' },
        status: {
          type: 'string', enum: ['testing', 'promoted', 'rejected'],
          description: 'for result: testing = not enough evidence yet, promoted = the test cleared its bar, rejected = it did not',
        },
        metrics: {
          type: 'object', additionalProperties: true,
          description: 'for result: the numbers the test produced (n, mean_excess, p, DSR…). A status without numbers is an opinion',
        },
        notes: { type: 'string', description: 'for result: what would have changed your mind' },
        limit: { type: 'integer', description: 'for list: rows, 1-60 as summaries or 1-20 with full=true; default 20' },
        full: {
          type: 'boolean',
          description: 'for list: include test_spec and the full result blob. Five rows is 11 KB that way and 1 KB as a summary; ask for it when you intend to re-run one, not to browse',
        },
      },
      (args) => {
        if (args.action === 'list') {
          return paged(
            args.limit,
            args.full === true ? LIMITS.rows.hypothesesFull : LIMITS.rows.hypotheses,
            (limit) => client.request('/research/hypotheses', {
              query: { limit, full: args.full },
            }),
            'full=true 的 20 条实测 10,638 B；服务端允许 200 条（约 104 KB），'
              + '超过宿主上限会被整包丢弃',
          )
        }
        if (args.action === 'result') {
          if (!args.hyp_id) throw new Error('hyp_id is required when action=result')
          if (!args.status) throw new Error('status is required when action=result')
          return client.request(
            `/research/hypotheses/${encodeURIComponent(args.hyp_id)}/result`,
            {
              method: 'POST',
              body: {
                status: args.status,
                metrics: args.metrics || {},
                notes: args.notes || '',
              },
            },
          )
        }
        if (!args.statement) throw new Error('statement is required when action=add')
        return client.request('/research/hypotheses', {
          method: 'POST',
          body: {
            statement: args.statement,
            rationale: args.rationale || '',
            test_spec: args.test_spec || '',
          },
        })
      },
    ),
    define(
      'quant_lineage',
      'Exact data snapshot ids that served a given date. Include them in every proposal\'s evidence.',
      { date: { type: 'string', required: true, description: 'YYYYMMDD' } },
      (args) => client.request(`/data/lineage/${encodeURIComponent(args.date)}`),
    ),
    define(
      'quant_discover',
      'Raw facts to think with: breadth, limit-up structure, industry dispersion, gap behaviour today, core regime, data health. Not a signal — form your own view.',
      { as_of: { type: 'string', description: 'YYYYMMDD, default latest' } },
      (args) => client.request('/discover', { query: { as_of: args.as_of } }),
    ),
    define(
      'quant_core',
      'The core ETF book: NAV, cash weight, target weights, regime, recent trades. RETIRED on 2026-11-01 — its defensive leg (511010, one lot = 14,112) costs 2.8x the account, so every defensive day wrote a 100%-cash NAV row that looked like a choice. `state` now reads "retired"; `executable` still carries price / lot_cost / share_of_nav / required_nav so the reason stays checkable. Reports only; nothing here is executable.',
      {},
      () => client.request('/core'),
    ),
    define(
      'quant_sector',
      '申万一级行业榜（31 行）：涨跌幅（市值加权 pct_chg / 等权 pct_chg_eq）、成交额、主力净流入。'
      + '回答"哪条线有资金"。两条口径不可混用：这里的资金流是**机构口径**（超大单+大单净额，按单笔金额分类，不含营业部席位），'
      + '不能用它判断游资行为——游资看连板高度、首封时间、封单/成交比（quant_pools / quant_limit_structure）。'
      + 'moneyflow 收盘后发布，盘中读到的是上一交易日的数，每行 flow_as_of 写明来源日期。',
      {
        date: { type: 'string', description: 'YYYYMMDD; default latest stored session' },
      },
      (args) => client.request('/market/sector', {
        query: { day: args.date || undefined },
      }),
    ),
    define(
      'quant_universe',
      'Browse the whole market with raw facts (turnover, volume ratio, float cap, PE/PB, industry, 20d range). No ranking, no advice — you decide what to look at and why. Each row carries the vendor label plus 申万 L1/L3, and the industry filter matches either. '
      + 'Every row states its board (MAIN / CHINEXT / STAR / BSE) and band, the daily limit as a fraction — 0.10 main board (ST included: the exchange moved main-board ST to 10% on 2026-07-06), 0.20 创业板/科创板, 0.30 北交所. Band is not decoration: whether +8% is 2 points or 12 points from the ceiling decides the stop distance. 北交所 names are listed (they are part of the tape) but marked tradeable=false, and the guard rejects any buy on them — a proposal there comes back with failed=[board].',
      {
        as_of: { type: 'string', description: 'YYYYMMDD, default latest' },
        industry: {
          type: 'string',
          description: [
            'substring match on either taxonomy: 银行 (vendor, 42 names) or',
            '城商行 (申万 L3, 17 names). Several boards share one sector label',
            'but are two different businesses; L3 is how you say which one.',
          ].join(' '),
        },
        query: { type: 'string', description: 'symbol or name substring' },
        min_amount: { type: 'number', description: 'minimum turnover in CNY' },
        max_price: { type: 'number', description: 'maximum price per share' },
        limit: {
          type: 'integer',
          description: 'rows, default 30 (~10.6 KB). Measured 2026-11-01: 40 rows is 14.0 KB, 50 rows is 17.5 KB — past the 16 KB host ceiling, and an oversized body is dropped whole rather than truncated. Clamped to 1-40',
        },
        order: { type: 'string', enum: ['code', 'amount', 'move'], description: 'display order only' },
      },
      (args) => client.request('/universe', {
        query: {
          as_of: args.as_of, industry: args.industry, query: args.query,
          min_amount: args.min_amount, max_price: args.max_price,
          // Same clamp as /market/pools: the service allows 200 rows (~67 KB) and
          // the host drops anything over 16 KB whole, so an honest "give me
          // everything" would come back empty.
          limit: clampRows(args.limit, LIMITS.rows.universe),
          order: args.order,
        },
      }),
    ),
    define(
      'quant_strategies',
      'Which strategies exist, which may trade in this regime, and how much capital each is allowed (with the reason for every weight). Read it before sizing anything.',
      {},
      () => client.request('/strategies'),
    ),
    define(
      'quant_decay',
      'Has the realised record of a live strategy drifted from what its evidence promised? Returns state (ok / insufficient / no_baseline / decaying), the sample size, the baseline it is compared against, and a note saying why it cannot answer yet. Facts only — a decaying strategy is not auto-liquidated and not re-weighted; the state is for you and the operator.',
      {
        strategy: { type: 'string', description: 'filter to one strategy key' },
        window: { type: 'integer', description: 'sessions in the rolling window, 10-60, default 20' },
      },
      (args) => client.request('/decay', {
        query: { strategy: args.strategy, window: args.window },
      }),
    ),
    define(
      'quant_execution_estimate',
      'Pre-trade cost for a given quantity: square-root market impact, participation rate and a TWAP liquidity schedule, plus explicit fillability boundaries (cash_free vs notional, whole lot, daily-limit queue flag) and complete round-trip cost. The schedule is NOT proof that the quantity can fill; queue position and fill probability remain unknown, and proposal/ticket guard is the final authority. Call it before proposing something large relative to ADV — above 30% participation the order is split across days. Realised cost comes from quant_execution_tca. `price` is a what-if input: passing one makes the reply a scenario, and `price_source` says so (`caller` vs `server_last`). Never quote either as the market price — that is quant_quote.',
      {
        symbol: { type: 'string', required: true },
        qty: { type: 'integer', required: true, description: 'shares to trade' },
        side: { type: 'string', description: 'BUY (default) or SELL; used only for the limit-queue reference check' },
        price: { type: 'number', description: 'optional, >0; a what-if price for the scenario, NOT a quote. Defaults to the latest close, and the reply says which it used' },
      },
      (args) => client.request('/execution/estimate', {
        query: { symbol: args.symbol, qty: args.qty, price: args.price, side: args.side },
      }),
    ),
    define(
      'quant_execution_tca',
      'Post-trade implementation shortfall per fill, measured against the price the decision was made at. This is the other half of execution_estimate: one predicts what an order will cost, this one reports what it did cost. A desk calibrated only on its own predictions never learns whether they were right.',
      { limit: { type: 'integer', description: 'fills to inspect, 1-60; default 60' } },
      (args) => paged(args.limit, LIMITS.rows.tca, (limit) => (
        client.request('/execution/tca', { query: { limit } })
      ), '一行 212 B：200 行（服务端默认）约 42 KB，超过宿主 16 KB 上限会被整包丢弃'),
      tabularOutput(),
    ),
    define(
      'quant_attribution',
      'PnL, win rate and market-excess per strategy, plus the same numbers per persona. Use it to answer "is this sleeve, or this style of deciding, earning its capital".',
      {},
      () => client.request('/attribution'),
    ),
    define(
      'quant_persona',
      'Which decision style is active right now (hot_money / retail / trader), what each style may spend, whether its data is present, and whether earlier switches actually made money. Read it before proposing: the persona decides the sleeve and the horizon.',
      {},
      () => client.request('/persona'),
    ),
    define(
      'quant_dashboard',
      'One page of "what the desk did today and what stopped it": the day timeline (proposals, fills, refusals, memos, persona switches, reviews), account, work queue, arena verdict, active persona and data readiness. Requests the slim shape: the full page runs ~24.5KB and a host that refuses oversized tool output returns nothing at all. The optional-dataset detail lives in quant_data_health.',
      {},
      () => client.request('/dashboard.json', { query: { slim: 1 } }),
    ),
    define(
      'quant_persona_switch',
      'Change the active decision style. Costs four things: reason, at least one evidence id, expected_edge_bps, and invalidation. Capped at two switches per trading day. Switching is a decision that gets scored afterwards, not a way to relabel a losing day.',
      {
        to: { type: 'string', enum: ['hot_money', 'retail', 'trader'], required: true },
        reason: { type: 'string', required: true, description: '20-800 characters' },
        evidence: {
          type: 'array', items: { type: 'string' }, required: true,
          description: '1-8 evidence ids, e.g. daily:20261026 / pools:20261026',
        },
        expected_edge_bps: {
          type: 'number', required: true,
          description: 'expected style improvement in bps (-500 to 500), used for later scoring',
        },
        invalidation: {
          type: 'string', required: true,
          description: 'what would prove the choice wrong (5-500 characters)',
        },
        horizon_days: {
          type: 'integer',
          description: 'how long the style should be judged over (1-60)',
        },
        regime: {
          type: 'string', enum: ['', 'risk_on', 'neutral', 'risk_off', 'stressed'],
          description: 'regime label you are switching in',
        },
      },
      (args) => client.request('/persona/switch', {
        method: 'POST',
        body: {
          to: args.to,
          reason: args.reason,
          evidence: args.evidence,
          expected_edge_bps: args.expected_edge_bps,
          invalidation: args.invalidation,
          horizon_days: args.horizon_days,
          regime: args.regime || '',
        },
      }),
    ),
    define(
      'quant_duty',
      'Your work queue for today: waiting data, queued orders, positions near a stop, memos due, open hypotheses, missing review, regime gate. Two fields answer two questions — `blocks` is the fact (`all_new_entries` means every new entry is rejected until you deal with it; `entries_in_subject` only blocks adding to that bucket; null blocks nothing), `priority` is an editorial ordering I made up. Blocking items sort first because they do not self-heal, not because they matter more. Read this first so you do not invent work.',
      {},
      () => client.request('/duty'),
    ),
    define(
      'quant_regime',
      'One market label (risk_on | neutral | risk_off | stressed) plus trend, volatility percentile and breadth. Use it to decide whether a setup is allowed to size up at all.',
      {},
      () => client.request('/regime'),
    ),
    define(
      'quant_exposure',
      'What the book is actually exposed to: industry and board weights, concentration (top1/top3/HHI) and beta vs the equal-weight market.',
      {},
      () => client.request('/risk/exposure'),
    ),
    define(
      'quant_stops',
      'Every open position that carries a stop, how far it is from it, and whether a breach is blocked by T+1. Run this before adding risk. '
      + '`account="external"` reads the live read-only book (data/external.sqlite) instead of the paper one — the kernel cannot see a live position '
      + 'otherwise, and on 2026-11-02 that meant 79% of the account had no stop monitored all afternoon. Check `stale_book`: true means the last '
      + 'recorded fill predates today, so the list is what the book knows, not necessarily what you hold.',
      {
        account: {
          type: 'string',
          enum: ['paper', 'external'],
          description: 'paper (default) = the simulated book; external = the live account, read-only',
        },
        rule: {
          type: 'string',
          enum: ['pct5', 'atr'],
          description: 'pct5 = cost -5%; atr = cost - 2.5 x ATR(14). A pinned price in configs/stops.json wins over both.',
        },
      },
      (args) => client.request('/risk/stops', {
        query: { account: args.account || 'paper', rule: args.rule || 'pct5' },
      }),
    ),
    define(
      'quant_arena',
      'Scoreboard: your book vs the core ETF book vs the market, plus per-decision excess with a bootstrap interval. Read this before claiming you have an edge.',
      {},
      () => client.request('/arena'),
    ),
    define(
      'quant_memo',
      'Your reasoning record: thesis + mechanism + falsifier + probabilistic predictions. score resolves predictions against later data and returns a Brier score. Read `bss`, not the raw score: bss = 1 - brier/brier_climatology, where brier_climatology is s(1-s) and s is the base rate of this sample (see base_rate). 0 means no better than always predicting the base rate, negative means worse. Do not use 0.5 as the reference — that is the s=0.5 special case and the weakest baseline there is; on an imbalanced sample it flatters you. And a difference (reference minus brier) is not a skill score. bss is null when every outcome landed on the same side: undefined, not zero. Use this for the decision NOT to act too: nothing else in the desk scores restraint, so a day with no trade and no memo is a day with no evidence about your judgement.',
      {
        action: { type: 'string', enum: ['list', 'add', 'score', 'calibration'], required: true },
        topic: { type: 'string', description: 'for add: short title' },
        thesis: { type: 'string', description: 'for add: what you believe and why' },
        mechanism: { type: 'string', description: 'for add: causal chain, not correlation' },
        falsifier: { type: 'string', description: 'for add: what would prove you wrong' },
        confidence: { type: 'number', description: 'for add: 0..1' },
        limit: { type: 'integer', description: 'for list: rows, default 20, clamped 1-50' },
        predictions: {
          type: 'string',
          description: 'for add: JSON array of {claim, kind: market_up|symbol_up|core_up, horizon, probability, symbol?}',
        },
      },
      (args) => {
        if (args.action === 'list') {
          return client.request('/memo', {
            query: { limit: clampRows(args.limit, LIMITS.rows.memo) },
          })
        }
        if (args.action === 'score') return client.request('/memo/score', { method: 'POST' })
        if (args.action === 'calibration') return client.request('/memo/calibration')
        if (!args.topic || !args.thesis) throw new Error('topic and thesis are required when action=add')
        let predictions = []
        if (args.predictions) {
          try {
            predictions = typeof args.predictions === 'string'
              ? JSON.parse(args.predictions)
              : args.predictions
          } catch (err) {
            throw new Error(`predictions must be a JSON array: ${err.message}`)
          }
        }
        return client.request('/memo', {
          method: 'POST',
          body: {
            topic: args.topic,
            thesis: args.thesis,
            mechanism: args.mechanism || '',
            falsifier: args.falsifier || '',
            confidence: args.confidence ?? 0.5,
            predictions,
          },
        })
      },
    ),
    define(
      'quant_declined',
      'Record what you looked at and did NOT buy, and score it later. This is the fourth scoreable judgement: proposals are settled by arena (excess vs the equal-weight market), persona switches by the scorecard, hypotheses by the referee — a decision not to act had nowhere to land, so restraint was never measured. 2026-11-01..03 produced zero trades and zero records: three days of judgement with no evidence. `add` takes symbols, a reason, an optional invalidation and a horizon in sessions; each symbol is recorded at most once per day (a repeat add returns the existing row and lists the names in `skipped`); it takes NO price — the settlement reads the closing prices itself, so a declined basket cannot be scored against a price you picked afterwards. `score` settles matured rows: per symbol it gives the realised return over the horizon, the equal-weight market over the same window, and the difference. Negative mean_excess means those names underperformed the market — not buying was right that time; positive means restraint cost you. That is arithmetic, not a verdict. `list` shows rows plus a summary with the observation count and the share that underperformed; observers are not independent (same-day names move together), so read n as observations, not as a sample size. Nothing here is a gate: it never tells you to trade or not to.',
      {
        action: { type: 'string', enum: ['add', 'list', 'score'], required: true },
        symbols: {
          type: 'array',
          items: { type: 'string' },
          description: 'for add: the names you nearly bought, with exchange suffix (600000.SH), 1-30. Not the whole candidate list — the few you actually weighed',
        },
        reason: { type: 'string', description: 'for add: why you did not act. Required: without it the settled row cannot be read later' },
        invalidation: { type: 'string', description: 'for add: what would have made you act' },
        horizon_days: { type: 'integer', description: 'for add: sessions to hold the counterfactual, 1-20, default 3' },
        day: { type: 'string', description: 'for add: YYYYMMDD, default the book day; the session whose close you were looking at' },
        settled: { type: 'boolean', description: 'for list: true = only settled, false = only pending' },
        limit: { type: 'integer', description: 'for list: rows, default 50' },
        dry_run: { type: 'boolean', description: 'for score: compute without writing the result back' },
      },
      (args) => {
        if (args.action === 'add') {
          return client.request('/declined', {
            method: 'POST',
            body: {
              symbols: args.symbols,
              reason: args.reason,
              invalidation: args.invalidation || '',
              horizon_days: args.horizon_days ?? 3,
              day: args.day,
            },
          })
        }
        if (args.action === 'score') {
          return client.request('/declined/score', {
            method: 'POST',
            query: { dry_run: args.dry_run },
          })
        }
        return client.request('/declined', {
          query: { limit: clampRows(args.limit, LIMITS.rows.memo), settled: args.settled },
        })
      },
    ),
    define(
      'quant_watch',
      'Cheap wake gate for a scheduled session. Returns findings only where a threshold was crossed: stop breached or within 2%, a holding that moved 5% today, a queued order within 1% of its limit, and an extreme limit-up tape. Empty findings means nothing happened — answer one line and end the turn there, **but a 巡检 still writes that line to the journal** (quant_journal_append, tags=watch): the cross-day stop chain reads the journal, not the turn. A dead quote feed shows up as quotes_unavailable, not as "all clear". Pass `since` and it also subtracts that day from today: what changed in breadth, pool counts, what the price cap left buyable, the account, and which 申万 industries moved 3+ places. "Did anything cross a threshold" and "what changed" are different questions, and a wake-up needs the second one.',
      {
        as_of: { type: 'string', description: 'YYYYMMDD, default latest trading day' },
        since: { type: 'string', description: 'YYYYMMDD; when given, adds a changes block (last time you looked). About 0.9s warm, up to 4s cold' },
        include_tape: { type: 'boolean', description: 'include limit-up/seal-rate extremes, default true' },
      },
      (args) => client.request('/watch', {
        query: { as_of: args.as_of, since: args.since, include_tape: args.include_tape },
      }),
    ),
    define(
      'quant_observations',
      'The state of the book as measured facts: stops breached (with the cost of exiting at the next open and how this stock behaved after similar down days), caps pressed, the registered core rule vs the actual weights (with the order list and fees), and remaining risk budget per strategy. No recommendation field exists — read it and decide.',
      {},
      () => client.request('/observations'),
    ),
    define(
      'quant_alert_handoff',
      'The position-alert monitor is waiting for a reading, and you are the one who gives it. Call with no arguments to list pending alerts: each item carries a compact prompt (numbered facts, quoted plan clauses, the book) small enough to answer in one pass — read it, then answer in three sentences that each cite a number from the prompt, and say what is missing rather than filling it in. Submitting the answer sends the mail; nothing else does. Use at most once per pending id, and never invent a price or event the prompt did not contain.',
      {
        id: { type: 'string', description: 'pending id from the list; omit to list' },
        analysis: { type: 'string', description: 'the three-sentence reading; omit to just read' },
      },
      (args) => (args.id && args.analysis
        ? client.request('/monitor/handoff/answer', {
          method: 'POST',
          body: { id: args.id, analysis: args.analysis },
        })
        : client.request('/monitor/handoff')),
    ),
    define(
      'quant_core_plan',
      'RETIRED 2026-11-01: returns 409 CORE_RETIRED. Kept as a name so old calls fail with a reason instead of a 404. The sleeve was closed because its defensive leg (511010) costs 2.8x the account per lot; see quant_core for the numbers.',
      { force: { type: 'boolean', description: 'show the allocation even if the rule is not due' } },
      (args) => client.request('/core/plan', { method: 'POST', query: { force: args.force } }),
    ),
    define(
      'quant_core_preview',
      'Price YOUR OWN target weights for the core ETF book into whole lots before committing: resulting orders, fees, cash weight, and the two reasons a weight can buy nothing (one lot too expensive, or the target is smaller than one lot). Nothing is booked.',
      {
        targets: { type: 'object', required: true, additionalProperties: true,
                   description: 'ETF ts_code -> target weight, e.g. {"510300.SH": 0.25, "518880.SH": 0.15}; the rest stays in cash, so the sum must be <= 1' },
        as_of: { type: 'string', description: 'YYYYMMDD, default latest bar' },
      },
      (args) => client.request('/core/preview', {
        method: 'POST',
        body: { targets: args.targets, as_of: args.as_of },
      }),
    ),
    define(
      'quant_core_execute',
      'RETIRED 2026-11-01: returns 409 CORE_RETIRED, so no core order can be booked. Individual stocks still go through quant_propose / quant_approve. Use quant_core_preview to check what a weight would cost in lots — that endpoint is pure and still open.',
      {
        targets: { type: 'object', required: true, additionalProperties: true,
                   description: 'ETF ts_code -> target weight, sum <= 1; the remainder stays in cash' },
        thesis: { type: 'string', required: true, description: 'why this allocation, >= 20 chars' },
        evidence: { type: 'array', items: { type: 'string' }, required: true },
        invalidation: { type: 'string', required: true, description: 'what would make this the wrong call' },
      },
      (args) => client.request('/core/execute', {
        method: 'POST',
        body: { targets: args.targets, thesis: args.thesis,
                evidence: args.evidence, invalidation: args.invalidation },
      }),
    ),
    define(
      'quant_knowledge',
      'Ask the kernel what it knows. Two kinds of topic; the difference decides whether you pass `symbol`. Market-level: cards (map), ledger (measured setups: exact spec + sample + numbers; `gate_reading` is the mechanical reading of a pre-registered threshold, not a conclusion — the decision is yours), regime (what each market state paid), cycle (same 炸板率/晋级率 档 → next 1 and 3 sessions, with n_days and n_episodes because consecutive days are not independent), industries (申万 L1: return, breadth, limit-ups, main-capital flow — read the four together), limit_structure (seal rate, ladder depth, per-board promotion, percentile), tape_flow (yesterday’s limit-ups today: open premium, red ratio, promotion by board count — the 打板 on/off switch, measured against yesterday’s limit price), seal_tape (封单 as a process: which frame the count peaked, whose seal was eaten into the close; needs pool_tape frames, so only densely recorded sessions answer; curve is time-ordered and paginated with offset/limit, follow market.pagination.next_offset), auction, anomaly, lhb (+ seat records), board_check (out-of-sample check of `board`), risk, news, filings. Per-instrument (need `symbol`): crowding, position, liquidity, distribution, analog, board, industry_rank, timing_distribution (read-only conditional forward-return buckets from intraday_features: n, p25/median/p75 and `insufficient` when n < 30; no action, no verdict, no target price; one partition per trading day, so a single day is almost always insufficient). `position` also carries `stop_risk_context`: stop width as a multiple of ATR20, risk-to-stop vs the per-ticket risk budget, and a what-if for a 2.5xATR stop on one lot — pure arithmetic, no action or recommendation; it is null when ATR/limits are unavailable. `board` is the 封板体检: board count, seal vs float, first-seal window, theme cluster, and the forward distribution of same-structure history with two exit legs (T+1 open vs close, ~1.2pp apart in mean and ~7.7pp at p10) plus a ladder showing how n shrinks as the condition tightens; mean and reseal-rate intervals use trade-day block bootstrap, so read n_days beside n and never treat same-day limit-ups as independent. `factors` is the read-only cross-sectional factor layer: 9 pre-registered factors (momentum, reversal, volatility, turnover, size, book-to-price, earnings yield, 5-day main-capital flow, Amihud illiquidity) scored over 1/3/5/10-session horizons with RankIC, ICIR, a Newey-West t that corrects for overlapping horizons, quintile monotonicity, IC decay and industry+log-size neutralisation; every (factor, horizon) p-value goes through Benjamini-Hochberg and survivors also need |t|>3. Pass `horizons=dense` (or `1,2,3,4,5`) for a continuous 1-to-5-session read including 2 and 4 days: that is a DIFFERENT 45-test family, not a richer view of the 36-test default, so its q-values are not comparable with the on-book conclusions of the default family and `family_scope` says so. It uses daily_basic and moneyflow, which previously only fed coverage scores. There is no signal, verdict, score, ranking or target price anywhere in it, and an empty survivor list is a normal honest answer. `market_state` is the market picture plus what followed similar pictures: five metrics (成交额/20日均值, 20日动量, 上涨家数占比, 截面波动, 两融5日变化) on ROLLING percentiles, then the 5/10/20-session equal-weight market return distribution that followed the k=120 historically closest states (n, p25/median/p75, share positive, IQR width). No action, score, ranking or timing signal: a low percentile is not cheap, a positive median is not a forecast, and the IQR is reported alongside the median because a wide interval makes the median almost uninformative. Unlike `regime` (one current label), this answers when it last looked like this and what happened next. `breadth` is the read-only data-breadth gap: which datasets actually enter the alpha estimate (9 of 46 ingested), which short-horizon sets such as intraday_features / stk_mins / auction / LHB seats are still unused and why, and the rule that wiring any of them in is a NEW test that must be registered in the trial ledger first. `ledger` now carries two extra read-only fields on every measured row: `fill_scope` (the execution assumption behind the numbers, including the optimistic t_close limit-up fill whose bias was documented in spec.py but absent from the stored verdict) and `alpha_reading` (signal-book Sharpe vs account-implementable alpha, with the 1x/2x/3x cost collapse). Neither changes `gate_reading` or `reasons`. Use `detail=full` on ledger only when reading a file directly: the full payload exceeds the host body ceiling. Every answer carries caveats and provenance.',
      {
        topic: {
          type: 'string',
          required: true,
          description: 'market-level: cards / ledger / regime / cycle / industries / limit_structure / tape_flow / seal_tape / auction / anomaly / lhb / board_check / risk / news / filings / factors / breadth / market_state — per-instrument: crowding / position / liquidity / distribution / analog / board / industry_rank / timing_distribution. The per-instrument eight need `symbol`; the market-level ones ignore it. `factors` is read-only cross-sectional factor reads (RankIC / ICIR / HAC t / layer monotonicity / decay, industry+size neutralised, BH corrected): it has no signal, verdict, score or target price, and an empty survivor list is a normal honest answer. `breadth` reports the 46-dataset coverage gap and the integration rule, with no new tests. `market_state` is the market picture (five metrics on rolling percentiles) plus the forward return distribution of the k closest historical states, with IQR width beside every median — no action, no score, no timing signal',
        },
        window: {
          type: 'integer',
          description: 'factors only: lookback in trading sessions, 60-500; default 120. Short windows return short_calendar rather than a fabricated IC',
        },
        factor: {
          type: 'string',
          description: 'factors only: one pre-registered factor key (mom20 / reversal_5 / vol20 / turnover20 / log_mcap / bp / ep / net_mf_5 / amihud_20). Naming one returns full horizon + layer detail for that factor',
        },
        detail: {
          type: 'string',
          description: 'factors only: summary (default, ~7 KB) or full (~27 KB, can exceed the host body ceiling — prefer naming a single factor instead)',
        },
        horizons: {
          type: 'string',
          description: 'factors only: horizon family. `default` (or omit) = 1,3,5,10 sessions, the 36-test family used by every delivered report; `dense` / `1-5` = 1,2,3,4,5 (45 tests, includes 2 and 4 days); or a comma-separated list such as `1,2,3,4,5,10` (1-20 sessions, max 8). Every added horizon is an added hypothesis test inside the BH family, so results from a non-default family are explicitly marked not comparable with the default one',
        },
        symbol: {
          type: 'string',
          description: 'REQUIRED for crowding, position, liquidity, distribution, analog, board, industry_rank, timing_distribution. Omitting it there returns {need:"symbol", example:{...}} rather than an answer — those facts are computed one name at a time, there is no market-wide version',
        },
        days: { type: 'integer', description: 'news lookback 1-120 days; default 7' },
        limit: { type: 'integer', description: 'rows, 1-40; default 20. For seal_tape this is curve frames per page' },
        offset: { type: 'integer', description: 'seal_tape only: zero-based curve offset; use market.pagination.next_offset to read the early/middle/late path without host clipping' },
        // 没有这个参数时，FastAPI 对未知查询串是**静默忽略**的：技能里写
        // `as_of=今天` 也会被丢掉，服务端返回"最近一次入库"，可能是前一交易日。
        // 打板最依赖的两个 topic（tape_flow 的昨涨停今日溢价、seal_tape 的封单过程）
        // 恰好都按日取数，错一天就没有任何提示。
        as_of: {
          type: 'string',
          description: 'YYYYMMDD. Default is the latest ingested row, which is not always the session you mean. Pass it for any question about a specific day',
        },
      },
      (args) => client.request(`/knowledge/${encodeURIComponent(args.topic)}`, {
        query: { symbol: args.symbol, days: args.days, as_of: args.as_of,
                 limit: clampRows(args.limit, LIMITS.rows.knowledge),
                 offset: args.offset,
                 window: args.window, factor: args.factor, detail: args.detail,
                 horizons: args.horizons },
      }),
      tabularOutput(),
    ),
    define(
      'quant_limit_structure',
      'The limit-up tape for a session: how many sealed, how many broke, seal rate, how deep the streak ladder went, per-board promotion rate, top limit reasons, strongest seals ranked by 封单/流通市值 (seal_rank_basis says which scale was used), and the last few sessions for trend. counts.limit_up, max_streak and promotion all come from the derived pool (close == up_limit); counts.vendor keeps the 同花顺 numbers as published, which run 1.6 names low on an average day and 16 low on 2026-10-26. Seal money, first-seal time and the reasons only exist in the vendor feed, so one response mixes two sources — the caveats say so. `setup_thresholds` gives, per board height, the historical re-seal rate, the rate you would need just to break even at this account\'s actual ticket size, the gap in pp, and how many of those boards were buyable at all. There is no has_edge / no_edge label any more: a positive gap means the actual rate sits below that threshold, a negative one means it clears it — the trade is yours to decide. Its reseal_rate is the **buyable** group only — a limit order fills when price trades through the limit, so the 一字 boards that never traded below it (2.2% of first boards, 28.6% at 7 boards, reseal rate 100% by construction) are excluded; including them flips the sign of the height trend (gap narrows 19.2→12.7pp on the full sample, widens 21.7→31.0pp on buyable). reseal_rate_all is there to reconcile. Read it before choosing a height, because "3 板强封单 +2.91%" is a conditional mean, not an expectancy. It also states what it still cannot answer (queue fill probability).',
      { as_of: { type: 'string', description: 'YYYYMMDD, default latest bar' } },
      (args) => client.request('/knowledge/limit_structure', { query: { as_of: args.as_of } }),
      tabularOutput(),
    ),
    define(
      'quant_lhb',
      'Dragon-tiger board: which names were bought, net amounts, institutions versus hot money, the seat names (游资), plus the top seats of the day. data_date tells you which session it is actually for — the board is published after the close. Pass seat to also get that seat\'s track record: how many times it appeared, and what the next open and next close returned after each appearance.',
      {
        symbol: { type: 'string', description: 'filter to one ts_code' },
        limit: { type: 'integer', description: 'rows, 1-40; default 20' },
        as_of: { type: 'string', description: 'YYYYMMDD, default latest bar' },
        seat: { type: 'string', description: 'seat nickname from top_seats, e.g. T王 — adds seat_record over the last 60 sessions' },
      },
      (args) => client.request('/knowledge/lhb', {
        query: { symbol: args.symbol,
                 limit: clampRows(args.limit, LIMITS.rows.lhb),
                 as_of: args.as_of, seat: args.seat },
      }),
      tabularOutput(),
    ),
    define(
      'quant_auction',
      'Pre-open call auction alerts: who was being grabbed (竞价抢筹), who was dumped (竞价砸盘), big gaps, probing orders, plus the cross-tab of grabbed names that later sealed. Alerts are vendor rules, not a full-market snapshot.',
      {
        as_of: { type: 'string', description: 'YYYYMMDD, default latest bar' },
        limit: { type: 'integer', description: 'rows per side, 1-40; default 10' },
      },
      (args) => paged(args.limit, LIMITS.rows.auction, (limit) => (
        client.request('/knowledge/auction', { query: { as_of: args.as_of, limit } })
      ), '40 行实测 13,547 B；服务端允许 200 行（约 68 KB），超过宿主上限会被整包丢弃'),
      tabularOutput(),
    ),
    define(
      'quant_anomaly',
      'Why a stock moved today: the vendor machine-written summary of public information, tagged 涨停/跌停/大涨/大跌 with keyword hits. A lead to verify, never a filing. Omit symbol for the whole list of the day.',
      {
        symbol: { type: 'string', description: 'one ts_code' },
        limit: { type: 'integer', description: 'rows, 1-8; default 8 (one attribution runs 0.7-1.7 KB)' },
      },
      (args) => paged(args.limit, LIMITS.rows.anomaly, (limit) => (
        client.request('/knowledge/anomaly', { query: { symbol: args.symbol, limit } })
      ), '单条归因实测 0.7-1.7 KB（300750.SZ 20261103 一条 1,700 B），10 条可能越过 16 KB'),
      tabularOutput(),
    ),
    define(
      'quant_analog',
      'Historical analogs: the k most similar past setups to this symbol today (features from the close, weighted kNN), and what happened 1/2/3/4/5/10 sessions later (continuous 1-5 days plus a 10-day decay reference) — entry at next open, mean/median/win rate, excess vs an equal-weight basket, 95% interval resampled by trading day, plus match.distance_band (where the nearest-neighbour distance sits against random names: tight / normal / far — geometry, not a verdict). Read caveats before trusting it.',
      {
        symbol: { type: 'string', required: true, description: '600000.SH' },
        k: { type: 'integer', description: '50-1000 neighbours; default 300' },
        same_industry: { type: 'boolean', description: 'restrict to the same industry' },
      },
      (args) => client.request('/knowledge/analog', {
        query: { symbol: args.symbol, k: args.k, same_industry: args.same_industry },
      }),
    ),
  ]
}

/** The only tools advertised to hosts. Legacy handlers stay available internally. */
export function buildTools(client) {
  return buildFacadeTools({
    internalTools: buildInternalTools(client),
    define,
    guard,
    formatJsonOutput,
    shrinkLadder,
    maxOutput: DEFAULT_MAX_OUTPUT,
  })
}

export function guard(exec) {
  if (PUBLIC_TOOL_NAMES.includes(exec.name)) {
    try {
      const resolved = resolveFacadeOperation(exec.name, exec.arguments || {})
      if (resolved.local) return undefined
      return guard({ name: resolved.legacyName, arguments: resolved.legacyArgs })
    } catch (error) {
      return error.message
    }
  }
  const args = exec.arguments || {}
  // 价格只能由内核算。这一条对**所有**提案工具都成立，不只主账户那张：
  // `quant_samples_propose` 以前不检查，`price` 会被工具层白名单丢掉，模型却
  // 以为自己给的价格被采纳了——票是真的，价格是另一个。
  if (exec.name === 'quant_propose' || exec.name === 'quant_samples_propose') {
    for (const forbidden of ['price', 'limit_price', 'stop_price']) {
      if (Object.hasOwn(args, forbidden)) {
        return `${forbidden} must be computed by the service, not supplied by the model`
      }
    }
  }
  if (exec.name === 'quant_propose') {
    if (!args.thesis || String(args.thesis).trim().length < 20) {
      return 'thesis too short: explain the trade in at least 20 characters'
    }
    if (!Array.isArray(args.evidence) || args.evidence.length === 0) {
      return 'evidence required: cite at least one id you actually read (e.g. daily:20261023)'
    }
    if (!args.invalidation || String(args.invalidation).trim().length < 5) {
      return 'invalidation required: state what would prove you wrong'
    }
    if (args.qty !== undefined && (
      !Number.isInteger(args.qty) || args.qty < 1 || args.qty > 1000000
    )) {
      return 'qty must be a positive integer no greater than 1000000'
    }
    if (args.qty != null && args.target_weight != null) {
      return 'qty and target_weight are mutually exclusive'
    }
  }
  if (exec.name === 'quant_approve') {
    if (!String(args.ticket_id || '').startsWith('td-')) {
      return 'ticket_id must look like td-YYYYMMDD-xxxxxxxx'
    }
    if (args.confirm !== true) {
      return 'approve requires confirm=true (explicit confirmation)'
    }
  }
  if (exec.name === 'quant_journal_append' && !String(args.text || '').trim()) {
    return 'journal text must not be empty'
  }
  if (exec.name === 'quant_core_execute' || exec.name === 'quant_core_preview') {
    if (!args.targets || typeof args.targets !== 'object' || !Object.keys(args.targets).length) {
      return 'targets required: the weights are yours to choose, the kernel only does the arithmetic'
    }
  }
  if (exec.name === 'quant_core_execute') {
    if (!args.thesis || String(args.thesis).trim().length < 20) {
      return 'thesis too short: say why this allocation, not why the rule exists'
    }
    if (!Array.isArray(args.evidence) || args.evidence.length === 0) {
      return 'evidence required: cite at least one id you actually read'
    }
  }
  return undefined
}

export function apply(ctx, config = {}) {
  const client = new QuantClient({ ...DEFAULTS, ...config })
  for (const tool of buildTools(client)) {
    ctx.tools.register(tool)
  }
  ctx.tools.guard(guard)
  ctx.systemPrompt.section({ name: 'quant-desk:trading', order: 3500, text: PROMPT })
  return () => {}
}

export default { name, inject, apply }
