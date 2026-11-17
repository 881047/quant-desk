"""P0: the fill assumption must be visible, and must not go stale silently."""

from __future__ import annotations

import pathlib

from service.research import fill_scope as fs

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_quoted_source_phrases_still_exist():
    """The displayed numbers are transcribed, not estimated. If the fill model
    or the board split changes, this test must fail instead of leaving a stale
    claim in the ledger."""
    spec = (ROOT / "service/research/spec.py").read_text(encoding="utf-8")
    board = (ROOT / "service/trading/board.py").read_text(encoding="utf-8")
    for phrase in ("乐观", "0.34%", "7.01%", "买入之后才知道", "buyable / one_word"):
        assert phrase in spec, f"spec.py 里的成交口径注释变了: {phrase}"
    for phrase in ("8.9%", "5.3%", "85.8%", "一字", "buyable"):
        assert phrase in board, f"board.py 的封板分组注释变了: {phrase}"


def test_t_close_limit_up_is_flagged_as_optimistic():
    out = fs.fill_scope("t_close", "limit_up")
    assert out["status"] == "optimistic"
    assert out["unbiased_reading"]["value_pct"] == -0.34
    assert len(out["unfillable_legs"]) == 3
    one_word = next(k for k in out["unfillable_legs"] if k["kind"] == "one_word")
    assert one_word["buyable"] is False
    assert "fill_model" in out["reading_guard"]
    assert out["note"].startswith("只读展示层")


def test_plain_entries_and_fail_closed_cases():
    nxt = fs.fill_scope("next_open", "limit_up")
    assert nxt["status"] == "documented" and nxt["known_bias"] is None
    assert nxt["unfillable_legs"][0]["buyable"] is False
    assert fs.fill_scope("t_close", "broken_board")["status"] == "documented"
    assert fs.fill_scope("next_limit", "limit_up")["status"] == "documented"
    assert fs.fill_scope("next_close", "limit_up")["status"] == "documented"
    assert fs.fill_scope(None)["status"] == "unknown_spec"
    assert fs.fill_scope("something_else")["status"] == "unsupported_entry"


def test_slim_scope_and_gap_only_lists_optimistic_rows():
    full = fs.fill_scope("t_close", "limit_up")
    slim = fs.slim_scope(full)
    assert set(slim) == {"status", "entry", "pool", "unbiased_reading", "detail"}
    assert "unfillable_legs" not in slim and "reading_guard" not in slim
    assert fs.scope_key("t_close", "limit_up") == "t_close|limit_up"

    rows = [
        {"id": 1, "status": "rejected", "evidence": {"mean_excess": -0.001},
         "fill_scope": fs.slim_scope(fs.fill_scope("next_open", "limit_up"))},
        {"id": 6, "status": "rejected", "evidence": {"mean_excess": 0.007547},
         "fill_scope": full},
    ]
    gap = fs.scope_gap(rows)
    assert gap["optimistic_fill_count"] == 1
    assert gap["hypotheses"][0]["id"] == 6
    assert "reasons" in gap["note"]


def test_no_verdict_or_signal_fields():
    for entry in ("t_close", "next_open", "next_limit", "next_close"):
        out = fs.fill_scope(entry, "limit_up")
        for forbidden in ("action", "verdict", "signal", "score",
                          "ranking", "target_price"):
            assert forbidden not in out
