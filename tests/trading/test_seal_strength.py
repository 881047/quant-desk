"""封单强度 is 封单/流通市值, and ranking by 封单额 answers a different question.

The two rankings are not near-duplicates. Across 198 sessions of 2026 the
absolute top-5 and the ratio top-5 agreed on 34% of names, never matched exactly,
and disagreed on at least three of five on 154 days. The absolute number is a
proxy for market cap: 1.15亿 of sealed bids on a 246亿 float is 0.47% and is one
of the weakest boards of the session, while 6.10亿 on 54.6亿 is 11.2% and is the
strongest.

These tests pin which scale produced the list, because a reader who cannot see
that would compare a number in 亿 against a number in percent.
"""

from __future__ import annotations

import pandas as pd

from service.trading import tape
from service.trading.market import MarketRepository


def _up(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["trade_date"] = "20261030"
    return frame


def _pool() -> pd.DataFrame:
    """Six boards: the biggest seal is not the strongest seal."""
    return _up([
        # name, seal 亿, float 亿
        {"ts_code": "000001.SZ", "name": "大票弱板", "seal_money": 6.0e8,
         "continue_text": "首板", "limit_up_time": "14:30"},
        {"ts_code": "000002.SZ", "name": "小票强板", "seal_money": 1.2e8,
         "continue_text": "2连板", "limit_up_time": "09:33"},
        {"ts_code": "000003.SZ", "name": "次强", "seal_money": 3.0e8,
         "continue_text": "3连板", "limit_up_time": "09:40"},
        {"ts_code": "000004.SZ", "name": "陪跑一", "seal_money": 1.0e8,
         "continue_text": "首板", "limit_up_time": "10:10"},
        {"ts_code": "000005.SZ", "name": "陪跑二", "seal_money": 8.0e7,
         "continue_text": "首板", "limit_up_time": "10:20"},
        {"ts_code": "000006.SZ", "name": "陪跑三", "seal_money": 5.0e7,
         "continue_text": "首板", "limit_up_time": "13:10"},
    ])


def _strength() -> pd.Series:
    floats = {
        "000001.SZ": 1000e8,  # 0.60%  — the weakest
        "000002.SZ": 9e8,     # 13.3%  — the strongest
        "000003.SZ": 40e8,    # 7.5%
        "000004.SZ": 30e8,    # 3.3%
        "000005.SZ": 60e8,    # 1.3%
        "000006.SZ": 50e8,    # 1.0%
    }
    return pd.Series(floats)


def test_without_a_float_series_the_ranking_is_absolute_and_says_so():
    summary = tape.summarize_limit(None, _pool(), None, None, None)

    assert summary["seal_rank_basis"] == "seal_money"
    assert summary["seal_strength"][0]["name"] == "大票弱板"
    assert summary["seal_strength"][0]["seal_ratio"] is None
    assert summary["seal_ratio_pool_median"] is None


def test_with_a_float_series_the_weak_large_cap_leaves_the_top_five():
    summary = tape.summarize_limit(None, _pool(), None, None, None,
                                   strength=_strength(),
                                   strength_basis="seal_money_over_circ_mv")

    assert summary["seal_rank_basis"] == "seal_money_over_circ_mv"
    names = [row["name"] for row in summary["seal_strength"]]
    assert names[0] == "小票强板"
    assert names[1] == "次强"
    # 6.0亿 on a 1000亿 float is 0.60%: below every other board of the session.
    assert "大票弱板" not in names
    assert summary["seal_pool_n"] == 6
    assert summary["seal_ratio_pool_median"] is not None


def test_the_rank_and_the_ratio_travel_together():
    summary = tape.summarize_limit(None, _pool(), None, None, None,
                                   strength=_strength(),
                                   strength_basis="seal_money_over_circ_mv")
    top = summary["seal_strength"][0]

    assert top["rank"] == 1
    assert top["seal_ratio"] == round(1.2e8 / 9e8, 4)
    # The absolute number is still reported: the ratio explains it, it does not replace it.
    assert top["seal_money_cny"] == 1.2e8


def test_a_board_with_no_float_is_dropped_rather_than_ranked_blind():
    strength = _strength().drop("000003.SZ")
    summary = tape.summarize_limit(None, _pool(), None, None, None,
                                   strength=strength,
                                   strength_basis="seal_money_over_circ_mv")

    names = [row["name"] for row in summary["seal_strength"]]
    assert "次强" not in names
    assert summary["seal_pool_n"] == 5


def test_seal_strength_falls_back_when_daily_basic_is_missing():
    class FakeData:
        def read(self, name, start=None, end=None, columns=None, **kwargs):
            return pd.DataFrame()

    market = MarketRepository(FakeData())
    strength, basis = tape._seal_strength(market, "20261030")

    assert strength is None
    assert basis == "seal_money"


def test_seal_strength_converts_wan_yuan_to_yuan_exactly_once():
    """``circ_mv`` is 万元; a missed factor of 1e4 makes every board look 10000x strong."""
    frame = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20261030"],
                          "circ_mv": [2_460_000.0]})

    class FakeData:
        def read(self, name, start=None, end=None, columns=None, **kwargs):
            return frame.copy()

    market = MarketRepository(FakeData())
    strength, basis = tape._seal_strength(market, "20261030")

    assert basis == "seal_money_over_circ_mv"
    assert strength["600000.SH"] == 2.46e10
