/**
 * End-to-end test for the MCP adapter: spawn the real server, talk JSON-RPC
 * over stdio, and point it at a stub HTTP service so no live data is needed.
 *
 * What must hold:
 *   - the tool list matches the DSH plugin (one source of truth, no drift)
 *   - DSH parameter maps survive the trip as standard JSON Schema
 *   - a call reaches the right route with the right query/body
 *   - the plugin guards still refuse lazy proposals, even over MCP
 */

import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { buildInternalTools, buildTools, ServiceError } from '../../lib/index.js'
import { buildServer, DEFAULT_TIMEOUT_MS, handleToolCall, listTools, toJsonSchema } from '../server.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SERVER = path.join(HERE, '..', 'server.mjs')
const TOKEN = 'test-token'

function startStub() {
  const calls = []
  const server = http.createServer((req, res) => {
    let raw = ''
    req.on('data', (chunk) => { raw += chunk })
    req.on('end', () => {
      const url = new URL(req.url, 'http://127.0.0.1')
      const payload = {
        ok: true,
        path: url.pathname,
        method: req.method,
        query: Object.fromEntries(url.searchParams),
        body: raw ? JSON.parse(raw) : null,
        token: req.headers['x-quant-token'] || null,
      }
      calls.push(payload)
      // A table-shaped body that only fits once prose is cut: 20 rows of ~1 KB.
      // `/market/pools` is the tool the 2026-11-04 measurement used (16,946 B at
      // limit=20), so the stub returns that shape rather than an echo.
      if (url.pathname === '/market/pools') {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({
          as_of: '20261104',
          pools: {
            limit_up: {
              rows: Array.from({ length: 20 }, (_, i) => ({
                code: `60000${i}.SH`,
                name: `第${i}只`,
                boards: i % 5,
                note: '封单很强，换手充分，题材集中，是一段用来把响应撑过预算的说明。'.repeat(10),
              })),
            },
          },
        }))
        return
      }
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(payload))
    })
  })
  return { server, calls }
}

