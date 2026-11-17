from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pandas as pd
import pytest

from service.trading.core import (
    DEFENSIVE_ETF,
    RISK_ETFS,
    CoreBook,
    CoreConfig,
    load_universe,
)
from service.trading.state import PaperStore


class FundStore:
    """Just enough of DatasetStore for the core book."""

    def __init__(self, prices: pd.DataFrame, adj_factor: float = 1.0):
        self._prices = prices
        self._adj_factor = adj_factor

    def read(self, dataset, start=None, end=None, **kwargs):
        if dataset == "fund_daily":
            return self._prices.rename(columns={"ts_code": "ts_code"})[
                ["ts_code", "trade_date", "close"]].copy()
        if dataset == "fund_adj":
            return self._prices[["ts_code", "trade_date"]].assign(
                adj_factor=self._adj_factor)
        return pd.DataFrame()


class MemoryPaper:
    def __init__(self):
        self.nav: list[dict] = []
        self.trades: list[dict] = []

    def core_record_nav(self, trade_date, nav, cash, invested, weights):
        self.nav = [r for r in self.nav if r["trade_date"] != trade_date]
        self.nav.append({"trade_date": trade_date, "nav": nav, "cash": cash,
                         "invested": invested, "weights": weights})
        self.nav.sort(key=lambda r: r["trade_date"])

    def core_nav_history(self, limit=400):
        return self.nav[-limit:]

    def core_record_trade(self, trade_date, symbol, side, qty, price, fee, reason=""):
        self.trades.append({"trade_date": trade_date, "symbol": symbol, "side": side,
                            "qty": qty, "price": price, "fee": fee, "reason": reason})

    def core_trades(self, limit=100):
        return list(reversed(self.trades))[:limit]


def make_prices(days: int = 260, slope: float = 0.0015) -> pd.DataFrame:
    rows = []
    for i, code in enumerate(RISK_ETFS + (DEFENSIVE_ETF,)):
        base = 1.0 + i * 0.7
        for t in range(days):
            date = f"2025{(t // 28) + 1:02d}{(t % 28) + 1:02d}"
            rows.append({"ts_code": code, "trade_date": date,
                         "close": round(base * (1 + slope * t), 4)})
    return pd.DataFrame(rows)


def _own_targets(book, day, targets=None):
    """A plan whose weights were supplied, not chosen by the rule.

    ``execute()`` refuses rule-generated plans, and the API no longer accepts
    them either. Tests of lots, fees and cash need an executable plan, so they
    submit the rule's own weights as if a caller had picked them - the
    arithmetic is identical, the provenance is not.
    """
    if targets is None:
        targets = book.target_weights(day)
        targets.pop("_meta", None)
    return book.plan(day, targets=targets)


def test_trend_up_allocates_to_risk_assets():
    store = FundStore(make_prices())
    book = CoreBook(store, MemoryPaper())
    weights = book.target_weights("20250101") if False else book.target_weights(book.last_day())
    meta = weights.pop("_meta")
    assert meta["trend_ok"] is True
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert max(weights.values()) <= book.cfg.max_weight + 1e-6
    assert DEFENSIVE_ETF in weights


def test_trend_down_goes_defensive():
    store = FundStore(make_prices(slope=-0.0015))
    book = CoreBook(store, MemoryPaper())
    weights = book.target_weights(book.last_day())
    meta = weights.pop("_meta")
    assert meta["trend_ok"] is False
    assert weights == {DEFENSIVE_ETF: 1.0}


def test_rebalance_respects_lots_and_fees():
    store = FundStore(make_prices())
    paper = MemoryPaper()
    book = CoreBook(store, paper)
    day = book.last_day()
    result = book.execute(_own_targets(book, day))
    assert result["orders"], "a trend-up book must buy something"
    for order in result["orders"]:
        assert order["qty"] % 100 == 0
        assert order["fee"] >= 5.0
    assert result["nav"] < 5000.0  # commissions are real
    assert result["nav"] > 4800.0
    holdings = book.state().holdings
    assert all(q % 100 == 0 for q in holdings.values())


def test_unaffordable_sleeve_stays_in_cash():
    prices = make_prices()
    prices.loc[prices["ts_code"] == DEFENSIVE_ETF, "close"] = 147.0
    store = FundStore(prices)
    book = CoreBook(store, MemoryPaper())
    # 511010 at 147/share costs 14,700 per lot: a 5,000 book cannot hold it.
    result = book.execute(_own_targets(book, book.last_day(),
                                       {DEFENSIVE_ETF: 0.6, "510300.SH": 0.4}))
    assert DEFENSIVE_ETF in result["skipped_unaffordable"]
    assert result["cash_weight"] > 0.4


