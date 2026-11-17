#!/usr/bin/env node
/**
 * 新鲜性与数据质量：不重算，只读已有的测量。
 *
 * 台子里已经有 `/data/brief`（每个数据集的覆盖终点与滞后交易日）和
 * `/data/quality`（2,700+ 个分区检查：ok / warn / quarantine），再造一份平行实现
 * 只会多一处会分叉的真相。这份脚本做的是**把它们按"模型能不能判断自己看到的东西有多旧"**
 * 重新组织：
 *
 *   1. 数据集层：每个数据集落后几个交易日、覆盖到哪天、多少个分区报警
 *   2. 工具层：每个响应自述的时间锚是什么、离最新交易日多久
 *   3. 无锚工具：哪些响应根本不告诉读者自己有多旧
 *
 * 用法: node freshness_audit.mjs [--json]
 */

import fs from 'node:fs'

import { CASES, RECORD_CASES, tagOf } from './audit_cases.mjs'

const TOKEN = (() => {
  const line = fs.readFileSync('../.env', 'utf8').split(/\r?\n/)
    .find((l) => l.startsWith('QUANT_TOKEN='))
  return line ? line.split('=')[1].trim() : ''
})()
const BASE = 'http://127.0.0.1:8811'

const { QuantClient, buildTools } = await import('./lib/index.js')
const tools = new Map(buildTools(new QuantClient({ token: TOKEN })).map((t) => [t.name, t]))

async function get(path, query) {
  const url = new URL(BASE + path)
  for (const [k, v] of Object.entries(query || {})) url.searchParams.set(k, String(v))
  const res = await fetch(url, { headers: { 'X-Quant-Token': TOKEN } })
  return res.json()
}

const brief = await get('/data/brief')
const quality = await get('/data/quality')
const lastTrading = String(brief.last_trading_day || '')

// ---------------------------------------------------------------- datasets
const datasets = []
for (const sleeve of Object.values(brief).filter((v) => v && v.datasets)) {
  for (const [name, meta] of Object.entries(sleeve.datasets)) {
    datasets.push({
      dataset: name,
      cover_end: String(meta.cover_end || ''),
      lag: meta.lag_trading_days,
      rows: meta.rows,
      warn: meta.warn_partitions,
      quarantine: meta.quarantined_partitions,
      errors: meta.error_issues,
    })
  }
}
datasets.sort((a, b) => (b.lag ?? 0) - (a.lag ?? 0) || String(a.dataset).localeCompare(String(b.dataset)))

const issues = {}
const worstRatio = {}
for (const item of quality.items || []) {
  for (const issue of item.issues || []) {
    issues[issue.code] = (issues[issue.code] || 0) + (issue.count || 1)
    // 同一个 code 在 300 万行的分区里出现 1 次和在 500 行的分区里出现 1 次
    // 不是一回事，所以要记住最坏的分区占比，而不是只数次数。
    worstRatio[issue.code] = Math.max(worstRatio[issue.code] || 0, issue.ratio || 0)
  }
}

// ---------------------------------------------------------------- anchors
const ANCHORS = ['as_of', 'trade_date', 'date', 'day', 'snapshot_day', 'last_bar', 'updated_at']
const tools_ = []
for (const caze of CASES) {
  const tool = tools.get(caze[0])
  if (!tool) continue
  let body
  try {
    body = await tool.execute(caze[1], {})
  } catch {
    continue
  }
  if (typeof body === 'string') {
    try { body = JSON.parse(body) } catch { /* keep string */ }
  }
  if (!body || typeof body !== 'object') continue
  let anchorKey = ANCHORS.find((k) => body[k] !== undefined)
  let anchor = anchorKey ? String(body[anchorKey]) : null
  // 历史记录列表：顶层没有锚，逐行有。取第一行的时间说明"这份清单有多新"。
  if (!anchor) {
    for (const value of Object.values(body)) {
      if (!Array.isArray(value) || !value.length || typeof value[0] !== 'object') continue
      const key = ['ts', 'created_at', 'pub_date', 'closed_at', 'opened_at',
        'updated_at', 'date'].find((k) => value[0][k] !== undefined)
      if (key) { anchorKey = `rows.${key}`; anchor = String(value[0][key]); break }
    }
  }
  const quoteAge = body.age_seconds ?? body.delay_seconds ?? null
  // A document identifies itself with an id; its date is the subject of the
  // answer, not a measure of how stale the answer is.
  const document = body.announcement_id !== undefined
  // 判断记录：年龄是"什么时候记下的"，`generated_at` 才是它的锚。只认清单里
  // 显式标为 record 的调用——否则任何行情快照盖个时间戳就能绕过这条判定。
  if (!anchor && RECORD_CASES.has(tagOf(caze)) && body.generated_at) {
    anchorKey = 'generated_at'
    anchor = String(body.generated_at)
  }
  tools_.push({ tag: tagOf(caze), anchorKey, anchor, quoteAge, document })
}

