#!/usr/bin/env node
/**
 * quant-desk — MCP server (stdio).
 *
 * The third host adapter for the same kernel, next to the HTTP API itself and
 * the DSH plugin. It contains no business logic and no duplicated tool table:
 * it imports `buildTools` from `../lib/index.js` and reuses every definition,
 * description and guard verbatim, so a tool added to the DSH plugin appears
 * here without a second edit.
 *
 * How a call flows:
 *   1. `guard()` from the plugin rejects illegal arguments (same rules).
 *   2. The tool runs against a *dry* client that records the HTTP request it
 *      would have made instead of sending it. Tools are thin clients, so this
 *      yields {path, method, query, body} without hard-coding route tables.
 *   3. The recorded request is executed by the real `QuantClient`, which owns
 *      the base URL, the token and the error text.
 *
 * Everything on stdout belongs to the protocol; diagnostics go to stderr.
 */

import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'

import {
  DEFAULT_TOKEN, PROMPT, QuantClient, buildInternalTools, buildTools, errorText, formatJsonOutput, guard,
  integerOption, name as PLUGIN_NAME, shrinkLadder,
} from '../lib/index.js'

const DEFAULT_BASE_URL = 'http://127.0.0.1:8811'
const DEFAULT_MAX_OUTPUT = 16000
/**
 * 60 s, not 20: the service is a single-process GIL server and a heavy read
 * under CPU contention was measured at 17.6 s (2026-11-02). A 20 s ceiling
 * reported that as "service unreachable", which sends the reader to the wrong
 * remedy. Exported so the README's documented default can be checked against
 * this one instead of being copied.
 */
export const DEFAULT_TIMEOUT_MS = 60000

/**
 * Parameters -> JSON Schema. The DSH SDK already normalises the plugin's
 * shorthand (`{symbol: {type, required}}`) into a full object schema, so the
 * common path is a pass-through; the converter below only runs when the SDK is
 * absent and `defineTool` is the pass-through fallback.
 *
 * `additionalProperties: false` is not decoration. The SDK's own converter
 * (`parameterSchemaSpecToJsonSchema`) also omits it, and without it a client is
 * allowed to send `directedBy` / `sleeves` / `limt` and have them validated as
 * fine. Inside this process they are now refused by `unknownArguments`, but a
 * host that validates the schema first should never get that far — and the
 * schema is the only part of the contract a client can read.
 */
export function toJsonSchema(parameters = {}) {
  if (parameters && parameters.type === 'object' && parameters.properties) {
    return { ...parameters, additionalProperties: false }
  }
  const properties = {}
  const required = []
  for (const [key, spec] of Object.entries(parameters)) {
    const { required: isRequired, ...rest } = spec || {}
    properties[key] = rest
    if (isRequired) required.push(key)
  }
  const schema = { type: 'object', properties, additionalProperties: false }
  if (required.length) schema.required = required
  return schema
}

export function listTools(tools) {
  return tools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    inputSchema: toJsonSchema(tool.parameters),
  }))
}

const asText = (value, isError = false) => ({
  content: [{ type: 'text', text: typeof value === 'string' ? value : String(value) }],
  ...(isError ? { isError: true } : {}),
})

/**
 * Render a formatted body as a normal result, even when it did not fit.
 *
 * ``formatJsonOutput`` returns ``truncated: true`` only when the whole shrink
 * ladder failed — the payload is 200 keys of 200 chars and no level gets it
 * under the ceiling. What comes back is an envelope that says so and carries a
 * ``next`` ("retry with a smaller limit"). Marking that as ``isError`` was the
 * two-host disagreement: DSH rendered it as data, this host as a failure, and a
 * failure is the one signal that makes a client repeat the same oversized call.
 * MCP has no third state for "succeeded, here is what to do instead", so the
 * honest encoding is a successful result whose body is the instruction.
 */
const asRendered = (rendered) => asText(rendered.text)

