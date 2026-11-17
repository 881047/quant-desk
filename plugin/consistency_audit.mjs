#!/usr/bin/env node
/**
 * 同一个事实，两个出口说的是不是同一件事。
 *
 * 另外三个审计各问一件事：`delivery` 问字节、`quality` 问表述、`freshness` 问年龄。
 * 没有一个问"这两个数是同一个数吗"——而这个项目里最贵的几个 bug 全在这一类：
 * P68 的 board 有两份实现、P79 的正文路径有两份、P81 的两个 vendor 撞名。
 *
 * 每项检查声明一个容差，不声明"看起来对不对"：
 *   exact  完全相等（价格、家数、笔数）
 *   rel    相对误差（派生量，两边各自四舍五入过）
 *   abs    绝对误差（同一总体被切分过，末端有取整）
 *
 * 三种结论：一致 / 分歧（给两个值和差多少） / 不可比（数据没覆盖，不是错）
 *
 * `--selftest` 把某个数故意改坏，验证这套判定真的会报——一个从没报过警的审计
 * 不算证据，只算一次幸运。
 *
 * 用法: node consistency_audit.mjs [--json] [--selftest]
 */

import fs from 'node:fs'

const TOKEN = (() => {
  const line = fs.readFileSync('../.env', 'utf8').split(/\r?\n/)
    .find((l) => l.startsWith('QUANT_TOKEN='))
  return line ? line.split('=')[1].trim() : ''
})()

const { QuantClient, buildTools } = await import('./lib/index.js')
const client = new QuantClient({ token: TOKEN })
const tools = new Map(buildTools(client).map((tool) => [tool.name, tool]))

async function ask(name, args = {}) {
  const tool = tools.get(name)
  if (!tool) throw new Error(`no such tool: ${name}`)
  const body = await tool.execute(args, {})
  return typeof body === 'string' ? JSON.parse(body) : body
}

