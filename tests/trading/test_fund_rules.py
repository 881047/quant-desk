"""ETF settlement is a property of the product, not of the ticker prefix.

The desk treated every ETF as T+1. Five of the core book's eighteen exposures
are not: the treasury ETF (511010), the two US-index QDIIs (513100, 513500), the
Hong Kong ETF (159920) and gold (518880) all settle T+0 on the exchange. For a
daily book that only rebalances monthly the error is small, but it is the wrong
direction on exactly the legs that are supposed to be the escape hatch: the
defensive leg could be sold the same session it was bought, and the model was
told it could not.

The rule is read from ``fund_basic.fund_type`` rather than guessed from the code:
159915 is ChiNext (T+1) and 159920 is Hong Kong (T+0) — same prefix, different
settlement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "configs" / "core.json"

# The core universe as of 2026-10-29. Written out rather than derived so that a
# change to the config has to confront this expectation instead of silently
# reclassifying a leg.
T0_EXPOSURES = {"511010.SH", "513100.SH", "513500.SH", "159920.SZ", "518880.SH"}


def _core_codes() -> set[str]:
    config = json.loads(CORE.read_text(encoding="utf-8"))
    return {config["defensive"]["code"], *[row["code"] for row in config["risk"]]}


def test_every_core_exposure_has_the_right_settlement(desk):
    day = desk.market.latest_bar_date()
    wrong = {}
    for code in sorted(_core_codes()):
        inst = desk.market.instrument(code, day)
        want = 0 if code in T0_EXPOSURES else 1
        if inst.t_plus != want:
            wrong[code] = (inst.t_plus, want)
    assert not wrong, f"settlement mismatch (got, want): {wrong}"


def test_the_defensive_leg_is_sellable_same_day(desk):
    day = desk.market.latest_bar_date()
    inst = desk.market.instrument("511010.SH", day)
    assert inst.is_etf and inst.t_plus == 0


@pytest.mark.parametrize("code", ["510300.SH", "512880.SH", "159915.SZ", "588000.SH"])
def test_domestic_equity_etfs_stay_t_plus_one(desk, code):
    day = desk.market.latest_bar_date()
    assert desk.market.instrument(code, day).t_plus == 1


def test_a_share_rules_are_untouched(desk):
    day = desk.market.latest_bar_date()
    daily = desk.market.daily(day)
    stock = str(daily[daily["vol"] > 0].iloc[0]["ts_code"])
    inst = desk.market.instrument(stock, day)
    assert inst.t_plus == 1 and not inst.is_etf
