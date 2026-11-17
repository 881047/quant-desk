"""The paper book's own watchdog: run the tripwire on every captured frame.

The market is captured every 30 s–3 min all session (``snapshot_intraday.py``).
Until now nothing looked at *our* book on those frames: the tripwire existed, ran
in 76 ms warm, and had no scheduler — it ran only when the model called
``/watch``, which is to say only when the model was already awake. So the last
piece of "the desk watches itself" was missing on the side that actually holds
the money a human asked it to manage.

This module is that loop's other half. It does three things per frame, in order:

1. price the book on **that frame** (not on a second vendor request — see
   ``QuoteService.from_frame``);
2. ask the tripwire what crossed a threshold, without the tape percentile check
   (that number compares today with twenty sessions of history and does not move
   between two frames of the same minute; paying two parquet reads every thirty
   seconds for it buys nothing);
3. hand the findings to ``alert_log.reconcile``, which writes only transitions
   and keeps the open set, then beat.

Why the heartbeat is written here and not by the caller: the two files must be
written by the same run, or a crash between them reports "ran at 14:28" with a
finding set from 14:20. The loop wraps this call in ``try`` and beats ``error``
on failure, so a broken wire is visible as a state, not as silence.

No mail, no handoff. Two kinds of **already-approved** order may be placed here,
and neither is a new judgement: a stop the model wrote when it opened the
position (``autostop``, gated by ``autostop.enabled``) and an entry intent the
model wrote when it proposed (``intent``, gated by ``trigger.enabled``). The
second only calls ``approve(ticket_id)`` on the SAME ticket — re-proposing would
create a second ticket with a second thesis and a second guard pass. Everything
else the model reads at its next wake and decides; this module only makes sure
that decision is made about facts that are current.

The order of the three steps is load-bearing: record, then act, then beat. A
sale that throws must still leave the breach it was based on on disk, and a
heartbeat written before the act would claim a run that never finished.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from service import settings
from service.kernel.clock import SHANGHAI
from service.notify import alert_log

from . import autostop, intent, tripwire
from . import frame as frame_view

#: 这份日志属于 paper 账本。整个模块只有这一个地方写它，读它的 `read()` 也用它。
SCOPE = "paper"


#: 只记录、不裁决：意图终态写回票据，但 ``status`` 仍由状态机管（armed 靠跨日
#: 自动 expired）。把观察结果当成状态迁移会让「过期未触发」与「主动撤销」不可分辨。
INTENT_TERMINAL_RECORD_STATES = frozenset({"expired_untriggered", "denied"})


def _record_intent_terminals(desk, rows: list[dict], now) -> int:
    """把 shadow 轨迹里的终态回填到票据，供离线审计链直接读。

    放在 watch 而不是 ``intent.run``：后者是零 I/O 的纯求值函数，写票据属于
    执行层职责。只增三个可选字段，**不改 status、不新增状态枚举、不释放资金额度**；
    已有记录就跳过（幂等）。审计写失败不能弄死盯盘循环，所以逐票兜住异常。

    D-18：``expired_untriggered`` 额外释放 **fingerprint 去重槽位**（不是
    ``client_request_id`` 承诺），让模型用相同条件重提时能拿到新票。释放放在
    写票据**之前**：任一步失败时票还没被标记终态，下一帧会自然重试，不会把
    槽位永久卡住。``denied`` **不释放**——它包含 ``unobservable:price``
    这类「这一帧没看到报价」，下一帧可能恢复，永久退役会误杀合法意图。
    """
    store = getattr(getattr(desk, "decisions", None), "store", None)
    if store is None:
        return 0
    stamp = now.isoformat(timespec="seconds") if hasattr(now, "isoformat") else str(now)
    written = 0
    for row in rows:
        state = row.get("status")
        if state not in INTENT_TERMINAL_RECORD_STATES or not row.get("ticket_id"):
            continue
        try:
            ticket = store.get_ticket(row["ticket_id"])
            if ticket is None or ticket.get("intent_terminal_state"):
                continue
            if state == "expired_untriggered":
                store.release_proposal_claim(row["ticket_id"])
            ticket["intent_terminal_state"] = state
            ticket["intent_terminal_at"] = stamp
            ticket["intent_terminal_reason"] = (
                "; ".join(str(r) for r in (row.get("reasons") or [])) or None)
            store.save_ticket(ticket)
            written += 1
        except Exception:  # noqa: BLE001,S112 - 审计写失败不影响盯盘，轨迹仍在 shadow
            continue
    return written


def _run_intents(desk, day: str, captured, *, now, base) -> list[dict]:
    """扫描 armed 票 → 写 shadow 轨迹 → 开关开启时兑现**同一张票**。

    与 autostop 平行，分工不同：``intent.run`` 只求值模型写好的条件（纯观察），
    本函数负责唯一的资金动作。两道 flag 检查缺一不可——主生效点在
    ``propose/arm``，这里兜住「开关从 true 切回 false 后遗留的 armed 票」：
    每个兑现动作前都要再读一次，否则关掉开关仍会被下一帧扫出来出钱。
    """
    try:
        armed = desk.decisions.list_tickets(limit=100, status="armed", view="full")
    except Exception as exc:  # noqa: BLE001 - 盯盘不能弄死抓取循环
        return [{"symbol": None, "status": "error", "qty": 0, "ticket_id": None,
                 "reasons": [f"list_armed: {type(exc).__name__}: {exc}"]}]
    # D-18: 已判定「过期未触发」的票事实上退役了，不再参与求值，免得每帧
    # 重复往 shadow 轨迹里写同一条终态。``denied`` 不在这里过滤：缺观测可能
    # 只是这一帧的事，下一帧有了报价仍应能触发。
    armed = [t for t in armed
             if t.get("intent_terminal_state") != "expired_untriggered"]
    if not armed:
        return []

    frame_rows: dict = {}
    try:
        wanted = {str(t.get("symbol") or "").split(".")[0] for t in armed}
        for record in captured.to_dict("records"):
            code = str(record.get("code") or "")
            if code in wanted:
                frame_rows[code] = record
    except Exception:  # noqa: BLE001 - 缺帧就是 unobservable → denied 留证
        frame_rows = {}

    # D-20: ``intent.run`` 内部会写 shadow 轨迹；磁盘满/权限错不该让整帧
    # watch 抛出去（外层 snapshot_intraday 虽兜住，但那一帧的求值、兑现与
    # 正常心跳会一起丢）。求值失败时**不写终态、不兑现**，返回一条 error row
    # 与 list_armed 失败同 shape，让调用方看得见而不是静默跳过。
    try:
        rows = intent.run(None, day=day, base=base, frame_rows=frame_rows,
                          session_state={"at": now}, tickets=armed)
    except Exception as exc:  # noqa: BLE001 - 盯盘不能弄死抓取循环
        return [{"symbol": None, "status": "error", "qty": 0, "ticket_id": None,
                 "reasons": [f"intent_eval: {type(exc).__name__}: {exc}"]}]
    # 终态回填是观察记录，不是资金动作：即使总开关已关，存量 armed 票的
    # 「为什么没触发」也必须留在票上，不能只剩一份要另外去翻的轨迹文件。
    _record_intent_terminals(desk, rows, now)
    if not settings.flag("trigger.enabled"):
        return rows
    for row in rows:
        if row.get("status") != "triggered":
            continue
        # 存量兜底：每次出钱前再读一次开关。
        if not settings.flag("trigger.enabled"):
            break
        try:
            result = desk.decisions.approve(
                row["ticket_id"], actor=intent.actor_for(row["ticket_id"]))
            row["promoted"] = str(result.get("status") or "unknown")
        except Exception as exc:  # noqa: BLE001 - 代下单失败不该弄死抓取循环
            row["promoted"] = "error"
            row["reasons"] = [f"{type(exc).__name__}: {exc}"]
    return rows


def run(
    desk,
    path: Path | str,
    *,
    day: str | None = None,
    now: datetime | None = None,
    base: Path | str | None = None,
    clear_after: float | None = None,
) -> dict:
    """Judge the book on one archived frame, log what changed, beat.

    ``path`` is the frame file the capture just wrote. Reading it back rather than
    taking the in-memory frame is deliberate: ``quant_live`` will later serve the
    file, and the alert the model reads must be about the bytes it can open.
    """
    now = now or datetime.now(SHANGHAI)
    day = str(day or now.strftime("%Y%m%d"))
    # This process is not the book's writer. The API filled a buy at 09:26; this
    # process booted at 09:15 and its `PaperBook` holds a snapshot from then.
    # `PaperBook` re-reads on its own only when a *write* conflicts, so a reader
    # that never writes would watch the opening book all day — a guard that
    # reports "no positions" every afternoon, which is worse than no guard.
    desk.book.reload()
    captured = frame_view.load(path)
    result = tripwire.evaluate(desk, day=day, now=now, include_tape=False, frame=captured)
    moved = alert_log.reconcile(day, result["findings"], at=now, base=base,
                                scope=SCOPE, clear_after=clear_after)
    # 先记账，再动手：动作可能失败、可能抛，而"几点、哪条线、破了多久"必须
    # 已经落盘。顺序反过来的话，一笔失败的卖出会把它的证据一起带走。
    acted = autostop.run(desk, day=day, scope=SCOPE, now=now, base=base,
                         findings=result["findings"])
    intent_rows = _run_intents(desk, day, captured, now=now, base=base)
    # 代下单的异常不能只留在 stdout：模型下次醒来读的是 alerts 文件。把
    # acted 里 status=="error" 的行写进已经存在的 <day>_alerts.jsonl，一条
    # 一个人工能读的字。只补这一条，不改 beat 的 outcome——心跳的 ok/closed
    # 语义由 _closing_beat 管，动它会让"失败后的退出"被盖掉。
    stamp = moved["at"]
    failures = [
        {
            "ts": stamp,
            "event": "autostop_error",
            "rule": "autostop_error",
            "symbol": row.get("symbol"),
            "title": (f"自动止损代下单失败：{row.get('ticket_id') or '?'}；"
                      + "；".join(str(r) for r in (row.get("reasons") or [])[:2])),
            "facts": [f"qty={row.get('qty')}"],
        }
        for row in acted
        if row.get("status") == "error"
    ]
    if failures:
        alert_log.append(day, failures, base=base, scope=SCOPE)
    alert_log.beat(day, scope=SCOPE, outcome="ok", now=now, base=base)
    return {
        "at": moved["at"],
        "day": day,
        "frame": Path(path).stem,
        "quotes": result["checked"]["quotes"],
        "live_quotes": result["checked"]["live_quotes"],
        "findings": result["findings"],
        "counts": result["counts"],
        "fired": moved["fired"],
        "cleared": moved["cleared"],
        "open": moved["open"],
        "acted": acted,
        # 意图行走独立字段：不并进 acted，autostop 的调用方按旧 shape 读它。
        "intent": intent_rows,
    }