class Rpc {
  constructor(child) {
    this.child = child
    this.buffer = ''
    this.pending = new Map()
    this.stderr = ''
    this.nextId = 0
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      this.buffer += chunk
      let cut
      while ((cut = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, cut).trim()
        this.buffer = this.buffer.slice(cut + 1)
        if (!line) continue
        let message
        try {
          message = JSON.parse(line)
        } catch {
          const error = new Error(`non-JSON stdout from MCP server: ${line}`)
          for (const pending of this.pending.values()) {
            clearTimeout(pending.timer)
            pending.reject(error)
          }
          this.pending.clear()
          return
        }
        const pending = this.pending.get(message.id)
        if (pending) {
          this.pending.delete(message.id)
          clearTimeout(pending.timer)
          if (message.error) pending.reject(new Error(JSON.stringify(message.error)))
          else pending.resolve(message.result)
        }
      }
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => { this.stderr += chunk })
  }

  request(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++this.nextId
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`timeout on ${method}; stderr=${this.stderr}`))
      }, 8000)
      this.pending.set(id, { resolve, reject, timer })
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
    })
  }

  notify(method, params) {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`)
  }
}

test('MCP adapter mirrors the plugin tools and guards', async (t) => {
  const { server: stub, calls } = startStub()
  await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve))
  const port = stub.address().port

  const child = spawn(process.execPath, [SERVER], {
    env: { ...process.env, QUANT_BASE_URL: `http://127.0.0.1:${port}`, QUANT_TOKEN: TOKEN },
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const rpc = new Rpc(child)

  t.after(() => {
    child.kill()
    stub.close()
  })

  const init = await rpc.request('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'quant-desk-test', version: '0.0.0' },
  })
  rpc.notify('notifications/initialized', {})

  assert.equal(init.serverInfo.name, 'quant-desk')
  assert.match(init.instructions || '', /内核/)
  assert.ok(init.capabilities.tools)

  const { tools } = await rpc.request('tools/list', {})
  const expectedNames = buildTools().map((tool) => tool.name).sort()
  assert.deepEqual(tools.map((tool) => tool.name).sort(), expectedNames)
  assert.equal(new Set(tools.map((tool) => tool.name)).size, tools.length)
  assert.ok(tools.every((tool) => tool.name.startsWith('quant_')))
  assert.ok(tools.every((tool) => tool.inputSchema.type === 'object'))
  // 未知字段必须在 schema 这一层就关掉：SDK 自己的 parameterSchemaSpecToJsonSchema
  // 不写 additionalProperties，于是 `directedBy` / `limt` 在客户端看来是合法的。
  assert.ok(
    tools.every((tool) => tool.inputSchema.additionalProperties === false),
    '每个工具的 inputSchema 都必须 additionalProperties:false',
  )

  const query = tools.find((tool) => tool.name === 'quant_query')
  assert.deepEqual(query.inputSchema.required, ['resource'])
  assert.ok(query.inputSchema.properties.resource.enum.includes('catalog'))
  assert.ok(query.inputSchema.properties.resource.enum.includes('context'))
  assert.equal(query.inputSchema.properties.params.additionalProperties, true)

  const order = tools.find((tool) => tool.name === 'quant_order')
  assert.deepEqual([...order.inputSchema.required].sort(), ['action', 'book'])
  assert.deepEqual(order.inputSchema.properties.book.enum, ['paper', 'samples'])
  assert.deepEqual(order.inputSchema.properties.action.enum, ['propose', 'approve', 'reject', 'cancel'])

  const workflow = tools.find((tool) => tool.name === 'quant_workflow')
  assert.deepEqual(workflow.inputSchema.required, ['operation'])
  assert.ok(workflow.inputSchema.properties.operation.enum.includes('short_alpha_judge'))

  const alert = tools.find((tool) => tool.name === 'quant_alert')
  assert.deepEqual(alert.inputSchema.required, ['action'])
  assert.deepEqual(alert.inputSchema.properties.params.properties.confirm.enum, [true])

  await t.test('a public query reaches the right route', async () => {
    const result = await rpc.request('tools/call', {
      name: 'quant_query', arguments: { resource: 'context', params: {} },
    })
    assert.ok(!result.isError)
    const payload = JSON.parse(result.content[0].text)
    assert.equal(payload.path, '/context_pack')
    assert.equal(payload.method, 'GET')
    assert.equal(payload.token, TOKEN)
  })

  await t.test('a hidden legacy alias remains callable without being listed', async () => {
    const result = await rpc.request('tools/call', { name: 'quant_context', arguments: {} })
    assert.ok(!result.isError)
    assert.equal(JSON.parse(result.content[0].text).path, '/context_pack')
    assert.equal(tools.some((tool) => tool.name === 'quant_context'), false)
  })

  await t.test('query parameters are typed and forwarded', async () => {
    await rpc.request('tools/call', {
      name: 'quant_bars',
      arguments: { symbol: '600000.SH', days: 30 },
    })
    const call = calls.at(-1)
    assert.equal(call.path, '/market/bars')
    assert.deepEqual(call.query, { symbol: '600000.SH', days: '30' })
  })

  await t.test('a write tool posts the documented body', async () => {
    await rpc.request('tools/call', {
      name: 'quant_approve',
      arguments: { ticket_id: 'td-20261028-abcdef12', confirm: true },
    })
    const call = calls.at(-1)
    assert.equal(call.path, '/decision/td-20261028-abcdef12/approve')
    assert.equal(call.method, 'POST')
    assert.deepEqual(call.body, { confirm: true })
  })

  await t.test('a misspelled argument is an error, not a default', async () => {
    const before = calls.length
    const bad = await rpc.request('tools/call', {
      name: 'quant_auction',
      arguments: { limt: 5 },
    })
    assert.equal(bad.isError, true)
    assert.match(bad.content[0].text, /unknown argument\(s\) for quant_auction: limt/)
    assert.match(bad.content[0].text, /legal: as_of, limit/)
    assert.equal(calls.length, before, 'cannot reach the service with a dropped field')
  })

  await t.test('guards still refuse over MCP', async () => {
    const lazy = await rpc.request('tools/call', {
      name: 'quant_propose',
      arguments: { symbol: '600000.SH', side: 'BUY', thesis: '想买', evidence: [], invalidation: '' },
    })
    assert.equal(lazy.isError, true)
    assert.match(lazy.content[0].text, /thesis too short/)

    const smuggled = await rpc.request('tools/call', {
      name: 'quant_propose',
      arguments: {
        symbol: '600000.SH',
        side: 'BUY',
        thesis: '银行板块 20 日动量转正，估值处于低位，核心仓回补到两成。',
        evidence: ['daily:20261028'],
        invalidation: '收盘跌破 9.24',
        price: 9.5,
      },
    })
    assert.equal(smuggled.isError, true)
    assert.match(smuggled.content[0].text, /price must be computed/)

    const noConfirm = await rpc.request('tools/call', {
      name: 'quant_approve',
      arguments: { ticket_id: 'td-20261028-abcdef12', confirm: false },
    })
    assert.equal(noConfirm.isError, true)
    assert.match(noConfirm.content[0].text, /confirm=true/)
  })

  await t.test('bounded quantity and explicit attribution fields are forwarded', async () => {
    const result = await rpc.request('tools/call', {
      name: 'quant_propose',
      arguments: {
        symbol: '600000.SH',
        side: 'BUY',
        thesis: '银行板块 20 日动量转正，估值处于低位，核心仓回补到两成。',
        evidence: ['daily:20261028'],
        invalidation: '收盘跌破 9.24',
        qty: 100,
        strategy: 'llm',
        persona: 'retail',
      },
    })
    assert.ok(!result.isError)
    const call = calls.at(-1)
    assert.equal(call.body.qty, 100)
    assert.equal(call.body.strategy, 'llm')
    assert.equal(call.body.persona, 'retail')
    assert.equal(call.body.price, undefined)
    assert.match(call.body.client_request_id, /^propose-/,
      '提案必须带幂等句柄，否则超时重提就是第二张票')
  })

  await t.test('ambiguous quantity and weight are refused', async () => {
    const result = await rpc.request('tools/call', {
      name: 'quant_propose',
      arguments: {
        symbol: '600000.SH', side: 'BUY', qty: 100, target_weight: 0.2,
        thesis: '银行板块 20 日动量转正，估值处于低位，核心仓回补到两成。',
        evidence: ['daily:20261028'], invalidation: '收盘跌破 9.24',
      },
    })
    assert.equal(result.isError, true)
    assert.match(result.content[0].text, /mutually exclusive/)
  })

  await t.test('oversized output is shrunk and still counts as a success', async () => {
    const result = await rpc.request('tools/call', {
      name: 'quant_journal_append', arguments: { text: 'x'.repeat(20000) },
    })
    // `isError` is MCP's "the call failed". A shrunk answer did not fail: the
    // shape is intact and `_shrink` says what was cut. Sending isError:true here
    // is what made the same condition look like a failure on this host and like
    // data on the DSH host. Absent is the protocol's "no error".
    assert.ok(!result.isError)
    const payload = JSON.parse(result.content[0].text)
    assert.equal(payload.truncated ?? false, false)
    assert.ok(payload._shrink, 'the cut must be visible, not inferred')
    assert.ok(result.content[0].text.length < 16000)
  })

  await t.test('a table over budget loses prose before rows', async () => {
    // Before: this adapter called `formatJsonOutput` without a ladder, so every
    // tool took the *text* ladder (maxArray 12) while the DSH host read the
    // `rows` ladder through `output.render`. `quant_pools(limit=20)` answered 12
    // rows here and 20 there — same tool, same query, different data.
    const result = await rpc.request('tools/call', {
      name: 'quant_pools', arguments: { limit: 20 },
    })
    assert.ok(!result.isError)
    const payload = JSON.parse(result.content[0].text)
    assert.equal(payload.pools.limit_up.rows.length, 20, '行是数据，20 行一行都不能少')
    assert.ok(payload._shrink, '压过就要说，不能静默')
    assert.equal(payload._shrink.rows_dropped, 0)
    assert.ok(payload._shrink.strings_shortened > 0)
    assert.ok(result.content[0].text.length < 16000)
  })

  await t.test('unknown tools fail loudly', async () => {
    const result = await rpc.request('tools/call', { name: 'quant_nope', arguments: {} })
    assert.equal(result.isError, true)
    assert.match(result.content[0].text, /unknown tool/)
  })
})

