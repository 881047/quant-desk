import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const REPORT = path.join(ROOT, 'reports', '20261115-quant-facade-live-mcp.json')
const config = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.workbuddy', 'mcp.json'), 'utf8'))
const entry = config.mcpServers?.['quant-desk']
assert.ok(entry, 'WorkBuddy mcp.json has no quant-desk entry')
const child = spawn(entry.command || 'node', entry.args, {
  cwd: ROOT,
  env: { ...process.env, ...(entry.env || {}) },
  stdio: ['pipe', 'pipe', 'pipe'],
})
let buffer = ''
let stderr = ''
let nextId = 0
const pending = new Map()
child.stdout.setEncoding('utf8')
child.stdout.on('data', (chunk) => {
  buffer += chunk
  let cut
  while ((cut = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, cut).trim()
    buffer = buffer.slice(cut + 1)
    if (!line) continue
    const message = JSON.parse(line)
    const item = pending.get(message.id)
    if (!item) continue
    pending.delete(message.id)
    clearTimeout(item.timer)
    if (message.error) item.reject(new Error(JSON.stringify(message.error)))
    else item.resolve(message.result)
  }
})
child.stderr.setEncoding('utf8')
child.stderr.on('data', (chunk) => { stderr += chunk })

function request(method, params) {
  return new Promise((resolve, reject) => {
    const id = ++nextId
    const timer = setTimeout(() => {
      pending.delete(id)
      reject(new Error(`timeout on ${method}; ${stderr}`))
    }, 70000)
    pending.set(id, { resolve, reject, timer })
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
  })
}

function notify(method, params) {
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`)
}

function payload(result) {
  const text = result.content?.find((item) => item.type === 'text')?.text
  assert.notEqual(result.isError, true, text || 'MCP call failed')
  assert.ok(text, 'missing text payload')
  return JSON.parse(text)
}

const report = {
  verified_at: new Date().toISOString(),
  transport: 'real stdio JSON-RPC using WorkBuddy mcp.json command and env',
  write_operations_attempted: 0,
}
try {
  const initialized = await request('initialize', {
    protocolVersion: '2024-11-05', capabilities: {},
    clientInfo: { name: 'quant-facade-live-verifier', version: '1.0.0' },
  })
  notify('notifications/initialized', {})
  assert.equal(initialized.serverInfo.name, 'quant-desk')
  const listed = await request('tools/list', {})
  const names = listed.tools.map((tool) => tool.name).sort()
  assert.deepEqual(names, ['quant_alert', 'quant_order', 'quant_query', 'quant_workflow'])
  assert.ok(listed.tools.every((tool) => tool.inputSchema.additionalProperties === false))
  report.public_tools = names
  report.hidden_legacy_names_listed = listed.tools.length - names.length

  const catalog = payload(await request('tools/call', {
    name: 'quant_query', arguments: { resource: 'catalog', params: {} },
  }))
  assert.equal(catalog.operations.length, 78)
  report.catalog_operations = catalog.operations.length
  report.catalog_coverage = {
    query: catalog.operations.filter((row) => row.tool === 'quant_query').length,
    order: catalog.operations.filter((row) => row.tool === 'quant_order').length,
    workflow: catalog.operations.filter((row) => row.tool === 'quant_workflow').length,
    alert: catalog.operations.filter((row) => row.tool === 'quant_alert').length,
  }

  const execution = payload(await request('tools/call', {
    name: 'quant_query', arguments: { resource: 'short_alpha_execution', params: { limit: 1 } },
  }))
  assert.ok(execution && typeof execution === 'object')
  report.live_read_canary = {
    resource: 'short_alpha_execution',
    keys: Object.keys(execution).sort(),
    returned_items: Array.isArray(execution.items) ? execution.items.length : null,
  }

  const legacy = payload(await request('tools/call', { name: 'quant_context', arguments: {} }))
  assert.ok(legacy && typeof legacy === 'object')
  report.hidden_legacy_canary = { name: 'quant_context', keys: Object.keys(legacy).sort() }

  const rejected = await request('tools/call', {
    name: 'quant_order',
    arguments: { book: 'external', action: 'propose', params: { symbol: '600000.SH', side: 'BUY' } },
  })
  assert.equal(rejected.isError, true)
  report.external_order_rejected_before_http = true
  report.startup_diagnostics = stderr.trim().split(/\r?\n/).filter(Boolean)
  report.ok = true
} catch (error) {
  report.ok = false
  report.error = error?.stack || String(error)
  report.startup_diagnostics = stderr.trim().split(/\r?\n/).filter(Boolean)
  throw error
} finally {
  fs.writeFileSync(REPORT, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  for (const item of pending.values()) clearTimeout(item.timer)
  child.kill()
}
console.log(JSON.stringify(report, null, 2))
