"""Every dataset must have an owner.

2026-10-26 found four in one day: `cb_daily` and `news` were three sessions behind
(both are read at decision time), `limit_pool_t0` was three sessions behind (the
research runner reads it) and `stk_mins` had no reader at all. Each was invisible
until someone opened the registry by hand, and each had the same shape — a
catalog entry, a writer, and no scheduler.

This test is the cheap way to keep the fifth from happening. A dataset is owned
if it is refreshed by the 17:30 job, or is a reference table (its `cover_end` is
the newest entity's own date, not a boundary), or only exists inside a session
(a quote snapshot), or is explicitly opt-in.
"""

from __future__ import annotations

from pathlib import Path

from service.adapters.catalog import DAILY_JOB_STEPS, SCHEDULED_DATASETS, SPECS

ROOT = Path(__file__).resolve().parents[2]

# Which callable implements each declared step. Kept next to the contract so the
# two drift together: declaring a step and forgetting to call it is the exact
# failure this file exists to catch.
CALLS = {
    "backfill_p1": "backfill.main(",
    "backfill_panel": "backfill.main(",
    "ths_panels": "fetch_ths_special.main(",
    "derived_pools": "derive_pools.main(",
    "intraday_features": "derive_intraday.main(",
    "cb_bars": "backfill_cb.main(",
    "text": "fetch_text.main(",
}


def _unowned() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in SPECS.items():
        if name in SCHEDULED_DATASETS:
            continue
        if "reference" in spec.tags:
            continue
        if "snapshot" in spec.tags:
            continue
        if spec.on_demand:
            continue
        out[name] = spec.description
    return out


def test_every_dataset_has_an_owner():
    unowned = _unowned()
    assert not unowned, (
        "catalog 里有、但没人负责更新的数据集："
        + "，".join(f"{n}（{d}）" for n, d in sorted(unowned.items()))
        + "。要么在 DAILY_JOB_STEPS 里声明并接进 scripts/daily_job.py，"
        + "要么在 catalog 里标成 on_demand / reference / snapshot。"
    )


def test_scheduled_names_are_real_datasets():
    unknown = sorted(set(SCHEDULED_DATASETS) - set(SPECS))
    assert not unknown, f"DAILY_JOB_STEPS 里有不存在的数据集：{unknown}"


def test_steps_and_call_sites_agree():
    """Declaring a step is not scheduling it."""
    assert set(CALLS) == set(DAILY_JOB_STEPS), (
        "DAILY_JOB_STEPS 与 CALLS 不一致；新增步骤时两边都要改，"
        "否则契约会变成一句自我安慰的话。"
    )
    src = (ROOT / "scripts" / "daily_job.py").read_text(encoding="utf-8")
    missing = [f"{step} → {marker}" for step, marker in CALLS.items() if marker not in src]
    assert not missing, (
        "这些步骤声明了但 scripts/daily_job.py 没有调用：" + "；".join(missing)
    )


def test_snapshot_datasets_cannot_be_scheduled():
    """A quote panel is only writable while the session is open.

    Scheduling one for 17:30 would produce a partition dated today holding
    yesterday's prices, which is worse than an honest gap.
    """
    from service.adapters.catalog import DAILY_JOB_STEPS as steps

    scheduled = set().union(*steps.values())
    wrong = sorted(n for n, s in SPECS.items()
                   if "snapshot" in s.tags and n in scheduled)
    assert not wrong, f"快照类数据集不能排进日更：{wrong}"


def test_announcement_datasets_keep_every_announced_version():
    wrong = sorted(
        name for name, spec in SPECS.items()
        if "ann_date" in spec.required and "ann_date" not in spec.pk
    )
    assert not wrong, f"自然键会吞掉同一报告期的公告重述：{wrong}"


def test_the_close_sentiment_is_built_after_the_derived_pool():
    """The panel reads ``limit_pool_t0``; building it first locks it to vendor.

    ``close_panel`` prefers the derived counts and falls back to the vendor
    pools, so running it before ``derive_pools`` does not fail — it produces a
    labelled but permanently second-best panel. On 2026-11-13 that shipped
    0.3621 (vendor) beside the 0.387 that ``facts`` and ``limit_structure`` read
    from the derived pool for the same session. The order is the whole fix.
    """
    src = (ROOT / "scripts" / "daily_job.py").read_text(encoding="utf-8")
    derive_at = src.index("derive_pools.main(")
    panel_at = src.index("close_panel(store, end)")
    assert panel_at > derive_at, "收盘情绪面板必须在派生池重建之后计算"
