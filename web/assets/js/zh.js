// 中文对照表：后端的枚举值是契约（不许改），界面显示必须说人话。
//
// 为什么要一张表而不是各页自己 translate：同一个 `armed` 在票据列表、状态机、
// 待办里必须叫同一个词；一旦分散，用户会以为那是三种状态 —— 而这台机器的全部
// 价值就是不产生"看起来对但意思变了"的读数。
//
// 约定：表里没有的值**原样显示**并保留英文，同时加 `title` 说明这是未翻译的枚举。
// 不猜、不硬编一个中文名 —— 猜错比不译更糟。

const STATUS = {
  proposed: '待批',
  armed: '已批未成交',
  queued: '排队中',
  filled: '已成交',
  rejected: '已否决',
  cancelled: '已撤销',
  expired: '已过期',
};

const SIDE = { BUY: '买入', SELL: '卖出' };
const SLEEVE = { CORE: '核心', AGGRESSIVE: '进攻' };

const REGIME = {
  risk_on: '进攻',
  neutral: '中性',
  risk_off: '防守',
  stressed: '应激',
};

const READINESS = {
  READY: '数据就绪',
  NON_TRADING_DAY: '非交易日',
  SESSION_OPEN: '盘中',
  AWAITING_PUBLISH: '等待发布',
  THIN_COVERAGE: '覆盖不足',
  NO_CALENDAR: '没有日历',
};

const VENDOR = {
  eastmoney: '东方财富',
  ths: '同花顺',
  tencent: '腾讯',
  baostock: 'baostock',
  derived: '派生池',
  'derived:limit_pool_t0': '派生池（收盘）',
  manual: '手工录入',
  ledger: '账本兜底',
};

// 情绪计数的口径。两个页面各用一套：总览用东财收盘数，盘面用派生池
// （盘中触及涨停价即算炸板）。不把口径写在数字旁边，32 与 33 就会像打架。
const BASIS = {
  vendor: '东财收盘口径',
  'touched: 盘中触及涨停价即算炸板': '派生池口径：盘中触及涨停即算炸板',
};

const BOARD = {
  MAIN: '主板',
  CHINEXT: '创业板',
  STAR: '科创板',
  BSE: '北交所',
  CB: '可转债',
};

const DECAY = {
  no_baseline: '没有基线',
  insufficient: '样本不足',
  decaying: '正在衰减',
  ok: '正常',
  healthy: '正常',
  probation: '观察期',
};

const STRATEGY = {
  llm: '模型决策',
  core_etf: '核心 ETF',
  cb_link_down: '转债联动',
  sample_1d: '一日样本',
  market: '等权基准',
};

/** 复盘里"未计分"的归类。键来自 /arena 的 not_scored。 */
const NOT_SCORED = {
  user_instruction: '用户指令',
  unknown_closing_ticket: '收盘票据未识别',
  no_holding_window_data: '缺持仓窗口行情',
  no_baseline: '没有基准',
  open_position: '还没平仓',
};

/** 衰减说明改用后端 note_meta.reason 做键，不再拿英文句子当键、也不再正则抓参数。 */
const DECAY_REASON = {
  sample_below_min: '样本不足',
  no_baseline: '证据里既没有均值/波动，也没有年化收益/夏普',
  none: '—',
};

/** 数据集 cover_end 的语义。只有逐日面板的覆盖日才代表"数据到哪天"。 */
const DATASET_KIND = {
  calendar: '日历/日程',
  reference: '参考表',
  snapshot: '快照',
  on_demand: '按需',
  data: '逐日面板',
};

/** 27 项 guard 检查的中文名。值域见 GET /meta/vocabulary 的 guard_checks。 */
const CHECK = {
  kill_switch: '总开关',
  safe_mode: '安全模式',
  daily_actions: '当日动作数',
  max_positions: '持仓只数',
  min_cash: '最低现金',
  daily_loss: '当日亏损',
  reduce_only: '只减仓线',
  drawdown: '回撤分级',
  stop_gate: '破线闸门',
  equity: '净值有效',
  bar: '当日行情',
  st: 'ST 限制',
  suspended: '停牌',
  board: '板块准入',
  cb_maturity: '转债到期',
  price_band: '价格带',
  cash: '可用资金',
  single_cap: '单票上限',
  sleeve_cap: '仓位上限',
  industry_cap: '行业上限',
  strategy_cap: '策略上限',
  cost_bps: '往返费用',
  liquidity: '流动性',
  t_plus_one: 'T+1 可卖',
  has_position: '是否有持仓',
  // 拒因/详情里还会出现这几个（guard.py 全量核对过）：
  'data.core': '核心数据齐',
  'data.aggressive': '激进盘数据齐',
  limit_up_pool: '涨停池状态',
  round_trip: '往返成本',
  cost: '费用',
  source: '数据源',
  strategy_of: '策略归属',
  // observations 用的叫法与 guard 不同（single_name_cap），两张表都收，避免漏翻
  single_name_cap: '单票超上限',
};

/** 观察层的四类事实。 */
const OBS = {
  stop_breach: '破止损',
  single_name_cap: '单票超上限',
  industry_cap: '行业超上限',
  target_drift: '目标偏离',
  constraint: '约束',
  deviation: '偏离',
};

/** 趋势与波动状态。 */
const TREND = { up: '上行', down: '下行', flat: '走平', sideways: '震荡' };
const VOL = { calm: '平静', normal: '正常', elevated: '偏高', stressed: '应激', panic: '恐慌' };

/** 三池闸门的三个检查项。 */
const GATE = {
  tone_ok: '语气',
  break_rate_ok: '炸板率',
  height_ok: '高度',
  breadth_ok: '宽度',
};

