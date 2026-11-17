"""Strategy registry: what can run, in which regimes, and with what evidence.

The shape follows the LEAN algorithm framework (Alpha -> Portfolio Construction
-> Risk -> Execution) but without the ceremony: a strategy here is metadata plus
an optional signal function. The LLM is a strategy like any other — a
"discretionary" one whose signals arrive as tickets rather than computed rows.

Why a registry at all: without one, "add a strategy" means "add a script", and
there is no place to answer "how much capital is this allowed to touch?".
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALL_REGIMES = ("risk_on", "neutral", "risk_off", "stressed")


@dataclass(frozen=True)
class StrategyMeta:
    key: str
    name: str
    kind: str                 # discretionary | signal | passive
    regimes: tuple[str, ...]  # regimes in which it may open risk
    status: str               # live | probation | research | retired
    max_weight: float         # hard cap on its share of equity
    book: str = "paper"       # paper | core — which ledger it spends from
    note: str = ""
    evidence: dict = field(default_factory=dict)
    auto_allocate: bool = True  # False = capital only from explicit per-decision target_weight

    def eligible(self, regime: str) -> bool:
        return self.status in ("live", "probation") and regime in self.regimes

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "regimes": list(self.regimes),
            "status": self.status,
            "max_weight": self.max_weight,
            "book": self.book,
            "note": self.note,
            "evidence": self.evidence,
            "auto_allocate": self.auto_allocate,
        }


# Evidence 是**写这份注册表时的静态记录**，不是此刻的状态。要现算的笔数与超额，
# 看 /strategies 响应里的 stats（n / mean_excess / max_drawdown）与 knowledge.ledger。
#
# 每条原来还带一个 `verdict`（rejected / unproven）。删掉的理由和 knowledge.PRIORS
# 一样：数字都在旁边，标签不增加信息，只增加权威。而且 `unproven` 根本不是裁判的
# 词汇（裁判只有 testing / promoted / rejected / underpowered），它是手写的。
#
# `status` 留着，但它不是"这条策略好不好"的判断，是**运营开关**：allocator 按它
# 决定分不分配资金。`retired` 的拿不到权重，这与它是否曾经赚钱无关，也与它将来
# 会不会赚钱无关——要复活一条 retired 策略，改这里不是证据，跑一次研究才是。
REGISTRY: dict[str, StrategyMeta] = {
    "llm": StrategyMeta(
        key="llm",
        name="LLM 自主决策",
        kind="discretionary",
        regimes=ALL_REGIMES,
        status="live",
        max_weight=0.60,
        note="模型自由浏览后提案；不受 regime 限制，但 stressed 下单笔风险减半",
        evidence={"as_of": "2026-11-03", "closed_when_written": 0,
                  "why": "写这一行时是 0/20 笔平仓，还没有超额证据。"
                         "现算的笔数与超额在同一响应的 stats 里，不要引这里的数"},
    ),
    "short_alpha": StrategyMeta(
        key="short_alpha",
        name="WorkBuddy 短线 α",
        kind="discretionary",
        regimes=ALL_REGIMES,
        status="probation",
        max_weight=0.60,
        note="T 日由 WorkBuddy 冻结个股与目标权重，T+1 开盘入场，动态复评，最迟 T+2 退出；不参与通用自动分配",
        evidence={"as_of": "2026-11-15", "closed_when_written": 0,
                  "why": "仅获自动模拟交易授权；尚无该策略的真实样本外平仓证据。"
                         "candidate/watch 都可参与，实际成交与收益必须读票据和账本"},
        auto_allocate=False,
    ),
    "core_etf": StrategyMeta(
        key="core_etf",
        name="核心 ETF 风险平价",
        kind="passive",
        regimes=ALL_REGIMES,
        status="live",
        max_weight=0.80,
        book="core",
        note="逆波动率权重 + MA200 趋势闸门 + 12% 波动目标；余量留现金",
        evidence={"as_of": "2025-12-31", "cagr": 0.096, "sharpe": 1.33,
                  "max_dd": -0.083, "window": "2019-01~2025-12",
                  "cost_bps_per_side": 10,
                  "why": "这是 ETF 轮动规则的回测，不是自由裁量的证据"},
    ),
    "cb_link_down": StrategyMeta(
        key="cb_link_down",
        name="正股跌停→买转债",
        kind="signal",
        regimes=("risk_off", "stressed"),
        status="retired",
        max_weight=0.0,
        note="P21 按内核真实费用重判后转负；不再分配资金",
        evidence={"as_of": "2026-10", "mean_excess": -0.00195, "p": 0.357, "n": 300,
                  "pbo": 0.043, "cpcv_p05": -1.21,
                  "why": "P8 的 +0.30% 是 20bps 平坦成本下的数；按内核 40bps 重跑为 "
                         "-0.195%，p 0.078→0.357，2x 成本 Sharpe 为负"},
    ),
    # 样本账本的策略。它不是一条 alpha，是一个采样装置：独立本金、1 只持仓、
    # 每天买一卖一，目的是把「一个想法要多久才能被证伪」从年压到月。
    # max_weight=1.0 是机械要求（2,000 元一手就占七成），不是风险偏好。
    "sample_1d": StrategyMeta(
        key="sample_1d",
        name="样本账本·一日轮换",
        kind="discretionary",
        regimes=ALL_REGIMES,
        status="probation",
        max_weight=1.00,
        book="samples",
        note="独立 2,000 本金、独立 sqlite、每日买一卖一；KPI 是结算笔数不是收益率",
        evidence={"as_of": "2026-11-03", "closed_when_written": 0,
                  "why": "写这一行时 0/20 笔结算。这个装置的 KPI 是结算笔数，"
                         "不是收益率——现算的笔数在 samples 里，不在这一行"},
    ),
}


def get(key: str) -> StrategyMeta | None:
    return REGISTRY.get(str(key or "").lower())


def keys() -> list[str]:
    return sorted(REGISTRY)


def describe(regime: str | None = None) -> list[dict]:
    out = []
    for meta in REGISTRY.values():
        row = meta.as_dict()
        row["eligible_now"] = None if regime is None else meta.eligible(regime)
        out.append(row)
    return sorted(out, key=lambda r: (r["status"] != "live", r["key"]))
