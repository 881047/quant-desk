// 内核解剖：这台机器由什么组成、它今天看到了什么、它凭什么拒绝你。
// 这一页不交易，只回答"我能不能相信上面那些数字"。

import { api } from '../api.js';
import {
  h, panel, chip, table, slot, kv, meterRow,
} from '../ui.js';
import { money, ratio, cny, ymd, num, esc } from '../fmt.js';
import { zh } from '../zh.js';
import { mountShortAlpha } from './kernel-short-alpha.js';

export async function mount(ctx) {
  const root = h('div', { class: 'stack' });

  const top = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  top.append(
    panel({ title: '三层契约', note: '事实 / 信息 / 执行', body: h('div', { id: 'layers' }) }),
    panel({ title: '决策管线', note: '一次判断经过的九道门', body: h('div', { id: 'pipeline' }) }),
  );
  root.append(top);
  mountShortAlpha(root);


  root.append(panel({
    title: '风控矩阵',
    note: '25 项 guard 检查 · 账户级实时，订单级待提案时判定',
    body: h('div', { id: 'guard' }),
    footer: '这一表是**检查清单与阈值**，不是"当前全部通过"的结论：订单级检查只有在一张具体票据上才有答案，界面不替 guard 预演。',
  }));

  const bottom = h('div', { class: 'grid g2', style: { alignItems: 'start' } });
  // 数据集个数写死过一次（"44 个"），加到 46 之后标题还在说 44。个数由 /data/status
  // 自己报，标题只留"信任分层"这四个字。
  const lineageNote = h('span', {}, '信任分层');
  bottom.append(
    panel({ title: '数据血缘', note: lineageNote, body: h('div', { id: 'lineage' }), footer: h('div', { id: 'freshness', style: { display: 'flex', height: '12px', gap: '2px' } }) }),
    panel({ title: '端点遥测', note: '/usage 近 7 天', body: h('div', { id: 'usage' }) }),
  );
  root.append(bottom);

  root.append(panel({ title: '系统体检', note: '/health', body: h('div', { id: 'health' }) }));

  slot(root.querySelector('#layers'), () => layersBlock(), { lines: 6 });
  slot(root.querySelector('#pipeline'), () => pipelineBlock(), { lines: 8 });
  slot(root.querySelector('#guard'), () => guardBlock(), { lines: 8 });
  slot(root.querySelector('#lineage'), () => lineageBlock(lineageNote), { lines: 8 });
  slot(root.querySelector('#freshness'), () => freshnessBar(), { lines: 1 });
  slot(root.querySelector('#usage'), () => usageBlock(), { lines: 6 });
  slot(root.querySelector('#health'), () => healthBlock(), { lines: 3 });

  return root;
}

export function unmount() {}

// ------------------------------------------------------------------ 三层
function layersBlock() {
  return Promise.all([
    api('/portfolio', { ttl: 15000, silent: true }).catch(() => null),
    api('/universe?limit=1', { ttl: 60000, silent: true }).catch(() => null),
    api('/observations', { ttl: 15000, silent: true }).catch(() => null),
    api('/risk', { ttl: 15000, silent: true }).catch(() => null),
    api('/knowledge/cards', { ttl: 120000, silent: true }).catch(() => null),
  ]).then(([p, u, o, r, k]) => {
    const topics = (k?.layers || []).reduce((s, l) => s + (l.topics?.length || 0), 0);
    const col = (title, sub, rows) => h('div', { class: 'stack', style: { gap: '6px', borderLeft: '2px solid var(--line-strong)', paddingLeft: '9px' } },
      h('div', { class: 'row' }, h('span', { class: 'strong' }, title), h('div', { class: 'spacer' }), h('span', { class: 'micro faint' }, sub)),
      kv(rows));
    return h('div', { class: 'grid g3', style: { gap: '14px' } },
      col('事实', '行情 · 账本 · 账户', [
        ['宇宙', u ? `${num(u.universe_size)} 只` : '—'],
        ['净值', p ? money(p.equity) : '—'],
        ['持仓', p ? `${p.position_count} 只` : '—'],
        ['现金', p ? money(p.cash_free) : '—'],
      ]),
      col('信息', '类比 · 知识卡 · 观察层', [
        ['观察条目', o?.counts ? `${(o.counts.constraints || 0) + (o.counts.deviations || 0)} 条` : '—'],
        ['知识主题', k ? `${topics} 个` : '—'],
        ['市场状态', o?.budget?.regime ? zh.regime(o.budget.regime) : '—'],
        ['可分配', o?.budget?.allocation?.llm === undefined ? '—' : ratio(o.budget.allocation.llm, 0)],
      ]),
      col('执行', '决策 · 风控 · 撮合', [
        ['风控检查', `${GUARD_CHECKS.length} 项`],
        ['动作上限', r?.limits ? `${r.limits.max_actions_per_day} 笔/日` : '—'],
        ['成交口径', r?.limits?.fill_mode === 'intraday' ? '盘中' : '次日开盘'],
        ['T+1', '卖出按可用数量'],
      ]));
  });
}

