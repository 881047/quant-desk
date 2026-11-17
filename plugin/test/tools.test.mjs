import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { PROMPT, QuantClient, SHRINK_LEVELS, apply, buildInternalTools, buildTools, errorText, forgetRequestId, formatJsonOutput, guard, requestIdFor, shrinkJson, shrinkLadder } from '../lib/index.js'

function makeCtx() {
  const tools = []
  const guards = []
  const sections = []
  return {
    tools: {
      register: (tool) => tools.push(tool),
      guard: (fn) => guards.push(fn),
    },
    systemPrompt: { section: (section) => sections.push(section) },
    _tools: tools,
    _guards: guards,
    _sections: sections,
  }
}

function fakeFetch(handler) {
  const original = globalThis.fetch
  globalThis.fetch = async (url, init) => handler(String(url), init)
  return () => { globalThis.fetch = original }
}

const reply = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

/** 按 schema 给一个类型正确的最小值：用来把工具推进到"真的发请求"那一步。 */
function sampleValue(spec = {}) {
  if (spec.const !== undefined) return spec.const
  if (spec.enum) return spec.enum[0]
  if (spec.type === 'string') return '600000.SH'
  if (spec.type === 'integer') return 1
  if (spec.type === 'number') return 0.5
  if (spec.type === 'boolean') return true
  if (spec.type === 'array') {
    if (spec.items?.type === 'object') return [sampleValue(spec.items)]
    if (spec.items?.type === 'string') return ['daily:20261023']
    return ['daily:20261023']
  }
  if (spec.type === 'object') {
    const value = {}
    for (const key of spec.required || []) value[key] = sampleValue(spec.properties[key])
    return value
  }
  return 'x'
}

test('apply registers four public tools, one guard and one prompt section', () => {
  const ctx = makeCtx()
  const dispose = apply(ctx, { baseUrl: 'http://127.0.0.1:8811', token: 't' })
  assert.equal(ctx._tools.length, 4)
  assert.equal(ctx._guards.length, 1)
  assert.equal(ctx._sections.length, 1)
  assert.equal(new Set(ctx._tools.map((t) => t.name)).size, 4)
  assert.equal(typeof dispose, 'function')
})

test('the pool tool clamps rows so a large limit cannot blow the 16 KB ceiling', async () => {
  // 服务端允许 100 行（约 65 KB），而宿主对超过 16 KB 的响应是**整体丢弃**，
  // 不是截断。所以 limit=100 的结果是"什么都拿不到"，比数据不全更糟。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return {} } }
  const pools = buildInternalTools(client).find((t) => t.name === 'quant_pools')

  await pools.execute({ limit: 100 }, {})
  await pools.execute({}, {})
  await pools.execute({ limit: 0 }, {})

  assert.equal(calls[0][1].query.limit, 20)
  assert.equal(calls[1][1].query.limit, 15)
  assert.equal(calls[2][1].query.limit, 15, '0 是 falsy，走默认而不是零行')
})

test('the minutes tool clamps last_n into the range the server accepts', async () => {
  // 服务端要求 limit >= 8（更少的 bar 答不了任何问题）。schema 只有描述、没有
  // minimum 可写，所以以前传 last_n=5 会拿到 422 + 一串 pydantic 原文——在四分钟
  // 的决策窗口里，那和"这个工具坏了"没区别。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return {} } }
  const mins = buildInternalTools(client).find((t) => t.name === 'quant_mins')

  await mins.execute({ symbol: '600000.SH', last_n: 5 }, {})
  await mins.execute({ symbol: '600000.SH', last_n: 500 }, {})
  await mins.execute({ symbol: '600000.SH' }, {})

  assert.equal(calls[0][1].query.limit, 8, '5 被抬到服务端下限')
  assert.equal(calls[1][1].query.limit, 240, '500 被压到上限')
  assert.equal(calls[2][1].query.limit, 48, '默认一整个交易日')
})

test('the universe tool cannot ask for a page the host would drop', async () => {
  // 默认 50 行实测 16.8 KB，已经越过 16 KB 上限——而这个工具以前默认就是 50。
  // 也就是说"看一眼全市场"这个最普通的调用，拿到的是空响应。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return {} } }
  const uni = buildInternalTools(client).find((t) => t.name === 'quant_universe')

  await uni.execute({ limit: 200 }, {})
  await uni.execute({}, {})

  assert.equal(calls[0][1].query.limit, 40, '200 行约 67 KB，必须被夹住')
  assert.equal(calls[1][1].query.limit, 30)
})

test('every row-counting tool clamps to a page the host can carry', async () => {
  // W-09（2026-11-04）：六个工具把 limit 原样送出去，上界就成了路由的上界——
  // /knowledge/{topic} 收 200 行、/execution/tca 收 2000 行。要 2000 行不是"更多行"，
  // 而是整包被宿主丢弃：和"今天没有数据"长得一模一样。夹紧之后必须写进 capped，
  // 否则模型会把 40 条当成当天的全部。
  const cases = [
    ['quant_screen', { limit: 500 }, 20, '/research/screen'],
    ['quant_memory_search', { limit: 500 }, 12, '/memory/search'],
    ['quant_memory_search', { limit: 500, full: true }, 6, '/memory/search'],
    ['quant_hypothesis', { action: 'list', limit: 500 }, 60, '/research/hypotheses'],
    ['quant_hypothesis', { action: 'list', full: true, limit: 500 }, 20, '/research/hypotheses'],
    ['quant_execution_tca', { limit: 5000 }, 60, '/execution/tca'],
    ['quant_auction', { limit: 500 }, 40, '/knowledge/auction'],
    ['quant_anomaly', { limit: 500 }, 8, '/knowledge/anomaly'],
  ]
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { ok: true } } }
  const tools = buildInternalTools(client)
  for (const [name, args, served, route] of cases) {
    const tool = tools.find((candidate) => candidate.name === name)
    const data = await tool.execute(args, {})
    const [path, opts] = calls.at(-1)
    assert.equal(path, route, name)
    assert.equal(opts.query.limit, served, `${name} 应在 ${served} 行夹住`)
    assert.equal(data.capped?.served_limit, served, `${name} 必须说清这一页被夹过`)
    assert.equal(data.capped?.asked_limit, args.limit, name)
  }
})

