"""The tape module: unit conversions and aggregation are where bugs hide."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from service.adapters import ths
from service.trading import tape


def _up(rows):
    return pd.DataFrame(rows, columns=["ts_code", "name", "continue_cnt",
                                       "continue_text", "seal_money", "reason",
                                       "limit_up_time"])


def test_summarize_counts_and_derives_seal_rate():
    up = _up([
        {"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 100, "reason": "机器人+AI", "limit_up_time": "09:35"},
        {"ts_code": "B", "name": "b", "continue_cnt": 2, "continue_text": "2连板",
         "seal_money": 300, "reason": "机器人", "limit_up_time": "14:10"},
        {"ts_code": "C", "name": "c", "continue_cnt": 2, "continue_text": "2连板",
         "seal_money": 200, "reason": "AI", "limit_up_time": "10:30"},
    ])
    brk = pd.DataFrame({"ts_code": ["D"]})
    stat = pd.DataFrame([{"level": "one", "stock_count": 1, "promotion_rate": None,
                          "seal_rate": None, "touched_limit_up_stock_count": 4}])

    out = tape.summarize_limit(stat, up, pd.DataFrame(), brk, pd.DataFrame())
    # ``source`` says which pool produced the counts. Without a breadth argument
    # that is the vendor, and the field is present on purpose: a caller that
    # cannot see the basis has no way to notice it is reading a list that misses
    # names (同花顺 averaged 1.5 missing limit-ups a day over 2026-09/10).
    assert out["counts"] == {"limit_up": 3, "limit_down": 0, "broken": 1, "touched": 4,
                             "break_rate": 0.25, "source": "vendor:ths"}
    # 3 sealed / (3 sealed + 1 broken)
    assert out["seal_rate"] == pytest.approx(0.75, abs=1e-6)
    assert out["seal_rate_derived"] is True
    assert out["max_streak"] == 2
    assert out["tiers"] == {"1板": 1, "2板": 2}


def test_seal_rate_from_the_vendor_is_preferred_over_the_derived_one():
    up = _up([{"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
               "seal_money": 1, "reason": "x", "limit_up_time": "09:31"}])
    stat = pd.DataFrame([{"level": "one", "stock_count": 1, "promotion_rate": None,
                          "seal_rate": 0.7, "touched_limit_up_stock_count": 10}])
    out = tape.summarize_limit(stat, up, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert out["seal_rate"] == 0.7
    assert out["seal_rate_derived"] is False


def test_first_seal_buckets_split_at_ten_and_one():
    up = _up([
        {"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 1, "reason": "x", "limit_up_time": "09:31"},
        {"ts_code": "B", "name": "b", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 1, "reason": "x", "limit_up_time": "11:05"},
        {"ts_code": "C", "name": "c", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 1, "reason": "x", "limit_up_time": "14:40"},
    ])
    out = tape.summarize_limit(None, up, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert out["first_seal"] == {"before_1000": 1, "1000_1300": 1, "after_1300": 1}


def test_limit_reasons_are_split_ranked_and_capped():
    up = _up([
        {"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 1, "reason": "机器人+AI", "limit_up_time": "09:31"},
        {"ts_code": "B", "name": "b", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 1, "reason": "机器人", "limit_up_time": "09:31"},
    ])
    out = tape.summarize_limit(None, up, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert out["top_reasons"][0] == {"tag": "机器人", "count": 2}
    assert {r["tag"] for r in out["top_reasons"]} == {"机器人", "AI"}


def test_empty_pool_is_not_reported_as_zero_limits():
    out = tape.summarize_limit(None, pd.DataFrame(), pd.DataFrame(),
                               pd.DataFrame(), pd.DataFrame())
    assert out["counts"]["limit_up"] == 0
    assert out["max_streak"] is None
    assert out["first_seal"] is None


def test_date_helpers_anchor_to_shanghai_midnight():
    # Midnight in a fixed-offset zone means consecutive days differ by exactly
    # one day in milliseconds; a naive local-time conversion would not.
    assert ths.date_ms("20261024") - ths.date_ms("20261023") == 86_400_000
    assert ths.iso_date("20261023") == "2026-10-23"


def test_lhb_units_are_converted_from_fractions_to_percent(monkeypatch):
    payload = {
        "trade_date": "2026-10-23",
        "stock_items": [{
            "thscode": "002354.SZ", "name": "天娱数科", "change": 0.0999,
            "concept_list": [{"name": "AI视频"}, {"name": "算力"}],
            "net_value": 489098145.0, "net_rate": 0.2117, "buy_value": 611352842.0,
            "sell_value": 122254697.0, "hot_rank": 7, "range_days": 1,
            "limit_reason": "具身智能", "org_net_value": 67388870.0,
            "hot_money_net_value": 401981701.0,
        }],
        "hot_money_items": [{"name": "佛山系", "rows": [
            {"thscode": "002354.SZ", "name": "天娱数科",
             "hot_money_item_net_value": 206343698.0,
             "hot_money_item_net_rate": 0.0894}]}],
    }
    monkeypatch.setattr(ths, "_get", lambda path, params=None: payload)
    stocks, seats = ths.fetch_lhb("20261023", "all")

    row = stocks.iloc[0]
    assert row["pct"] == pytest.approx(9.99, abs=1e-6), "change is a fraction"
    assert row["net_rate"] == pytest.approx(21.17, abs=1e-6), "net_rate is a fraction"
    assert row["concepts"] == "AI视频|算力"
    assert seats.iloc[0]["seat"] == "佛山系"
    assert seats.iloc[0]["net_rate"] == pytest.approx(8.94, abs=1e-6)


def test_a_vendor_error_code_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: (_ for _ in ()).throw(
        ths.ThsError(2001, "bad key", path)))
    with pytest.raises(ths.ThsError) as exc:
        ths.fetch_limit_up("20261023")
    assert exc.value.code == 2001


def test_limit_stat_does_not_fabricate_a_missing_upstream_session(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {"seal_rate": 0.5})
    with pytest.raises(ths.ThsError) as exc:
        ths.fetch_limit_stat("20261023")
    assert exc.value.code == "missing_trade_date"


def test_limit_stat_keeps_the_real_upstream_session(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "trade_date": "2026-10-24", "seal_rate": 0.5})
    stat = ths.fetch_limit_stat("20261023")
    assert stat["trade_date"] == "20261024"
    assert stat["requested_date"] == "20261023"


def test_stat_frame_keeps_summary_when_the_board_ladder_is_empty():
    frame = ths.stat_frame({"trade_date": "20261023", "page_boards": [],
                            "seal_rate": 0.5, "limit_up_stock_count": 3,
                            "touched_limit_up_stock_count": 4, "open_stock_count": 2})
    assert len(frame) == 1
    row = frame.iloc[0]
    assert pd.isna(row["level"])
    assert row["seal_rate"] == 0.5
    assert row["limit_up_stock_count"] == 3
    assert row["touched_limit_up_stock_count"] == 4
    assert row["open_stock_count"] == 2


def test_auction_rejects_instead_of_relabelling_an_upstream_session(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "trade_date": "2026-10-24",
                  "warn_name": "竞价抢筹", "warn_timestamp": 100}],
        "pagination": {"has_next": False},
    })
    with pytest.raises(ths.ThsError) as exc:
        ths.fetch_auction("20261023")
    assert exc.value.code == "trade_date_conflict"


def test_auction_pages_until_has_next_is_false(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append(params["page"])
        rows = [{"thscode": f"00000{params['page']}",
                 "name": f"s{params['page']}", "warn_type": "1",
                 "warn_name": "竞价抢筹", "warn_timestamp": 1792718700000 + params["page"],
                 "bid_price": 10.0, "bid_change_ratio": 0.05, "bid_amount": 1e7,
                 "previous_amount": 1e8}]
        return {"item": rows,
                "pagination": {"page": params["page"], "has_next": params["page"] < 3}}

    monkeypatch.setattr(ths, "_get", fake_get)
    frame = ths.fetch_auction("20261023", max_pages=10)
    assert calls == [1, 2, 3], "stop as soon as has_next is false"
    assert len(frame) == 3
    assert frame["bid_pct"].iloc[0] == pytest.approx(5.0), "ratio is stored as percent"
    assert frame["trade_date"].unique().tolist() == ["20261023"]


def test_auction_keeps_the_first_time_an_alert_fired(monkeypatch):
    rows = [
        {"thscode": "000001.SZ", "name": "a", "warn_name": "竞价抢筹",
         "warn_type": "1", "warn_timestamp": 200, "bid_price": 1, "bid_change_ratio": 0.01,
         "bid_amount": 1, "previous_amount": 1},
        {"thscode": "000001.SZ", "name": "a", "warn_name": "竞价抢筹",
         "warn_type": "1", "warn_timestamp": 100, "bid_price": 1, "bid_change_ratio": 0.01,
         "bid_amount": 1, "previous_amount": 1},
    ]
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": rows, "pagination": {"has_next": False}})
    frame = ths.fetch_auction("20261023")
    assert len(frame) == 1
    assert frame["warn_ts"].iloc[0] == 100, "the earliest trigger is the interesting one"
    assert frame["event_count"].iloc[0] == 2
    assert frame["last_warn_ts"].iloc[0] == 200
    assert len(json.loads(frame["events_json"].iloc[0])) == 2
    assert frame.attrs["capture"]["complete"] is True


def test_auction_page_cap_is_explicitly_incomplete(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "warn_name": "竞价抢筹",
                  "warn_timestamp": 100}],
        "pagination": {"has_next": True, "total": 40},
    })
    frame = ths.fetch_auction("20261023", max_pages=1)
    assert frame["capture_truncated"].tolist() == [True]
    assert frame["capture_complete"].tolist() == [False]
    assert frame.attrs["capture"] == {"pages_fetched": 1, "upstream_total": 40,
                                      "capture_rows": 1, "truncated": True,
                                      "complete": False}


def test_auction_timestamp_cannot_relabel_an_ignored_history_parameter(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "warn_name": "竞价抢筹",
                  "warn_timestamp": 1792718703000}],  # 2026-10-23 09:25:03 +08
        "pagination": {"has_next": False, "total": 1},
    })
    with pytest.raises(ths.ThsError) as exc:
        ths.fetch_auction("20261022")
    assert exc.value.code == "trade_date_conflict"


def test_anomaly_slices_the_analysis_and_joins_keywords(monkeypatch):
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {"item": [{
        "thscode": "001360.SZ", "stock_name": "南矿集团",
        "analysis_content": "x" * 2000, "keyword_list": ["黄金下跌", "净利下滑"],
        "tag_name": "跌停"}]})
    frame = ths.fetch_anomaly("20261026")
    row = frame.iloc[0]
    assert len(row["analysis"]) == 800, "prose is truncated, the tag is not"
    assert row["keywords"] == "黄金下跌|净利下滑"
    assert row["trade_date"] == "20261026"


# ---------------------------------------------------------------- fetch script
def _fetch_module():
    """Load scripts/fetch_ths_special.py by path: it is a CLI, not a package."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "fetch_ths_special.py"
    spec = importlib.util.spec_from_file_location("fetch_ths_special", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calendar():
    from service.adapters.pit import TradingCalendarStore

    days = pd.bdate_range("2025-09-01", "2026-10-30").strftime("%Y%m%d").tolist()
    frame = pd.DataFrame({"cal_date": days, "is_open": [1] * len(days),
                          "pretrade_date": days})
    return TradingCalendarStore.from_frame(frame)


def _isolate_fetch_main(monkeypatch, mod):
    class EmptyRegistry:
        def list_manifest(self, **_kwargs):
            return []

    monkeypatch.setattr(mod, "Registry", lambda _path: EmptyRegistry())
    monkeypatch.setattr(mod, "DatasetStore", lambda *_args: object())
    monkeypatch.setattr(mod, "_calendar", lambda _store: _calendar())
    monkeypatch.setattr(mod, "_today", lambda: "20261026")


def test_each_dataset_floor_matches_the_measured_vendor_limit():
    """These numbers were probed against the vendor, not read from docs.

    If someone "tidies" them into one shared start date, a backfill will spend
    hours collecting code=1002 errors or, worse, write today's data under
    historical dates. Pin them.
    """
    mod = _fetch_module()
    assert mod.DATASETS["ths_limit_up"] == ("pools", "20201201")
    assert mod.DATASETS["ths_limit_down"] == ("pools", "-1y")
    assert mod.DATASETS["ths_limit_break"] == ("pools", "-1y")
    assert mod.DATASETS["ths_lhb"] == ("lhb", "-1y")
    assert mod.DATASETS["ths_ladder"] == ("ladder", "30d")
    assert mod.DATASETS["ths_auction"] == ("auction", "-1y")
    assert mod.TODAY_ONLY == {"ths_limit_stat", "ths_anomaly"}


def test_window_tokens_resolve_to_trading_days():
    mod = _fetch_module()
    cal = _calendar()

    assert mod._window(cal, "today", "20261026") == ["20261026"]
    assert mod._window(cal, "30d", "20261026") == sorted(
        mod._window(cal, "30d", "20261026"), reverse=True), "ladder window is newest-first"

    year = mod._window(cal, "-1y", "20261026")
    assert year[0] >= "20251026" and year[-1] == "20261026"
    assert len(year) > 200

    explicit = mod._window(cal, "20260105", "20260201")
    assert explicit[0] == "20260105"
    assert explicit[-1] == "20260130", "2026-02-01 is a Sunday; the window ends on Friday"


def test_a_date_the_calendar_does_not_cover_is_treated_asymmetrically():
    """Two different questions, two different answers - on purpose.

    ``today`` trusts the weekday fallback in ``is_trading_day`` so that a stale
    calendar cannot make the daily job skip forever. A *range* backfill must not:
    it only returns days the calendar actually contains, because inventing
    trading days across a decade would produce a plan that fetches holidays.
    """
    mod = _fetch_module()
    cal = _calendar()

    assert mod._window(cal, "today", "20150105") == ["20150105"], "weekday fallback"
    assert mod._window(cal, "20200101", "20210101") == [], "range queries are strict"


def test_dry_run_never_touches_the_network(monkeypatch):
    """The first version of this script fetched everything and then declined to
    write it, so a 1,400-day "preview" took 20 minutes and hammered the vendor.
    A dry run must answer from the registry alone."""
    mod = _fetch_module()

    def explode(*_a, **_k):
        raise AssertionError("dry run must not fetch")

    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_Fetcher", explode)
    _isolate_fetch_main(monkeypatch, mod)
    assert mod.main(["--dates", "20261023", "--datasets", "ths_limit_up", "--dry-run"]) == 0
    assert mod.main(["--backfill", "--dry-run"]) == 0


def test_backfill_says_out_loud_that_two_datasets_cannot_go_back(monkeypatch, capsys):
    mod = _fetch_module()
    monkeypatch.setattr(mod.ths, "available", lambda: True)
    monkeypatch.setattr(mod, "_Fetcher", lambda args: None)
    _isolate_fetch_main(monkeypatch, mod)
    mod.main(["--backfill", "--dry-run"])
    out = capsys.readouterr().out
    for name in ("ths_limit_stat", "ths_anomaly"):
        assert f"{name}: today-only upstream" in out
    assert "ths_auction: today-only upstream" not in out


def test_seal_history_never_treats_a_missing_broken_row_as_zero(monkeypatch):
    """A date absent from the broken-board pool means "we do not know", not
    "no board failed". Counting it as zero would hand back a seal rate of 1.0
    and quietly lift the whole distribution."""
    up = pd.DataFrame({"trade_date": ["20260101"] * 4 + ["20260102"] * 3
                       + ["20260103"] * 2})
    # 20260102 has no broken rows at all; 20260101 and 03 do
    brk = pd.DataFrame({"trade_date": ["20260101"] * 1 + ["20260103"] * 2})
    monkeypatch.setattr(tape, "_read", lambda desk, name, start, end, columns=None: {
        "ths_limit_up": up, "ths_limit_break": brk,
        "ths_limit_stat": pd.DataFrame({"trade_date": [], "seal_rate": []}),
    }[name])

    result = tape.limit_history(object(), "20260103")
    assert result["seal_rate"]["sessions"] == 2, "only the two known days count"
    # 4/(4+1)=0.80 and 2/(2+2)=0.50; a fabricated 1.0 for 0102 would raise both
    assert result["seal_rate"]["p90"] == pytest.approx(0.8, abs=0.01)
    assert result["seal_rate"]["today"] == pytest.approx(0.5)


def test_history_reports_the_sample_size_it_actually_has(monkeypatch):
    up = pd.DataFrame({"trade_date": ["20260101", "20260101", "20260102"]})
    monkeypatch.setattr(tape, "_read", lambda desk, name, start, end, columns=None: {
        "ths_limit_up": up, "ths_limit_break": pd.DataFrame({"trade_date": []}),
        "ths_limit_stat": pd.DataFrame({"trade_date": [], "seal_rate": []}),
    }[name])

    result = tape.limit_history(object(), "20260102")
    assert result["limit_up"]["sessions"] == 2, "one session, not one row"
    assert result["limit_up"]["today"] == 1
    assert result["window"] == {"start": "20260101", "end": "20260102"}


def test_limit_history_says_nothing_when_there_is_nothing(monkeypatch):
    monkeypatch.setattr(tape, "_read",
                        lambda desk, name, start, end, columns=None: pd.DataFrame())
    assert tape.limit_history(object(), "20260103")["ok"] is False


def test_an_unfetched_pool_is_null_not_zero():
    """For a 2023 question the limit-down and broken-board pools do not exist.
    Reporting 0 would let a caller conclude "no stocks fell, nobody failed" -
    and deriving a seal rate from it produced 1.0, the strongest possible tape."""
    up = _up([{"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
               "seal_money": 1, "reason": "x", "limit_up_time": "09:30"}] * 3)
    result = tape.summarize_limit(
        pd.DataFrame(), up, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
        unknown=frozenset({"broken", "limit_down"}),
    )
    assert result["counts"]["limit_up"] == 3
    assert result["counts"]["limit_down"] is None
    assert result["counts"]["broken"] is None
    assert result["seal_rate"] is None, "no denominator, no ratio"
    assert result["seal_rate_derived"] is False
    assert result["unknown_pools"] == ["broken", "limit_down"]


def test_a_known_pool_with_no_rows_is_still_zero():
    """The other direction: coverage exists, the pool is simply empty."""
    up = _up([{"ts_code": "A", "name": "a", "continue_cnt": 1, "continue_text": "首板",
               "seal_money": 1, "reason": "x", "limit_up_time": "09:30"}])
    result = tape.summarize_limit(
        pd.DataFrame(), up, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), unknown=frozenset())
    assert result["counts"]["limit_down"] == 0
    assert result["counts"]["broken"] == 0
    assert result["seal_rate"] == 1.0