// ------------------------------------------------------------------ 管线
/** 管线右侧那一个词：说清楚"这一步发生了什么"，不是打一个英文标记。 */
function stateLabel(state) {
  return ({
    skip: '无需动作',
    ok: '通过',
    warn: '待处理',
    blocking: '阻断',
    fail: '不通过',
    done: '已完成',
    NON_TRADING_DAY: '非交易日',
    SESSION_OPEN: '盘中',
    READY: '就绪',
  })[state] || (state || '');
}
function pipelineBlock() {
  return Promise.all([
    api('/persona', { ttl: 15000, silent: true }).catch(() => null),
    api('/dashboard.json?slim=1', { ttl: 15000, silent: true }).catch(() => null),
    api('/watch', { ttl: 15000, silent: true }).catch(() => null),
    api('/context_pack', { ttl: 30000, silent: true }).catch(() => null),
    api('/tickets?limit=50', { ttl: 15000, silent: true }).catch(() => null),
    api('/duty', { ttl: 15000, silent: true }).catch(() => null),
    api('/observations', { ttl: 30000, silent: true }).catch(() => null),
  ]).then(([p, d, w, c, t, du, ob]) => {
    // 服务启动时后台预热了 scorecard，/persona 没有冷调用（实测 ~28 ms），
    // 于是改回直连、读完整的 current/today。不再从 dashboard 的精简人格字段
    // 绕行，也不写 `p || d.persona` 那种静默双路径。
    const pe = p || null;
    const tickets = t?.tickets || [];
    const steps = [
      ['watch', w ? (w.findings?.length ? `${w.findings.length} 条 finding` : '无发现（空仓是合法答案）') : '—', w?.verdict],
      ['context', c ? `数据可用 ${c.session?.data_ready ? '是' : '否'} · ${zh.readiness(c.session?.readiness)}` : '—', c?.session?.readiness],
      ['observations', ob?.counts ? `${(ob.counts.constraints || 0) + (ob.counts.deviations || 0)} 条约束/偏离` : '—', (ob?.counts?.constraints || 0) ? 'warn' : ''],
      ['duty', du ? `${du.count} 项 · 阻断 ${du.blocking}` : '—', du?.blocking ? 'blocking' : ''],
      ['persona', pe ? `${pe.current?.name || '—'} · ${pe.today?.can_place_orders ? '可下单' : '不可下单'}` : '—', pe?.today?.can_place_orders ? 'ok' : 'warn'],
      ['propose', `${tickets.filter((x) => (x.status || '') === 'proposed').length} 张待批`, ''],
      ['approve', `${tickets.filter((x) => (x.status || '') === 'armed').length} 张已批未成交`, ''],
      ['fill', d ? `${d.summary?.fills ?? 0} 笔成交` : '—', ''],
    ];
    return h('div', { class: 'pipe' }, steps.map(([name, note, state], i) => h('div', {
      class: `pipe-step ${state === 'ok' ? 'done' : state ? 'on' : ''}`,
    },
      h('div', { class: 'pipe-idx' }, String(i + 1)),
      h('div', {}, h('div', { class: 'pipe-name' }, zh.pipe(name)), h('div', { class: 'pipe-note' }, note)),
      h('div', { class: 'pipe-meta' }, stateLabel(state)))));
  });
}

