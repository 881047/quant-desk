/**
 * How much context the tool surface itself costs, before any tool is called.
 *
 * Fifty tool definitions and one prompt section are sent with every request.
 * That is a fixed tax on the window, it never shows up in a "how many bytes did
 * the tool return" measurement, and it is the first thing to look at when the
 * desk feels like it has no room left to think.
 *
 *   node plugin/tools_size.mjs
 *   node plugin/tools_size.mjs --json
 */

const here = new URL('./lib/index.js', import.meta.url)

function estimateTokens(text) {
  // One token per CJK character, one per four ASCII characters. Crude, and
  // deliberately pessimistic: a budget that flatters itself is not a budget.
  let cjk = 0
  for (const ch of text) {
    const code = ch.codePointAt(0)
    if (code >= 0x4e00 && code <= 0x9fff) cjk += 1
  }
  return cjk + Math.floor((text.length - cjk) / 4)
}

const mod = await import(here)
const tools = mod.buildTools({ request: async () => ({}) })

const rows = tools.map((tool) => {
  const schema = JSON.stringify({
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters ?? tool.inputSchema ?? {},
  })
  return {
    name: tool.name,
    bytes: Buffer.byteLength(schema, 'utf8'),
    descriptionBytes: Buffer.byteLength(String(tool.description ?? ''), 'utf8'),
    tokens: estimateTokens(schema),
  }
})

const schemaBytes = rows.reduce((sum, row) => sum + row.bytes, 0)
const schemaTokens = rows.reduce((sum, row) => sum + row.tokens, 0)
const prompt = String(mod.PROMPT ?? '')
const promptBytes = Buffer.byteLength(prompt, 'utf8')
const promptTokens = estimateTokens(prompt)

const payload = {
  tools: rows.length,
  schema_bytes: schemaBytes,
  schema_tokens: schemaTokens,
  prompt_bytes: promptBytes,
  prompt_tokens: promptTokens,
  fixed_tokens: schemaTokens + promptTokens,
  heaviest: rows.sort((a, b) => b.bytes - a.bytes).slice(0, 5),
}

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(payload, null, 1))
} else {
  console.log(`工具 ${payload.tools} 个：定义 ${schemaBytes.toLocaleString()} B ≈ ${schemaTokens.toLocaleString()} token`)
  console.log(`提示词：${promptBytes.toLocaleString()} B ≈ ${promptTokens.toLocaleString()} token`)
  console.log(`固定开销合计 ≈ ${payload.fixed_tokens.toLocaleString()} token（每次请求都要付）`)
  console.log('最重的定义：')
  for (const row of payload.heaviest) {
    console.log(`  ${row.name.padEnd(22)}${String(row.bytes).padStart(6)} B  (描述 ${row.descriptionBytes} B)`)
  }
}
