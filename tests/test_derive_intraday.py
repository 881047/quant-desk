"""P2 日更归属：derive_intraday 的两种结果都不能拖垮主链路。"""

from __future__ import annotations

import pandas as pd

from scripts import derive_intraday


class FakeStore:
    def __init__(self, frame: pd.DataFrame, tape: pd.DataFrame | None = None):
        self.frame = frame
        self.tape = tape if tape is not None else pd.DataFrame()
        self.written: list = []
        self.reads: list = []

    def read(self, dataset, start, end, **kwargs):
        self.reads.append((dataset, start, end))
        return self.tape if dataset == "pool_tape" else self.frame

    def write(self, dataset, day, frame, **kwargs):
        self.written.append((dataset, day, len(frame), kwargs))


def _framed() -> pd.DataFrame:
    rows = []
    for fid, last in (("093000_000001", 10.0), ("100000_000001", 11.0)):
        rows.append({"code": "600000", "frame_id": fid, "last": last,
                     "pct_chg": 1.0, "volume_ratio": 1.0, "amount": 1e6,
                     "snapshot_ts": "2026-11-09T09:30:00+08:00",
                     "source": "eastmoney"})
    return pd.DataFrame(rows)


def test_a_legacy_partition_returns_zero_and_writes_nothing():
    """旧分区缺 frame_id 是历史边界，不是日更失败；不许染红 gate。"""
    store = FakeStore(_framed().drop(columns=["frame_id"]))
    assert derive_intraday.main(["--day", "20261106"], store=store) == 0
    assert store.written == []
    assert store.reads[0] == ("rt_quote", "20261106", "20261106")
    assert ("pool_tape", "20261106", "20261106") in store.reads


def test_a_framed_partition_writes_one_derived_partition():
    store = FakeStore(_framed())
    assert derive_intraday.main(["--day", "20261109"], store=store) == 0
    assert len(store.written) == 1
    dataset, day, rows, kwargs = store.written[0]
    assert dataset == "intraday_features" and day == "20261109" and rows == 2
    assert kwargs["mode"] == "replace" and kwargs["status"] == "ok"


def test_an_empty_source_is_a_state_not_a_failure():
    store = FakeStore(pd.DataFrame())
    assert derive_intraday.main(["--day", "20261109"], store=store) == 0
    assert store.written == []