/** 待办类型。 */
const DUTY = {
  memo_due: '备忘录到期',
  regime_note: '状态提示',
  stop_watch: '止损观察',
  data_gap: '数据缺口',
  review_due: '待复盘',
};

/**
 * 决策日志的动作（/decisions 的 kind）。
 * 逐项对齐 GET /meta/vocabulary 的 decisions.kind（27 项）——值域由后端单点声明，
 * 不再"看到一条英文现编一条中文"；拼错一个字母导致整条失效的事故就出在这张表上。
 */
const DECISION = {
  propose: '提案',
  reject: '否决',
  risk_veto: '风控否决',
  arm: '已批准待成交',
  expire: '过期',
  orphan_reservation_release: '孤儿预留释放',
  corporate_action_mismatch: '公司行动不符',
  cb_maturity_deferred: '转债到期延后',
  corporate_action: '公司行动',
  live_refused: '实时拒单',
  queue: '入队',
  recover_fill: '补记成交',
  cancel: '撤销',
  manual_reject: '人工否决',
  intent_cancel: '意图撤单',
  execution_reject: '执行拒单',
  fill: '成交',
  proposal_blocked: '提案被挡',
  hypothesis: '假设',
  hypothesis_result: '假设结论',
  kill_switch: '总开关',
  safe_mode: '安全模式',
  core_execute: '核心盘执行',
  memo: '备忘',
  persona_refused: '人格切换被拒',
  author_backfill: '作者回填',
  order_reconstructed: '订单重建',
  // 词表未登记的旧值：留着是给历史账本兜底，不参与新写入。
  approve: '批准',
  journal: '日志',
};

/** 复盘/日志的类型。 */
const JOURNAL = {
  review: '复盘',
  plan: '计划',
  note: '笔记',
  trade: '成交',
  memo: '备忘',
};

/** 盘口封板状态。 */
const LIMIT = {
  one_word: '一字板',
  sealed: '封板',
  opened: '开过板',
  none: '无',
};

/** 决策管线的九道门。键与内核页的顺序一致。 */
const PIPE = {
  watch: '巡检',
  context: '上下文',
  observations: '观察层',
  duty: '待办',
  persona: '人格',
  propose: '提案',
  approve: '审批',
  fill: '成交',
  settle: '结算',
};

/** 错误码：正文用中文，原始码放进 title —— 排查时要能对上服务端日志。 */
const CODE = {
  PAGE_FAILED: '页面渲染失败',
  POOLS_MISSING: '三池还没入库',
  CHECK_FORM: '表单没填完',
  REFRESH_REFUSED: '已拦下这次刷新',
  COMMIT_REFUSED: '写入被拒',
  BRIDGE: '窗口桥出错',
  EVENTS: '动作流水读不到',
  PREVIEW: '预检失败',
  NETWORK: '连不上服务',
  UNAUTHORIZED: '口令不对',
  STATE_CONFLICT: '账本刚被别人改过',
  STORE_BUSY: '账本正被占用',
  EXECUTION_EXISTS: '这笔已经记过账',
  OPEN_VIOLATION: '有持仓破线，不能加仓',
  RATE_LIMITED: '同一错误重试太多次',
  INTERNAL: '服务端错误',
  NO_BARS: '这只票没有行情',
  NO_SYMBOL: '没有这个代码',
  BAD_SECTION: '档案分段名不认识',
  AMBIGUOUS_DAY: '两个日期不一致',
};

const SESSION = {
  open: '盘中',
  closed: '已收盘',
  pre: '盘前',
  lunch: '午间',
  weekend: '周末',
};

function pick(map, key, fallback = null) {
  if (key === null || key === undefined || key === '') return fallback ?? '—';
  return map[key] ?? (fallback ?? String(key));
}

export const zh = {
  status: (v) => pick(STATUS, v),
  side: (v) => pick(SIDE, v),
  sleeve: (v) => pick(SLEEVE, v),
  regime: (v) => pick(REGIME, v),
  readiness: (v) => pick(READINESS, v),
  vendor: (v) => pick(VENDOR, v),
  basis: (v) => (v ? BASIS[v] ?? String(v) : '—'),
  board: (v) => pick(BOARD, v),
  decay: (v) => pick(DECAY, v),
  strategy: (v) => pick(STRATEGY, v),
  notScored: (v) => pick(NOT_SCORED, v),
  decayReason: (v) => pick(DECAY_REASON, v),
  datasetKind: (v) => pick(DATASET_KIND, v),
  check: (v) => pick(CHECK, v),
  pipe: (v) => pick(PIPE, v),
  obs: (v) => pick(OBS, v),
  duty: (v) => pick(DUTY, v),
  gate: (v) => pick(GATE, v),
  trend: (v) => pick(TREND, v),
  vol: (v) => pick(VOL, v),
  journal: (v) => pick(JOURNAL, v),
  decision: (v) => pick(DECISION, v),
  limit: (v) => pick(LIMIT, v),
  code: (v) => pick(CODE, v, null) || (v ? String(v) : '—'),
  session: (v) => pick(SESSION, v),
  /**
   * 只用于 guard 的 ``failures`` 字符串（`"check_name: detail"`）：翻前缀、
   * 正文原样，避免改写丢定位。检查表里的 Check 对象走另一条路 —— 后端给了
   * ``detail_zh`` 与 ``structured``，由 ui.js / decide.js 直接消费。
   */
  reason(str) {
    const raw = String(str ?? '');
    const at = raw.indexOf(':');
    if (at <= 0) return raw;
    const head = raw.slice(0, at).trim();
    const tail = raw.slice(at + 1).trim();
    const zhHead = CHECK[head];
    if (!zhHead) return raw;
    return `${zhHead}：${tail}`;
  },
};
