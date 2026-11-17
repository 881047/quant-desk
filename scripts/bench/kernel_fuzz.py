"""随机化不变量探针：现金守恒、股数恒等式、冻结与挂单一致。

不写入仓库数据，只在内存里跑。

用法：PYTHONPATH=. .venv/Scripts/python.exe scripts/bench/kernel_fuzz.py 7
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

cal = TradingCalendar.always()
matcher = BarExecutionModel()


def make_bar(sym, ts, px, prev_vol=2_000_000):
    p = Dec(str(px))
    return Bar(
        sym, ts, p,
        (p * Dec("1.02")).quantize(Dec("0.01")),
        (p * Dec("0.98")).quantize(Dec("0.01")),
        p, p, 2_000_000, p * Dec(2_000_000),
        (p * Dec("1.1")).quantize(Dec("0.01")),
        (p * Dec("0.9")).quantize(Dec("0.01")),
        prior_volume=prev_vol,
    )


def run(seed: int, trials: int = 200, steps: int = 12):
    random.seed(seed)
    viol = []
    for trial in range(trials):
        led = Ledger.fresh("50000")
        insts = {s: Instrument(s, limit_pct=Dec("0.10"))
                 for s in ("600000.SH", "000001.SZ")}
        ts = datetime(2026, 11, 3, 9, 30, tzinfo=SHANGHAI)
        for step in range(steps):
            sym = random.choice(list(insts))
            side = random.choice([Side.BUY, Side.SELL])
            px = Dec(str(random.choice([9.5, 10.0, 10.5])))
            b = make_bar(sym, ts, px)
            ot = random.choice([OrderType.MARKET, OrderType.LIMIT])
            mult = random.choice(["0.99", "1.0", "1.01"])
            lim = ((px * Dec(mult)).quantize(Dec("0.01"))
                   if ot == OrderType.LIMIT else None)
            intent = Intent(sym, side, qty=random.choice([100, 200, 300]),
                            order_type=ot, limit_price=lim)
            o = led.submit(intent, ts, insts[sym], b, cal)
            if o.status is OrderStatus.REJECTED:
                continue
            r = matcher.match(o, b, insts[sym], ts, cal)
            if r.fill:
                led.apply_fill(o, r.fill)
                if o.remaining_qty:
                    led.cancel(o.order_id, ts, cal)
            else:
                led.cancel(o.order_id, ts, cal)
            for s, p in led.account.positions.items():
                if not p.is_consistent():
                    viol.append((trial, step, s, "identity", p.total_qty,
                                 p.available_qty, p.frozen_qty, p.locked_qty))
            acct = led.account
            if acct.cash_available < 0 or acct.cash_frozen < 0:
                viol.append((trial, step, "cash", "negative",
                             str(acct.cash_available), str(acct.cash_frozen)))
            if acct.cash_withdrawable > acct.cash_available + Dec("0.001"):
                viol.append((trial, step, "cash", "withdrawable>available",
                             str(acct.cash_withdrawable), str(acct.cash_available)))
            reserved = sum((o.frozen_cash for o in led.oms.open_orders()
                            if o.side is Side.BUY), Dec(0))
            if reserved != acct.cash_frozen:
                viol.append((trial, step, "cash", "frozen!=reserved",
                             str(reserved), str(acct.cash_frozen)))
            share_res = {}
            for o in led.oms.open_orders():
                if o.side is Side.SELL:
                    share_res[o.symbol] = share_res.get(o.symbol, 0) + o.frozen_shares
            for s, p in acct.positions.items():
                if p.frozen_qty != share_res.get(s, 0):
                    viol.append((trial, step, s, "frozen_qty!=orders",
                                 p.frozen_qty, share_res.get(s, 0)))
    return viol


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    v = run(seed)
    print(f"seed={seed} violations={len(v)}")
    for row in v[:15]:
        print(row)
