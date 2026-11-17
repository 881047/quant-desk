"""Regression tests for incremental data jobs and quality-gate exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import backfill_mins, verify_data
from scripts import derive_pools as derive_job
from service.adapters.derive import derive_pools


def _bind_store(monkeypatch, module, store):
    monkeypatch.setattr(module, "Registry", lambda _path: store.registry)
    monkeypatch.setattr(module, "DatasetStore", lambda _root, _registry: store)
    monkeypatch.setattr(module, "load_dotenv", lambda *_args, **_kwargs: None)


def _limit_history():
    days = ["20240102", "20240103", "20240104"]
    closes = [11.0, 12.1, 13.31]
    pre_closes = [10.0, 11.0, 12.1]
    daily = pd.DataFrame({
        "ts_code": ["600001.SH"] * 3,
        "trade_date": days,
        "open": closes,
        "high": closes,
        "low": pre_closes,
        "close": closes,
        "pre_close": pre_closes,
        "vol": [1000, 1100, 1200],
        "amount": [1_000_000, 1_100_000, 1_200_000],
    })
    limits = pd.DataFrame({
        "ts_code": ["600001.SH"] * 3,
        "trade_date": days,
        "up_limit": closes,
        "down_limit": [9.0, 9.9, 10.89],
    })
    names = pd.DataFrame({
        "ts_code": ["600001.SH"],
        "name": ["合成股票"],
        "list_date": ["20200101"],
    })
    return daily, limits, names


def _seed_pool_inputs(store):
    daily, limits, names = _limit_history()
    store.write("daily", "2024", daily, as_of="20240104")
    store.write("stk_limit", "2024", limits, as_of="20240104")
    store.write("stock_basic", "all", names, as_of="20240104")
    return daily, limits, names


def test_single_day_derive_preserves_other_dates_in_year(monkeypatch, store):
    daily, limits, names = _seed_pool_inputs(store)
    existing = derive_pools(daily, limits, names)
    store.write("limit_pool_t0", "2024", existing, as_of="20240104")
    _bind_store(monkeypatch, derive_job, store)

    rc = derive_job.main(["--start", "20240104", "--end", "20240104"])

    assert rc == 0
    stored = store.read("limit_pool_t0")
    assert set(stored["trade_date"]) == set(daily["trade_date"])
    assert len(stored) == len(existing)


def test_single_day_derive_uses_warmup_without_writing_it(monkeypatch, store):
    _seed_pool_inputs(store)
    _bind_store(monkeypatch, derive_job, store)

    rc = derive_job.main([
        "--start", "20240104", "--end", "20240104", "--warmup-days", "10",
    ])

    assert rc == 0
    stored = store.read("limit_pool_t0")
    assert stored["trade_date"].tolist() == ["20240104"]
    assert stored["boards"].tolist() == [3]


def _minute_frame(symbol):
    return pd.DataFrame([{
        "ts_code": symbol,
        "trade_date": "20240102",
        "trade_time": f"2024-01-02 09:35:00 {symbol}",
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "close": 10.1,
        "vol": 1000,
        "amount": 10_000,
    }])


def test_minute_backfill_writes_two_symbols_together(monkeypatch, store):
    _bind_store(monkeypatch, backfill_mins, store)
    monkeypatch.setattr(backfill_mins, "fetch_eastmoney",
                        lambda symbol, _period: _minute_frame(symbol))
    writes = []
    original_write = store.write

    def record_write(dataset, partition, frame, **kwargs):
        writes.append((partition, len(frame), kwargs["mode"]))
        return original_write(dataset, partition, frame, **kwargs)

    monkeypatch.setattr(store, "write", record_write)

    rc = backfill_mins.main([
        "--symbols", "600001.SH,000001.SZ", "--days", "100000",
    ])

    assert rc == 0
    assert writes == [("20240102", 2, "merge")]
    stored = store.read("stk_mins")
    assert set(stored["ts_code"]) == {"600001.SH", "000001.SZ"}


def _missing_file_partition(store):
    daily, _limits, _names = _limit_history()
    rec = store.write("daily", "2024", daily, as_of="20240104")
    Path(rec.path).unlink()


def test_verify_missing_file_is_a_nonzero_quality_failure(monkeypatch, store, tmp_path):
    _missing_file_partition(store)
    _bind_store(monkeypatch, verify_data, store)
    out = tmp_path / "quality.json"

    rc = verify_data.main(["--out", str(out)])

    assert rc == 1
    assert json.loads(out.read_text(encoding="utf-8"))["counts"]["quarantine"] == 1


def test_verify_quarantine_can_be_downgraded_to_warning(monkeypatch, store, tmp_path):
    _missing_file_partition(store)
    _bind_store(monkeypatch, verify_data, store)

    rc = verify_data.main([
        "--out", str(tmp_path / "quality.json"), "--allow-quarantine",
    ])

    assert rc == 0


def test_verify_report_write_failure_has_distinct_exit_code(monkeypatch, store, tmp_path):
    _bind_store(monkeypatch, verify_data, store)

    def fail_write(_report, _path):
        raise OSError("disk full")

    monkeypatch.setattr(verify_data, "write_markdown", fail_write)

    rc = verify_data.main(["--out", str(tmp_path / "quality.json")])

    assert rc == 2


def test_a_failed_derived_pool_turns_the_daily_gate_red():
    """派生池是家数/连板/晋级率的唯一来源，它失败必须让当天变成 not_ready。

    不这么做的话，那一天的数字会安静地退回厂商口径（同花顺日均漏 1.6 只），
    而日更报告仍然写着 ready——正是这个仓库反复在修的那类"看起来正常"。
    """
    from scripts.daily_job import _gate

    gate, failed, supplementary = _gate(
        0, 0, {"ths": 0, "derived_pools": 2, "snapshot": 0})

    assert gate == "not_ready"
    assert failed == ["derived_pools"]
    assert supplementary == []


def test_a_clean_day_passes_and_names_no_failed_step():
    from scripts.daily_job import _gate

    gate, failed, supplementary = _gate(
        0, 0, {"ths": 0, "derived_pools": 0, "snapshot": 0})

    assert gate == "ready"
    assert failed == []
    assert supplementary == []


def test_backfill_or_verify_failure_also_blocks_the_day():
    from scripts.daily_job import _gate

    assert _gate(1, 0, {})[0] == "not_ready"
    assert _gate(0, 3, {})[0] == "not_ready"
