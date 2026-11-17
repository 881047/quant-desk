"""P2-1：逐帧特征。帧序、桶边界、旧分区口径。"""

from __future__ import annotations

import pandas as pd

from service.trading import intraday_features as feat

_FRAMES = [("093000_000001", 10.0, 20.0),
           ("094257_999355", 10.5, 19.0),   # 人工刷新插入的非周期帧
           ("100000_000001", 11.0, 18.0)]


def _quotes(with_frame_id: bool = True) -> pd.DataFrame:
    rows = []
    for fid, price_a, price_b in _FRAMES:
        for code, last in (("600000", price_a), ("000001", price_b)):
            row = {"code": code, "last": last, "pct_chg": (last / 10 - 1) * 100,
                   "volume_ratio": 1.2, "amount": 1_000_000.0,
                   "snapshot_ts": "2026-11-06T09:30:00+08:00",
                   "source": "eastmoney"}
            if with_frame_id:
                row["frame_id"] = fid
            rows.append(row)
    return pd.DataFrame(rows)


def test_build_features_has_one_row_per_frame_and_code():
    out = feat.build_features(_quotes(), "20261106")
    assert out["ok"] is True and out["status"] == "ok"
    assert out["rows"] == 6 and out["frames"] == 3
    assert list(out["frame"].columns) == list(feat.OUT_COLUMNS)
    assert out["frame"]["day"].unique().tolist() == ["20261106"]


def test_buckets_come_from_real_frame_time_not_from_the_index():
    out = feat.build_features(_quotes(), "20261106")
    buckets = out["frame"][out["frame"]["code"] == "600000"]["bucket_5min"].tolist()
    assert buckets == ["0930", "0940", "1000"]   # 094257 落 0940，不按序号推
    assert feat.frame_bucket("145944_343236") == "1455"
    assert feat.frame_bucket("garbage") is None
    assert feat.frame_at("094257_999355") == "09:42:57"


def test_frame_return_and_forward_return_use_actual_frames():
    out = feat.build_features(_quotes(), "20261106")
    rows = out["frame"][out["frame"]["code"] == "600000"].sort_values("frame_id")
    assert pd.isna(rows["frame_return_pct"].iloc[0])  # 第一帧没有前帧可减
    assert round(rows["frame_return_pct"].iloc[1], 4) == 5.0
    assert round(rows["frame_return_pct"].iloc[2], 4) == 4.7619
    assert round(rows["forward_to_close_pct"].iloc[0], 4) == 10.0
    assert round(rows["forward_to_close_pct"].iloc[-1], 4) == 0.0


def test_legacy_partition_without_frame_id_is_refused_explicitly():
    out = feat.build_features(_quotes(with_frame_id=False), "20261106")
    assert out["ok"] is False
    assert out["status"] == "legacy_partition_no_frame_id"
    assert out["frame"] is None and out["rows"] == 0
    assert "snapshot_ts" in out["note"]


def test_shape_is_a_pct_chg_proxy_not_a_limit_pool_verdict():
    assert feat.shape_of(10.0) == "limit_up_like"
    assert feat.shape_of(-10.0) == "limit_down_like"
    assert feat.shape_of(5.0) == "strong"
    assert feat.shape_of(-5.0) == "weak"
    assert feat.shape_of(0.0) == "flat"
    assert feat.shape_of(None) == "unknown"


def test_empty_and_columnless_inputs_are_explicit_states():
    assert feat.build_features(pd.DataFrame(), "20261106")["status"] == "empty"
    assert feat.build_features(None, "20261106")["status"] == "empty"
    bad = _quotes().assign(frame_id="not-a-frame")
    assert feat.build_features(bad, "20261106")["status"] == "no_source"


def test_output_has_no_signal_score_or_advice_columns():
    out = feat.build_features(_quotes(), "20261106")
    for word in ("signal", "score", "ranking", "action", "advice"):
        assert word not in out["frame"].columns


def _tape(with_frame_id: bool = True, bid: float = 100.0,
          ask: float = 50.0) -> pd.DataFrame:
    rows = []
    for fid, b, a in (("093000_000001", bid, ask),
                      ("100000_000001", bid * 0.8, ask * 1.6)):
        for code in ("600000", "000001"):
            row = {"code": code, "bid1_volume": b, "ask1_volume": a,
                   "bid_volume_5": b * 2, "ask_volume_5": a * 2,
                   "open_times": 0.0, "boards": 2.0,
                   "snapshot_ts": "2026-11-06T10:00:00+08:00"}
            if with_frame_id:
                row["frame_id"] = fid
            rows.append(row)
    return pd.DataFrame(rows)