/**
 * Carry notes the tool wrote onto the *planned request* over to the answer.
 *
 * ``paged()`` clamps a row count and records why on the object it returns
 * (``capped``). On this host that object is the ``{__request}`` placeholder, so
 * the note used to die with it: ``quant_auction(limit=200)`` returned 40 rows
 * with nothing saying which 40, while the same call under DSH carried
 * ``capped``. The request is the transport; these keys are the answer's
 * metadata, and they merge only where the response does not already say it.
 */
export function mergePlannedExtras(value, extras) {
  if (!extras || !Object.keys(extras).length) return value
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value
  const out = { ...value }
  for (const [key, note] of Object.entries(extras)) {
    if (!(key in out)) out[key] = note
  }
  return out
}

/** A client that records the request instead of performing it. */
const recordingClient = () => ({
  request: async (requestPath, options = {}) => ({ __request: { path: requestPath, options } }),
})

export function buildServer({ client, maxOutput = DEFAULT_MAX_OUTPUT, tools, legacyTools } = {}) {
  const outputLimit = integerOption(maxOutput, DEFAULT_MAX_OUTPUT, {
    name: 'maxOutput', min: 128, max: 1000000,
  })
  const registry = tools || buildTools(recordingClient())
  // Compatibility aliases are callable but never advertised. Custom test
  // registries stay isolated unless they explicitly pass legacyTools.
  const legacyRegistry = legacyTools || (!tools ? buildInternalTools(recordingClient()) : [])
  const index = new Map([...legacyRegistry, ...registry].map((tool) => [tool.name, tool]))
  const real = client || new QuantClient({})

  const server = new Server(
    { name: PLUGIN_NAME, version: '0.1.0' },
    { capabilities: { tools: {} }, instructions: PROMPT },
  )

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: listTools(registry) }))

  const context = { index, real, outputLimit }
  server.setRequestHandler(CallToolRequestSchema, (request) => handleToolCall(request, context))

  return server
}

/**
 * The CallTool body. Split out of the SDK registration because everything that
 * can be wrong with the MCP path lives here — the unknown-tool case, the guard,
 * and which of the two error envelopes a failure gets — and none of it is
 * reachable through `server.setRequestHandler` without a live transport.
 *
 * Three outcomes, in the order they are decided:
 *   1. bad name / guard → the guard's own sentence, `isError`;
 *   2. the tool ran locally (it answered without a request) → render it;
 *   3. the tool planned a request → perform it, then render.
 */
export async function handleToolCall(request, { index, real, outputLimit }) {
  const { name, arguments: args = {} } = request.params || {}
  const tool = index.get(name)
  if (!tool) return asText(`unknown tool: ${name}`, true)

  const denied = guard({ name, arguments: args })
  if (denied) return asText(denied, true)

  let planned
  try {
    planned = await tool.execute(args)
  } catch (error) {
    return asText(errorText(error), true)
  }

  // The ladder is chosen per tool, from the shape of its answer. Defaulting
  // to `text` here made every tabular tool cut rows before prose on this
  // host, while the same call under DSH cut prose and kept the rows: calling
  // `quant_pools(limit=20)` answered 12 rows here and 20 there, and nothing
  // in the payload said which 12 had been kept.
  const outputKind = typeof tool.outputKindFor === 'function'
    ? tool.outputKindFor(args)
    : tool.outputKind
  const ladder = shrinkLadder(outputKind)

  if (!planned || !planned.__request) {
    const rendered = formatJsonOutput(planned === undefined ? {} : planned, outputLimit, ladder)
    return asRendered(rendered)
  }

  const extras = Object.fromEntries(
    Object.entries(planned).filter(([key]) => key !== '__request'))

  try {
    const data = await real.request(planned.__request.path, planned.__request.options || {})
    // Response-level post-processing the tool declared. It could not run inside
    // `run` on this host: the value `run` saw was the `{__request}` placeholder,
    // not the answer. Applying the same hook here keeps one source of truth —
    // without it `quant_bars(days=250)` dropped its `window_capped` note on this
    // host only (measured 2026-11-08).
    let value = data
    if (typeof tool.afterFor === 'function') {
      const responseHandler = tool.afterFor(args)
      if (responseHandler && typeof responseHandler.after === 'function') {
        value = responseHandler.after(data, responseHandler.args)
      }
    } else if (typeof tool.after === 'function') {
      value = tool.after(data, args)
    }
    const rendered = formatJsonOutput(mergePlannedExtras(value, extras), outputLimit, ladder)
    return asRendered(rendered)
  } catch (error) {
    return asText(errorText(error), true)
  }
}

