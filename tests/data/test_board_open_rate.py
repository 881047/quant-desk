"""开板判定：5 分钟 bar 的边界就是全部答案所在。

一根标 09:35 的 bar 覆盖 09:30–09:35。如果 09:33 才封板，它的最低价天然低于涨停价
——把这种 bar 算作"开过板"，会把每一个早盘板都记成开过，开板率变成 100%，而这正好
是"打板一定能买到"那个错误假设的另一种写法。

所以判定规则只有一条，但它必须精确：只取**起点晚于首封时刻**的 bar。少算一根是
保守，多算一根是造假。
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.board_open_rate import _minute_of_day, opened_after_seal


def _bars(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """(bar end label, low) pairs, in the shape ``stk_mins`` returns."""
    return pd.DataFrame({
        "trade_time": [stamp for stamp, _ in rows],
        "low": [low for _, low in rows],
    })


def test_bar_labels_parse_from_both_shapes():
    assert _minute_of_day("09:33") == 9 * 60 + 33
    assert _minute_of_day("2026-10-30 09:35:00") == 9 * 60 + 35
    assert _minute_of_day("") is None
    assert _minute_of_day(None) is None


def test_the_bar_that_contains_the_seal_does_not_count_as_an_open():
    """09:33 seal, 09:35 bar (covers 09:30–09:35) traded through 9.90 before it sealed.

    Counting it would mark every morning board as reopened — the single mistake
    that would make this whole calibration read "you can always get filled".
    """
    bars = _bars([("2026-10-30 09:35:00", 9.90),
                  ("2026-10-30 09:40:00", 11.00),
                  ("2026-10-30 09:45:00", 11.00)])

    assert opened_after_seal(bars, "09:33", 11.00) is False


def test_a_later_bar_below_the_ceiling_is_a_reopened_board():
    bars = _bars([("2026-10-30 09:40:00", 11.00),
                  ("2026-10-30 10:15:00", 10.94),
                  ("2026-10-30 10:20:00", 11.00)])

    assert opened_after_seal(bars, "09:33", 11.00) is True


def test_a_board_sealed_at_the_close_has_no_window_at_all():
    """14:57 gives no bar that starts after it; the question is unanswerable.

    ``None`` (not ``False``) keeps these out of the denominator. Counting them
    as "never opened" would pull the rate down by exactly the late-session
    boards — the bucket whose premium a buyer would most like to know.
    """
    bars = _bars([("2026-10-30 14:55:00", 11.00),
                  ("2026-10-30 15:00:00", 11.00)])

    assert opened_after_seal(bars, "14:57", 11.00) is None


def test_a_seal_before_the_first_bar_still_uses_the_whole_session():
    bars = _bars([("2026-10-30 09:35:00", 11.00),
                  ("2026-10-30 09:40:00", 11.00)])

    assert opened_after_seal(bars, "09:31", 11.00) is False


def test_a_missing_seal_time_is_unanswerable_rather_than_false():
    bars = _bars([("2026-10-30 09:40:00", 10.90)])

    assert opened_after_seal(bars, "", 11.00) is None
    assert opened_after_seal(bars, None, 11.00) is None


def test_a_fen_of_tolerance_keeps_a_touch_from_looking_like_a_break():
    """A low of 10.9999 is a float artifact, not a trade below the ceiling."""
    bars = _bars([("2026-10-30 10:00:00", 10.9999)])

    assert opened_after_seal(bars, "09:33", 11.00) is False


def test_the_limit_up_pool_and_the_calibration_agree_on_the_session_set():
    """The calibration window must be sessions the pool actually carries.

    A window built from the trading calendar instead of the dataset's own
    partitions would include days the vendor never delivered, and every board on
    those days would silently drop out as "unanswerable".
    """
    days = ["20260911", "20260914", "20261030"]

    class Registry:
        def list_manifest(self, dataset):
            assert dataset == "ths_limit_up"
            return [type("R", (), {"partition": d, "status": "ok"})() for d in days]

    store = type("S", (), {"registry": Registry()})()
    from scripts.board_open_rate import _sessions

    assert _sessions(store, 2) == ["20260914", "20261030"]
    assert _sessions(store, 30) == days


def test_the_summary_measures_open_rate_only_where_it_was_answerable():
    """The denominator is answerable boards, not all boards.

    ``summarize`` used to be handed everything and average a nullable column,
    which pandas turns into ``NaN`` — a rate that prints as 0% and reads as
    "no board ever opens".
    """
    from scripts.board_open_rate import summarize

    boards = pd.DataFrame({
        "ts_code": ["A", "B", "C", "D"],
        "trade_date": ["20261028", "20261028", "20261029", "20261029"],
        "opened": pd.array([True, False, None, True], dtype="boolean"),
        "gap": [0.01, 0.03, 0.02, -0.01],
        "open_d": [10.50, 11.00, 10.80, 11.00],
        "last_price": [11.00, 11.00, 11.00, 11.00],
        "seal_ratio": [0.01, 0.02, 0.03, 0.04],
        "continue_cnt": [1, 1, 1, 1],
        "limit_up_time": ["09:33", "10:00", "14:57", "09:40"],
    })
    summary = summarize(boards)

    assert summary["n"] == 3
    assert summary["unanswerable"] == 1
    assert summary["overall"]["open_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_a_one_word_board_is_not_counted_as_a_lost_queue():
    """“Never opened” is two trades: no queue ever existed, or the queue was lost.

    Folding them together makes sealed boards look uniformly lucrative and hides
    the one group that is unbuyable no matter how fast the buyer is.
    """
    from scripts.board_open_rate import summarize

    boards = pd.DataFrame({
        "ts_code": ["A", "B", "C"],
        "trade_date": ["20261028"] * 3,
        "opened": pd.array([False, False, True], dtype="boolean"),
        "gap": [0.04, 0.005, 0.006],
        "open_d": [11.00, 10.20, 10.40],
        "last_price": [11.00, 11.00, 11.00],
        "seal_ratio": [0.05, 0.01, 0.02],
        "continue_cnt": [2, 1, 1],
        "limit_up_time": ["09:31", "10:10", "09:50"],
    })
    groups = {row["bucket"]: row for row in summarize(boards)["by_opened"]}

    assert groups["一字板（没排队机会）"]["n"] == 1
    assert groups["一字板（没排队机会）"]["next_open_gap_mean"] == 0.04
    assert groups["没开·非一字板"]["n"] == 1
    assert groups["没开·非一字板"]["next_open_gap_mean"] == 0.005
    assert groups["开过板（可排队）"]["n"] == 1
