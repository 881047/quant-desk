"""Safety contracts for the standalone collection and maintenance scripts."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from service.adapters.pit import TradingCalendarStore
from service.kernel.clock import SHANGHAI

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calendar(*days: str) -> TradingCalendarStore:
    return TradingCalendarStore.from_frame(pd.DataFrame({
        "cal_date": list(days), "is_open": [1] * len(days),
    }))


@pytest.mark.parametrize("ann_id", ["../escape", "..\\escape", "/tmp/escape", "1/2", ""])
def test_announcement_ids_cannot_escape_the_cache(tmp_path, ann_id):
    """A bad id is reported, not raised — the collector walks 218k rows and one
    malformed id must not stop the loop. What matters is that nothing is written:
    `cache_path` returns None and `write` refuses outright."""
    from service.adapters import anns_text

    result = anns_text.fetch_one(object(), {"announcement_id": ann_id}, tmp_path)

    assert result["status"] == "bad_id"
    assert anns_text.cache_path(tmp_path, ann_id) is None
    with pytest.raises(ValueError, match="invalid announcement_id"):
        anns_text.write(tmp_path, {"announcement_id": ann_id, "text": "x"})
    assert not (tmp_path / "cache" / "escape.json").exists()


def test_announcement_cache_write_is_atomic(tmp_path, monkeypatch):
    """临时文件 + os.replace。进程死在一个 5 MB 的 PDF 中途时，留下的不能是半个
    JSON——那会被下一次读当成"已缓存"，于是这份公告永远拿不到正文。"""
    from service.adapters import anns_text

    path = anns_text.cache_path(tmp_path, "123456")
    replaced = []
    real_replace = anns_text.os.replace

    def recording_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(anns_text.os, "replace", recording_replace)
    anns_text.write(tmp_path, {"announcement_id": "123456", "text": "正文"})

    assert json.loads(path.read_text(encoding="utf-8"))["text"] == "正文"
    assert replaced == [(replaced[0][0], path)]
    assert replaced[0][0].parent == path.parent
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("argv", [
    ["--dates", "20261023", "--datasets", "ths_limit_stat"],
    ["--start", "20261001", "--datasets", "ths_anomaly"],
])
def test_today_only_fetch_rejects_historical_requests(monkeypatch, argv):
    mod = _script("fetch_ths_special")
    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_today", lambda: "20261026")
    monkeypatch.setattr(mod, "Registry", lambda _path: object())
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: object())
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar("20261023", "20261026"))
    monkeypatch.setattr(mod, "_Fetcher", lambda _args: pytest.fail("rejected request must not fetch"))

    assert mod.main(argv + ["--dry-run"]) == 2


def test_auction_historical_window_is_allowed_by_the_verified_rolling_year(monkeypatch):
    mod = _script("fetch_ths_special")

    class EmptyRegistry:
        def list_manifest(self, **_kwargs):
            return []

    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_today", lambda: "20261026")
    monkeypatch.setattr(mod, "Registry", lambda _path: EmptyRegistry())
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: object())
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar("20261023", "20261026"))
    monkeypatch.setattr(mod, "_Fetcher", lambda _args: pytest.fail("dry run must not fetch"))

    assert mod.main(["--end", "20261023", "--datasets", "ths_auction", "--dry-run"]) == 0


def test_freeze_auction_refuses_to_file_a_late_capture_under_another_day(monkeypatch):
    mod = _script("fetch_ths_special")
    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "Registry", lambda _path: object())
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: object())
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar("20261023", "20261026"))

    assert mod.main(["--freeze-auction", "--dates", "20261023", "--dry-run"]) == 2


def test_today_only_response_with_wrong_session_is_not_written(monkeypatch):
    mod = _script("fetch_ths_special")

    class FakeRegistry:
        def list_manifest(self, **_kwargs):
            return []

    class FakeStore:
        def __init__(self):
            self.registry = FakeRegistry()
            self.writes = []

        def write(self, *args, **kwargs):
            self.writes.append((args, kwargs))

    store = FakeStore()
    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_today", lambda: "20261026")
    monkeypatch.setattr(mod, "Registry", lambda _path: store.registry)
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: store)
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar("20261026"))

    class WrongDayFetcher:
        def __init__(self, _args):
            pass

        def get(self, _dataset, _day):
            return pd.DataFrame({"trade_date": ["20261023"], "ts_code": ["000001.SZ"]})

    monkeypatch.setattr(mod, "_Fetcher", WrongDayFetcher)

    assert mod.main(["--dates", "20261026", "--datasets", "ths_anomaly", "--sleep", "0"]) == 1
    assert store.writes == []


def test_existing_auction_freeze_is_preserved_on_a_same_day_rerun(monkeypatch):
    mod = _script("fetch_ths_special")

    class FakeRegistry:
        def list_manifest(self, **_kwargs):
            return []

        def get_manifest(self, dataset, _day):
            return object() if dataset == "ths_auction_freeze" else None

        def mark_ingest(self, *_args, **_kwargs):
            return None

    class FakeStore:
        def __init__(self):
            self.registry = FakeRegistry()
            self.writes = []

        def write(self, dataset, *args, **kwargs):
            self.writes.append((dataset, kwargs.get("mode")))

    store = FakeStore()
    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_today", lambda: "20261026")
    monkeypatch.setattr(mod, "Registry", lambda _path: store.registry)
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: store)
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar("20261026"))

    class Fetcher:
        def __init__(self, _args):
            pass

        def get(self, _dataset, _day):
            frame = pd.DataFrame({"trade_date": ["20261026"], "ts_code": ["000001.SZ"],
                                  "warn_name": ["竞价抢筹"]})
            frame.attrs["capture"] = {"complete": True}
            return frame

    monkeypatch.setattr(mod, "_Fetcher", Fetcher)
    assert mod.main(["--freeze-auction", "--sleep", "0"]) == 0
    assert store.writes == [("ths_auction", "replace")], \
        "freeze is preserved while the mutable auction snapshot is wholly replaced"


def test_close_snapshot_uses_each_quote_time_and_drops_outliers():
    # 抓取本体在 service 里（2026-11-05 从 scripts/ 移出，API 不再 import 脚本）；
    # 这个用例测的是"每行用自己的报价时间"，属于抓取，不属于窗口调度。
    from service.trading.snapshot import _prepare_quotes

    now = datetime(2026, 10, 26, 15, 10, tzinfo=SHANGHAI)
    stamps = [
        pd.Timestamp("2026-10-26 14:59:00", tz=SHANGHAI),
        pd.Timestamp("2026-10-26 14:58:00", tz=SHANGHAI),
        pd.Timestamp("2026-10-23 14:59:00", tz=SHANGHAI),
        pd.Timestamp("2026-10-26 16:00:00", tz=SHANGHAI),
        pd.Timestamp("2026-10-27 09:30:00", tz=SHANGHAI),
    ]
    quotes = pd.DataFrame({
        "code": ["A", "B", "old", "future", "next-day"],
        "quote_ts": [stamp.timestamp() for stamp in stamps],
    })

    clean, day = _prepare_quotes(quotes, now, label_from_quotes=True)

    assert day == "20261026"
    assert clean["code"].tolist() == ["A", "B"]
    assert clean["snapshot_ts"].tolist() == [
        stamp.isoformat(timespec="seconds") for stamp in stamps[:2]
    ]
    assert clean["snapshot_ts"].nunique() == 2
    assert clean["trade_date"].tolist() == [day, day]


def test_snapshot_loop_stops_on_non_trading_days_and_at_close():
    mod = _script("snapshot_intraday")
    cal = _calendar("20261026")

    assert mod._session_open(datetime(2026, 10, 26, 14, 59, 59, tzinfo=SHANGHAI), cal)
    assert not mod._session_open(datetime(2026, 10, 26, 15, 0, 0, tzinfo=SHANGHAI), cal)
    assert not mod._session_open(datetime(2026, 10, 25, 10, 0, 0, tzinfo=SHANGHAI), cal)


def test_reclassify_rejects_every_non_anns_partition(monkeypatch):
    mod = _script("reclassify_anns")
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: pytest.fail("must reject before reading"))

    assert mod.main(["--dataset", "daily", "--partition", "20261026"]) == 2


def test_snapshot_windows_cover_every_hour_that_a_board_can_seal():
    """窗口必须盖住整段封板时间，而不只是首封与尾盘。

    2026-11-03 的第一版默认只抓 09:25-10:00 与 14:30-15:00，理由是"中间三小时
    封单基本不动"。那句话对封单额的斜率成立，对"封这件事有没有发生"不成立：
    22 个交易日 544 笔炸板样本里，首次封板时间落在 10:00-14:30 的占 **50.0%**
    （295 笔"封一次就炸"里 50.2%）。只抓首尾等于训练集少一半，而且少的那一半
    没有系统性——是随机的。
    """
    mod = _script("snapshot_intraday")
    windows = mod._parse_windows(mod.DEFAULT_WINDOWS)

    at = lambda h, m: datetime(2026, 11, 4, h, m, tzinfo=SHANGHAI)
    assert mod._window_state(at(9, 24), windows) == "between"
    assert mod._window_state(at(9, 25), windows) == "inside", "首封窗口含起点"
    # 10:00 起必须还在窗口内：这正是上一版漏掉的那一半。
    # 午休（11:30–13:00）除外——那 90 分钟的池子就是 11:30 那一刻，抓了等于重复写。
    for hour in range(10, 15):
        for minute in (0, 30):
            if (hour, minute) == (11, 30) or hour == 12:
                continue
            assert mod._window_state(at(hour, minute), windows) == "inside", (hour, minute)
    assert mod._window_state(at(11, 29), windows) == "inside"
    assert mod._window_state(at(11, 30), windows) == "between", "午休不抓：池子没变化"
    assert mod._window_state(at(12, 59), windows) == "between"
    assert mod._window_state(at(13, 0), windows) == "inside"
    assert mod._window_state(at(15, 0), windows) == "after", "最后一个窗口结束后自己退出"


def test_snapshot_sampling_is_dense_at_the_edges_and_coarse_in_the_middle():
    """密度按需要给：首封与尾盘 30 秒，盘中 3 分钟。

    盘中要的是覆盖面，不是分辨率——封单金额在那里变化慢，但"这一只是不是
    在这个窗口封的"必须看得到。
    """
    mod = _script("snapshot_intraday")
    windows = mod._parse_windows(mod.DEFAULT_WINDOWS)

    at = lambda h, m: datetime(2026, 11, 4, h, m, tzinfo=SHANGHAI)
    assert mod._window_interval(at(9, 30), windows, 30) == 30
    assert mod._window_interval(at(14, 45), windows, 30) == 30
    assert mod._window_interval(at(11, 0), windows, 30) == 180, "盘中放稀，但不放过"
    assert mod._window_interval(at(13, 30), windows, 30) == 180
    # 没配间隔的窗口沿用 --loop
    plain = mod._parse_windows("09:25-10:00")
    assert mod._window_interval(at(9, 30), plain, 7) == 7


def test_snapshot_windows_parse_and_reject_bad_input():
    mod = _script("snapshot_intraday")
    assert mod._parse_windows("09:25-10:00") == [((9, 25), (10, 0), None)]
    assert mod._parse_windows("10:00-11:30/180") == [((10, 0), (11, 30), 180)]
    with pytest.raises(SystemExit):
        mod._parse_windows("10:00-09:25")
    with pytest.raises(SystemExit):
        mod._parse_windows("9点半-10:00")
    with pytest.raises(SystemExit):
        mod._parse_windows("09:25-10:00/0"), "间隔为 0 会变成忙等"