const n = (value) => {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const SYMBOL = '600519.SH'
const PEER = '000858.SZ'

// 前一交易日不需要问服务：bars 里就有窗口，取当天的前一个。
async function previousSession(day) {
  const window = await ask('quant_bars', { symbol: SYMBOL, days: 10 })
  const dates = (window?.bars || []).map((bar) => String(bar.trade_date))
  const at = dates.indexOf(String(day))
  if (at > 0) return dates[at - 1]
  return dates.length >= 2 ? dates[dates.length - 2] : null
}

// ---------------------------------------------------------------- 取数
async function loadState() {
  const facts = await ask('quant_facts')
  const since = facts?.day ? await previousSession(facts.day) : null
  const screen = await ask('quant_screen', {})
  const lead = screen?.candidates?.[0]?.symbol
  return {
    facts,
    factsPrev: since ? await ask('quant_facts', { date: since }) : null,
    watch: since ? await ask('quant_watch', { since }) : null,
    screen,
    universeHit: lead ? await ask('quant_universe', { query: lead, limit: 3 }) : null,
    tape: await ask('quant_knowledge', { topic: 'limit_structure' }),
    industries: await ask('quant_knowledge', { topic: 'industries' }),
    portfolio: await ask('quant_portfolio'),
    bars: await ask('quant_bars', { symbol: SYMBOL, days: 5 }),
    profile: await ask('quant_profile', {
      symbol: SYMBOL,
      sections: ['tradability', 'technicals', 'relative_strength', 'valuation', 'intraday'],
    }),
    pair: await ask('quant_compare', { symbols: [SYMBOL, PEER] }),
    journal: await ask('quant_journal_search', { limit: 20, recent: 0 }),
    memory: await ask('quant_memory_search', { limit: 20, recent: 0 }),
  }
}

const rowsOf = (payload) => (Array.isArray(payload?.rows) ? payload.rows : [])
const metricRow = (payload, metric) => rowsOf(payload).find((row) => row.metric === metric)

// 两个出口对同一条记忆的核对结论：同一个 id，必须给同一个 verified。
// 分母是"两端都带结论的条目数"，分子是"完全一致的条目数"——用 exact 比，
// 全一致时两者相等。分母为 0 时返回 null，让审计报"不可比"而不是绿。
function journalCross(state) {
  const byId = new Map((state.memory?.journal || []).map((row) => [String(row.id), row]))
  const shared = (state.journal?.entries || [])
    .filter((row) => byId.has(String(row.id)) && row.verified)
    .map((row) => [row, byId.get(String(row.id))])
  const agree = shared.filter(([a, b]) => JSON.stringify(a.verified) === JSON.stringify(b.verified))
  const sound = shared.filter(([row]) => {
    const v = row.verified
    const checked = v.checked ?? 0
    const matched = v.matched ?? 0
    const count = v.unmatched_count ?? 0
    const listed = (v.unmatched || []).length
    return matched + count === checked && listed <= 3
  })
  // 标签那一半也要自洽：labelled == ok + 四类。少一个计数，读的人就没法复核
  // "这篇 3 个标签里有 2 个是错的"这句话是不是真的。
  const labelled = shared.filter(([row]) => {
    const claims = row.verified.claims
    if (!claims) return true
    const parts = ['misplaced', 'variant', 'unbacked', 'provisional']
      .reduce((sum, key) => sum + (claims[key] || 0), 0)
    return (claims.ok || 0) + parts === claims.labelled
  })
  return { total: shared.length, agree: agree.length, sound: sound.length,
           labelled: labelled.length }
}

// ---------------------------------------------------------------- 检查表
const checks = [
  {
    id: 'last_price',
    what: '当日收盘价：profile.tradability.last ↔ bars.bars[-1].close',
    a: (c) => n(c.profile?.tradability?.last),
    b: (c) => n(c.bars?.bars?.at(-1)?.close),
    tol: 'exact',
  },
  {
    id: 'ret_20d_inside_profile',
    what: '20 日收益：technicals.return_pct ↔ relative_strength.window.20d.stock_pct（同一个块里两处算的）',
    a: (c) => n(c.profile?.technicals?.return_pct?.['20d']),
    b: (c) => n(c.profile?.relative_strength?.window?.['20d']?.stock_pct),
    tol: 'rel',
    rel: 0.01,
  },
  {
    id: 'intraday_close',
    what: '分时最后收盘：profile.intraday.close ↔ tradability.last',
    a: (c) => n(c.profile?.intraday?.close),
    b: (c) => n(c.profile?.tradability?.last),
    tol: 'exact',
  },
  {
    id: 'limit_up_count',
    what: '涨停家数：facts.pools ↔ knowledge(limit_structure).counts',
    a: (c) => n(c.facts?.pools?.limit_up),
    b: (c) => n(c.tape?.counts?.limit_up),
    tol: 'exact',
  },
  {
    id: 'limit_down_count',
    what: '跌停家数：facts.pools ↔ knowledge(limit_structure).counts',
    a: (c) => n(c.facts?.pools?.limit_down),
    b: (c) => n(c.tape?.counts?.limit_down),
    tol: 'exact',
  },
  {
    id: 'advancers_vs_industries',
    what: '上涨家数：facts.breadth ↔ Σ industries.rows[].advancers',
    a: (c) => n(c.facts?.breadth?.advancers),
    b: (c) => rowsOf(c.industries).reduce((sum, row) => sum + (n(row.advancers) || 0), 0),
    tol: 'abs',
    abs: 6,
  },
  {
    id: 'universe_rows',
    what: '分母是否同一个：facts.breadth.rows ↔ Σ industries.rows[].members',
    a: (c) => n(c.facts?.breadth?.rows),
    b: (c) => rowsOf(c.industries).reduce((sum, row) => sum + (n(row.members) || 0), 0),
    tol: 'abs',
    abs: 6,
  },
  {
    id: 'pe_ttm',
    what: 'PE：compare.rows[pe_ttm] ↔ profile.valuation（同一窗口应当逐位相同）',
    a: (c) => n(metricRow(c.pair, 'pe_ttm')?.values?.[SYMBOL]),
    b: (c) => n(c.profile?.valuation?.current?.pe_ttm),
    tol: 'rel',
    rel: 0.005,
  },
  {
    id: 'equity',
    what: '账户净值：facts.account.equity ↔ portfolio.equity',
    a: (c) => n(c.facts?.account?.equity),
    b: (c) => n(c.portfolio?.equity),
    tol: 'exact',
  },
  {
    id: 'position_count',
    what: '持仓只数：facts.account.positions ↔ portfolio.position_count',
    a: (c) => n(c.facts?.account?.positions),
    b: (c) => n(c.portfolio?.position_count),
    tol: 'exact',
  },
  {
    id: 'screen_vs_universe',
    what: '候选价：screen.candidates[0].close ↔ universe.close（同一只、同一天）',
    a: (c) => n(c.screen?.candidates?.[0]?.close),
    b: (c) => n(c.universeHit?.rows?.[0]?.close),
    tol: 'exact',
  },
  {
    id: 'screen_pct_vs_universe',
    // 两个工具各算一遍当日涨跌，而且量纲不同：screen 是百分数，universe 是小数。
    // 这一项同时验证"数一致"和"量纲声明是真的"——差 100 倍会立刻暴露。
    what: '当日涨跌幅：screen.pct_chg（%）↔ universe.ret_1d（小数）×100',
    a: (c) => n(c.screen?.candidates?.[0]?.pct_chg),
    b: (c) => {
      const value = n(c.universeHit?.rows?.[0]?.ret_1d)
      return value === null ? null : value * 100
    },
    tol: 'abs',
    abs: 0.02,
  },
  {
    id: 'screen_band_vs_universe',
    what: '涨跌幅上限：screen.band ↔ universe.band（同一只、同一天）',
    a: (c) => n(c.screen?.candidates?.[0]?.band),
    b: (c) => n(c.universeHit?.rows?.[0]?.band),
    tol: 'exact',
  },
  // 下面三条不是"两个出口对一个事实"，而是"声明过的政策必须成立"：
  // 工具描述里写了 screen 不选科创板/北交所、band 由板块决定——那就应该每次都对。
  {
    id: 'screen_excludes_star_bse',
    what: 'screen 的政策：候选里不出现科创板 / 北交所（描述里写死的；违规数应为 0）',
    a: (c) => 0,
    b: (c) => (c.screen?.candidates || [])
      .filter((row) => !['MAIN', 'CHINEXT'].includes(String(row.board))).length,
    tol: 'exact',
  },
  {
    id: 'band_matches_board',
    what: 'band 必须由板块推出：MAIN→0.10、CHINEXT/STAR→0.20、BSE→0.30（违规数应为 0）',
    a: (c) => 0,
    b: (c) => (c.screen?.candidates || []).filter((row) => {
      const expected = { MAIN: 0.1, CHINEXT: 0.2, STAR: 0.2, BSE: 0.3 }[String(row.board)]
      return expected !== undefined && Math.abs(Number(row.band) - expected) > 1e-9
    }).length,
    tol: 'exact',
  },
  {
    id: 'breadth_partitions_rows',
    what: '涨跌平必须切完总体：advancers + decliners + flat ↔ breadth.rows',
    a: (c) => {
      const b = c.facts?.breadth || {}
      const parts = [b.advancers, b.decliners, b.flat].map(n)
      return parts.some((v) => v === null) ? null : parts.reduce((x, y) => x + y, 0)
    },
    b: (c) => n(c.facts?.breadth?.rows),
    tol: 'exact',
  },
  {
    id: 'watch_changes_vs_facts',
    what: '涨跌家数变化：watch(since).changes ↔ facts 两日的差',
    a: (c) => {
      const row = (c.watch?.changes?.changes || []).find((item) => item.key === 'breadth.advancers')
      return row ? n(row.delta) : null
    },
    b: (c) => {
      const today = n(c.facts?.breadth?.advancers)
      const then = n(c.factsPrev?.breadth?.advancers)
      return today === null || then === null ? null : today - then
    },
    tol: 'exact',
  },
  {
    // 分母 / 分子：两端都带结论的条目数 / 其中结论完全一致的条目数。
    id: 'journal_verified_both_ends',
    what: '同一条记忆的核对结论：journal ↔ memory_search（同一个 id 给同一个 verified）',
    a: (c) => journalCross(c).total || null,
    b: (c) => journalCross(c).agree,
    tol: 'exact',
  },
  {
    id: 'journal_verdict_adds_up',
    what: '核对结论自洽：matched + unmatched_count == checked，且 unmatched 列表已截到 3 个',
    a: (c) => journalCross(c).total || null,
    b: (c) => journalCross(c).sound,
    tol: 'exact',
  },
  {
    id: 'journal_claims_add_up',
    what: '标签核对自洽：labelled == ok + misplaced + variant + unbacked + intraday',
    a: (c) => journalCross(c).total || null,
    b: (c) => journalCross(c).labelled,
    tol: 'exact',
  },
]

// ---------------------------------------------------------------- 判定
function verdict(check, state) {
  const a = check.a(state)
  const b = check.b(state)
  if (a === null || b === null) return { state: 'na', a, b, delta: null }
  const delta = b - a
  if (check.tol === 'exact') {
    // 价格与家数是整数字段或两位小数，容 1e-6 只为浮点表示。
    return { state: Math.abs(delta) <= 1e-6 ? 'ok' : 'diverge', a, b, delta }
  }
  if (check.tol === 'rel') {
    const base = Math.max(Math.abs(a), Math.abs(b), 1e-9)
    const over = Math.abs(delta) / base > (check.rel || 0.01)
    return { state: over ? 'diverge' : 'ok', a, b, delta }
  }
  return { state: Math.abs(delta) <= (check.abs || 1) ? 'ok' : 'diverge', a, b, delta }
}

function run(state) {
  return checks.map((check) => ({ id: check.id, what: check.what, ...verdict(check, state) }))
}

const state = await loadState()

// ---------------------------------------------------------------- 自检
if (process.argv.includes('--selftest')) {
  // 三个方向各改坏一处：计数漂移、量纲错位、同一结论两个出口不一致。
  const drifted = structuredClone(state)
  drifted.facts.breadth.advancers += 37
  const scaled = structuredClone(state)
  if (scaled.screen?.candidates?.[0]) scaled.screen.candidates[0].pct_chg *= 100
  const split = structuredClone(state)
  const victim = (split.memory?.journal || []).find((row) => row.verified)
  if (victim) victim.verified.matched += 1

  const caughtCount = run(drifted).find((r) => r.id === 'advancers_vs_industries')?.state === 'diverge'
  const caughtUnits = run(scaled).find((r) => r.id === 'screen_pct_vs_universe')?.state === 'diverge'
  const caughtSplit = !victim
    || ['journal_verified_both_ends', 'journal_verdict_adds_up']
      .some((id) => run(split).find((r) => r.id === id)?.state === 'diverge')
  console.log(`自检：注入计数漂移 ${caughtCount ? '已捕获' : '未捕获'}；`
    + `注入 100 倍量纲错位 ${caughtUnits ? '已捕获' : '未捕获'}；`
    + `注入双出口分歧 ${victim ? (caughtSplit ? '已捕获' : '未捕获') : '无可改坏的条目'}`)
  process.exit(caughtCount && caughtUnits && caughtSplit ? 0 : 1)
}

const results = run(state)
const ok = results.filter((r) => r.state === 'ok')
const bad = results.filter((r) => r.state === 'diverge')
const na = results.filter((r) => r.state === 'na')

if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ results }, null, 1))
  process.exit(bad.length ? 1 : 0)
}

console.log(`\n一致性审计：${results.length} 项检查`)
for (const row of results) {
  const mark = row.state === 'ok' ? '  ok  ' : row.state === 'na' ? '  --  ' : ' 分歧 '
  const numbers = row.state === 'na'
    ? `（不可比：${row.a === null ? 'A' : 'B'} 无值）`
    : `A=${row.a} B=${row.b} Δ=${Number(row.delta.toFixed(6))}`
  console.log(`${mark} ${row.id.padEnd(24)} ${numbers}`)
  if (row.state === 'diverge') console.log(`        ${row.what}`)
}
console.log(`\n一致 ${ok.length} / 分歧 ${bad.length} / 不可比 ${na.length}`)
if (bad.length) {
  console.log('\n分歧项（两份出口说的是同一个事实，读者只能信一个）：')
  for (const row of bad) console.log(`  - ${row.id}: ${row.what}`)
}
process.exit(bad.length ? 1 : 0)