test('the screen does not invent a row count when the caller gave none', async () => {
  // `/research/screen` 的默认值来自 `screen.max_candidates`（签名里是 None，就是为了
  // 让配置说话）。工具层补一个 8 会把 yaml 顶掉，改配置再重启也不生效。
  const calls = []
  const client = { request: async (path, opts) => { calls.push(opts); return {} } }
  const screen = buildInternalTools(client).find((tool) => tool.name === 'quant_screen')

  await screen.execute({}, {})
  await screen.execute({ limit: 5 }, {})

  assert.equal(calls[0].query.limit, undefined, '不填就不发，让配置决定')
  assert.equal(calls[1].query.limit, 5)
})

test('a page inside the ceiling carries no capped note', async () => {
  const client = { request: async () => ({ ok: true }) }
  const auction = buildInternalTools(client).find((tool) => tool.name === 'quant_auction')
  const data = await auction.execute({ limit: 10 }, {})
  assert.equal(data.capped, undefined)
})

test('no tool forwards a page size without clamping first', () => {
  // 结构检查：上一条按工具点名，这一条防"新加的工具又忘了夹"。
  const source = fs.readFileSync(new URL('../lib/index.js', import.meta.url), 'utf8')
  assert.equal(/limit:\s*args\.limit\s*[,}]/.test(source), false,
    'limit 必须先过 clampRows/paged，否则上界由路由决定')
})

test('a misspelled argument is refused, and the error names the legal ones', async () => {
  // W-08（2026-11-04）：run 按名取字段，没声明的字段静默消失——limt=5 会跑成默认
  // 10 条，targtes=... 会被答成"targets required"（在说另一个名字）。服务端的
  // extra="forbid" 永远看不到这些字段，因为工具层先重建了 body。
  const calls = []
  const client = { request: async (path) => { calls.push(path); return {} } }
  const auction = buildInternalTools(client).find((tool) => tool.name === 'quant_auction')
  await assert.rejects(
    () => auction.execute({ limt: 5 }, {}),
    /unknown argument\(s\) for quant_auction: limt\. legal: as_of, limit\./,
  )
  assert.equal(calls.length, 0, '不能带着默认值把请求发出去')
})

test('every tool refuses a field it does not declare', async () => {
  // 逐条点名太容易漏：validator 挂在 define 上，所以 73 条一起生效。必填字段先按
  // schema 填一个类型正确的值——SDK 会在进入 execute 之前就拒绝缺必填的调用，
  // 那样测到的就不是这里要测的那道门。
  const tools = buildInternalTools({ request: async () => ({}) })
  assert.equal(tools.length, 73)
  for (const tool of tools) {
    const args = { __no_such_field__: 1 }
    for (const key of tool.parameters.required || []) {
      args[key] = sampleValue(tool.parameters.properties[key])
    }
    await assert.rejects(
      () => tool.execute(args, {}),
      /unknown argument\(s\) for quant_[a-z_]+: __no_such_field__/,
      `${tool.name} 没有拦住未声明的字段`,
    )
  }
})

test('every tool actually runs when given only its required fields', async () => {
  // C-01（2026-11-04）：覆盖率数字答不了这个问题。73 个工具的定义（描述、参数、
  // 输出 schema）都是"行"，而 run 只有一两行——91% 的行覆盖可以和"30 个 run
  // 一次都没执行过"同时成立，事实上有过之。
  //
  // 所以按 schema 的必填项自动填参数，要求每条工具真的发出一次请求。用假 client
  // 记调用：这条只看"跑到了没有"，不看"答得对不对"（那是各工具的用例）。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { ok: true } } }
  const tools = buildInternalTools(client)
  const missed = []
  for (const tool of tools) {
    const args = {}
    for (const key of tool.parameters.required || []) {
      args[key] = sampleValue(tool.parameters.properties[key])
    }
    calls.length = 0
    let refused = null
    try {
      await tool.execute(args, {})
    } catch (error) {
      refused = error
    }
    // 本地拒单（guard、id 校验）是允许的，但前提是它已经发出过请求或至少进过 run。
    // 这里只认一种失败：一个请求都没发出去。
    if (!calls.length) missed.push(`${tool.name}: ${refused ? refused.message : '没发请求'}`)
  }
  assert.deepEqual(missed, [], '这些工具的 run 没有被任何测试执行过')
})

test('the sample-book proposal refuses price too', () => {
  // 白名单会先把 price 丢掉，模型于是以为自己给的价格被采纳了：票是真的，价格是另一个。
  assert.match(
    guard({ name: 'quant_samples_propose', arguments: { symbol: '600000.SH', price: 9.5 } }),
    /price must be computed/,
  )
})

test('the ticket list defaults to what is still open, and says so', async () => {
  // `proposed` 票据以前在工具层完全看不见：quant_orders 走 /orders（委托记录）、
  // quant_portfolio.pending 只含已排队的开盘撮合。propose 之后没有 approve 的
  // 票停在 proposed，既不在持仓也不在任何列表里——19:30 对账只能按 id 抽查，
  // 抽不到就等于不存在。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { tickets: [] } } }
  const list = buildInternalTools(client).find((t) => t.name === 'quant_tickets')

  await list.execute({}, {})
  await list.execute({ status: 'filled', limit: 200 }, {})

  assert.equal(calls[0][0], '/tickets')
  assert.equal(calls[0][1].query.status, 'open', '不传 status 时不该退化成"全部"')
  assert.equal(calls[0][1].query.limit, 10)
  assert.equal(calls[1][1].query.status, 'filled')
  assert.equal(calls[1][1].query.limit, 15, '简报实测 967 B/行，15 行约 14.5 KB')
})