// ------------------------------------------------------------------ 风控矩阵
const GUARD_CHECKS = [
  ['kill_switch', '账户级', '开关关闭'],
  ['safe_mode', '账户级', '买入未被禁'],
  ['daily_actions', '账户级', '当日成交 < 4'],
  ['max_positions', '账户级', '持仓 < 3'],
  ['min_cash', '账户级', '交易后现金 ≥ 20%'],
  ['daily_loss', '账户级', '日亏 > −6%'],
  ['reduce_only', '账户级', '日亏 > −10%'],
  ['drawdown', '账户级', '回撤 > −8% / −12%'],
  ['stop_gate', '账户级', '无破线持仓'],
  ['equity', '账户级', '净值 > 0'],
  ['bar', '订单级', '有该日行情'],
  ['st', '订单级', '非 ST（卖出放行）'],
  ['suspended', '订单级', '未停牌'],
  ['board', '订单级', '北交所不开新仓'],
  ['cb_maturity', '订单级', '距到期 > 阈值'],
  ['price_band', '订单级', '价格在涨跌停内'],
  ['cash', '订单级', '含排队买单后够付'],
  ['single_cap', '订单级', '单票 ≤ 30% / 50%'],
  ['sleeve_cap', '订单级', '单盘仓位 ≤ 70% / 60%'],
  ['industry_cap', '订单级', '行业 ≤ 35%'],
  ['strategy_cap', '订单级', '策略权重上限'],
  ['cost_bps', '订单级', '往返费用（仅报告）'],
  ['liquidity', '订单级', '≤ 1% ADV20'],
  ['t_plus_one', '卖出', '卖出 ≤ 可用'],
  ['has_position', '卖出', '有持仓'],
];

function guardBlock() {
  return Promise.all([
    api('/risk', { ttl: 15000, silent: true }).catch(() => null),
    api('/portfolio', { ttl: 15000, silent: true }).catch(() => null),
    api('/risk/stops', { ttl: 15000, silent: true }).catch(() => null),
  ]).then(([r, p, s]) => {
    const live = {};
    if (p && r) {
      const eq = Number(p.equity);
      const cashW = Number.isFinite(eq) && eq > 0 ? Number(p.cash_free) / eq : null;
      live.kill_switch = p.kill_switch === true ? 'fail' : 'pass';
      live.safe_mode = p.safe_mode === true ? 'unknown' : 'pass';
      live.daily_actions = p.fills < r.limits.max_actions_per_day ? 'pass' : 'fail';
      live.max_positions = p.position_count < r.limits.max_positions ? 'pass' : 'fail';
      live.min_cash = cashW === null ? 'none' : cashW >= r.limits.min_cash_weight ? 'pass' : 'fail';
      const day = Number(p.daily_pnl_pct);
      live.daily_loss = Number.isFinite(day) ? (day > r.limits.daily_loss_no_new ? 'pass' : 'fail') : 'none';
      live.reduce_only = Number.isFinite(day) ? (day > r.limits.daily_loss_reduce_only ? 'pass' : 'fail') : 'none';
      const dd = Number(p.drawdown);
      live.drawdown = Number.isFinite(dd) ? (dd > r.limits.drawdown_half ? 'pass' : dd > r.limits.drawdown_stop ? 'unknown' : 'fail') : 'none';
      live.equity = Number.isFinite(eq) && eq > 0 ? 'pass' : 'fail';
    }
    if (s) live.stop_gate = (s.breached || []).length ? 'fail' : 'pass';

    const cells = GUARD_CHECKS.map(([name, scope, rule]) => {
      const state = live[name] || 'na';
      const title = `${zh.check(name)}（${name}）· ${scope} · ${rule} · ${state === 'na' ? '订单级检查，提案时由风控判定' : state === 'pass' ? '当前通过' : state === 'fail' ? '当前不通过' : '无法判定'}`;
      // 判定只用方：空心=过，实心=不过，订单级=空位（不画方，写一行原因）。
      // 底色一律中性 —— 底色若表示通过，25 格里 23 格又会糊成一片。
      // 状态必须落成 class：.guard-cell 的"通过态不描边"是按 .pass 写的，
      // 只写 na 的话 pass/fail/unknown 三态在 CSS 里全走默认分支。
      return h('div', { class: `guard-cell ${state}`, title },
        h('div', { class: 'gc-name', title: name }, zh.check(name)),
        h('div', { class: 'gc-val', style: { display: 'flex', alignItems: 'center', gap: '4px' } },
          state === 'na' ? h('span', { class: 'micro faint' }, '提案时判定')
            : state === 'pass' ? h('span', { class: 'verdict pass', 'aria-hidden': 'true' })
              : state === 'fail' ? h('span', { class: 'verdict fail', 'aria-hidden': 'true' })
                : null,
          state === 'pass' ? h('span', { class: 'micro faint' }, '过') : state === 'fail' ? h('b', {}, '不过') : ''));
    });

    const passed = Object.values(live).filter((v) => v === 'pass').length;
    const failed = Object.values(live).filter((v) => v === 'fail').length;
    const unknown = Object.values(live).filter((v) => v === 'unknown').length;
    const awaiting = GUARD_CHECKS.length - passed - failed - unknown;

    return h('div', { class: 'stack', style: { gap: '8px' } },
      h('div', { class: 'row small num' },
        h('span', { class: 'muted' }, `${GUARD_CHECKS.length} 项 · `),
        h('span', { class: 'up' }, `过 ${passed}`),
        h('span', { class: 'faint' }, ' · '),
        h('span', { class: failed ? 'up' : 'faint' }, `不过 ${failed}`),
        h('span', { class: 'faint' }, ' · '),
        h('span', { class: 'faint' }, `未运行 ${unknown}`),
        h('span', { class: 'faint' }, ' · '),
        h('span', { class: 'faint' }, `订单级 ${awaiting}`),
        h('div', { class: 'spacer' }),
        h('span', { class: 'micro faint' }, '账户级实时 · 订单级待提案')),
      h('div', { class: 'guard-matrix' }, cells),
      h('div', { class: 'guard-legend' },
        h('span', {}, h('span', { class: 'verdict pass', style: { marginRight: '4px' } }), '当前通过'),
        h('span', {}, h('span', { class: 'verdict fail', style: { marginRight: '4px' } }), '当前不通过'),
        h('span', {}, '空位 = 订单级，提案时判定')));
  });
}

