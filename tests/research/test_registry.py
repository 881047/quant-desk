"""The denominator: every look at the same panel has to be counted.

Nine hypotheses each deflating themselves by their own 8-variant grid book 72
searches as 8. These tests pin the bookkeeping: a repeated spec is a new look,
a different spec is a new trial, and the adjusted p-value is computed over the
whole family.
"""

from __future__ import annotations

import pytest

from service.research import runner
from service.research.registry import TrialRegistry, bh_adjust, spec_hash
from service.research.spec import EventStudy, SpecError


def _spec(**kwargs) -> EventStudy:
    base = {"pool": "limit_up", "boards": (1,), "hold": 1}
    return EventStudy(**{**base, **kwargs})


def test_bh_adjustment_matches_a_hand_calculation():
    # sorted p: 0.01, 0.03, 0.04 with n=3 -> 0.03, 0.04, 0.04 after the step-up
    assert bh_adjust([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.04, 0.04])
    assert bh_adjust([]) == []
    assert bh_adjust([0.5]) == pytest.approx([0.5])
    assert all(a <= 1.0 for a in bh_adjust([0.9, 0.95, 0.99]))


def test_a_repeated_spec_is_a_second_look_not_a_new_trial(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    first = registry.record(_spec(), n_variants=8, p_value=0.4, verdict="rejected")
    second = registry.record(_spec(), n_variants=8, p_value=0.3, verdict="rejected")

    assert first["n_looks"] == 1 and first["trials_total"] == 1
    assert second["n_looks"] == 2 and second["trials_total"] == 2
    # BH is a step-up: the smaller p is capped by the larger one, n/k = 2/2.
    # 0.3 * 2 / 1 = 0.6 would exceed 0.4, so the answer is 0.4, not 0.6.
    assert second["p_adjusted"] == pytest.approx(0.4)
    assert registry.looks(_spec()) == 2


def test_different_specs_are_different_trials(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    registry.record(_spec(), n_variants=8, p_value=0.4)
    registry.record(_spec(hold=2), n_variants=8, p_value=0.4)
    assert registry.trials_total() == 2
    assert registry.looks(_spec()) == 1


def test_cost_assumptions_are_not_search_dimensions():
    """Re-running the same idea at a different cost is the same hypothesis."""
    assert spec_hash(_spec()) == spec_hash(_spec(cost_bps=25, cost_mult=3.0))
    assert spec_hash(_spec()) != spec_hash(_spec(rank_by="amount"))


def test_families_do_not_pollute_each_other(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    registry.record(_spec(), n_variants=8, p_value=0.01, family="a")
    registry.record(_spec(), n_variants=8, p_value=0.2, family="b")
    assert registry.trials_total("a") == 1
    assert registry.trials_total("b") == 1
    assert registry.program_status("b")["min_p"] == pytest.approx(0.2)


def test_p_adjusted_uses_the_whole_family(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    for i, p in enumerate((0.01, 0.20, 0.30)):
        registry.record(_spec(hold=i), n_variants=8, p_value=p, verdict="rejected")
    status = registry.program_status(spec=_spec(hold=0))
    assert status["trials_total"] == 3
    assert status["p_adjusted"] == pytest.approx(0.03)  # 0.01 * 3 / 1
    assert status["program_fdr"] == pytest.approx(0.03)
    assert status["survivors_q10"] == 1


def test_runner_only_records_when_asked(store):
    payload = runner.run(store, {"pool": "limit_up", "boards": [1], "hold": 1})
    assert payload["program"]["recorded"] is False
    assert payload["program"]["family_variants"] == 0
    assert payload["program"]["dsr_trials"] == payload["trials"]


def test_not_recording_still_deflates_by_the_family(store, tmp_path):
    """``record=False`` 是不写一行，不是把已经试过的一百次搜索假装没发生。

    读取分母和写入一行原本共用一个开关，于是 ``--no-record`` 成了一个
    让 Sharpe 更好看的手段：跳过族内计数，只按本次网格收缩。
    """
    registry = TrialRegistry(tmp_path / "research.sqlite")
    for i in range(9):
        registry.record(_spec(start=f"2023010{i + 1}"), n_variants=8, p_value=0.5)

    payload = runner.run(store, _spec(), registry=registry, record=False)
    assert payload["program"]["recorded"] is False
    assert payload["program"]["family_variants"] == 72
    assert payload["trials_effective"] == 72 + payload["trials"]
    assert payload["deflated_sharpe"]["trials"] == payload["trials_effective"]
    assert registry.trials_total() == 9, "没有新增行"


def test_runner_deflates_by_every_search_in_the_family(store, tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    for i in range(9):
        registry.record(_spec(start=f"2023010{i + 1}"), n_variants=8, p_value=0.5,
                        verdict="rejected")

    payload = runner.run(store, _spec(), registry=registry)
    program = payload["program"]
    assert program["trials_total"] == 10          # ten runs
    assert program["variants_total"] == 80        # 9 x 8 + this run's 8
    assert payload["trials_effective"] == 80
    assert payload["trials_effective"] > payload["trials"], "program total must win"
    assert payload["deflated_sharpe"]["trials"] == 80
    assert payload["deflated_sharpe"]["trials_local"] == payload["trials"]


def test_failed_or_empty_run_is_still_recorded(store, tmp_path, monkeypatch):
    registry = TrialRegistry(tmp_path / "research.sqlite")

    def no_events(*args, **kwargs):
        raise SpecError("no pool events for this test")

    monkeypatch.setattr(runner, "load_inputs", no_events)
    with pytest.raises(SpecError, match="no pool events"):
        runner.run(store, _spec(), registry=registry)
    snapshot = registry.snapshot()
    assert snapshot["trials_total"] == 1
    row = snapshot["trials"][0]
    assert row["n_trades"] == 0
    assert row["source"] == "run:error"
    assert "no pool events" in row["verdict"]
    assert row["code_fingerprint"]
    assert row["config_fingerprint"]
    assert row["data_inputs"] == []


def test_runner_records_the_reads_and_runtime_that_produced_a_verdict(store, tmp_path, monkeypatch):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    # Keep this test about provenance, not about the separate attribution math.
    # The real runner still performs its daily_basic read before this stub.
    monkeypatch.setattr(runner, "attribute", lambda *_args: {"n": 0, "coverage": 0.0})

    payload = runner.run(store, _spec(), registry=registry, attach_attribution=True)
    provenance = payload["provenance"]
    row = registry.snapshot()["trials"][0]

    assert {"daily", "limit_pool_t0", "stk_limit", "daily_basic"} <= set(
        provenance["data_inputs"])
    assert len(provenance["code_fingerprint"]) == 16
    assert len(provenance["config_fingerprint"]) == 16
    assert row["data_inputs"] == provenance["data_inputs"]
    assert row["data_fingerprint"] == provenance["data_fingerprint"]
    assert row["code_fingerprint"] == provenance["code_fingerprint"]
    assert row["config_fingerprint"] == provenance["config_fingerprint"]


# ------------------------------------------------- looks that are not run()
def test_a_bucket_test_counts_as_a_look(tmp_path):
    """The cheapest way to search is the one most likely to be left uncounted."""
    registry = TrialRegistry(tmp_path / "research.sqlite")
    first = registry.record_bucket(bucket="low|low", horizon=20,
                                   window=("20050101", "20261027"),
                                   p_value=0.136, effect=-0.0151, n_episodes=202)
    again = registry.record_bucket(bucket="low|low", horizon=20,
                                   window=("20050101", "20261027"),
                                   p_value=0.12, effect=-0.014, n_episodes=202)
    other = registry.record_bucket(bucket="high|high", horizon=20,
                                   window=("20050101", "20261027"),
                                   p_value=0.30, effect=0.004, n_episodes=210)

    assert first["n_looks"] == 1 and again["n_looks"] == 2
    assert other["n_looks"] == 1, "换一个桶就是换一个假设"
    assert registry.trials_total() == 3
    assert registry.variants_total() == 3, "每次 bucket 检验就是一次搜索"
    assert registry.by_source() == {"bucket": 3}


def test_an_overlay_is_keyed_by_the_rule_and_the_book_it_sits_on(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    base = _spec()
    trades = registry.record_overlay(base, rule="stop_loss", params={"threshold": 0.05},
                                     p_value=0.0005, effect=0.00137, n_trades=30087,
                                     n_fired=4712)
    same = registry.record_overlay(base, rule="stop_loss", params={"threshold": 0.05},
                                   p_value=0.0005, effect=0.00137, n_trades=30087,
                                   n_fired=4712)
    tighter = registry.record_overlay(base, rule="stop_loss", params={"threshold": 0.03},
                                      p_value=0.001, effect=0.0019, n_trades=30087,
                                      n_fired=8947)
    elsewhere = registry.record_overlay(_spec(hold=2), rule="stop_loss",
                                        params={"threshold": 0.05}, p_value=0.02,
                                        effect=0.0004, n_trades=1000, n_fired=90)

    assert trades["n_looks"] == 1 and same["n_looks"] == 2
    assert tighter["n_looks"] == 1, "3% 和 5% 是两个假设"
    assert elsewhere["n_looks"] == 1, "同一个规则套在另一个账本上是另一个假设"
    assert trades["spec_hash"] != tighter["spec_hash"]
    assert trades["spec_hash"] != elsewhere["spec_hash"]


def test_the_family_report_says_where_its_looks_came_from(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    registry.record(_spec(), n_variants=8, p_value=0.4)
    registry.record_bucket(bucket="low|low", horizon=20, window=("20050101", "20261027"),
                           p_value=0.14, effect=-0.015, n_episodes=202)
    registry.record_overlay(_spec(), rule="stop_loss", params={"threshold": 0.05},
                            p_value=0.0005, effect=0.0014, n_trades=300, n_fired=40)

    status = registry.program_status()
    assert status["trials_total"] == 3
    assert status["variants_total"] == 10, "8 个网格 + 2 次单点搜索"
    assert status["by_source"] == {"run": 1, "bucket": 1, "overlay": 1}
    # 一个 8 变体的网格不该被两次单点搜索挤掉：分母是搜索次数，不是行数。
    assert status["variants_total"] > status["trials_total"]


def test_the_snapshot_survives_the_database(tmp_path):
    """账本是 gitignore 的 sqlite；能被删掉的书，分母就会归零。"""
    registry = TrialRegistry(tmp_path / "research.sqlite")
    registry.record(_spec(), n_variants=8, p_value=0.4)
    registry.record_bucket(bucket="low|low", horizon=20, window=("20050101", "20261027"),
                           p_value=0.14, effect=-0.015, n_episodes=202)

    snap = registry.snapshot()
    assert snap["trials_total"] == 2
    assert snap["variants_total"] == 9
    assert snap["by_source"] == {"run": 1, "bucket": 1}
    assert [t["id"] for t in snap["trials"]] == [1, 2], "按时间顺序读才像一段历史"
    assert snap["trials"][0]["spec"]["pool"] == "limit_up", "spec 要能复原"
    assert snap["trials"][1]["window"] == ["20050101", "20261027"]
    # 只应该增长：任何一条变小都是被删过的证据
    assert snap["variants_total"] >= snap["trials_total"]


# ------------------------------------------------------------- supersession
def _three(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    for i, p in enumerate((0.01, 0.20, 0.30)):
        registry.record(_spec(hold=i), n_variants=8, p_value=p,
                        effect=0.001 * (i + 1), verdict="testing")
    return registry


def test_supersession_labels_without_deleting(tmp_path):
    """被推翻的那次 look 仍然算在分母里。删掉它，DSR 就会好看——那正是错的。"""
    registry = _three(tmp_path)
    before = registry.program_status()
    assert before["variants_total"] == 24 and before["superseded"] == 0

    result = registry.supersede(1, by=2, reason="同一份数据上换了持有期就不成立")
    assert result["changed"] is True
    assert result["superseded"] == 1 and result["live"] == 2

    after = registry.program_status()
    assert after["variants_total"] == before["variants_total"], "分母不许变小"
    assert after["trials_total"] == 3
    assert after["denominator_includes_superseded"] is True
    assert registry.trials_total() == 3, "行还在"


def test_the_same_supersession_twice_is_recorded_once(tmp_path):
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="理由")
    again = registry.supersede(1, by=2, reason="理由")
    assert again["changed"] is False
    assert again["superseded"] == 1
    assert len(registry.supersessions()) == 1


def test_a_different_overturning_trial_replaces_the_label(tmp_path):
    """一条 trial 被记为「被 A 推翻」之后又被 B 推翻，最新的那条说了算——但仍然只增。"""
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="先")
    later = registry.supersede(1, by=3, reason="后")
    assert later["changed"] is True
    assert registry.supersessions()[1]["superseded_by"] == 3
    assert registry.supersessions()[1]["reason"] == "后"


def test_a_supersession_without_a_reason_is_refused(tmp_path):
    registry = _three(tmp_path)
    with pytest.raises(ValueError, match="理由"):
        registry.supersede(1, by=2, reason="   ")


def test_a_trial_cannot_overturn_itself_or_a_stranger(tmp_path):
    registry = _three(tmp_path)
    with pytest.raises(ValueError, match="自己"):
        registry.supersede(1, by=1, reason="理由")
    with pytest.raises(ValueError, match="不在 family"):
        registry.supersede(1, by=99, reason="理由")


def test_withdrawing_a_supersession_brings_the_trial_back(tmp_path):
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="理由")
    back = registry.unsupersede(1, reason="后来发现那条证据不适用")
    assert back["changed"] is True and back["superseded"] == 0
    assert registry.supersession_counts()["live"] == 3

    noop = registry.unsupersede(1, reason="再来一次")
    assert noop["changed"] is False


def test_live_trials_excludes_the_overturned_but_the_denominator_does_not(tmp_path):
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="理由")
    assert len(registry.live_trials()) == 2
    assert registry.trials_total() == 3
    assert registry.variants_total() == 24


def test_the_family_report_says_what_was_overturned_and_how_promising_it_was(tmp_path):
    """被推翻者的平均 effect 是这套系统有没有在自我修正的证据。"""
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="理由")
    status = registry.program_status()
    assert status["superseded"] == 1
    assert status["superseded_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert status["superseded_effect_mean"] is not None


def test_the_snapshot_carries_the_label(tmp_path):
    registry = _three(tmp_path)
    registry.supersede(2, by=1, reason="证据不成立")
    snap = registry.snapshot()
    labelled = {t["id"]: t for t in snap["trials"]}
    assert labelled[2]["superseded_by"] == 1
    assert labelled[2]["superseded_reason"] == "证据不成立"
    assert labelled[1]["superseded_by"] is None
    assert snap["superseded"] == 1 and snap["live"] == 2
    assert snap["denominator_includes_superseded"] is True


def test_failed_and_empty_looks_stay_in_the_denominator(tmp_path):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    registry.record(_spec(), n_variants=8, n_trades=0, p_value=None,
                    verdict="error: synthetic failure")
    status = registry.program_status()
    assert status["trials_total"] == 1
    assert status["p_values_total"] == 0
    assert status["variants_total"] == 8


@pytest.mark.parametrize("kwargs", [
    {"p_value": float("nan")}, {"p_value": -0.1}, {"p_value": 1.1},
    {"effect": float("inf")}, {"n_variants": 0}, {"n_trades": -1},
])
def test_record_look_rejects_non_finite_or_out_of_range_values(tmp_path, kwargs):
    registry = TrialRegistry(tmp_path / "research.sqlite")
    args = {"n_variants": 1, "p_value": 0.1, "effect": 0.0, **kwargs}
    with pytest.raises(ValueError):
        registry.record(_spec(), **args)


def test_supersession_replacement_must_be_live_and_acyclic(tmp_path):
    registry = _three(tmp_path)
    registry.supersede(1, by=2, reason="first")
    with pytest.raises(ValueError, match="live"):
        registry.supersede(2, by=1, reason="cycle")
    with pytest.raises(ValueError, match="live"):
        registry.supersede(3, by=1, reason="dead replacement")