test('the hypothesis tool can settle a hypothesis, not only open one', async () => {
  // 只有 list/add 时，一条假设永远不会离开 draft：结案只能靠直接调 HTTP。
  // 这台机器上 5 条假设全是 rejected，说明端点被用过、而工具够不到——描述里
  // 写着"只有统计能提升它"，却没有记录统计结果的地方。
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { ok: true } } }
  const hyp = buildInternalTools(client).find((t) => t.name === 'quant_hypothesis')

  await hyp.execute({ action: 'result', hyp_id: 7, status: 'promoted',
                      metrics: { n: 120, mean_excess: 0.004 }, notes: 'cleared the bar' }, {})

  assert.equal(calls[0][0], '/research/hypotheses/7/result')
  assert.equal(calls[0][1].method, 'POST')
  assert.deepEqual(calls[0][1].body, {
    status: 'promoted',
    metrics: { n: 120, mean_excess: 0.004 },
    notes: 'cleared the bar',
  })
})

test('a hypothesis result without an id or a status never reaches the network', async () => {
  let called = false
  const client = { request: async () => { called = true; return {} } }
  const hyp = buildInternalTools(client).find((t) => t.name === 'quant_hypothesis')

  await assert.rejects(() => hyp.execute({ action: 'result', status: 'promoted' }, {}),
                       /hyp_id/)
  await assert.rejects(() => hyp.execute({ action: 'result', hyp_id: 7 }, {}),
                       /status/)
  assert.equal(called, false)
})

test('propose guard allows bounded quantity but never price or ambiguous sizing', () => {
  const base = {
    name: 'quant_propose',
    arguments: {
      symbol: '600000.SH',
      thesis: '银行板块 20 日动量转正，估值处于低位，核心仓回补到两成。',
      evidence: ['daily:20261023'],
      invalidation: '收盘跌破 9.24',
    },
  }
  assert.equal(guard(base), undefined)
  assert.equal(guard({ ...base, arguments: { ...base.arguments, qty: 100 } }), undefined)
  assert.match(guard({ ...base, arguments: { ...base.arguments, thesis: '想买' } }), /thesis too short/)
  assert.match(guard({ ...base, arguments: { ...base.arguments, evidence: [] } }), /evidence required/)
  assert.match(guard({ ...base, arguments: { ...base.arguments, qty: 1.5 } }), /positive integer/)
  assert.match(guard({ ...base, arguments: { ...base.arguments, qty: 0 } }), /positive integer/)
  assert.match(guard({ ...base, arguments: { ...base.arguments, qty: 1000001 } }), /1000000/)
  assert.match(
    guard({ ...base, arguments: { ...base.arguments, qty: 100, target_weight: 0.2 } }),
    /mutually exclusive/,
  )
  assert.match(guard({ ...base, arguments: { ...base.arguments, price: 9.5 } }), /price must be computed/)
})

test('approve guard requires a ticket id and explicit confirm', () => {
  assert.match(guard({ name: 'quant_approve', arguments: { ticket_id: 'x', confirm: true } }), /td-/)
  assert.match(guard({ name: 'quant_approve', arguments: { ticket_id: 'td-20261023-abc', confirm: false } }), /confirm=true/)
  assert.equal(guard({ name: 'quant_approve', arguments: { ticket_id: 'td-20261023-abc', confirm: true } }), undefined)
})

test('short alpha is a read-only candidate synthesis with an explicit horizon request', async () => {
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { ok: true, candidates: [] } } }
  const tool = buildInternalTools(client).find((t) => t.name === 'quant_short_alpha')
  assert.ok(tool)
  await tool.execute({ as_of: '20261113', limit: 2 }, {})
  assert.equal(calls[0][0], '/short-alpha/candidates')
  assert.deepEqual(calls[0][1].query, { as_of: '20261113', limit: 2 })
  assert.equal(calls[0][1].method, undefined, 'GET must not become a proposal/write')
  assert.doesNotMatch(tool.description, /已证实|保证收益|必能成交/)
})

