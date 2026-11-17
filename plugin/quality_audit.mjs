#!/usr/bin/env node
/**
 * 信息质量的可测部分：不靠形容词，按六条规则扫每个工具的**真实响应**。
 *
 * 六条都是从这半年踩过的坑里长出来的，每条都对应一次真实的误读：
 *
 *   A 时间锚     没有 as_of，读者不知道这句话什么时候为真
 *   B 占位符     四个空字符串组成的 basis、空对象——看着有字段，实际没信息
 *   C 精度       9.983452796936035 这种 float32 尾巴（P67）
 *   D 失败信封   ok:false 却不说下一步，与"工具坏了"同形（P69）
 *   E 统计无误差 有 mean/win_rate/ci95 却没有 caveats/provenance/n
 *   F 空而无说明 所有列表为空，却不解释是"没有"还是"不知道"
 *
 * 用法: node quality_audit.mjs [--json]
 */

import fs from 'node:fs'
import { CASES, tagOf } from './audit_cases.mjs'

const TOKEN = (() => {
  const line = fs.readFileSync('../.env', 'utf8').split(/\r?\n/)
    .find((l) => l.startsWith('QUANT_TOKEN='))
  return line ? line.split('=')[1].trim() : ''
})()

const { QuantClient, buildTools } = await import('./lib/index.js')
const tools = new Map(buildTools(new QuantClient({ token: TOKEN })).map((t) => [t.name, t]))

const TIME_KEYS = ['as_of', 'date', 'trade_date', 'day', 'generated_at', 'today', 'snapshot_day', 'updated_at']
// 任何"这组数是怎么来的 / 有多可信"的键都算说明。放开这一条是因为只认 `caveats`
// 会把 profile 的 how_to_read、discover 的 forward_basis、regime 的 reasons 全判成违规，
// 而它们其实是同一件事的不同写法。
const EXCUSE_RE = /^(note|notes|reason|reasons|caveats|basis|why|error|reading|how_to_read|forward_basis|edge_definition|method|definition)/
// 只有**推断性**统计才必须自带误差：percentile / vol / skew 是描述，excess_ci95 / t_stat / p_value
// 是在下结论。把描述也要求误差，等于让每个数字都带一段免责声明。
// 词边界不是装饰：`t_stat` 不带边界会匹配 `limit_state` 里的 `t_stat`，
// 于是每一条盘口报价都被判成"统计无误差说明"（2026-11-02 抓到）。
const INFERENTIAL_RE = /(ci95|ci_(?:low|high|pct)|p_value|t_stat|excess|win_rate|sharpe)/
const DESCRIPTIVE_RE = /^(mean|median|vol|skew|kurtosis|autocorr|win)/

function walk(node, depth, visit) {
  if (depth > 3) return
  if (Array.isArray(node)) {
    for (const item of node.slice(0, 20)) walk(item, depth + 1, visit)
  } else if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node)) {
      visit(key, value, depth)
      walk(value, depth + 1, visit)
    }
  }
}

function decimals(value) {
  const text = String(value)
  if (!text.includes('.')) return 0
  return text.split('.')[1].replace(/e.*$/, '').length
}

