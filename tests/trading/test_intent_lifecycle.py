"""P0-D 意图生命周期：两阶段 approve、帧兑现、开关兜底、fail-closed。

这些用例守的是资金路径。任何一条变红，都意味着「条件还没成立钱就出去了」
或「开关关了昨天的票还会被下一帧兑现」。
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from service import settings
from service.kernel.clock import SHANGHAI
from service.trading import watch
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal

_CONDITION = {"kind": "price", "op": "lte", "value": 10.0}
_NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=SHANGHAI)


@pytest.fixture
def trigger(monkeypatch):
    """可切换的两级开关；其它 flag（尤其 autostop.enabled）一律透传原值。

    on = 兑现出钱（trigger.enabled），observe = 只写条件与影子轨迹。
    """
    original = settings.flag
    state = {"on": False, "observe": False}

    def flag(key: str) -> bool:
        if key == "trigger.enabled":
            return state["on"]
        if key == "trigger.observe":
            return state["observe"]
        return original(key)

    monkeypatch.setattr(settings, "flag", flag)
    return state


def _propose(desk, *, condition=_CONDITION, **overrides):
    symbol = pick_symbols(desk, 1)[0]
    req = {**proposal(symbol), "contingency": condition, **overrides}
    return symbol, desk.decisions.propose(req, actor="agent")


def _frame(symbol: str, last: float = 9.0) -> pd.DataFrame:
    return pd.DataFrame({"code": [symbol.split(".")[0]], "last": [last],
                         "snapshot_ts": ["10:00:00"]})


def _account(desk) -> tuple:
    """账本三项：现金、持仓、挂单。资金动作的零变化判据。"""
    book = desk.book
    return (float(book.equity()),
            json.dumps(book.summary()["positions"], ensure_ascii=False, sort_keys=True,
                       default=str),
            json.dumps(book.state.get("pending") or [], ensure_ascii=False,
                       sort_keys=True, default=str))


def test_propose_with_condition_is_refused_when_switch_is_off(desk, trigger):
    """裁定 ①：fail-closed。关闭时不接收条件，零票据、零资金动作。"""
    with pytest.raises(DecisionError) as err:
        _propose(desk)
    assert err.value.code == "TRIGGER_DISABLED"
    assert err.value.status == 409


def test_observe_only_accepts_conditions_and_arms_without_spending(desk, trigger):
    """observe 单开：能写买卖点、能冻结成 armed，账本三项零变化。"""
    trigger["observe"] = True
    before = _account(desk)
    _symbol, ticket = _propose(desk)
    assert ticket["status"] == "proposed"
    armed = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert armed["status"] == "armed"
    assert _account(desk) == before


def test_observe_only_second_approve_never_becomes_an_order(desk, trigger):
    """最关键的一道：手动二次 approve 也不许绕过兑现开关出钱。

    首次 approve 的 arm 分支原本只在 enabled=true 时进入；只放开 propose
    而漏掉这里，条件票会直接落到 execute_ticket，影子模式就成了真下单。
    """
    trigger["observe"] = True
    before = _account(desk)
    _symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    second = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert second["status"] == "armed"
    assert "observe-only" in second.get("note", "")
    assert _account(desk) == before
    assert desk.book._fill_exists(ticket["ticket_id"]) is None


def test_observe_only_watch_evaluates_but_never_promotes(desk, trigger, tmp_path,
                                                         monkeypatch):
    """命中帧照常观察、写影子终态，但 promoted 为空、账本零变化。"""
    trigger["observe"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    before = _account(desk)
    monkeypatch.setattr(desk.book, "_try_fill",
                        lambda *a, **k: pytest.fail("_try_fill 不该被调用"))

    rows = watch._run_intents(desk, "20261106", _frame(symbol), now=_NOW,
                              base=tmp_path)
    assert rows and rows[0]["status"] == "triggered"
    assert rows[0].get("promoted") is None
    assert _account(desk) == before
    assert desk.decisions.store.get_ticket(ticket["ticket_id"])["status"] == "armed"


def test_manual_approve_of_untriggered_armed_ticket_never_fills(desk, trigger):
    """11-08 深审 B2 核心：条件没成立时手工二次 approve 不得按市价成交。"""
    trigger["on"] = True
    before = _account(desk)
    _symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    out = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert out["status"] == "armed"
    assert "手工 approve" in out["note"]
    assert _account(desk) == before
    assert desk.book._fill_exists(ticket["ticket_id"]) is None


def test_expired_terminal_intent_cannot_be_revived_by_trigger_actor(desk, trigger):
    """已记 expired_untriggered 的票，连兑现回路也不许把它复活成交。"""
    trigger["on"] = True
    _symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    stored = desk.decisions.store.get_ticket(ticket["ticket_id"])
    stored["intent_terminal_state"] = "expired_untriggered"
    desk.decisions.store.save_ticket(stored)
    before = _account(desk)

    out = desk.decisions.approve(ticket["ticket_id"],
                                 actor=f"trigger:{ticket['ticket_id']}")
    assert out["status"] == "armed"
    assert "不得复活" in out["note"]
    assert _account(desk) == before


def test_past_valid_until_cannot_be_filled_even_by_trigger_actor(desk, trigger):
    """票跨过 valid_until 才轮到兑现时，approve 必须二次复核有效期。"""
    trigger["on"] = True
    condition = {"kind": "price", "op": "lte", "value": 10.0,
                 "valid_until": "2026-11-06T09:00:00+08:00"}
    _symbol, ticket = _propose(desk, condition=condition)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    before = _account(desk)

    out = desk.decisions.approve(ticket["ticket_id"],
                                 actor=f"trigger:{ticket['ticket_id']}")
    assert out["status"] == "armed"
    assert "valid_until" in out["note"]
    assert _account(desk) == before


def test_stale_contingency_ticket_expires_instead_of_arming(desk, trigger):
    """11-08 深审 B3：昨天的条件票今天 approve 必须直接 expired。

    arm 分支原本早于跨日检查，会把过期票先冻结成 armed、写一条 arm 日志；
    虽然再 approve 会撞跨日不出钱，但状态标签错、审计链也脏。
    """
    trigger["observe"] = True
    _symbol, ticket = _propose(desk)
    stored = desk.decisions.store.get_ticket(ticket["ticket_id"])
    stored["trade_date"] = "20200102"
    desk.decisions.store.save_ticket(stored)

    out = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert out["status"] == "expired"

    after = desk.decisions.store.get_ticket(ticket["ticket_id"])
    assert after["status"] == "expired"
    assert after.get("armed_at") is None
    assert desk.book._fill_exists(ticket["ticket_id"]) is None
    arms = [r for r in desk.paper.list_decisions(limit=100)
            if str(r.get("kind")) == "arm"
            and r.get("ticket_id") == ticket["ticket_id"]]
    assert arms == [], "过期票不该留下 arm 决策日志"


def test_both_flags_off_still_refuses_conditions(desk, trigger):
    """两个开关都关 = 原来的 fail-closed，行为一字不变。"""
    assert trigger["on"] is False and trigger["observe"] is False
    with pytest.raises(DecisionError) as err:
        _propose(desk)
    assert err.value.code == "TRIGGER_DISABLED"


def test_bad_condition_is_refused_before_any_ticket(desk, trigger):
    trigger["on"] = True
    with pytest.raises(DecisionError) as err:
        _propose(desk, condition={"kind": "volume_spike", "op": "gte", "value": 1})
    assert err.value.code == "BAD_CONTINGENCY"


def test_first_approve_arms_without_spending(desk, trigger):
    """两阶段 approve 的第一阶段：不出钱、不跑 guard 出单、不进 pending。"""
    trigger["on"] = True
    _symbol, ticket = _propose(desk)
    assert ticket["status"] == "proposed"
    before = _account(desk)

    result = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert result["status"] == "armed"
    assert _account(desk) == before
    assert desk.book._fill_exists(ticket["ticket_id"]) is None

    stored = desk.decisions.store.get_ticket(ticket["ticket_id"])
    assert stored["status"] == "armed"
    assert stored["decision_ts"] and stored["armed_at"]
    # D-7：决策价在 arm 当场冻结。真实帧可取就写 frame，取不到如实 unavailable；
    # 两种都合法，但绝不能出现「basis=frame 却没有价」这种假装有基准的状态。
    assert stored["decision_quote_basis"] in {"frame", "unavailable"}
    if stored["decision_quote_basis"] == "frame":
        assert stored["decision_quote"] and stored["decision_frame_id"]
        assert stored["decision_acquisition_ts"]
    else:
        assert stored["decision_quote"] is None
        assert stored["decision_frame_id"] is None
        assert stored.get("decision_price_reason")
    assert stored["price_ref"] == ticket["price_ref"]
    assert stored["contingency"] == _CONDITION


def test_second_approve_executes_the_same_ticket(desk, trigger):
    """触发兑现那次 approve 必须穿过 guard + execute_ticket，且票只有一张。"""
    trigger["on"] = True
    _symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    second = desk.decisions.approve(ticket["ticket_id"],
                                    actor=f"trigger:{ticket['ticket_id']}")
    assert second["status"] != "armed"
    assert second["status"] in {"filled", "queued", "rejected"}
    tickets = desk.decisions.list_tickets(limit=50, view="full")
    assert sum(1 for t in tickets if t["ticket_id"] == ticket["ticket_id"]) == 1


def test_arm_freezes_the_frame_price_and_the_second_approve_does_not_overwrite(
        desk, trigger, monkeypatch):
    """裁定 D-7：decision price 必须在决策时刻冻结，事后不许改写。"""
    from service.trading import replay

    monkeypatch.setattr(replay, "backfill_fields", lambda ticket, root: {
        "decision_quote": "9.87", "decision_quote_basis": "frame",
        "decision_frame_id": "100000_000001", "source_ts": "2026-11-06T10:00:00+08:00",
        "acquisition_ts": "2026-11-06T10:00:00.000001+08:00", "reason": None,
    })
    trigger["on"] = True
    _symbol, ticket = _propose(desk)
    armed = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert armed["status"] == "armed"
    stored = desk.decisions.store.get_ticket(ticket["ticket_id"])
    assert stored["decision_quote"] == "9.87"
    assert stored["decision_quote_basis"] == "frame"
    assert stored["decision_frame_id"] == "100000_000001"
    assert stored["decision_source_ts"] != stored["decision_acquisition_ts"]

    desk.decisions.approve(ticket["ticket_id"],
                           actor=f"trigger:{ticket['ticket_id']}")
    after = desk.decisions.store.get_ticket(ticket["ticket_id"])
    assert after["decision_quote"] == "9.87"          # 第二次 approve 不覆盖
    assert after["decision_ts"] == stored["decision_ts"]
    assert after["decision_frame_id"] == "100000_000001"


def test_a_failed_frame_read_never_blocks_the_arm(desk, trigger, monkeypatch):
    """帧读取失败只能退化成 unavailable + 原因，不能把合法批准变成 500。"""
    from service.trading import replay

    def broken(*_args, **_kwargs):
        raise RuntimeError("parquet gone")

    monkeypatch.setattr(replay, "backfill_fields", broken)
    trigger["on"] = True
    _symbol, ticket = _propose(desk)
    before = _account(desk)
    result = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert result["status"] == "armed"
    stored = desk.decisions.store.get_ticket(ticket["ticket_id"])
    assert stored["decision_quote"] is None
    assert stored["decision_quote_basis"] == "unavailable"
    assert stored["decision_price_reason"].startswith("replay_failed:")
    assert _account(desk) == before


def test_replay_of_the_same_request_id_returns_one_ticket(desk, trigger):
    trigger["on"] = True
    symbol = pick_symbols(desk, 1)[0]
    req = {**proposal(symbol), "contingency": _CONDITION,
           "client_request_id": "intent-replay-0001"}
    first = desk.decisions.propose(req, actor="agent")
    again = desk.decisions.propose(req, actor="agent")
    assert again["ticket_id"] == first["ticket_id"]


def test_switch_off_blocks_stale_armed_ticket(desk, trigger, tmp_path, monkeypatch):
    """v4 #17 高优先级验收：true→false 后，存量 armed 票命中帧也零账变。"""
    trigger["on"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    before = _account(desk)
    trigger["on"] = False
    monkeypatch.setattr(desk.book, "_try_fill",
                        lambda *a, **k: pytest.fail("_try_fill 不该被调用"))

    rows = watch._run_intents(desk, "20261106", _frame(symbol), now=_NOW,
                              base=tmp_path)
    assert rows and rows[0]["status"] == "triggered"     # 观察照常发生
    assert rows[0].get("promoted") is None                 # 但没兑现
    assert _account(desk) == before
    assert desk.book._fill_exists(ticket["ticket_id"]) is None
    assert desk.decisions.store.get_ticket(ticket["ticket_id"])["status"] == "armed"


def test_trigger_only_calls_approve_and_never_reproposes(desk, trigger, tmp_path,
                                                        monkeypatch):
    """命中只调 approve(ticket_id)，不重新 propose 生成第二张票。"""
    trigger["on"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")

    calls: list = []
    approve = desk.decisions.approve
    monkeypatch.setattr(
        desk.decisions, "approve",
        lambda tid, actor="agent": calls.append((tid, actor)) or approve(tid, actor=actor))
    monkeypatch.setattr(desk.decisions, "propose",
                        lambda *a, **k: pytest.fail("触发路径不得重新 propose"))

    rows = watch._run_intents(desk, "20261106", _frame(symbol), now=_NOW,
                              base=tmp_path)
    assert rows[0]["status"] == "triggered"
    assert len(calls) == 1
    assert calls[0][0] == ticket["ticket_id"]
    assert calls[0][1] == f"trigger:{ticket['ticket_id']}"


def test_untriggered_intent_leaves_the_account_alone(desk, trigger, tmp_path,
                                                     monkeypatch):
    """未命中 = pending，零账变；_try_fill 从未被调用。"""
    trigger["on"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    before = _account(desk)
    monkeypatch.setattr(desk.book, "_try_fill",
                        lambda *a, **k: pytest.fail("_try_fill 不该被调用"))

    rows = watch._run_intents(desk, "20261106", _frame(symbol, last=11.0),
                              now=_NOW, base=tmp_path)
    assert rows[0]["status"] == "pending"
    assert _account(desk) == before
    assert desk.decisions.store.get_ticket(ticket["ticket_id"])["status"] == "armed"


def test_watch_run_end_to_end_reports_the_terminal_state(desk, trigger, tmp_path):
    """整条盯盘回路：帧 → tripwire → reconcile → intent 轨迹 → 返回。"""
    trigger["on"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    trigger["on"] = False  # 只观察，避免端到端用例真的动账本
    path = tmp_path / "rt_snapshots" / "20261106" / "100000_000000.parquet"
    path.parent.mkdir(parents=True)
    _frame(symbol).to_parquet(path, index=False)

    shot = watch.run(desk, path, day="20261106", now=_NOW, base=tmp_path)
    assert shot["intent"] and shot["intent"][0]["status"] == "triggered"
    assert shot["intent"][0].get("promoted") is None
    assert "acted" in shot and "findings" in shot  # 旧返回字段一个不少


def test_kernel_answer_still_has_no_advice(desk, trigger, tmp_path):
    """意图回路不得让内核长出 action / advice / signal。"""
    trigger["on"] = True
    symbol, ticket = _propose(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="agent")
    trigger["on"] = False
    rows = watch._run_intents(desk, "20261106", _frame(symbol), now=_NOW,
                              base=tmp_path)
    blob = json.dumps(rows, ensure_ascii=False, default=str).lower()
    for word in ('"action"', '"advice"', '"signal"'):
        assert word not in blob
