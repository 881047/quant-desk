/**
 * Four public tools over the 73 internal handlers.
 *
 * This module deliberately contains no route and no trading rule. It resolves a
 * small allowlist of operations to the existing handlers, which still own path
 * construction, validation, idempotency, pagination and response post-processing.
 */

const QUERY = {
  context: ['quant_context'],
  data_health: ['quant_data_health'],
  information: ['quant_information'],
  facts: ['quant_facts'],
  live: ['quant_live'],
  quote: ['quant_quote'],
  market_status: ['quant_market_status'],
  bars: ['quant_bars'],
  mins: ['quant_mins'],
  profile: ['quant_profile'],
  filing: ['quant_filing'],
  compare: ['quant_compare'],
  short_alpha_evidence: ['quant_short_alpha_evidence'],
  short_alpha_execution: ['quant_short_alpha_execution'],
  short_alpha_tracking: ['quant_short_alpha_tracking'],
  short_alpha_legacy: ['quant_short_alpha'],
  pools: ['quant_pools'],
  screen: ['quant_screen'],
  portfolio: ['quant_portfolio'],
  risk: ['quant_risk'],
  ticket: ['quant_ticket'],
  tickets: ['quant_tickets'],
  samples: ['quant_samples'],
  orders: ['quant_orders'],
  journal_search: ['quant_journal_search'],
  reports: ['quant_reports'],
  playbook: ['quant_playbook'],
  memory_search: ['quant_memory_search'],
  cases: ['quant_cases'],
  hypothesis: ['quant_hypothesis', { action: 'list' }],
  lineage: ['quant_lineage'],
  discover: ['quant_discover'],
  core: ['quant_core'],
  sector: ['quant_sector'],
  universe: ['quant_universe'],
  strategies: ['quant_strategies'],
  decay: ['quant_decay'],
  execution_estimate: ['quant_execution_estimate'],
  execution_tca: ['quant_execution_tca'],
  attribution: ['quant_attribution'],
  persona: ['quant_persona'],
  dashboard: ['quant_dashboard'],
  duty: ['quant_duty'],
  regime: ['quant_regime'],
  exposure: ['quant_exposure'],
  stops: ['quant_stops'],
  arena: ['quant_arena'],
  watch: ['quant_watch'],
  observations: ['quant_observations'],
  core_preview: ['quant_core_preview'],
  knowledge: ['quant_knowledge'],
  limit_structure: ['quant_limit_structure'],
  lhb: ['quant_lhb'],
  auction: ['quant_auction'],
  anomaly: ['quant_anomaly'],
  analog: ['quant_analog'],
  memo: ['quant_memo', {}, ['list', 'calibration']],
  declined: ['quant_declined', { action: 'list' }],
}

const ORDER = {
  'paper.propose': ['quant_propose'],
  'paper.approve': ['quant_approve'],
  'paper.reject': ['quant_reject'],
  'paper.cancel': ['quant_cancel'],
  'samples.propose': ['quant_samples_propose'],
  'samples.approve': ['quant_samples_approve'],
  'samples.reject': ['quant_samples_reject'],
}

const WORKFLOW = {
  short_alpha_judge: ['quant_short_alpha_judge'],
  short_alpha_review: ['quant_short_alpha_review'],
  journal_append: ['quant_journal_append'],
  playbook_propose: ['quant_playbook_propose'],
  persona_switch: ['quant_persona_switch'],
  hypothesis_add: ['quant_hypothesis', { action: 'add' }, null, ['statement', 'test_spec']],
  hypothesis_result: ['quant_hypothesis', { action: 'result' }, null, ['hyp_id', 'status']],
  memo_add: ['quant_memo', { action: 'add' }, null, ['topic', 'thesis']],
  memo_score: ['quant_memo', { action: 'score' }],
  declined_add: ['quant_declined', { action: 'add' }, null, ['symbols', 'reason']],
  declined_score: ['quant_declined', { action: 'score' }],
}

export const PUBLIC_TOOL_NAMES = Object.freeze([
  'quant_query', 'quant_order', 'quant_workflow', 'quant_alert',
])

const PARAMS = {
  type: 'object',
  additionalProperties: true,
  description: 'Operation-specific parameters. Read quant_query(resource=catalog) for the strict schema; path/method are never accepted.',
}

const PUBLIC_SCHEMAS = {
  quant_query: {
    type: 'object',
    properties: {
      resource: { type: 'string', enum: ['catalog', ...Object.keys(QUERY)] },
      params: PARAMS,
    },
    required: ['resource'],
    additionalProperties: false,
  },
  quant_order: {
    type: 'object',
    properties: {
      book: { type: 'string', enum: ['paper', 'samples'] },
      action: { type: 'string', enum: ['propose', 'approve', 'reject', 'cancel'] },
      params: PARAMS,
    },
    required: ['book', 'action'],
    additionalProperties: false,
  },
  quant_workflow: {
    type: 'object',
    properties: {
      operation: { type: 'string', enum: Object.keys(WORKFLOW) },
      params: PARAMS,
    },
    required: ['operation'],
    additionalProperties: false,
  },
  quant_alert: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['list', 'answer'] },
      params: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          analysis: { type: 'string' },
          confirm: { type: 'boolean', enum: [true] },
        },
        additionalProperties: false,
      },
    },
    required: ['action'],
    additionalProperties: false,
  },
}