/* node:coverage disable */
/**
 * 进程入口。这里只有接线，没有判断：会出错的判断都在 `buildServer` /
 * `handleToolCall` 里，而它们有进程内用例。
 *
 * 豁免的理由不是"没测"，是**测在另一个进程里**：`mcp/test/server.test.mjs`
 * spawn 这个文件、真的走一遍 stdio JSON-RPC，但 v8 的覆盖率不跨进程收集。
 * 写成未覆盖比假装覆盖过诚实——前提是这段别长出逻辑来。
 */
export async function main() {
  const toolTablePath = fileURLToPath(new URL('../lib/index.js', import.meta.url))
  let toolTableStamp = 'missing'
  try {
    toolTableStamp = fs.statSync(toolTablePath).mtime.toISOString()
  } catch {
    // A missing table would have failed the import above; this is defensive.
  }
  const client = new QuantClient({
    baseUrl: process.env.QUANT_BASE_URL || DEFAULT_BASE_URL,
    token: process.env.QUANT_TOKEN || DEFAULT_TOKEN,
    timeoutMs: process.env.QUANT_TIMEOUT_MS || DEFAULT_TIMEOUT_MS,
    maxResponseBytes: process.env.QUANT_MAX_RESPONSE_BYTES || 1024 * 1024,
    allowRemote: process.env.QUANT_ALLOW_REMOTE === '1',
  })
  const tools = buildTools(recordingClient())
  const legacyTools = buildInternalTools(recordingClient())
  const server = buildServer({
    client,
    tools,
    legacyTools,
    maxOutput: Number(process.env.QUANT_MAX_OUTPUT || DEFAULT_MAX_OUTPUT),
  })
  const transport = new StdioServerTransport()
  await server.connect(transport)
  // stderr only: stdout carries JSON-RPC frames.
  console.error(`[${PLUGIN_NAME}-mcp] ${tools.length} tools -> ${client.baseUrl}`)
  // The tool table is read once at import and then lives for the life of this
  // process. Editing `lib/index.js` — descriptions, schemas, the shrink ladder,
  // the timeout — changes nothing here until a restart, and `/health` cannot see
  // it (that fingerprints the Python service). Printing the stamp on startup
  // makes "which tool table am I talking to" answerable at all.
  console.error(`[${PLUGIN_NAME}-mcp] tool table loaded from ${toolTablePath} ` +
    `(mtime ${toolTableStamp})`)

  const shutdown = async () => {
    try {
      await server.close()
    } finally {
      process.exit(0)
    }
  }
  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
  process.on('uncaughtException', (error) => {
    console.error(`[${PLUGIN_NAME}-mcp] uncaught: ${error?.stack || error}`)
    process.exit(1)
  })
  process.on('unhandledRejection', (error) => {
    console.error(`[${PLUGIN_NAME}-mcp] unhandled: ${error?.stack || error}`)
  })
}

const entry = process.argv[1] ? path.resolve(process.argv[1]).toLowerCase() : ''
if (entry && entry === fileURLToPath(import.meta.url).toLowerCase()) {
  await main()
}
/* node:coverage enable */
