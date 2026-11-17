"""内核关键路径微基准：submit / match / apply_fill / snapshot / deepcopy / equity。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/bench/kernel_bench.py
"""
import json
import time
from copy import deepcopy
from datetime import datetime
from decimal import Decimal as Dec

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Bar,
    Instrument,
    Intent,
    OrderType,
    Position,
    Side,
)

CAL = TradingCalendar.always()
MATCH = BarExecutionModel()
TS = datetime(2026, 11, 3, 9, 30, tzinfo=SHANGHAI)


def make_bar(sym, px="10.00", vol=2_000_000):
    p = Dec(px)
    return Bar(sym, TS, p, p, p, p, p, vol, p * Dec(vol),
               (p * Dec("1.1")).quantize(Dec("0.01")),
               (p * Dec("0.9")).quantize(Dec("0.01")), prior_volume=vol)


def timed(label, fn, n, warmup=20):
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    print(f"{label:<38} {dt / n * 1e6:9.1f} us/op   ({n} ops)")
    return dt / n


def build_history(n_orders):
    led = Ledger.fresh("50000000")
    inst = Instrument("600000.SH")
    bar = make_bar("600000.SH")
    for i in range(n_orders):
        o = led.submit(Intent("600000.SH", Side.BUY, qty=100,
                              order_type=OrderType.LIMIT, limit_price=Dec("10.00"),
                              client_order_id=f"hist-{i}"),
                       TS, inst, bar, CAL)
        r = MATCH.match(o, bar, inst, TS, CAL)
        if r.fill:
            led.apply_fill(o, r.fill)
    return led, inst, bar


def main():
    led, inst, bar = build_history(0)
    print("== 单笔往返 ==")
    seq = [0]
    def one_ticket():
        seq[0] += 1
        o = led.submit(Intent("600000.SH", Side.BUY, qty=100,
                              order_type=OrderType.LIMIT, limit_price=Dec("10.00"),
                              client_order_id=f"bench-{seq[0]}"),
                       TS, inst, bar, CAL)
        r = MATCH.match(o, bar, inst, TS, CAL)
        if o.status.value != "REJECTED" and r.fill:
            led.apply_fill(o, r.fill)
            if o.remaining_qty:
                led.cancel(o.order_id, TS, CAL)
    timed("submit+match+apply_fill (单笔)", one_ticket, 2000)

    print("\n== 历史增长后的固定动作 ==")
    for n in (10, 100, 500, 2000, 5000):
        big, inst2, bar2 = build_history(n)
        # Bind the loop variable. These are called inside this iteration, so the
        # late-binding closure would work today; every other lambda in this file
        # already binds, and the two spellings side by side read as a difference
        # that is not there.
        snap = lambda big=big: big.snapshot()
        dc = lambda big=big: deepcopy(big)
        payload = json.dumps(snap(), ensure_ascii=False)
        print(f"-- {n} 笔历史，snapshot {len(payload)} bytes")
        timed("  snapshot()", snap, 200)
        timed("  deepcopy(ledger)", dc, 200)

    print("\n== 持仓数增长 ==")
    for npos in (5, 20, 100):
        acct = Ledger.fresh("50000000").account
        for i in range(npos):
            acct.positions[f"S{i:04d}"] = Position(
                symbol=f"S{i:04d}", total_qty=100, available_qty=100,
                avg_cost=Dec(10), last_price=Dec(11))
        prices = {s: Dec(12) for s in acct.positions}
        timed(f"  equity({npos} 持仓)", lambda a=acct, p=prices: a.equity(p), 2000)


if __name__ == "__main__":
    main()
