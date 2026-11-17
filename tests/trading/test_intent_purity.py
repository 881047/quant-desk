"""P0 意图层：求值必须纯、拒绝必须显式、Shadow 绝不兑现。

这一层是资金路径之前的护栏。任何一条测试变红，都意味着「意图」有可能
绕过两阶段 approve 直接变成订单，或者把看不见的数据当成没触发。
"""

from __future__ import annotations

import dataclasses

import pytest

from service import settings
from service.trading import intent


def test_feature_flag_is_declared_and_off_by_default():
    """开关缺键即启动失败；缺省必须关，由人激活。"""
    assert "trigger.enabled" in settings.SPECS
    assert settings.flag("trigger.enabled") is False


def test_observe_is_a_separate_flag_and_cannot_enable_execution():
    """observe 只是影子求值；它开着也不等于资金开关开着。"""
    assert "trigger.observe" in settings.SPECS
    # 资金路径的硬约束：无论 observe 将来是否被激活，兑现开关缺省必须关。
    assert settings.flag("trigger.enabled") is False


def test_price_condition_triggers_only_when_met():
    c = {"kind": "price", "op": "lte", "value": 10.0}
    assert intent.evaluate(c, {"last": 9.5})["state"] == "triggered"
    assert intent.evaluate(c, {"last": 11.0})["state"] == "pending"
    assert intent.evaluate({"kind": "price", "op": "gte", "value": 10.0},
                           {"price": 10.2})["state"] == "triggered"


def test_zero_price_is_denied_not_pending():
    """停牌 / 缺报价常写成 0。看不见 ≠ 没触发，必须显式 denied 留证。"""
    out = intent.evaluate({"kind": "price", "op": "lte", "value": 10.0}, {"last": 0})
    assert out["state"] == "denied"
    assert out["reason"].startswith("unobservable")


def test_missing_and_unsupported_conditions_are_denied_with_a_reason():
    assert intent.evaluate(None, {"last": 1})["reason"] == "contingency_missing_or_malformed"
    out = intent.evaluate({"kind": "volume_spike", "op": "gte", "value": 1}, {"last": 1})
    assert out["state"] == "denied" and out["reason"].startswith("unsupported_kind")
    out = intent.evaluate({"kind": "price", "op": "lte", "value": "abc"}, {"last": 1})
    assert out["state"] == "denied"


def test_expired_condition_does_not_trigger_even_if_the_price_matches():
    """过期票即使这一帧价格命中也不算触发，否则昨天的票今天会被兑现。"""
    c = {"kind": "price", "op": "lte", "value": 10.0,
         "valid_until": "2026-11-06T10:00:00+08:00"}
    out = intent.evaluate(c, {"last": 9.0}, {"at": "2026-11-06T10:01:00+08:00"})
    assert out["state"] == "expired_untriggered"


def test_frame_time_uses_only_the_callers_clock():
    c = {"kind": "frame_time", "op": "gte", "value": "093500"}
    assert intent.evaluate(c, {}, {"at": "2026-11-06T09:34:00+08:00"})["state"] == "pending"
    assert intent.evaluate(c, {}, {"at": "2026-11-06T09:36:00+08:00"})["state"] == "triggered"
    assert intent.evaluate(c, {})["state"] == "denied"  # 无帧时钟不可求值


def test_contingency_is_frozen():
    """帧扫描不许改写模型写好的条件。"""
    c = intent.Contingency(kind="price", op="lte", value=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.value = 9


def test_run_writes_shadow_trail_and_never_touches_the_desk(monkeypatch):
    """求值层只落 shadow 轨迹：pending 不落，desk 一次都不能被读。"""
    captured: dict = {}
    monkeypatch.setattr(
        intent.alert_log, "append",
        lambda day, records, base=None, *, scope: captured.update(
            scope=scope, rows=list(records)) or len(records),
    )

    class ForbiddenDesk:
        def __getattribute__(self, name):
            raise AssertionError("intent.run 在 Shadow 阶段不得触碰 desk")

    tickets = [
        {"ticket_id": "td-1", "symbol": "600000.SH", "qty": 100,
         "contingency": {"kind": "price", "op": "lte", "value": 10.0}},
        {"ticket_id": "td-2", "symbol": "600001.SH", "qty": 100,
         "contingency": {"kind": "price", "op": "gte", "value": 20.0}},
        {"ticket_id": "td-3", "symbol": "600002.SH", "qty": 100,
         "contingency": {"kind": "price", "op": "lte", "value": 10.0,
                         "valid_until": "2026-11-06T09:00:00+08:00"}},
    ]
    rows = intent.run(
        ForbiddenDesk(), day="20261106", frame_row={"last": 9.0},
        session_state={"at": "2026-11-06T09:40:00+08:00"}, tickets=tickets,
    )
    assert [r["status"] for r in rows] == ["triggered", "pending", "expired_untriggered"]
    assert captured["scope"] == intent.SCOPE == "shadow"
    assert len(captured["rows"]) == 2           # pending 不是终态，不落盘
    assert {r["state"] for r in captured["rows"]} == {"triggered", "expired_untriggered"}
    assert all(r["ticket_id"] for r in captured["rows"])


def test_explicit_base_still_separates_shadow_from_paper(tmp_path):
    """上游 ``alert_log._base`` 在显式 base 下忽略 scope，隔离必须由本模块补上。"""
    from service.notify import alert_log

    assert (alert_log.alerts_path("20261106", base=tmp_path, scope="shadow")
            == alert_log.alerts_path("20261106", base=tmp_path, scope="paper"))
    intent.run(
        None, day="20261106", base=tmp_path, frame_row={"last": 9.0},
        session_state={"at": "2026-11-06T09:40:00+08:00"},
        tickets=[{"ticket_id": "td-probe", "symbol": "600000.SH", "qty": 100,
                  "contingency": {"kind": "price", "op": "lte", "value": 10.0}}],
    )
    assert (tmp_path / "shadow" / "20261106_alerts.jsonl").is_file()
    assert not (tmp_path / "20261106_alerts.jsonl").exists()  # paper 不被污染


def test_actor_for_points_back_to_the_intent_ticket():
    """审计链要能看出「不是模型当场下的单」。"""
    assert intent.actor_for("td-20261106-abcd") == "trigger:td-20261106-abcd"