// ------------------------------------------------------------------ 血缘
function lineageBlock(noteEl) {
  return api('/data/status', { ttl: 120000 }).then((d) => {
    // 后端给每条数据集标了 kind / coverage_boundary：只有逐日面板的 cover_end
    // 才是覆盖边界，按它倒序排才有意义；日历/参考/快照排到未来是业务语义，
    // 这类按名字排并放在后面，不再被误读成"抓到了未来数据"。
    const rows = (d.datasets || []).slice().sort((a, b) => {
      const ka = a.coverage_boundary ? 0 : 1;
      const kb = b.coverage_boundary ? 0 : 1;
      if (ka !== kb) return ka - kb;
      if (ka === 0) return String(b.cover_end || '').localeCompare(String(a.cover_end || ''));
      return String(a.dataset).localeCompare(String(b.dataset));
    });
    if (noteEl) noteEl.textContent = `${rows.length} 个数据集 · 信任分层`;

    const codes = (raw) => String(raw || '').split(',').filter(Boolean);

    return table({
      cols: [
        { key: 'dataset', label: '数据集', render: (r) => `<span class="sym">${esc(r.dataset)}</span>` },
        { key: 'kind', label: '类型', render: (r) => `<span class="muted">${esc(zh.datasetKind(r.kind))}</span>` },
        { key: 'rows', label: '行数', cls: 'r', render: (r) => cny(r.rows, { unit: false }) },
        { key: 'cover_end', label: '截至', cls: 'r', render: (r) => ymd(r.cover_end) },
        { key: 'trust_tier', label: '信任', cls: 'r', render: (r) => chip(r.trust_tier, r.trust_tier === 'T0' ? 'ok' : 'off') },
        // 以前两列叫「告警分区」和「错误」，并排放着两个同量纲不同的数：前者是
        // 分区个数，后者是问题条数——adj_factor 两边都是 18，读起来像"18 个分区
        // 各有 18 个错"，其实是一件事被数了两遍。现在一列写分区构成，一列写问题
        // 条数，各自把规则名放进 title。
        {
          key: 'partitions',
          label: '分区',
          cls: 'r',
          title: '干净时写「N ok」，有告警写「N 警」，有隔离再补「N 隔」；悬停看规则名',
          render: (r) => {
            const n = Number(r.partitions || 0);
            const w = Number(r.warn_partitions || 0);
            const q = Number(r.quarantine_partitions || 0);
            const warnCodes = codes(r.warn_codes);
            const tip = `共 ${n} 个分区：${Math.max(0, n - w - q)} ok · ${w} warn · ${q} 隔离`
              + (warnCodes.length ? `\nwarn 的规则：${warnCodes.join('、')}` : '');
            const main = w
              ? `<span style="color:var(--al-stale)">${w} 警</span>`
              : `<span class="muted">${Math.max(0, n - q)} ok</span>`;
            const quar = q ? ` · <span style="color:var(--al-fault)">${q} 隔</span>` : '';
            return `<span title="${esc(tip)}">${main}${quar}</span>`;
          },
        },
        {
          key: 'error_issues',
          label: '错误问题',
          cls: 'r',
          title: 'severity=error 的问题**条数**（不是坏行数、也不是分区数）；悬停看是哪条规则',
          render: (r) => {
            const n = Number(r.error_issues || 0);
            if (!n) return '0';
            const names = codes(r.error_codes);
            const tip = `${n} 条 error 级问题：${names.join('、') || '（旧报告没有规则名）'}`
              + '\n每条命中的行数不一样，行数见 /data/quality 的 issues[].count';
            return `<span style="color:var(--al-fault)" title="${esc(tip)}">${n}</span>`;
          },
        },
      ],
      rows,
      emptyText: '没有数据集记录',
    });
  });
}

