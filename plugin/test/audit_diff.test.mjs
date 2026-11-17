/**
 * `audit_diff` 的测试：对比本身必须会报警。
 *
 * 一个从不报警的对比，和一个从不报警的审计是同一类东西——它只会让"没有变坏"
 * 这句话变得不可信。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { absolute, compare, normalise } from '../audit_diff.mjs'

const snap = (over = {}) => ({
  when: '2026-11-02T04:00:00.000Z',
  delivery: { n: 2, ok: 2, bytes: { a: 1000, b: 2000 }, bad: [] },
  quality: { n: 2, ok: 2, dirty: {} },
  freshness: { tools: 2, anchored: 2, unanchored: [] },
  consistency: { n: 2, ok: 2, na: [], diverge: [] },
  ...over,
})

test('字节数涨过 5% 才算变坏', () => {
  const grown = snap({ delivery: { n: 2, ok: 2, bytes: { a: 1040, b: 2000 }, bad: [] } })
  assert.deepEqual(compare(grown, snap()).worse, [], '4% 是正常波动')

  const jumped = snap({ delivery: { n: 2, ok: 2, bytes: { a: 1400, b: 2000 }, bad: [] } })
  assert.match(compare(jumped, snap()).worse[0], /送达 a 1000 → 1400 B/)
})

test('极小的响应按绝对增量判，不按百分比', () => {
  // 空状态的一句说明把 14 B 变成 190 B 是 +1257%，但它离 16 KB 上限还差 16 KB。
  // 对 14 B 用 5% 阈值，等于"每加一句解释就报一次变坏"。
  const tiny = snap({ delivery: { n: 1, ok: 1, bytes: { a: 14 }, bad: [] } })
  const explained = snap({ delivery: { n: 1, ok: 1, bytes: { a: 190 }, bad: [] } })

  assert.deepEqual(compare(explained, tiny).worse, [], '176 B 的增量不是送达风险')
  // 但同一个工具从 14 B 长到 2 KB 就要报：绝对增量够大，说明它换了个东西。
  const bloated = snap({ delivery: { n: 1, ok: 1, bytes: { a: 2048 }, bad: [] } })
  assert.match(compare(bloated, tiny).worse[0], /送达 a 14 → 2048 B/)
})

test('字节数掉下来算变好，不算变坏', () => {
  const shrunk = snap({ delivery: { n: 2, ok: 2, bytes: { a: 700, b: 2000 }, bad: [] } })
  const { worse, better } = compare(shrunk, snap())

  assert.deepEqual(worse, [])
  assert.match(better[0], /送达 a 1000 → 700 B/)
})

test('一个工具新变脏要报出来，原样不能报', () => {
  const dirty = snap({ quality: { n: 2, ok: 1, dirty: { a: ['A 缺时间锚'] } } })
  assert.match(compare(dirty, snap()).worse[0], /质量 a 新出现问题：A 缺时间锚/)
  assert.deepEqual(compare(dirty, dirty).worse, [], '同样的脏不该每次都报')
})

test('修好了要说一声，那是信息', () => {
  const clean = snap()
  const was = snap({ quality: { n: 2, ok: 1, dirty: { a: ['B 占位符'] } } })

  assert.deepEqual(compare(clean, was).worse, [])
  assert.match(compare(clean, was).better[0], /质量 a 修好了/)
})

test('一致性从 ok 变分歧要报，回到一致要报', () => {
  const broke = snap({ consistency: { n: 2, ok: 1, na: [], diverge: [{ id: 'x', a: 1, b: 2 }] } })
  assert.match(compare(broke, snap()).worse[0], /一致 x 分歧：A=1 B=2/)
  assert.match(compare(snap(), broke).better[0], /一致 x 回到一致/)
})

test('第二次跑同一份结果，什么都不该报', () => {
  const { worse, better, first } = compare(snap(), snap())

  assert.equal(first, false)
  assert.deepEqual(worse, [])
  assert.deepEqual(better, [])
})

test('没有上一次时只标记 first，不假装有结论', () => {
  const { worse, first } = compare(snap(), null)

  assert.equal(first, true)
  assert.deepEqual(worse, [])
})

test('没有上一次时，绝对值要能自己说话', () => {
  // `compare()` 在没有基线时一定返回空的 worse。**只看它就等于第一次永远绿**，
  // 而"第一次"恰好是最需要被看见的一次——那时没有任何历史在替你背书。
  assert.deepEqual(absolute(snap()), [], '干净快照不能凭空报问题')

  const broken = snap({
    delivery: { n: 2, ok: 0, bytes: { a: 1000, b: 2000 },
      bad: [{ tag: 'a', state: 'dropped' }, { tag: 'b', state: 'threw' }] },
    consistency: { n: 2, ok: 1, na: [], diverge: [{ id: 'x', a: 1, b: 2 }] },
  })
  const problems = absolute(broken)

  assert.equal(problems.length, 3)
  assert.match(problems[0], /送达 a 是 dropped/)
  assert.match(problems[1], /送达 b 是 threw/)
  assert.match(problems[2], /一致 x 分歧：A=1 B=2/)
})

test('degraded 是设计内的降级，不能当成送达失败', () => {
  // 16 KB 上限下砍行是取舍：`quant_pools(limit=20)` 与
  // `quant_journal_search(limit=20)` 每天都砍。把它们算成失败，这个闸就会天天红，
  // 而"永远在响的告警"和没有告警是一回事。它们变大了由 `compare()` 的差分盯。
  const degraded = snap({
    delivery: { n: 2, ok: 0, bytes: { a: 15731, b: 9436 },
      bad: [{ tag: 'a', state: 'degraded' }, { tag: 'b', state: 'degraded' }] },
  })

  assert.deepEqual(absolute(degraded), [])
})

test('四种形状都能压成同一份快照', () => {
  const delivery = normalise('delivery', [
    { tag: 't1', bytes: 10, state: 'intact' }, { tag: 't2', bytes: 99, state: 'dropped' }])
  assert.equal(delivery.ok, 1)
  assert.equal(delivery.bad[0].tag, 't2')

  const quality = normalise('quality', [{ tag: 't1', issues: [] }, { tag: 't2', issues: ['C'] }])
  assert.deepEqual(quality.dirty, { t2: ['C'] })

  const freshness = normalise('freshness', {
    tools: [{ tag: 't1', anchor: '20261030' }, { tag: 't2', anchor: null, quoteAge: null }],
    stale: [], datasets: [{ dataset: 'daily', lag: 9 }],
  })
  assert.deepEqual(freshness.unanchored, ['t2'])
  assert.deepEqual(freshness.datasets_stale, ['daily:9'])

  const consistency = normalise('consistency', {
    results: [{ id: 'a', state: 'ok' }, { id: 'b', state: 'diverge', a: 1, b: 2 }],
  })
  assert.equal(consistency.ok, 1)
  assert.equal(consistency.diverge[0].id, 'b')
})
