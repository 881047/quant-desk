"""接口词表：唯一一处声明"这些字符串到底有哪些值、数字是什么单位"。

前端 ``web/assets/js/zh.js`` 的 ``DECISION`` 映射表过去追着数据加条目，
2026-11-07 还把 ``proposal_blocked`` 拼成过 ``propose_blocked``——差一个字母，
整张映射失效，而后端毫无察觉（docs/14 C1）。这里把值域收成常量，
``GET /meta/vocabulary`` 一次交给消费方；写入点是**软校验**：遇到没登记的值
不拦交易（阻断一条已写了一半的决策比拼写错误危险得多），只记进
``undeclared``，让下一次巡检能看见。

模块刻意不 import 任何 trading 子模块：``state.py`` 与 ``decisions.py`` 互相
依赖，词表放进任何一方都会造成循环引用。
"""

from __future__ import annotations

# ---------------------------------------------------------------- decisions
#: ``decisions.kind``。含三类来源：源码里的字面值、``state.commit_ticket_state``
#: 内部写入的值、以及账本里已经存在的历史/回填值。少一个都不会导致写入失败，
#: 但 ``tests/trading/test_backend_debt_contracts.py`` 会扫描源码字面值并要求
#: 它们全部在这里登记——新增一个 kind 却忘了改词表，测试会先不通过。
DECISION_KINDS = frozenset({
    # service/trading/decisions.py
    "propose", "reject", "risk_veto", "arm", "expire",
    # service/trading/book.py
    "orphan_reservation_release", "corporate_action_mismatch",
    "corporate_action_catchup_pending",
    "cb_maturity_deferred", "corporate_action", "live_refused",
    "queue", "recover_fill", "cancel", "manual_reject", "intent_cancel",
    "execution_reject",
    # service/trading/state.py（票据状态变更与成交）
    "fill",
    # service/api/trading.py
    "proposal_blocked", "hypothesis", "hypothesis_result", "kill_switch",
    "safe_mode", "core_execute", "memo", "persona_refused",
    # data/paper.sqlite 里的历史与回填值（2026-11-08 实测 distinct）
    "author_backfill", "order_reconstructed",
})

#: ``decisions.actor``。
DECISION_ACTORS = frozenset({"agent", "guard", "system", "human", "user"})

# ---------------------------------------------------------------- guard
#: ``guard.Check.name`` 的全部取值。前端 ``zh.js`` 的 CHECK 表只翻这些名字，
#: 新增检查项不登记就永远显示英文。
GUARD_CHECKS = frozenset({
    "data.core", "data.aggressive", "kill_switch", "safe_mode", "daily_actions",
    "bar", "st", "suspended", "board", "cb_maturity", "price_band",
    "stop_gate", "equity", "cash", "max_positions", "single_cap", "sleeve_cap",
    "industry_cap", "strategy_cap", "cost_bps", "liquidity", "min_cash",
    "t_plus_one", "has_position", "daily_loss", "reduce_only", "drawdown",
})

# ---------------------------------------------------------------- decay
DECAY_REASONS = frozenset({"sample_below_min", "no_baseline", "none"})
DECAY_REASON_NOTES = {
    "sample_below_min": "已平仓笔数不足，note_meta.need / have 给出门槛与现值",
    "no_baseline": "证据里没有 mean/sigma 或 cagr/sharpe，没法判衰减",
    "none": "正常判定，没有说明性文案",
}

# ---------------------------------------------------------------- profile
#: ``profile.identity.delist_status``。null 的 delist_date 不再独自承担语义。
DELIST_STATUSES = frozenset({"listed", "delisted", "scheduled", "unknown"})

# ---------------------------------------------------------------- units
#: 数值字段的量纲。命名约定：百分数一律 ``_pct`` 结尾且值已 ×100；
#: 比例是 0..1 的分数。``pct_of_equity`` 是历史命名，值同样是百分数。
POOL_UNITS = {
    "seal_ratio": "fraction (0..1)",
    "seal_ratio_pct": "percent (0..100)",
    "pct_of_equity": "percent (0..100)",
    "pct_chg": "percent (0..100)",
    "close": "cny", "lot_cost_cny": "cny", "seal_amount": "cny",
    "amount": "cny",
    # 2026-11-08 精确路径核验删掉了 float_mv：它并不在 /market/pools 的
    # 响应里，只在早先手写的 units 里出现过 —— 声明一个不存在的字段，
    # 比不声明更误导消费方。
    "seal_window_no": "ordinal (1..4)",
}

UNDECLARED_KINDS: set[str] = set()
UNDECLARED_ACTORS: set[str] = set()