test('short-alpha evidence, model judgment and tracking stay separate', async () => {
  const calls = []
  const client = { request: async (path, opts) => { calls.push([path, opts]); return { ok: true } } }
  const tools = buildInternalTools(client)
  const evidence = tools.find((t) => t.name === 'quant_short_alpha_evidence')
  await evidence.execute({ symbols: ['000001.SZ'], limit: 99, offset: 2 }, {})
  assert.equal(calls[0][0], '/short-alpha/evidence')
  assert.equal(calls[0][1].method, undefined)
  assert.deepEqual(calls[0][1].query, {
    as_of: undefined, symbols: '000001.SZ', limit: 4, offset: 2,
  })

  const judge = tools.find((t) => t.name === 'quant_short_alpha_judge')
  assert.equal('price' in judge.parameters.properties, false)
  assert.equal('side' in judge.parameters.properties, false)
  assert.equal('qty' in judge.parameters.properties, false)
  const item = {
    symbol: '000001.SZ', role: 'candidate', target_weight: 0.2,
    thesis: 'WorkBuddy独立阅读证据后写下的短线候选理由，长度足够。',
    evidence: ['short-alpha-evidence:20261113:000001.SZ'],
    invalidation: '开盘结构失效',
  }
  await judge.execute({
    day: '20261113', reviewed_evidence: item.evidence, items: [item],
    client_request_id: 'sa-plugin-test-1',
  }, {})
  assert.equal(calls[1][0], '/short-alpha/judgments')
  assert.equal(calls[1][1].method, 'POST')
  assert.equal(calls[1][1].body.items[0].role, 'candidate')
  assert.equal(calls[1][1].body.items[0].target_weight, 0.2)
  assert.equal(calls[1][1].body.replay, false)
  assert.equal('price' in calls[1][1].body, false)

  const review = tools.find((t) => t.name === 'quant_short_alpha_review')
  await review.execute({
    day: '20261113', client_request_id: 'short-alpha-review:20261113:1910',
    items: [{ symbol: '000001.SZ', decision: 'exit_next_open',
      reason: 'WorkBuddy重新阅读证据后认为失效条件成立，因此选择下一开盘退出。',
      evidence: ['daily:20261116'] }],
  }, {})
  assert.equal(calls[2][0], '/short-alpha/reviews')
  assert.equal(calls[2][1].method, 'POST')
  assert.equal(calls[2][1].body.items[0].decision, 'exit_next_open')

  const execution = tools.find((t) => t.name === 'quant_short_alpha_execution')
  await execution.execute({ day: '20261113', limit: 3 }, {})
  assert.equal(calls[3][0], '/short-alpha/execution')
  assert.equal(calls[3][1].method, undefined)
  assert.deepEqual(calls[3][1].query, { day: '20261113', limit: 3 })

  const tracking = tools.find((t) => t.name === 'quant_short_alpha_tracking')
  await tracking.execute({}, {})
  assert.equal(calls[4][0], '/short-alpha/tracking')
  assert.deepEqual(calls[4][1].query, { scope: 'prospective', limit: 4 })
})

test('quant_context calls /context_pack with the token', async () => {
  const ctx = makeCtx()
  apply(ctx, { baseUrl: 'http://127.0.0.1:9999', token: 'secret' })
  let seen
  const restore = fakeFetch((url, init) => {
    seen = { url, init }
    return reply({ account: { equity: '5000.00' } })
  })
  try {
    const tool = ctx._tools.find((t) => t.name === 'quant_query')
    const value = await tool.execute({ resource: 'context', params: {} }, {})
    assert.equal(value.account.equity, '5000.00')
    assert.equal(seen.url, 'http://127.0.0.1:9999/context_pack')
    assert.equal(seen.init.headers['X-Quant-Token'], 'secret')
  } finally {
    restore()
  }
})

test('knowledge pagination and execution side reach the HTTP query', async () => {
  const ctx = makeCtx()
  apply(ctx, { baseUrl: 'http://127.0.0.1:9999', token: 't' })
  const seen = []
  const restore = fakeFetch((url) => {
    seen.push(url)
    return reply({ ok: true })
  })
  try {
    const query = ctx._tools.find((t) => t.name === 'quant_query')
    await query.execute({
      resource: 'knowledge',
      params: { topic: 'seal_tape', as_of: '20261113', offset: 40, limit: 20 },
    }, {})
    await query.execute({
      resource: 'execution_estimate',
      params: { symbol: '605255.SH', qty: 100, side: 'SELL' },
    }, {})
    const knowledge = new URL(seen[0])
    assert.equal(knowledge.pathname, '/knowledge/seal_tape')
    assert.equal(knowledge.searchParams.get('offset'), '40')
    assert.equal(knowledge.searchParams.get('limit'), '20')
    assert.equal(knowledge.searchParams.get('as_of'), '20261113')
    const execution = new URL(seen[1])
    assert.equal(execution.pathname, '/execution/estimate')
    assert.equal(execution.searchParams.get('side'), 'SELL')
  } finally {
    restore()
  }
})

test('quant_propose forwards qty, strategy and persona but never price', async () => {
  const ctx = makeCtx()
  apply(ctx, { baseUrl: 'http://127.0.0.1:9999', token: 't' })
  let body
  const restore = fakeFetch((_url, init) => {
    body = JSON.parse(init.body)
    return reply({ status: 'proposed', qty: 100 })
  })
  try {
    const tool = ctx._tools.find((t) => t.name === 'quant_order')
    await tool.execute({
      book: 'paper',
      action: 'propose',
      params: {
        symbol: '600000.SH',
        side: 'BUY',
        sleeve: 'CORE',
        thesis: '银行板块 20 日动量转正，估值处于低位，核心仓回补到两成。',
        evidence: ['daily:20261023'],
        invalidation: '跌破 9.24',
        qty: 100,
        strategy: 'llm',
        persona: 'retail',
      },
    }, {})
    assert.equal(body.symbol, '600000.SH')
    assert.equal(body.qty, 100)
    assert.equal(body.strategy, 'llm')
    assert.equal(body.persona, 'retail')
    assert.equal(body.target_weight, undefined)
    assert.equal(body.price, undefined)
    assert.deepEqual(body.evidence, ['daily:20261023'])
  } finally {
    restore()
  }
})

test('http errors surface the service detail', async () => {
  const ctx = makeCtx()
  apply(ctx, { baseUrl: 'http://127.0.0.1:9999', token: 't' })
  const restore = fakeFetch(() => reply({ detail: { code: 'NOT_FOUND', message: 'ticket x not found' } }, 404))
  try {
    const tool = ctx._tools.find((t) => t.name === 'quant_query')
    await assert.rejects(
      () => tool.execute({ resource: 'ticket', params: { ticket_id: 'td-x' } }, {}),
      /404.*NOT_FOUND/,
    )
  } finally {
    restore()
  }
})

test('internal and public builders are pure and reusable', () => {
  const client = { request: async () => ({ ok: true }) }
  const a = buildInternalTools(client)
  const b = buildInternalTools(client)
  assert.equal(a.length, 73)
  assert.notEqual(a[0], b[0])
  const publicA = buildTools(client)
  const publicB = buildTools(client)
  assert.equal(publicA.length, 4)
  assert.notEqual(publicA[0], publicB[0])
})