def test_a_rate_limit_is_retried_not_recorded_as_a_failed_day(monkeypatch):
    """A 429 means "you asked too fast", not "that day had no data". Letting it
    through leaves a hole that every later query reads as a genuine zero."""
    import argparse

    mod = _fetch_module()
    calls = {"n": 0}

    def flaky(day):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ths.ThsError(429, "Global request rate limit exceeded", "/x")
        return pd.DataFrame({"ts_code": ["A"], "trade_date": [day]})

    monkeypatch.setattr(ths, "fetch_limit_up", flaky)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    fetcher = mod._Fetcher(argparse.Namespace(auction_pages=80))
    frame = fetcher.get("ths_limit_up", "20260401")
    assert len(frame) == 1
    assert calls["n"] == 3, "two 429s, then success"


def test_an_upstream_timeout_is_retried_with_the_same_bounded_backoff(monkeypatch):
    import argparse

    mod = _fetch_module()
    calls = {"n": 0}

    def flaky(day, _pages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ths.ThsError(5002, "Upstream timeout", "/x")
        return pd.DataFrame({"ts_code": ["A"], "trade_date": [day]})

    monkeypatch.setattr(ths, "fetch_auction", flaky)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    fetcher = mod._Fetcher(argparse.Namespace(auction_pages=80))
    assert len(fetcher.get("ths_auction", "20261102")) == 1
    assert calls["n"] == 2


def test_a_non_rate_limit_error_is_not_retried(monkeypatch):
    import argparse

    mod = _fetch_module()
    calls = {"n": 0}

    def broken(day):
        calls["n"] += 1
        raise ths.ThsError(1002, "Invalid parameter format", "/x")

    monkeypatch.setattr(ths, "fetch_limit_up", broken)
    fetcher = mod._Fetcher(argparse.Namespace(auction_pages=80))
    with pytest.raises(ths.ThsError):
        fetcher.get("ths_limit_up", "20200102")
    assert calls["n"] == 1, "a boundary error is final, do not hammer it"


def test_a_pool_row_dated_differently_from_the_request_is_refused(monkeypatch):
    """A pool endpoint that ignores `date_ms` must not file today under a past day.

    The three pool endpoints were the only ones that took the request day on
    trust: `_pool` overwrote any date the response carried. A sibling endpoint
    already ignores its date parameter, so this is a live failure mode — and one
    that ends up stamped with a checksum and a snapshot id, which makes it
    indistinguishable from real history afterwards.
    """
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "trade_date": "2026-10-30",
                  "last_price": 10.5}],
        "pagination": {"pages": 1},
    })
    with pytest.raises(ths.ThsError) as exc:
        ths.fetch_limit_up("20260105")
    assert exc.value.code == "trade_date_conflict"


def test_a_pool_row_with_no_date_still_uses_the_requested_day(monkeypatch):
    """The common case: the vendor says nothing, so the request day is the answer."""
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "last_price": 10.5}],
        "pagination": {"pages": 1},
    })
    frame = ths.fetch_limit_up("20260105")
    assert frame["trade_date"].tolist() == ["20260105"]


def test_an_unparseable_pool_date_does_not_become_an_outage(monkeypatch):
    """A vendor format change must not stop every pool from being collected."""
    monkeypatch.setattr(ths, "_get", lambda path, params=None: {
        "item": [{"thscode": "000001.SZ", "trade_date": 1793342700,
                  "last_price": 10.5}],
        "pagination": {"pages": 1},
    })
    frame = ths.fetch_limit_up("20260105")
    assert frame["trade_date"].tolist() == ["20260105"]
