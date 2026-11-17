"""对抗性探针：部分成交、多挂单、结算、现金守恒。

重点验证 ``apply_fill`` 的释放/扣减在部分成交下不会漏钱或多钱。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/bench/kernel_fuzz_pools.py 3
"""
import random
import sys
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
    OrderStatus,
    OrderType,
    Side,
)

CAL = TradingCalendar.always()
MATCH = BarExecutionModel()
TS = datetime(2026, 11, 3, 9, 30, tzinfo=SHANGHAI)
OPENING = Dec(100000)


def make_bar(sym, px, ratio=1.0, vol=1_000_000):
    p = Dec(px)
    return Bar(sym, TS, p, p, p, p, p, vol, p * Dec(vol),
               (p * Dec("1.1")).quantize(Dec("0.01")),
               (p * Dec("0.9")).quantize(Dec("0.01")),
               fill_ratio=ratio, prior_volume=vol)


def run(seed, trials=150):
    random.seed(seed)
    problems = []
    for t in range(trials):
        led = Ledger.fresh(str(OPENING))
        inst = Instrument("600000.SH")
        bought = Dec(0)
        sold = Dec(0)
        fees = Dec(0)
        live = []
        for step in range(10):
            action = random.choice(["buy", "buy", "sell", "cancel", "settle"])
            if action == "settle":
                led.settle()
                continue
            if action == "cancel" and live:
                oid = random.choice(live)
                led.cancel(oid, TS, CAL)
                continue
            side = Side.BUY if action == "buy" else Side.SELL
            px = random.choice(["10.00", "10.01", "9.99"])
            ratio = random.choice([1.0, 1.0, 0.6, 0.3])
            b = make_bar("600000.SH", px, ratio)
            ot = random.choice([OrderType.MARKET, OrderType.LIMIT])
            lim = Dec(px) if ot == OrderType.LIMIT else None
            qty = random.choice([100, 200, 300, 500])
            o = led.submit(Intent("600000.SH", side, qty=qty, order_type=ot,
                                  limit_price=lim,
                                  client_order_id=f"{t}-{step}"),
                           TS, inst, b, CAL)
            if o.status is OrderStatus.REJECTED:
                continue
            r = MATCH.match(o, b, inst, TS, CAL)
            if r.fill is None:
                led.cancel(o.order_id, TS, CAL)
                continue
            try:
                led.apply_fill(o, r.fill)
            except ValueError as exc:
                problems.append((t, step, "apply_fill raised", str(exc)))
                break
            if o.remaining_qty:
                live.append(o.order_id)
            f = r.fill
            if f.side is Side.BUY:
                bought += f.price * f.qty
            else:
                sold += f.price * f.qty
            fees += f.fees
        else:
            pass
        # 收尾：撤掉一切挂单，冻结应归零
        for oid in live:
            led.cancel(oid, TS, CAL)
        acct = led.account
        if acct.cash_frozen != 0:
            problems.append((t, "end", "frozen!=0", str(acct.cash_frozen)))
        expected = OPENING - bought + sold - fees
        actual = acct.cash_available + acct.cash_frozen
        if abs(actual - expected) > Dec("0.02"):
            problems.append((t, "end", "cash drift",
                             f"actual={actual} expected={expected} diff={actual-expected}"))
        if acct.cash_withdrawable > acct.cash_available + Dec("0.001"):
            problems.append((t, "end", "withdrawable>available",
                             str(acct.cash_withdrawable), str(acct.cash_available)))
        for s, p in acct.positions.items():
            if not p.is_consistent():
                problems.append((t, s, "identity", p.total_qty, p.available_qty,
                                 p.frozen_qty, p.locked_qty))
    return problems


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    probs = run(seed)
    print(f"seed={seed} problems={len(probs)}")
    for p in probs[:10]:
        print(p)