test('quant_approve sends the mandatory confirmation body', async () => {
  const ctx = makeCtx()
  apply(ctx, { baseUrl: 'http://127.0.0.1:9999', token: 't' })
  let seen
  const restore = fakeFetch((url, init) => {
    seen = { url, body: JSON.parse(init.body) }
    return reply({ status: 'filled' })
  })
  try {
    const tool = ctx._tools.find((t) => t.name === 'quant_order')
    await tool.execute({
      book: 'paper', action: 'approve',
      params: { ticket_id: 'td-20261028-abcdef12', confirm: true },
    }, {})
    assert.match(seen.url, /\/decision\/td-20261028-abcdef12\/approve$/)
    assert.deepEqual(seen.body, { confirm: true })
  } finally {
    restore()
  }
})

test('client rejects userinfo, unapproved remote hosts and invalid numeric limits', () => {
  assert.throws(() => new QuantClient({ baseUrl: 'http://user:pass@127.0.0.1:8811' }), /userinfo/)
  assert.throws(() => new QuantClient({ baseUrl: 'https://example.com' }), /allowRemote/)
  assert.throws(() => new QuantClient({ baseUrl: 'http://127.evil.com' }), /allowRemote/)
  assert.equal(new QuantClient({ baseUrl: 'https://example.com', allowRemote: true }).allowRemote, true)
  for (const timeoutMs of [0, -1, 1.5, Infinity, NaN, 300001]) {
    assert.throws(() => new QuantClient({ timeoutMs }), /timeoutMs/)
  }
  for (const maxResponseBytes of [0, -1, 1.5, Infinity, NaN]) {
    assert.throws(() => new QuantClient({ maxResponseBytes }), /maxResponseBytes/)
  }
  for (const maxOutput of [0, 127, 1.5, Infinity, NaN, 1000001]) {
    assert.throws(() => formatJsonOutput({}, maxOutput), /maxOutput/)
  }
})

test('a successful non-JSON response is an error, never raw success data', async () => {
  const client = new QuantClient({ baseUrl: 'http://127.0.0.1:9999' })
  const restore = fakeFetch(() => new Response('<html>not json</html>', { status: 200 }))
  try {
    await assert.rejects(() => client.request('/context_pack'), /expected JSON response/)
  } finally {
    restore()
  }
})

test('response streaming enforces a hard byte cap and cancels the reader', async () => {
  let cancelled = false
  const stream = new ReadableStream({
    pull(controller) { controller.enqueue(new Uint8Array(1024)) },
    cancel() { cancelled = true },
  })
  const client = new QuantClient({ baseUrl: 'http://127.0.0.1:9999', maxResponseBytes: 2048 })
  const restore = fakeFetch(() => new Response(stream, { status: 200 }))
  try {
    await assert.rejects(() => client.request('/context_pack'), /exceeds 2048 bytes/)
    assert.equal(cancelled, true)
  } finally {
    restore()
  }
})

test('a real cross-origin 302 is blocked before the token can be forwarded', async () => {
  let targetHits = 0
  const target = http.createServer((req, res) => {
    targetHits += 1
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ token: req.headers['x-quant-token'] || null }))
  })
  await new Promise((resolve) => target.listen(0, '127.0.0.1', resolve))
  const targetUrl = `http://127.0.0.1:${target.address().port}/stolen`
  const source = http.createServer((_req, res) => {
    res.writeHead(302, { Location: targetUrl })
    res.end()
  })
  await new Promise((resolve) => source.listen(0, '127.0.0.1', resolve))
  const client = new QuantClient({
    baseUrl: `http://127.0.0.1:${source.address().port}`, token: 'must-not-leak',
  })
  try {
    await assert.rejects(() => client.request('/redirect'))
    assert.equal(targetHits, 0)
  } finally {
    await new Promise((resolve) => source.close(resolve))
    await new Promise((resolve) => target.close(resolve))
  }
})

test('an unsqueezable answer is a complete JSON error envelope, not sliced JSON', () => {
  // Shrinking cuts strings and array rows, not object keys — so a map with
  // thousands of keys is the residual case that genuinely cannot be delivered.
  const wide = Object.fromEntries(
    Array.from({ length: 4000 }, (_, i) => [`k${i}`, 'value-value']),
  )
  const tools = buildInternalTools({ request: async () => ({ ok: true }) })
  const rendered = tools[0].output.render({}, wide)[0].text
  const envelope = JSON.parse(rendered)
  assert.equal(envelope.truncated, true)
  assert.equal(envelope.error, 'output too large')
  assert.ok(!Object.hasOwn(envelope, 'k0'))
})

test('prompts and plugin docs mention only tools that actually exist', () => {
  const publicTools = buildTools({ request: async () => ({}) })
  const known = new Set([
    ...publicTools.map((tool) => tool.name),
    'quant_core_plan', 'quant_core_execute', // documented only as retired 409 warnings
  ])
  const pluginReadme = fs.readFileSync(new URL('../README.md', import.meta.url), 'utf8')
  const mcpReadme = fs.readFileSync(new URL('../mcp/README.md', import.meta.url), 'utf8')
  const sources = [
    PROMPT,
    fs.readFileSync(new URL('../skills/quant-desk/SKILL.md', import.meta.url), 'utf8'),
    pluginReadme,
    mcpReadme,
  ]
  assert.ok(pluginReadme.includes(`Tools (${publicTools.length})`), 'plugin README tool count drifted')
  assert.ok(mcpReadme.includes(`${publicTools.length} 个`), 'MCP README tool count drifted')
  for (const source of sources) {
    for (const [name] of source.matchAll(/\bquant_[a-z][a-z_]*\b/g)) {
      assert.ok(known.has(name), `documentation references missing tool ${name}`)
    }
  }
})

