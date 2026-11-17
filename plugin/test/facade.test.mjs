import assert from 'node:assert/strict'
import test from 'node:test'

import { buildInternalTools, buildTools, guard } from '../lib/index.js'
import { handleToolCall } from '../mcp/server.mjs'

function sampleValue(spec = {}) {
  if (spec.const !== undefined) return spec.const
  if (spec.enum) return spec.enum[0]
  if (spec.oneOf) return sampleValue(spec.oneOf[0])
  if (spec.type === 'string') return '600000.SH'
  if (spec.type === 'integer') return 1
  if (spec.type === 'number') return 0.5
  if (spec.type === 'boolean') return true
  if (spec.type === 'array') return [sampleValue(spec.items || { type: 'string' })]
  if (spec.type === 'object') {
    const value = {}
    for (const key of spec.required || []) value[key] = sampleValue(spec.properties[key])
    return value
  }
  return 'x'
}

function sampleParams(schema) {
  return sampleValue(schema || { type: 'object', properties: {} })
}

function publicArgs(row, params) {
  if (row.tool === 'quant_query') return { resource: row.operation, params }
  if (row.tool === 'quant_order') {
    const [book, action] = row.operation.split('.')
    return { book, action, params }
  }
  if (row.tool === 'quant_workflow') return { operation: row.operation, params }
  return { action: row.operation, params }
}

function specialParams(row, params) {
  if (row.operation === 'core_preview') return { targets: { '510300.SH': 0.2 } }
  if (row.operation === 'paper.propose') {
    return {
      ...params,
      thesis: '银行板块二十日动量转正，估值处于低位，核心仓回补到两成。',
      evidence: ['daily:20261023'], invalidation: '跌破九元二角四分', qty: 100,
    }
  }
  if (row.operation === 'paper.approve' || row.operation === 'samples.approve') {
    return { ...params, ticket_id: 'td-20261028-abcdef12', confirm: true }
  }
  if (row.operation === 'short_alpha_judge') {
    return {
      day: '20261113', reviewed_evidence: ['evidence-1'], items: [],
      no_candidate_reason: '证据不足，明确无候选', client_request_id: 'facade-contract-judge',
    }
  }
  if (row.operation === 'short_alpha_review') {
    return { ...params, day: '20261113', client_request_id: 'facade-contract-review' }
  }
  if (row.tool === 'quant_alert' && row.operation === 'answer') {
    return { id: 'pending-test', analysis: '第一句有数字 1。第二句有数字 2。第三句说明缺口。', confirm: true }
  }
  return params
}

test('four public façades cover every non-retired internal handler', async () => {
  const client = { request: async () => ({ ok: true }) }
  const publicTools = buildTools(client)
  const internal = buildInternalTools(client)
  assert.deepEqual(publicTools.map((tool) => tool.name), [
    'quant_query', 'quant_order', 'quant_workflow', 'quant_alert',
  ])
  assert.equal(internal.length, 73)
  assert.equal(new Set(internal.map((tool) => tool.name)).size, 73)

  const query = publicTools[0]
  const compact = await query.execute({ resource: 'catalog', params: {} }, {})
  assert.equal(compact.operations.length, 78)
  assert.ok(compact.operations.every((row) => !row.paramsSchema))
  const covered = new Set(compact.operations.map((row) => row.legacy))
  assert.equal(covered.size, 71)
  assert.equal(covered.has('quant_core_plan'), false)
  assert.equal(covered.has('quant_core_execute'), false)
  for (const legacy of covered) assert.ok(internal.some((tool) => tool.name === legacy))
})

test('catalog exposes strict per-operation schemas without mixing read and write', async () => {
  const query = buildTools({ request: async () => ({}) })[0]
  const read = async (operation) => (await query.execute({
    resource: 'catalog', params: { operation, full: true },
  }, {})).operations[0]

  const hypothesis = await read('hypothesis')
  assert.equal(hypothesis.paramsSchema.properties.action, undefined)
  assert.ok(!hypothesis.paramsSchema.required?.includes('action'))
  const memo = await read('memo')
  assert.deepEqual(memo.paramsSchema.properties.action.enum, ['list', 'calibration'])
  assert.ok(memo.paramsSchema.required.includes('action'))
  const approve = await read('paper.approve')
  assert.equal(approve.paramsSchema.properties.confirm.const, true)
  const result = await read('hypothesis_result')
  assert.equal(result.paramsSchema.properties.action, undefined)
  assert.ok(result.paramsSchema.required.includes('hyp_id'))
  assert.ok(result.paramsSchema.required.includes('status'))
  const alert = await read('answer')
  assert.equal(alert.externalEffect, 'mail')
  assert.deepEqual(alert.paramsSchema.properties.confirm.enum, [true])

  await assert.rejects(
    () => query.execute({ resource: 'catalog', params: { full: true } }, {}),
    /requires operation/,
  )
})

