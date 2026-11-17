/**
 * 一次跑完四个审计，并把结果和上一次比。
 *
 *   node audit_all.mjs              # 跑四个，记录到 ../reports/audits/，有问题则退出码 1
 *   node audit_all.mjs --no-record  # 只跑不记
 *   node audit_all.mjs --detail     # 连每个工具的字节数也打出来
 *
 * 为什么要有这个：四个审计各说各的格式，跑一次要记四条命令；而它们存在的理由
 * 恰恰是"人会忘"。忘一次不要紧，忘三次就等于没有——P82 到 P86 五轮建的检查，
 * 只有真跑才算数。
 *
 * 比什么：两份东西都要。
 *  - **回归**：结果落盘，下次和上一次比。它答"这两天之间坏了什么"。
 *  - **绝对值**：`absolute()` 答"现在有没有坏着"。第一次跑没有上一次，只看回归
 *    等于第一次永远绿；而一个从没修好的问题两次都一样，回归也看不见它。
 *
 * 退出码 1 的三种来源：某条审计没跑成、比上次变坏、第一次跑就有绝对值问题。
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { absolute, compare, normalise } from './audit_diff.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const AUDITS = ['delivery', 'quality', 'freshness', 'consistency']
// 快照默认落在仓库里；`QUANT_AUDIT_DIR` 让"没有上一次"这条路径能被真的跑一次，
// 而不是靠读代码相信它。
const OUT_DIR = process.env.QUANT_AUDIT_DIR
  ? path.resolve(process.env.QUANT_AUDIT_DIR)
  : path.resolve(HERE, '..', 'reports', 'audits')

function run(name) {
  return new Promise((resolve) => {
    execFile(process.execPath, [`${name}_audit.mjs`, '--json'], {
      cwd: HERE, timeout: 600_000, maxBuffer: 64 * 1024 * 1024,
    }, (error, stdout) => {
      let body = null
      try {
        body = JSON.parse(stdout)
      } catch {
        body = null
      }
      // 退出码不能单独当判据：`consistency_audit` 的**正常结论**就是"有分歧 →
      // exit 1"，而它已经先把 JSON 打完了。所以"跑成没跑成"看有没有 JSON。
      // 但也不能完全不看：打印完 JSON 之后再崩（或进程被信号杀掉）也会留下
      // 一份看似完整的 body，而退出码是那时唯一还在的东西。
      const code = error ? (error.code ?? -1) : 0
      resolve({ name, body, code, failed: !body || code < 0 || code > 1,
                raw: body ? '' : String(stdout || error).slice(0, 400) })
    })
  })
}

async function previous() {
  try {
    const files = (await fs.readdir(OUT_DIR)).filter((f) => f.endsWith('.json')).sort()
    if (!files.length) return null
    return JSON.parse(await fs.readFile(path.join(OUT_DIR, files.at(-1)), 'utf8'))
  } catch {
    return null
  }
}

const results = await Promise.all(AUDITS.map(async (name) => {
  const raw = await run(name)
  return {
    name,
    raw,
    failed: raw.failed,
    body: raw.body ? normalise(name, raw.body) : { failed: true },
  }
}))

const when = new Date().toISOString()
const now = { when, ...Object.fromEntries(results.map((r) => [r.name, r.body])) }
const before = await previous()
const { worse, better, first } = compare(now, before)
// 绝对值这一半不能在 `first` 时缺位：第一次跑没有历史，`worse` 一定是空的。
const problems = absolute(now)

const line = (name, text) => console.log(`  ${name.padEnd(11)} ${text}`)
console.log(`审计（${when.slice(0, 16).replace('T', ' ')}）`)
for (const { name, raw, failed, body } of results) {
  if (failed) { line(name, `没跑成：${raw.raw || `退出码 ${raw.code}`}`); continue }
  if (name === 'delivery') line(name, `${body.ok}/${body.n} 原样送达`)
  if (name === 'quality') line(name, `${body.ok}/${body.n} 表述干净`)
  if (name === 'freshness') {
    line(name, `${body.anchored}/${body.tools} 自述年龄`
      + (body.unanchored.length ? `，无锚：${body.unanchored.join('、')}` : ''))
  }
  if (name === 'consistency') {
    line(name, `${body.ok}/${body.n - body.na.length} 一致`
      + (body.na.length ? `，${body.na.length} 项不可比` : ''))
  }
}

if (first) {
  console.log('\n第一次记录，没有上一次可比。')
  if (problems.length) {
    console.log(`当前就有 ${problems.length} 项不达标——这不是"和上次比没变坏"，是没有上次：`)
    for (const item of problems) console.log(`  ! ${item}`)
  } else {
    console.log('当前也没有绝对值上的问题，下一次开始能报回归。')
  }
} else if (!worse.length) {
  console.log(`\n与上一次（${String(before.when).slice(0, 16).replace('T', ' ')}）相比没有变坏`
    + (better.length ? `，${better.length} 项变好` : '') + '。')
} else {
  console.log(`\n比上一次（${String(before.when).slice(0, 16).replace('T', ' ')}）变坏的：`)
  for (const item of worse) console.log(`  ! ${item}`)
}
// 一直坏着的问题 `compare()` 看不见（两次都有，差值为零），但它比回归更该被看见。
if (!first && problems.length) {
  console.log(`\n当前不达标（上次也在，所以不在上面的回归里）：`)
  for (const item of problems) console.log(`  ! ${item}`)
}
for (const item of better.slice(0, 6)) console.log(`  + ${item}`)

if (process.argv.includes('--detail')) {
  console.log('\n最大的五个响应：')
  for (const [key, bytes] of Object.entries(now.delivery?.bytes || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 5)) {
    console.log(`  ${key.padEnd(36)} ${String(bytes).padStart(7)} B`)
  }
}

if (!process.argv.includes('--no-record')) {
  await fs.mkdir(OUT_DIR, { recursive: true })
  const stamp = when.replace(/[:.]/g, '-').slice(0, 19)
  await fs.writeFile(path.join(OUT_DIR, `${stamp}.json`), `${JSON.stringify(now, null, 1)}\n`)
}

process.exit(results.some((r) => r.failed) || worse.length || (first && problems.length) ? 1 : 0)
