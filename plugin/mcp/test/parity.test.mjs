/**
 * The two-host answer test.
 *
 * `lib/index.js` is the only tool table, and both hosts are supposed to render
 * the same answer from it. The MCP adapter does not run tools against the
 * service — it records the request, sends it, and formats the response — so any
 * post-processing written inside `run` silently applied to DSH and not to MCP.
 * That is exactly what happened to `quant_bars(days=250)`: DSH said "clamped to
 * 60, here is why", MCP said nothing (measured 2026-11-08).
 *
 * These cases pin the contract at the boundary, not at the table:
 *   - the clamped answer carries `window_capped` on the MCP path;
 *   - both hosts produce byte-identical text for the same payload;
 *   - the hook runs once (no nested `window_capped.window_capped`);
 *   - a legal window (<= 60) announces nothing on either host.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { buildInternalTools, formatJsonOutput, shrinkLadder } from '../../lib/index.js'
import { handleToolCall } from '../server.mjs'

const recording = () => ({
  request: async (requestPath, options = {}) => ({ __request: { path: requestPath, options } }),
})

const BARS = { bars: [{ ts: '20261106', close: 9.5, vol: 1200 }] }

/** A fresh answer per call: the hook annotates in place, and a shared fixture
 *  would leak `window_capped` from one case into the next. */
const answer = () => structuredClone(BARS)

/** Call `quant_bars` through the MCP adapter with a service that returns `payload`. */
async function viaMcp(payload, args) {
  const tools = buildInternalTools(recording())
  const context = {
    index: new Map(tools.map((tool) => [tool.name, tool])),
    real: { request: async () => payload() },
    outputLimit: 16000,
  }
  const result = await handleToolCall({ params: { name: 'quant_bars', arguments: args } }, context)
  return { text: result.content[0].text, isError: result.isError === true }
}

/** Call the same tool the way the DSH host does: real client, real render. */
async function viaDsh(payload, args) {
  const tools = buildInternalTools({ request: async () => payload() })
  const tool = tools.find((entry) => entry.name === 'quant_bars')
  const value = await tool.execute(args, {})
  return formatJsonOutput(value, 16000, shrinkLadder(tool.outputKind)).text
}

test('quant_bars keeps window_capped on the MCP path', async () => {
  const { text, isError } = await viaMcp(answer, { symbol: '600000.SH', days: 250 })
  assert.equal(isError, false)
  const body = JSON.parse(text)
  assert.equal(body.window_capped.asked_for, 250)
  assert.equal(body.window_capped.served, 60)
  assert.match(body.window_capped.why, /27 KB/)
})

test('both hosts render the same bytes for the same answer', async () => {
  const args = { symbol: '600000.SH', days: 250 }
  const mcp = await viaMcp(answer, args)
  const dsh = await viaDsh(answer, args)
  assert.equal(mcp.text, dsh)
})

test('the post-processing hook runs once, not twice', async () => {
  const { text } = await viaMcp(answer, { symbol: '600000.SH', days: 250 })
  const body = JSON.parse(text)
  assert.equal(body.window_capped.window_capped, undefined)
  assert.deepEqual(Object.keys(body).sort(), ['bars', 'window_capped'])
})

test('a legal window announces nothing on either host', async () => {
  const args = { symbol: '600000.SH', days: 60 }
  const mcp = await viaMcp(answer, args)
  const dsh = await viaDsh(answer, args)
  assert.equal(mcp.text, dsh)
  assert.equal(JSON.parse(mcp.text).window_capped, undefined)
})