function behindLatest(anchor) {
  if (!anchor) return null
  const text = String(anchor)
  // 带时刻的是实时戳（今天几点算的），不是"数据日期"。拿它跟最新交易日比
  // 只会得出"周日的数据落后两天"这种假结论。
  if (text.includes('T') || text.includes(':')) return 0
  const digits = text.replace(/[^0-9]/g, '').slice(0, 8)
  if (digits.length < 8) return null
  // 比最新交易日**新**不是落后：周日跑出来的 as_of 是 20261101，而最新交易日
  // 是 20261030。按"不等于即落后"判，会把"今天现算的"报成"旧数据"。
  if (digits >= lastTrading) return 0
  return 1
}

// "时点型"和"清单型"要分开看：一份历史清单的最新一条是三天前，说明的是
// "这三天没有新记录"，不是"数据旧了"。混在一起报会把正常状态说成缺陷。
const stale = []
const records = []
const noAnchor = []
// 文档类回答的日期是它的**主题**，不是它的新鲜度：一份 10-24 的公告，10-30 读它
// 依然是最新的一版。拿它和最新交易日比只会得出"这份公告落后六天"这种假结论。
const documents = []
for (const row of tools_) {
  if (row.document) { documents.push(row); continue }
  if (!row.anchor && row.quoteAge === null) { noAnchor.push(row); continue }
  const behind = behindLatest(row.anchor)
  if (!behind) continue
  if (String(row.anchorKey).startsWith('rows.')) records.push({ ...row, behind })
  else stale.push({ ...row, behind })
}

if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ datasets, issues, tools: tools_, stale, noAnchor }, null, 1))
} else {
  console.log('== 数据集：覆盖终点与滞后 ==')
  for (const d of datasets) {
    console.log(`${String(d.dataset).padEnd(22)} 覆盖到 ${d.cover_end || '-'}  滞后 ${d.lag ?? '-'} 交易日`
      + `  行数 ${d.rows ?? '-'}  warn ${d.warn ?? 0} 隔离 ${d.quarantine ?? 0}`)
  }
  console.log(`\n== 分区检查 ==  ok ${quality.counts?.ok} / warn ${quality.counts?.warn}`
    + ` / quarantine ${quality.counts?.quarantine}`)
  const top = Object.entries(issues).sort((a, b) => b[1] - a[1]).slice(0, 6)
  console.log('  主要问题（次数 / 最坏分区占比）：',
    top.map(([k, v]) => `${k}×${v}(${(worstRatio[k] * 100).toFixed(3)}%)`).join('  '))

  console.log(`\n== 工具自述的年龄 ==（最新交易日 ${lastTrading}）`)
  const withAnchor = tools_.length - noAnchor.length
  console.log(`  有锚 ${withAnchor} / 共 ${tools_.length}；时点型落后于最新交易日的 ${stale.length} 个`)
  for (const row of stale) {
    console.log(`  ${row.tag.padEnd(40)} ${row.anchorKey}=${row.anchor}`)
  }
  if (records.length) {
    console.log('  清单型（最新一条记录的年龄，不是数据旧了）：')
    for (const row of records) console.log(`  ${row.tag.padEnd(40)} ${row.anchorKey}=${row.anchor}`)
  }
  if (noAnchor.length) {
    console.log('  完全不告诉读者自己有多旧：')
    for (const row of noAnchor) console.log(`  ${row.tag}`)
  }
}
