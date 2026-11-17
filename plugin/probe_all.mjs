/**
 * 全工具缺陷探查：盘中跑，按"这个回答能不能用"判，而不是按"有没有报错"判。
 *
 *   node probe_all.mjs            # 表格
 *   node probe_all.mjs --json     # 完整载荷，供逐条看
 *   node probe_all.mjs --only quote,pools
 *
 * 和四个审计的分工：那四个问的是"字节/表述/年龄/一致"，是**横切**的；这一份问的是
 * "每个工具在盘中这一分钟给出的答案，有没有洞"，是**逐个**的。一次真正的交易
 * （2026-11-02）暴露的十三条缺陷里，没有一条会让工具报错——它们都会返回 200 和一个
 * 看起来完整的对象。所以判据必须是内容：
 *
 *   THREW   调用抛了
 *   ERROR   payload 里 ok=false 或带 error
 *   EMPTY   该有数据的地方是空的（0 行 / 0 个键）
 *   STALE   自己承认旧了：stale=true / age_seconds>600 / as_of 落后于最新交易日
 *   NULLS   第一层就有 null（数出来，人看）
 *   HOLE    关键字段缺失（按 REQUIRED 表）
 *   SLOW    > 2500 ms
 *   BIG     > 16,384 B
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { CASES, tagOf } from './audit_cases.mjs'
import { QuantClient, buildTools } from './lib/index.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')

function token() {
  try {
    const env = fs.readFileSync(path.join(ROOT, '.env'), 'utf8')
    const hit = env.match(/^\s*QUANT_TOKEN\s*=\s*(.+)\s*$/m)
    if (hit) return hit[1].trim()
  } catch { /* fall through */ }
  return process.env.QUANT_TOKEN
}

//: 关键字段：缺了就等于这个工具没回答问题。只列真的会被引用的那几个。
const REQUIRED = {
  quant_quote: ['quotes'],
  quant_pools: ['counts', 'pool_state'],
  quant_bars: ['bars', 'as_of'],
  quant_facts: ['day'],
  quant_context: ['session', 'account'],
  quant_stops: ['stops'],
  quant_profile: ['symbol'],
  quant_mins: ['bars', 'count'],
  quant_journal_search: ['entries'],
  quant_screen: ['candidates'],
}

const LIMIT_MS = 2500
const LIMIT_BYTES = 16384

const EXTRA = [
  ['quant_mins', { symbol: '600519.SH' }],
  ['quant_mins', { symbol: '600519.SH', last_n: 12 }, 'last 12'],
  ['quant_stops', { account: 'external' }, 'external'],
  ['quant_quote', { symbols: '000001.SH,399001.SZ,600519.SH' }, 'index+stock'],
  ['quant_quote', { symbols: '999999.SZ' }, 'unknown symbol'],
]

//: 空值身边有说明就算"诚实的空"。这套东西的规矩是"空值必须自带原因"，
//: 检查它的工具也得认这句话——只数 null 会让每一处已解释的空值天天报警。
function explained(parent, key) {
  if (!parent || typeof parent !== 'object') return false
  for (const candidate of [`${key}_note`, `${key}_reason`, 'note', 'reason', 'why']) {
    const value = parent[candidate]
    if (typeof value === 'string' && value.trim()) return true
    if (value && typeof value === 'object') return true
  }
  return false
}

// `excused` 沿树往下传：一个块级 note（"这家公司没披露这些指标"）就能解释它下面
// 所有空值。按字段逐个写原因是噪音——一个银行没有毛利率不是二十条缺陷，是一条事实。
function walkNulls(value, depth = 0, out = { count: 0, keys: [] }, pathKey = '',
                   excused = false) {
  if (depth > 2) return out
  if (Array.isArray(value)) {
    for (const row of value.slice(0, 2)) {
      walkNulls(row, depth + 1, out, `${pathKey}[]`, excused)
    }
    return out
  }
  if (value && typeof value === 'object') {
    const blockExcused = excused || explainedBlock(value)
    for (const [key, row] of Object.entries(value)) {
      const at = pathKey ? `${pathKey}.${key}` : key
      if (row === null) {
        if (blockExcused || explained(value, key)) continue
        out.count += 1
        if (out.keys.length < 6) out.keys.push(at)
        continue
      }
      walkNulls(row, depth + 1, out, at, blockExcused)
    }
  }
  return out
}