def test_should_rebalance_is_monthly_or_on_regime_flip():
    store = FundStore(make_prices())
    paper = MemoryPaper()
    book = CoreBook(store, paper)
    day = book.last_day()
    need, why = book.should_rebalance(day)
    assert need is True and why == "empty book"
    book.execute(_own_targets(book, day))
    need, why = book.should_rebalance(day)
    assert need is False and why == "hold"


def test_config_caps_are_enforced():
    cfg = CoreConfig(max_weight=0.30, vol_target=0.08)
    store = FundStore(make_prices())
    book = CoreBook(store, MemoryPaper(), config=cfg)
    weights = book.target_weights(book.last_day())
    weights.pop("_meta", None)
    assert max(weights.values()) <= 0.30 + 1e-6


def test_an_impossible_cap_raises_instead_of_breaking_the_cap():
    """Four names cannot each stay under 20% and still sum to 1.

    The old code clipped to the cap and then renormalised, which put the
    survivors straight back over it - so the limit was decorative. Now the
    combination is refused, because a risk cap that silently does not hold is
    worse than a config error.
    """
    cfg = CoreConfig(max_weight=0.20, vol_target=0.08)
    book = CoreBook(FundStore(make_prices()), MemoryPaper(), config=cfg)
    with pytest.raises(RuntimeError) as exc:
        book.target_weights(book.last_day())
    assert "cannot both hold" in str(exc.value)
    assert "at least 5" in str(exc.value)


# ---------------------------------------------------------------- universe config
def _synthetic(days: int = 320):
    """Risk names with clearly different volatility, and a bond that barely moves."""
    specs = [(RISK_ETFS[0], 0.0020, 0.020),
             (RISK_ETFS[1], 0.0012, 0.012),
             (RISK_ETFS[2], 0.0008, 0.006),
             (DEFENSIVE_ETF, 0.0001, 0.00002)]
    rows = []
    for code, drift, swing in specs:
        px = 1.0
        for t in range(days):
            px *= 1 + drift + (swing if t % 2 else -swing)
            rows.append({"ts_code": code,
                         "trade_date": f"2025{(t // 28) % 12 + 1:02d}{(t % 28) + 1:02d}",
                         "close": round(px, 6)})
    return pd.DataFrame(rows)


