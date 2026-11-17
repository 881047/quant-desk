"""记一笔成交：费用要算对，因为成本价是止损的锚。"""

from __future__ import annotations

import pytest

from service.trading import fees, manual
from service.trading.external import ExternalStore


def _store(tmp_path) -> ExternalStore:
    return ExternalStore(tmp_path / "external.sqlite")


def test_commission_has_a_floor_and_stamp_duty_is_sell_only():
    buy = fees.compute("BUY", 8.12, 200)
    assert buy["commission"] == 5.00, "1624 元 × 万 2.5 = 0.41，低于最低 5 元"
    assert buy["stamp_duty"] == 0.0
    assert buy["total"] == pytest.approx(5.02)

    sell = fees.compute("SELL", 8.30, 200)
    assert sell["stamp_duty"] == pytest.approx(0.83, abs=0.01)
    assert sell["total"] == pytest.approx(5.85, abs=0.01)


def test_a_large_order_pays_the_rate_not_the_floor():
    big = fees.compute("BUY", 100.0, 100_000)
    assert big["commission"] == pytest.approx(2500.0)


@pytest.mark.parametrize("key", list(fees.KEYS))
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01])
def test_fee_config_rejects_non_finite_and_negative_rates(key, bad):
    cfg = fees.schedule()
    cfg[key] = bad
    with pytest.raises(ValueError, match="finite and non-negative"):
        fees.compute("BUY", 10.0, 100, config=cfg)


def test_rate_note_uses_the_actual_fee_configuration():
    cfg = {"commission_rate": 0.0003, "min_commission": 3.0,
           "stamp_duty_sell": 0.001, "transfer_fee": 0.00002}
    out = fees.compute("SELL", 10.0, 1000, config=cfg)
    assert "佣金 3.00‱" in out["rate_note"]
    assert "最低 3 元" in out["rate_note"]
    assert "印花税 0.1%" in out["rate_note"]
    assert "过户费 0.002%" in out["rate_note"]


def test_buy_updates_the_position_cost_including_fees(tmp_path):
    store = _store(tmp_path)
    result = manual.apply_trade(store, {"symbol": "000700", "side": "BUY",
                                        "qty": 100, "price": 13.07,
                                        "trade_date": "20261027"})
    assert result["ok"] is True
    held = manual.current_holdings(store)["000700.SZ"]
    assert held["qty"] == 100
    # (13.07×100 + 5.01) / 100 = 13.1201
    assert held["cost"] == pytest.approx(13.1201, abs=1e-3)


def test_selling_more_than_held_is_refused_with_the_real_quantity(tmp_path):
    store = _store(tmp_path)
    manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                               "price": 13.07, "trade_date": "20261027"})
    result = manual.apply_trade(store, {"symbol": "000700", "side": "SELL", "qty": 200,
                                        "price": 13.5, "trade_date": "20261027"})
    assert result["ok"] is False
    assert "100" in result["message"]
    assert manual.current_holdings(store)["000700.SZ"]["qty"] == 100, "被拒绝的卖出不能改持仓"
    assert len(store.trades()) == 1, "被拒绝的卖出也不能留下成交流水"


def test_a_full_sell_clears_the_row_and_books_realised_pnl(tmp_path):
    store = _store(tmp_path)
    manual.apply_trade(store, {"symbol": "000590", "side": "BUY", "qty": 200,
                               "price": 8.12, "trade_date": "20261027"})
    result = manual.apply_trade(store, {"symbol": "000590", "side": "SELL", "qty": 200,
                                        "price": 8.30, "trade_date": "20261027"})
    assert result["ok"] is True
    assert manual.current_holdings(store) == {}
    assert result["holding"]["qty"] == 0
    # (8.30 - 8.145) × 200 - 5.85 ≈ 25.15
    assert result["realized"] > 0


