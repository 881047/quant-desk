"""Personas: three decision styles, one explicit chooser.

Between "what is true" (the kernel's job) and "what to do" (the model's job)
there is a third question the desk had no place for: *which style of deciding is
appropriate right now*. A hot-money tape and a defensive tape reward opposite
behaviours; a single undifferentiated "the LLM proposes" hides that choice
inside prose, where it cannot be audited or scored.

This module makes the choice a first-class object:

* three personas with fixed sleeves, horizons and **data requirements**;
* switching costs an explicit reason, a directional expectation in bps and an
  invalidation, so the choice can be scored later instead of narrated;
* attribution per persona, because "the book made 3%" answers nothing when
  three styles are mixed into one number.

What it deliberately does not do
--------------------------------
**It never picks a persona by itself.** ``probe()`` reports which data each
style needs and whether that data is present; ``suggest()`` reports what the
tape and each persona's own evidence imply. The model decides and calls
``POST /persona/switch``. Auto-rotation on trailing returns is itself a
strategy, and an unmeasured one: the 2026 rotation literature finds regime
classification transfers out of sample far better than leadership hit-rates,
and Swingfolio's rule is to record the condition at entry, never re-label it
after the outcome. A controller that chases the last winner measures noise.
Anything that switches by itself belongs in ``service/research`` behind the same
referee as the ten falsified structures — not in the execution path.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from service import settings
from service.kernel.clock import SHANGHAI

ALL_REGIMES = ("risk_on", "neutral", "risk_off", "stressed")
# A style switch is a scarce act because it is recorded and scored later, but
# "twice a day" was an administrative number rather than a risk number: a third
# genuinely different setup could not be expressed at all. Configurable, so the
# budget can be raised without editing code.
MAX_SWITCHES_PER_DAY = settings.integer("persona.max_switches_per_day")
MIN_REASON_CHARS = settings.integer("persona.min_reason_chars")
MIN_INVALIDATION_CHARS = settings.integer("persona.min_invalidation_chars")
EDGE_BPS_BOUND = settings.num("persona.edge_bps_bound")


class PersonaError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PersonaMeta:
    key: str
    name: str
    style: str
    sleeves: tuple[str, ...]   # sleeves this style may spend from
    horizon: str
    regimes: tuple[str, ...]   # regimes in which the style has a reason to exist
    max_weight: float          # share of equity the style may hold at once
    requires: tuple[str, ...]  # datasets without which the style is self-deception
    why_not: str = ""
    evidence: dict = field(default_factory=dict)

    def allows_sleeve(self, sleeve: str) -> bool:
        return str(sleeve or "").upper() in self.sleeves

    def suited_to(self, regime: str) -> bool:
        return str(regime or "") in self.regimes

    def missing_requirements(self, probe: dict) -> list[str]:
        out = []
        for name in self.requires:
            item = probe.get(name) or {}
            if not item.get("ok"):
                out.append(name)
        return out

    def as_dict(self, regime: str | None = None, probe: dict | None = None) -> dict:
        row = {
            "key": self.key,
            "name": self.name,
            "style": self.style,
            "sleeves": list(self.sleeves),
            "horizon": self.horizon,
            "regimes": list(self.regimes),
            "max_weight": self.max_weight,
            "requires": list(self.requires),
            "why_not": self.why_not,
            "evidence": self.evidence,
        }
        if regime is not None:
            row["suited_to_regime"] = self.suited_to(regime)
        if probe is not None:
            missing = self.missing_requirements(probe)
            row["missing_requirements"] = missing
            row["ready"] = not missing
        return row


# Evidence 是**写这份注册表时的静态记录**，带 as_of；现算的战绩在 scorecard 里。
#
# 每条原来带一个 `verdict`。删掉的理由与 strategy.REGISTRY / knowledge.PRIORS 相同：
# 数字与 `why` 都在旁边，标签只增加权威；而且 `unproven` 不是裁判的词汇
# （裁判只有 testing / promoted / rejected / underpowered），是手写的。
#
# `why_not` 留着：它写的是**什么情况下这条风格最容易亏**，是策略描述，
# 不是许可。代码不校验它，拒单的只有 guard 那几条硬约束。
REGISTRY: dict[str, PersonaMeta] = {
    "hot_money": PersonaMeta(
        key="hot_money",
        name="游资",
        style="情绪驱动，按连板高度与封板质量出手，1–2 个交易日内必须了结",
        sleeves=("AGGRESSIVE",),
        horizon="1-2 日",
        regimes=("risk_on", "neutral"),
        max_weight=0.30,
        requires=("rt_quote", "pools", "sentiment"),
        why_not=(
            "情绪在冰点或退潮（tone=冰点/退潮）、炸板率 >35%、最高板 <3、"
            "或报价不可成交（盘后、断源、报价超过 90 秒）时不应使用——"
            "这四种情况下该风格的历史超额最差，且用收盘静态价冒充实时成交等于自欺"
        ),
        evidence={
            "as_of": "2026-11-03",
            # 读数是真的，但它测的动作和这条人格的动作不是同一个。
            #
            # `spec.py` 的 `_ENTRIES` 直到 2026-11-03 只有 next_open / next_close，
            # 两者都落在 T+1 之后；于是 P5–P24 全部在测"次日开盘接力"，而游资这条
            # 人格做的是"T 日封板买入"。这不是同一个动作：本地 2023-01~2026-10 首板
            # （n=44,705）T 收→T+1 收 +1.75%，其中隔夜段 +2.00%、T+1 日内 −0.22%
            # ——钱在隔夜那一段，而那一段在当时的研究语言里根本表达不出来。
            #
            # 所以判决保留，口径必须写出来。2026-11-03 之前的版本只写"九个结构全部
            # rejected"，模型据此在 reports/close/20261030.md 里写下"追二板既越权限
            # 又无证据"，把一条从未被测过的路当成已否定的路。信息源不能这样变成信念。
            "why": "P5–P24 九个短线结构全部 rejected，但**那些判决测的都是 next_open"
                   "（次日开盘接力）**：首板 -0.11%/笔、二板 p=0.159、炸板最深"
                   " -0.91%/笔（t -3.20）。打板这个入场点（T 日收盘价买入）当时的"
                   "语言表达不了，因此从未被这些判决覆盖——2026-11-03 起"
                   " spec.entry=t_close 才可测。判决不变，但不要拿它关闭打板这条路；"
                   "要关闭，先用 t_close 重跑。",
            "cost": "¥5,000 往返 45bps，盈亏平衡 0.85%/笔；这些结构最好毛边 0.30%",
            "closed_when_written": 0,
        },
    ),
    "retail": PersonaMeta(
        key="retail",
        name="散户",
        style="存活优先：低估值高股息、买跌不动的票，不追高、不补仓、不加杠杆",
        sleeves=("CORE",),
        horizon="10-60 日",
        regimes=("neutral", "risk_off", "stressed"),
        max_weight=0.70,
        requires=("daily",),
        why_not=(
            "趋势与宽度同时向上（risk_on）时必然跑输，此时用防守风格是拿机会成本"
            "换心安；也不该用来'等回本'——摊低成本是这条风格明令禁止的动作"
        ),
        evidence={
            "as_of": "2026-11-03",
            "why": "核心盘规则有 2019–2025 回测（CAGR 9.6%、Sharpe 1.33、MDD -8.3%，"
                   "见 strategy.core_etf），但那是 ETF 轮动规则的证据，"
                   "不是这个自由裁量风格的证据。这条风格本身：0 笔",
            "closed_when_written": 0,
        },
    ),
    "trader": PersonaMeta(
        key="trader",
        name="交易员",
        style="事件驱动与错杀修复：等一个可证伪的催化，1–5 个交易日兑现",
        sleeves=("CORE", "AGGRESSIVE"),
        horizon="1-5 日",
        regimes=ALL_REGIMES,
        max_weight=0.50,
        requires=("daily",),
        why_not=(
            "说不清'为什么是现在'、写不出失效条件、或这笔是在补偿上一笔亏损时"
            "不该使用；没有催化的事件交易只是把随机波动包装成故事"
        ),
        evidence={
            "as_of": "2026-11-03",
            "why": "写这一行时 1 笔持仓、0 笔平仓，样本 0/20——不够算区间，"
                   "不等于没有优势。现算的战绩看 /persona 的 scorecard",
            "closed_when_written": 0,
        },
    ),
}

DEFAULT_PERSONA = "retail"


def get(key: str) -> PersonaMeta | None:
    return REGISTRY.get(str(key or "").strip().lower())


def keys() -> list[str]:
    return sorted(REGISTRY)


# --------------------------------------------------------------------- probe
def probe(market, registry) -> dict[str, dict]:
    """Which data each persona needs is actually present, right now.

    A persona is only usable if the data its style depends on exists. Hot money
    without live quotes is a person reading yesterday's newspaper and calling it
    a tape; the kernel says so instead of letting the proposal fail later with a
    less obvious error.
    """
    day = ""
    try:
        day = str(market.latest_bar_date() or "")
    except Exception:  # noqa: BLE001  # pragma: no cover - reporting must not fail
        day = ""

    pools_end = ""
    try:
        for row in registry.summary():
            if str(row.get("dataset")) == "limit_up_pool":
                pools_end = str(row.get("cover_end") or "")
                break
    except Exception:  # noqa: BLE001  # pragma: no cover - reporting must not fail
        pools_end = ""

    # The pool panel is written after the close, so on a live session it is
    # always one trading day behind. Requiring today's partition would mark hot
    # money "blocked" during the only hours it can legally operate. One day of
    # lag is the same tolerance `build_brief` uses for the aggressive sleeve.
    prev_day = ""
    if day:
        try:
            prev_day = str(market.calendar.prev(day) or "")
        except Exception:  # noqa: BLE001  # pragma: no cover
            prev_day = ""

    session: dict[str, Any] = {}
    try:
        from .realtime import session_state

        # Pass the calendar: without it a weekday public holiday reports
        # `in_session=True`, and the probe then claims a live price can be
        # obtained on a day the exchange is shut.
        session = session_state(calendar=getattr(market, "calendar", None))
    except Exception:  # noqa: BLE001  # pragma: no cover - reporting must not fail
        session = {}

    # "In session" is not enough: if realtime quotes are switched off, a live
    # price cannot be obtained even at 10:30. Both conditions must hold, and
    # tests turn the network off so the answer is deterministic.
    allow_net = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")

    # The sentiment panel is a file the daily job writes, because the label is a
    # daily-frequency fact (pool counts, streak height, sealed amount) and the
    # only thing that ever needed a live session was where it used to live.
    sentiment_ok = False
    sentiment_tone = ""
    if day:
        path = Path(__file__).resolve().parents[2] / "data" / "sentiment" / f"{day}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                sentiment_tone = str((payload.get("sentiment") or {}).get("tone") or "")
                sentiment_ok = bool(sentiment_tone)
            except (OSError, json.JSONDecodeError):
                sentiment_ok = False

    return {
        "daily": {"ok": bool(day), "as_of": day,
                  "label": "日线（含复权与涨跌停价）"},
        "pools": {"ok": bool(pools_end) and (not day or pools_end >= (prev_day or day)),
                  "as_of": pools_end, "label": "盘面结构（涨停/炸板池，容忍 1 个交易日延迟）"},
        "sentiment": {"ok": sentiment_ok, "as_of": day if sentiment_ok else "",
                      "tone": sentiment_tone,
                      "label": "情绪面板（tone / 炸板率 / 最高板）"},
        # Two different questions, and conflating them produced a misleading
        # answer: the quote feed is wired and does answer after the close — with
        # the session's closing price (source=ths, age a few seconds, because the
        # vendor stamps the response, not the exchange). What it cannot do after
        # 15:00 is fill an order. `ok` therefore means "usable as a fill price",
        # not "the feed is broken".
        "rt_quote": {"ok": bool(allow_net and session.get("in_session")),
                     "tradable": bool(allow_net and session.get("in_session")),
                     "feed_available": bool(allow_net),
                     "in_session": bool(session.get("in_session")),
                     "session_label": str(session.get("label") or ""),
                     "as_of": str(session.get("now") or ""),
                     "label": "实时报价：盘中可取可成交；盘后仍取得到收盘价，但不可成交"},
    }


# -------------------------------------------------------------------- switch
def _commit_switch(store, *, day: str, to_key: str, reason: str,
                   evidence: list[str], expected_edge_bps: float, invalidation: str,
                   horizon_days: int | None, regime: str, actor: str,
                   readiness: str, nav: float | None) -> dict:
    """Write the audit row and active style in one IMMEDIATE transaction.

    The count and current-style checks must share that transaction too. Two
    processes could otherwise both observe the last available switch and commit
    an over-limit change, or leave an audit row pointing at a style that was
    never activated. PaperStore's connection rolls back on close after an error.
    """
    from .state import utc_now

    with store._lock, store._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT current FROM persona_state WHERE id=1").fetchone()
        from_key = str(row["current"]) if row is not None else DEFAULT_PERSONA
        if from_key == to_key:
            raise PersonaError("SAME_PERSONA",
                               f"already running {to_key}; a no-op switch would be noise", 409)

        used = 0
        if day:
            used = int(conn.execute(
                "SELECT COUNT(*) FROM persona_switch WHERE day=?", (day,),
            ).fetchone()[0] or 0)
        if used >= MAX_SWITCHES_PER_DAY:
            raise PersonaError(
                "SWITCH_LIMIT",
                f"{day} 已经切换 {used} 次（上限 {MAX_SWITCHES_PER_DAY}）。"
                f"继续换风格不是重新分析，是把当天的亏损换一种说法", 429)

        ts = utc_now()
        cur = conn.execute(
            """INSERT INTO persona_switch(ts, day, from_key, to_key, reason, evidence,
                 expected_edge_bps, invalidation, horizon_days, regime, readiness,
                 nav_at_switch, actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, day, from_key, to_key, reason,
             json.dumps(evidence, ensure_ascii=False), expected_edge_bps,
             invalidation, horizon_days, regime, readiness, nav, actor),
        )
        conn.execute(
            """INSERT INTO persona_state(id, current, since, day, switches_today,
                                         updated_at)
               VALUES (1,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 current=excluded.current, since=excluded.since, day=excluded.day,
                 switches_today=excluded.switches_today,
                 updated_at=excluded.updated_at""",
            (to_key, day, day, used + 1, ts),
        )
        return {"id": cur.lastrowid, "ts": ts, "day": day,
                "from": from_key, "switches_today": used + 1}


