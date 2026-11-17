"""成交可行性: the half of execution the desk can measure, and the half it cannot.

A ticket that says ``proposed`` tells the model the order is *legal*. It says
nothing about whether it will *clear*, and for a buy into a sealed ceiling those
are different facts: the guard passes, the price is exact, and the morning
refuses the fill. The model then burns a session and a queued ticket on an
outcome that was knowable at proposal time.

So ``feasibility`` reports three things it can measure — board state, whether a
queue is unavoidable, and how strong the seal is relative to float — and returns
``fill_ratio=None`` for the one it cannot. The refusal to produce a number is the
feature: a fabricated 0.3 would be traded on, and nothing in any subscribed feed
supports it.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from service.trading import fill
from service.trading.market import MarketRepository


class FakeStore:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.reads: list[str] = []

    def read(self, name, start=None, end=None, columns=None):
        self.reads.append(name)
        frame = self.frames.get(name)
        if frame is None:
            return pd.DataFrame()
        frame = frame.copy()
        return frame[columns] if columns and not frame.empty else frame


class FakeMarket(MarketRepository):
    """A real repository over a fake store, with one canned snapshot.

    Inheriting matters: ``circ_mv`` is a repository method now, and a hand-rolled
    double would have to reimplement the 万元→元 conversion — which is exactly the
    duplication this suite is here to prevent.
    """

    def __init__(self, snapshot, frames: dict[str, pd.DataFrame] | None = None):
        super().__init__(FakeStore(frames or {}))
        self._snapshot = snapshot

    def snapshot(self, symbol, day=None):
        return self._snapshot


def snap(*, last: str, up: str = "11.00", down: str = "9.00", open_: str | None = None,
         suspended: bool = False):
    from service.trading.market import Snapshot

    return Snapshot(
        symbol="000001.SZ", trade_date="20261030",
        last=Decimal(last), pre_close=Decimal("10.00"),
        open=Decimal(open_ or last), high=Decimal(up), low=Decimal("9.90"),
        up_limit=Decimal(up), down_limit=Decimal(down),
        volume=5_000_000.0, amount=5.0e7, suspended=suspended, source="daily",
    )


def _up(*, seal: float = 6.0e8, streak: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"],
        "name": ["本人", "同池"],
        "seal_money": [seal, 1.2e8],
        "continue_cnt": [streak, 1],
        "limit_up_time": ["09:33", "10:10"],
        "trade_date": ["20261030", "20261030"],
    })


def _basic() -> pd.DataFrame:
    # circ_mv is 万元. 246亿 float => 2,460,000 万元.
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"],
        "trade_date": ["20261030", "20261030"],
        "circ_mv": [2_460_000.0, 900_000.0],
    })


def setup_function():
    fill.reset_cache()


def test_a_sealed_ceiling_forces_a_queue_and_says_it_cannot_be_measured():
    market = FakeMarket(snap(last="11.00"), {"ths_limit_up": _up(), "daily_basic": _basic()})
    result = fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert result["ok"] is True
    assert result["board"]["state"] == "sealed_up"
    assert result["queue"]["required"] is True
    assert "排队" in result["queue"]["why"]
    # The refusal that matters: no number, with the reason attached.
    assert result["fill_ratio"]["value"] is None
    assert result["fill_ratio"]["why"]


def test_the_seal_is_measured_against_float_not_against_the_pool():
    market = FakeMarket(snap(last="11.00"), {"ths_limit_up": _up(), "daily_basic": _basic()})
    seal = fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")["seal"]

    assert seal["basis"] == "observed"
    assert seal["cny"] == 6.0e8
    assert seal["ratio"] == round(6.0e8 / 2.46e10, 4)
    assert seal["pool_n"] == 2
    assert seal["streak"] == 2
    assert seal["seal_time"] == "09:33"


def test_a_board_that_opened_at_the_ceiling_is_flagged_as_unbuyable_from_the_start():
    market = FakeMarket(snap(last="11.00", open_="11.00"),
                        {"ths_limit_up": _up(), "daily_basic": _basic()})
    result = fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert result["board"]["opened_at_ceiling"] is True


def test_a_price_below_the_ceiling_needs_no_queue():
    market = FakeMarket(snap(last="10.42"), {"ths_limit_up": _up(), "daily_basic": _basic()})
    # A name that is in no pool today: the answer is "no seal to talk about",
    # which is different from a seal of zero.
    result = fill.feasibility(market, "000009.SZ", day="20261030", side="BUY")

    assert result["board"]["state"] == "trading"
    assert result["queue"]["required"] is False
    # Not in today's limit-up pool and not at a ceiling: the answer is "no seal
    # to talk about", stated as such rather than as a zero.
    assert result["seal"]["basis"] == "absent"


def test_a_suspended_name_is_not_called_tradeable():
    market = FakeMarket(snap(last="10.42", suspended=True))
    result = fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert result["board"]["state"] == "suspended"
    assert result["queue"]["required"] is False


def test_selling_into_a_sealed_floor_also_queues():
    market = FakeMarket(snap(last="9.00"), {"ths_limit_up": pd.DataFrame()})
    result = fill.feasibility(market, "000001.SZ", day="20261030", side="SELL")

    assert result["board"]["state"] == "sealed_down"
    assert result["queue"]["required"] is True
    # A sell never has a seal to measure; saying so beats reporting a zero bid.
    assert result["seal"]["basis"] == "not_applicable"


def test_a_missing_pool_degrades_to_unknown_instead_of_zero():
    market = FakeMarket(snap(last="11.00"), {})
    result = fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert result["seal"]["basis"] == "unknown"
    assert "入库" in result["seal"]["why"]


def test_the_float_series_is_read_once_per_session():
    market = FakeMarket(snap(last="11.00"), {"ths_limit_up": _up(), "daily_basic": _basic()})
    fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")
    fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert market.store.reads.count("daily_basic") == 1


def test_the_float_cache_lives_on_the_repository_and_drops_on_invalidate():
    """缓存在 ``MarketRepository`` 上，所以 ``invalidate()`` 是它的失效开关。

    这条以前测的是 ``fill._CIRC_CACHE``——一个只服务封单的私有缓存。它和
    ``tape._seal_strength`` 里那份重复实现一起并进了 repository，因为同一个
    ×10,000 出现在两处，两处都写着"只在这里换算"。
    """
    market = FakeMarket(snap(last="11.00"), {"ths_limit_up": _up(), "daily_basic": _basic()})
    fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")
    market.invalidate()
    fill.feasibility(market, "000001.SZ", day="20261030", side="BUY")

    assert market.store.reads.count("daily_basic") == 2


# --------------------------------------------------------------- calibration

@pytest.fixture(autouse=True)
def _restore_calibration():
    """Tests below repoint CALIBRATION; put it back so they cannot leak."""
    original = fill.CALIBRATION
    yield
    fill.CALIBRATION = original
    fill.reset_cache()


def _write_calibration(tmp_path, *, end: str = "20261030", n: int = 40) -> None:
    import json

    payload = {
        "window": {"start": "20260911", "end": end, "sessions": 30},
        "seal_quantiles": [0.002, 0.004, 0.006, 0.010],
        "by_seal_ratio": [
            {"bucket": "Q1 最弱", "n": n, "open_rate": 0.66, "next_open_gap_mean": 0.008},
            {"bucket": "Q2", "n": n, "open_rate": 0.55, "next_open_gap_mean": -0.013},
            {"bucket": "Q3", "n": n, "open_rate": 0.35, "next_open_gap_mean": -0.005},
            {"bucket": "Q4", "n": n, "open_rate": 0.55, "next_open_gap_mean": -0.010},
            {"bucket": "Q5 最强", "n": n, "open_rate": 0.05, "next_open_gap_mean": 0.054},
        ],
    }
    path = tmp_path / "board_open_rate.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    fill.CALIBRATION = path
    fill.reset_cache()


def test_without_a_calibration_there_is_no_history_block():
    """No file means no prior. A default would be an invented number in every ticket."""
    import pathlib

    fill.CALIBRATION = pathlib.Path("/nonexistent/board_open_rate.json")
    fill.reset_cache()
    try:
        assert fill._seal_history(0.005) is None
    finally:
        fill.reset_cache()


def test_the_current_ratio_lands_in_the_measured_quintile(tmp_path):
    _write_calibration(tmp_path)

    strong = fill._seal_history(0.20)
    weak = fill._seal_history(0.001)
    middle = fill._seal_history(0.005)

    assert strong["bucket"] == "Q5 最强"
    assert strong["open_rate"] == 0.05
    assert strong["gap_if_opened"] == 0.054
    assert weak["bucket"] == "Q1 最弱"
    assert middle["bucket"] == "Q3"
    fill.reset_cache()


def test_the_history_block_carries_its_own_sample_size_and_window(tmp_path):
    """A prior without n and a date is an opinion."""
    _write_calibration(tmp_path)
    block = fill._seal_history(0.005)

    assert block["n"] == 40
    assert block["as_of"] == "20261030"
    assert "age_days" in block
    fill.reset_cache()


def test_a_month_old_calibration_says_so(tmp_path):
    _write_calibration(tmp_path, end="20200102")
    block = fill._seal_history(0.005)

    assert block["age_days"] > 30
    assert "情绪周期" in block["note"]
    fill.reset_cache()


def test_band_edges_are_used_not_hardcoded_thresholds(tmp_path):
    """If the measured distribution moves, the buckets have to move with it."""
    _write_calibration(tmp_path)
    before = fill._seal_history(0.005)["bucket"]

    import json

    payload = json.loads(fill.CALIBRATION.read_text(encoding="utf-8"))
    payload["seal_quantiles"] = [0.010, 0.020, 0.030, 0.040]
    fill.CALIBRATION.write_text(json.dumps(payload), encoding="utf-8")
    fill.reset_cache()

    assert before == "Q3"
    assert fill._seal_history(0.005)["bucket"] == "Q1 最弱"
    fill.reset_cache()
