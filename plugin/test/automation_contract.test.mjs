import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'

import { buildTools } from '../lib/index.js'
import { validateFacadeExamples } from './facade_docs_helpers.mjs'

// The eleven frozen prompts are the model's daily operating instructions. Their
// online copies and hashes are checked by freeze_automations.py; this file checks
// that every example uses one of the four public facades and a real catalog operation.
const DIR = new URL('../../docs/automations/', import.meta.url)
const SKILL = readFileSync(new URL('../skills/quant-desk/SKILL.md', import.meta.url), 'utf8')
const files = readdirSync(DIR).filter((f) => f.endsWith('.md')).sort()
const raw = new Map(files.map((f) => [f, readFileSync(new URL(f, DIR), 'utf8')]))
const bodies = new Map([...raw].map(([f, text]) => [f, text.split(/^---$/m).slice(2).join('---')]))
const tools = buildTools({ request: async () => ({}) })
const byName = new Map(tools.map((tool) => [tool.name, tool]))

test('目录里每一份都是冻结产物，且每份都点名了工具', () => {
  for (const [file, text] of raw) {
    assert.ok(text.startsWith('---'), `${file} 没有 frontmatter，不是冻结产物`)
    assert.match(text, /^id: \S+/m, `${file} 没有 frontmatter id，不是冻结产物`)
    assert.match(text, /^prompt_sha256: [0-9a-f]{64}\s*$/m, `${file} 没有 prompt_sha256`)
  }
  for (const [file, body] of bodies) {
    const names = new Set([...body.matchAll(/\b(quant_[a-z_]+)\b/g)].map((m) => m[1]))
    assert.ok(names.size >= 1, `${file} 没有点名任何公开工具，像是被截断了`)
  }
})

test('提示词里没有不存在的工具', () => {
  const bad = []
  for (const [file, body] of bodies) {
    for (const match of body.matchAll(/\b(quant_[a-z_]+)\b/g)) {
      if (match[1].endsWith('_') || body.slice(match.index + match[1].length).startsWith('_*')) continue
      if (!byName.has(match[1])) bad.push(`${file}: ${match[1]}`)
    }
  }
  assert.deepEqual(bad, [], `这些工具不存在：${bad.join(', ')}`)
})

test('调用示例里的 operation 与 params 都在 catalog 中声明过', async () => {
  const bad = []
  let calls = 0
  for (const [file, body] of bodies) {
    const result = await validateFacadeExamples(body, file)
    calls += result.count
    bad.push(...result.errors)
  }
  assert.ok(calls >= 20, `只解析到 ${calls} 个调用示例，解析器坏了`)
  assert.deepEqual(bad, [], bad.join('; '))
})

test('提示词点名的工具，技能里都有一行说明', () => {
  const missing = new Set()
  for (const body of bodies.values()) {
    for (const match of body.matchAll(/\b(quant_[a-z_]+)\b/g)) {
      if (match[1].endsWith('_')) continue
      if (byName.has(match[1]) && !SKILL.includes(match[1])) missing.add(match[1])
    }
  }
  assert.deepEqual([...missing], [], `技能里没有这些工具的入口：${[...missing].join(', ')}`)
})
