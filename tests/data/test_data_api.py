"""Isolated regression tests for data API dataset-read pushdown."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from service.adapters.brief import build_brief
from service.adapters.pit import TradingCalendarStore
from service.api import data as data_api


class RecordingStore:
    def __init__(self, *frames):
        self._frames = iter(frames)
        self.calls = []

    def read(self, dataset, start=None, end=None, **kwargs):
        self.calls.append((dataset, start, end, kwargs))
        frame = next(self._frames).copy()
        columns = kwargs.get("columns")
        return frame.loc[:, columns].copy() if columns else frame


class FakeRegistry:
    def __init__(self, summary):
        self._summary = summary

    def summary(self):
        return self._summary


def _install(monkeypatch, store, summary):
    monkeypatch.setattr(data_api, "get_store", lambda: store)
    monkeypatch.setattr(data_api, "get_registry", lambda: FakeRegistry(summary))


def _without_as_of(payload):
    payload.pop("as_of")
    return payload


def test_status_uses_manifest_rows_and_reads_only_latest_quote_day(monkeypatch, tmp_path):
    summary = [{
        "dataset": "rt_quote", "rows": 9, "cover_end": "20240108",
        "quarantine_partitions": 0,
    }]
    latest = "2024-01-08T15:00:00+00:00"
    store = RecordingStore(pd.DataFrame({
        "snapshot_ts": ["2024-01-08T14:59:00+00:00", latest],
        "last": [10.0, 10.1],
    }))
    _install(monkeypatch, store, summary)
    monkeypatch.setattr(data_api, "_data_root", lambda: tmp_path)

    now = datetime(2024, 1, 8, 15, 1, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(data_api, "datetime", FixedDatetime)
    # 派生池的健康检查单独测（在下文），这里钉住它，好让这条断言只测 manifest 与行情。
    monkeypatch.setattr(data_api, "_derived_health",
                        lambda summary: {"status": "fresh", "day": "20240108"})

    payload = data_api.status()
    # A3：数据集带类型标志后，按 cover_end 判新鲜度的消费方才有分流依据。
    # rt_quote 是快照，cover_end 不是覆盖边界；日历类单独成一类。
    assert payload["datasets"][0]["kind"] == "snapshot"
    assert payload["datasets"][0]["coverage_boundary"] is False
    assert "calendar" in payload["dataset_kinds"]
    assert "coverage_boundary=true" in payload["dataset_kind_note"]
    payload.pop("dataset_kinds")
    payload.pop("dataset_kind_note")
    payload["datasets"] = [
        {key: value for key, value in row.items()
         if key not in ("kind", "coverage_boundary")}
        for row in payload["datasets"]
    ]
    assert payload == {
        "as_of": now.astimezone().isoformat(timespec="seconds"),
        "datasets": summary,
        "missing_required": ["trade_cal", "stock_basic", "daily", "adj_factor", "stk_limit"],
        "rt_quote": {
            "rows": 9, "latest_snapshot": latest, "age_seconds": 60.0, "stale": False,
        },
        "derived_pools": {"status": "fresh", "day": "20240108"},
        "quarantined": [],
        "last_backfill": None,
    }
    assert store.calls == [
        ("rt_quote", "20240108", "20240108", {"columns": ["snapshot_ts"]}),
    ]


def _brief_summary():
    return [
        {
            "dataset": "daily", "cover_end": "20240104", "quarantine_partitions": 0,
            "warn_partitions": 0, "last_snapshot_id": "daily-v1", "rows": 100,
        },
        {
            "dataset": "stk_limit", "cover_end": "20240108", "quarantine_partitions": 0,
            "warn_partitions": 0, "last_snapshot_id": "limits-v1", "rows": 100,
        },
    ]


def _calendar_frame():
    return pd.DataFrame({
        "cal_date": [
            "20240102", "20240103", "20240104", "20240105",
            "20240106", "20240107", "20240108", "20240109",
        ],
        "is_open": [1, 1, 1, 1, 0, 0, 1, 1],
        "pretrade_date": [
            "20231229", "20240102", "20240103", "20240104",
            "20240105", "20240105", "20240105", "20240108",
        ],
    })


def test_brief_reads_open_calendar_through_target_without_changing_output(monkeypatch):
    full = _calendar_frame()
    window = full[(full["cal_date"] <= "20240108") & (full["is_open"] == 1)]
    store = RecordingStore(window)
    summary = _brief_summary()
    _install(monkeypatch, store, summary)
    monkeypatch.setattr(data_api, "_latest_rt", lambda day: (0, None, 5.0))
    # `brief` now reads `_sentiment_snapshot`, which falls back to the last
    # session's file and reports its date. Pinning `_sentiment` here stopped
    # having any effect the moment that changed — the assertion still passed the
    # day it was written and would have passed forever after.
    monkeypatch.setattr(data_api, "_sentiment_snapshot", lambda day: {"tone": "修复"})
    monkeypatch.setattr(data_api, "_derived_health", lambda summary: {"status": "fresh"})

    expected = build_brief(
        summary=summary,
        calendar=TradingCalendarStore.from_frame(full),
        today="20240108",
        rt_age_seconds=5.0,
        sentiment={"tone": "修复"},
    )
    expected["derived_pools"] = {"status": "fresh"}
    actual = data_api.brief("20240108")

    assert _without_as_of(actual) == _without_as_of(expected)
    assert store.calls == [
        (
            "trade_cal", None, "20240108",
            {"columns": ["cal_date"], "filters": [("is_open", "==", 1)]},
        ),
    ]


def test_brief_before_first_session_keeps_weekday_fallback(monkeypatch):
    full = _calendar_frame()
    empty = full.iloc[0:0]
    store = RecordingStore(empty, full[full["is_open"] == 1])
    summary = _brief_summary()
    _install(monkeypatch, store, summary)
    monkeypatch.setattr(data_api, "_latest_rt", lambda day: (0, None, None))
    monkeypatch.setattr(data_api, "_sentiment_snapshot", lambda day: None)
    monkeypatch.setattr(data_api, "_derived_health", lambda summary: {"status": "fresh"})

    expected = build_brief(
        summary=summary,
        calendar=TradingCalendarStore.from_frame(full),
        today="20240101",
        rt_age_seconds=None,
        sentiment=None,
    )
    expected["derived_pools"] = {"status": "fresh"}
    actual = data_api.brief("20240101")

    assert _without_as_of(actual) == _without_as_of(expected)
    assert actual["is_trading_day"] is True
    assert len(store.calls) == 2
    assert store.calls[1] == (
        "trade_cal", None, None,
        {"columns": ["cal_date"], "filters": [("is_open", "==", 1)]},
    )


# ------------------------------------------------------------ derived health

def test_brief_reads_the_manifest_once_for_the_whole_answer(monkeypatch):
    """一次请求查两遍 ``summary()``：多花 5 ms，还可能跨过 17:30 的刷新。

    ``build_brief`` 与 ``_derived_health`` 各问 registry 一次。两次之间有
    5 ms 的窗口，正好够一次日更把清单指针推进一格：于是 brief 说 ``daily``
    覆盖到 20261105，而 ``derived_pools`` 按 20261104 的 ``cover_end`` 算——
    同一个响应里两个日子，读的人没有任何线索。
    """
    full = _calendar_frame()
    window = full[(full["cal_date"] <= "20240108") & (full["is_open"] == 1)]
    store = RecordingStore(window)

    rows = _brief_summary()
    reads: list[list] = []

    class CountingRegistry:
        def summary(self):
            reads.append(rows)
            return rows

    monkeypatch.setattr(data_api, "get_store", lambda: store)
    monkeypatch.setattr(data_api, "get_registry", lambda: CountingRegistry())
    monkeypatch.setattr(data_api, "_latest_rt", lambda day: (0, None, 5.0))
    monkeypatch.setattr(data_api, "_sentiment_snapshot", lambda day: None)

    seen: list[list] = []
    monkeypatch.setattr(data_api, "_derived_health",
                        lambda summary: seen.append(summary) or {"status": "fresh"})

    data_api.brief("20240108")

    assert len(reads) == 1, f"一次 brief 查了 {len(reads)} 遍 manifest"
    assert seen and seen[0] is rows, "派生池拿到的不是 brief 用的那份清单"

def test_brief_carries_the_pipeline_state_for_today(monkeypatch, tmp_path):
    """覆盖到没有，和跑完没有，是两个问题。

    `core_ok` 说的是每个数据集覆盖到哪一天；日线先落库、池子后落库，崩在中间时
    两者会分叉。这一块只在问当天时给——历史某天要的不是"今晚能不能用"。
    """
    from service.adapters import pipeline_state as ps

    full = _calendar_frame()
    window = full[(full["cal_date"] <= "20240108") & (full["is_open"] == 1)]
    store = RecordingStore(window, window, window, window)
    _install(monkeypatch, store, _brief_summary())
    monkeypatch.setattr(data_api, "_latest_rt", lambda day: (0, None, 5.0))
    monkeypatch.setattr(data_api, "_sentiment_snapshot", lambda day: None)
    monkeypatch.setattr(data_api, "_derived_health", lambda summary: {"status": "fresh"})
    monkeypatch.setattr(data_api, "_today", lambda: "20240108")
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))

    no_marker = data_api.brief(None)
    assert no_marker["pipeline"]["state"] in ("missing", "pending", "not_required")

    ps.write_marker(tmp_path, "20240108", "close",
                    {"gate": "not_ready", "failed_steps": ["ths"]})
    after = data_api.brief(None)
    assert after["pipeline"]["state"] == "done_not_ready"
    assert after["pipeline"]["failed_steps"] == ["ths"]

    # A historical day still answers the old question only.
    assert "pipeline" not in data_api.brief("20240105")


def test_derived_health_says_unknown_when_daily_has_no_coverage(monkeypatch):
    """没有 daily 的覆盖区间，就不知道该检查哪一天——不猜，也不返回 0。"""
    _install(monkeypatch, RecordingStore(), [{"dataset": "rt_quote", "rows": 1}])

    got = data_api._derived_health([{"dataset": "rt_quote", "rows": 1}])

    assert got["status"] == "unknown"
    assert "daily" in got["reason"]


def test_derived_health_never_raises_on_a_broken_store(monkeypatch):
    """状态页必须能渲染：读池子炸了也只写 error，不把 /data/status 带下去。"""
    class Broken:
        def read(self, *a, **k):
            raise RuntimeError("store down")

    monkeypatch.setattr(data_api, "get_store", lambda: Broken())

    got = data_api._derived_health([{"dataset": "daily", "cover_end": "20240108"}])

    assert got["status"] == "error"
    assert "store down" in got["reason"]


# ------------------------------------------------------- sentiment freshness

def _write_snapshot(root, day: str, *, tone: str, as_of: str = "",
                    quality: str | None = "ok") -> None:
    import json

    # 形状要与生产一致。这里原来写的是 ``{"quality": {"ok": True}}``——一个
    # 生产端从来不会写的 dict，于是 ``_sentiment_snapshot`` 里那个
    # ``isinstance(quality, dict)`` 分支只在测试里成立、在线上永远走不到。
    # 夹具写错形状，比没有夹具更糟：它会让一个死分支看起来是活的。
    (root / "sentiment").mkdir(parents=True, exist_ok=True)
    payload = {"trade_date": day, "as_of": as_of or f"{day[:4]}-{day[4:6]}-{day[6:]}T17:30:00+08:00",
               "sentiment": {"tone": tone, "limit_up": 47},
               "quality": quality}
    (root / "sentiment" / f"{day}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_missing_today_snapshot_falls_back_to_the_last_session(monkeypatch, tmp_path):
    """周末、日更前、漏跑快照：上一交易日的那份仍然要能看到，并标明是哪天。

    以前 ``_sentiment`` 只认当天文件，于是 ``/context`` 在周六报 ``stage: unknown``
    —— 而上一交易日的完整情绪就躺在磁盘上，那是模型唯一能看到的一份。报空丢掉
    了唯一的输入，报成"今天"则是撒谎，所以两个都要避免：回退 + 带上日期。
    """
    _write_snapshot(tmp_path, "20261030", tone="退潮")
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(data_api, "_sessions_between", lambda a, b: 0)

    got = data_api._sentiment_snapshot("20261031")

    assert got["tone"] == "退潮"
    assert got["trade_date"] == "20261030"
    assert got["sessions_behind"] == 0, "10-30 就是最近一个交易日，没有落后"


def test_a_stale_snapshot_says_how_many_sessions_behind_it_is(monkeypatch, tmp_path):
    _write_snapshot(tmp_path, "20261020", tone="主升")
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(data_api, "_sessions_between", lambda a, b: 7)

    got = data_api._sentiment_snapshot("20261030")

    assert got["sessions_behind"] == 7
    assert "20261020" in got["note"]
    assert "7 个交易日" in got["note"]


def test_a_snapshot_from_the_future_is_never_used(monkeypatch, tmp_path):
    """回退只能往回走。用一份"未来的"快照等于把后视镜当挡风玻璃。"""
    _write_snapshot(tmp_path, "20261030", tone="退潮")
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))

    assert data_api._sentiment_snapshot("20261023") is None


def test_the_quote_verdict_never_rides_along_with_the_sentiment_block(monkeypatch, tmp_path):
    """``quality`` 是实时报价的判定，不是这段情绪的判定，所以不往外给。

    池的家数来自厂商池，跟一帧 tick 无关；报价质量有自己的出口（``/data/brief``、
    ``/readiness``、``quality_report``）。挂在情绪块旁边，``quarantine`` 会被读成
    "情绪被判为隔离"——而它可能只是当天的停牌股（占全市场 6%）被判成了坏行
    （那条误判已修，但即便判定正确，它说的也不是这件事）。
    """
    _write_snapshot(tmp_path, "20261030", tone="退潮", quality="quarantine")
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(data_api, "_sessions_between", lambda a, b: 0)

    got = data_api._sentiment_snapshot("20261030")

    assert "quality" not in got
    assert "quotes_quality" not in got
    assert got["tone"] == "退潮", "报价的判定不该影响情绪本身"


def test_a_snapshot_that_never_looked_at_quotes_says_nothing_about_them(monkeypatch, tmp_path):
    """日更那份判不出来时写 None。没评过就不该冒出一个结论。"""
    _write_snapshot(tmp_path, "20261030", tone="主升", quality=None)
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(data_api, "_sessions_between", lambda a, b: 0)

    got = data_api._sentiment_snapshot("20261030")

    assert "quality" not in got and "quotes_quality" not in got
    assert got["tone"] == "主升"
