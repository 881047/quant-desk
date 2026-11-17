import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

import {
  evidenceRowModel,
  evidenceUniverseCount,
  executionRowModel,
  outcomeText,
  trackingView,
} from '../../web/assets/js/pages/kernel-short-alpha.js'

test('short-alpha evidence view uses the actual universe and pagination contract', () => {
  assert.equal(evidenceUniverseCount({
    universe: { matched_current_after_requested_filter: 7 },
  }), 7)
  assert.equal(evidenceUniverseCount({ universe: { matched_current: 99 } }), 0,
    'legacy/wrong field must not silently invent a count')

  const model = evidenceRowModel({
    evidence_id: 'short-alpha-evidence:20261113:000001.SZ',
    symbol: '000001.SZ', name: '示例', boards: 2, seal_ratio: 0.15,
    fill_kind: 'one_word',
    evidence: { horizons: { t1_close: { net_excess_mean: 0, n: 0, n_days: 0 } } },
    execution: { account_ok: false, total_round_trip_cost_bps: 0 },
  })
  assert.equal(model.historyMean, 0)
  assert.equal(model.historyN, 0)
  assert.equal(model.costBps, 0)
  assert.equal(model.boundary, '一字：无成交承诺')
})

test('tracking view reads prospective only and keeps unregistered distinct', () => {
  const view = trackingView({
    as_of: '20261113', latest_bar_judgment_registered: false,
    summary: {
      prospective: {
        signal_days: 0, candidate_signals: 0, watch_signals: 0,
        groups: {
          candidate: { executable_t1_open: { t1_close: { n: 0, net_excess_mean: null } } },
        },
      },
      replay: { signal_days: 20, candidate_signals: 20 },
    },
  })
  assert.equal(view.registered, false)
  assert.equal(view.signalDays, 0)
  assert.equal(view.candidateT1.n, 0)
  assert.equal(view.candidateT1.net_excess_mean, null)
})

test('pending, invalid and no-fill never become a zero return', () => {
  assert.equal(outcomeText({ status: 'pending', reason: 'T+1 daily bar not available' }), '等待后续日线')
  assert.equal(outcomeText({ status: 'no_fill', reason: 'no_fill_open_one_word' }), '一字，无成交代理')
  assert.equal(outcomeText({ status: 'no_fill', reason: 'queue_required_fill_unknown' }), '排队位置未知')
  assert.equal(outcomeText({ status: 'no_fill', reason: 'no_fill_account_budget_exceeded' }), '账户预算不足')
  assert.match(outcomeText({ status: 'invalid', reason: 'missing_or_noncontiguous_or_corporate_action_label' }), /不计0/)
  assert.equal(outcomeText({ status: 'settled', net_excess: 0 }), null,
    'settled zero is rendered by the numeric cell, not replaced by a status label')
})

test('execution view keeps queued distinct from filled and shows dynamic exit reasons', () => {
  const model = executionRowModel({
    symbol: '000001.SZ', name: '示例', role: 'watch', target_weight: 0.1,
    state: 'open', exit_deadline_day: '20261117',
    entry_ticket: { status: 'queued' },
    position: { qty: 100, available_qty: 100 },
    latest_review: { decision: 'exit_next_open', reason: '失效条件成立，下一开盘退出。' },
    exit_ticket: { status: 'proposed' },
  })
  assert.equal(model.entryStatus, 'queued')
  assert.equal(model.exitStatus, 'proposed')
  assert.equal(model.review, '下一开盘退出')
  assert.equal(model.availableQty, 100)
  assert.equal(model.deadline, '20261117')
})

test('the frontend is read-only and never substitutes legacy kernel candidates', () => {
  const source = fs.readFileSync(new URL('../../web/assets/js/pages/kernel-short-alpha.js', import.meta.url), 'utf8')
  assert.match(source, /\/short-alpha\/evidence/)
  assert.match(source, /\/short-alpha\/tracking\?scope=prospective/)
  assert.match(source, /\/short-alpha\/execution\?limit=6/)
  assert.doesNotMatch(source, /\/short-alpha\/candidates/)
  assert.doesNotMatch(source, /\/short-alpha\/judgments/)
  assert.doesNotMatch(source, /\/short-alpha\/reviews/)
  assert.doesNotMatch(source, /method:\s*['"]POST['"]/)
})
