"""P3-4：9 项一致性、G1–G5 闸门、影子重放 fail-closed。"""

from __future__ import annotations

import pytest

from service.trading import parity, shadow
from service.trading.replay import QuoteFact


def _rows(n: int, *, price: float = 10.0, bad_item: str | None = None,
          violations: dict | None = None):
    rows = []
    for i in range(n):
        row = {"ticket_id": f"td-{i}", "symbol": "600000.SH", "side": "BUY",
               "qty": 100, "status": "filled", "fill_basis": "close",
               "price": price, "fees": 5.0, "cash_after": 4000.0}
        if bad_item:
            row[bad_item] = "changed"
        row.update(violations or {})
        rows.append(row)
    return rows


def _full_gate(comparison, **overrides):
    params = {"trading_days": 10,
              "regime_counts": {"主升": 30, "分歧": 30, "退潮": 30},
              "real_fills": 25}
    params.update(overrides)
    return parity.gate(comparison, **params)


def test_nine_items_compare_exactly_when_engines_agree():
    out = parity.compare(_rows(120), _rows(120))
    assert out["n"] == 120 and out["consistency_rate"] == 1.0
    assert out["median_price_gap_bps"] == 0.0 and out["pnl_error_pct"] == 0.0
    assert all(v == 0 for v in out["hard_violations"].values())
    assert out["unmatched_live"] == [] and out["unmatched_shadow"] == []
    assert len(out["comparisons"]) == 120


def test_one_different_item_lowers_the_rate_by_exactly_one_cell():
    out = parity.compare(_rows(10), _rows(10, bad_item="fill_basis"))
    assert out["mismatches"]["fill_basis"] == 10
    assert round(out["consistency_rate"], 6) == round(1 - 10 / 90, 6)


def test_all_four_hard_violations_are_counted():
    bad = {"duplicate_fill": 1, "impossible_fill": 1,
           "lookahead": 1, "order_state_mismatch": 1}
    out = parity.compare(_rows(5, violations=bad), _rows(5))
    assert all(out["hard_violations"][k] == 5 for k in bad)


def test_g4_middle_states_are_explicit():
    assert _full_gate(parity.compare(_rows(50), _rows(50)))["status"] == "no_parity"
    assert (_full_gate(parity.compare(_rows(80), _rows(80)))["status"]
            == "insufficient_shadow_samples")


def test_g1_and_g2_and_g3_block_before_promotion():
    full = parity.compare(_rows(120), _rows(120))
    assert _full_gate(full, trading_days=5)["status"] == "blocked"
    assert _full_gate(full, regime_counts={"a": 30, "b": 30})["status"] == "blocked"
    blocked = parity.compare(_rows(120, violations={"lookahead": 1}), _rows(120))
    assert _full_gate(blocked)["status"] == "blocked"
    low = parity.compare(_rows(120), _rows(120, bad_item="status"))
    assert _full_gate(low)["status"] == "blocked"


def test_g5_never_activates_without_a_human():
    full = parity.compare(_rows(120), _rows(120))
    out = _full_gate(full)
    assert out["status"] == "promotable" and out["passed"] is True
    assert out["activated"] is False
    assert out["gates"]["intent_parity_g5"]["human_activated"] is False

    activated = _full_gate(full, human_activated=True)
    assert activated["status"] == "activated" and activated["activated"] is True
    warned = _full_gate(full, trigger_enabled=True)
    assert "warning" in warned["gates"]["intent_parity_g5"]


def test_g1_real_fill_guard_does_not_block_offline_parity():
    out = _full_gate(parity.compare(_rows(120), _rows(120)), real_fills=3)
    assert out["gates"]["intent_parity_g1_real"]["status"] == "insufficient_real_fills"
    assert out["gates"]["intent_parity_g1_real"]["blocks_parity"] is False
    assert out["status"] == "promotable"


def test_gate_names_are_isolated_from_research_shadow_keys():
    out = _full_gate(parity.compare(_rows(120), _rows(120)))
    assert all(key.startswith("intent_parity_") for key in out["gates"])
    assert "research.shadow_gate" not in str(out["gates"])


def test_shadow_replay_is_fail_closed_and_writes_nothing(tmp_path):
    fact = QuoteFact("600000.SH", "20261106", "100000_000001", 10.5,
                     "2026-11-06T10:00:00+08:00",
                     "2026-11-06T10:00:00.000001+08:00", "frame", None)
    out = shadow.replay_decisions(
        [{"ticket_id": "td-1", "symbol": "600000.SH", "trade_date": "20261106",
          "decision_ts": "2026-11-06T10:00:00+08:00"},
         {"ticket_id": "td-2", "symbol": "600000.SH"}],  # 缺 provenance
        root=tmp_path, quote_at=lambda day, ts, symbol: fact,
    )
    assert out["n"] == 2 and out["unscorable"] == 1
    assert out["rows"][0]["shadow_status"] == "scored"
    assert out["rows"][0]["shadow_decision_price"] == 10.5
    assert out["rows"][1]["shadow_status"] == shadow.UNSCORABLE
    assert "不写账本" in out["note"]


def test_shadow_replay_failure_is_visible_not_silently_neutral(tmp_path):
    def broken(*_a, **_k):
        raise RuntimeError("parquet gone")

    out = shadow.replay_decisions(
        [{"ticket_id": "td-1", "symbol": "600000.SH", "trade_date": "20261106",
          "decision_ts": "2026-11-06T10:00:00+08:00"}],
        root=tmp_path, quote_at=broken,
    )
    assert out["unscorable"] == 1
    assert "replay_failed" in out["rows"][0]["reason"]
