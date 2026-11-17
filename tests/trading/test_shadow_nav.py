"""P3-3：双净值只给上界，arena 双阈值分别标注。"""

from __future__ import annotations

import json

from service.trading import shadow_nav


def _fills(n: int = 1, *, with_quote: bool = True, side: str = "BUY"):
    out = []
    for i in range(n):
        row = {"ticket_id": f"td-{i}", "side": side, "qty": 100,
               "price": "10.10", "fees": "5"}
        if with_quote:
            row.update({"decision_quote": "10.00",
                        "decision_quote_basis": "frame"})
        out.append(row)
    return out


def test_two_cash_legs_and_the_gap():
    out = shadow_nav.dual_nav(_fills())
    assert out["ideal_cash_delta_cny"] == -1000.0
    assert out["strict_cash_delta_cny"] == -1015.0
    assert out["gap_cny"] == 15.0 and out["gap_bps"] == 150.0
    assert out["ideal_nav"] == 4000.0 and out["strict_nav"] == 3985.0


def test_sell_side_reverses_the_sign_consistently():
    out = shadow_nav.dual_nav(_fills(side="SELL"))
    assert out["ideal_cash_delta_cny"] == 1000.0
    assert out["strict_cash_delta_cny"] == 1005.0
    assert out["gap_cny"] == -5.0   # 卖在更高价：严格腿反而更好，漏损为负


def test_missing_frame_quote_is_counted_not_rendered_as_zero():
    out = shadow_nav.dual_nav(_fills(with_quote=False))
    assert out["no_quote_reference"] == 1
    assert out["n_with_quote"] == 0
    assert out["arena"]["measured"] is False
    assert out["arena"]["ci_low"] is None
    assert out["ideal_cash_delta_cny"] == 0.0


def test_arena_thresholds_are_reported_separately():
    assert shadow_nav.arena_guard(5)["measured"] is False
    assert shadow_nav.arena_guard(5)["may_claim_edge"] is False
    mid = shadow_nav.arena_guard(15)
    assert mid["measured"] is True and mid["may_claim_edge"] is False
    assert mid["ci_position"] == "sample_below_20"
    high = shadow_nav.arena_guard(25)
    assert high["may_claim_edge"] is True
    assert high["thresholds"] == {"no_interval_below": 10,
                                  "no_edge_below": 20, "promotion_min": 20}


def test_no_verdict_and_the_upper_bound_is_stated():
    out = shadow_nav.dual_nav(_fills(25))
    blob = json.dumps(out, ensure_ascii=False).lower()
    for word in ('"verdict"', '"action"', '"advice"', '"signal"'):
        assert word not in blob
    assert any("上界" in note for note in out["caveats"])
    assert any("not measurable" in note for note in out["caveats"])
    assert any("不以净值曲线变好" in note for note in out["caveats"])


def test_denominator_keeps_the_two_kinds_of_missing_fills_apart():
    out = shadow_nav.dual_nav(_fills(2))
    assert out["denominator"]["fills"] == 2
    assert out["denominator"]["untriggered_intents_excluded"] is True
    assert out["denominator"]["partial_fill_remainder"] == "not measurable"


def test_empty_input_is_still_an_honest_answer():
    out = shadow_nav.dual_nav([])
    assert out["n_fills"] == 0 and out["gap_bps"] is None
    assert out["arena"]["measured"] is False
