"""手动同步：一个没被读到的小数点，会让每一根止损线都站错位置。"""

from __future__ import annotations

import pytest

from service.trading import manual, timeline
from service.trading.external import ExternalStore


def _store(tmp_path) -> ExternalStore:
    return ExternalStore(tmp_path / "external.sqlite")


def test_a_code_that_cannot_be_resolved_is_named():
    rows, problems, _ = manual.clean_positions([{"symbol": "700", "qty": 100, "cost": 9}])
    assert rows == []
    assert any("700" in p for p in problems)


def test_zero_quantity_means_sold_not_an_error():
    rows, problems, _ = manual.clean_positions([
        {"symbol": "000700", "qty": 0, "cost": 13.07},
        {"symbol": "000590", "qty": 200, "cost": 9.035},
    ])
    assert [r["symbol"] for r in rows] == ["000590.SZ"]
    assert problems == []


def test_duplicate_rows_are_refused_instead_of_doubling_the_position():
    rows, problems, _ = manual.clean_positions([
        {"symbol": "000590", "qty": 200, "cost": 9.0},
        {"symbol": "000590.SZ", "qty": 200, "cost": 9.0},
    ])
    assert len(rows) == 1
    assert any("重复" in p for p in problems)


def test_missing_cost_falls_back_to_the_price_with_a_warning():
    rows, problems, warnings = manual.clean_positions(
        [{"symbol": "000590", "qty": 200}], prices={"000590.SZ": 8.16})
    assert rows[0]["cost"] == 8.16
    assert any("成本价留空" in w for w in warnings)
    assert problems == []


def test_a_missed_decimal_is_refused_before_it_reaches_the_ledger():
    """1624 写成 162.40 时，止损会算在十分之一的价格上 —— 这一笔不能写。"""
    plan = manual.prepare(as_of="20261027",
                          positions=[{"symbol": "000590", "qty": 200, "cost": 9.035,
                                      "last": 8.16}],
                          total="4795.56", cash="3173.37", market_value="162.40")
    assert plan["ok"] is False
    assert any("面板市值" in p for p in plan["problems"])


def test_consistent_numbers_pass_and_carry_the_panel_value():
    plan = manual.prepare(as_of="2026-10-27",
                          positions=[{"symbol": "000590", "qty": 200, "cost": 9.035,
                                      "last": 8.16}],
                          total="4795.56", cash="3173.37", market_value="1632.00")
    assert plan["ok"] is True
    assert plan["as_of"] == "20261027"
    assert plan["market_value"] == 1632.0
    assert plan["computed_market_value"] == 1632.0


def test_cash_plus_value_short_of_equity_is_a_warning_not_a_block():
    plan = manual.prepare(as_of="20261027",
                          positions=[{"symbol": "000590", "qty": 200, "cost": 9.035,
                                      "last": 8.16}],
                          total="5000.00", cash="3000.00", market_value="1632.00")
    assert plan["ok"] is True
    assert any("冻结" in w for w in plan["warnings"])


def test_saving_a_full_sell_removes_the_row_instead_of_leaving_a_ghost(tmp_path):
    store = _store(tmp_path)
    first = manual.prepare(as_of="20261027",
                           positions=[{"symbol": "000700", "qty": 100, "cost": 13.07,
                                       "last": 13.07}])
    manual.persist(store, first)
    assert set(store.snapshot()) == {"000700.SZ"}

    second = manual.prepare(as_of="20261027", positions=[])
    assert second["ok"] is True
    manual.persist(store, second)
    assert store.snapshot() == {}, "卖光之后快照里不该还留着 100 股"
    assert store.snapshot_day() == "20261027"


def test_persist_writes_the_day_totals_too(tmp_path):
    store = _store(tmp_path)
    plan = manual.prepare(as_of="20261027", positions=[], total="4795.56",
                          cash="4793.56", market_value="0")
    manual.persist(store, plan)
    day = store.days(1)[0]
    assert day["as_of"] == "20261027"
    assert day["equity"] == pytest.approx(4795.56)
    assert day["cash"] == pytest.approx(4793.56)


def test_persist_refuses_a_plan_with_problems(tmp_path):
    store = _store(tmp_path)
    plan = manual.prepare(as_of="20261027",
                          positions=[{"symbol": "000590", "qty": 200, "cost": 9.0,
                                      "last": 8.16}],
                          market_value="162.40")
    assert plan["ok"] is False
    result = manual.persist(store, plan)
    assert result["ok"] is False
    assert store.snapshot() == {}


@pytest.mark.parametrize("kind,payload,needle", [
    ("fill", {"symbol": "000590.SZ", "side": "SELL", "qty": 200, "price": 8.12,
              "fees": 5.0}, "000590.SZ SELL 200 股"),
    ("proposal_blocked", {"code": "PAPER_ONLY", "message": "实盘袖未开启"},
     "PAPER_ONLY"),
    ("memo", {"id": 7, "topic": "明天的计划"}, "#7 明天的计划"),
])
def test_the_timeline_says_the_same_thing_everywhere(kind, payload, needle):
    described = timeline.describe(kind, payload)
    assert needle in (described["title"] + described["detail"])
