"""Plain helpers for trading tests. **Not a conftest, never imported as a plugin.**

Until 2026-11-07 these lived in ``tests/trading/conftest.py``. That file is a
pytest plugin (registered under its filesystem path) *and* was imported by 15
test modules for ``proposal`` / ``pick_symbols``. Depending on the order of an
explicit multi-file pytest command, the plain import created a second module
object and the fixtures declared in the first one became invisible —
``fixture 'desk' not found``, 12 errors, while the same six files with the
order reversed produced 47 passed. Pre-importing the conftest and switching to
``--import-mode=importlib`` were both measured and neither fixed it.

The durable fix is the one this file represents: a conftest holds fixtures only,
helpers live in a module that pytest never registers as a plugin. No test module
imports ``tests.trading.conftest`` any more.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from service.kernel.clock import SHANGHAI

ROOT = Path(__file__).resolve().parents[2]


def pin_to_dataset_bar(desk, dataset: str) -> str:
    """Make "today" the last session *this dataset* covers.

    Not the same date as ``latest_bar_date()``: the four bar datasets are
    ingested on separate schedules, so on the evening the stock partition lands
    ``daily`` is at today while ``cb_daily`` and ``fund_daily`` still end
    yesterday. A bond test that pinned to the stock calendar asked the desk to
    price a bond on a session the bond feed does not have — the guard then
    refused (correctly) and the test looked broken. 2026-11-05 17:40, four
    cases went red that way.
    """
    bar = desk.market.dataset_cover_end(dataset)
    assert bar, f"{dataset} has no published partition"
    stamp = datetime.strptime(bar, "%Y%m%d").replace(hour=17, minute=0, tzinfo=SHANGHAI)
    desk.decisions._now = lambda: stamp
    return bar


def pin_to_last_bar(desk) -> str:
    """Make "today" the last date with a published bar.

    The real clock is a trading day whose bar does not exist yet, and the desk
    correctly refuses to trade on a stale close. Tests are not about that guard,
    so they pin the clock instead of weakening the rule.
    """
    return pin_to_dataset_bar(desk, "daily")


def pick_symbols(desk, n: int = 3, max_price: float = 9.5) -> list[str]:
    date = desk.market.latest_bar_date()
    daily = desk.market.daily(date)
    basic = desk.market.basic()
    df = daily.merge(basic[["ts_code", "name", "list_date"]], on="ts_code", how="left")
    df = df[(df["close"] <= max_price) & (df["close"] > 3) & (df["vol"] > 0)]
    df = df[~df["ts_code"].str.endswith((".BJ",))]
    names = df["name"].astype(str)
    df = df[~names.str.contains("ST", na=False)]
    return df["ts_code"].head(n).tolist()


def pick_st_symbol(desk) -> str | None:
    date = desk.market.latest_bar_date()
    nc = desk.market.namechange()
    if nc.empty:
        return None
    active = nc[
        (nc["start_date"].astype(str) <= date)
        & (nc["end_date"].fillna("99999999").astype(str) >= date)
        & (nc["name"].astype(str).str.contains("ST"))
    ]
    if active.empty:
        return None
    listed = set(desk.market.basic()["ts_code"])
    traded = set(desk.market.daily(date)["ts_code"])
    for code in active["ts_code"]:
        if code in listed and code in traded:
            return str(code)
    return None


def proposal(symbol: str, **overrides) -> dict:
    body = {
        "symbol": symbol,
        "side": "BUY",
        "sleeve": "CORE",
        "intent_type": "test",
        "thesis": "核心仓再平衡，估值与股息提供安全边际，按票据流程测试。",
        "evidence": ["daily:latest", "quality:ok"],
        "invalidation": "收盘跌破前低或数据被隔离",
        "target_weight": 0.2,
    }
    body.update(overrides)
    return body
