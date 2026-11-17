"""The state index must be point-in-time, and its forward leg must start at the open.

Two ways this module can lie, both of them quiet:

* a percentile computed over the whole sample instead of a trailing window, so
  "today is the 6th percentile" quietly used tomorrow's number;
* a forward return measured from the close the decision was made on, crediting
  the overnight gap that a T+1 buyer never gets.

The tests below pin both, plus the sample-size arithmetic that decides whether a
bucket is a distribution or an anecdote.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import state as state_mod


def panel_from_prices(prices: dict[str, list[tuple[float, float]]],
                      start_day: str = "20240101") -> pd.DataFrame:
    """Build a daily frame from {code: [(open, close), ...]} on consecutive days."""
    days = pd.bdate_range(start_day, periods=len(next(iter(prices.values()))))
    rows = []
    for code, path in prices.items():
        prev = path[0][1]
        for day, (open_px, close_px) in zip(days, path):
            rows.append({"ts_code": code, "trade_date": day.strftime("%Y%m%d"),
                         "open": open_px, "close": close_px, "pre_close": prev})
            prev = close_px
    return pd.DataFrame(rows)


def flat_world(days: int = 140, price: float = 10.0) -> pd.DataFrame:
    return panel_from_prices({f"60000{i}.SH": [(price, price)] * days for i in range(3)})


def test_forward_return_starts_at_the_next_open_not_the_close():
    """A +10% overnight gap after the signal must not be credited to the trade."""
    path = [(10.0, 10.0)] * 130 + [(11.0, 11.0)] + [(11.0, 11.0)] * 9
    world = panel_from_prices({f"60000{i}.SH": path for i in range(3)})
    state = state_mod.build_state(world)

    dates = list(state.index)
    signal = dates[129]
    assert state.loc[signal, "fwd_5d"] == pytest.approx(0.0, abs=1e-9), \
        "买入发生在次日开盘，那根 +10% 的跳空不属于这笔交易"


def test_a_close_to_close_measurement_would_have_shown_the_gap():
    """Control: the same world, measured from the close, is +10%."""
    path = [(10.0, 10.0)] * 130 + [(11.0, 11.0)] + [(11.0, 11.0)] * 9
    world = panel_from_prices({f"60000{i}.SH": path for i in range(3)})
    state = state_mod.build_state(world)
    dates = list(state.index)
    level = state["close_level"]
    close_to_close = level.iloc[134] / level.iloc[129] - 1
    assert close_to_close == pytest.approx(0.10, abs=1e-9)
    assert state.loc[dates[129], "fwd_5d"] < 0.001


def test_truncating_the_future_does_not_change_todays_state():
    """Point in time: cutting the panel after day D must leave day D untouched."""
    rng = np.random.default_rng(7)
    days = 260
    prices = {}
    for i in range(12):
        path, px = [], 10.0
        for _ in range(days):
            gap = 1 + rng.normal(0, 0.01)
            intra = 1 + rng.normal(0, 0.015)
            open_px = round(px * gap, 2)
            close_px = round(open_px * intra, 2)
            path.append((open_px, close_px))
            px = close_px
        prices[f"6000{i:02d}.SH"] = path
    world = panel_from_prices(prices)

    full = state_mod.build_state(world)
    cut_day = str(sorted(world["trade_date"].unique())[199])
    truncated = state_mod.build_state(world[world["trade_date"] <= cut_day])

    cols = ["breadth20", "ew_ret_20d", "vol20", "breadth20_pct",
            "ew_ret_20d_pct", "vol20_pct", "bucket"]
    assert cut_day in truncated.index
    for col in cols:
        a, b = full.loc[cut_day, col], truncated.loc[cut_day, col]
        if isinstance(a, float) and np.isnan(a):
            assert isinstance(b, float) and np.isnan(b), col
        else:
            assert a == b, f"{col} 用了未来数据"


def test_episodes_count_runs_not_days():
    """Two separate stretches are two observations, however many days each holds."""
    index = pd.Index([f"2024010{i}" for i in range(1, 10)])
    bucket = ["low|low", "low|low", "low|low", "mid|mid", "mid|mid",
              "low|low", "low|low", "low|low", "mid|mid"]
    state = pd.DataFrame({
        "bucket": bucket,
        "fwd_5d": [0.01] * 9,
        "fwd_20d": [0.02] * 9,
    }, index=index)

    hist = state_mod.bucket_history(state, "low|low")
    assert hist["n"] == 6
    assert hist["episodes"] == 2, "两段行情，不是六次观测"


def test_a_bucket_can_be_large_and_still_unusable():
    index = pd.Index([f"2024{d:04d}" for d in range(1, 41)])
    state = pd.DataFrame({"bucket": ["low|low"] * 40,
                          "fwd_5d": np.linspace(-0.01, 0.01, 40),
                          "fwd_20d": np.linspace(-0.02, 0.02, 40)}, index=index)
    hist = state_mod.bucket_history(state, "low|low")
    assert hist["n"] == 40
    assert hist["episodes"] == 1
    assert hist["usable"] is False
    assert "段数" in hist["note"]


def test_warmup_days_are_not_classified():
    state = state_mod.build_state(flat_world(days=140))
    reading = state_mod.reading(state, day=str(state.index[10]))
    assert reading["ok"] is False
    assert "预热" in reading["error"]


def test_reading_reports_the_sample_behind_the_bucket():
    state = state_mod.build_state(flat_world(days=400))
    reading = state_mod.reading(state)
    assert reading["ok"] is True
    assert reading["history"]["n"] > 0
    assert reading["history"]["episodes"] >= 1
    assert "次日开盘" in reading["forward_basis"]


def test_the_cache_rebuilds_when_a_session_arrives(tmp_path):
    """A stale cache would report yesterday's state as today's."""
    class Store:
        def __init__(self, frame):
            self.frame = frame

        def read(self, *args, **kwargs):
            return self.frame

    short = flat_world(days=400)
    longer = flat_world(days=401)
    first = state_mod.cached_state(Store(short), start="20240101",
                                   cache_dir=tmp_path, daily=short)
    assert (tmp_path / "state_20240101.parquet").exists()
    assert len(first) == 400

    again = state_mod.cached_state(Store(short), start="20240101",
                                   cache_dir=tmp_path, daily=short)
    assert list(again.index) == list(first.index), "同一份数据应命中缓存"

    refreshed = state_mod.cached_state(Store(longer), start="20240101",
                                       cache_dir=tmp_path, daily=longer)
    assert len(refreshed) == 401, "新交易日必须让缓存失效"


def test_block_is_the_compact_shape_the_context_pack_reads():
    state = state_mod.build_state(flat_world(days=400))
    payload = state_mod.block(state)
    assert payload["ok"] is True
    assert set(payload) == {"ok", "as_of", "bucket", "state", "horizons",
                            "history", "baseline", "forward_basis",
                            "edge_definition"}
    assert payload["history"]["n"] > 0
    assert "fwd_20d_3y" in payload["history"]
    # `edge_*` shipped for a month with no definition, which reads as expected
    # profit. Losing this key again is the regression, not a cosmetic change.
    assert "不是可赚收益" in payload["edge_definition"]


def test_recent_slice_is_cut_on_the_calendar_not_on_bucket_rows():
    """A bucket from years ago must not report itself as "100% recent".

    The first version sliced ``rows.tail(252)``, which takes the last 252
    *occurrences*. A 76-row bucket therefore kept all 76 and claimed to be one
    year of data while spanning 2007-2026.
    """
    index = pd.Index([f"2024{d:04d}" for d in range(1, 401)])
    bucket = ["low|low"] * 100 + ["mid|mid"] * 300
    state = pd.DataFrame({"bucket": bucket,
                          "fwd_5d": [0.01] * 400,
                          "fwd_20d": [0.02] * 400}, index=index)

    hist = state_mod.bucket_history(state, "low|low")
    assert hist["n"] == 100
    assert hist["recent_share"] == 0.0, "这 100 天全在 252 个交易日之前"

    recent = state_mod.bucket_history(state, "mid|mid")
    assert recent["recent_share"] == pytest.approx(252 / 300, abs=0.01)


# ------------------------------------------------------- 窗口要能按问题裁剪
def test_block_can_be_trimmed_to_the_windows_the_question_is_about():
    """1-2 日的问题不该收到一份 20 日分布：那是另一个问题的答案，而且答得很自信。"""
    state = state_mod.build_state(flat_world(days=400))
    short = state_mod.block(state, horizons=(1, 2))
    full = state_mod.block(state)

    assert short["horizons"] == [1, 2]
    assert "fwd_20d" not in short["history"]
    assert "fwd_20d_3y" not in short["history"], "三年切片只在 20 日窗口下有意义"
    assert {"fwd_1d", "fwd_2d"} <= set(short["history"])
    assert short["history"]["n"] > 0
    assert full["horizons"] == list(state_mod.HORIZONS)
    assert len(short["history"]) < len(full["history"])


def test_a_window_the_builder_never_made_is_an_error_not_a_blank():
    state = state_mod.build_state(flat_world(days=400))
    with pytest.raises(ValueError, match="unknown horizons"):
        state_mod.block(state, horizons=(3,))


def test_a_known_last_session_skips_the_sixteen_million_row_read(tmp_path):
    """缓存命中时不该再读全量日线——那正是缓存要避免的 5.8 秒。"""

    class Exploding:
        def read(self, *args, **kwargs):
            raise AssertionError("命中缓存还去读了数据")

    frame = flat_world(days=400)
    first = state_mod.cached_state(Exploding(), cache_dir=tmp_path, daily=frame)
    hit = state_mod.cached_state(Exploding(), cache_dir=tmp_path,
                                 last=str(frame.trade_date.max()))
    assert len(hit) == len(first) > 0
    assert list(hit.index) == list(first.index)


def test_historical_reading_excludes_all_later_outcomes():
    frame = state_mod.build_state(flat_world(days=400))
    day = str(frame.index[-10])
    full = state_mod.reading(frame, day)
    truncated = state_mod.reading(frame.loc[:day], day)
    assert full["ok"] and truncated["ok"]
    assert full["history"] == truncated["history"]
    assert full["baseline"] == truncated["baseline"]
    assert full["coverage"] == truncated["coverage"]
    same_bucket = int((frame.loc[:day, "bucket"] == full["bucket"]).sum())
    assert full["history"]["n"] == same_bucket - 1
    assert full["history"]["fwd_20d"]["n"] == same_bucket - 20


def test_any_warmup_dimension_blocks_the_reading():
    frame = state_mod.build_state(flat_world(days=400))
    day = str(frame.index[-1])
    frame.loc[day, "bucket"] = "low|?"
    out = state_mod.reading(frame, day)
    assert out["ok"] is False and "预热" in out["error"]


def test_cache_digest_detects_history_rewritten_with_the_same_last_day(tmp_path):
    class Store:
        def read(self, *args, **kwargs):
            raise AssertionError("daily was supplied")

    frame = flat_world(days=400)
    state_mod.cached_state(Store(), cache_dir=tmp_path, daily=frame)
    meta = tmp_path / "state_20050101.meta.json"
    before = meta.read_text(encoding="utf-8")
    changed = frame.copy()
    changed.loc[changed.index[0], "close"] *= 1.01
    state_mod.cached_state(Store(), cache_dir=tmp_path, daily=changed)
    assert meta.read_text(encoding="utf-8") != before
