"""The ALERT file is how a failed pipeline reaches the next session.

On 2026-10-29 `daily_job` exited non-zero at 18:39 and nothing read it: the
scheduled task's `LastResult` is invisible from inside a trading session, so the
only record was a log line nobody greps. This pins the contract that replaces
that silence — written on `not_ready`, cleared on the next clean run, and short
enough for the 4 KB pack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_job


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


def test_a_failed_run_leaves_a_readable_alert(root):
    path = daily_job.write_alert(
        "20261029", ("20261026", "20261029"), "not_ready",
        rc_backfill=1, rc_verify=0, failed_steps=["stk_mins", "ths_ladder"],
        root=root,
    )

    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "20261029 daily_job gate=not_ready" in text
    assert "backfill rc=1" in text
    assert "failed steps: stk_mins, ths_ladder" in text
    assert "GET /readiness" in text       # tells the reader what to do next
    assert "verify rc" not in text        # zero is not a failure


def test_a_clean_run_clears_the_previous_alert(root):
    """A stale alert is worse than none: it would block a day that is fine."""
    daily_job.write_alert("20261029", ("20261029", "20261029"), "not_ready",
                          rc_backfill=1, rc_verify=0, failed_steps=[], root=root)
    assert (root / "data" / "ALERT.txt").exists()

    assert daily_job.write_alert(
        "20261030", ("20261030", "20261030"), "ready",
        rc_backfill=0, rc_verify=0, failed_steps=[], root=root) is None
    assert not (root / "data" / "ALERT.txt").exists()


def test_clearing_when_nothing_is_there_is_not_an_error(root):
    assert daily_job.write_alert(
        "20261030", ("20261030", "20261030"), "ready",
        rc_backfill=0, rc_verify=0, failed_steps=[], root=root) is None