def test_the_defensive_etf_is_the_residual_not_a_parity_member():
    """The bond sleeve must receive exactly (1 - scale), nothing else.

    It used to be computed over every column in the price frame, so the treasury
    ETF entered the inverse-volatility pool with its near-zero vol, swallowed the
    weight, was clipped, and then *also* received the volatility-target
    remainder - while occupying one of the position slots. The docstring has
    always said "the remainder in a treasury ETF"; the code disagreed.
    """
    book = CoreBook(FundStore(_synthetic()), MemoryPaper())
    weights = book.target_weights(book.last_day())
    meta = weights.pop("_meta")
    assert meta["trend_ok"] is True, "the synthetic series trends up on purpose"

    risk_total = sum(v for k, v in weights.items() if k != DEFENSIVE_ETF)
    # The invariant: the bond sleeve is exactly the complement of the risk book.
    # If the treasury ETF were a parity member it would also carry an
    # inverse-vol weight here and this would overshoot.
    assert weights[DEFENSIVE_ETF] == pytest.approx(1.0 - risk_total, abs=1e-9)
    assert risk_total < 1.0, "the account is not fully invested at a 12% vol target"
    assert meta["held"] == 3, "only the three risk names are parity members"
    assert DEFENSIVE_ETF not in meta["dropped_for_breadth"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_the_book_never_holds_more_names_than_the_account_can_buy():
    book = CoreBook(FundStore(make_prices()), MemoryPaper())
    weights = book.target_weights(book.last_day())
    meta = weights.pop("_meta")
    assert meta["held"] <= CoreConfig().max_positions
    risk_weights = {k: v for k, v in weights.items()
                    if k not in (DEFENSIVE_ETF, "_meta") and v > 0}
    assert len(risk_weights) <= CoreConfig().max_positions
    assert len(meta["dropped_for_breadth"]) == len(RISK_ETFS) - meta["held"]


def test_the_universe_is_read_from_config_not_from_code(tmp_path):
    cfg = tmp_path / "core.json"
    cfg.write_text(
        '{"defensive": {"code": "511990.SH"}, '
        '"risk": [{"code": "510300.SH", "exposure": "a_large"}, '
        '{"code": "518880.SH", "exposure": "commodity_gold"}], '
        '"max_positions": 2}', encoding="utf-8")
    universe = load_universe(cfg)
    assert universe.defensive == "511990.SH"
    assert universe.risk == ("510300.SH", "518880.SH")
    assert universe.max_positions == 2
    assert universe.bucket("518880.SH") == "commodity_gold"


@pytest.mark.parametrize("body, message", [
    ('{"defensive": {"code": "511990.SH"}, "risk": []}', "at least one risk"),
    ('{"risk": [{"code": "510300.SH"}]}', "defensive"),
    ('{"defensive": {"code": "510300.SH"}, "risk": [{"code": "510300.SH"}]}',
     "both defensive and risk"),
    ('{"defensive": {"code": "511990.SH"}, '
     + '"risk": [{"code": "510300.SH"}, {"code": "510300.SH"}]}', "more than once"),
])
def test_a_broken_universe_stops_the_desk(tmp_path, body, message):
    """Silently trading a different universe would be worse than not trading."""
    cfg = tmp_path / "core.json"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        load_universe(cfg)
    assert message in str(exc.value)


def test_every_core_etf_is_actually_downloaded():
    """configs/core.json decides what is held, configs/data.json what is fetched.
    Two files, one truth: a name that is allocated but never fetched produces a
    book that silently cannot buy it."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    core = json.loads((root / "configs" / "core.json").read_text(encoding="utf-8"))
    data = json.loads((root / "configs" / "data.json").read_text(encoding="utf-8"))
    held = {core["defensive"]["code"]} | {r["code"] for r in core["risk"]}
    missing = sorted(held - set(data["etf_codes"]))
    assert not missing, f"allocated but never fetched: {missing}"


def test_execute_refuses_a_plan_the_kernel_generated_itself():
    """The last place a code-authored trade can still sneak in.

    `plan(day)` with no targets is how the rule shows its work. If `execute`
    accepted that object, a scheduler could do `book.execute(book.plan(day))`
    and the desk would be running a strategy again, whatever the API allows.
    """
    book = CoreBook(FundStore(make_prices()), MemoryPaper())
    day = book.last_day()
    rule_plan = book.plan(day)
    assert rule_plan["regime"]["source"].startswith("registered rule")

    with pytest.raises(RuntimeError) as exc:
        book.execute(rule_plan)
    assert "does not execute its own output" in str(exc.value)
    assert book.state().holdings == {}, "nothing may be booked by the refusal"

    # The same weights, submitted as the caller's, do execute.
    targets = dict(rule_plan["targets"])
    booked = book.execute(book.plan(day, targets=targets))
    assert booked["applied"] is True


# ------------------------------------------------------- raw prices and atomic execution
def test_adjusted_close_is_for_signals_not_cash_or_marks(tmp_path):
    factor = 2.75
    book = CoreBook(FundStore(make_prices(), adj_factor=factor),
                    PaperStore(tmp_path / "paper.sqlite"))
    day = book.last_day()
    symbol = RISK_ETFS[0]
    raw = float(book.raw_prices().loc[day, symbol])
    assert book.prices().loc[day, symbol] == pytest.approx(raw * factor)

    weights = book.target_weights(day)
    assert weights["_meta"]["benchmark"] == pytest.approx(round(raw * factor, 4))
    result = book.execute(book.plan(day, targets={symbol: 0.4}))
    order = next(o for o in result["orders"] if o["symbol"] == symbol)
    assert order["price"] == pytest.approx(round(raw, 4))

    stored = next(t for t in book.paper.core_trades(limit=100)
                  if t["symbol"] == symbol)
    assert float(stored["price"]) == pytest.approx(round(raw, 4))
    marked = book.mark(day)
    expected_invested = sum(q * float(book.raw_prices().loc[day, s])
                            for s, q in marked["holdings"].items())
    assert marked["invested"] == pytest.approx(round(expected_invested, 2))
    assert marked["nav"] == pytest.approx(round(marked["cash"] + expected_invested, 2))


def test_cash_is_conserved_when_adjustment_factor_is_not_one(tmp_path):
    book = CoreBook(FundStore(make_prices(), adj_factor=3.25),
                    PaperStore(tmp_path / "paper.sqlite"))
    result = book.execute(book.plan(book.last_day(), targets={RISK_ETFS[0]: 0.4}))
    fees = sum(float(o["fee"]) for o in result["orders"])
    assert result["cash"] + result["invested"] + fees == pytest.approx(5000.0, abs=0.02)
    assert result["nav"] == pytest.approx(5000.0 - fees, abs=0.02)
    assert book.state().cash == pytest.approx(result["cash"])


def test_crash_rolls_back_whole_plan_and_retry_does_not_duplicate(tmp_path):
    db = tmp_path / "paper.sqlite"
    book = CoreBook(FundStore(make_prices(), adj_factor=2.0), PaperStore(db))
    plan = book.plan(book.last_day(), plan_id="crash-retry", targets={
        RISK_ETFS[0]: 0.2, RISK_ETFS[1]: 0.2,
    })
    assert len(plan["orders"]) >= 2
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TRIGGER fail_core_nav BEFORE INSERT ON core_nav
                        BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")

    with pytest.raises(sqlite3.DatabaseError, match="simulated crash"):
        book.execute(plan)
    assert book.paper.core_trades(limit=100) == []
    assert book.paper.core_nav_history(limit=100) == []

    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER fail_core_nav")
    assert book.execute(plan)["applied"] is True
    assert book.execute(plan)["applied"] is False
    trades = book.paper.core_trades(limit=100)
    assert len(trades) == len(plan["orders"])
    assert len({t["id"] for t in trades}) == len(plan["orders"])
    assert len(book.paper.core_nav_history(limit=100)) == 1


def test_all_zero_targets_remain_an_executable_cash_plan(tmp_path):
    book = CoreBook(FundStore(make_prices()), PaperStore(tmp_path / "paper.sqlite"))
    plan = book.plan(book.last_day(), targets={RISK_ETFS[0]: 0.0})
    assert plan["targets"] == {} and plan["orders"] == []
    assert book.execute(plan)["nav"] == 5000.0
    assert book.execute(plan)["applied"] is False


def test_plan_id_cannot_be_reused_for_different_content(tmp_path):
    book = CoreBook(FundStore(make_prices()), PaperStore(tmp_path / "paper.sqlite"))
    day = book.last_day()
    first = book.plan(day, plan_id="unique", targets={RISK_ETFS[0]: 0.3})
    other = book.plan(day, plan_id="unique", targets={RISK_ETFS[1]: 0.3})
    book.execute(first)
    with pytest.raises(ValueError, match="different content"):
        book.execute(other)
    assert {t["symbol"] for t in book.paper.core_trades(limit=100)} == {RISK_ETFS[0]}


def test_stale_plan_is_refused_even_with_force(tmp_path):
    book = CoreBook(FundStore(make_prices()), PaperStore(tmp_path / "paper.sqlite"))
    day = book.last_day()
    stale = book.plan(day, plan_id="stale", targets={RISK_ETFS[0]: 0.4})
    current = book.plan(day, plan_id="current", targets={RISK_ETFS[1]: 0.4})
    assert book.execute(current)["applied"] is True

    with pytest.raises(RuntimeError, match="stale plan"):
        book.execute(stale, force=True)
    reasons = {t["reason"] for t in book.paper.core_trades(limit=100)}
    assert reasons == {"current"}


def test_force_resumes_only_an_exact_legacy_partial_plan(tmp_path):
    book = CoreBook(FundStore(make_prices()), PaperStore(tmp_path / "paper.sqlite"))
    plan = book.plan(book.last_day(), plan_id="legacy-partial", targets={
        RISK_ETFS[0]: 0.2, RISK_ETFS[1]: 0.2,
    })
    assert len(plan["orders"]) >= 2
    first = plan["orders"][0]
    book.paper.core_record_trade(plan["trade_date"], first["symbol"], first["side"],
                                 first["qty"], str(first["price"]), str(first["fee"]),
                                 plan["plan_id"])

    with pytest.raises(RuntimeError, match="incomplete legacy"):
        book.execute(plan)
    recovered = book.execute(plan, force=True)
    assert recovered["applied"] is True and recovered["recovered"] is True
    trades = list(reversed(book.paper.core_trades(limit=100)))
    assert len(trades) == len(plan["orders"])
    assert [t["symbol"] for t in trades] == [o["symbol"] for o in plan["orders"]]
    assert book.execute(plan)["applied"] is False
    assert len(book.paper.core_trades(limit=100)) == len(plan["orders"])


def test_state_aggregates_beyond_ten_thousand_trades(tmp_path):
    db = tmp_path / "paper.sqlite"
    paper = PaperStore(db)
    day = "20250101"
    old = (day, RISK_ETFS[0], "BUY", 100, "1.0", "5.0", "oldest")
    # The newest 10,000 rows net to zero. Dropping the oldest row used to make
    # the only actual position disappear from the reconstructed account.
    recent = [(day, RISK_ETFS[1], "BUY" if i % 2 == 0 else "SELL", 100,
               "1.0", "5.0", f"recent-{i}") for i in range(10_000)]
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO core_trade(trade_date, symbol, side, qty, price, fee, reason) "
            "VALUES (?,?,?,?,?,?,?)", [old, *recent])

    book = CoreBook(FundStore(make_prices()), paper)
    assert book.state().holdings == {RISK_ETFS[0]: 100}


def test_concurrent_memory_books_share_the_receipt_check():
    paper = MemoryPaper()
    store = FundStore(make_prices())
    first = CoreBook(store, paper)
    second = CoreBook(store, paper)
    plan = first.plan(first.last_day(), plan_id="memory-concurrent",
                      targets={RISK_ETFS[0]: 0.4})
    barrier = Barrier(2)

    def run(book):
        barrier.wait(timeout=30)
        return book.execute(plan)["applied"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        applied = list(pool.map(run, (first, second)))
    assert sorted(applied) == [False, True]
    assert len(paper.trades) == len(plan["orders"])
    assert len(paper.nav) == 1


def test_concurrent_execution_is_unique_at_the_database(tmp_path):
    db = tmp_path / "paper.sqlite"
    store = FundStore(make_prices())
    first = CoreBook(store, PaperStore(db))
    second = CoreBook(store, PaperStore(db))
    plan = first.plan(first.last_day(), plan_id="concurrent", targets={RISK_ETFS[0]: 0.4})
    barrier = Barrier(2)

    def run(book):
        barrier.wait(timeout=30)
        return book.execute(plan)["applied"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        applied = list(pool.map(run, (first, second)))
    assert sorted(applied) == [False, True]
    assert len(first.paper.core_trades(limit=100)) == len(plan["orders"])
    assert len(first.paper.core_nav_history(limit=100)) == 1
    expected_qty = sum(o["qty"] if o["side"] == "BUY" else -o["qty"]
                       for o in plan["orders"] if o["symbol"] == RISK_ETFS[0])
    assert first.state().holdings[RISK_ETFS[0]] == expected_qty
    with sqlite3.connect(db) as conn:
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_core_plan_executed_unique'").fetchone()
    assert index is not None and "UNIQUE INDEX" in index[0]


def test_the_regime_is_read_from_the_ledger_not_from_stored_weights():
    """Holding a risk leg is a fact about the book, not about a rounded weight.

    The stored NAV weights are rounded to four decimals and filtered to ``> 0``
    before they are written, so a dust position disappears from the dict while
    still being held. On the first session after a rebalance there is no NAV row
    at all, and then every book read as defensive — including one that had just
    bought the risk leg.
    """
    paper = MemoryPaper()
    paper.core_record_trade("20260105", RISK_ETFS[0], "BUY", 100, 4.0, 5.0, "rebalance")
    book = CoreBook(FundStore(make_prices()), paper)

    assert book.state().holdings[RISK_ETFS[0]] == 100
    assert book.state().last_regime == "risk", "no NAV row yet, but the leg is held"


def test_a_weight_that_rounded_to_zero_does_not_flip_the_regime():
    paper = MemoryPaper()
    paper.core_record_trade("20260105", RISK_ETFS[0], "BUY", 100, 4.0, 5.0, "rebalance")
    paper.core_record_nav("20260105", "5000.00", "4600.00", "400.00", {})
    book = CoreBook(FundStore(make_prices()), paper)

    assert book.state().last_regime == "risk"


def test_a_cash_only_book_is_defensive():
    paper = MemoryPaper()
    paper.core_record_nav("20260105", "5000.00", "5000.00", "0.00", {})
    book = CoreBook(FundStore(make_prices()), paper)

    assert book.state().last_regime == "defensive"


def test_the_core_book_charges_the_schedule_in_costs_yaml():
    """One account, one commission. The core book used to carry its own copy.

    ``CoreConfig`` had ``fee_rate = 0.00025`` and ``fee_min = 5.0`` — the same
    numbers as ``costs.yaml``, frozen in code. A broker change would have moved
    the stock sleeve and left the core sleeve simulating the old one, which is
    the kind of split that only shows up as an unexplainable NAV drift.
    """
    from service.kernel.costs import CostModel
    from service.kernel.models import D

    book = CoreBook(FundStore(make_prices()), MemoryPaper())
    model = CostModel()
    for notional in (1200.0, 5000.0, 50_000.0):
        expected = float(model.commission(D(str(notional))))
        assert book._fee(notional) == pytest.approx(expected, abs=0.01)


def test_the_core_config_reads_its_policy_from_params():
    from service import settings
    from service.kernel.units import SHARES_PER_LOT

    cfg = CoreConfig()
    assert cfg.vol_window == settings.integer("core.vol_window")
    assert cfg.vol_target == settings.num("core.vol_target")
    assert cfg.max_weight == settings.num("core.max_weight")
    assert cfg.ma_window == settings.integer("core.ma_window")
    assert cfg.min_trade_cny == settings.num("core.min_trade_cny")
    assert cfg.lot == SHARES_PER_LOT


def test_the_core_config_is_read_per_instance_not_captured_at_import(monkeypatch):
    """Same contract as the other config dataclasses: no import-time snapshot."""
    from service import settings

    monkeypatch.setitem(settings.load(), "core.vol_target", 0.09)
    assert CoreConfig().vol_target == 0.09


def _alive(monkeypatch):
    """Force the sleeve into its pre-retirement state.

    `configs/core.json` now carries `retired: true`, so a test that asserts on
    cash behaviour has to say which world it is in. Making that explicit is the
    point: the default flipped to closed, and a test that silently inherited the
    default would be testing the tombstone, not the mechanism.
    """
    import dataclasses

    import service.trading.core as core_mod

    monkeypatch.setattr(core_mod, "UNIVERSE_CFG",
                        dataclasses.replace(core_mod.UNIVERSE_CFG, retired=False))


def test_a_cash_book_that_cannot_move_says_so(monkeypatch):
    """A flat 0.00% with 100% cash used to read as "the rule chose cash".

    It did not. On 2026-10-30 the defensive leg is 511010 at 141.12, so one lot
    costs 14,112 against a 5,007 book — 2.8x. The rule asked for 100% of an
    instrument this account cannot buy, and the sleeve reported six sessions of
    0.00% as if that were an opinion. `state` separates the two cases and
    `required_nav` states the size at which the target becomes executable.
    """
    _alive(monkeypatch)
    prices = make_prices(slope=-0.0015)   # trend down -> the rule asks for bonds
    prices.loc[prices["ts_code"] == DEFENSIVE_ETF, "close"] = 147.0
    store = FundStore(prices)
    book = CoreBook(store, MemoryPaper())

    status = book.status(book.last_day())

    assert status["state"] == "cash_unexecutable"
    row = status["executable"][DEFENSIVE_ETF]
    assert row["lot_cost"] == pytest.approx(14_700.0)
    assert row["share_of_nav"] > 2.5
    assert row["required_nav"] == pytest.approx(14_700.0)
    assert "买不起" in status["why"]


def test_an_affordable_defensive_leg_is_not_called_unexecutable(monkeypatch):
    """The label must not become a permanent excuse for any cash book."""
    _alive(monkeypatch)
    store = FundStore(make_prices())
    book = CoreBook(store, MemoryPaper())

    status = book.status(book.last_day())

    assert status["state"] in {"cash_by_rule", "invested"}
    assert status["why"] is None or "买不起" not in status["why"]


def test_a_retired_sleeve_reports_closed_not_flat():
    """Closed is a fact about the product, not a reading of the tape.

    After 2026-11-01 the sleeve is retired in config, so `state` must say so even
    though every other number (targets, lot cost, required NAV) still computes.
    Keeping those readable is deliberate: the reason is checkable, not remembered.
    """
    store = FundStore(make_prices(slope=-0.0015))
    book = CoreBook(store, MemoryPaper())

    status = book.status(book.last_day())

    assert status["state"] == "retired"
    assert "2026-11-01" in status["why"]
    assert status["target_weights"], "退休不等于清空标的，数字仍要可查"
    assert status["executable"][DEFENSIVE_ETF]["required_nav"] > 0
