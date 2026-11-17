"""P1-b: the data-breadth gap is measurable without adding any new test."""

from __future__ import annotations

import pathlib

from service.research import breadth

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fake_desk(names):
    class Registry:
        def summary(self):
            return [{"dataset": n, "rows": 100 + i, "cover_end": "20261112",
                     "trust_tier": "T0"} for i, n in enumerate(names)]

    class Store:
        registry = Registry()

    class Market:
        store = Store()

    class Desk:
        market = Market()

    return Desk()


def test_declared_alpha_inputs_are_real_read_sites():
    """A dataset listed as an alpha input must actually be read somewhere on
    the research path; otherwise this list drifts into wishful thinking."""
    files = ["service/research/spec.py", "service/research/runner.py",
             "service/research/factors.py", "service/trading/analog.py",
             "service/trading/knowledge.py"]
    text = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in files)
    for name in breadth.ALPHA_INPUT_DATASETS:
        assert name in text, f"{name} 不再被研究路径读取，breadth 清单需同步"
    assert set(breadth.SIGNAL_CARRYING_DATASETS) <= set(breadth.ALPHA_INPUT_DATASETS)


def test_readout_counts_and_read_only_contract():
    names = list(breadth.ALPHA_INPUT_DATASETS) + list(breadth.SHORT_HORIZON_CANDIDATES) + [
        "adj_factor", "fund_daily", "dividend"]
    out = breadth.breadth_read(_fake_desk(names))
    assert out["ok"] is True and out["read_only"] is True
    assert out["dataset_count"] == len(names)
    assert out["in_alpha_estimate_count"] == 9
    assert out["signal_carrying_count"] == 6
    assert out["unused_short_horizon_count"] == len(breadth.SHORT_HORIZON_CANDIDATES)
    assert out["not_in_alpha_count"] == len(names) - 9
    assert out["in_alpha_estimate"][0]["role"] == "signal"
    assert any(row["role"] == "metadata_or_label"
               for row in out["in_alpha_estimate"])
    assert all("why_not_in_alpha" in row for row in out["short_horizon_unused"])
    assert "TrialRegistry" in out["integration_rule"]
    assert "预注册" in out["integration_rule"]
    assert "36 检验" in out["integration_rule"]
    assert out["caveats"]


def test_missing_datasets_are_reported_not_dropped():
    out = breadth.breadth_read(_fake_desk(["daily"]))
    missing = [r for r in out["short_horizon_unused"] if not r.get("available")]
    assert missing
    assert out["unused_short_horizon_count"] == 0


def test_registry_failure_is_an_answer():
    class BrokenDesk:
        class market:
            class store:
                class registry:
                    @staticmethod
                    def summary():
                        raise RuntimeError("manifest gone")

    out = breadth.breadth_read(BrokenDesk())
    assert out["ok"] is False and out["status"] == "registry_unavailable"
    assert out["read_only"] is True


def test_no_signal_or_verdict_fields():
    out = breadth.breadth_read(_fake_desk(breadth.ALPHA_INPUT_DATASETS))
    for forbidden in ("action", "verdict", "signal", "score",
                      "ranking", "target_price", "trials"):
        assert forbidden not in out
