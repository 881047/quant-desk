#!/usr/bin/env node
/**
 * 模型调用工具时，到底拿到多少。
 *
 * 走的是真实链路：buildTools() 里的 execute（含 guard 与 clamp）→ output.render()
 * 里那个 formatJsonOutput（16,000 字节上限 + 三级 shrink）。不自己拼 URL，
 * 因为要测的正是"工具替模型做的那些决定"。
 *
 * 三个状态：
 *   intact   原样送达
 *   degraded 被 shrink 过（附第几级、砍了几行/几串）——信息还在，但少了
 *   dropped  超上限，模型只收到一个 error 信封
 *
 * 用法: node delivery_audit.mjs [--json]
 */

import fs from 'node:fs'

import { CASES, tagOf } from './audit_cases.mjs'

const TOKEN = (() => {
  const line = fs.readFileSync('../.env', 'utf8').split(/\r?\n/)
    .find((l) => l.startsWith('QUANT_TOKEN='))
  return line ? line.split('=')[1].trim() : ''
})()

const { QuantClient, buildTools } = await import('./lib/index.js')
const client = new QuantClient({ token: TOKEN })
const tools = new Map(buildTools(client).map((tool) => [tool.name, tool]))

const rows = []
for (const caze of CASES) {
  const [name, args] = caze
  const tool = tools.get(name)
  const tag = tagOf(caze)
  if (!tool) {
    rows.push({ tag, state: 'missing-tool', bytes: 0, note: '' })
    continue
  }
  let value
  try {
    value = await tool.execute(args, {})
  } catch (error) {
    rows.push({ tag, state: 'threw', bytes: 0, note: String(error.message || error).slice(0, 60) })
    continue
  }
  const rendered = tool.output.render(args, value)[0].text
  const bytes = Buffer.byteLength(rendered, 'utf8')
  let state = 'intact'
  let note = ''
  if (bytes > 16000) state = 'dropped'
  try {
    const envelope = JSON.parse(rendered)
    if (envelope && envelope._shrink) {
      state = 'degraded'
      note = `L${envelope._shrink.level} 砍 ${envelope._shrink.rows_dropped} 行 / ${envelope._shrink.strings_shortened} 串`
    }
    if (envelope && envelope.truncated) state = 'dropped'
    if (value && value.capped) note = `capped(limit ${value.capped.asked_limit}→${value.capped.served_limit})`
    if (value && value.window_capped) note = `capped(days ${value.window_capped.asked_for}→${value.window_capped.served})`
  } catch { /* not JSON: leave intact */ }
  rows.push({ tag, state, bytes, note })
}

const order = { dropped: 0, degraded: 1, threw: 2, 'missing-tool': 3, intact: 4 }
rows.sort((a, b) => order[a.state] - order[b.state] || b.bytes - a.bytes)

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(rows, null, 1))
} else {
  const bad = rows.filter((row) => row.state !== 'intact')
  for (const row of bad) {
    console.log(`${row.state.padEnd(9)} ${row.tag.padEnd(34)} ${String(row.bytes).padStart(7)}  ${row.note}`)
  }
  console.log(`\n${rows.length} 次调用：intact ${rows.length - bad.length}，需处理 ${bad.length}`)
  const biggest = rows.filter((row) => row.state === 'intact')
    .sort((a, b) => b.bytes - a.bytes).slice(0, 5)
  console.log('最接近上限但完好的：')
  for (const row of biggest) console.log(`  ${row.tag.padEnd(34)} ${String(row.bytes).padStart(7)}`)
}