/**
 * The CallTool body, in-process.
 *
 * 上面那组用例走的是真进程：它们证明"这条路走得通"，但跑在子进程里，覆盖率收不到。
 * `server.mjs` 因此长期只有 43% 行覆盖，而漏掉的正是这里——一个工具失败时，
 * 模型读到的到底是"服务说 X"还是"我不知道"，是由这几行决定的。
 */
test('a call decides error, local answer or request in that order', async (t) => {
  const seen = []
  const tool = (name, run) => ({
    name,
    description: name,
    outputKind: 'rows',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    execute: run,
  })
  const registry = [
    tool('quant_local', async () => ({ answer: 42 })),
    tool('quant_planned', async () => ({ __request: { path: '/x', options: { query: { a: 1 } } } })),
    tool('quant_boom', async () => { throw new Error('boom') }),
    tool('quant_http', async () => {
      throw new ServiceError({ status: 503, code: 'STORE_BUSY', detail: '锁住了', next: '稍后重试' })
    }),
  ]
  const real = { request: async (p, o) => { seen.push([p, o]); return { rows: [1, 2] } } }
  const context = {
    index: new Map(registry.map((row) => [row.name, row])),
    real,
    outputLimit: 16000,
  }
  const call = (name, args = {}) => handleToolCall({ params: { name, arguments: args } }, context)

  await t.test('an unknown name is an error, not an empty answer', async () => {
    const out = await call('quant_nope')
    assert.equal(out.isError, true)
    assert.match(out.content[0].text, /unknown tool: quant_nope/)
  })

  await t.test('a tool that answered locally never touches HTTP', async () => {
    const out = await call('quant_local')
    assert.equal(out.isError, undefined)
    assert.match(out.content[0].text, /"answer":\s*42/)
    assert.equal(seen.length, 0)
  })

  await t.test('a planned request goes out with the tool\'s own path and options', async () => {
    const out = await call('quant_planned')
    assert.equal(seen.at(-1)[0], '/x')
    assert.deepEqual(seen.at(-1)[1], { query: { a: 1 } })
    assert.match(out.content[0].text, /"rows"/)
  })

  await t.test('a thrown error keeps its message', async () => {
    const out = await call('quant_boom')
    assert.equal(out.isError, true)
    assert.equal(out.content[0].text, 'boom')
  })

  await t.test('a service error keeps its envelope instead of a sentence', async () => {
    const out = await call('quant_http')
    assert.equal(out.isError, true)
    const body = JSON.parse(out.content[0].text)
    assert.equal(body.code, 'STORE_BUSY')
  })

  await t.test('a body that cannot fit is a result with a next step, not a failure', async () => {
    // The shrink ladder cuts strings and rows, never keys. Two hundred keys of
    // 200 chars stay ~13 KB even at `maxString: 60`, so a 128-byte ceiling is
    // the one shape `formatJsonOutput` cannot fit: it answers `truncated: true`
    // and a `next` telling the caller to ask for less.
    //
    // This branch used to be sent as `isError`, which in MCP means "the call
    // failed" — and a client that hears "failed" retries the same request, the
    // one request guaranteed to fail again. DSH rendered the same envelope as
    // data. Same condition, two semantics; this pins the one both hosts share.
    const wide = Object.fromEntries(
      Array.from({ length: 200 }, (_, i) => [`k${i}`, 'x'.repeat(200)]))
    const out = await handleToolCall({ params: { name: 'quant_wide', arguments: {} } }, {
      index: new Map([['quant_wide', tool('quant_wide', async () => wide)]]),
      real: { request: async () => wide },
      outputLimit: 128,
    })

    assert.equal(out.isError, undefined, 'isError 会让客户端重试同一个超限请求')
    const body = JSON.parse(out.content[0].text)
    assert.equal(body.truncated, true)
    assert.match(body.next, /smaller/)
    assert.ok(body.bytes > body.max_output)
  })

  await t.test('a request that fails after planning is also an error envelope', async () => {
    // 两条失败路径不一样：工具自己抛（上面那条），和"请求发出去了、服务端拒"。
    // 后者才是 409/422 的常见形状，而它走的是另一个 catch。
    const out = await handleToolCall({ params: { name: 'quant_planned', arguments: {} } }, {
      index: new Map([['quant_planned', registry[1]]]),
      real: {
        request: async () => {
          throw new ServiceError({ status: 500, code: 'INTERNAL', detail: '炸了' })
        },
      },
      outputLimit: 16000,
    })
    assert.equal(out.isError, true)
    assert.equal(JSON.parse(out.content[0].text).code, 'INTERNAL')
  })
})