test('「开工先调什么」只有一个权威来源', () => {
  // 2026-11-03 之前这句话在四处各写一套：PROMPT 说"永远是 quant_watch"，
  // 技能说"任务型不需要"，AGENTS 说"第一个动作是检索新闻"，quant_context
  // 说"Call this first every session"。模型按最后读到的那套走，而它不一定
  // 适合这个问题。分派表只留在技能里，其余都是指针。
  assert.ok(!PROMPT.includes('永远是 quant_watch'), 'PROMPT 不该再给绝对顺序')
  assert.ok(!/Call this first every session/i.test(PROMPT))

  const tools = buildInternalTools({ request: async () => ({}) })
  const context = tools.find((tool) => tool.name === 'quant_context')
  assert.ok(!/Call this first every session/i.test(context.description),
    'quant_context 不是所有会话的入口：纯告警的唤醒从 quant_watch 开始')

  const watch = tools.find((tool) => tool.name === 'quant_watch')
  assert.match(watch.description, /journal/i,
    'findings 为空也要写 journal——跨日止损链读的是 journal，不是这一轮')

  const skill = fs.readFileSync(new URL('../skills/quant-desk/SKILL.md', import.meta.url), 'utf8')
  assert.ok(skill.includes('被定时任务叫醒时'), '分派表必须在技能里，别处只引用')

  const agents = fs.readFileSync(new URL('../../AGENTS.md', import.meta.url), 'utf8')
  assert.ok(!agents.includes('先新闻，后工具（所有自动化任务）'),
    '"先新闻"不能覆盖所有自动化任务：一次 09:26 的决策会在检索上花掉几分钟，而新闻不参与定价')
})

test('DSH installer uses a version whitelist and atomic writes in a temp HOME', (t) => {
  const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'quant-desk-dsh-install-'))
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }))
  const copied = path.join(temp, 'plugin')
  fs.mkdirSync(copied, { recursive: true })
  fs.copyFileSync(path.join(source, 'install.mjs'), path.join(copied, 'install.mjs'))
  fs.copyFileSync(path.join(source, 'package.json'), path.join(copied, 'package.json'))
  fs.cpSync(path.join(source, 'skills'), path.join(copied, 'skills'), { recursive: true })

  const runtime = path.join(temp, 'app', 'runtime')
  fs.mkdirSync(runtime, { recursive: true })
  fs.writeFileSync(path.join(runtime, 'runtime.json'), JSON.stringify({ desktopVersion: '9.9.9;not-semver' }))
  const sdk = path.join(copied, 'node_modules', '@deepseek-ai', 'dsh-tools')
  fs.mkdirSync(path.join(sdk, 'lib'), { recursive: true })
  fs.writeFileSync(path.join(sdk, 'lib', 'index.js'), '')
  fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({ version: '0.2.1-rc.3' }))

  const profile = path.join(temp, 'home', 'profiles', 'desktop')
  const profilePkg = path.join(profile, 'package.json')
  fs.mkdirSync(profile, { recursive: true })
  fs.writeFileSync(profilePkg, JSON.stringify({
    custom: { keep: true }, dependencies: { left: '1.0.0' }, dsh: { profile: { bundles: ['other'] } },
  }))
  const result = spawnSync(process.execPath, [path.join(copied, 'install.mjs')], {
    env: { ...process.env, HOME: temp, DSH_HOME: path.join(temp, 'home'), DSH_APP_DIR: path.join(temp, 'app') },
    encoding: 'utf8', timeout: 10000,
  })
  assert.equal(result.status, 0, result.stderr || result.stdout)

  const pluginPkg = JSON.parse(fs.readFileSync(path.join(copied, 'package.json'), 'utf8'))
  assert.equal(pluginPkg.dependencies['@deepseek-ai/dsh-tools'], '0.2.1-rc.3')
  const profileValue = JSON.parse(fs.readFileSync(profilePkg, 'utf8'))
  assert.deepEqual(profileValue.custom, { keep: true })
  assert.equal(profileValue.dependencies.left, '1.0.0')
  assert.ok(profileValue.dsh.profile.bundles.includes('other'))
  assert.ok(profileValue.dsh.profile.bundles.includes('quant-desk'))
  assert.ok(fs.existsSync(`${profilePkg}.bak-quant`))
  assert.equal(fs.readdirSync(profile).some((name) => name.includes('.tmp-quant-')), false)
})


test('the output ceiling is measured in UTF-8 bytes, not in JS string length', () => {
  // 6,000 CJK characters = 6,000 UTF-16 units but 18,000 bytes. Before the fix
  // this passed the check, was dropped by the host whole, and render() reported
  // truncated:false — the model saw nothing and was told everything was fine.
  const payload = '中'.repeat(6000)
  assert.ok(payload.length < 16000)
  assert.ok(Buffer.byteLength(payload, 'utf8') > 16000)

  const rendered = formatJsonOutput({ text: payload })
  assert.equal(rendered.truncated, false, 'a long string is squeezable, not a failure')
  assert.ok(rendered.bytes <= 16000, 'what goes out must fit the byte ceiling')
  assert.ok(Buffer.byteLength(rendered.text, 'utf8') <= 16000)
})

test('a payload that fits is emitted compact, with its byte count reported', () => {
  const rendered = formatJsonOutput({ a: 1, b: [1, 2, 3] })
  assert.equal(rendered.truncated, false)
  assert.equal(rendered.text.includes('\n'), false, 'compact JSON must be one line')
  assert.equal(rendered.bytes, Buffer.byteLength(rendered.text, 'utf8'))
})

