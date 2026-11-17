"""Two price series, two jobs.

`close` is what an order pays and what a stop is compared against. `adj_close`
is what a return, a moving average or a percentile is measured on. Conflating
them books every dividend as a loss: measured on 2026-10-27, the 250-session
return of 600519.SH reads -8.69% raw and -4.94% adjusted, and across the market
the 200-day error exceeds 2% for one stock in five.

The adjustment runs on the exchange's own `pre_close` reference, not on the
vendor's cumulative factor — see `MarketRepository._attach_adj`. These tests pin
the invariants: a corporate action may not move the series, the last adjusted
price must equal the price on screen, and a corrupt factor table must not be able
to touch either.
"""

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from service.kernel.models import Board, D
from service.trading.market import MarketRepository


def _bars(rows):
    return pd.DataFrame(
        rows, columns=["ts_code", "trade_date", "close", "pre_close",
                       "open", "high", "low"])


def _attach(df):
    # `_attach_adj` touches no state: any instance will do.
    return MarketRepository._attach_adj(MarketRepository.__new__(MarketRepository), df)


def test_an_ex_date_gap_disappears_from_the_adjusted_series():
    """10 派 1: the tape prints a lower reference price, the holder lost nothing."""
    bars = _bars([
        ("600000.SH", "20260105", 10.00, 10.00, 10.00, 10.00, 10.00),
        ("600000.SH", "20260106", 9.05, 9.00, 9.05, 9.10, 9.00),   # ex-date, 1.00 cash
        ("600000.SH", "20260107", 9.10, 9.05, 9.05, 9.15, 9.02),
    ])

    out = _attach(bars)

    assert out["adj_close"].iloc[-1] == out["close"].iloc[-1], "level stays quotable"
    assert out["adj_close"].notna().all()
    # close/pre_close is the true overnight return on the ex-date, so the two-day
    # adjusted move is 9.10/10.00 by construction only if the dividend is ignored;
    # with it, the holder's return is (+0.56%) x (+0.56%).
    adj_return = float(out["adj_close"].iloc[-1] / out["adj_close"].iloc[0] - 1)
    raw_return = float(out["close"].iloc[-1] / out["close"].iloc[0] - 1)
    assert round(adj_return, 5) == round((9.05 / 9.00) * (9.10 / 9.05) - 1, 5)
    assert adj_return > raw_return, "the dividend may not read as a loss"


def test_a_flat_series_is_left_alone():
    """No corporate action in the window means adj_close is close."""
    bars = _bars([
        ("000001.SZ", "20260105", 10.0, 10.0, 0, 0, 0),
        ("000001.SZ", "20260106", 10.5, 10.0, 0, 0, 0),
        ("000001.SZ", "20260107", 10.8, 10.5, 0, 0, 0),
    ])
    out = _attach(bars)
    assert list(out["adj_close"].round(6)) == list(out["close"].round(6))


def test_a_split_does_not_become_a_ninety_percent_loss():
    """10 转 10: price halves, share count doubles, holder is even."""
    bars = _bars([
        ("300001.SZ", "20260105", 20.00, 20.00, 0, 0, 0),
        ("300001.SZ", "20260106", 10.00, 10.00, 0, 0, 0),   # ex-date, 1:2
        ("300001.SZ", "20260107", 10.20, 10.00, 0, 0, 0),
    ])
    out = _attach(bars)
    assert round(float(out["adj_close"].iloc[-1] / out["adj_close"].iloc[0] - 1), 2) == 0.02


def test_a_corrupt_factor_table_cannot_reach_the_series():
    """920227.BJ's vendor factor fell 938x in one day while the price did not move.

    `_attach_adj` no longer reads `adj_factor`, so that row is inert. This test
    exists to make the dependency explicit: if someone reintroduces the factor,
    it fails.
    """
    bars = _bars([("920227.BJ", "20261023", 47.96, 46.65, 0, 0, 0),
                  ("920227.BJ", "20261026", 47.91, 47.96, 0, 0, 0),
                  ("920227.BJ", "20261027", 46.20, 47.91, 0, 0, 0)])
    out = _attach(bars)
    reported = float(out["adj_close"].iloc[-1] / out["adj_close"].iloc[0] - 1)
    assert -0.10 < reported < 0.10, f"a flat week read as {reported:.1%}"
    assert abs(float(out["adj_close"].iloc[-1]) - 46.20) < 1e-9


def test_each_symbol_is_anchored_on_its_own_last_price():
    bars = _bars([
        ("A", "20260105", 10.0, 10.0, 0, 0, 0),
        ("A", "20260106", 12.0, 10.0, 0, 0, 0),
        ("B", "20260105", 20.0, 20.0, 0, 0, 0),
        ("B", "20260106", 22.0, 20.0, 0, 0, 0),
    ])
    out = _attach(bars)
    last = out.groupby("ts_code").tail(1).set_index("ts_code")
    assert last.loc["A", "adj_close"] == last.loc["A", "close"]
    assert last.loc["B", "adj_close"] == last.loc["B", "close"]


def test_a_missing_pre_close_falls_back_to_flat_not_to_a_jump():
    bars = _bars([("000003.SZ", "20260105", 10.0, None, 0, 0, 0),
                  ("000003.SZ", "20260106", 11.0, 10.0, 0, 0, 0)])
    out = _attach(bars)
    assert out["adj_close"].notna().all()
    assert abs(float(out["adj_close"].iloc[-1]) - 11.0) < 1e-9


class _EmptyStore:
    def __init__(self, root):
        self.root = root

    def read(self, dataset):
        return pd.DataFrame()


def _st_market(tmp_path):
    market = MarketRepository(_EmptyStore(tmp_path))
    market._universe = SimpleNamespace(is_st=lambda symbol, day: True)
    return market


@pytest.mark.parametrize(("symbol", "board", "limit"), [
    ("600000.SH", Board.MAIN, D("0.05")),
    ("300001.SZ", Board.CHINEXT, D("0.20")),
    ("688001.SH", Board.STAR, D("0.20")),
    ("920001.BJ", Board.BSE, D("0.30")),
])
def test_st_limit_depends_on_the_board_not_one_global_five_percent(
        tmp_path, symbol, board, limit):
    instrument = _st_market(tmp_path).instrument(symbol, "20260107")

    assert instrument.board == board
    assert instrument.is_st
    assert instrument.limit_pct == limit


def test_announcement_text_rejects_path_traversal(tmp_path):
    cache = tmp_path / "cache"
    folder = cache / "anns_text"
    folder.mkdir(parents=True)
    (folder / "123.json").write_text(json.dumps({"text": "ok"}), encoding="utf-8")
    (cache / "secret.json").write_text(json.dumps({"text": "outside"}), encoding="utf-8")
    market = MarketRepository(_EmptyStore(tmp_path))

    assert market.announcement_text("123") == {"text": "ok"}
    assert market.announcement_text("../secret") is None
    assert market.announcement_text("..\\secret") is None
    assert market.announcement_text("123.json") is None


def test_single_symbol_adv_uses_the_same_cny_unit_as_adv_map():
    market = MarketRepository.__new__(MarketRepository)
    market.history = lambda symbol, day, days: pd.DataFrame({"amount": [1000.0, 3000.0]})

    assert market.adv("600000.SH", "20260107") == pytest.approx(2_000_000.0)
