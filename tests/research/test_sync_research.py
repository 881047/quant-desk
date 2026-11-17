"""The weekly sync must stay idle outside a disclosure season."""

from __future__ import annotations

from scripts.sync_research_data import _due


def test_breached_coverage_is_due():
    assert _due({"available": True, "sla_breached": True,
                 "days_since_announcement": 90}) is True


def test_recently_disclosed_report_is_due():
    assert _due({"available": True, "sla_breached": False,
                 "days_since_announcement": 8}) is True


def test_quiet_period_is_not_due():
    assert _due({"available": True, "sla_breached": False,
                 "days_since_announcement": 100}) is False


def test_unknown_state_is_a_reason_to_check_not_to_skip():
    assert _due({"available": False, "reason": "rewrite in progress"}) is True


def test_only_a_new_reporting_period_restarts_the_cursor():
    from scripts.sync_research_data import _new_cycle

    assert _new_cycle({}, "20260930") is True
    assert _new_cycle({"cycle_period": "20260930"}, "20260930") is False
    assert _new_cycle({"cycle_period": "20260630"}, "20260930") is True
    assert _new_cycle({"cycle_period": "20260930"}, "") is False