test('an oversized answer is shrunk, not discarded — the model keeps the shape', () => {
  const big = {
    symbol: '300750.SZ',
    valuation: { pe: 22.1, percentile: 0.31 },
    events: Array.from({ length: 40 }, (_, i) => ({ id: i, excerpt: '公'.repeat(400) })),
  }
  const rendered = formatJsonOutput(big)
  assert.equal(rendered.truncated, false)
  const out = JSON.parse(rendered.text)
  assert.equal(out.symbol, '300750.SZ')
  assert.equal(out.valuation.pe, 22.1, 'scalars survive untouched')
  assert.ok(out.events.length <= 12)
  assert.equal(out._shrink.level, 1)
  assert.ok(out._shrink.rows_dropped > 0)
  assert.ok(Buffer.byteLength(rendered.text, 'utf8') <= 16000)
})

test('shrinking reports itself so a cut excerpt is visible, not inferred', () => {
  const rendered = formatJsonOutput({ excerpt: '甲'.repeat(6000) })
  const out = JSON.parse(rendered.text)
  assert.equal(rendered.truncated, false)
  assert.ok(out.excerpt.length <= 241)
  assert.ok(out.excerpt.endsWith('…'))
  assert.ok(out._shrink.strings_shortened >= 1)
})

test('the oversized envelope tells the caller what to do next', () => {
  const wide = Object.fromEntries(
    Array.from({ length: 4000 }, (_, i) => [`k${i}`, 'value-value']),
  )
  const rendered = formatJsonOutput(wide)
  const envelope = JSON.parse(rendered.text)
  assert.equal(envelope.truncated, true)
  assert.match(envelope.next, /smaller/)
  assert.ok(envelope.bytes > envelope.max_output)
})

test('a request the host cannot carry is clamped, and the answer says so', async () => {
  const calls = []
  const tools = buildInternalTools({
    request: async (path, opts) => { calls.push([path, opts]); return { days: 60, bars: [] } },
  })
  const bars = tools.find((tool) => tool.name === 'quant_bars')

  const out = await bars.execute({ symbol: '600519.SH', days: 250 }, {})

  assert.equal(calls[0][1].query.days, 60)
  assert.equal(out.window_capped.asked_for, 250)
  assert.equal(out.window_capped.served, 60)
})

test('an in-range window is passed through untouched', async () => {
  const calls = []
  const tools = buildInternalTools({
    request: async (path, opts) => { calls.push([path, opts]); return { days: 20, bars: [] } },
  })
  const out = await tools.find((tool) => tool.name === 'quant_bars')
    .execute({ symbol: '600519.SH', days: 20 }, {})

  assert.equal(calls[0][1].query.days, 20)
  assert.equal(out.window_capped, undefined, '没被夹就不该出现这个键')
})

test('journal full text is capped at the row count that fits', async () => {
  const calls = []
  const tools = buildInternalTools({
    request: async (path, opts) => { calls.push([path, opts]); return { count: 8, entries: [] } },
  })
  const out = await tools.find((tool) => tool.name === 'quant_journal_search')
    .execute({ limit: 50, full: true, recent: 20 }, {})

  // 20 条全文实测 34,047 B；8 是 16 KB 之内的最后一步
  assert.equal(calls[0][1].query.limit, 8)
  assert.equal(calls[0][1].query.recent, 5)
  assert.equal(out.capped.asked_limit, 50)
  assert.equal(out.capped.served_limit, 8)
})

test('recent=0 means "no full text" and must not be replaced by the default', async () => {
  const calls = []
  const tools = buildInternalTools({
    request: async (path, opts) => { calls.push([path, opts]); return { count: 0, entries: [] } },
  })
  await tools.find((tool) => tool.name === 'quant_journal_search')
    .execute({ limit: 20, recent: 0 }, {})

  assert.equal(calls[0][1].query.recent, 0)
})

test('shrink keeps the tail of a time series and the head of a ranking', () => {
  const bars = Array.from({ length: 30 }, (_, i) => ({
    trade_date: `2026${String(1001 + i).padStart(4, '0')}`, close: 10 + i,
  }))
  const shrunk = shrinkJson({ bars }, 0)
  assert.equal(shrunk.bars.length, 12)
  assert.equal(shrunk.bars.at(-1).close, 39, '最后一行必须是最近的，不是最早的')
  assert.equal(shrunk._shrink.kept, 'tail')

  const pool = Array.from({ length: 30 }, (_, i) => ({ symbol: `00000${i}.SZ`, boards: 30 - i }))
  const ranked = shrinkJson({ pool }, 0)
  assert.equal(ranked.pool[0].boards, 30, '榜单类留头部：最强的那只不能丢')
  assert.notEqual(ranked._shrink.kept, 'tail')
})

test('a retry of the same trade reuses one request id, even reworded', () => {
  const trade = { symbol: '600000.SH', side: 'BUY', sleeve: 'CORE', qty: 100 }
  const first = requestIdFor(trade)
  const retry = requestIdFor({ ...trade, thesis: '换了措辞、少了一条证据' })

  assert.equal(retry, first, '同一笔交易的重试必须复用同一个句柄')
  assert.match(first, /^propose-[0-9a-f-]{36}$/)
  assert.notEqual(requestIdFor({ ...trade, qty: 200 }), first, '加仓是另一个请求')
  assert.equal(requestIdFor({ symbol: '600000.SH', client_request_id: 'propose-x1' }),
    'propose-x1', '显式句柄优先，且原样透传')
  assert.notEqual(
    requestIdFor(trade, Date.now() + 6 * 60 * 1000), first, '过期后是新请求')
})

test('a terminal proposal retires its handle so the fix can be re-proposed', async () => {
  // The service promises "same id, same answer" — including "rejected". That is
  // right for a timeout and wrong for a refusal the caller has already read:
  // re-proposing with a corrected thesis used to return the old rejected ticket.
  const trade = { symbol: '600000.SH', side: 'BUY', sleeve: 'CORE', qty: 100 }
  const first = requestIdFor(trade)

  const tools = buildInternalTools({ request: async () => ({}) })
  const propose = tools.find((tool) => tool.name === 'quant_propose')
  assert.equal(typeof propose.after, 'function', 'propose must inspect its answer')

  const returned = propose.after({ status: 'rejected', client_request_id: first }, trade)
  assert.equal(returned.status, 'rejected', 'the answer is passed through unchanged')
  assert.notEqual(requestIdFor(trade), first, 'after a refusal the next attempt is a new request')
  assert.equal(forgetRequestId(first), false, 'retiring twice is a no-op, not an error')
})