function paramsOf(args) {
  const params = args.params ?? {}
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    throw new Error('params must be an object')
  }
  return params
}

function resolve(table, operation, args) {
  const spec = table[operation]
  if (!spec) throw new Error(`unknown operation: ${operation}`)
  const [legacyName, fixed = {}, allowedActions = null, requiredParams = []] = spec
  const params = paramsOf(args)
  for (const key of Object.keys(fixed)) {
    if (Object.hasOwn(params, key)) {
      throw new Error(`${key} is fixed by operation ${operation}; do not pass it inside params`)
    }
  }
  for (const key of requiredParams) {
    if (!Object.hasOwn(params, key) || params[key] === undefined || params[key] === null) {
      throw new Error(`${key} is required for operation ${operation}`)
    }
  }
  if (allowedActions) {
    if (!Object.hasOwn(params, 'action')) {
      throw new Error(`params.action is required for ${operation}: ${allowedActions.join(' | ')}`)
    }
    if (!allowedActions.includes(params.action)) {
      throw new Error(`operation ${operation} is read-only; action must be ${allowedActions.join(' | ')}`)
    }
  }
  return {
    legacyName,
    legacyArgs: { ...params, ...fixed },
    operation,
  }
}

/** Pure operation resolution shared by guard, execution, rendering and catalog. */
export function resolveFacadeOperation(name, args = {}) {
  if (name === 'quant_query') {
    const resource = args.resource
    if (resource === 'catalog') return { local: 'catalog', operation: 'catalog' }
    if (typeof resource !== 'string' || !resource) throw new Error('resource is required')
    return resolve(QUERY, resource, args)
  }
  if (name === 'quant_order') {
    const book = args.book
    const action = args.action
    if (!book || !action) throw new Error('book and action are required; neither has a default')
    if (book === 'external') throw new Error('external is read-only and is not an order book')
    return resolve(ORDER, `${book}.${action}`, args)
  }
  if (name === 'quant_workflow') {
    if (!args.operation) throw new Error('operation is required')
    return resolve(WORKFLOW, args.operation, args)
  }
  if (name === 'quant_alert') {
    const action = args.action
    const params = paramsOf(args)
    if (action === 'list') {
      if (Object.keys(params).length) throw new Error('alert list takes no params')
      return { legacyName: 'quant_alert_handoff', legacyArgs: {}, operation: 'list' }
    }
    if (action !== 'answer') throw new Error('action must be list or answer')
    const legal = ['id', 'analysis', 'confirm']
    const unknown = Object.keys(params).filter((key) => !legal.includes(key))
    if (unknown.length) throw new Error(`unknown alert answer param(s): ${unknown.join(', ')}`)
    if (!params.id || !params.analysis) throw new Error('alert answer requires id and analysis')
    if (params.confirm !== true) throw new Error('alert answer requires confirm=true because it sends mail')
    return {
      legacyName: 'quant_alert_handoff',
      legacyArgs: { id: params.id, analysis: params.analysis },
      operation: 'answer',
    }
  }
  throw new Error(`not a facade tool: ${name}`)
}

function paramsSchema(tool, spec) {
  const [, fixed = {}, allowedActions = null, requiredParams = []] = spec || []
  const source = tool?.parameters || { type: 'object', properties: {} }
  const properties = Object.fromEntries(
    Object.entries(source.properties || {}).filter(([key]) => !Object.hasOwn(fixed, key)),
  )
  if (allowedActions && properties.action) {
    properties.action = { ...properties.action, enum: allowedActions }
  }
  const required = [...new Set([...(source.required || []), ...requiredParams])]
    .filter((key) => !Object.hasOwn(fixed, key))
  return {
    ...source,
    properties,
    required,
    additionalProperties: false,
  }
}

