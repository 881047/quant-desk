"""The analog window: derived from the store, capped, and never hardcoded.

A literal start date is the kind of bug that hides for months. The data gets
backfilled, the queries keep reading the old window, and every answer looks
normal because a shorter history still produces a plausible number.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from service.trading import analog


class _Registry:
    def __init__(self, rows):
        self._rows = rows

    def summary(self):
        return self._rows


class _Store:
    def __init__(self, rows, calendar):
        self.registry = _Registry(rows)
        self._calendar = calendar

    def read(self, dataset, start=None, end=None, *, columns=None, status=None):
        if dataset == "trade_cal":
            return self._calendar
        raise KeyError(dataset)


def _calendar(first: str, last: str) -> pd.DataFrame:
    days = pd.bdate_range(first, last).strftime("%Y%m%d").tolist()
    return pd.DataFrame({"cal_date": days, "is_open": [1] * len(days)})


def _rows(start: str, end: str) -> list[dict]:
    return [{"dataset": "daily", "cover_start": start, "cover_end": end}]


def test_start_follows_the_data_not_a_constant():
    store = _Store(_rows("20050104", "20261023"),
                   _calendar("2005-01-04", "2026-10-23"))
    start = analog.default_start(store, max_sessions=10_000)
    assert start == "20050104", "with enough room the whole cover is used"


def test_start_is_capped_by_session_count():
    store = _Store(_rows("20050104", "20261023"),
                   _calendar("2005-01-04", "2026-10-23"))
    start = analog.default_start(store, max_sessions=250)
    assert start > "20250101", "250 sessions back from 2026 is about a year"
    # the cap must land on a real trading day, not a weekend
    assert start in set(_calendar("2005-01-04", "2026-10-23")["cal_date"])


def test_a_missing_calendar_falls_back_to_the_cover_start():
    class _NoCal(_Store):
        def read(self, dataset, *a, **k):
            raise KeyError(dataset)

    store = _NoCal(_rows("20050104", "20261023"), None)
    assert analog.default_start(store) == "20050104"


def test_an_empty_manifest_still_returns_a_usable_date():
    store = _Store([], _calendar("2005-01-04", "2026-10-23"))
    assert analog.default_start(store) == "19900101"


def test_the_cache_key_changes_when_the_window_moves():
    """Two different windows over the same cover_end are different indexes."""
    a = analog._key("20261023", 1200, 2e7, "20050104")
    b = analog._key("20261023", 1200, 2e7, "20230101")
    assert a != b

    # and a deeper backfill that does not move cover_end must still invalidate
    c = analog._key("20261023", 1200, 2e7, "20100101")
    assert c not in {a, b}


def test_episodes_count_runs_not_days():
    """Three hundred consecutive stressed days is one event. Reporting it as 300
    observations is how a single crash becomes a "sample"."""
    import numpy as np

    labels = np.array(["stressed"] * 300 + ["neutral"] * 5 + ["stressed"] * 4
                      + ["risk_on"] * 2)
    counts = analog._episodes(labels)
    assert counts["stressed"] == 2, "one long run and one short one"
    assert counts["neutral"] == 1
    assert counts["risk_on"] == 1


def test_a_label_that_never_appears_is_absent():
    import numpy as np

    counts = analog._episodes(np.array(["neutral", "neutral"]))
    assert counts == {"neutral": 1}


def test_regime_caveats_carry_live_numbers_not_prose():
    """The old caveat said "281 stressed days" as a literal. After the window
    widened to 3,000 sessions the sentence was describing a dataset that no
    longer existed, and nothing failed."""
    source = (analog.__file__ or "")
    text = Path(source).read_text(encoding="utf-8")
    assert "281 个 stressed" not in text, "hardcoded sample size in a caveat"
    assert "2023-01 至今" not in text, "hardcoded window in a caveat"


class _DailyStore:
    def __init__(self, future_scale: float = 1.0):
        self.days = pd.bdate_range("2026-01-05", periods=100).strftime("%Y%m%d").tolist()
        self.calendar = pd.DataFrame({"cal_date": self.days, "is_open": 1})
        self.registry = _Registry(_rows(self.days[0], self.days[-1]))
        self.daily_calls = []
        rows = []
        for code, offset in (("600000.SH", 0.0), ("000001.SZ", 5.0)):
            previous = offset + 10.0
            for i, day in enumerate(self.days):
                price = offset + 10.0 + i * 0.01
                if i >= 80:
                    price *= future_scale
                amount = 1_000_000 * (1.0 + offset / 10.0 + i * 0.001)
                rows.append({"ts_code": code, "trade_date": day, "close": price,
                             "pre_close": previous, "open": price, "amount": amount})
                previous = price
        self.daily = pd.DataFrame(rows)

    def read(self, dataset, start=None, end=None, *, columns=None, status=None):
        if dataset == "trade_cal":
            return self.calendar.copy()
        if dataset == "stock_basic":
            return pd.DataFrame()
        if dataset != "daily":
            raise KeyError(dataset)
        self.daily_calls.append((start, end))
        frame = self.daily.copy()
        if start:
            frame = frame[frame["trade_date"] >= start]
        if end:
            frame = frame[frame["trade_date"] <= end]
        return frame[columns] if columns else frame


def test_historical_build_reads_no_future_and_standardizes_on_history_only():
    first_store = _DailyStore(future_scale=1.0)
    second_store = _DailyStore(future_scale=50.0)
    as_of = first_store.days[79]
    start = first_store.days[0]

    first = analog.build(first_store, start=start, end=as_of)
    second = analog.build(second_store, start=start, end=as_of)

    assert first_store.daily_calls == [(start, as_of)]
    assert second_store.daily_calls == [(start, as_of)]
    assert first.meta["end"] == as_of
    assert max(first.dates) <= as_of
    np.testing.assert_array_equal(first.mkt_mean, second.mkt_mean)
    np.testing.assert_array_equal(first.mkt_std, second.mkt_std)
    np.testing.assert_array_equal(first.day_mean, second.day_mean)
    np.testing.assert_array_equal(first.day_std, second.day_std)
    np.testing.assert_array_equal(first.x, second.x)
    np.testing.assert_array_equal(first.fwd, second.fwd)
    latest_setup = max(int(day) for day in first.day_of_row)
    assert latest_setup + min(analog.HORIZONS) <= first.day_index(as_of)
    longest = first.day_of_row[np.isfinite(first.fwd[:, -1])]
    assert max(int(day) for day in longest) + max(analog.HORIZONS) <= first.day_index(as_of)


def test_query_on_a_full_index_still_excludes_future_or_unrealised_windows():
    store = _DailyStore()
    index = analog.build(store, start=store.days[0])
    as_of = store.days[79]
    i = index.day_index(as_of)

    hit = index.query(np.zeros(len(analog.FEATURES), dtype=np.float32), as_of, k=100)
    setup_days = hit["rows"]["day"]

    assert len(setup_days) > 0
    assert max(int(day) for day in setup_days) + max(analog.HORIZONS) < i


def test_get_index_cache_is_bounded_by_as_of_and_writes_only_under_tmp_path(tmp_path):
    store = _DailyStore()
    as_of = store.days[79]
    analog.reset_cache()
    try:
        index = analog.get_index(store, tmp_path, start=store.days[0], end=as_of)
    finally:
        analog.reset_cache()

    assert index.meta["end"] == as_of
    assert max(index.dates) == as_of
    assert store.daily_calls == [(store.days[0], as_of)]
    assert list((tmp_path / "data" / "cache" / "analog").glob("*.npz"))


def test_each_horizon_uses_its_own_realised_candidate_mask():
    store = _DailyStore()
    index = analog.build(store, start=store.days[0])
    as_of = store.days[79]
    raw = np.zeros(len(analog.FEATURES), dtype=np.float32)

    short = index.query(raw, as_of, k=500, horizon=1)
    long = index.query(raw, as_of, k=500, horizon=10)
    short_days = short["rows"]["day"]
    long_days = long["rows"]["day"]

    assert max(int(day) for day in short_days) > max(int(day) for day in long_days)
    assert np.isfinite(short["rows"]["fwd"][:, 0]).all()
    assert np.isfinite(long["rows"]["fwd"][:, -1]).all()
    assert max(int(day) for day in short_days) + 1 < index.day_index(as_of)
    assert max(int(day) for day in long_days) + 10 < index.day_index(as_of)


def test_explain_reports_each_horizon_from_its_own_candidate_pool(monkeypatch):
    from types import SimpleNamespace

    store = _DailyStore()
    index = analog.build(store, start=store.days[0])
    as_of = store.days[79]
    ends = []
    monkeypatch.setattr(analog, "features_for",
                        lambda *args, **kwargs: {name: 0.0 for name in analog.STOCK_FEATURES})

    def historical_index(_store, _root, *, end=None, **kwargs):
        ends.append(end)
        return index

    monkeypatch.setattr(analog, "get_index", historical_index)
    desk = SimpleNamespace(market=SimpleNamespace(
        store=store, names=dict, industry_of=lambda symbol: ""))

    report = analog.explain(desk, "600000.SH", as_of=as_of, k=500)

    assert report["ok"] is True
    assert ends == [as_of]
    assert report["horizons"]["1d"]["n"] > report["horizons"]["10d"]["n"]

    early = store.days[25]
    short_only = analog.explain(desk, "600000.SH", as_of=early, k=500)
    assert short_only["ok"] is True
    assert short_only["horizons"]["1d"]["n"] > 0
    assert short_only["horizons"]["10d"]["n"] == 0
    # The longest pool has no realised forward return this early, so the
    # nearest-neighbour pool must fall back to a shorter horizon. Keep this
    # tied to the configured family instead of hardcoding (1, 3, 5): 2 and 4
    # are now valid horizons too.
    shorter = tuple(h for h in analog.HORIZONS if h != max(analog.HORIZONS))
    assert short_only["match"]["nearest_horizon"] in shorter


def test_cache_round_trip_rejects_bad_shapes_and_non_finite_values(tmp_path):
    store = _DailyStore()
    index = analog.build(store, start=store.days[0], end=store.days[79])
    path = analog.save(index, tmp_path)
    loaded = analog.load(path)
    np.testing.assert_array_equal(loaded.x, index.x)

    with np.load(path, allow_pickle=False) as payload:
        arrays = {name: payload[name] for name in payload.files}
    bad_shape = tmp_path / "bad-shape.npz"
    arrays["x"] = arrays["x"][:-1]
    np.savez_compressed(bad_shape, **arrays)
    assert not analog._usable(bad_shape)

    arrays["x"] = index.x.copy()
    arrays["x"][0, 0] = np.nan
    non_finite = tmp_path / "non-finite.npz"
    np.savez_compressed(non_finite, **arrays)
    assert not analog._usable(non_finite)
    truncated = tmp_path / "truncated.npz"
    truncated.write_bytes(b"not a zip")
    assert not analog._usable(truncated)


def test_cache_write_failure_preserves_the_previous_file(monkeypatch, tmp_path):
    store = _DailyStore()
    index = analog.build(store, start=store.days[0], end=store.days[79])
    path = analog.save(index, tmp_path)
    previous = path.read_bytes()

    def fail_write(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(analog.np, "savez_compressed", fail_write)
    with pytest.raises(RuntimeError, match="injected"):
        analog.save(index, tmp_path)

    assert path.read_bytes() == previous
    assert not list(path.parent.glob(f".{path.stem}.*.npz"))