test('the guard runs before the tool, over MCP too', async () => {
  // e2e 里也有这条，但那是在子进程里跑真服务。这一条盯的是顺序：guard 拒了，
  // 工具一次都不该执行——否则"提案缺理由"会变成一次真的下单尝试。
  const tools = buildTools({ request: async () => ({}) })
  let executed = 0
  const registry = tools.map((row) => ({
    ...row,
    execute: async (args) => { executed += 1; return row.execute(args) },
  }))
  const context = {
    index: new Map(registry.map((row) => [row.name, row])),
    real: { request: async () => ({}) },
    outputLimit: 16000,
  }

  const out = await handleToolCall(
    { params: { name: 'quant_order', arguments: { book: 'paper', action: 'propose', params: { qty: 100 } } } }, context)

  assert.equal(out.isError, true)
  assert.match(out.content[0].text, /thesis too short/)
  assert.equal(executed, 0, 'guard 拦下的调用不该进到 run')
})

test('the shorthand path still produces a closed schema', () => {
  // `define` 里的 parameters 经 SDK 规范化后已经是 object schema，于是
  // `toJsonSchema` 的**第一半**（pass-through）在测试里总被走到，第二半——
  // 不装 SDK 时的降级转换——一次也没跑过。它是"SDK 缺失"那条路的唯一保险，
  // 而那条路平时没人走，坏了也不会有人立刻发现。
  const schema = toJsonSchema({
    symbol: { type: 'string', required: true },
    limit: { type: 'integer', description: '1-15 rows' },
  })

  assert.deepEqual(schema, {
    type: 'object',
    properties: {
      symbol: { type: 'string' },
      limit: { type: 'integer', description: '1-15 rows' },
    },
    required: ['symbol'],
    additionalProperties: false,
  })
  // 空参数也要是一个合法的 object schema，而不是 `{}`：客户端要能读到
  // "这个工具不接受任何字段"，否则它会照旧传。
  assert.deepEqual(toJsonSchema(), {
    type: 'object', properties: {}, additionalProperties: false,
  })
})

