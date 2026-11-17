"""G1 就绪度工具：可复算、只读、不触发 Promotion。"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd

from scripts import parity_readiness as pr


def _root(tmp_path, *, with_frame_id: bool = False, tickets: int = 0):
    root = tmp_path / "data"
    for day, n in (("20261104", 3), ("20261105", 2), ("20261106", 4)):
        d = root / "rt_snapshots" / day
        d.mkdir(parents=True)
        for i in range(n):
            pd.DataFrame({"code": ["600000"]}).to_parquet(
                d / f"10000{i}_000001.parquet", index=False)
        qdir = root / "parquet" / "rt_quote"
        qdir.mkdir(parents=True, exist_ok=True)
        cols = {"code": ["600000"], "last": [10.0]}
        if with_frame_id:
            cols["frame_id"] = ["100000_000001"]
        pd.DataFrame(cols).to_parquet(qdir / f"{day}.parquet", index=False)
        tdir = root / "parquet" / "pool_tape"
        tdir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"snapshot_ts": ["09:30:00", "10:00:00"],
                      "code": ["600000", "600000"]}).to_parquet(
            tdir / f"{day}.parquet", index=False)

    cold = root / "rt_archive" / "20261104"
    cold.mkdir(parents=True)
    pd.DataFrame({"code": ["600000"]}).to_parquet(
        cold / "093000_000001.parquet", index=False)

    db = root / "paper.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_fills (payload TEXT)")
    con.execute("CREATE TABLE tickets (payload TEXT)")
    con.execute("CREATE TABLE decisions (payload TEXT)")
    for i in range(tickets):
        con.execute("INSERT INTO tickets VALUES (?)",
                    (json.dumps({"ticket_id": f"td-{i}", "status": "armed",
                                 "decision_ts": "2026-11-06T10:00:00+08:00"}),))
    con.commit()
    con.close()
    return root


def test_readiness_counts_frames_cold_storage_and_tape(tmp_path):
    out = pr.readiness(_root(tmp_path))
    assert out["totals"]["frame_days"] == 3
    assert out["totals"]["hot_frames"] == 9
    assert out["totals"]["cold_frames"] == 1
    assert out["totals"]["comparable_days"] == 0
    assert all(d["tape_frames"] == 2 for d in out["days"])
    assert out["intraday_features"] == []


def test_a_day_with_frame_id_counts_as_comparable(tmp_path):
    out = pr.readiness(_root(tmp_path, with_frame_id=True))
    assert out["totals"]["comparable_days"] == 3
    assert all(d["rt_quote_has_frame_id"] for d in out["days"])


def test_no_parity_when_there_are_no_terminal_comparisons(tmp_path):
    out = pr.readiness(_root(tmp_path, with_frame_id=True, tickets=2))
    assert out["status"] == "no_parity"
    assert out["promotion_allowed"] is False
    assert out["ledger"]["terminal_comparisons"] == 2
    checks = {c["name"]: c for c in out["checks"]}
    assert checks["real_fills"]["blocks_parity"] is False
    assert checks["regimes"]["ok"] is False


def test_real_fills_guard_does_not_block_parity(tmp_path):
    out = pr.readiness(_root(tmp_path))
    checks = {c["name"]: c for c in out["checks"]}
    assert checks["real_fills"]["actual"] == 0
    assert checks["real_fills"]["blocks_parity"] is False


def test_main_json_output_is_machine_readable(tmp_path, capsys):
    assert pr.main(["--root", str(_root(tmp_path)), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "no_parity"


def test_missing_data_root_is_an_honest_empty_answer(tmp_path):
    out = pr.readiness(tmp_path / "nowhere")
    assert out["totals"]["frame_days"] == 0
    assert out["ledger"]["paper_fills"] == 0
    assert out["status"] == "no_parity"
    assert out["promotion_allowed"] is False
