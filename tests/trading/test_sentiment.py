"""The sentiment panel: four if-statements that must not drift.

It is the only input a hot-money decision has besides prices, and it used to be
computed in a script that only ran during a session — so the evening answer was
"no panel". These tests pin the label boundaries and the fact that the panel can
be computed from stored pools, with no live quote anywhere.
"""

from __future__ import annotations

import pandas as pd

from service.trading import sentiment as sed


def _pool(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_tone_boundaries():
    assert sed.tone(0, 0, 0, 0) == "冰点"
    assert sed.tone(20, 5, 30, 2) == "退潮"          # more limit-downs than ups
    assert sed.tone(30, 25, 1, 2) == "分歧"          # break_rate 0.45+
    assert sed.tone(55, 23, 26, 4) == "主升"         # 4 boards and 40+ ups
    assert sed.tone(30, 5, 1, 2) == "修复"


def test_panel_reads_a_dataframe():
    pools = {
        "limit_up_pool": _pool([{"boards": 3, "seal_amount": 1e8},
                                {"boards": 1, "seal_amount": 2e8}]),
        "broken_board_pool": _pool([{"boards": 1}]),
        "limit_down_pool": _pool([]),
    }
    panel = sed.panel(pools)
    assert panel["limit_up"] == 2
    assert panel["broken_board"] == 1
    assert panel["max_boards"] == 3
    assert panel["seal_amount_cny"] == 3e8
    assert panel["break_rate"] == round(1 / 3, 4)
    assert panel["height_ok"] is True


def test_panel_also_reads_records():
    """The HTTP layer hands it list[dict]; the daily job hands it DataFrames."""
    pools = {
        "limit_up_pool": [{"boards": 4, "seal_amount": 5e7}],
        "broken_board_pool": [],
        "limit_down_pool": [{"boards": 1}],
    }
    panel = sed.panel(pools)
    assert panel == sed.panel({
        "limit_up_pool": _pool([{"boards": 4, "seal_amount": 5e7}]),
        "broken_board_pool": _pool([]),
        "limit_down_pool": _pool([{"boards": 1}]),
    })


def test_panel_without_pools_is_cold_not_crashing():
    panel = sed.panel({"limit_up_pool": None, "broken_board_pool": None,
                       "limit_down_pool": None})
    assert panel["limit_up"] == 0
    assert panel["tone"] == "冰点"
    assert panel["break_rate"] is None


def test_gates_name_the_failing_condition():
    """'The tape is cold' and 'the height is missing' are different decisions."""
    warm = {"tone": "主升", "break_rate": 0.29, "max_boards": 4, "height_ok": True}
    assert sed.gates(warm)["ok"] is True

    short = {"tone": "修复", "break_rate": 0.20, "max_boards": 2, "height_ok": False}
    out = sed.gates(short)
    assert out["ok"] is False
    assert out["failed"] == ["height_ok"]

    cold = {"tone": "冰点", "break_rate": 0.60, "max_boards": 1, "height_ok": False}
    assert set(sed.gates(cold)["failed"]) == {"tone_ok", "break_rate_ok", "height_ok"}


def test_no_panel_is_not_a_pass_but_also_not_a_verdict():
    """A missing partition is "cannot judge", never "no trade today".

    `ok=False` is this desk's sentence for "there is no short-term trade" — the
    skill tells the model to stop reading there. A pipeline that never ran must
    not be able to say that sentence, or a data outage reads as a market call.
    """
    out = sed.gates(None)
    assert out["ok"] is None
    assert out["failed"] == []
    assert out["indeterminate"] == ["no_panel"]
    assert "没得判" in out["note"]


def test_unformed_pools_are_indeterminate_not_failed():
    """An unfinished loader is not a cold tape.

    On 2026-10-30 the 09:41 read was limit_up=0 / limit_down=1 / tone=冰点 and the
    close was 49 / 86. The gate said "failed"; it should have said "cannot tell".
    """
    out = sed.gates({"tone": sed.PROVISIONAL_TONE, "break_rate": None,
                     "max_boards": 0, "height_ok": False,
                     "phase": "opening_window", "provisional": True})
    assert out["ok"] is False
    assert out["failed"] == []
    assert set(out["indeterminate"]) == {"tone_ok", "break_rate_ok", "height_ok"}
    assert out["why_indeterminate"]
    assert out["checks"]["tone_ok"] is None


def test_missing_break_rate_is_indeterminate_after_the_window():
    """Past the opening window the counts are usable; only the ratio is missing."""
    out = sed.gates({"tone": "退潮", "break_rate": None, "max_boards": 4,
                     "height_ok": True, "phase": "intraday"})
    assert out["failed"] == ["tone_ok"]
    assert out["indeterminate"] == ["break_rate_ok"]
    assert out["checks"]["height_ok"] is True


def test_opening_window_never_reports_ice_point():
    cold_looking = {"limit_up_pool": [], "broken_board_pool": [], "limit_down_pool": []}
    opened = sed.panel(cold_looking, phase="opening_window")
    settled = sed.panel(cold_looking, phase="settled")
    assert opened["tone"] == sed.PROVISIONAL_TONE
    assert opened["provisional"] is True
    assert settled["tone"] == "冰点"
    assert settled["provisional"] is False


def test_tone_vocabulary_matches_the_persona_registry():
    """The registry once documented tone=ice/ebb while the panel returned 冰点/退潮."""
    from service.trading import persona as persona_mod

    why_not = persona_mod.REGISTRY["hot_money"].why_not
    for word in ("冰点", "退潮"):
        assert word in why_not
    for alias in ("ice", "ebb"):
        assert alias not in why_not


def test_a_vendor_rate_is_judged_by_the_vendor_threshold():
    """2026-11-13: 0.3621 vendor vs 0.387 derived, one 0.38 ruler, two answers.

    ``panel`` labels which definition produced ``break_rate``. ``gates`` ignored
    the label and compared the vendor number against the derived 0.38, so that
    tape passed a gate its own 0.35 threshold rejects — and the same day's
    derived 0.387 failed the same 0.38. Two sources disagreed only because one
    of them was measured with the other's ruler.
    """
    from service import settings

    out = sed.gates({"tone": "退潮", "break_rate": 0.3621,
                     "break_rate_basis": "vendor", "max_boards": 4,
                     "height_ok": True, "phase": "settled"})

    assert out["break_rate_gate"] == settings.num("sentiment.break_rate_gate_vendor")
    assert out["checks"]["break_rate_ok"] is False
    assert "break_rate_ok" in out["failed"]


def test_the_same_rate_passes_under_the_derived_threshold():
    """The number is not the metric: 0.3621 on the touched basis is below 0.38.

    This is the other half of the pair above. Pinning both keeps a future edit
    from "fixing" the disagreement by forcing one ruler on both definitions.
    """
    from service import settings

    out = sed.gates({"tone": "修复", "break_rate": 0.3621,
                     "break_rate_basis": "touched: 盘中触及涨停价即算炸板",
                     "max_boards": 4, "height_ok": True, "phase": "settled"})

    assert out["break_rate_gate"] == settings.num("sentiment.break_rate_gate")
    assert out["checks"]["break_rate_ok"] is True


class _Store:
    """Just enough of DatasetStore for ``close_panel``: two read shapes."""

    def __init__(self, frames):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None):
        frame = self.frames.get(name)
        if frame is None:
            raise KeyError(name)
        if columns:
            return frame[[c for c in columns if c in frame.columns]].copy()
        return frame


