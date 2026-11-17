"""Implementation shortfall: what the fill cost against the decision price.

Without a reference price a fill records what was paid but not what it was
measured from, so "did execution eat the edge" has no answer. These tests pin
three things: the field survives a snapshot round trip, the sign convention is
the trader's (positive = worse), and a fill booked by the desk carries the price
the decision was made against.
"""

from __future__ import annotations

from decimal import Decimal

from service.kernel.models import D, Fill, Side
from service.kernel.oms import OrderManager
from service.trading.tca import _bps, summary


def _fill(**kwargs) -> Fill:
    from datetime import datetime

    from service.kernel.clock import SHANGHAI
    base = {"fill_id": "f1", "order_id": "O1", "symbol": "600000.SH", "side": Side.BUY,
            "qty": 100, "price": D("10.10"), "fees": D("5.00"),
            "ts": datetime(2026, 10, 28, 9, 30, tzinfo=SHANGHAI),
            "rule_id": "CONTINUOUS", "ref_price": D("10.00")}
    base.update(kwargs)
    return Fill(**base)


def test_the_reference_price_survives_a_snapshot_round_trip():
    fill = _fill()
    restored = OrderManager.fill_from_dict(OrderManager.fill_to_dict(fill))
    assert restored == fill
    assert restored.ref_price == D("10.00")


def test_a_fill_without_a_reference_still_round_trips():
    fill = _fill(ref_price=None)
    restored = OrderManager.fill_from_dict(OrderManager.fill_to_dict(fill))
    assert restored.ref_price is None


def test_shortfall_is_signed_from_the_traders_point_of_view():
    # Paid 1% more than the decision price on a buy: 100 bps worse.
    assert _bps(D("10.10"), D("10.00"), "BUY") == 100.0
    # Sold 1% below the decision price: also 100 bps worse.
    assert _bps(D("9.90"), D("10.00"), "SELL") == 100.0
    # Paid below the decision price: a negative cost.
    assert _bps(D("9.90"), D("10.00"), "BUY") == -100.0


class _Store:
    def __init__(self, fills):
        self._fills = fills

    def recent_fills(self, limit=500):
        return self._fills[:limit]


class _Desk:
    def __init__(self, fills):
        self.paper = _Store(fills)


def _record(**kwargs):
    base = {"ticket_id": "t1", "symbol": "600000.SH", "side": "BUY",
            "qty": 100, "price": "10.10", "ref_price": "10.00", "fees": "5.00",
            "fill_basis": "next_open"}
    base.update(kwargs)
    return base


def test_summary_separates_shortfall_from_fees():
    report = summary(_Desk([_record()]))
    row = report["fills"][0]
    assert row["shortfall_bps"] == 100.0
    # ¥5 on ¥1,010 is 49.5 bps — the minimum commission, not a percentage.
    assert row["fee_bps"] == 49.5
    assert row["total_bps"] == 149.5
    assert report["overall"]["n"] == 1


def test_a_fill_without_a_reference_is_counted_not_dropped():
    report = summary(_Desk([_record(), _record(ticket_id="t2", ref_price=None)]))
    assert report["n"] == 1
    assert report["no_reference"] == 1
    assert any("no reference" in note for note in report["caveats"])


def test_sells_are_grouped_separately():
    report = summary(_Desk([
        _record(),
        _record(ticket_id="t2", side="SELL", price="9.90", ref_price="10.00"),
    ]))
    assert report["by_side"]["BUY"]["n"] == 1
    assert report["by_side"]["SELL"]["n"] == 1
    assert report["by_side"]["SELL"]["mean_shortfall_bps"] == 100.0


def _tradable_stock(desk) -> str:
    day = desk.market.latest_bar_date()
    daily = desk.market.daily(day)
    frame = daily[(daily["close"] > 3) & (daily["close"] < 9) & (daily["vol"] > 0)]
    frame = frame[~frame["ts_code"].astype(str).str.endswith(".BJ")]
    assert not frame.empty
    return str(frame.iloc[0]["ts_code"])


def test_a_booked_ticket_records_the_price_the_decision_used(desk):
    symbol = _tradable_stock(desk)
    day = desk.market.latest_bar_date()
    ticket = desk.decisions.propose({
        "symbol": symbol, "side": "BUY", "sleeve": "CORE", "qty": 100,
        "thesis": "买入一手验证成交参考价是否随票据落库，理由写满二十字以上。",
        "evidence": ["daily:" + day], "invalidation": "跌破买入价 3% 即离场",
        "persona": desk.paper.persona_current()["current"],
    }, actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")

    result = desk.book.execute_ticket(ticket)
    assert result.ok and result.status == "filled", result.reason or result.status

    record = desk.paper.get_fill(ticket["ticket_id"])
    assert record is not None
    assert Decimal(str(record["ref_price"])) == Decimal(str(ticket["price_ref"]))

    report = summary(desk)
    assert report["n"] >= 1
    assert report["fills"][0]["basis"] in {"close", "next_open", "intraday"}
