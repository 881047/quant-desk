"""The agent work queue: what is waiting, in priority order.

Split out of the HTTP layer so the dashboard and ``GET /duty`` cannot disagree
about what today's work is. Two implementations of "what is urgent" drift within
a week, and the one the model reads ends up being whichever was edited last.

Priority 0 is reserved for constraint violations (a breached stop, a cap that is
already exceeded), because those invalidate every other item on the list.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from service import settings
from service.kernel.clock import SHANGHAI
from service.trading.observation import snapshot as observe
from service.trading.regime import compute
from service.trading.stops import open_stops

#: The chain's handoff reports — five files, six reads (``market`` is read twice:
#: as ``D-1`` by 09:26 and as ``D`` by 19:30) — and the time the *reader*
#: arrives, not the time the writer is supposed to finish.
#:
#: ``docs/12-automation-chain.md`` §2 is the authority for what each task hands
#: to the next; §5 is the authority for what the reader does without it. This
#: table only names the artifact and the deadline; inventing a fallback here
#: would be a second policy that drifts from the documented one.
#:
#: Why a file list and not a scheduler API: the 7 automations live in the host,
#: and nothing on this side can ask "did the 09:05 run succeed?". What this side
#: *can* see is the handoff each run is supposed to leave behind — which is the
#: same thing a human would check.
_HANDOFFS: tuple[tuple[str, int, str, str], ...] = (
    ("review", -1, "09:26", "09:26 的「明天三条」与「一条改动」都在上一交易日的这份对账里"),
    ("market", -1, "09:26", "09:26 的「明日三档情景」在上一交易日的这份复盘里"),
    ("open", 0, "13:35", "13:35 要逐条对它的条件"),
    ("noon", 0, "14:35", "14:35 要接着午间的结论，避免重复买同一只"),
    ("close", 0, "19:30", "19:30 要逐条核这份尾盘报告的成交与理由"),
    ("market", 0, "19:30", "19:30 要用当日复盘做外部校验（抽三个数与 facts 对）"),
)


def _upstream_gaps(desk, today: str, reports_root: Path | None = None,
                   now_hm: str | None = None) -> list[dict]:
    """Handoffs that should be on disk by now and are not.

    Without this, a task that silently did not run is invisible: the next task
    reads a missing file, quietly takes its fallback, and the day looks normal.
    2026-11-01..04 was that shape for four days — zero trades, no gap named
    anywhere, and every report read as if the one before it had existed.

    Absence is reported only after the reader's own clock time, otherwise the
    queue would announce a gap at 09:10 for a report that is not due until
    19:30, and an alarm that always fires is a blind spot.
    """
    calendar = desk.market.calendar
    if not calendar.is_trading_day(today):
        return []
    if reports_root is None:
        from . import ROOT

        reports_root = ROOT / "reports"
    now_hm = now_hm or datetime.now(SHANGHAI).strftime("%H:%M")
    out: list[dict] = []
    for folder, offset, due, why in _HANDOFFS:
        if now_hm < due:
            continue
        day = today if offset == 0 else calendar.prev(today)
        if day is None:
            continue
        relative = f"reports/{folder}/{day}.md"
        if (reports_root / folder / f"{day}.md").is_file():
            continue
        out.append({
            "priority": 6, "kind": "upstream_missing", "blocks": None,
            "artifact": relative, "due_by": due,
            "detail": (f"{why}；但 {relative} 不在，{due} 那次任务大概率没跑成。"
                       f"按 docs/12-automation-chain.md §5 降级，并在输出里点名这个缺口——"
                       f"不要用别的数字补它。"),
        })
    return out


def _quote_state(desk) -> dict:
    """Can a decision be priced right now, at a price someone could fill at?"""
    try:
        from .persona import probe

        rt = (probe(desk.market, desk.registry) or {}).get("rt_quote") or {}
        if rt.get("tradable"):
            return {"tradable": True, "reason": f"盘中，{rt.get('session_label') or ''}"}
        if rt.get("feed_available"):
            return {"tradable": False,
                    "reason": f"{rt.get('session_label') or '盘后'}，只有收盘静态价"}
        return {"tradable": False, "reason": "行情源不可用"}
    except Exception:  # noqa: BLE001 - a missing probe must not hide the item
        return {"tradable": False, "reason": "行情状态读不到"}


def build(desk, day: str | None = None, today: str | None = None,
          reports_root: Path | None = None, now_hm: str | None = None) -> dict:
    """``reports_root`` and ``now_hm`` exist so a test can answer "what would the
    queue say at 14:00 with no afternoon report" without writing to the real
    ``reports/`` tree or waiting for the wall clock."""
    day = day or desk.market.latest_bar_date()
    today = today or datetime.now(SHANGHAI).strftime("%Y%m%d")
    summary = desk.book.summary()
    items: list[dict] = []

    if desk.market.calendar.is_trading_day(today) and day != today:
        # "No bar for today" is the normal state until 17:30, and it is *not*
        # the same thing as "cannot trade": a decision can be priced off a live
        # quote, which is what `DecisionService._date()` has done since the
        # intraday fill path landed. This line used to say 禁止下单 to a desk
        # that could fill, so the agent read "wait" and stopped proposing.
        quote_state = _quote_state(desk)
        if quote_state["tradable"]:
            items.append({"priority": 1, "kind": "intraday_pricing", "blocks": None,
                          "detail": (f"{today} 的日线要等收盘后（17:30）落库；"
                                     f"现在下单按实时行情定价，可以开仓")})
        else:
            items.append({"priority": 1, "kind": "wait_data",
                          "blocks": "all_new_entries",
                          "detail": (f"{today} 无日线，最近可用 {day}；"
                                     f"现在也没有可成交的实时价"
                                     f"（{quote_state['reason']}），新开会报 DATA_NOT_READY")})

    # A table written straight into SQLite is outside every rule this repo has.
    # On 2026-10-26 an `advice` table appeared with a pending SET_BUDGET that no
    # code reads, so a "suggestion" looked live for a week without expiring.
    for table, rows in desk.paper.orphan_tables().items():
        items.append({"priority": 2, "kind": "orphan_table", "blocks": None,
                      "detail": f"{table} 有 {rows} 行，但 state.DDL 未声明；"
                                f"它绕过 schema、guard 与决策日志，需人工处理"})

    # 当前人格的 sleeve 有没有一个真能用。
    #
    # ``retail`` 只持有 CORE，而 CORE 在 2026-11-01 被账户所有者关闭了
    # （``configs/core.json`` 的 ``retired=true``）。于是默认人格一个 sleeve
    # 都用不了，而这件事**不会报错**：调 ``propose(sleeve=AGGRESSIVE)`` 拿到的
    # 是 400 ``PERSONA_SLEEVE``，读起来像"你选错了袖"；``sleeve=CORE`` 则是
    # 409 ``CORE_RETIRED``。两条路都通向失败，但都没有一句"你现在没有可用的袖"。
    #
    # 写进待办而不是替他换人格：``persona.switch`` 要求 reason ≥20 字、至少一条
    # evidence、以及 expected_edge_bps——那是一个要留痕的决定，不该由内核代劳。
    try:
        from .persona import DEFAULT_PERSONA, REGISTRY, retired_sleeves

        current_key = str((desk.paper.persona_current() or {}).get("current")
                          or DEFAULT_PERSONA)
        meta = REGISTRY.get(current_key)
        if meta is not None:
            # 用 persona.retired_sleeves()，不在这里重算 UNIVERSE_CFG：
            # "哪些袖关了"只能有一个来源，否则 eligibility 与 duty 迟早给出
            # 两个不同的答案，而两个都自称是事实。
            closed = retired_sleeves()
            usable = [s for s in meta.sleeves if s not in closed]
            if not usable:
                items.append({
                    "priority": 2, "kind": "persona_dead_end",
                    "blocks": "all_new_entries",
                    "detail": (f"当前人格 {meta.key} 的袖 {list(meta.sleeves)} 一个都不可用："
                               f"CORE 已关闭（configs/core.json retired=true），"
                               f"而它不允许 AGGRESSIVE。要开短线，先 "
                               f"POST /persona/switch 到 trader（跨两袖、1-5 日）或 "
                               f"hot_money（1-2 日，需实时报价就绪）；两个都需要 "
                               f"reason ≥20 字、evidence 与 expected_edge_bps。"),
                })
    except Exception:  # noqa: BLE001, S110 - 一条提示不该让待办队列整体失败
        pass

    # Constraint state first: a breached stop is the one fact that invalidates
    # every other item on this list, and it blocks new risk until resolved.
    try:
        obs = observe(desk, day)
    except (settings.MissingParameter, settings.BadParameter):
        # ``observe`` prices exits, so it reads the cost model. A missing rate
        # is a configuration error, not "no constraints today".
        raise
    except (ValueError, KeyError, RuntimeError):
        obs = {"constraints": []}
    for record in obs.get("constraints", []):
        fact = record.get("fact") or {}
        kind = record.get("kind")
        if kind == "stop_breach":
            pct = fact.get("breach_pct")
            sessions = fact.get("sessions_below")
            detail = (f"{record.get('subject')} 收盘 {fact.get('close')} 低于止损 "
                      f"{fact.get('stop')}"
                      + (f"（{pct:+.2%}）" if isinstance(pct, (int, float)) else "")
                      + f"，已连续 {sessions} 个交易日")
            # 这一条不只是"某只票该走了"。``decisions.stop_gate`` 在任何一只持仓
            # 跌破止损时拒绝**所有**新开仓；内核/接口不替你下新判断的单，但破线
            # 持续 autostop.confirm_seconds 后，抓取循环会经 /decision/propose +
            # approve 代执行建仓票上写好的止损（configs/params.yaml 的
            # autostop.enabled 可关）。所以它是全账户的单点：未被处置或代执行前
            # 一直是 409 OPEN_VIOLATION。只说"低于止损"会让人以为这是单票问题。
            if isinstance(sessions, int) and sessions >= 1:
                detail += ("。它同时拒绝所有新开仓（409 OPEN_VIOLATION）；"
                           "内核不替你下新判断的单，但破线持续 autostop.confirm_seconds 后"
                           "抓取循环会代执行建仓票上写好的止损"
                           "（configs/params.yaml 的 autostop.enabled 可关）")
            items.append({"priority": 0, "kind": kind,
                          "subject": record.get("subject"), "detail": detail,
                          "blocks": "all_new_entries",
                          "sessions_unresolved": sessions})
            continue
        elif str(kind).endswith("_cap"):
            weight, cap = fact.get("weight"), fact.get("cap")
            detail = (f"{record.get('subject')} 权重 {weight:.1%} 超过上限 {cap:.0%}"
                      if isinstance(weight, (int, float)) and isinstance(cap, (int, float))
                      else f"{record.get('subject')} 触及上限")
        else:
            detail = f"{kind} / {record.get('subject')}"
        # 触及上限只挡"继续往这个桶里加"，与止损击穿那种全账户冻结不同。
        blocks = "entries_in_subject" if str(kind).endswith("_cap") else None
        items.append({"priority": 0, "kind": kind, "blocks": blocks,
                      "subject": record.get("subject"), "detail": detail})

    for order in summary.get("pending", []):
        items.append({"priority": 2, "kind": "pending_order", "blocks": None,
                      "detail": f"{order['ticket_id']} {order['symbol']} "
                                f"{order['side']} {order['qty']} 股，等待下一交易日开盘"})

    for view in open_stops(desk, day):
        if view.stop is None or view.stop <= 0:
            # ``if view.stop`` treated Decimal("0") as "no stop" and then fell
            # through to dist = 0.0, which is <= 2% — so a position whose stop
            # was never set produced a "distance to stop +0.00%" to-do every
            # single morning. No stop is not a stop that is very close.
            continue
        dist = float(view.last / view.stop - 1)
        if dist <= 0.02:
            items.append({"priority": 3, "kind": "near_stop", "blocks": None,
                          "detail": f"{view.symbol} 距止损 {dist:+.2%} "
                                    f"({view.last} vs {view.stop})"})

    for memo in desk.paper.list_memos(limit=50, status="open"):
        created = str(memo.get("created_at") or "")[:10].replace("-", "")
        due = False
        for pred in memo.get("predictions") or []:
            horizon = int(pred.get("horizon") or 0)
            if not horizon or not created:
                continue
            elapsed = sum(1 for d in desk.market.calendar.days if created < d <= day)
            if elapsed >= horizon:
                due = True
        if due:
            items.append({"priority": 4, "kind": "memo_due", "blocks": None,
                          "detail": f"#{memo['id']} {memo['topic'][:40]} 已到期，跑 score_memos"})

    for hyp in desk.paper.list_hypotheses(limit=50):
        if hyp["status"] in ("draft", "testing"):
            items.append({"priority": 5, "kind": "hypothesis_open", "blocks": None,
                          "detail": f"#{hyp['id']} {hyp['statement'][:40]}（{hyp['status']}）"})

    # 「看过，没买」也要到期结算。不提醒的话它会一直 pending——而 pending 的
    # 记录不会进 summary，等于又变回散文。判据和 memo 一样按交易日数，不按自然日。
    for row in desk.paper.list_declined(limit=100, settled=False):
        created = str(row.get("day") or "")
        horizon = int(row.get("horizon_days") or 0)
        if not created or not horizon:
            continue
        elapsed = sum(1 for d in desk.market.calendar.days if created < d <= day)
        if elapsed >= horizon:
            items.append({"priority": 4, "kind": "declined_due", "blocks": None,
                          "detail": (f"#{row['id']} {created} 放弃的 {len(row['symbols'])} 只"
                                     f"已过 {horizon} 个交易日，跑 declined/score")})
    # 直接按 kind + 日期查，不在内存里过滤 20 条窗口：note 的产出速度是 review
    # 的 4 倍（11-03 那天近 20 条里 15 条 note、4 条 review），窗口一旦滑过去，
    # 这个检查就会在真有复盘的日子里也报"还没有复盘记录"。
    # 顺带删掉一个死分支：ts 是 "2026-11-03T19:35:50+08:00"，`ts[:8] == "20261103"`
    # 永远不成立。
    reviews = desk.paper.search_journal(kind="review", since=day, until=day, limit=1)
    if not reviews:
        items.append({"priority": 6, "kind": "review_missing", "blocks": None,
                      "detail": f"{day} 还没有复盘记录"})

    # 交接物缺席：与上面那条 review_missing 分工不同——那条查 journal（19:30 的一半），
    # 这条查五份报告文件（整条链的交接）。两者都缺时会出现两条待办，因为那是两个
    # 独立的缺口：报告没写，账也没对。
    items.extend(_upstream_gaps(desk, today, reports_root, now_hm))

    try:
        regime = compute(desk.market, day)
        if regime.label == "stressed":
            # A state, stated as a state. This used to say "策略上宜减仓、不追高",
            # which is advice — the kernel does not give advice, it gives facts
            # and the rule that applies to them. The fact worth carrying is that
            # `stressed` is *not* a gate, because an agent that believes it is
            # one stops trading for weeks.
            facts = "；".join(regime.reasons[1:3]) if regime.reasons else ""
            items.append({"priority": 7, "kind": "regime_note", "blocks": None,
                          "detail": (f"stressed（{facts}）。这是状态标签，不是准入条件："
                                     f"guard 不会因为 stressed 拒单，仓位上限与单笔风险照常生效。")})
    except (ValueError, KeyError):
        pass

    # 会挡住下单的排在最前，不是因为"优先级高"，而是因为它们**不会自愈**：
    # 止损击穿天天返回 409 OPEN_VIOLATION，人格死路每一笔提案都被拒，
    # 放着不动明天还是这样。其余条目拖一天，系统状态不变。
    items.sort(key=lambda i: (0 if i.get("blocks") else 1, i["priority"]))
    blocking = [i for i in items if i.get("blocks") == "all_new_entries"]
    return {"as_of": day, "count": len(items), "items": items,
            "blocking": len(blocking),
            "note": ("两个字段回答两个问题：blocks 说现在能不能下单"
                     "（all_new_entries = 处置前所有新开仓都会被拒；"
                     "entries_in_subject = 只挡往那一桶里加），"
                     "priority 说先看哪一条——那是我排的编辑顺序，不是这个账户的价值判断。"
                     "blocks 非空的排在前面，理由是它们不自愈，不是它们更重要。"
                     "没有条目表示今天没有待办，可以选择不交易。")}