def check_decision(kind: str, actor: str) -> None:
    """写入前的软校验：只登记，不拒绝。

    ``decisions`` 表是审计记录，任何一个新代码路径多写一种 kind 都不该让
    一笔交易挂掉。真正的约束在测试里（源码字面值必须登记）和这里的可观测性
    （``/meta/vocabulary`` 会把没登记的值列出来）。
    """
    if str(kind) not in DECISION_KINDS:
        UNDECLARED_KINDS.add(str(kind))
    if str(actor) not in DECISION_ACTORS:
        UNDECLARED_ACTORS.add(str(actor))


#: 跨端点的数值量纲 —— 本项目**唯一**一处手写口径。
#:
#: 各端点响应的 ``units`` / ``units_note``、web 的格式化入口、plugin 的提示词
#: 全部由这张表经 ``scripts/export_units.py`` 确定性生成；``--check`` 让生成物
#: 过期时测试直接红，而不是等下一次 100 倍的数字出现才发现（2026-11-08 复查）。
#:
#: 键本身就是**从响应根出发的完整路径**（数组用 ``[]`` 标一层），顶层字段
#: 直接写裸名。消费方按这个路径机械定位，不需要第二张"容器表"，也不递归猜。

UNIT_CONVENTIONS = {
    "rule": (
        "字段量纲以接口响应的 units / GET /meta/vocabulary 的 unit_conventions 为准；"
        "以 _pct 结尾的字段原则上是百分数（值已 ×100），但 naming_exceptions 里的"
        "例外是小数，比例字段是 0..1 的 fraction —— 不要按名字猜。"
    ),
    "by_endpoint": {
        "/market/pools": {f"pools.limit_up[].{k}": v for k, v in POOL_UNITS.items()},
        "/market/sector": {
            "industries[].pct_chg": "percent (0..100)",
            "industries[].pct_chg_eq": "percent (0..100)",
            "industries[].amount_cny": "cny",
            "industries[].main_net_inflow_cny": "cny",
            "industries[].members": "count", "industries[].traded": "count",
        },
        "/market/pools.sentiment": {"sentiment.break_rate": "fraction (0..1)"},
        "/facts": {
            "breadth.equal_weight_return": "fraction (0..1)",
            "breadth.median_return": "fraction (0..1)",
            "breadth.advancers": "count", "breadth.decliners": "count",
            "breadth.flat": "count",
            "account.day_pnl_pct": "fraction (0..1)",
        },
        "/regime": {
            "breadth_above_ma20": "fraction (0..1)",
            "vol_20d": "fraction (0..1)",
            "vol_percentile_1y": "fraction (0..1)",
            "equal_weight_index": "index level (base 1.0)",
        },
        "/risk": {"drawdown": "fraction (0..1)",
                  "daily_pnl_pct": "fraction (0..1)"},
        "/risk/exposure": {"invested_weight": "fraction (0..1)",
                           "cash_weight": "fraction (0..1)"},
        "/portfolio": {"daily_pnl_pct": "fraction (0..1)"},
        "/context_pack": {"account.day_pnl_pct": "fraction (0..1)"},
        "/discover": {
            "market.ew_return_1d": "fraction (0..1)",
            "market.ew_return_5d": "fraction (0..1)",
            "market.ew_return_20d": "fraction (0..1)",
            "market.realised_vol_20d": "fraction (0..1)",
            "market.vol_percentile_1y": "fraction (0..1)",
            "market.breadth_above_ma20": "fraction (0..1)",
            "market.breadth_above_ma60": "fraction (0..1)",
        },
        "/research/screen": {
            "candidates[].pct_chg": "percent (0..100)",
            "candidates[].mom20": "percent (0..100)",
            "candidates[].close": "cny",
            "candidates[].adv20_cny": "cny",
        },
    },
    "naming_exceptions": [
        {
            "field": "daily_pnl_pct",
            "endpoints": ["/risk", "/portfolio"],
            "actual": "fraction (0..1)",
            "why": ("内核 engine.py 里是 (equity - day_start_equity)/day_start_equity，"
                    "名字带 _pct 但值是小数；前端 pct() 会 ×100。改名会动到内核、"
                    "账本与既有消费者，登记为例外而不是重命名。"),
        },
        {
            "field": "day_pnl_pct",
            "endpoints": ["/facts", "/context_pack"],
            "actual": "fraction (0..1)",
            "why": "daily_pnl_pct 在 facts / context_pack 里的别名，同样不是百分数。",
        },
    ],
    "note": (
        "改后端字段量纲前先改这张表，再跑 scripts/export_units.py --write，"
        "让 web / plugin 的生成物一起更新；--check 会在两者不同步时失败。"
    ),
}