test('the published schema is closed for every tool, and the table can build itself', async () => {
  const listed = listTools(buildTools({ request: async () => ({}) }))

  assert.equal(listed.length, 4)
  assert.equal(new Set(listed.map((t) => t.name)).size, 4)
  assert.ok(listed.every((row) => row.inputSchema.type === 'object'))
  assert.ok(listed.every((row) => row.inputSchema.additionalProperties === false),
    'schema 是客户端唯一能读到的契约：不关掉 additionalProperties，它就会以为 limt 合法')

  // 不传 tools 时它自己造一份 registry。这条默认路径以前只出现在 `main()` 里，
  // 而 `main()` 在覆盖率之外——于是它坏没坏，没有任何信号。
  const server = buildServer()
  assert.equal(typeof server.setRequestHandler, 'function')
})

test('quant_knowledge 的参数描述与主描述同步列出全部 per-instrument topic', () => {
  // 2026-11-10 宿主重连后发现：主描述加了只读的 timing_distribution，
  // 但 topic / symbol 两个参数描述仍列 7 个并写着 "seven"。模型读参数说明时
  // 会以为第 8 个 topic 不存在。工具数没变，漂移只在文案里，所以必须锁死。
  const knowledge = buildInternalTools({ request: async () => ({}) })
    .find((row) => row.name === 'quant_knowledge')
  assert.ok(knowledge)
  const topic = knowledge.parameters.properties.topic.description
  const symbol = knowledge.parameters.properties.symbol.description
  for (const key of ['crowding', 'position', 'liquidity', 'distribution',
                     'analog', 'board', 'industry_rank', 'timing_distribution']) {
    assert.ok(knowledge.description.includes(key), `主描述缺 ${key}`)
    assert.ok(topic.includes(key), `topic 参数描述缺 ${key}`)
    assert.ok(symbol.includes(key), `symbol 参数描述缺 ${key}`)
  }
  assert.ok(!topic.includes('seven'), 'per-instrument 已有 8 个，不能再写 seven')
})

