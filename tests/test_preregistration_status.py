"""D-23 progress tool: counts calendar partitions, never opens a parquet."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import preregistration_status as ps
from service.trading.preregistration import V1


def _root(tmp_path, days):
    root = tmp_path / "data"
    tape = root / "parquet" / "pool_tape"
    tape.mkdir(parents=True)
    for day in days:
        # A zero-byte file is enough: the tool only reads the filename stem.
        (tape / f"{day}.parquet").write_bytes(b"")
    return root


def test_contaminated_days_are_excluded_forever(tmp_path):
    root = _root(tmp_path, ["20261104", "20261105", "20261106",
                            "20261109", "20261110"])
    out = ps.status(root)
    assert out["eligible_days"] == 2
    assert out["first_day"] == "20261109"
    assert out["last_day"] == "20261110"
    assert out["contaminated_days"] == list(V1.contaminated_days)


def test_empty_root_is_insufficient_not_an_error(tmp_path):
    out = ps.status(_root(tmp_path, []))
    assert out["ok"] is True
    assert out["eligible_days"] == 0
    assert out["status"] == "insufficient_confirmatory_samples"
    assert out["readiness"]["allowed_to_test"] is False


def test_holdout_is_reserved_by_time_not_random(tmp_path):
    # 60 consecutive synthetic day labels, all after the first eligible day.
    days = [f"202611{i:02d}" for i in range(9, 29)] + \
           [f"202612{i:02d}" for i in range(1, 21)] + \
           [f"202701{i:02d}" for i in range(1, 21)]
    assert len(days) == 60
    out = ps.status(_root(tmp_path, days))
    assert out["eligible_days"] == 60
    assert out["holdout_reserved_days"] == 12
    assert out["readiness"]["checks"][0]["ok"] is True
    assert out["readiness"]["checks"][1]["ok"] is True
    # No regime labels exist yet; the tool must not invent them to look green.
    assert out["regime_labels"] == 0
    assert out["readiness"]["allowed_to_test"] is False


def test_json_cli_is_machine_readable(tmp_path, capsys):
    root = _root(tmp_path, ["20261109"])
    assert ps.main(["--root", str(root), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "v1"
    assert out["eligible_days"] == 1
    assert out["status"] == "below_gate"


def test_text_cli_lists_the_gaps(tmp_path, capsys):
    root = _root(tmp_path, ["20261109"])
    assert ps.main(["--root", str(root)]) == 0
    printed = capsys.readouterr().out
    assert "GAP" in printed
    assert "allowed_to_test=False" in printed


def test_tool_never_reads_market_data():
    """Counting filenames is not peeking at the holdout."""
    source = Path(ps.__file__).read_text(encoding="utf-8")
    for word in ("read_parquet", "pd.read_", "to_parquet", "write_text",
                 "open(", "requests"):
        assert word not in source, f"{word} would let the tool peek at results"