def units_for(endpoint: str) -> dict[str, str]:
    """端点响应顶层 ``units``：裸字段名 -> 量纲。

    ``/market/pools`` 额外带上 sentiment 的 ``break_rate``：同一个响应里
    两种量纲，只声明表格行字段会让读者以为炸板率也是百分数。
    """
    by = UNIT_CONVENTIONS["by_endpoint"]
    fields = dict(by.get(endpoint, {}))
    if endpoint == "/market/pools":
        fields.update(by["/market/pools.sentiment"])
    return {key.rsplit(".", 1)[-1]: scale for key, scale in fields.items()}


def units_text() -> str:
    """给模型/人读的量纲段，由词表生成 —— 插件提示词直接插值，不再手写。

    生成而非手抄，是因为 ``plugin/lib/index.js`` 原先那段"facts 是小数、
    profile 里 _pct 是百分数"与后来登记的 ``daily_pnl_pct`` 例外已经互相
    矛盾；两边各自改字，迟早再分叉一次。
    """
    by = UNIT_CONVENTIONS["by_endpoint"]
    lines = [
        ("- **数值量纲以接口响应的 `units` / `GET /meta/vocabulary.unit_conventions` "
         "为准，不要按字段名猜**："),
        f"  {UNIT_CONVENTIONS['rule']}",
    ]
    for endpoint, fields in sorted(by.items()):
        if endpoint == "/market/pools.sentiment":
            continue
        leaf = lambda k: k.rsplit(".", 1)[-1]
        frac = [leaf(k) for k, v in sorted(fields.items()) if v.startswith("fraction")]
        percent = [leaf(k) for k, v in sorted(fields.items()) if v.startswith("percent")]
        bits = []
        if frac:
            bits.append("小数 " + ", ".join(frac))
        if percent:
            bits.append("百分数 " + ", ".join(percent))
        if bits:
            lines.append(f"  {endpoint}：{'；'.join(bits)}。")
    for ex in UNIT_CONVENTIONS["naming_exceptions"]:
        lines.append(
            f"  例外 `{ex['field']}`（{', '.join(ex['endpoints'])}）名字带 _pct "
            f"但值是 {ex['actual']}，web 的 pct() 会 ×100。")
    lines.append(
        "  把小数写成百分数（或反过来）是 100 倍的错；web 的 fmtByUnits() "
        "由 units 自动挑格式化函数，不要手写换算。")
    return "\n".join(lines)


#: 语义说明文案。也收在这里：散在各模块里的中文句子描述的是同一套口径，
#: 各自改字就会和词表分叉（2026-11-08 复查在 facts.py 找到过一句
#: "profile 里 *_pct 是百分数"，与命名例外直接冲突）。
UNITS_NOTES = {
    "returns": ("收益是小数（-0.0304 = -3.04%）。以 `_pct` 结尾的字段**不全是**"
                "百分数，例外见 GET /meta/vocabulary 的 "
                "unit_conventions.naming_exceptions，别按字段名猜。"),
    "pools": (UNIT_CONVENTIONS["rule"]
              + f" 历史命名 `pct_of_equity` 是 {POOL_UNITS['pct_of_equity']}，"
                f" `seal_ratio` 是 {POOL_UNITS['seal_ratio']}，要百分数用同行的 "
                "`seal_ratio_pct`。`seal_window` 保留带编号的原值；新消费者用 "
                "seal_window_no (int) 与 seal_window_text（纯中文时段名），"
                "不要自己剥前缀。"),
    "frame_delta": ("equal_weight_return 的差值是百分点：0.021 → 0.008 记作 "
                    "-0.013（即 -1.3 个百分点）。turnover_cny 的差值是这段窗口"
                    "新增的成交额，不是总量。"),
    "buckets": "百分数（pct）；interval 是 p25→p75 的经验区间，不是置信区间",
}


def note(key: str) -> str:
    return UNITS_NOTES[key]


def as_dict() -> dict:
    """给 ``GET /meta/vocabulary`` 的整张词表。"""
    return {
        "decisions": {
            "kind": sorted(DECISION_KINDS),
            "actor": sorted(DECISION_ACTORS),
            "undeclared_kind": sorted(UNDECLARED_KINDS),
            "undeclared_actor": sorted(UNDECLARED_ACTORS),
            "note": "undeclared_* 非空 = 有代码路径写了词表外的值，去 vocabulary.py 登记",
        },
        "guard_checks": sorted(GUARD_CHECKS),
        "decay": {"reasons": sorted(DECAY_REASONS), "notes": DECAY_REASON_NOTES},
        "delist_status": sorted(DELIST_STATUSES),
        "units": units_for("/market/pools"),
        "units_text": units_text(),
        "unit_conventions": UNIT_CONVENTIONS,
        "note": (
            "kind / actor / guard 检查项 / decay reason 的值域与数值量纲都在这里。"
            "前端映射表与本表逐项对齐（tests 会比对），units_text 与 web/plugin "
            "的生成物同源；不要照着某一次的响应现编中文。"
        ),
    }