test('all 78 public operations reach their original handler with valid minimum inputs', async () => {
  const calls = []
  const client = { request: async (path, options) => { calls.push([path, options]); return { ok: true } } }
  const tools = new Map(buildTools(client).map((tool) => [tool.name, tool]))
  const query = tools.get('quant_query')
  const compact = await query.execute({ resource: 'catalog', params: {} }, {})
  const missed = []
  for (const row of compact.operations) {
    const detail = (await query.execute({
      resource: 'catalog', params: { operation: row.operation, full: true },
    }, {})).operations.find((candidate) => candidate.tool === row.tool)
    let params = specialParams(row, sampleParams(detail.paramsSchema))
    const before = calls.length
    try {
      await tools.get(row.tool).execute(publicArgs(row, params), {})
    } catch (error) {
      missed.push(`${row.tool}.${row.operation}: ${error.message}`)
    }
    if (calls.length !== before + 1) missed.push(`${row.tool}.${row.operation}: expected exactly one request`)
  }
  assert.deepEqual(missed, [])
  assert.equal(calls.length, 78)
})

test('facades reject arbitrary transport, invalid books and read/write confusion before HTTP', async () => {
  const calls = []
  const client = { request: async () => { calls.push(1); return {} } }
  const tools = new Map(buildTools(client).map((tool) => [tool.name, tool]))
  const query = tools.get('quant_query')
  const order = tools.get('quant_order')
  const workflow = tools.get('quant_workflow')
  const alert = tools.get('quant_alert')

  await assert.rejects(() => query.execute({
    resource: 'pools', params: { path: '/decision/propose', method: 'POST' },
  }, {}), /unknown argument\(s\) for quant_pools/)
  await assert.rejects(() => query.execute({
    resource: 'pools', params: {}, path: '/decision/propose',
  }, {}), /unknown argument\(s\) for quant_query: path/)
  await assert.rejects(() => query.execute({
    resource: 'memo', params: { action: 'add', topic: 'x', thesis: 'y' },
  }, {}), /read-only/)
  await assert.rejects(() => query.execute({
    resource: 'hypothesis', params: { action: 'add' },
  }, {}), /fixed by operation/)
  await assert.rejects(() => order.execute({
    book: 'external', action: 'propose', params: {},
  }, {}), /external is read-only|must be one of/)
  await assert.rejects(() => order.execute({
    book: 'samples', action: 'cancel', params: { ticket_id: 's-x' },
  }, {}), /unknown operation: samples.cancel/)
  await assert.rejects(() => workflow.execute({
    operation: 'paper.approve', params: {},
  }, {}), /unknown operation: paper.approve|must be one of/)
  await assert.rejects(() => alert.execute({
    action: 'answer', params: { id: 'x', analysis: 'y', confirm: false },
  }, {}), /confirm=true|must be one of/)
  assert.equal(calls.length, 0)
})

test('new order facade keeps proposal and approval guards', async () => {
  const client = { request: async () => ({}) }
  const order = buildTools(client).find((tool) => tool.name === 'quant_order')
  const lazy = {
    book: 'paper', action: 'propose',
    params: { symbol: '600000.SH', side: 'BUY', thesis: '想买', evidence: [], invalidation: '' },
  }
  assert.match(guard({ name: 'quant_order', arguments: lazy }), /thesis too short/)
  await assert.rejects(() => order.execute(lazy, {}), /thesis too short/)
  const approve = {
    book: 'paper', action: 'approve',
    params: { ticket_id: 'td-20261028-abcdef12', confirm: false },
  }
  assert.match(guard({ name: 'quant_order', arguments: approve }), /confirm=true/)
  await assert.rejects(() => order.execute(approve, {}), /confirm=true/)
})

test('dynamic output kind and after hook survive both DSH and MCP paths', async () => {
  const dshClient = { request: async () => ({ bars: [] }) }
  const dshQuery = buildTools(dshClient).find((tool) => tool.name === 'quant_query')
  assert.equal(dshQuery.outputKindFor({ resource: 'pools', params: {} }), 'rows')
  assert.equal(dshQuery.outputKindFor({ resource: 'context', params: {} }), 'text')
  const direct = await dshQuery.execute({ resource: 'bars', params: { symbol: '600000.SH', days: 250 } }, {})
  assert.equal(direct.window_capped.served, 60)

  const recording = { request: async (path, options) => ({ __request: { path, options } }) }
  const mcpQuery = buildTools(recording).find((tool) => tool.name === 'quant_query')
  const index = new Map([['quant_query', mcpQuery]])
  const response = await handleToolCall({
    params: { name: 'quant_query', arguments: { resource: 'bars', params: { symbol: '600000.SH', days: 250 } } },
  }, {
    index,
    real: { request: async () => ({ bars: [] }) },
    outputLimit: 16000,
  })
  const body = JSON.parse(response.content[0].text)
  assert.equal(body.window_capped.served, 60)
})