def test_depth_joins_on_frame_id_and_computes_normalized_imbalance():
    """OBI 必须来自同一批快照的两端；分母归一化，不排原始量。"""
    out = feat.build_features(_quotes(), "20261106", tape=_tape())
    assert out["tape_status"] == "ok" and out["depth_available"] is True
    assert out["depth_rows"] == 4                 # 2 帧 × 2 只票
    frame = out["frame"]
    first = frame[(frame["frame_id"] == "093000_000001")
                  & (frame["code"] == "600000")].iloc[0]
    assert round(first["order_imbalance_1"], 4) == 0.3333
    assert round(first["order_imbalance_5"], 4) == 0.3333
    assert first["bid1_volume"] == 100.0         # 原始量保留
    mid = frame[frame["frame_id"] == "094257_999355"].iloc[0]
    assert pd.isna(mid["bid1_volume"])            # 该帧 tape 没采到，不补 0


def test_zero_depth_denominator_is_none_not_a_fake_balance():
    out = feat.build_features(_quotes(), "20261106",
                              tape=_tape(bid=0.0, ask=0.0))
    frame = out["frame"]
    assert frame["order_imbalance_1"].isna().all()
    assert frame["order_imbalance_5"].isna().all()


def test_seal_change_rate_uses_adjacent_real_frames():
    out = feat.build_features(_quotes(), "20261106", tape=_tape())
    rows = out["frame"][out["frame"]["code"] == "600000"].sort_values("frame_id")
    assert round(rows["seal_change_rate"].iloc[-1], 4) == -0.2   # 100 → 80


def test_a_legacy_tape_is_explicit_and_never_used_as_a_join_key():
    """snapshot_ts 不是批次键；缺 frame_id 就如实说取不到深度。"""
    out = feat.build_features(_quotes(), "20261106",
                              tape=_tape(with_frame_id=False))
    assert out["ok"] is True                    # 特征仍产出，不拖垮日更
    assert out["tape_status"] == "legacy_partition_no_frame_id"
    assert out["depth_available"] is False and out["depth_rows"] == 0
    assert out["frame"]["order_imbalance_1"].isna().all()


def test_a_zero_last_frame_is_missing_not_a_minus_100_percent_return():
    """停牌/缺价的最后一帧写成 0 时，前向收益与前帧收益都必须是缺失。

    旧口径只对分母 `last > 0` 过滤，分子 `target` 仍是 0：这只票全天每一帧
    的 `forward_to_close_pct` 都精确等于 -100，直接把桶均值拉穿。
    """
    quotes = _quotes()
    last = quotes.index[(quotes["code"] == "600000")
                        & (quotes["frame_id"] == "100000_000001")][0]
    quotes.loc[last, "last"] = 0.0
    out = feat.build_features(quotes, "20261106")
    rows = out["frame"][out["frame"]["code"] == "600000"].sort_values("frame_id")
    assert rows["forward_to_close_pct"].isna().all()
    assert pd.isna(rows["frame_return_pct"].iloc[-1])


def test_depth_merge_survives_quotes_that_already_carry_order_book_columns():
    """2026-11-12 日更失败的根因。

    rt_quote 快照本身带 bid1_volume / ask1_volume / bid_volume_5 /
    ask_volume_5，_merge_depth 又 merge 了 pool_tape 的同名列，pandas 生成
    _x/_y 后缀后 merged["bid1_volume"] 抛 KeyError，整条派生失败，
    daily_done 把 intraday_features 记成失败步骤、gate 染红。
    这里同时钉死：tape 命中时用 tape 的值，未命中时回退快照值，
    depth_rows 只统计 tape 真正命中的帧，不能把回退算成深度覆盖。
    """
    quotes = _quotes()
    for column in ("bid1_volume", "ask1_volume", "bid_volume_5", "ask_volume_5"):
        quotes[column] = 999.0

    out = feat.build_features(quotes, "20261106", tape=_tape(bid=100.0, ask=50.0))
    assert out["ok"] is True and out["tape_status"] == "ok"
    assert out["depth_rows"] == 4, "只有 2 帧 x 2 只票从 tape 命中盘口"

    frame = out["frame"]
    hit = frame[(frame["frame_id"] == "093000_000001")
                & (frame["code"] == "600000")].iloc[0]
    assert hit["bid1_volume"] == 100.0, "tape 命中时必须用 tape 的盘口"
    assert round(hit["order_imbalance_1"], 4) == 0.3333

    missed = frame[(frame["frame_id"] == "094257_999355")
                   & (frame["code"] == "600000")].iloc[0]
    assert missed["bid1_volume"] == 999.0, "tape 未命中时回退快照自带盘口"