test('README 里的传输参数与代码一致', () => {
  // 2026-11-05 之前这里写着 20000，而 server.mjs 早已是 60000；文档是接入时
  // 唯一会读的东西，照它调小超时会把一次 17.6 s 的重读误报成"服务不可达"。
  const readme = fs.readFileSync(path.join(HERE, '..', 'README.md'), 'utf8')
  const timeoutRow = readme.split('\n').find((line) => line.includes('`QUANT_TIMEOUT_MS`'))
  assert.ok(timeoutRow, 'README 的环境变量表里少了 QUANT_TIMEOUT_MS')
  assert.ok(timeoutRow.includes(`\`${DEFAULT_TIMEOUT_MS}\``),
    `README 写的超时默认值与代码的 ${DEFAULT_TIMEOUT_MS} 不一致`)

  const outputRow = readme.split('\n').find((line) => line.includes('`QUANT_MAX_OUTPUT`'))
  assert.ok(outputRow, 'README 的环境变量表里少了 QUANT_MAX_OUTPUT')
  assert.match(outputRow, /UTF-8 字节/,
    '上限按字节算，写成"字符"会让中文响应在宿主那里整包丢掉')
})

test('MCP output limit is validated at startup', () => {  for (const maxOutput of [0, 127, 1.5, Infinity, NaN, 1000001]) {
    assert.throws(() => buildServer({ maxOutput, tools: [] }), /maxOutput/)
  }
})