//: 块级说明：`note` / `missing_metrics` / `basis` 中任一非空，就足以解释这一块的空值。
function explainedBlock(node) {
  // `checked` 就是"我读了多少期"：读满 20 期而没有记录，null 的含义是"确实没有"，
  // 不是"没查"。`n` / `n_days` 同理（样本量）。
  for (const key of ['note', 'missing_metrics', 'basis', 'why', 'checked',
                     'n', 'n_days', 'sessions_read']) {
    const value = node[key]
    if (typeof value === 'string' && value.trim()) return true
    if (Array.isArray(value) && value.length) return true
    if (typeof value === 'number') return true
    if (value && typeof value === 'object' && Object.keys(value).length) return true
  }
  return false
}

const client = new QuantClient({ baseUrl: process.env.QUANT_URL, token: token() })
const tools = new Map(buildTools(client).map((tool) => [tool.name, tool]))
const only = (process.argv.find((a) => a.startsWith('--only=')) || '').slice(7)
const wantJson = process.argv.includes('--json')

const cases = [...CASES, ...EXTRA].filter(([name]) =>
  !only || only.split(',').some((part) => name.includes(part)))

const rows = []
for (const item of cases) {
  const [name, args = {}] = item
  const tag = tagOf(item)
  const tool = tools.get(name)
  if (!tool) {
    rows.push({ tag, name, flags: ['HOLE'], note: '工具不存在' })
    continue
  }
  // 冷启动不算工具的成本：`quant_discover` 首调 16.9 s、二调 1.5 s，差别是
  // 进程/缓存有没有热，不是工具本身慢。所以每个工具先跑一次（丢弃结果和耗时），
  // 再跑第二次计时——报出来的才是"这个工具稳定时要多久"。
  let warmed = ''
  try {
    await tool.execute(args)
  } catch (error) {
    warmed = error instanceof Error ? error.message : String(error)
  }
  const started = Date.now()
  let raw
  let failure = ''
  try {
    raw = await tool.execute(args)
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error)
  }
  const ms = Date.now() - started
  if (warmed && !failure) failure = warmed
  if (failure) {
    rows.push({ tag, name, flags: ['THREW'], ms, note: failure.slice(0, 120) })
    continue
  }
  const text = JSON.stringify(raw) ?? 'null'
  const bytes = Buffer.byteLength(text, 'utf8')
  const body = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  const flags = []
  const notes = []

  if (body.ok === false || body.error) {
    flags.push('ERROR')
    notes.push(String(body.error || body.note || '').slice(0, 100))
  }
  const nulls = walkNulls(body)
  if (body.ok !== false && Object.keys(body).length === 0) {
    flags.push('EMPTY')
  }
  if (body.stale === true) flags.push('STALE')
  if (typeof body.age_seconds === 'number' && body.age_seconds > 600) {
    flags.push('STALE')
    notes.push(`age ${Math.round(body.age_seconds)}s`)
  }
  if (nulls.count) {
    flags.push('NULLS')
    notes.push(`${nulls.count} null: ${nulls.keys.join(',')}`)
  }
  for (const key of REQUIRED[name] || []) {
    if (!(key in body)) {
      flags.push('HOLE')
      notes.push(`缺 ${key}`)
    }
  }
  if (ms > LIMIT_MS) { flags.push('SLOW'); notes.push(`${ms} ms`) }
  if (bytes > LIMIT_BYTES) { flags.push('BIG'); notes.push(`${bytes} B`) }

  rows.push({ tag, name, flags, ms, bytes, note: notes.join(' · ') })
}

const ORDER = ['THREW', 'ERROR', 'EMPTY', 'HOLE', 'STALE', 'SLOW', 'BIG', 'NULLS']
const defective = rows.filter((row) => row.flags.length)
defective.sort((a, b) => ORDER.indexOf(a.flags[0]) - ORDER.indexOf(b.flags[0]))

console.log(`探查 ${rows.length} 次调用 · 有缺陷 ${defective.length}\n`)
for (const row of defective) {
  console.log(`${row.flags.join(',').padEnd(14)} ${row.tag.padEnd(42)} `
    + `${String(row.bytes ?? '').padStart(6)}B ${String(row.ms ?? '').padStart(5)}ms  ${row.note}`)
}
const clean = rows.length - defective.length
console.log(`\n干净 ${clean} / ${rows.length}`)
if (wantJson) {
  fs.writeFileSync(path.join(HERE, 'probe_all.json'), JSON.stringify(rows, null, 1))
  console.log('wrote plugin/probe_all.json')
}
process.exit(0)
