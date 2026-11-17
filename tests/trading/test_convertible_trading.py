"""A convertible is a different instrument, not a stock with a different name.

Before this, `market.instrument()` recognised stocks and ETFs only, so a bond
inherited the main-board rules: 100-unit lots, T+1, a 10% band and a 5bps stamp
duty it does not owe. The kernel already supported T+0 (`oms.apply_fill` reads
`order.t_plus`), and the research layer already built a T+0 `Instrument` — the
trading desk was the one place that could not express it. That made
`cb_link_down`, the single structure the referee kept alive, impossible to
ticket.

The rules pinned here are the ones that change money: lot, tick, T+0, taxes,
band. A bond is also identified from the security master's own code ranges
(`symbols.CB_PREFIXES`), not from a guess at the first two digits.
"""

from __future__ import annotations

from datetime import datetime

from service.adapters.symbols import (
    A_SHARE_PREFIXES,
    CB_PREFIXES,
    is_convertible,
)
from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Board,
    D,
    Intent,
    OrderStatus,
    OrderType,
    Side,
)
from tests.trading.helpers import pin_to_dataset_bar

CAL = TradingCalendar.always()


def _liquid_bond(market, max_price: float | None = None) -> tuple[str, str]:
    """One bond with a real bar and an open comfortably inside the band."""
    frame = market.store.read_partition(
        "cb_daily", "2026",
        columns=["ts_code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "vol"],
    )
    frame = frame[frame["vol"] > 0]
    frame = frame[(frame["open"] > frame["pre_close"] * 0.85)
                  & (frame["open"] < frame["pre_close"] * 1.15)]
    if max_price is not None:
        frame = frame[frame["close"] < max_price]
    assert not frame.empty, "no convertible bar to test against"
    latest = str(frame["trade_date"].max())
    row = frame[frame["trade_date"] == latest].iloc[0]
    return str(row["ts_code"]), latest


def test_the_bond_is_classified_before_the_stock_rules(desk):
    symbol, day = _liquid_bond(desk.market)
    inst = desk.market.instrument(symbol, day)

    assert inst.board is Board.CB
    assert inst.is_etf is False
    assert inst.t_plus == 0, "a convertible settles T+0"
    assert inst.min_qty == 10 and inst.qty_step == 10
    assert inst.price_tick == D("0.001"), "bond prices carry three decimals"
    assert inst.stamp_duty == D("0") and inst.transfer_fee == D("0")
    assert inst.limit_pct == D("0.20")


def test_lots_are_ten_bonds_not_a_hundred(desk):
    symbol, day = _liquid_bond(desk.market)
    inst = desk.market.instrument(symbol, day)

    assert inst.buy_lot(7) == 0
    assert inst.buy_lot(25) == 20
    assert inst.buy_lot(10) == 10


def test_the_bond_reads_its_own_dataset(desk):
    """`daily` has no bond rows; an empty frame would look like "did not trade"."""
    symbol, day = _liquid_bond(desk.market)

    stocks = desk.market.daily(day)
    assert stocks[stocks["ts_code"] == symbol].empty
    snap = desk.market.snapshot(symbol, day)
    assert snap is not None
    assert snap.last > 0 and snap.volume > 0
    assert snap.source == "cb_daily"


def test_the_bar_carries_a_prior_session_for_capacity(desk):
    symbol, day = _liquid_bond(desk.market)
    built = desk.market.bar(symbol, day)
    assert built is not None
    bar, inst = built
    assert inst.t_plus == 0
    assert bar.prior_volume > 0, "capacity at the open needs yesterday's turnover"
    assert bar.volume > 0


def test_a_bond_can_be_bought_and_sold_in_the_same_session(desk):
    """The T+0 round trip, through the same ledger the desk uses."""
    symbol, day = _liquid_bond(desk.market)
    bar, inst = desk.market.bar(symbol, day)
    ts = datetime.strptime(day, "%Y%m%d").replace(hour=10, minute=0, tzinfo=SHANGHAI)
    ledger = Ledger.fresh("5000")

    limit = bar.open.quantize(D("0.001"))
    buy = Intent(symbol=symbol, side=Side.BUY, qty=10,
                 order_type=OrderType.LIMIT, limit_price=limit, sleeve="AGGRESSIVE")
    order = ledger.submit(buy, ts, inst, bar, CAL)
    assert order.status is OrderStatus.NEW, order.reason
    assert order.t_plus == 0

    matcher = BarExecutionModel()
    fill = matcher.match(order, bar, inst, ts, CAL)
    assert fill.filled, fill.reject_reason
    ledger.apply_fill(order, fill.fill)

    position = ledger.account.position(symbol)
    assert position.total_qty == 10
    assert position.available_qty == 10, "T+0: bought bonds are sellable today"

    sell = Intent(symbol=symbol, side=Side.SELL, qty=10,
                  order_type=OrderType.LIMIT,
                  limit_price=bar.close.quantize(D("0.001")), sleeve="AGGRESSIVE")
    exit_order = ledger.submit(sell, ts, inst, bar, CAL)
    assert exit_order.status is OrderStatus.NEW, exit_order.reason

    # No stamp duty on the sell leg either: 10 bonds at ~138 CNY is a ¥5 minimum
    # commission and nothing else.
    assert inst.stamp_duty == 0
    assert ledger.account.fees_paid > 0


def test_a_stock_bar_carries_the_previous_session_too(desk):
    """The capacity basis is shared with stocks; the routing is what differs."""
    day = desk.market.latest_bar_date()
    previous = desk.market.calendar.prev(day)
    today = desk.market.daily(day)
    symbol = str(today[today["vol"] > 0].iloc[0]["ts_code"])

    units = desk.market.traded_volume(symbol, previous)
    assert units > 0, "a liquid name traded yesterday"

    bar, _ = desk.market.bar(symbol, day)
    assert bar.prior_volume == units


def test_a_bond_can_be_ticketed_and_filled_through_the_desk(desk):
    """The whole chain: master → instrument → guard → ticket → fill.

    The first attempt at this stopped one layer short. The instrument rules were
    right, but `adv_map` was built from `daily` only, so every bond had ADV 0,
    `guard.liquidity` read that as unknown liquidity, and the buy was refused
    with "ADV20 unavailable". A T+0 instrument that cannot be ticketed is still
    a missing capability; the test has to cross the same boundary the model does.
    """
    symbol, day = _liquid_bond(desk.market, max_price=135)
    # The desk prices a proposal at *its* today, and `desk` pins that to the
    # stock feed's last bar. The bond feed lands later the same evening, so the
    # two are one session apart for a while; pin to the feed this test trades
    # from, or the guard refuses (rightly) and the failure reads like a bug.
    assert pin_to_dataset_bar(desk, "cb_daily") == day
    ticket = desk.decisions.propose({
        "symbol": symbol,
        "side": "BUY",
        "sleeve": "CORE",
        "qty": 10,
        "thesis": "可转债 T+0 试单：验证交易台能表达转债的整手、税率与当日回转规则，理由写满二十字。",
        "evidence": ["cb_daily:" + day],
        "invalidation": "跌破买入价 3% 即离场",
        "persona": desk.paper.persona_current()["current"],
    }, actor="test")

    assert ticket["status"] == "proposed", ticket.get("reasons")
    assert ticket["qty"] == 10, "the lot must survive sizing"

    result = desk.book.execute_ticket(ticket)
    assert result.ok and result.status == "filled", result.reason or result.status

    position = desk.book.ledger.account.position(symbol)
    assert position.total_qty == 10
    assert position.available_qty == 10, "T+0 through the paper book, not just the ledger"


def test_every_tradable_bond_in_the_master_is_recognised(desk):
    """The prefix list is data, so it is checked against the master each run.

    Scoped to codes that have traded, because the master also carries
    instruments that have no tradable code yet: a 定向可转债 appears under a
    vendor placeholder before it lists (`99SAQ2.SH`，芯导定转，`list_date` 为
    空、`cb_daily` 里没有它). 2026-11-05 那条码第一次进主表，这条用例就红了 —
    红的是"我把主表当成了可交易清单"，不是"规则漏了前缀".
    """
    master = desk.market.store.read("cb_basic")
    traded = desk.market.store.read_partition(
        "cb_daily", "2026", columns=["ts_code"])
    codes = set(traded["ts_code"].astype(str))
    live = [code for code in master["ts_code"].astype(str) if code in codes]
    assert len(live) > 50, f"only {len(live)} master codes have bars; join broke"

    unknown = [code for code in live if not is_convertible(code)]
    assert not unknown, f"bond codes outside the rule: {unknown[:5]}"


def test_a_placeholder_code_is_not_taught_as_a_bond():
    """`99xxxx` 是未上市定向可转债的占位码（`99SAQ2.SH` = 芯导定转）。

    If this goes red because someone widened the prefix list, the fix is not to
    widen it back: a code with no listing and no bar would then be priced with
    bond rules (T+0, 20% band, no stamp duty) and sized by a model that can
    never fill it. `instrument()` currently falls back to MAIN for it and
    `snapshot()` returns None, so nothing can be priced — that is the wanted
    behaviour, not an accident to paper over.
    """
    assert not is_convertible("99SAQ2.SH")
    assert not is_convertible("990001.SH"), "an index code is not a bond either"


def test_a_stock_and_an_etf_are_not_bonds():
    for code in ("600000.SH", "000001.SZ", "300750.SZ", "688981.SH",
                 "510300.SH", "159915.SZ", "430047.BJ"):
        assert not is_convertible(code), code


def test_the_bond_prefixes_do_not_overlap_the_stock_namespace():
    """A false positive would turn a stock into a T+0, tax-free instrument."""
    for bond in CB_PREFIXES:
        for stock in A_SHARE_PREFIXES:
            assert not bond.startswith(stock), f"{bond} vs {stock}"
            assert not stock.startswith(bond), f"{stock} vs {bond}"
