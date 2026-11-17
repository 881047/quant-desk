"""Was there a run, did it finish, and may the numbers be quoted?

`data/ALERT.txt` only exists when the gate is `not_ready`, so before this the
absence of an alert meant two different things: "the run finished clean" and
"the run never started". 2026-10-29 was the second, and a close report went out
over a session nobody had published. These tests pin the two facts that separate
those cases — a marker written only when the run reaches the end, and a lock
that keeps a second run from merging the same partitions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_job

from service.adapters import pipeline_state as ps
from service.kernel.clock import SHANGHAI


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A data directory the API would also resolve to."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("QUANT_DATA_ROOT", str(root))
    return root


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 4, hour, minute, tzinfo=SHANGHAI)


def test_a_finished_run_can_be_told_apart_from_one_that_never_started(data_root):
    ps.write_marker(data_root, "20261104", "close",
                    {"gate": "ready", "failed_steps": []}, now=_at(17, 36))

    now = ps.status(data_root, today="20261104", now=_at(18, 50), trading_day=True)
    assert now["state"] == "done"
    assert now["gate"] == "ready"
    assert now["age_minutes"] == pytest.approx(74.0)
    assert now["note"].startswith("20261104 的日更已结束")

    # Same data directory, no marker: the previous absence of an alert file
    # looked exactly like this, and it is not the same answer.
    empty = ps.status(data_root, today="20261105", now=_at(18, 50), trading_day=True)
    assert empty["state"] == "missing"


def test_a_finished_run_with_a_bad_gate_names_the_steps(data_root):
    ps.write_marker(data_root, "20261104", "close",
                    {"gate": "not_ready", "failed_steps": ["derived_pools", "ths"]},
                    now=_at(17, 36))

    now = ps.status(data_root, today="20261104", now=_at(18, 50), trading_day=True)
    assert now["state"] == "done_not_ready"
    assert now["failed_steps"] == ["derived_pools", "ths"]
    assert "derived_pools" in now["note"] and "别从里面取数" in now["note"]


def test_before_the_due_time_a_missing_marker_is_not_a_failure(data_root):
    """Every 09:26 looks like this. Saying 'missing' there would train the
    reader to ignore the field."""
    now = ps.status(data_root, today="20261104", now=_at(9, 26), trading_day=True)
    assert now["state"] == "pending"
    assert "17:30" in now["note"]


def test_a_non_trading_day_has_nothing_to_wait_for(data_root):
    now = ps.status(data_root, today="20261107", now=_at(18, 50), trading_day=False)
    assert now["state"] == "not_required"


def test_missing_after_the_due_time_says_how_long_the_pipeline_has_been_quiet(data_root):
    ps.write_marker(data_root, "20261103", "close",
                    {"gate": "ready", "failed_steps": []}, now=_at(17, 35))

    now = ps.status(data_root, today="20261104", now=_at(18, 50), trading_day=True)
    assert now["state"] == "missing"
    assert now["last_done"] == {"day": "20261103", "gate": "ready"}
    assert "daily_job.log" in now["note"]


def test_a_running_job_is_reported_as_running_not_as_a_missing_marker(data_root):
    with ps.hold(data_root, "close", now=_at(17, 30)):
        now = ps.status(data_root, today="20261104", now=_at(17, 33), trading_day=True)

    assert now["state"] == "running"
    assert now["age_minutes"] == pytest.approx(3.0)
    assert "不要再起一个" in now["note"]


def test_the_lock_admits_one_run_at_a_time(data_root):
    with ps.hold(data_root, "close", now=_at(17, 30)), \
            pytest.raises(ps.PipelineBusy) as caught, \
            ps.hold(data_root, "close", now=_at(17, 31)):
        pass
    assert caught.value.holder["pid"] == os.getpid()
    assert "pid=" in str(caught.value)


def test_a_dead_holder_does_not_block_the_next_run(data_root):
    """A killed run leaves the file behind, and the scheduler will not run it
    again until tomorrow — so stealing has to be automatic."""
    ps.write_marker(data_root, "20261103", "close", {"gate": "ready"})
    (data_root / ps.LOCK_NAME).write_text(
        json.dumps({"pid": 999_999, "pass": "close",
                    "started_at": _at(17, 30).isoformat()}), encoding="utf-8")

    with ps.hold(data_root, "close", now=_at(17, 40)):
        holder = ps.lock_holder(data_root, now=_at(17, 40))
    assert holder is not None and holder["pid"] == os.getpid()


def test_an_overrun_lock_is_stolen_even_though_the_pid_is_alive(data_root):
    """Task Scheduler kills the process at the time limit; Windows may hand its
    pid to something else. Age, not liveness, is the last word."""
    path = data_root / ps.LOCK_NAME
    path.write_text(json.dumps({"pid": os.getpid(), "pass": "close",
                                "started_at": _at(11, 0).isoformat()}),
                    encoding="utf-8")
    old = (_at(17, 40) - timedelta(hours=7)).timestamp()
    os.utime(path, (old, old))

    assert ps.lock_holder(data_root, now=_at(17, 40)) is None
    with ps.hold(data_root, "close", now=_at(17, 40)):
        assert ps.lock_holder(data_root, now=_at(17, 40))["pid"] == os.getpid()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle contract")
def test_windows_liveness_never_uses_os_kill(monkeypatch):
    """signal 0 is not a harmless liveness probe on Windows."""
    def forbidden_kill(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) can interrupt the console")

    monkeypatch.setattr(ps.os, "kill", forbidden_kill)
    assert ps._alive(os.getpid()) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle contract")
def test_windows_liveness_distinguishes_live_and_exited_processes():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert ps._alive(child.pid) is True
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert ps._alive(child.pid) is False


def test_posix_liveness_retains_the_signal_zero_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(ps.os, "name", "posix")
    monkeypatch.setattr(ps.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert ps._alive(123) is True
    assert calls == [(123, 0)]


def test_releasing_a_lock_someone_else_now_holds_deletes_nothing(data_root):
    with ps.hold(data_root, "close", now=_at(17, 30)):
        # The lock was taken over while we were running.
        (data_root / ps.LOCK_NAME).write_text(
            json.dumps({"pid": os.getpid() + 1, "pass": "close",
                        "started_at": _at(17, 35).isoformat()}), encoding="utf-8")
    assert (data_root / ps.LOCK_NAME).exists()


def test_a_second_daily_job_exits_before_it_touches_a_partition(data_root, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(daily_job.backfill, "main",
                        lambda *_a, **_k: calls.append("backfill") or 0)

    with ps.hold(data_root, "close", now=_at(17, 30)):
        rc = daily_job.main(["--days", "1"])

    assert rc == daily_job.EXIT_BUSY
    assert calls == []
    # A refused run must not claim the day finished.
    assert ps.read_marker(data_root, datetime.now(SHANGHAI).strftime("%Y%m%d")) is None


def test_the_marker_is_written_after_the_alert_so_its_presence_implies_both():
    """Ordering is the contract: a marker that can exist without the report or
    the alert is not evidence that the run reached the end."""
    source = Path(daily_job.__file__).read_text(encoding="utf-8")
    assert source.index("alert = write_alert(") < source.index("write_marker(data_root(), end")
    assert source.index("write_json_atomic(out, payload)") \
        < source.index("write_marker(data_root(), end")


def test_markers_are_pruned_so_the_directory_does_not_grow_forever(data_root):
    for index in range(ps.KEEP_MARKERS + 5):
        day = f"2026{index // 28 + 1:02d}{index % 28 + 1:02d}"
        ps.write_marker(data_root, day, "close", {"gate": "ready"})

    kept = list((data_root / ps.STATE_DIR).glob("*.json"))
    assert len(kept) == ps.KEEP_MARKERS