function descriptors(internal, full = false) {
  const rows = []
  const add = (toolName, operation, legacyName, spec, extra = {}) => {
    const legacy = internal.get(legacyName)
    if (!legacy) throw new Error(`internal handler missing for ${operation}: ${legacyName}`)
    const row = {
      tool: toolName,
      operation,
      legacy: legacyName,
      readOnly: toolName === 'quant_query',
      outputKind: legacy.outputKind || 'text',
      ...extra,
    }
    if (full) {
      row.description = legacy.description
      row.paramsSchema = paramsSchema(legacy, spec)
    }
    rows.push(row)
  }
  for (const [operation, spec] of Object.entries(QUERY)) {
    add('quant_query', operation, spec[0], spec)
  }
  for (const [operation, spec] of Object.entries(ORDER)) {
    add('quant_order', operation, spec[0], spec, { book: operation.split('.')[0] })
  }
  for (const [operation, spec] of Object.entries(WORKFLOW)) {
    add('quant_workflow', operation, spec[0], spec)
  }
  for (const operation of ['list', 'answer']) {
    const legacy = internal.get('quant_alert_handoff')
    const row = {
      tool: 'quant_alert', operation, legacy: 'quant_alert_handoff',
      readOnly: operation === 'list', outputKind: 'text',
      ...(operation === 'answer' ? { externalEffect: 'mail', confirmRequired: true } : {}),
    }
    if (full) {
      row.description = legacy.description
      row.paramsSchema = operation === 'list'
        ? { type: 'object', properties: {}, additionalProperties: false }
        : PUBLIC_SCHEMAS.quant_alert.properties.params
    }
    rows.push(row)
  }
  return rows
}

function catalog(internal, args) {
  const params = paramsOf(args)
  const legal = ['tool', 'operation', 'full']
  const unknown = Object.keys(params).filter((key) => !legal.includes(key))
  if (unknown.length) throw new Error(`unknown catalog param(s): ${unknown.join(', ')}`)
  if (params.tool && !PUBLIC_TOOL_NAMES.includes(params.tool)) {
    throw new Error(`unknown public tool: ${params.tool}`)
  }
  if (params.full === true && !params.operation) {
    throw new Error('catalog full=true requires operation to keep the response bounded')
  }
  const rows = descriptors(internal, params.full === true).filter((row) => (
    (!params.tool || row.tool === params.tool)
    && (!params.operation || row.operation === params.operation)
  ))
  if (params.operation && !rows.length) throw new Error(`unknown catalog operation: ${params.operation}`)
  return {
    ok: true,
    note: 'Compact by default. Pass params={operation:"...",full:true} for the strict params schema.',
    operations: rows,
  }
}

const DESCRIPTIONS = {
  quant_query: 'Read-only market, instrument, research, account, ticket, short-alpha, journal and report queries. resource=catalog returns the operation directory and strict params schemas. Never writes a book, freezes a judgement or sends mail.',
  quant_order: 'Order/ticket commands only. Explicit book=paper|samples and action are required; external is forbidden. Existing guards, server-computed prices, idempotency and queued!=filled semantics are unchanged.',
  quant_workflow: 'Non-ticket writes: immutable short-alpha judgement/review, journal, playbook draft, persona switch, hypotheses, memos and declined decisions. Cannot create/approve orders or send mail.',
  quant_alert: 'External alert handoff. list is read-only; answer sends mail and requires confirm=true. Separate from every simulated ledger and order command.',
}

function toParameterSpec(schema) {
  const required = new Set(schema.required || [])
  return Object.fromEntries(Object.entries(schema.properties || {}).map(([key, spec]) => [
    key,
    { ...spec, ...(required.has(key) ? { required: true } : {}) },
  ]))
}

/** Build exactly four public tools over one internal handler map. */
export function buildFacadeTools({
  internalTools, define, guard, formatJsonOutput, shrinkLadder, maxOutput,
}) {
  const internal = new Map(internalTools.map((tool) => [tool.name, tool]))
  const make = (name) => {
    const tool = define(name, DESCRIPTIONS[name], toParameterSpec(PUBLIC_SCHEMAS[name]), async (args) => {
      const resolved = resolveFacadeOperation(name, args)
      if (resolved.local === 'catalog') return catalog(internal, args)
      const legacy = internal.get(resolved.legacyName)
      if (!legacy) throw new Error(`internal handler unavailable: ${resolved.legacyName}`)
      const denied = guard({ name: resolved.legacyName, arguments: resolved.legacyArgs })
      if (denied) throw new Error(denied)
      return legacy.execute(resolved.legacyArgs, {})
    })
    tool.outputKindFor = (args) => {
      const resolved = resolveFacadeOperation(name, args)
      return resolved.local ? 'text' : (internal.get(resolved.legacyName)?.outputKind || 'text')
    }
    // DSH: legacy.execute() has already applied `after` to the real response.
    // MCP: it saw only __request, so the adapter uses this hook exactly once.
    tool.afterFor = (args) => {
      const resolved = resolveFacadeOperation(name, args)
      if (resolved.local) return null
      const legacy = internal.get(resolved.legacyName)
      return { after: legacy.after || null, args: resolved.legacyArgs }
    }
    tool.output = {
      ...tool.output,
      render: (args, value) => [{
        type: 'text',
        text: formatJsonOutput(value, maxOutput, shrinkLadder(tool.outputKindFor(args))).text,
      }],
    }
    return tool
  }
  return PUBLIC_TOOL_NAMES.map(make)
}