test('every tool declares its answer shape so both hosts pick the same ladder', () => {
  // The MCP protocol has no `output` field, so `server.mjs` cannot see which
  // ladder `output.render` would use. The shape is carried on the tool object
  // instead; a table tool that forgets it silently goes back to cutting rows.
  const tools = buildInternalTools()
  const tabular = tools.filter((tool) => tool.outputKind === 'rows')
    .map((tool) => tool.name).sort()

  assert.deepEqual(tabular, [
    'quant_anomaly', 'quant_auction', 'quant_execution_tca', 'quant_knowledge',
    'quant_lhb', 'quant_limit_structure', 'quant_pools', 'quant_screen',
    'quant_tickets',
  ])
  assert.ok(tools.every((tool) => tool.outputKind === 'rows' || tool.outputKind === 'text'))
  assert.equal(shrinkLadder('rows'), SHRINK_LEVELS.rows)
  assert.equal(shrinkLadder('text'), SHRINK_LEVELS.text)
})

test('a table-shaped payload loses prose before rows', () => {
  // 2026-11-04 实测 quant_pools：limit=20 的响应 16,946 B，旧的单套阶梯一上来
  // 就把每池 20 行砍到 12 行——问 20 行只得 12 行，问 16 行却得 16 行，
  // 「问得越多给得越少」。表格工具里行是数据、文字是装饰，所以它走另一套阶梯。
  const rows = Array.from({ length: 20 }, (_, i) => ({
    code: `60000${i}.SH`,
    name: `第${i}只`,
    boards: i % 5,
    note: '这是一段很长的说明文字，用来把体积撑到需要压缩的程度。'.repeat(3),
  }))
  const value = { as_of: '20261104', pools: { limit_up: { rows } },
                  note: '说明'.repeat(200) }

  const table = formatJsonOutput(value, 6000, SHRINK_LEVELS.rows)
  const text = formatJsonOutput(value, 6000, SHRINK_LEVELS.text)

  const kept = (r) => {
    const body = JSON.parse(r.text)
    return body.pools?.limit_up?.rows?.length ?? 0
  }
  assert.ok(table.degraded, '这份载荷应当被压缩')
  assert.ok(kept(table) > kept(text), `表格阶梯应保留更多行：${kept(table)} vs ${kept(text)}`)
  assert.ok(kept(table) >= 12, '表格阶梯至少留住 12 行')
})

// ---------------------------------------------------- 服务端错误不能只剩一句话
test('an error keeps the code, the next step and Retry-After', async (t) => {
  // 2026-11-04 之前的写法只保留 `detail`：/decision/propose 明明回了
  // `409 EXECUTION_EXISTS` 加一句"不要重复提交"，模型看到的却是
  // "quant service 409: <一句异常>"，于是照 500 的提示又提了一次。
  t.after(fakeFetch(() => reply({
    code: 'EXECUTION_EXISTS',
    detail: 'ticket td-20261104-abcd already booked',
    next: '这一笔已经记过账；去账本确认，不要重复提交。',
  }, 409)))
  const client = new QuantClient({ baseUrl: 'http://127.0.0.1:8811', token: 't' })

  await assert.rejects(() => client.request('/decision/x/approve', { method: 'POST' }),
    (error) => {
      assert.equal(error.name, 'ServiceError')
      assert.equal(error.status, 409)
      assert.equal(error.code, 'EXECUTION_EXISTS')
      assert.match(error.next, /不要重复提交/)
      const rendered = JSON.parse(errorText(error))
      assert.equal(rendered.code, 'EXECUTION_EXISTS')
      assert.equal(rendered.status, 409)
      assert.match(rendered.next, /不要重复提交/)
      // DSH 只渲染 error.message，所以那句话必须也在 message 里。
      assert.match(error.message, /EXECUTION_EXISTS/)
      assert.match(error.message, /不要重复提交/)
      return true
    })
})

test('FastAPI HTTPException detail keeps its nested code', async (t) => {
  // `_fail(DecisionError)` 的形状是 {detail: {code, message}}，与应用级
  // {code, detail, next} 不同；只认一种等于让 BAD_SIZE 在工具层消失。
  t.after(fakeFetch(() => reply({
    detail: { code: 'STATE_CONFLICT', message: 'paper_state revision 7 is stale' },
  }, 409)))
  const client = new QuantClient({ baseUrl: 'http://127.0.0.1:8811', token: 't' })

  await assert.rejects(() => client.request('/decision/propose', { method: 'POST' }),
    (error) => {
      assert.equal(error.code, 'STATE_CONFLICT')
      assert.match(error.detail, /revision 7 is stale/)
      assert.match(error.next, /直接重试/, '没有 next 时用 code 的动作表兜底')
      return true
    })
})

test('a 429 carries Retry-After into the rendered error', async (t) => {
  t.after(fakeFetch(() => new Response(JSON.stringify({
    code: 'RATE_LIMITED', detail: 'too many failures', fix: 'read /openapi.json',
  }), { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '60' } })))
  const client = new QuantClient({ baseUrl: 'http://127.0.0.1:8811', token: 't' })

  await assert.rejects(() => client.request('/risk'),
    (error) => {
      const rendered = JSON.parse(errorText(error))
      assert.equal(rendered.retry_after_seconds, 60)
      assert.equal(rendered.fix, 'read /openapi.json')
      return true
    })
})