def test_cash_moves_only_when_the_day_already_has_numbers(tmp_path):
    store = _store(tmp_path)
    manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                               "price": 13.07, "trade_date": "20261027"})
    assert store.days(1) == [], "没有期初资金时不要去编一个现金余额"

    plan = manual.prepare(as_of="20261027", positions=[], total="5000",
                          cash="5000", market_value="0")
    manual.persist(store, plan)
    result = manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                                        "price": 13.08, "trade_date": "20261027"})
    assert result["cash"] == pytest.approx(5000 - 1308 - result["fee_total"], abs=0.01)


def test_an_explicit_fee_overrides_the_calculation(tmp_path):
    store = _store(tmp_path)
    result = manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                                        "price": 13.07, "fee": 5.0,
                                        "trade_date": "20261027"})
    assert result["fee_total"] == 5.0


def test_a_fill_preserves_other_positions_market_values(tmp_path):
    store = _store(tmp_path)
    store.replace_snapshot("20261027", [
        {"symbol": "000700.SZ", "qty": 100, "cost": 13.07, "market_value": 1307},
        {"symbol": "002966.SZ", "qty": 200, "cost": 9.29, "market_value": 1872},
    ])
    store.save_day("20261027", cash=5000, market_value=3179, equity=8179)

    result = manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                                        "price": 13.08, "trade_date": "20261027"})
    other = next(row for row in result["rows"] if row["symbol"] == "002966.SZ")
    assert other["market_value"] == 1872
    assert store.snapshot("20261027")["002966.SZ"]["market_value"] == 1872
    day = store.days(1)[0]
    assert day["market_value"] == pytest.approx(13.08 * 200 + 1872)
    assert day["equity"] == pytest.approx(day["cash"] + day["market_value"])


def test_a_duplicate_fill_is_a_complete_no_op(tmp_path):
    store = _store(tmp_path)
    row = {"symbol": "000700", "side": "BUY", "qty": 100,
           "price": 13.07, "trade_date": "20261027"}
    assert manual.apply_trade(store, row)["duplicate"] is False
    store.save_day("20261027", cash=5000, market_value=1307, equity=6307)
    before = (store.trades(), store.snapshot("20261027"),
              store.days(), store.imports())

    result = manual.apply_trade(store, row)
    assert result["ok"] is True
    assert result["duplicate"] is True
    assert "未重复更新" in result["message"]
    after = (store.trades(), store.snapshot("20261027"),
             store.days(), store.imports())
    assert after == before


def test_concurrent_duplicate_fills_are_serialized_by_sqlite(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = tmp_path / "external.sqlite"
    stores = [ExternalStore(db), ExternalStore(db)]
    barrier = Barrier(2)
    row = {"symbol": "000700", "side": "BUY", "qty": 100,
           "price": 13.07, "trade_date": "20261027"}

    def apply(store):
        barrier.wait(timeout=30)
        return manual.apply_trade(store, row)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, stores))
    assert sorted(result["inserted"] for result in results) == [0, 1]
    assert len(stores[0].trades()) == 1
    assert stores[0].snapshot("20261027")["000700.SZ"]["qty"] == 100
    assert len(stores[0].imports()) == 1


def test_fill_snapshot_cash_and_log_roll_back_together(tmp_path, monkeypatch):
    store = _store(tmp_path)
    manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                               "price": 13.07, "trade_date": "20261027"})
    store.save_day("20261027", cash=5000, market_value=1307, equity=6307)
    before = (store.trades(), store.snapshot("20261027"),
              store.days(), store.imports())

    def fail_import_log(*_args, **_kwargs):
        raise RuntimeError("simulated import-log failure")

    monkeypatch.setattr(store, "log_import", fail_import_log)
    with pytest.raises(RuntimeError, match="simulated"):
        manual.apply_trade(store, {"symbol": "000700", "side": "BUY", "qty": 100,
                                   "price": 13.08, "trade_date": "20261027"})
    after = (store.trades(), store.snapshot("20261027"),
             store.days(), store.imports())
    assert after == before
