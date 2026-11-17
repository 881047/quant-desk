"""涨停结构：能推导，但推导出来的不是上游那一条；前向必须 T+1 可执行。

两个错误由这里钉住：

* 第一版的前向是「次日开盘买、次日收盘卖」——同日买卖在 A 股不允许，
  那个 +0.29% 没有人能拿到。现在必须是两个交易日。
* 上游的炸板池只有一年，所以封板率曾经没有分布。推导把它从 243 天变成 4,000 天，
  但推导序列与上游**水平不可互换**，只能比分位。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import tape_state


def _panel():
    """两天就能分辨「同日回转」和「隔日」：A 次日涨、第三日崩。"""
    days = ["20240102", "20240103", "20240104"]
    daily = pd.DataFrame([
        # A：d0 封在 11；d1 开 10 收 11（同日 +10%）；d2 收 8（隔日 -20%）
        {"ts_code": "A", "trade_date": days[0], "open": 10.0, "high": 11.0, "close": 11.0},
        {"ts_code": "A", "trade_date": days[1], "open": 10.0, "high": 11.5, "close": 11.0},
        {"ts_code": "A", "trade_date": days[2], "open": 9.0, "high": 9.0, "close": 8.0},
        # B：d0 摸到 11 但收 10.5 —— 炸板
        {"ts_code": "B", "trade_date": days[0], "open": 10.0, "high": 11.0, "close": 10.5},
        {"ts_code": "B", "trade_date": days[1], "open": 10.5, "high": 10.6, "close": 10.5},
        {"ts_code": "B", "trade_date": days[2], "open": 10.4, "high": 10.5, "close": 10.4},
    ])
    limits = pd.DataFrame([
        {"ts_code": "A", "trade_date": days[0], "up_limit": 11.0},
        {"ts_code": "A", "trade_date": days[1], "up_limit": 12.1},
        {"ts_code": "A", "trade_date": days[2], "up_limit": 12.1},
        {"ts_code": "B", "trade_date": days[0], "up_limit": 11.0},
        {"ts_code": "B", "trade_date": days[1], "up_limit": 11.55},
        {"ts_code": "B", "trade_date": days[2], "up_limit": 11.55},
    ])
    return daily, limits


def test_sealed_and_broken_are_counted_from_the_limit_price():
    tape = tape_state.build_tape_state(*_panel())
    d0 = tape.loc["20240102"]
    assert d0["up_count"] == 1, "只有 A 收盘封住"
    assert d0["brk_count"] == 1, "B 摸到涨停价但没封住"
    assert d0["seal_rate"] == pytest.approx(0.5)


def test_a_missing_limit_price_is_excluded_not_guessed():
    daily, limits = _panel()
    limits = limits[limits.ts_code != "B"]
    tape = tape_state.build_tape_state(daily, limits)
    # B 没有涨停价，不能按 10% 猜一个：那会把 20% 板和新股一起算错。
    assert tape.loc["20240102", "up_count"] == 1
    assert tape.loc["20240102", "brk_count"] == 0


def test_the_forward_is_two_sessions_because_one_is_not_tradable():
    """同期买同期卖在 A 股不允许。A 在 d1 同日 +10%、到 d2 是 −20%：
    只有取到 d2，这个数才是真的能拿到的那个。"""
    tape = tape_state.build_tape_state(*_panel())
    fwd = tape.loc["20240102", "fwd_seal2d"]
    assert fwd == pytest.approx(8.0 / 10.0 - 1.0), "次日开盘 10 → 第三日收盘 8"
    assert fwd < 0, "若退回同日口径，这里会是 +10%"


def test_episodes_count_runs_not_days():
    index = pd.Index([f"2024{d:04d}" for d in range(1, 201)])
    tape = pd.DataFrame({
        "bucket": ["low|low" if i % 2 == 0 else "mid|mid" for i in range(200)],
        "seal_rate": [0.6] * 200, "seal_rate_pct": [0.4] * 200,
        "up_count": [40.0] * 200, "up_count_pct": [0.4] * 200,
        "fwd_seal2d": [0.005] * 200, "n_sealed_fwd": [20] * 200,
    }, index=index)
    means = tape_state.episode_means(tape, "low|low")
    assert len(means) == 100, "隔天交替 = 100 段，不是 100 天里的一半再算一半"


def test_a_thin_bucket_is_not_treated_as_a_distribution():
    index = pd.Index(["20240102", "20240103"])
    tape = pd.DataFrame({
        "bucket": ["low|low", "low|low"],
        "seal_rate": [0.5, 0.6], "seal_rate_pct": [0.4, 0.4],
        "up_count": [40.0, 41.0], "up_count_pct": [0.4, 0.4],
        "fwd_seal2d": [0.01, -0.01], "n_sealed_fwd": [20, 20],
    }, index=index)
    out = tape_state.block(tape, "20240102")
    assert out["ok"] is True
    assert out["usable"] is False
    assert "段数" in out["note"]


def test_block_refuses_during_warmup_instead_of_guessing():
    tape = tape_state.build_tape_state(*_panel())
    out = tape_state.block(tape, "20240102")
    assert out["ok"] is False
    assert "预热" in out["error"]


def _tape_frame(bucket: str = "low|low", n: int = 200) -> pd.DataFrame:
    index = pd.Index([f"2024{d:04d}" for d in range(1, n + 1)])
    return pd.DataFrame({
        "bucket": [bucket if i % 2 == 0 else "mid|mid" for i in range(n)],
        "seal_rate": [0.6] * n, "seal_rate_pct": [0.4] * n,
        "up_count": [40.0] * n, "up_count_pct": [0.4] * n,
        "fwd_seal2d": [0.005] * n, "n_sealed_fwd": [20] * n,
    }, index=index)


def test_the_provenance_points_at_a_measurement_instead_of_quoting_one():
    """P36 把「相关 0.990、中位比值 1.09」写死在 provenance 里，实测是 0.984 / 1.145。

    一个没人复查的常数比没有常数更糟：它看起来像已经被核过。所以这里钉的是
    「provenance 不许带常数，只许指向一份带日期的测量」——而不是某个具体的数字。
    """
    out = tape_state.block(_tape_frame(), "20240200")
    assert out["usable"] is True
    assert "推导" in out["provenance"]
    for stale in ("0.990", "1.09", "0.98"):
        assert stale not in out["provenance"], f"{stale} 是写死的常数"
    assert out["calibration"]["ok"] is False
    assert "未校准" in out["calibration"]["note"]
    assert "T+1" in out["forward"]


def test_the_cache_is_invalidated_by_a_schema_change(tmp_path, monkeypatch):
    daily, limits = _panel()
    store = object()
    first = tape_state.cached_tape(store, cache_dir=tmp_path, daily=daily, limits=limits)
    assert len(first) == 3

    meta = tmp_path / "tape_20100101.meta.json"
    assert '"schema": "tape_v1"' in meta.read_text(encoding="utf-8")

    monkeypatch.setattr(tape_state, "SCHEMA", "tape_v2")
    again = tape_state.cached_tape(store, cache_dir=tmp_path, daily=daily, limits=limits)
    assert len(again) == 3
    assert '"schema": "tape_v2"' in meta.read_text(encoding="utf-8")


def test_a_known_last_session_skips_the_full_read(tmp_path):
    """缓存命中时不该再去读全量日线——那正是缓存要避免的事。"""

    class Exploding:
        def read(self, *args, **kwargs):
            raise AssertionError("命中缓存还去读了数据")

    daily, limits = _panel()
    tape_state.cached_tape(object(), cache_dir=tmp_path, daily=daily, limits=limits)
    hit = tape_state.cached_tape(Exploding(), cache_dir=tmp_path, last="20240104")
    assert len(hit) == 3


# ------------------------------------------------- 推导的序列能不能代表上游那一条
def _pair(n: int = 400, scale: float = 1.0, seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """两条一起动、水平不同的序列。``scale`` 只改水平，不改形状。"""
    import numpy as np

    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2020-01-01", periods=n).strftime("%Y%m%d")
    base = 40 + 12 * np.sin(np.arange(n) / 17) + rng.normal(0, 4, n)
    base = np.clip(base, 5, None)
    derived = pd.DataFrame({"up_count": base,
                            "seal_rate": np.clip(0.55 + base / 400, 0, 1)}, index=days)
    vendor = pd.DataFrame({"up_count": base * scale,
                           "seal_rate": np.clip((0.55 + base / 400) * scale, 0, 1)},
                          index=days)
    return derived, vendor


def test_the_same_series_in_the_same_window_agrees_completely():
    derived, vendor = _pair()
    out = tape_state.calibrate(derived, vendor, window=120)
    assert out["ok"] is True
    assert out["seal"]["agree"] == 1.0 and out["seal"]["far"] == 0.0
    assert out["seal"]["rank_corr"] == pytest.approx(1.0)
    assert out["seal"]["n"] > 200


def test_a_pure_level_shift_does_not_touch_the_buckets():
    """上游多算 15% 的涨停家数，不代表它的分位要平移——分位是秩，不是水平。

    实测中位比 1.145、秩相关 0.972，正是这种情形：水平差 15%，分桶仍然 87% 一致。
    """
    derived, vendor = _pair(scale=1.15)
    out = tape_state.calibrate(derived, vendor, window=120)
    assert out["up"]["agree"] == 1.0, "纯比例变换不该改变任何一天的档位"
    assert out["up"]["median_ratio"] == pytest.approx(1 / 1.15, abs=1e-3)
    assert out["up"]["level_shift"] < 0, "推导比上游低，偏移必须是负的"
    assert out["up"]["far"] == 0.0


def test_a_reversed_series_is_reported_as_disagreeing_not_as_noise():
    """如果推导序列其实是反的，同桶率必须掉到 0 而远桶率升到 1——不能报成「噪声」。"""
    derived, vendor = _pair()
    flipped = vendor.copy()
    for col in ("up_count", "seal_rate"):
        flipped[col] = flipped[col].max() + flipped[col].min() - flipped[col]
    out = tape_state.calibrate(derived, flipped, window=120)
    seal = out["seal"]
    # 反序只能把 low 映到 high、mid 映到 mid，所以远桶率的上限就是尾档占比。
    # 正确的断言不是「far 接近 1」，而是「每一个尾档日都落到了对面」。
    assert seal["tail_agree"] == 0.0
    assert seal["far"] == pytest.approx(seal["tail_n"] / seal["n"], abs=0.02)


def test_the_window_is_a_parameter_because_it_changes_the_answer():
    """第一次比这个的时候，推导用 252 日窗、上游只有 120–243 天，同桶率 60.5%；
    压到同一个 120 日窗之后 85.5%。差的那 25 个百分点是窗口，不是序列。"""
    derived, vendor = _pair()
    wide = tape_state.calibrate(derived, vendor, window=252)
    narrow = tape_state.calibrate(derived, vendor, window=120)
    assert wide["window"] == 252 and narrow["window"] == 120
    assert narrow["seal"]["n"] > wide["seal"]["n"]


def test_calibration_reports_full_or_actual_minimum_window():
    derived, vendor = _pair()
    full = tape_state.calibrate(derived, vendor, window=120)
    assert full["full_window"] is True
    assert full["effective_window"] == full["min_periods"] == 120
    shorter = tape_state.calibrate(derived, vendor, window=120, min_periods=60)
    assert shorter["full_window"] is False
    assert shorter["effective_window"] == shorter["min_periods"] == 60
    assert shorter["window"] == 120


def test_the_middle_is_reported_separately_from_the_tails():
    """尾档和中档差 30 个百分点，混成一个数会把「中档也能用」这句话说成真的。"""
    derived, vendor = _pair(scale=1.02, seed=11)
    vendor = vendor.copy()
    vendor["seal_rate"] = vendor["seal_rate"] + 0.03  # 加一点噪声，让档位开始翻
    out = tape_state.calibrate(derived, vendor, window=120)
    seal = out["seal"]
    assert seal["tail_n"] + seal["mid_n"] == seal["n"]
    assert seal["tail_agree"] is not None and seal["mid_agree"] is not None


def test_calibrating_nothing_says_so_instead_of_returning_zeros():
    empty = pd.DataFrame()
    assert tape_state.calibrate(empty, empty)["ok"] is False
    derived, _ = _pair()
    assert tape_state.calibrate(derived, pd.DataFrame())["ok"] is False


def test_block_carries_the_calibration_it_was_given():
    tape = _tape_frame()
    calib = {"ok": True, "window": 120, "seal": {"n": 124, "agree": 0.855}}
    assert tape_state.block(tape, "20240102", calibration=calib)["calibration"] is calib


def test_a_missing_or_broken_calibration_file_is_not_an_error(tmp_path):
    assert tape_state.load_calibration(tmp_path / "nope.json")["ok"] is False
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert tape_state.load_calibration(broken)["ok"] is False
    good = tmp_path / "good.json"
    good.write_text('{"ok": true, "window": 120}', encoding="utf-8")
    assert tape_state.load_calibration(good)["ok"] is True


# --------------------------------------------- 全样本均值是哪个年代挣的
def test_eras_partition_every_session_exactly_once():
    """分段必须覆盖全部日子且不重叠——否则「贡献占比」的分母是错的。"""
    days = [f"{y}0615" for y in range(2010, 2027)]
    labels = [tape_state.era_of(d) for d in days]
    assert all(lab != "?" for lab in labels), "有日子落在分段之外"
    assert len([e for e in tape_state.ERAS if e[1] is None]) == 1, "最后一段必须开口"


def test_the_last_era_has_no_upper_bound_so_the_table_never_needs_editing():
    assert tape_state.era_of("20991231") == tape_state.era_of("20260101")


def test_era_means_lists_the_most_recent_era_first():
    """最先读到的那个数应当是当下适用的那一个。"""
    index = pd.Index([f"{y}0615" for y in (2012, 2017, 2021, 2025)])
    tape = pd.DataFrame({"bucket": ["low|low"] * 4,
                         "fwd_seal2d": [0.001, 0.03, 0.002, -0.004]}, index=index)
    eras = tape_state.era_means(tape, "low|low")
    assert eras[0]["era"].startswith("2024")
    assert eras[-1]["era"].startswith("2010")
    assert eras[0]["mean"] == pytest.approx(-0.004)


def test_the_dominant_era_is_where_the_full_sample_mean_comes_from():
    """全历史 +0.9% 里四分之三来自一段已经结束的行情——这必须是可算的，不是叙述。"""
    by_era = [{"era": "2024-今", "n": 102, "mean": -0.0028, "win": 0.5},
              {"era": "2020-23", "n": 198, "mean": 0.0026, "win": 0.6},
              {"era": "2015-19", "n": 201, "mean": 0.0247, "win": 0.7},
              {"era": "2010-14", "n": 183, "mean": 0.0074, "win": 0.6}]
    dom = tape_state.dominant_era(by_era)
    assert dom["era"] == "2015-19"
    assert dom["share"] == pytest.approx(0.758, abs=0.005)
    # total_mean 是 round 到 4 位的，所以容差按 1e-4，不是 1e-6。
    average = sum(e["n"] * e["mean"] for e in by_era) / sum(e["n"] for e in by_era)
    assert average == pytest.approx(dom["total_mean"], abs=1e-4)


def test_a_mean_near_zero_reports_no_dominant_era_instead_of_a_wild_ratio():
    """均值为零时占比的分母趋零，会得到一个几百倍的「贡献」——不如没有。"""
    by_era = [{"era": "2024-今", "n": 100, "mean": 0.01, "win": 0.5},
              {"era": "2020-23", "n": 100, "mean": -0.01, "win": 0.5}]
    assert tape_state.dominant_era(by_era) is None
    assert tape_state.dominant_era([]) is None


def test_block_reports_a_recent_window_beside_the_full_sample():
    """4,081 天给的是精度，不是时效。近端另算一份，两边都写 n 与段数。"""
    # 要让「近端 756 天」真的比全样本短，序列本身得长过 756 天。
    index = pd.bdate_range("2010-01-01", "2026-12-31").strftime("%Y%m%d")
    n = len(index)
    values = ([0.03] * (n - 900)) + ([-0.004] * 900)
    tape = pd.DataFrame({
        "bucket": ["low|low"] * n, "seal_rate": [0.6] * n,
        "seal_rate_pct": [0.2] * n, "up_count": [40.0] * n,
        "up_count_pct": [0.2] * n, "fwd_seal2d": values,
        "n_sealed_fwd": [20] * n,
    }, index=index)

    out = tape_state.block(tape, str(index[-1]))
    assert out["ok"] is True
    assert out["fwd_seal2d"]["mean"] > 0, "全样本把两段混起来"
    assert out["recent"]["fwd"]["mean"] < 0, "近端只有后一段"
    assert out["recent"]["since"] > str(index[n - 900]), "近端应只覆盖后 900 天的一部分"
    assert out["recent"]["fwd"]["n"] < out["n"]
    assert out["recent"]["episodes"] >= 1
    assert out["by_era"] and out["by_era"][0]["era"].startswith("2024")
    assert out["era_dominant"]["era"] != out["by_era"][0]["era"], "挣钱的不是最近那一段"


def test_historical_tape_block_uses_only_data_available_then():
    tape = _tape_frame()
    day = str(tape.index[20])
    full = tape_state.block(tape, day)
    truncated = tape_state.block(tape.loc[:day], day)
    assert full["fwd_seal2d"] == truncated["fwd_seal2d"]
    assert full["baseline"] == truncated["baseline"]
    assert full["by_era"] == truncated["by_era"]
    assert full["n"] == 10  # 11 个同桶日里，最后一天的两日前向尚未到期
    assert "sell gate" in full["execution_caveat"]


def test_any_warmup_dimension_blocks_tape():
    tape = _tape_frame()
    day = str(tape.index[0])
    tape.loc[day, "bucket"] = "low|?"
    assert tape_state.block(tape, day)["ok"] is False


def test_episode_means_can_be_limited_to_a_window():
    """一段横跨 2015 和 2024 的连续状态，不该被算成一次观测。"""
    index = pd.Index([f"{y}0102" for y in range(2010, 2027)])
    tape = pd.DataFrame({"bucket": ["low|low"] * len(index),
                         "fwd_seal2d": [0.01] * len(index)}, index=index)
    assert len(tape_state.episode_means(tape, "low|low")) == 1
    assert len(tape_state.episode_means(tape, "low|low", since="20240101")) == 1
    none = tape_state.episode_means(tape, "low|low", since="20990101")
    assert len(none) == 0


# ------------------------------------------- 段是观测单位，不是日
def _run_tape(runs: list[tuple[str, int, float]]) -> pd.DataFrame:
    """按 (起始日, 天数, 段内日收益) 拼序列，段之间插一天另一档。

    段是按**状态变化**切分的，不是按日期空洞——所以日期不连续并不会开一个新段，
    要造多段必须换档。这也说明 `episode_table` 不会因为节假日而把一段拆开。
    """
    days, values, buckets = [], [], []
    for i, (start, length, value) in enumerate(runs):
        begin = pd.Timestamp(start)
        for j in range(length):
            days.append((begin + pd.Timedelta(days=j)).strftime("%Y%m%d"))
            values.append(value)
            buckets.append("low|low")
        if i < len(runs) - 1:
            days.append((begin + pd.Timedelta(days=length)).strftime("%Y%m%d"))
            values.append(0.0)
            buckets.append("mid|mid")
    return pd.DataFrame({"bucket": buckets, "fwd_seal2d": values},
                        index=pd.Index(days))


def test_episode_table_counts_one_row_per_run():
    tape = _run_tape([("2024-01-01", 3, 0.01), ("2024-02-01", 2, -0.02),
                      ("2024-03-01", 1, 0.03)])
    table = tape_state.episode_table(tape, "low|low")
    assert len(table) == 3
    assert list(table["n"]) == [3, 2, 1]
    assert list(table["start"]) == ["20240101", "20240201", "20240301"]
    assert table["mean"].tolist() == pytest.approx([0.01, -0.02, 0.03])


def test_the_episode_mean_differs_from_the_daily_mean_when_runs_are_uneven():
    """一长段 +0.01 和一短段 −0.02：日均被长段压住，段均不会。"""
    tape = _run_tape([("2024-01-01", 8, 0.01), ("2024-02-01", 2, -0.02)])
    daily = tape.loc[tape.bucket == "low|low", "fwd_seal2d"].mean()
    episodes = tape_state.episode_means(tape, "low|low")
    assert daily == pytest.approx((8 * 0.01 + 2 * -0.02) / 10)
    assert episodes.mean() == pytest.approx((0.01 - 0.02) / 2)
    assert episodes.mean() < daily


def test_a_ci_needs_ten_episodes_and_says_nothing_below_that():
    """少于 10 段时给一个 [0,0] 的区间比不给更糟——那看起来像一个确定的零。"""
    assert tape_state.episode_ci(np.full(9, 0.01)) is None
    got = tape_state.episode_ci(np.full(10, 0.01))
    assert got is not None and got["n"] == 10
    assert got["ci_low"] <= got["mean"] <= got["ci_high"]
    assert tape_state.episode_ci(np.array([])) is None


def test_clustered_returns_get_a_wider_interval_than_iid_would():
    """同一段内的日收益是相关的。按日重采样会把区间做窄，按段不会。

    这不是精度细节：`low|low` 近三年的日均 −0.28% 段均 −0.09%，
    两个区间都横跨零——只报日均那个点，它会被读成一个方向。
    """
    rng = np.random.default_rng(5)
    # 12 段，每段 20 天，段内几乎不变、段间差别很大
    level = rng.normal(0, 0.02, 12)
    daily = np.repeat(level, 20) + rng.normal(0, 0.001, 240)
    episodes = level

    daily_ci = tape_state.episode_ci(daily)
    episode_ci = tape_state.episode_ci(episodes)
    assert daily_ci is not None and episode_ci is not None
    daily_width = daily_ci["ci_high"] - daily_ci["ci_low"]
    episode_width = episode_ci["ci_high"] - episode_ci["ci_low"]
    assert episode_width > daily_width * 2, (episode_width, daily_width)


def _alternating_tape(days: int = 4300, seed: int = 9) -> pd.DataFrame:
    """每天交替两档，于是每一两天就是一个新段——段数够做区间。"""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2010-01-01", periods=days).strftime("%Y%m%d")
    return pd.DataFrame({
        "bucket": [("low|low" if i % 3 == 0 else "mid|mid") for i in range(days)],
        "seal_rate": [0.6] * days, "seal_rate_pct": [0.2] * days,
        "up_count": [40.0] * days, "up_count_pct": [0.2] * days,
        "fwd_seal2d": rng.normal(0.001, 0.02, days), "n_sealed_fwd": [20] * days,
    }, index=pd.Index(index))


def test_every_era_carries_its_own_episode_interval():
    tape = _alternating_tape()
    eras = tape_state.era_means(tape, "low|low")
    assert len(eras) >= 3
    for entry in eras:
        assert entry["episodes"] >= 1
        assert entry["episode_ci"] is not None
        assert entry["episode_ci"]["ci_low"] <= entry["episode_ci"]["ci_high"]


def test_block_reports_the_interval_beside_both_windows():
    tape = _alternating_tape(seed=11)
    out = tape_state.block(tape, str(tape.index[-1]))
    assert out["episode_ci"] is not None
    assert out["recent"]["episode_ci"] is not None
    assert out["episode_ci"]["n"] == out["episodes"]
    assert out["recent"]["episode_ci"]["n"] == out["recent"]["episodes"]
    # 区间必须同时给上下界，而不是一个点
    ci = out["episode_ci"]
    assert ci["ci_low"] < ci["mean"] < ci["ci_high"]


# ------------------------------------------- 档与板块：制度稳定口径的基础
def test_board_of_separates_bse_from_b_shares():
    """``9`` 开头既有沪 B（900xxx）也有北交所（920xxx）。

    第一版按一位前缀分，于是所有沪 B 都被算进北交所——北交所的涨跌幅是 30%、
    B 股是 10%，混在一起会让「30% 板」这一档凭空多出几百只票。
    """
    assert tape_state.board_of("600000.SH") == "沪主板"
    assert tape_state.board_of("688981.SH") == "科创"
    assert tape_state.board_of("000001.SZ") == "深主板"
    assert tape_state.board_of("002415.SZ") == "深主板"
    assert tape_state.board_of("300750.SZ") == "创业"
    assert tape_state.board_of("301029.SZ") == "创业"
    assert tape_state.board_of("430047.BJ") == "北交"
    assert tape_state.board_of("832000.BJ") == "北交"
    assert tape_state.board_of("920001.BJ") == "北交"
    assert tape_state.board_of("900901.SH") == "沪B"
    assert tape_state.board_of("200011.SZ") == "深B"


def test_band_of_reads_the_limit_off_the_data_not_off_a_name():
    """幅度是 stk_limit 与日线直接给的事实；名字要另建一条时间轴还有滞后。"""
    assert tape_state.band_of(11.0, 10.0) == "10%"
    assert tape_state.band_of(10.5, 10.0) == "5%"
    assert tape_state.band_of(12.0, 10.0) == "20%"
    assert tape_state.band_of(13.0, 10.0) == "30%"
    # 真实的一例：8.22 → 涨停价 9.04，幅度 9.98%，是 10% 板
    assert tape_state.band_of(9.04, 8.22) == "10%"
    # 落在档位之间的不算任何一档，也不猜最近的那一档
    assert tape_state.band_of(11.5, 10.0) is None
    assert tape_state.band_of(0.0, 10.0) is None
    assert tape_state.band_of(11.0, 0.0) is None


def test_the_main_board_is_the_only_stable_rule_across_the_whole_sample():
    """创业板 2020-08 才从 10% 改到 20%——所以按幅度分档会说「10% 档的成分变了」。

    这一条钉的是那个事实本身：创业板与主板的档位在 2020 前后不再一致，
    要一个跨 2010-2026 不变的口径只能按板块切。
    """
    assert tape_state.board_of("300001.SZ") == "创业"
    assert tape_state.board_of("600001.SH") == "沪主板"
    # 同一个 20% 幅度，在两个板块上含义不同（主板没有 20% 板）
    assert tape_state.band_of(12.0, 10.0) == "20%"
    assert tape_state.board_of("600001.SH") != tape_state.board_of("300001.SZ")


# ------------------------------------------- 一次运行查完三件事
def _alt_tape(mult: float, days: int = 400, seed: int = 0) -> pd.DataFrame:
    """和基准同标签、不同收益的序列，用来测「换定义后结论还在不在」。"""
    base = _alternating_tape(days=days, seed=seed)
    out = base.copy()
    out["fwd_seal2d"] = base["fwd_seal2d"] * mult
    return out


def test_audit_reports_a_side_count_not_a_verdict():
    """有没有判决字段是设计：代码给区间与口径的计数，读的人是调用方。"""
    tape = _alternating_tape()
    res = tape_state.audit(tape, "low|low")
    assert res["ok"] is True
    assert "verdict" not in res and "reliable" not in res and "trusted" not in res
    assert res["definitions"] == 1
    assert res["base"]["days"] > 0 and res["base"]["episodes"] > 0
    assert res["base"]["episode"] is not None
    assert sum(res["sides"].values()) == 1


def test_audit_counts_every_definition_on_the_same_side():
    tape = _alternating_tape(seed=3)
    alts = {"×1.0": _alt_tape(1.0, seed=3), "×1.01": _alt_tape(1.01, seed=3)}
    res = tape_state.audit(tape, "low|low", alts)
    assert res["definitions"] == 3
    assert sum(res["sides"].values()) == 3
    assert res["sides"][res["base"]["side"]] >= 1
    for name in alts:
        assert name in res["alternatives"]
        assert res["alternatives"][name]["episode"] is not None


def _fixed_tape(value: float, days: int = 4300) -> pd.DataFrame:
    """所有段的收益都一样，于是区间宽度为零——用来把「符号」单独拿出来测。"""
    tape = _alternating_tape(days=days)
    tape["fwd_seal2d"] = value
    return tape


def test_an_alternative_that_flips_the_sign_shows_up_in_the_counts():
    """把收益翻成负的：sides 必须出现两个非零项，而不是被平均掉。"""
    res = tape_state.audit(_fixed_tape(0.01), "low|low", {"翻负": _fixed_tape(-0.01)})
    sides = res["sides"]
    assert sides["positive"] == 1 and sides["negative"] == 1
    assert sides["crosses"] == 0


def test_a_zero_mean_lands_in_crosses_not_in_a_side():
    res = tape_state.audit(_fixed_tape(0.0), "low|low", {"也是零": _fixed_tape(0.0)})
    assert res["sides"]["crosses"] == 2
    assert res["sides"]["positive"] == 0 and res["sides"]["negative"] == 0


def test_a_degenerate_alternative_is_counted_as_unknown_not_dropped():
    tape = _alternating_tape(seed=7)
    res = tape_state.audit(tape, "low|low", {"空": tape.iloc[0:0]})
    assert res["sides"]["unknown"] == 1
    assert sum(res["sides"].values()) == 2


def test_audit_reports_how_much_the_labels_move_under_another_definition():
    """标签一致率不是结论的一部分，但没有它就看不出「结论不依赖标签」。"""
    tape = _alternating_tape(seed=9)
    shifted = tape.copy()
    shifted["bucket"] = "high|high"     # 序列里从不出现的档
    res = tape_state.audit(tape, "low|low", {"全换": shifted})
    overlap = res["label_agreement"]["全换"]
    assert overlap["whole"] == 0.0
    assert overlap["seal"] == 0.0 and overlap["count"] == 0.0
    assert overlap["days"] > 0


def test_audit_on_nothing_says_so():
    empty = pd.DataFrame()
    assert tape_state.audit(empty, "low|low")["ok"] is False
    tape = _alternating_tape(seed=11).drop(columns=["fwd_seal2d"])
    assert tape_state.audit(tape, "low|low")["ok"] is False


def test_the_side_of_an_interval_is_arithmetic():
    assert tape_state._side({"mean": 0.01, "ci_low": 0.002, "ci_high": 0.02}) == "positive"
    assert tape_state._side({"mean": -0.01, "ci_low": -0.02, "ci_high": -0.002}) == "negative"
    assert tape_state._side({"mean": 0.0, "ci_low": -0.005, "ci_high": 0.005}) == "crosses"
    assert tape_state._side({"mean": 0.0, "ci_low": 0.0, "ci_high": 0.005}) == "crosses"
    assert tape_state._side(None) == "unknown"