function freshnessBar() {
  return api('/data/status', { ttl: 120000 }).then((d) => {
    const sets = d.datasets || [];
    const today = new Date();
    return h('div', { style: { display: 'flex', gap: '2px', height: '12px', alignItems: 'stretch' } },
      sets.map((ds) => {
        const end = String(ds.cover_end || '');
        // 日历/参考/快照的 cover_end 不是覆盖边界：不着色，避免"2027 年"被画成极新。
        if (ds.coverage_boundary === false) {
          return h('div', {
            class: 'fresh-seg',
            style: { flex: '1' },
            'data-tip': `${ds.dataset} · ${zh.datasetKind(ds.kind)} · 覆盖日不作新鲜度判据`,
          });
        }
        let lag = 99;
        if (/^\d{8}$/.test(end)) {
          lag = Math.round((today - new Date(+end.slice(0, 4), +end.slice(4, 6) - 1, +end.slice(6, 8))) / 86400000);
        }
        const kind = lag <= 1 ? 'fresh' : lag <= 4 ? 'warm' : 'cold';
        return h('div', {
          class: `fresh-seg ${kind}`,
          style: { flex: '1' },
          'data-tip': `${ds.dataset} · ${ymd(end)} · ${ds.trust_tier}`,
        });
      }));
  });
}

// ------------------------------------------------------------------ 遥测
function usageBlock() {
  return api('/usage', { ttl: 60000 }).then((d) => {
    const by = d.by_endpoint || {};
    const rows = Object.entries(by).sort((a, b) => b[1] - a[1]).slice(0, 12);
    const total = Object.values(by).reduce((s, v) => s + v, 0);
    const max = rows.length ? rows[0][1] : 1;
    return h('div', { class: 'stack', style: { gap: '8px' } },
      h('div', { class: 'meter' }, rows.map(([k, v]) => meterRow({ label: k, value: v, max, text: num(v) }))),
      h('div', { class: 'row micro faint' },
        h('span', {}, `近 ${d.days} 天共 ${num(total)} 次调用`),
        h('div', { class: 'spacer' }),
        h('span', {}, `since ${ymd(d.since)}`)));
  });
}

function healthBlock() {
  return api('/health', { ttl: 10000 }).then((hd) => {
    const cfg = hd.config || {};
    return h('div', { class: 'grid g4', style: { gap: '10px' } },
      kv([
        ['进程', chip(hd.ok ? '正常' : '中断', hd.ok ? 'ok' : 'bad')],
        ['mode', hd.mode || '—'],
        ['commit', hd.build?.commit?.slice(0, 7) || '—'],
        ['dirty', hd.build?.dirty ? '是' : '否'],
      ]),
      kv([
        ['参数项', num(cfg.params)],
        ['缺项', (cfg.missing || []).length ? chip(`${cfg.missing.length} 项缺`, 'warn') : chip('0', 'ok')],
        ['env 覆盖', num((cfg.overridden || []).length)],
        ['旧变量', (cfg.legacy_env_ignored || []).length ? chip(String(cfg.legacy_env_ignored.length), 'warn') : '0'],
      ]),
      kv([
        ['默认口令', hd.security?.token_is_default ? chip('是（危险）', 'bad') : chip('否', 'ok')],
        ['熔断表', `${hd.breaker?.keys ?? '—'} / ${hd.breaker?.max_keys ?? '—'}`],
        ['熔断阈值', `${hd.breaker?.limit ?? '—'} 次 / ${hd.breaker?.window_seconds ?? '—'}s`],
      ]),
      kv([
        ['缓存', hd.cache?.error ? chip(hd.cache.error, 'bad') : 'ok'],
        ['日线快照', hd.market?.last_bar ? ymd(hd.market.last_bar) : '—'],
        ['usage 写入', num(hd.usage?.written ?? 0)],
        ['usage 失败', hd.usage?.failed ? chip(String(hd.usage.failed), 'warn') : '0'],
      ]));
  });
}
