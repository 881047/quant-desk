"""Forward-looking fundamentals are predictions, never reported results."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from service.adapters.catalog import get_spec
from service.trading import profile
from scripts import sync_forecast


def test_forecast_and_express_are_declared_as_forward_datasets():
    for name in ("forecast", "express"):
        spec = get_spec(name)
        assert spec is not None
        assert spec.date_column == "ann_date"
        assert spec.on_demand is True
        assert "forward" in spec.tags


def test_merge_dedups_by_symbol_announcement_and_period():
    class Store:
        def __init__(self):
            self.written = None

        def read(self, name):
            raise KeyError(name)

        def write(self, dataset, partition, frame, *, as_of):
            self.written = (dataset, partition, frame, as_of)

    frame = pd.DataFrame({
        "ts_code": ["A.SZ", "A.SZ", "B.SZ"],
        "ann_date": ["20261030", "20261030", "20261031"],
        "end_date": ["20260930", "20260930", "20260930"],
        "type": ["预增", "预增", "略增"],
    })
    store = Store()
    out = sync_forecast.merge(store, "forecast", frame)
    ds, partition, written, as_of = store.written
    assert (ds, partition) == ("forecast", "all")
    assert len(written) == 2
    assert out["as_of"] == "20261031" and as_of == "20261031"


def test_guidance_is_cut_on_ann_date_and_labelled_as_prediction(monkeypatch):
    frame = pd.DataFrame({
        "ts_code": ["X.SZ", "X.SZ"],
        "ann_date": ["20261001", "20261201"],  # the second one is in the future
        "end_date": ["20260930", "20260930"],
        "type": ["预增", "预增"],
        "p_change_min": [10.0, 99.0],
        "p_change_max": [20.0, 199.0],
        "net_profit_min": [1000.0, 9999.0],
        "net_profit_max": [2000.0, 19999.0],
        "summary": ["主业改善", "未来公告"],
        "change_reason": ["订单增长", "尚未发生"],
    })

    def fake_read(desk, dataset, **kwargs):
        return frame if dataset == "forecast" else pd.DataFrame()

    monkeypatch.setattr(profile, "_read_cached", fake_read)
    desk = MagicMock()
    desk.market.names.return_value = {}
    desk.market.industry_of.return_value = ""
    desk.market.announcements.return_value = []

    out = profile._events(desk, "X.SZ", "20261110")
    assert len(out["guidance"]) == 1
    row = out["guidance"][0]
    assert row["ann_date"] == "20261001"
    assert row["p_change_min_pct"] == 10.0
    assert row["kind"] == "业绩预告"
    assert "不是已实现业绩" in row["basis"]
