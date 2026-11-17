import { buildTools } from '../lib/index.js'

const client = { request: async () => ({}) }
const query = buildTools(client).find((tool) => tool.name === 'quant_query')
let compactPromise
const details = new Map()

async function compact() {
  compactPromise ||= query.execute({ resource: 'catalog', params: {} }, {})
  return compactPromise
}

async function operationRow(tool, operation) {
  if (tool === 'quant_query' && operation === 'catalog') {
    return { paramsSchema: { properties: { operation: {}, full: {} } } }
  }
  const key = `${tool}.${operation}`
  if (details.has(key)) return details.get(key)
  const rows = (await compact()).operations
  if (!rows.some((row) => row.tool === tool && row.operation === operation)) {
    details.set(key, null)
    return null
  }
  const full = await query.execute({ resource: 'catalog', params: { operation, full: true } }, {})
  const row = full.operations.find((candidate) => candidate.tool === tool) || null
  details.set(key, row)
  return row
}

function matching(text, start, open, close) {
  let depth = 1
  let quote = null
  let escaped = false
  for (let index = start + 1; index < text.length; index += 1) {
    const char = text[index]
    if (quote) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === quote) quote = null
      continue
    }
    if (char === "'" || char === '"' || char === '`') quote = char
    else if (char === open) depth += 1
    else if (char === close) {
      depth -= 1
      if (depth === 0) return index
    }
  }
  return -1
}

function splitTopLevel(text) {
  const parts = []
  let start = 0
  let round = 0
  let square = 0
  let curly = 0
  let quote = null
  let escaped = false
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (quote) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === quote) quote = null
      continue
    }
    if (char === "'" || char === '"' || char === '`') quote = char
    else if (char === '(') round += 1
    else if (char === ')') round -= 1
    else if (char === '[') square += 1
    else if (char === ']') square -= 1
    else if (char === '{') curly += 1
    else if (char === '}') curly -= 1
    else if (char === ',' && round === 0 && square === 0 && curly === 0) {
      parts.push(text.slice(start, index))
      start = index + 1
    }
  }
  parts.push(text.slice(start))
  return parts
}

function paramsBlock(call) {
  const marker = /params\s*=\s*\{/g.exec(call)
  if (!marker) return null
  const open = marker.index + marker[0].length - 1
  const close = matching(call, open, '{', '}')
  return close < 0 ? null : call.slice(open + 1, close)
}

function selector(call, name) {
  return new RegExp(`${name}\\s*=\\s*["']([^"']+)["']`).exec(call)?.[1] || null
}

function entries(block) {
  if (!block || !block.trim()) return []
  const result = []
  for (const raw of splitTopLevel(block)) {
    const part = raw.trim()
    if (!part || part.startsWith('...')) continue
    const match = /^([\p{L}_$][\p{L}\p{N}_$]*)\s*(?:=|:|$)([\s\S]*)$/u.exec(part)
    if (!match) return [{ error: `cannot parse params segment: ${part}` }]
    result.push({ key: match[1], value: match[2].trim() })
  }
  return result
}

/** Extract facade examples and validate operation names plus literal params keys. */
export async function validateFacadeExamples(text, source) {
  const errors = []
  const calls = []
  const pattern = /quant_(query|order|workflow|alert)\s*\(/g
  for (const match of text.matchAll(pattern)) {
    const open = match.index + match[0].length - 1
    const close = matching(text, open, '(', ')')
    if (close < 0) {
      errors.push(`${source}: unclosed ${match[0]}`)
      continue
    }
    calls.push({ tool: `quant_${match[1]}`, body: text.slice(open + 1, close) })
  }
  for (const { tool, body } of calls) {
    let operation
    if (tool === 'quant_query') operation = selector(body, 'resource')
    else if (tool === 'quant_workflow') operation = selector(body, 'operation')
    else if (tool === 'quant_alert') operation = selector(body, 'action')
    else {
      const book = selector(body, 'book')
      const action = selector(body, 'action')
      operation = book && action ? `${book}.${action}` : null
    }
    if (!operation) {
      errors.push(`${source}: ${tool} example lacks its operation selector`)
      continue
    }
    const row = await operationRow(tool, operation)
    if (!row) {
      errors.push(`${source}: unknown operation ${tool}.${operation}`)
      continue
    }
    const block = paramsBlock(body)
    const properties = row.paramsSchema?.properties || {}
    for (const entry of entries(block)) {
      if (entry.error) {
        errors.push(`${source}: ${tool}.${operation}: ${entry.error}`)
        continue
      }
      const spec = properties[entry.key]
      if (!spec) {
        errors.push(`${source}: ${tool}.${operation}: undeclared params.${entry.key}`)
        continue
      }
      if (spec.enum && /^['"]?[A-Za-z_]+['"]?$/.test(entry.value)) {
        const value = entry.value.replace(/^['"]|['"]$/g, '')
        if (!spec.enum.includes(value)) {
          errors.push(`${source}: ${tool}.${operation}: params.${entry.key}=${value} not in ${spec.enum.join('/')}`)
        }
      }
    }
  }
  return { count: calls.length, errors }
}