def _vendor_frames(day="20261113", up=2, broken=0, down=1):
    def frame(n):
        return pd.DataFrame([{"trade_date": day, "code": f"60000{i}.SH", "boards": 1}
                             for i in range(n)])
    return {"limit_up_pool": frame(up), "broken_board_pool": frame(broken),
            "limit_down_pool": frame(down)}


def test_the_close_panel_prefers_the_derived_pool():
    """The official file must carry the counts a reader can check name by name.

    Built before ``derive_pools`` ran, it was vendor-based every evening while
    `facts` and `limit_structure` used the derived pool: 2026-11-13 published
    0.3621 beside 0.387 for the same session.
    """
    stores = _vendor_frames()
    stores["limit_pool_t0"] = pd.DataFrame(
        [{"pool": "limit_up", "code": f"60000{i}.SH", "boards": b}
         for i, b in enumerate((2, 3, 5))]
        + [{"pool": "broken_board", "code": "600004.SH", "boards": 1}]
        + [{"pool": "limit_down", "code": "600005.SH", "boards": 1}])

    panel = sed.close_panel(_Store(stores), "20261113")

    assert panel["counts_source"] == "derived:limit_pool_t0"
    assert (panel["limit_up"], panel["broken_board"], panel["limit_down"]) == (3, 1, 1)
    assert panel["max_boards"] == 5
    assert panel["break_rate_basis"].startswith("touched")
    assert panel["break_rate"] == round(1 / 4, 4)


def test_the_close_panel_falls_back_to_vendor_when_the_derived_pool_is_absent():
    """No derived partition is a labelled fallback, not a missing panel."""
    panel = sed.close_panel(_Store(_vendor_frames(up=27, broken=9, down=3)), "20261113")

    assert panel["counts_source"] == "vendor"
    assert panel["break_rate_basis"] == "vendor"
    assert panel["limit_up"] == 27
    assert panel["break_rate"] == round(9 / 36, 4)
