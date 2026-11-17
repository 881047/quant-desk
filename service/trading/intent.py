"""ExecutionIntent：把「什么时候动手」变成可持久化、可求值、可被拒绝的事件对象。

为什么单独一个模块
------------------
事件驱动交易台的标准闭环是 MarketEvent → SignalEvent → Sizing/Risk →
OrderEvent → FillEvent。本台有帧（Market）、有 ticket（Order）、有 fill、
有 book、有 guard，**唯独模型的入场判断不是一个持久化对象**：它活在一次
agent turn 的上下文里，说完即蒸发。止损是唯一的例外——``autostop`` 已经有
一条分钟级自转回路，但它只服务离场意图。

本模块把那个抽象推广到入场意图。它与 ``autostop`` 平行同构，但有一条
更硬的边界：**内核不生成判断**。条件（contingency）是模型提票时写好的，
本模块只回答「这句话现在是否成立」，不生成信号、不打分、不排序、不发明
价格、不放宽风控、不决定买什么。

本文件与 watch 的分工（2026-11-08 校准）
--------------------------------------
- ``evaluate`` 是纯函数：无 I/O、无下单、无厂商请求、不读配置、不写状态。
- ``run`` 只写 **shadow 轨迹**并终态留痕，**不调用 approve、不产生任何订单、
  不动账本**。
- 兑现不在这里：``watch._run_intents`` 在 ``trigger.enabled=true`` 时对同一张
  已批准（armed）的票调 ``approve(ticket_id)``。**打开开关是真实出钱，不是只读
  观察**——旧版本这里写「P0 只读观察」，与实现相反，已改掉。开关关闭时
  ``propose`` 直接 409 ``TRIGGER_DISABLED``，不产生 armed 票，本模块与 watch 的
  兑现分支都不会被走到。
- 轨迹 scope 固定 ``shadow``，与 ``paper`` 的 alerts / book_wash 物理隔离，
  否则 ``quant_live`` 会把观察记录读成账户动作。

三态口径（钉死，不许后人当 bug 修）
----------------------------------
- ``triggered``：条件在有效窗内成立。终态，落轨迹。
- ``expired_untriggered``：越过 ``valid_until`` 仍未成立。终态，落轨迹。
  ``on_expiry`` 默认 ``mark_only``——只留痕，不代替模型下单，也不改现金。
- ``denied``：条件本身不可求值（未支持的 kind/op、类型错、帧里缺观测值）。
  **显式留证**，不许静默降级成 pending。看不见 ≠ 没触发，这两者必须分得开。
- ``pending`` 是求值结果但不是终态，不落轨迹；它只表示「这一帧继续等」。

时间口径
--------
``valid_from`` / ``valid_until`` 是 ISO 8601 字符串（上海时区即可，naive
按上海本地墙钟解释）。``frame_time`` 条件的 value 是 ``HHMMSS`` 六位串，
零填充下字典序等价于数值序。帧时钟只从 ``session_state`` 传入，本模块
不自己取 ``datetime.now()``——回放时那一帧的「现在」必须是调用方给的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from service.kernel.clock import SHANGHAI
from service.notify import alert_log

#: 观察记录与 paper 账本物理隔离。写错 scope 会把影子轨迹混进 quant_live。
SCOPE = "shadow"

#: 本阶段只认两种可观测维度。加新 kind 必须同时补 ``_observe``，否则一律
#: denied——宁可明确拒绝，不可把不认识的条件当成「没触发」放过去。
KINDS = frozenset({"price", "frame_time"})
OPS = frozenset({"lte", "gte", "eq"})

#: 只有这三种结果写进终态轨迹。pending 是等待，不是结论。
TERMINAL_STATES = frozenset({"triggered", "expired_untriggered", "denied"})

_PRICE_FIELDS = ("last", "price", "close")


def _shadow_base(base) -> Path | str | None:
    """把显式 base 也按 scope 分开，不能只靠 ``alert_log`` 的 scope 参数。

    实测 2026-11-07：``alert_log._base(base, scope)`` 把显式 base 当**精确目录**
    并忽略 scope（这是为测试重定向保留的既有契约，不是 bug）。生产路径传
    ``base=None`` 时靠 ``default_base(scope)`` 分成 reports/shadow 与
    reports/paper；但测试注入、以及将来 watch 若把 paper 的 base 顺手传过来，
    影子轨迹就会污染 paper 账本的 alerts。隔离统一走公共的
    ``alert_log.scoped_base``，不再在本模块私自补一层。
    """
    if base is None:
        return None
    return alert_log.scoped_base(base, SCOPE)


@dataclass(frozen=True)
class Contingency:
    """模型提票时冻结的条件。frozen 是刻意的：帧扫描不许改写意图。

    ``max_triggers=1`` 与 ``on_expiry="mark_only"`` 不是默认值习惯，是安全约束：
    一个被批准的判断只允许兑现一次，过期只允许留痕。
    """

    kind: str
    op: str
    value: Any
    valid_from: str | None = None
    valid_until: str | None = None
    max_triggers: int = 1
    on_expiry: str = "mark_only"


def coerce(raw: Mapping[str, Any] | Contingency | None) -> Contingency | None:
    """把票据里的 dict 还原成 frozen 对象。字段不全或多字段都返回 None（→ denied）。"""
    if raw is None:
        return None
    if isinstance(raw, Contingency):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        return Contingency(
            kind=str(raw["kind"]), op=str(raw["op"]), value=raw["value"],
            valid_from=raw.get("valid_from"), valid_until=raw.get("valid_until"),
            max_triggers=int(raw.get("max_triggers", 1)),
            on_expiry=str(raw.get("on_expiry", "mark_only")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def validate(c: Contingency | None) -> str | None:
    """返回拒绝原因；None 表示这个条件可以被求值。纯函数。"""
    if c is None:
        return "contingency_missing_or_malformed"
    if c.kind not in KINDS:
        return f"unsupported_kind:{c.kind}"
    if c.op not in OPS:
        return f"unsupported_op:{c.op}"
    if c.max_triggers != 1:
        return f"max_triggers_must_be_1:{c.max_triggers}"
    if c.on_expiry != "mark_only":
        return f"unsupported_on_expiry:{c.on_expiry}"
    if c.kind == "price":
        try:
            float(c.value)
        except (TypeError, ValueError):
            return "price_value_not_numeric"
    if c.kind == "frame_time" and (not isinstance(c.value, str) or len(c.value) != 6
                                   or not c.value.isdigit()):
        return "frame_time_value_not_HHMMSS"
    for label, stamp in (("valid_from", c.valid_from), ("valid_until", c.valid_until)):
        if stamp is not None and _parse(stamp) is None:
            return f"bad_{label}:{stamp}"
    return None


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _clock(session_state: Mapping[str, Any] | None) -> datetime | None:
    """帧时钟只认调用方给的。没有时钟就不判过期，也不判 frame_time。"""
    if not session_state:
        return None
    value = session_state.get("at") or session_state.get("now")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    return _parse(str(value)) if value is not None else None


def _observe(kind: str, frame_row: Mapping[str, Any] | None,
             session_state: Mapping[str, Any] | None) -> Any:
    """从这一帧取一个可比较的观测值。取不到返回 None → denied，不是 pending。"""
    if kind == "frame_time":
        now = _clock(session_state)
        return now.strftime("%H%M%S") if now is not None else None
    if not isinstance(frame_row, Mapping):
        return None
    for key in _PRICE_FIELDS:
        if key in frame_row and frame_row[key] is not None:
            try:
                observed = float(frame_row[key])
            except (TypeError, ValueError):
                return None
            # 停牌 / 缺报价常写成 0；0 不是价格，不能拿来比大小。
            return observed if observed > 0 else None
    return None


def _compare(observed: Any, op: str, value: Any) -> bool:
    if op == "eq":
        return observed == value
    try:
        left, right = float(observed), float(value)
    except (TypeError, ValueError):
        return False
    return left <= right if op == "lte" else left >= right


def evaluate(contingency: Mapping[str, Any] | Contingency | None,
             frame_row: Mapping[str, Any] | None = None,
             session_state: Mapping[str, Any] | None = None) -> dict:
    """回答「这个已批准的条件，在这一帧是否成立」。纯函数，零副作用。

    返回 ``{"state", "reason", "observed", "kind", "clock"}``。``state`` 取
    ``triggered / pending / expired_untriggered / denied``。求值顺序是
    **先合法性、再有效窗、再观测**：过期票即使这一帧价格命中也不算触发，
    否则一张昨天的票会在今天被兑现。
    """
    c = coerce(contingency)
    reason = validate(c)
    if reason is not None:
        return {"state": "denied", "reason": reason, "observed": None,
                "kind": getattr(c, "kind", None), "clock": None}

    now = _clock(session_state)
    clock = now.isoformat(timespec="seconds") if now else None
    if now is not None and c.valid_from and now < _parse(c.valid_from):
        return {"state": "pending", "reason": "before_valid_from",
                "observed": None, "kind": c.kind, "clock": clock}
    if now is not None and c.valid_until and now > _parse(c.valid_until):
        return {"state": "expired_untriggered", "reason": "past_valid_until",
                "observed": None, "kind": c.kind, "clock": clock}

    observed = _observe(c.kind, frame_row, session_state)
    if observed is None:
        return {"state": "denied", "reason": f"unobservable:{c.kind}",
                "observed": None, "kind": c.kind, "clock": clock}
    hit = _compare(observed, c.op, c.value)
    return {
        "state": "triggered" if hit else "pending",
        "reason": "condition_met" if hit else "condition_not_met",
        "observed": observed, "kind": c.kind, "clock": clock,
    }


def run(desk=None, *, day: str, now: datetime | None = None, base=None,
        frame_row: Mapping[str, Any] | None = None,
        frame_rows: Mapping[str, Mapping[str, Any]] | None = None,
        session_state: Mapping[str, Any] | None = None,
        tickets: list[dict] | None = None) -> list[dict]:
    """对一批 armed 票求值，终态写 shadow 轨迹。返回与 ``autostop.run`` 同 shape。

    **本函数不兑现**：不调 ``desk.decisions.approve``、不产生订单、不动账本。
    ``desk`` 只为与 ``watch.run`` 共用签名保留，本函数不读取它。兑现由 watch 拿
    ``status == "triggered"`` 的行去调 ``approve(ticket_id)``（开关打开时）——
    求值（内核）与出钱（执行层）分开，且绝不重新 propose 生成第二张票。

    ``frame_rows`` 按 symbol 或 6 位代码索引，让多只 armed 票各取自己那一行；
    ``frame_row`` 是全票共用的兜底（单票测试用）。
    """
    state = dict(session_state or {})
    if now is not None and "at" not in state and "now" not in state:
        state["at"] = now
    rows: list[dict] = []
    events: list[dict] = []
    for ticket in tickets or []:
        symbol = str(ticket.get("symbol") or "")
        row = frame_row
        if frame_rows:
            row = (frame_rows.get(symbol)
                   or frame_rows.get(symbol.split(".")[0])
                   or row)
        verdict = evaluate(ticket.get("contingency"), frame_row=row,
                           session_state=state)
        rows.append({
            "symbol": ticket.get("symbol"),
            "status": verdict["state"],
            "qty": int(ticket.get("qty") or 0),
            "ticket_id": ticket.get("ticket_id"),
            "reasons": [verdict["reason"]],
            "observed": verdict["observed"],
        })
        if verdict["state"] not in TERMINAL_STATES:
            continue
        events.append({
            "ts": verdict["clock"] or (now.isoformat(timespec="seconds") if now else None),
            "event": "intent_terminal",
            "rule": verdict["state"],
            "symbol": ticket.get("symbol"),
            "title": (f"入场意图 {ticket.get('ticket_id')}：{verdict['state']}"
                      f"（{verdict['reason']}）"),
            "facts": [f"observed={verdict['observed']}", f"kind={verdict['kind']}"],
            "ticket_id": ticket.get("ticket_id"),
            "state": verdict["state"],
            "scope": SCOPE,
        })
    if events:
        alert_log.append(day, events, base=_shadow_base(base), scope=SCOPE)
    return rows


def actor_for(ticket_id: str) -> str:
    """兑现时在决策日志里的身份：指回那张被批准的意图票，不是新判断。"""
    return f"trigger:{ticket_id}"