test('the JSON-RPC test harness fails loudly on non-JSON stdout', async (t) => {
  const child = spawn(process.execPath, ['-e', 'process.stdout.write("not-json\\n"); setTimeout(() => {}, 10000)'], {
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const rpc = new Rpc(child)
  t.after(() => child.kill())
  await assert.rejects(() => rpc.request('tools/list', {}), /non-JSON stdout/)
})

test('MCP installer preserves unmanaged fields and parses dotenv in a temp HOME', (t) => {
  const source = path.resolve(HERE, '..')
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'quant-desk-mcp-install-'))
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }))
  const copied = path.join(temp, 'repo', 'plugin', 'mcp')
  fs.mkdirSync(copied, { recursive: true })
  fs.copyFileSync(path.join(source, 'install.mjs'), path.join(copied, 'install.mjs'))
  fs.cpSync(path.join(source, '..', 'skills'), path.join(temp, 'repo', 'plugin', 'skills'), { recursive: true })
  fs.writeFileSync(path.join(temp, 'repo', '.env'), 'export QUANT_TOKEN="quoted token" # comment\n')
  const sdk = path.join(copied, 'node_modules', '@modelcontextprotocol', 'sdk')
  fs.mkdirSync(sdk, { recursive: true })
  fs.writeFileSync(path.join(sdk, 'package.json'), '{}')

  const workbuddy = path.join(temp, 'home')
  fs.mkdirSync(workbuddy, { recursive: true })
  const configPath = path.join(workbuddy, 'mcp.json')
  const original = {
    keepTop: true,
    mcpServers: {
      other: { command: 'other-command', custom: 1 },
      'quant-desk': {
        command: 'old-command', args: ['old'], custom: { keep: true }, disabled: false,
        env: { KEEP_ENV: 'yes', QUANT_TOKEN: 'old', QUANT_BASE_URL: 'http://127.0.0.1:9999' },
      },
    },
  }
  fs.writeFileSync(configPath, JSON.stringify(original, null, 2))
  const env = { ...process.env, HOME: temp, WORKBUDDY_HOME: workbuddy }
  delete env.QUANT_TOKEN
  delete env.QUANT_BASE_URL
  delete env.QUANT_ALLOW_REMOTE
  const result = spawnSync(process.execPath, [path.join(copied, 'install.mjs')], {
    env, encoding: 'utf8', timeout: 10000,
  })
  assert.equal(result.status, 0, result.stderr || result.stdout)

  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'))
  const installed = config.mcpServers['quant-desk']
  assert.equal(config.keepTop, true)
  assert.equal(config.mcpServers.other.custom, 1)
  assert.deepEqual(installed.custom, { keep: true })
  assert.equal(installed.disabled, false)
  assert.equal(installed.env.KEEP_ENV, 'yes')
  assert.equal(installed.env.QUANT_TOKEN, 'quoted token')
  assert.equal(installed.env.QUANT_BASE_URL, 'http://127.0.0.1:9999')
  assert.equal(installed.command, 'node')
  assert.deepEqual(JSON.parse(fs.readFileSync(`${configPath}.bak-quant`, 'utf8')), original)
  assert.equal(fs.readdirSync(workbuddy).some((name) => name.includes('.tmp-quant-')), false)
})

test('a note written on the planned request survives to the answer', async () => {
  // `paged()` clamps a row count and annotates whatever `run` returned. On this
  // host that was the `{__request}` placeholder, so `quant_auction(limit=200)`
  // answered 40 rows with nothing saying which 40 — while DSH carried `capped`.
  const planned = {
    name: 'quant_capped',
    outputKind: 'rows',
    description: 'stub',
    inputSchema: { type: 'object', properties: {} },
    execute: async () => ({
      __request: { path: '/market/pools', options: {} },
      capped: { asked_limit: 200, served_limit: 40, why: 'ceiling' },
    }),
  }
  const out = await handleToolCall(
    { params: { name: 'quant_capped', arguments: { limit: 200 } } },
    {
      index: new Map([['quant_capped', planned]]),
      real: { request: async () => ({ rows: [{ code: '600000.SH' }] }) },
      outputLimit: 16000,
    },
  )
  assert.equal(out.isError, undefined)
  const body = JSON.parse(out.content[0].text)
  assert.deepEqual(body.capped, { asked_limit: 200, served_limit: 40, why: 'ceiling' })
  assert.equal(body.rows.length, 1)
})
