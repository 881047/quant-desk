"""这一帧有多"现在"：时钟、新鲜度、以及涨跌停数的来源。

这个模块最容易骗人的地方不是算错，是**看起来像实时**。所以测试集中在三处：

* 帧的时刻只能来自文件名（行里的 ``snapshot_ts`` 是每只票自己的时间，停牌股一整天
  都是 08:00）；
* ``frame_is_stale`` 必须带时段——收盘后 age 一直涨是正常的，盘中不涨才是正常的；
* 涨跌停数来自 ``stk_limit`` 的当日限价，不是按幅度重算。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.trading import frame as live_frame

DAY = "20261105"


def _frame_root(
    tmp_path: Path, name: str, rows: list[dict], limits: list[dict] | None = None
) -> Path:
    root = tmp_path / "data"
    archive = root / "rt_snapshots" / DAY
    archive.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(archive / f"{name}.parquet", index=False)
    if limits is not None:
        # ``trade_date`` 是必须的：读的时候按它过滤当日分区，少了这一列过滤会失败，
        # 而失败会被当成"限价表不可用"——一个测试数据缺陷看起来像生产缺陷。
        (root / "parquet" / "stk_limit").mkdir(parents=True)
        pd.DataFrame([{**row, "trade_date": DAY} for row in limits]).to_parquet(
            root / "parquet" / "stk_limit" / "2026.parquet", index=False
        )
    return root


def _row(code: str, market: int, last: float | None, pre: float, name: str = "") -> dict:
    return {
        "code": code,
        "market": market,
        "name": name or code,
        "last": last,
        "pre_close": pre,
        "amount": 1.0e8,
    }


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, second, tzinfo=SHANGHAI)


def test_the_batch_clock_comes_from_the_filename_not_from_a_row(desk, tmp_path):
    """A halted name keeps 08:00 all day; it must not date the batch.

    ``snapshot_ts`` is per row. Taking its max answers "which quote is freshest",
    and taking its min would answer "the oldest quote in the file" — neither is
    when the capture ran. The filename is the batch clock.
    """
    root = _frame_root(
        tmp_path,
        "101530_000001",
        [
            _row("600000", 1, 11.0, 10.0, "浦发银行"),
            {
                **_row("000003", 0, None, 2.71, "PT金田A"),
                "snapshot_ts": "2026-11-05T08:00:00+08:00",
            },
        ],
    )
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16))

    assert out["ok"] is True
    assert out["frame_id"] == "101530_000001"
    assert out["frame_at"].startswith("2026-11-05T10:15:30")
    assert out["frame_age_seconds"] == 30


def test_a_missing_archive_says_so_instead_of_reporting_an_empty_market(desk, tmp_path):
    """No frames is a pipeline fault, not "zero gainers"."""
    out = live_frame.build(desk, day=DAY, root=tmp_path / "nowhere", now=_at(10, 16))

    assert out["ok"] is False
    assert out["error"] == "no_frames"
    assert "snapshot_intraday" in out["note"]


def test_a_large_age_after_the_close_is_not_staleness(desk, tmp_path):
    """The capture stops when the market does; age then only grows.

    Marking that stale would make the field true every evening, and a flag that
    is always true stops being read.
    """
    root = _frame_root(tmp_path, "145927_000001", [_row("600000", 1, 11.0, 10.0)])
    out = live_frame.build(desk, day=DAY, root=root, now=_at(18, 30))

    assert out["frame_age_seconds"] == 3 * 3600 + 30 * 60 + 33
    assert out["session"]["in_session"] is False
    assert out["frame_is_stale"] is False
    assert "不是 stale" in out["note"]


def test_the_same_age_during_the_session_is_staleness(desk, tmp_path):
    """Missing two whole batches during a live session means the writer died."""
    root = _frame_root(tmp_path, "101500_000001", [_row("600000", 1, 11.0, 10.0)])
    # 10:23:20 - 10:15:00 = 500 秒，跨过 FRAME_STALE_AFTER（360 = 两个批次）。
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 23, 20))

    assert out["frame_age_seconds"] == 500
    assert out["session"]["in_session"] is True
    assert out["frame_is_stale"] is True
    assert "quant_data_health" in out["note"]


def test_a_normal_intraday_gap_is_not_staleness(desk, tmp_path):
    """The writer runs every 3 minutes mid-session, so 180s is a healthy frame.

    ``realtime.stale_after`` is 120s and applies to a single quote. Reusing it
    here would flag every ordinary batch of the day.
    """
    root = _frame_root(tmp_path, "101500_000001", [_row("600000", 1, 11.0, 10.0)])
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 18))

    assert out["frame_age_seconds"] == 180
    assert out["frame_is_stale"] is False


def test_the_limit_counts_come_from_the_limit_table(desk, tmp_path):
    """Ten percent is not a rule this module is allowed to own.

    Main board is 10%, ChiNext/STAR 20%, ST 5%, Beijing 30% — the table already
    resolved all four per name. Recomputing the band here would be a second
    definition, and the two would drift the first time the exchange changes one.
    """
    root = _frame_root(
        tmp_path,
        "101500_000001",
        [
            _row("600000", 1, 11.0, 10.0, "浦发银行"),  # 封在 11.00
            _row("300001", 0, 9.9, 10.0, "特锐德"),  # 没封
            _row("000003", 0, None, 2.71, "PT金田A"),  # 无报价
        ],
        limits=[
            {"ts_code": "600000.SH", "up_limit": 11.0, "down_limit": 9.0},
            {"ts_code": "300001.SZ", "up_limit": 12.0, "down_limit": 8.0},
        ],
    )
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16))

    assert out["limits"]["available"] is True
    assert out["limits"]["up"] == 1
    assert out["limits"]["down"] == 0
    # 只命中两只：PT 股在限价表里没有行，它也就不该被算进涨跌家数。
    assert out["limits"]["matched"] == 2
    assert out["breadth"]["rows"] == 2


def test_an_unreadable_limit_table_disables_the_counts_without_failing(desk, tmp_path):
    """A missing partition is one unavailable field, not a dead endpoint."""
    root = _frame_root(tmp_path, "101500_000001", [_row("600000", 1, 11.0, 10.0)])
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16))

    assert out["ok"] is True
    assert out["limits"]["available"] is False
    assert "stk_limit" in out["limits"]["note"]
    assert out["breadth"]["advancers"] == 1


def test_limit_join_survives_a_frame_that_already_carries_limit_columns():
    """2026-11-12 日更后暴露：rt_quote 快照自带 up_limit / down_limit。

    直接 merge 同名的限价表会得到 up_limit_x / up_limit_y，
    joined["up_limit"] 抛 KeyError，/market/frame 整个 500。
    这里同时钉死另一件事：即使能合并，也必须用 stk_limit 的当日限价，
    不能用快照里那份可能过期的数字（故意给 999 / 0.01）。
    """
    frame = pd.DataFrame({
        "ts_code": ["600000.SH", "300001.SZ"],
        "last": [11.0, 9.9],
        "up_limit": [999.0, 999.0],
        "down_limit": [0.01, 0.01],
    })
    table = pd.DataFrame({
        "ts_code": ["600000.SH", "300001.SZ"],
        "up_limit": [11.0, 12.0],
        "down_limit": [9.0, 8.0],
    })
    out = live_frame._limits_from(frame, table)
    assert out["available"] is True
    assert out["up"] == 1
    assert out["down"] == 0
    assert out["matched"] == 2


def _add_frame(root: Path, name: str, rows: list[dict]) -> None:
    """同一份归档里再落一帧——两帧相减的用例需要时间上的第二个点。"""
    archive = root / "rt_snapshots" / DAY
    pd.DataFrame(rows).to_parquet(archive / f"{name}.parquet", index=False)


def _two_limits() -> list[dict]:
    return [
        {"ts_code": "600000.SH", "up_limit": 11.0, "down_limit": 9.0},
        {"ts_code": "300001.SZ", "up_limit": 11.0, "down_limit": 8.0},
    ]


def test_the_delta_comes_from_two_frames(desk, tmp_path):
    """一帧没有方向：读到「涨停 2 家」，读不出是从 1 家涨上来还是从 3 家掉下来。

    同一个数字，两件相反的事。把 build 再跑一遍不会给出变化——只有把两帧相减才有。
    """
    root = _frame_root(
        tmp_path,
        "100000_000001",
        [_row("600000", 1, 11.0, 10.0), _row("300001", 0, 9.9, 10.0)],
        limits=_two_limits(),
    )
    _add_frame(
        root,
        "101500_000002",
        [
            _row("600000", 1, 11.0, 10.0),
            _row("300001", 0, 11.0, 10.0),  # 十五分钟后也封上了
        ],
    )
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16), lookback=15)
    delta = out["delta"]

    assert delta["available"] is True
    assert delta["since_frame_id"] == "100000_000001"
    assert delta["elapsed_seconds"] == 900
    assert delta["short_of_lookback"] is False
    assert delta["before"]["limits_up"] == 1
    assert delta["now"]["limits_up"] == 2
    assert delta["change"]["limits_up"] == 1
    # change 是差值不是百分比：0.045 → 0.100 记作 +0.055，也就是 +5.5 个百分点。
    assert delta["change"]["equal_weight_return"] == pytest.approx(0.055, abs=1e-6)


def test_a_day_younger_than_the_lookback_reports_the_narrower_window(desk, tmp_path):
    """开盘五分钟时问「这 15 分钟」没有答案，但「这 60 秒」有。

    退回最早一帧比返回空更有用，前提是把窗口说窄——否则 `change` 会被当成
    15 分钟的变动，而它只是 60 秒的。
    """
    root = _frame_root(tmp_path, "101400_000001", [_row("600000", 1, 10.5, 10.0)])
    _add_frame(root, "101500_000002", [_row("600000", 1, 11.0, 10.0)])
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16), lookback=15)

    delta = out["delta"]
    assert delta["available"] is True
    assert delta["short_of_lookback"] is True
    assert delta["elapsed_seconds"] == 60
    assert "窗口比你问的窄" in delta["note"]


def test_a_single_frame_has_nothing_to_compare_against(desk, tmp_path):
    """一帧时说"没有可比对象"，不要说"变化为零"——那是两件不同的事。"""
    root = _frame_root(tmp_path, "101500_000001", [_row("600000", 1, 11.0, 10.0)])
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16))

    assert out["delta"]["available"] is False
    assert out["delta"]["lookback_minutes"] == 15
    assert "只有一帧" in out["delta"]["note"]


def test_the_lookback_parameter_decides_which_frame_is_the_baseline(desk, tmp_path):
    """窗口是调用方要的，不是写死的。30 分钟问的必须真的是 30 分钟。

    最新一帧永远是"现在"，所以它不能同时充当基线——``archived[:-1]`` 那句切片
    就是这个意思：一帧不能和自己比出变化来。
    """
    root = _frame_root(tmp_path, "090000_000001", [_row("600000", 1, 10.0, 10.0)])
    _add_frame(root, "091500_000002", [_row("600000", 1, 10.2, 10.0)])
    _add_frame(root, "093000_000003", [_row("600000", 1, 10.5, 10.0)])

    short = live_frame.build(desk, day=DAY, root=root, now=_at(9, 31), lookback=15)["delta"]
    long = live_frame.build(desk, day=DAY, root=root, now=_at(9, 31), lookback=30)["delta"]

    assert short["since_frame_id"] == "091500_000002"
    assert short["elapsed_seconds"] == 900
    assert long["since_frame_id"] == "090000_000001"
    assert long["elapsed_seconds"] == 1800
    assert short["change"] != long["change"]


def test_frames_are_listed_in_time_order(tmp_path):
    """``HHMMSS_microseconds`` sorts lexicographically the same way it sorts in time."""
    archive = tmp_path / "rt_snapshots" / DAY
    archive.mkdir(parents=True)
    for name in ("145927_000001", "093000_500000", "112959_999999"):
        pd.DataFrame([_row("600000", 1, 11.0, 10.0)]).to_parquet(
            archive / f"{name}.parquet", index=False
        )
    (archive / "not-a-frame.parquet").write_bytes(b"")

    listed = [path.stem for path in live_frame.frames(tmp_path, DAY)]

    assert listed == ["093000_500000", "112959_999999", "145927_000001"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("093000_500000", "09:30:00.500000"),
        ("145927_012988", "14:59:27.012988"),
    ],
)
def test_the_filename_microseconds_survive_parsing(name, expected):
    stamp = live_frame.frame_time(DAY, f"{name}.parquet")
    assert stamp is not None
    assert stamp.strftime("%H:%M:%S.%f") == expected


def test_production_frame_without_market_column_matches_canonical_limit_codes(desk, tmp_path):
    """Production rt_quote has code + native limits, but no numeric market column.

    The old fallback assigned ``.NA`` to every row, so a healthy stk_limit table
    reported matched=0. Exchange inference must also preserve 920xxx = Beijing.
    """
    rows = []
    for code, last, pre, name in (
        ("605255", 11.0, 10.0, "沪市"),
        ("920001", 12.0, 10.0, "北交所"),
        ("300001", 9.9, 10.0, "创业板"),
    ):
        row = _row(code, 0, last, pre, name)
        row.pop("market")
        row.update({"up_limit": 12.0, "down_limit": 8.0})
        rows.append(row)
    root = _frame_root(
        tmp_path, "101500_000001", rows,
        limits=[
            {"ts_code": "605255.SH", "up_limit": 11.0, "down_limit": 9.0},
            {"ts_code": "920001.BJ", "up_limit": 12.0, "down_limit": 8.0},
            {"ts_code": "300001.SZ", "up_limit": 12.0, "down_limit": 8.0},
        ],
    )
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 16))
    assert out["limits"]["available"] is True
    assert out["limits"]["matched"] == 3
    assert out["limits"]["up"] == 2
    assert out["limits"]["down"] == 0
