"""P0/P1-a in the ledger: new read-only fields, and the verdict stays intact."""

from __future__ import annotations

import json

from service.trading import build_desk, knowledge

REASONS = ["2x costs kill it: Sharpe -4.00",
           "deflated Sharpe 0.000 < 0.90 after 150 trials",
           "capacity: needs CNY 52,603"]

RESULT_6 = {
    "gate_reading": "rejected",
    "reasons": REASONS,
    "spec": {"pool": "limit_up", "entry": "t_close", "hold": 1},
    "best": {"metrics": {"sharpe": 7.0439},
             "trades": {"n": 44446, "mean_excess": 0.007547, "p_value": 0.0005}},
    "capacity": {"implementable": False, "account_equity": 5000.0,
                 "capital_one_lot_each": 52603.0},
    "stress": {"2x": {"sharpe": -3.9974}, "3x": {"sharpe": -3.6589}},
    "cpcv": {"oos_sharpe_p05": 4.1205, "oos_sharpe_mean": 7.4106},
    "dsr": {"dsr": 0.0}, "pbo": {"pbo": 0.0},
}


def _fake_desk():
    class Paper:
        def list_hypotheses(self, limit=200):
            return [
                {"id": 6, "status": "rejected", "statement": "t_close",
                 "result": json.dumps(RESULT_6)},
                {"id": 5, "status": "rejected", "statement": "old format",
                 "result": json.dumps({"metrics": {}, "notes": "legacy"})},
            ]

    class Desk:
        paper = Paper()

    return Desk()


def _row(out, hid):
    return next(r for r in out["measured"] if r.get("id") == hid)


def test_summary_is_light_and_keeps_the_optimistic_reference():
    out = knowledge.ledger(_fake_desk())
    assert out["detail"] == "summary"
    row6 = _row(out, 6)
    assert set(row6["fill_scope"]) == {"status", "entry", "pool",
                                       "unbiased_reading", "detail"}
    assert row6["fill_scope"]["status"] == "optimistic"
    assert row6["alpha_reading"]["signal_sharpe"] == 7.0439
    assert row6["alpha_reading"]["sharpe_at_2x_cost"] == -3.9974
    assert row6["alpha_reading"]["collapses_between_1x_and_2x"] is True
    ref = out["fill_scope_reference"]["t_close|limit_up"]
    assert ref["status"] == "optimistic" and len(ref["unfillable_legs"]) == 3
    assert "unfillable_legs" not in out["fill_scope_reference"]["|"]


def test_full_detail_expands_every_row():
    summary = knowledge.ledger(_fake_desk())
    full = knowledge.ledger(_fake_desk(), detail="full")
    assert full["detail"] == "full"
    assert len(json.dumps(full, ensure_ascii=False)) > len(
        json.dumps(summary, ensure_ascii=False))
    row6 = _row(full, 6)
    assert "reading_guard" in row6["fill_scope"]
    assert "signal_book" in row6["alpha_reading"]
    assert row6["alpha_reading"]["signal_book"]["basis"]


def test_verdict_fields_are_untouched():
    """The whole point of P0: attribution must not rewrite the verdict."""
    out = knowledge.ledger(_fake_desk())
    row6 = _row(out, 6)
    assert row6["gate_reading"] == "rejected"
    assert row6["reasons"] == REASONS
    assert row6["spec"] == RESULT_6["spec"]
    assert out["fill_gap"]["optimistic_fill_count"] == 1
    assert out["fill_gap"]["hypotheses"][0]["id"] == 6
    assert "只读" in out["fill_gap"]["note"]


def test_old_format_row_does_not_claim_a_clean_fill():
    row5 = _row(knowledge.ledger(_fake_desk()), 5)
    assert row5["fill_scope"]["status"] == "unknown_spec"
    assert row5["alpha_reading"]["status"] == "not_stored"


def test_breadth_topic_is_registered_and_answerable():
    assert "breadth" in knowledge.MARKET_TOPICS
    assert "breadth" in knowledge._needs_symbol("analog")["market_topics"]
    cards = knowledge.cards(_fake_desk())
    assert any(t["topic"] == "breadth"
               for layer in cards["layers"] for t in layer["topics"])


def test_real_ledger_stays_under_the_host_body_ceiling():
    """A response over ~16 KB is dropped whole by the host, not truncated."""
    out = knowledge.ledger(build_desk())
    payload = json.dumps(out, ensure_ascii=False, allow_nan=False)
    assert len(payload) < 16000, f"ledger summary is {len(payload)} bytes"
    # The pytest desk may point at an isolated empty paper.sqlite, so assert
    # the display contract here and leave the id=6 gap to the synthetic test.
    assert out["detail"] == "summary"
    assert "fill_scope_reference" in out and "alpha_reading_note" in out
