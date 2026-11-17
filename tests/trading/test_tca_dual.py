"""P3-1：并列两个 shortfall 基准。不许合并、不许把差额说成收益。"""

from __future__ import annotations

from service.trading import tca


class FakePaper:
    def __init__(self, fills, tickets=None):
        self._fills = fills
        self._tickets = tickets or {}

    def recent_fills(self, limit):
        return self._fills[:limit]

    def get_ticket(self, ticket_id):
        return self._tickets.get(ticket_id)


class FakeDesk:
    def __init__(self, fills, tickets=None):
        self.paper = FakePaper(fills, tickets)


def _fill(**overrides):
    row = {"ticket_id": "td-1", "symbol": "600000.SH", "side": "BUY",
           "qty": 100, "price": "10.10", "ref_price": "10.00",
           "fees": "0", "limit_up_penalty": "0", "fill_basis": "close"}
    row.update(overrides)
    return row


def test_two_benchmarks_are_parallel_and_total_still_uses_the_old_one():
    desk = FakeDesk([_fill()], {"td-1": {"decision_quote": "10.05",
                                         "decision_quote_basis": "frame"}})
    report = tca.summary(desk)
    row = report["fills"][0]
    assert row["shortfall_bps"] == row["shortfall_vs_ref_bps"] == 100.0
    assert round(row["shortfall_vs_quote_bps"], 2) == 49.75
    assert row["decision_latency_bps"] == 50.0
    assert row["total_bps"] == 100.0          # 旧口径没被新基准顶替
    assert row["decision_quote_basis"] == "frame"
    assert report["no_reference"] == 0 and report["no_quote_reference"] == 0
    assert report["overall"]["n_with_quote"] == 1


def test_a_missing_frame_quote_is_null_not_zero():
    desk = FakeDesk([_fill()], {"td-1": {}})
    report = tca.summary(desk)
    row = report["fills"][0]
    assert row["shortfall_vs_quote_bps"] is None
    assert row["decision_quote_basis"] == "unavailable"
    assert row["decision_latency_bps"] is None
    assert report["no_quote_reference"] == 1
    assert report["overall"]["mean_shortfall_vs_quote_bps"] is None
    assert any("never zero" in note for note in report["caveats"])


def test_unavailable_basis_never_becomes_a_price():
    """票上有数但 basis=unavailable —— D-7 明确说那是不能当成参考价的。"""
    desk = FakeDesk([_fill()], {"td-1": {"decision_quote": "10.05",
                                         "decision_quote_basis": "unavailable"}})
    assert tca.summary(desk)["fills"][0]["shortfall_vs_quote_bps"] is None


def test_the_fill_payload_can_carry_the_quote_without_a_ticket_lookup():
    desk = FakeDesk([_fill(decision_quote="10.05",
                           decision_quote_basis="frame")])
    assert tca.summary(desk)["fills"][0]["shortfall_vs_quote_bps"] is not None


def test_opportunity_cost_and_no_reference_semantics_are_unchanged():
    desk = FakeDesk([_fill(), _fill(ticket_id="td-2", ref_price=None)])
    report = tca.summary(desk)
    assert report["n"] == 1 and report["no_reference"] == 1
    assert any("opportunity cost is not measurable" in note for note in report["caveats"])
    assert any("PARALLEL benchmarks" in note for note in report["caveats"])
    assert any("DIFFERENT LAYER" in note for note in report["caveats"])


def test_sell_side_keeps_the_signed_cost_convention():
    desk = FakeDesk([_fill(side="SELL", price="9.90")],
                    {"td-1": {"decision_quote": "9.95",
                              "decision_quote_basis": "frame"}})
    row = tca.summary(desk)["fills"][0]
    assert row["shortfall_vs_ref_bps"] == 100.0
    assert row["shortfall_vs_quote_bps"] > 0
    assert row["decision_latency_bps"] == 50.0


def test_zero_or_negative_quote_is_not_a_benchmark():
    desk = FakeDesk([_fill()], {"td-1": {"decision_quote": "0",
                                         "decision_quote_basis": "frame"}})
    assert tca.summary(desk)["fills"][0]["shortfall_vs_quote_bps"] is None
