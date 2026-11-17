"""rt_quote.frame_id 只是复合外键：不改 pk、不进 required、旧分区不回填。

v4 §0.1 #9/#20：改 pk 才会让 9 个旧分区抛 ArrowInvalid；单纯加列在
``merge`` 路径上由 ``pd.concat`` 补 NaN，``normalize_frame`` 只校验
required 列存在，不校验「多出来的列」。这里把这条口径钉成测试。
"""

from __future__ import annotations

import pandas as pd

from service.adapters.catalog import SPECS
from service.adapters.parquet_store import normalize_frame
from service.trading.snapshot import with_frame_id


def _row(ts: str, last: float) -> pd.DataFrame:
    return pd.DataFrame({"snapshot_ts": [ts], "code": ["600000"], "last": [last],
                         "trade_date": ["20261106"]})


def test_with_frame_id_is_pure_and_uses_the_archive_stamp():
    quotes = _row("09:30:00", 10.0)
    out = with_frame_id(quotes, "093000_123456")
    assert out["frame_id"].tolist() == ["093000_123456"]
    assert "frame_id" not in quotes.columns  # 归档仍写原始帧，回放靠文件名


def test_frame_id_is_not_a_pk_or_required_column():
    spec = SPECS["rt_quote"]
    assert spec.pk == ("snapshot_ts", "code")
    assert "frame_id" not in spec.required
    assert "frame_id" not in spec.pk


def test_old_partition_without_frame_id_merges_with_nulls():
    """历史分区读回来 frame_id 为 NaN 不报错，也不许静默回填成今天的帧。"""
    old = _row("09:30:00", 10.0)
    new = with_frame_id(_row("09:31:00", 10.1), "093100_000001")
    merged = pd.concat([old, new], ignore_index=True)
    assert merged["frame_id"].isna().sum() == 1
    assert merged["frame_id"].notna().sum() == 1

    out = normalize_frame(merged, SPECS["rt_quote"])
    assert "frame_id" in out.columns
    assert len(out) == 2
