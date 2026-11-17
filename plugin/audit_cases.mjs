/**
 * 审计用的调用清单：一次定义，两个脚本共用。
 *
 * 为什么要有这份清单：`quant_profile` 在 600519 上是 13.8 KB、在 300750 上是 15.7 KB，
 * `quant_bars(days=60)` 是 8.5 KB、`days=250` 是 27 KB。同一个工具在不同参数下是
 * 不同的东西，所以"测一遍"必须按参数取极值，而不是挑一个顺眼的例子。
 */

export const CASES = [
  ['quant_context', {}],
  ['quant_watch', {}],
  ['quant_watch', { since: '20261029' }],
  ['quant_facts', { date: '20261030' }],
  ['quant_information', {}],
  ['quant_data_health', {}],
  ['quant_quote', { symbols: '600519.SH,000001.SZ' }],
  ['quant_bars', { symbol: '600519.SH', days: 60 }],
  ['quant_bars', { symbol: '600519.SH', days: 250 }, 'asks 250'],
  ['quant_pools', { limit: 20 }],
  ['quant_universe', { limit: 40 }],
  ['quant_screen', { limit: 8 }],
  ['quant_lhb', { limit: 40 }],
  ['quant_dashboard', {}],
  ['quant_knowledge', { topic: 'industries' }],
  ['quant_knowledge', { topic: 'ledger' }],
  ['quant_knowledge', { topic: 'cards' }],
  ['quant_knowledge', { topic: 'regime' }],
  ['quant_knowledge', { topic: 'analog', symbol: '600519.SH' }],
  ['quant_knowledge', { topic: 'distribution', symbol: '600519.SH' }],
  ['quant_knowledge', { topic: 'crowding', symbol: '600519.SH' }],
  ['quant_knowledge', { topic: 'liquidity', symbol: '600519.SH' }],
  ['quant_knowledge', { topic: 'position', symbol: '600519.SH' }],
  ['quant_knowledge', { topic: 'risk' }],
  ['quant_knowledge', { topic: 'filings', symbol: '300750.SZ' }],
  ['quant_journal_search', { limit: 20 }],
  ['quant_journal_search', { limit: 50, full: true, recent: 20 }, 'asks 50 full'],
  ['quant_memo', { action: 'list', limit: 50 }],
  ['quant_tickets', { limit: 15 }],
  ['quant_tickets', { status: 'filled', limit: 15 }],
  ['quant_orders', { limit: 80 }],
  ['quant_cases', { limit: 50 }],
  ['quant_attribution', {}],
  ['quant_decay', {}],
  ['quant_execution_tca', {}],
  ['quant_playbook', {}],
  ['quant_persona', {}],
  ['quant_discover', {}],
  ['quant_regime', {}],
  ['quant_exposure', {}],
  ['quant_stops', {}],
  ['quant_arena', {}],
  ['quant_observations', {}],
  ['quant_duty', {}],
  ['quant_strategies', {}],
  ['quant_samples', {}],
  ['quant_hypothesis', { action: 'list' }],
  ['quant_lineage', { date: '20261030' }],
  ['quant_portfolio', {}],
  ['quant_risk', {}],
  ['quant_auction', { limit: 10 }],
  ['quant_anomaly', { limit: 10 }],
  ['quant_limit_structure', {}],
  ['quant_sector', {}],
  ['quant_sector', { date: '20261030' }, 'last session'],
]

// profile varies by name more than by argument: the biggest one decided the fix.
for (const symbol of ['600519.SH', '300750.SZ', '000001.SZ', '688981.SH', '920592.BJ']) {
  CASES.push(['quant_profile', { symbol, valuation_window: 750 }, symbol])
}
CASES.push(['quant_compare', { symbols: ['600519.SH', '000858.SZ'] }, '600519 vs 000858'])
CASES.push(['quant_filing', { announcement_id: '1225620609' }, 'filing p1'])
CASES.push(['quant_filing', { announcement_id: '1225620609', offset: 3971 }, 'filing p2'])
CASES.push(['quant_filing', { announcement_id: '1225620609', chars: 6000 }, 'filing byte-capped'])
CASES.push(['quant_filing', { announcement_id: '9999999999' }, 'filing unknown id'])
// 分段请求：一块、几块，各自都要能完整送达
CASES.push(['quant_profile', { symbol: '300750.SZ', sections: ['events'] }, '300750 events only'])
CASES.push(['quant_profile', { symbol: '300750.SZ', sections: ['valuation', 'peers'] }, '300750 val+peers'])

/**
 * 判断记录，不是某一天的市场快照。
 *
 * 这两条回答的是"这个台子作过哪些结论"和"知识库里有什么"，它们的年龄是
 * **什么时候记下的**（`generated_at`），不是"哪一天的市场"——`ledger` 里的先验
 * 定稿于 P5–P8，`live` 的证据窗口到 2025-12，都不随行情更新。给它们硬塞一个
 * `as_of=<最新交易日>` 才是真正的不诚实：那会让一条 2025-12 的证据读起来像今天算的。
 *
 * 必须显式列在这里，而不是让审计见到 `generated_at` 就放行：一个真正的行情快照
 * 只要盖个"我刚跑的"时间戳就能骗过新鲜度判定，而它恰恰是这套审计要盯的东西。
 */
export const RECORD_CASES = new Set([
  'quant_knowledge(topic=ledger)',
  'quant_knowledge(topic=cards)',
])

/** `name(args)` with a short human label, for tables. */
export function tagOf([name, args, label]) {
  if (label) return `${name}(${label})`
  const brief = Object.entries(args || {})
    .map(([k, v]) => `${k}=${v}`)
    .join(',')
  return brief ? `${name}(${brief})` : name
}
