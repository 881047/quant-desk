/** The quant-desk skill must describe the same four facades exposed by the hosts. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { buildTools } from '../lib/index.js'
import { validateFacadeExamples } from './facade_docs_helpers.mjs'

const SKILL = fs.readFileSync(new URL('../skills/quant-desk/SKILL.md', import.meta.url), 'utf8')
const TOOLS = buildTools({ request: async () => ({}) })

test('四个公开工具都能从技能里找到入口', () => {
  const named = new Set(SKILL.match(/\bquant_[a-z_]+\b/g) || [])
  assert.equal(TOOLS.length, 4, '公开工具表不再是四项')
  assert.deepEqual([...named].sort(), ['quant_alert', 'quant_order', 'quant_query', 'quant_workflow'])
  for (const tool of TOOLS) assert.ok(named.has(tool.name), `${tool.name} 没有技能入口`)
  assert.ok(SKILL.includes('resource="catalog"'), '技能没有告诉模型如何发现具体操作')
})

test('技能里的 operation 与 params 都在 catalog 中声明过', async () => {
  const result = await validateFacadeExamples(SKILL, 'SKILL.md')
  assert.ok(result.count >= 20, `只解析到 ${result.count} 个调用示例，解析器或文档结构变了`)
  assert.deepEqual(result.errors, [], result.errors.join('; '))
})

test('买卖那条路必须写出 approve 之外的出口', () => {
  const row = SKILL.split('\n').find((line) => line.startsWith('| 帮我买 / 帮我卖'))
  assert.ok(row, '入口表里的「帮我买 / 帮我卖」不见了')
  assert.match(row, /action="propose"/)
  assert.match(row, /action="approve"/)
  assert.match(row, /action="reject"/,
    '只写 approve 不写 reject：提案会一直停在 proposed，19:30 只能记成"提了没下文"')
})

test('DeferExecuteTool 的示例必须用带前缀的纯工具全名', () => {
  const calls = [...SKILL.matchAll(/DeferExecuteTool\(\s*toolName\s*=\s*["']([^"']+)["']/g)]
  assert.ok(calls.length >= 1, '技能里没有 DeferExecuteTool 的调用示例，宿主那一节被删了？')
  for (const [, name] of calls) {
    assert.match(name, /^mcp__quant-desk__quant_(query|order|workflow|alert)$/)
    assert.ok(!name.includes('resource='), 'resource 必须放在 params，不能拼进工具名')
  }
})