function audit(tag, body) {
  const issues = []
  if (!body || typeof body !== 'object') return issues

  // A 时间锚：顶层一个，或者每条记录自带一个。历史记录列表本来就没有"顶层 as_of"，
  // 逐行的时间戳才是它的锚。
  // 失败信封不需要时间锚：它没有数字，"什么时候为真"这个问题没有意义。
  // 硬加一个 as_of 只会让每个错误都带一个假装精确的日期。
  const hasTime = body.ok === false
    || TIME_KEYS.some((key) => body[key] !== undefined)
  if (!hasTime) {
    // 只看**顶层**的数组：memo 的行有 created_at，但行里的 predictions 没有时间戳，
    // 递归下去会把"有锚的响应"判成没锚。
    const rowKeys = ['ts', 'created_at', 'pub_date', 'closed_at', 'opened_at',
      'updated_at', 'trade_date', 'date', 'as_of', 'day']
    let stamped = 0
    let total = 0
    for (const value of Object.values(body)) {
      if (!Array.isArray(value) || !value.length || typeof value[0] !== 'object') continue
      total += value.length
      stamped += value.filter((item) => item && rowKeys.some((k) => item[k] !== undefined)).length
    }
    if (!total || stamped < total * 0.8) issues.push('A 缺时间锚')
  }

  // B 占位符 + C 精度
  let placeholders = 0
  let fine = 0
  walk(body, 0, (key, value, depth) => {
    // 空的 {} 不算占位符：没有持仓时 by_industry 本来就是空映射，旁边 invested=0
    // 已经解释了它。真正的占位符是"有四个键、四个都空"的那种块。
    if (value && typeof value === 'object' && !Array.isArray(value) && depth > 0) {
      const keys = Object.keys(value)
      const vals = Object.values(value)
      const emptyish = (v) => v === '' || v === null
        || (Array.isArray(v) && v.length === 0)
        || (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0)
      if (keys.length >= 3 && vals.every(emptyish)) placeholders += 1
    }
    if (typeof value === 'number' && !Number.isInteger(value) && decimals(value) > 6) fine += 1
  })
  if (placeholders) issues.push(`B 占位符 ×${placeholders}`)
  if (fine) issues.push(`C 高精度 ×${fine}`)

  // D 失败信封：要么有结构化的下一步，要么 error 文字本身说了怎么办
  // （"跑 scripts/xxx.py 积累" 就是下一步，不能因为不是字段就判违规）。
  if (body.ok === false) {
    const text = String(body.error || body.reason || '')
    const structured = ['need', 'next', 'example', 'alternative', 'topics', 'market_topics']
      .some((key) => body[key] !== undefined)
    const prose = text.length >= 20 && /(跑|用|看|换|等|scripts|quant_|先|稍)/.test(text)
    if (!text) issues.push('D 失败无原因')
    else if (!structured && !prose) issues.push('D 失败无下一步')
  }

  // E 推断性统计必须带误差说明
  let inferential = 0
  let descriptive = 0
  walk(body, 0, (key) => {
    if (INFERENTIAL_RE.test(key)) inferential += 1
    else if (DESCRIPTIVE_RE.test(key)) descriptive += 1
  })
  if (inferential >= 2 && descriptive >= 0) {
    // 说明可以嵌一层：strategies 的 reasons 在 weights[key] 里，dashboard 的在子块里。
    let excused = false
    walk(body, 0, (key, value, depth) => {
      if (depth <= 2 && EXCUSE_RE.test(key) && value !== '' && value !== null) excused = true
    })
    if (['n', 'n_days', 'provenance'].some((key) => body[key] !== undefined)) excused = true
    if (!excused) issues.push('E 统计无误差说明')
  }

  // F 全空且无说明：只有"除了锚点之外一个值都没有"才算
  const meaningful = Object.entries(body).filter(([key, value]) => {
    if (['ok', 'as_of', 'date', 'day', 'trade_date', 'today'].includes(key)) return false
    if (Array.isArray(value)) return value.length > 0
    if (value && typeof value === 'object') return Object.keys(value).length > 0
    return value !== null && value !== undefined
  })
  if (meaningful.length === 0) {
    const excused = Object.keys(body).some((key) => EXCUSE_RE.test(key))
    if (!excused) issues.push('F 全空且无说明')
  }
  return issues
}

const rows = []
for (const caze of CASES) {
  const tag = tagOf(caze)
  const tool = tools.get(caze[0])
  if (!tool) {
    rows.push({ tag, issues: ['缺工具'] })
    continue
  }
  let body
  try {
    body = await tool.execute(caze[1], {})
    if (typeof body === 'string') body = JSON.parse(body)
  } catch (error) {
    rows.push({ tag, issues: [`调用失败: ${String(error.message || error).slice(0, 50)}`] })
    continue
  }
  rows.push({ tag, issues: audit(tag, body) })
}

const clean = rows.filter((row) => row.issues.length === 0)
const dirty = rows.filter((row) => row.issues.length > 0)

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(rows, null, 1))
} else {
  for (const row of dirty) console.log(`${row.tag.padEnd(46)} ${row.issues.join(' / ')}`)
  const tally = {}
  for (const row of rows) {
    for (const issue of row.issues) {
      const kind = issue.slice(0, 1)
      tally[kind] = (tally[kind] || 0) + 1
    }
  }
  console.log(`\n${rows.length} 次调用：干净 ${clean.length}，有问题 ${dirty.length}`)
  console.log('按规则：', Object.entries(tally).map(([k, v]) => `${k}=${v}`).join('  '))
}