def switch(store, *, to: str, reason: str, evidence: list[str] | None = None,
           expected_edge_bps: float | None = None, invalidation: str = "",
           horizon_days: int | None = None, regime: str = "",
           probe_map: dict | None = None, actor: str = "agent",
           nav: float | None = None, day: str = "") -> dict:
    """Record an explicit persona change. Refuses vague or unserialisable ones.

    The four required fields are the whole point: a ``reason`` (what you see),
    at least one ``evidence`` id (where you saw it), ``expected_edge_bps`` (what
    you think it is worth) and an ``invalidation`` (what would prove you wrong).
    Without the last two, "switch to whatever is working" is unfalsifiable and
    the scorecard in ``scorecard()`` has nothing to score.
    """
    # ``day`` defaults to "" and every downstream count is keyed on it: with an
    # empty string ``used`` restarts at 0 on every call, so the daily cap never
    # bites and ``switches_today`` always reads 1.
    day = day or datetime.now(SHANGHAI).strftime("%Y%m%d")

    meta = get(to)
    if meta is None:
        raise PersonaError("BAD_PERSONA",
                           f"unknown persona {to!r}; known={keys()}", 422)

    current = store.persona_current()
    if current["current"] == meta.key:
        raise PersonaError("SAME_PERSONA",
                           f"already running {meta.key}; a no-op switch would be noise",
                           409)

    reason = str(reason or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise PersonaError("REASON_TOO_SHORT",
                           f"reason must be >= {MIN_REASON_CHARS} characters", 422)

    evidence = [str(e) for e in (evidence or []) if str(e).strip()]
    if not evidence:
        raise PersonaError("NO_EVIDENCE",
                           "at least one evidence id is required (daily:20261026, "
                           "pools:20261026, analog:...)", 422)

    if expected_edge_bps is None:
        raise PersonaError(
            "NO_EXPECTATION",
            "expected_edge_bps is required: a switch you cannot put a number on "
            "cannot be scored afterwards", 422)
    try:
        edge = float(expected_edge_bps)
    except (TypeError, ValueError):
        raise PersonaError("BAD_EXPECTATION", "expected_edge_bps must be a number", 422)
    if not math.isfinite(edge) or not (-EDGE_BPS_BOUND <= edge <= EDGE_BPS_BOUND):
        raise PersonaError(
            "BAD_EXPECTATION",
            f"expected_edge_bps must be finite and within ±{EDGE_BPS_BOUND:.0f}", 422)

    invalidation = str(invalidation or "").strip()
    if len(invalidation) < MIN_INVALIDATION_CHARS:
        raise PersonaError("NO_INVALIDATION",
                           f"invalidation must be >= {MIN_INVALIDATION_CHARS} characters",
                           422)

    probe_map = probe_map if probe_map is not None else {}
    missing = meta.missing_requirements(probe_map)
    readiness = "ready" if not missing else "blocked:" + ",".join(missing)

    warnings: list[str] = []
    if regime and not meta.suited_to(regime):
        warnings.append(
            f"{meta.key} 在 {regime} 状态下没有历史理据（注册表：{list(meta.regimes)}）；"
            f"允许切换，但这笔切换本身需要额外的证据")
    if missing:
        warnings.append(
            "缺少 " + ",".join(missing) + "：可以预置，但下单时会被 409 PERSONA_NOT_READY 拒绝")

    record = _commit_switch(
        store, day=day, to_key=meta.key, reason=reason, evidence=evidence,
        expected_edge_bps=edge, invalidation=invalidation, horizon_days=horizon_days,
        regime=regime, actor=actor, readiness=readiness, nav=nav,
    )
    return {
        "ok": True,
        "from": record["from"],
        "to": meta.key,
        "persona": meta.as_dict(regime or None, probe_map),
        "readiness": readiness,
        "warnings": warnings,
        "expected_edge_bps": edge,
        "switch_id": record.get("id"),
        "switches_today": record["switches_today"],
        "note": ("切换是显式记录，不是自动择时。它的收益要在 scorecard 里单独结算——"
                 "换风格本身不产生 alpha，只有换对才产生"),
    }


# ------------------------------------------------------------------ describe
def describe(regime: str = "", probe_map: dict | None = None) -> list[dict]:
    return [REGISTRY[k].as_dict(regime or None, probe_map) for k in keys()]


def current(store, regime: str = "", probe_map: dict | None = None,
            retired: tuple[str, ...] | None = None) -> dict:
    state = store.persona_current()
    meta = get(state["current"]) or REGISTRY[DEFAULT_PERSONA]
    row = meta.as_dict(regime or None, probe_map)
    row["since"] = state.get("since")
    row["days_in_persona"] = state.get("days_in_persona")
    row["switches_today"] = state.get("switches_today", 0)
    row["switch_limit"] = MAX_SWITCHES_PER_DAY
    # 袖还开着吗。放在这里是因为"当前人格是谁"和"当前人格能不能下单"是同一个
    # 问题的两半，而它们曾经分开显示：11-01 之后 retail 一直是当前人格，
    # ready=true，然后每一笔提案都被拒。
    closed = retired if retired is not None else retired_sleeves()
    open_sleeves = [s for s in meta.sleeves if s not in closed]
    row["open_sleeves"] = open_sleeves
    row["tradable"] = bool(open_sleeves)
    if not open_sleeves:
        row["dead_end"] = (
            f"人格 {meta.key} 的袖 {list(meta.sleeves)} 全部关闭"
            f"（retired={list(closed)}）。提案会分别拿到 "
            f"CORE_RETIRED（自己持有的袖）与 PERSONA_SLEEVE（不持有的袖），"
            f"两条路都失败。要开单先 POST /persona/switch。"
        )
    return row


def retired_sleeves() -> tuple[str, ...]:
    """Which sleeves the account owner has closed. A fact, not a preference."""
    try:
        from .core import UNIVERSE_CFG
    except (ImportError, AttributeError):
        return ()
    return ("CORE",) if bool(getattr(UNIVERSE_CFG, "retired", False)) else ()


def eligibility(regime: str = "", probe_map: dict | None = None, *,
                retired: tuple[str, ...] | None = None) -> dict:
    """哪些人格没有被事实否掉。

    名字原来是 ``suggest``，读起来像"内核推荐你用这个"。它做的事其实只有三道
    硬筛：数据是否齐、袖是否还开着、状态是否对口。筛完怎么选是模型的事。

    This is *not* a recommendation engine. It filters on three hard facts only —
    data readiness, sleeve availability, regime fit — and returns the survivors
    with their caveats. Choosing among them is the model's job, and the scorecard
    is what judges it.

    ``sleeve`` was the missing third. On 2026-11-01 the owner closed CORE, and
    ``retail``'s only sleeve is CORE — so for three sessions this function
    reported ``retail`` as "consistent with the tape" while every proposal from
    it was refused (409 ``CORE_RETIRED`` on the sleeve it owns, 400
    ``PERSONA_SLEEVE`` on the one it does not). "Which style fits this tape" and
    "which style can place an order at all" are different questions, and only
    the first one was being asked. A candidate that cannot execute is not a
    candidate, however well it fits the regime.
    """
    probe_map = probe_map or {}
    closed_sleeves = retired if retired is not None else retired_sleeves()
    fits, blocked_data, blocked_sleeve, unsuited = [], [], [], []
    for key in keys():
        meta = REGISTRY[key]
        missing = meta.missing_requirements(probe_map)
        if missing:
            blocked_data.append({"key": key, "missing": missing})
            continue
        # 先看能不能下单，再看该不该用。反向的顺序会让"袖已关闭"的人格在
        # 状态不对口时被归进 not_suited_to_regime——那读起来像"今天不合适"，
        # 而事实是"哪天都不合适"。
        closed = [s for s in meta.sleeves if s in closed_sleeves]
        if len(closed) == len(meta.sleeves):
            blocked_sleeve.append({"key": key, "sleeves": list(meta.sleeves),
                                   "retired": closed})
            continue
        if regime and not meta.suited_to(regime):
            unsuited.append({"key": key, "regimes": list(meta.regimes)})
            continue
        fits.append(key)
    return {
        "regime": regime or None,
        "retired_sleeves": list(closed_sleeves),
        "consistent_with_tape": fits,
        "blocked_by_data": blocked_data,
        "blocked_by_sleeve": blocked_sleeve,
        "not_suited_to_regime": unsuited,
        "note": ("这里只做三件硬筛：数据是否齐、袖是否还开着、状态是否对口。"
                 "筛完的候选怎么选是模型的事，选得对不对由 scorecard 结算。"
                 "blocked_by_sleeve 是「哪天都不能用」，not_suited_to_regime 是"
                 "「今天不合适」——两件事，别混着读"),
    }


# ----------------------------------------------------------------- scorecard
def cached_scorecard(desk, day: str, *, switches=None) -> dict:
    """The slow half of ``/persona``, behind one cache key shared with warmup.

    ``/persona`` used to build its own key here; the startup warmup then had to
    either duplicate the key or warm a different entry (which warms nothing).
    Keeping the key in one function makes "the first request after a restart is
    fast" a property of the code rather than of two matching string literals.
    ``switches`` is passed by the endpoint (it already reads the history); the
    warmup passes nothing and reads the count itself.
    """
    count = len(switches if switches is not None
                else desk.paper.persona_switches(limit=10))
    key = f"api:persona:scorecard:{day}:{count}"
    return desk.market._cached(key, lambda: scorecard(desk, limit=50))


def scorecard(desk, limit: int = 200) -> dict:
    """Did switching actually pay? The only honest reading of "收益最大化".

    Measured from the switch moment to now, against the equal-weight market over
    the same window, so a switch made in a rising tape does not get credit for
    beta. It is a small sample by construction: with a two-switch daily cap this
    number needs a month before it says anything.
    """
    from .arena import _window_return, market_for_cases

    rows = desk.paper.persona_switches(limit=limit)
    if not rows:
        # Nothing to score. Reading the market series anyway was 4.5 seconds to
        # produce zero — and "0 switches" is already the whole answer.
        return {"switches": 0, "scored": 0, "mean_excess_pct": None, "detail": [],
                "note": "还没有切换记录，无从结算。先有带 expected_edge_bps 的切换，才有这一栏"}
    equity = float(desk.book.summary()["equity"])
    # Bounded by the oldest switch: this endpoint used to rebuild twenty years
    # of market returns to score two or three of them.
    market = market_for_cases(desk.data, rows)

    total = 0.0
    scored = 0
    detail = []
    for row in rows:
        nav0 = row.get("nav_at_switch")
        if not nav0:
            detail.append({**_trim(row), "since_switch_pct": None,
                           "excess_pct": None, "note": "切换时没有记净值，无法结算"})
            continue
        since = equity / float(nav0) - 1
        bench = _window_return(market, str(row.get("day") or ""),
                              str(desk.market.latest_bar_date() or ""))
        excess = (1 + since) / (1 + bench) - 1 if bench is not None else None
        total += excess if excess is not None else 0.0
        scored += 1 if excess is not None else 0
        detail.append({**_trim(row), "since_switch_pct": round(since, 5),
                       "bench_pct": None if bench is None else round(bench, 5),
                       "excess_pct": None if excess is None else round(excess, 5),
                       "beat_expectation": (
                           None if excess is None or row.get("expected_edge_bps") is None
                           else (excess * 10000) >= float(row["expected_edge_bps"]))})

    return {
        "switches": len(rows),
        "scored": scored,
        "mean_excess_pct": round(total / scored, 5) if scored else None,
        "detail": detail,
        "note": ("每次切换至今的账户收益减去同期等权市场；样本按设计很小，"
                 "一条切换不足以说明选择器有效或无效"),
    }


def _trim(row: dict) -> dict:
    keep = ("id", "day", "from_key", "to_key", "expected_edge_bps", "horizon_days",
            "regime", "readiness", "actor")
    return {k: row.get(k) for k in keep}
