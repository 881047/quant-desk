/**
 * 把四个审计的结果压成一份快照，并和上一份比出"变坏/变好"。
 *
 * 单拎出来是为了能测：一个不会报警的对比，和一个不会报警的审计是同一类东西。
 * `audit_all.mjs` 只负责跑和打印。
 */

/** 字节数涨多少算"变坏"。5% 在 16 KB 上是 800 B，比任何一次真实加字段都小。 */
export const LOOSE = 1.05

/**
 * 但百分比在极小的响应上没有意义。
 *
 * 2026-11-04 实测：`quant_tickets(limit=15)` 在"没有未结票据"时返回 14 B 的
 * `{"tickets":[]}`，补上 `as_of` 与一句说明后是 190 B——+1257%，而它还差 16 KB
 * 才碰到上限。涨幅阈值对空状态每加一句解释都会响一次，而每一次都被判成误报的
 * 对比，真变坏时也没人看了。绝对增量小于这个数就不算变坏：256 B 是 16 KB 的
 * 1.5%，不可能是一次"送达失败"的原因。
 */
export const FLOOR = 256

/** 把四种形状压成同一份快照：谁坏了、坏在哪、字节多大。 */
export function normalise(name, body) {
  if (!body) return { failed: true }
  if (name === 'delivery') {
    const bad = body.filter((row) => row.state !== 'intact')
    return {
      n: body.length, ok: body.length - bad.length,
      bytes: Object.fromEntries(body.map((row) => [row.tag, row.bytes])),
      bad: bad.map((row) => ({ tag: row.tag, state: row.state, bytes: row.bytes })),
    }
  }
  if (name === 'quality') {
    const dirty = body.filter((row) => row.issues?.length)
    return {
      n: body.length, ok: body.length - dirty.length,
      dirty: Object.fromEntries(dirty.map((row) => [row.tag, row.issues])),
    }
  }
  if (name === 'freshness') {
    const tools = body.tools || []
    const unanchored = tools
      .filter((row) => !row.anchor && row.quoteAge === null && !row.document)
      .map((row) => row.tag)
    return {
      tools: tools.length, anchored: tools.length - unanchored.length,
      unanchored,
      stale: (body.stale || []).map((row) => `${row.tag}@${row.anchor}`),
      datasets_stale: (body.datasets || []).filter((d) => (d.lag ?? 0) > 3)
        .map((d) => `${d.dataset}:${d.lag}`),
    }
  }
  const results = body.results || []
  return {
    n: results.length, ok: results.filter((row) => row.state === 'ok').length,
    na: results.filter((row) => row.state === 'na').map((row) => row.id),
    diverge: results.filter((row) => row.state === 'diverge')
      .map((row) => ({ id: row.id, a: row.a, b: row.b, what: row.what })),
  }
}

/**
 * 与历史无关的判断：这份快照本身有没有"送不到"或"说不清"的东西。
 *
 * `compare()` 只看差值，所以有两个问题它答不了：第一次跑（没有上一次），
 * 以及一直坏着（两次都有，差值为零）。2026-11-04 之前 `audit_all.mjs` 把它们
 * 都判成绿的——一轮审计从来没错过，只是因为它从没和"零"比过。
 *
 * `degraded` **不算**：16 KB 上限下砍行是设计内的取舍，`quant_pools` 与
 * `quant_journal_search` 每天都会砍。把它当失败就是那个永远在响的告警。
 * 它变没变大，交给 `compare()` 的 `bad` 差分。
 */
export function absolute(now) {
  const problems = []
  for (const row of now?.delivery?.bad || []) {
    if (row.state !== 'degraded') problems.push(`送达 ${row.tag} 是 ${row.state}`)
  }
  for (const [tag, issues] of Object.entries(now?.quality?.dirty || {})) {
    problems.push(`质量 ${tag}：${issues.join(' / ')}`)
  }
  for (const tag of now?.freshness?.unanchored || []) {
    problems.push(`新鲜 ${tag} 不自述年龄`)
  }
  for (const row of now?.consistency?.diverge || []) {
    problems.push(`一致 ${row.id} 分歧：A=${row.a} B=${row.b}`)
  }
  return problems
}

/** 和上一份快照比：只报"变坏"，顺带报"修好"。 */
export function compare(now, before) {
  const worse = []
  const better = []
  if (!before) return { worse, better, first: true }

  const d0 = before.delivery || {}
  const d1 = now.delivery || {}
  for (const [tag, bytes] of Object.entries(d1.bytes || {})) {
    const was = d0.bytes?.[tag]
    if (!was) continue
    if (bytes > was * LOOSE && bytes - was >= FLOOR) {
      worse.push(`送达 ${tag} ${was} → ${bytes} B（+${Math.round((bytes / was - 1) * 100)}%，上限 16,384）`)
    } else if (was > bytes * LOOSE && was - bytes >= FLOOR) {
      better.push(`送达 ${tag} ${was} → ${bytes} B`)
    }
  }
  const wasBad = new Set((d0.bad || []).map((row) => row.tag))
  for (const row of d1.bad || []) {
    if (!wasBad.has(row.tag)) worse.push(`送达 ${row.tag} 变成 ${row.state}`)
  }
  for (const tag of wasBad) {
    if (!(d1.bad || []).some((row) => row.tag === tag)) better.push(`送达 ${tag} 恢复了`)
  }

  const q0 = before.quality?.dirty || {}
  const q1 = now.quality?.dirty || {}
  for (const [tag, issues] of Object.entries(q1)) {
    if (!q0[tag]) worse.push(`质量 ${tag} 新出现问题：${issues.join(' / ')}`)
  }
  for (const tag of Object.keys(q0)) if (!q1[tag]) better.push(`质量 ${tag} 修好了`)

  const f0 = new Set(before.freshness?.unanchored || [])
  const f1 = new Set(now.freshness?.unanchored || [])
  for (const tag of f1) if (!f0.has(tag)) worse.push(`新鲜 ${tag} 不再自述年龄`)
  for (const tag of f0) if (!f1.has(tag)) better.push(`新鲜 ${tag} 补上了年龄`)

  const c0 = new Set((before.consistency?.diverge || []).map((row) => row.id))
  const c1 = new Set((now.consistency?.diverge || []).map((row) => row.id))
  for (const row of now.consistency?.diverge || []) {
    if (!c0.has(row.id)) worse.push(`一致 ${row.id} 分歧：A=${row.a} B=${row.b}`)
  }
  for (const id of c0) if (!c1.has(id)) better.push(`一致 ${id} 回到一致`)

  return { worse, better, first: false }
}
