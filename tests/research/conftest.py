from __future__ import annotations

import pandas as pd
import pytest


class FakeStore:
    """Minimal stand-in for DatasetStore.read()."""

    def __init__(self, daily: pd.DataFrame, limits: pd.DataFrame, pools: pd.DataFrame):
        self.data = {"daily": daily, "stk_limit": limits, "limit_pool_t0": pools}

    def read(self, dataset, start=None, end=None, *, columns=None, status=None):
        df = self.data.get(dataset, pd.DataFrame()).copy()
        if df.empty or "trade_date" not in df.columns:
            return df
        df["trade_date"] = df["trade_date"].astype(str)
        if start:
            df = df[df["trade_date"] >= str(start)]
        if end:
            df = df[df["trade_date"] <= str(end)]
        return df


def make_market() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Three names: one tradable, one opens at the limit, one suspended."""
    days = ["20240102", "20240103", "20240104", "20240105"]
    rows = []

    def append(code: str, series: dict, seed: float) -> None:
        """``pre_close`` is the previous session's close, as the exchange defines it.

        The fixture used to hold it at a constant, which is not a price history:
        every bar then claimed the previous close had been re-based overnight,
        i.e. every bar looked like an ex-date. A detector that reads
        ``pre_close`` cannot be tested against data that never uses it.
        """
        prev = seed
        for d, (o, c) in series.items():
            rows.append({"ts_code": code, "trade_date": d, "open": o, "close": c,
                         "pre_close": prev, "amount": 100000 if code != "600003.SH" else 50000})
            prev = c

    # 600001: normal, rises 10.5 -> 11.0 on the entry day.
    append("600001.SH", {"20240102": (10.0, 10.0), "20240103": (10.5, 11.0),
                         "20240104": (11.0, 11.55), "20240105": (11.5, 11.2)}, 10.0)
    # 600002: opens at the upper limit on the entry day -> cannot buy.
    append("600002.SH", {"20240102": (10.0, 10.0), "20240103": (11.0, 11.0),
                         "20240104": (11.5, 11.8), "20240105": (11.8, 12.0)}, 10.0)
    # 600003: suspended on the entry day -> no bar, must be skipped. Its pre_close
    # on 20240104 is still 20240102's close, because 20240103 never traded.
    append("600003.SH", {"20240102": (8.0, 8.0), "20240104": (8.4, 8.6),
                         "20240105": (8.6, 8.7)}, 8.0)
    daily = pd.DataFrame(rows)

    limits = pd.DataFrame([
        {"ts_code": code, "trade_date": d,
         "up_limit": round(10.0 * 1.1, 2) if code.startswith("60000") else 8.8,
         "down_limit": round(10.0 * 0.9, 2)}
        for code in ("600001.SH", "600002.SH", "600003.SH") for d in days
    ])
    limits.loc[(limits.ts_code == "600002.SH") & (limits.trade_date == "20240103"),
               "up_limit"] = 11.0

    pools = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20240102", "pool": "limit_up",
         "boards": 1, "name": "甲股份", "amount": 100000},
        {"ts_code": "600002.SH", "trade_date": "20240102", "pool": "limit_up",
         "boards": 1, "name": "乙股份", "amount": 100000},
        {"ts_code": "600003.SH", "trade_date": "20240102", "pool": "limit_up",
         "boards": 1, "name": "丙股份", "amount": 50000},
    ])
    return daily, limits, pools


@pytest.fixture()
def store() -> FakeStore:
    return FakeStore(*make_market())


@pytest.fixture()
def store_bb_blocked() -> FakeStore:
    """The deepest broken board opens limit-up next day; the second one does not."""
    rows = []
    for d, o, c, pc in [("20240102", 9.6, 9.2, 10.0),
                        ("20240103", 10.12, 10.12, 9.2),
                        ("20240104", 10.1, 10.0, 10.12)]:
        rows.append({"ts_code": "600001.SH", "trade_date": d, "open": o, "close": c,
                     "pre_close": pc, "amount": 100000})
    for d, o, c, pc in [("20240102", 10.8, 10.6, 11.0),
                        ("20240103", 10.4, 10.8, 10.6),
                        ("20240104", 10.8, 10.9, 10.8)]:
        rows.append({"ts_code": "600002.SH", "trade_date": d, "open": o, "close": c,
                     "pre_close": pc, "amount": 100000})
    daily = pd.DataFrame(rows)
    limits = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20240102", "up_limit": 11.0, "down_limit": 9.0},
        {"ts_code": "600001.SH", "trade_date": "20240103", "up_limit": 10.12, "down_limit": 8.28},
        {"ts_code": "600001.SH", "trade_date": "20240104", "up_limit": 11.13, "down_limit": 9.11},
        {"ts_code": "600002.SH", "trade_date": "20240102", "up_limit": 11.0, "down_limit": 9.0},
        {"ts_code": "600002.SH", "trade_date": "20240103", "up_limit": 11.66, "down_limit": 9.54},
        {"ts_code": "600002.SH", "trade_date": "20240104", "up_limit": 11.88, "down_limit": 9.72},
    ])
    pools = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20240102", "pool": "broken_board",
         "boards": 0, "name": "甲股份", "amount": 100000, "limit_price": 11.0, "close": 9.2},
        {"ts_code": "600002.SH", "trade_date": "20240102", "pool": "broken_board",
         "boards": 0, "name": "乙股份", "amount": 100000, "limit_price": 11.0, "close": 10.6},
    ])
    return FakeStore(daily, limits, pools)


@pytest.fixture()
def store_two_tradable() -> FakeStore:
    """Same market, but 600002 no longer opens at its limit."""
    daily, limits, pools = make_market()
    limits.loc[(limits.ts_code == "600002.SH") & (limits.trade_date == "20240103"),
               "up_limit"] = 12.0
    return FakeStore(daily, limits, pools)
