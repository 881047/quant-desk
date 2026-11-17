"""Ledger facade: the only public writer of account state.

It wraps OrderManager so callers cannot mutate cash/positions directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .costs import CostModel
from .models import (
    Account,
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    Order,
    OrderType,
    Position,
    Side,
    money,
)
from .oms import SNAPSHOT_VERSION, CancelResult, OrderManager, _session_key


def _configured_cash() -> Decimal:
    """Opening capital, from configs/params.yaml (``risk.initial_cash``)."""
    from service import settings

    return D(f"{settings.num('risk.initial_cash'):.2f}")


@dataclass
class Ledger:
    account: Account
    oms: OrderManager

    @property
    def fills(self) -> list[Fill]:
        """Fill history, from the one container that owns it.

        This used to be a second list beside ``oms.fills``: ``apply_fill``
        appended to both, ``from_snapshot`` filled both, and only one test read
        this one. Two containers of the same objects is two chances to diverge;
        the property keeps that reader working without keeping the copy.
        """
        return list(self.oms.fills.values())

    @classmethod
    def fresh(cls, initial_cash: str | None = None,
              cost_model: CostModel | None = None) -> Ledger:
        """A new account. ``None`` means "the configured account size".

        The default used to be the literal ``"5000"``, while ``risk.initial_cash``
        sat in ``params.yaml`` unread and three ``os.getenv("INITIAL_CASH",
        "5000")`` fallbacks repeated it. Resizing the account meant editing four
        places and hoping.
        """
        cash = D(initial_cash) if initial_cash is not None else _configured_cash()
        return cls(
            account=Account(
                cash_available=cash,
                cash_withdrawable=cash,
                peak_equity=cash,
            ),
            oms=OrderManager(cost_model),
        )

    def submit(
        self,
        intent: Intent,
        ts: datetime,
        instrument: Instrument,
        bar: Bar,
        calendar,
    ) -> Order:
        return self.oms.submit(intent, ts, self.account, instrument, bar, calendar)

    def cancel(self, order_id: str, ts: datetime, calendar,
               reason: str = "CANCELLED") -> CancelResult:
        return self.oms.cancel(order_id, ts, self.account, calendar, reason=reason)

    def expire_day_orders(self, ts: datetime) -> list[Order]:
        """Close of session: nothing may stay open into the next day."""
        return self.oms.expire_open_orders(ts, self.account)

    def expire_ioc(self, ts: datetime) -> list[Order]:
        """A market order that missed its own bar must not fill on the next one."""
        expired: list[Order] = []
        for order in self.oms.open_orders():
            if order.order_type != OrderType.MARKET:
                continue
            result = self.oms.expire(order.order_id, ts, self.account,
                                     reason="IOC_NO_FILL")
            if result.success:
                expired.append(order)
        return expired

    def apply_fill(self, order: Order, fill: Fill) -> bool:
        return self.oms.apply_fill(order, fill, self.account)

    def settle(self, session: str | date | None = None) -> None:
        self.oms.settle(self.account, session)

    def snapshot(self) -> dict:
        return self.oms.snapshot(self.account)

    def reconcile(self, expected: dict) -> list[str]:
        """Compare both directions without creating missing positions."""
        diffs: list[str] = []
        for field, default in (
            ("cash_frozen", "0"),
            ("cash_withdrawable", "0"),
            ("realized_pnl", "0"),
            ("fees_paid", "0"),
            ("dividend_income", "0"),
        ):
            actual = getattr(self.account, field)
            wanted = money(D(expected.get(field, default)))
            if actual != wanted:
                diffs.append(f"{field} {actual} != {wanted}")
        cash = money(D(expected.get("cash_available", self.account.cash_available)))
        if cash != self.account.cash_available:
            diffs.append(f"cash_available {self.account.cash_available} != {cash}")
        if "peak_equity" in expected:
            peak = D(expected["peak_equity"])
            if self.account.peak_equity != peak:
                diffs.append(f"peak_equity {self.account.peak_equity} != {peak}")

        expected_positions = expected.get("positions", {})
        for symbol in sorted(set(self.account.positions) | set(expected_positions)):
            pos = self.account.positions.get(symbol)
            row = expected_positions.get(symbol)
            if pos is None:
                diffs.append(f"{symbol} missing position")
                continue
            if row is None:
                diffs.append(f"{symbol} unexpected position")
                continue
            for field in ("total_qty", "available_qty", "frozen_qty"):
                actual = getattr(pos, field)
                wanted = int(row.get(field, 0))
                if actual != wanted:
                    diffs.append(f"{symbol} {field} {actual} != {wanted}")
            # Only compared when the snapshot states it: an older snapshot has no
            # ``locked_qty`` and comparing against an implied zero would report a
            # difference for every position bought before the upgrade.
            if "locked_qty" in row:
                wanted = int(row["locked_qty"])
                if pos.locked_qty != wanted:
                    diffs.append(f"{symbol} locked_qty {pos.locked_qty} != {wanted}")
            if not pos.is_consistent():
                diffs.append(
                    f"{symbol} shares do not add up: total {pos.total_qty} != "
                    f"available {pos.available_qty} + frozen {pos.frozen_qty} "
                    f"+ locked {pos.locked_qty}"
                )
            for field, default in (
                ("avg_cost", "0"),
                ("last_price", "0"),
                ("realized_pnl", "0"),
            ):
                actual = getattr(pos, field)
                wanted = D(row.get(field, default))
                if actual != wanted:
                    diffs.append(f"{symbol} {field} {actual} != {wanted}")
        return diffs

    @classmethod
    def from_snapshot(cls, snapshot: dict, cost_model: CostModel | None = None) -> Ledger:
        # Version 1 is the unlabelled shape: ``locked_qty`` may be absent and is
        # then derived from the share identity below, and ``order_records`` may
        # be absent on a book that never had a live order. From v2 on, the writer
        # says what it wrote, so a future shape can branch instead of probing
        # for keys. A *newer* snapshot is refused rather than misread: silently
        # ignoring fields this kernel does not know is how a restart loses cash.
        version = int(snapshot.get("v", 1) or 1)
        if version > SNAPSHOT_VERSION:
            raise ValueError(
                f"snapshot v{version} is newer than this kernel (v{SNAPSHOT_VERSION}); "
                "upgrade the reader instead of guessing the fields")
        ledger = cls.fresh(snapshot["cash_available"], cost_model)
        ledger.account.cash_frozen = D(snapshot.get("cash_frozen", "0"))
        ledger.account.cash_withdrawable = D(snapshot.get("cash_withdrawable", "0"))
        ledger.account.realized_pnl = D(snapshot.get("realized_pnl", "0"))
        ledger.account.fees_paid = D(snapshot.get("fees_paid", "0"))
        ledger.account.dividend_income = D(snapshot.get("dividend_income", "0"))
        ledger.account.peak_equity = D(
            snapshot.get("peak_equity", snapshot["cash_available"])
        )
        ledger.oms.seq = int(snapshot.get("order_seq", 0) or 0)
        settled_session = snapshot.get("settled_session")
        if settled_session is not None:
            # Normalise through the same parser as settle() so a date object or
            # an ISO timestamp cannot bypass the same-session guard on restart.
            ledger.oms._settled_session = _session_key(settled_session)
        for symbol, row in snapshot.get("positions", {}).items():
            total = int(row["total_qty"])
            available = int(row["available_qty"])
            frozen = int(row.get("frozen_qty", 0))
            # Snapshots written before ``locked_qty`` existed carry the T+1 lock
            # only as a difference. Deriving it keeps the identity true on the
            # first ``settle`` after an upgrade instead of silently unlocking.
            locked = (int(row["locked_qty"]) if "locked_qty" in row
                      else max(0, total - available - frozen))
            ledger.account.positions[symbol] = Position(
                symbol=symbol,
                total_qty=total,
                available_qty=available,
                frozen_qty=frozen,
                locked_qty=locked,
                avg_cost=D(row["avg_cost"]),
                last_price=D(row.get("last_price", "0")),
                realized_pnl=D(row.get("realized_pnl", "0")),
                sleeve=row.get("sleeve", "CORE"),
            )

        cash_values = (ledger.account.cash_available, ledger.account.cash_frozen,
                       ledger.account.cash_withdrawable)
        if (any(not value.is_finite() for value in cash_values)
                or min(cash_values) < D("0")
                or ledger.account.cash_withdrawable > ledger.account.cash_available):
            raise ValueError("snapshot cash pools are inconsistent")
        for symbol, pos in ledger.account.positions.items():
            if (min(pos.total_qty, pos.available_qty, pos.frozen_qty,
                    pos.locked_qty) < 0
                    or not pos.avg_cost.is_finite() or pos.avg_cost < D("0")
                    or not pos.last_price.is_finite() or pos.last_price < D("0")
                    or not pos.is_consistent()):
                raise ValueError(f"snapshot position {symbol} is inconsistent")

        records = snapshot.get("order_records")
        legacy_open = any(
            status in {"PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
            for status in snapshot.get("orders", {}).values()
        )
        has_reservations = ledger.account.cash_frozen != 0 or any(
            p.frozen_qty for p in ledger.account.positions.values()
        )
        if records is None and (legacy_open or has_reservations):
            raise ValueError("snapshot has live reservations but no complete order records")

        for row in records or []:
            order = OrderManager.order_from_dict(row)
            if order.order_id in ledger.oms.orders:
                raise ValueError(f"duplicate order_id in snapshot: {order.order_id}")
            if order.client_order_id in ledger.oms.client_index:
                raise ValueError(
                    f"duplicate client_order_id in snapshot: {order.client_order_id}"
                )
            # Through the OMS index, not by writing ``oms.orders`` directly:
            # a restored order that skipped the live index would be invisible to
            # ``open_orders()`` and its reservation would never be released.
            ledger.oms.register(order)
            if order.order_id.startswith("O") and order.order_id[1:].isdigit():
                ledger.oms.seq = max(ledger.oms.seq, int(order.order_id[1:]))

        for row in snapshot.get("fills", []):
            fill = OrderManager.fill_from_dict(row)
            previous = ledger.oms.fills.get(fill.fill_id)
            if previous is not None and previous != fill:
                raise ValueError(f"conflicting fill_id in snapshot: {fill.fill_id}")
            if fill.order_id not in ledger.oms.orders:
                raise ValueError(f"fill references unknown order: {fill.order_id}")
            if previous is None:
                ledger.oms.fills[fill.fill_id] = fill

        if records is not None:
            frozen_cash = money(sum(
                (o.frozen_cash for o in ledger.oms.open_orders() if o.side == Side.BUY),
                D("0"),
            ))
            if frozen_cash != ledger.account.cash_frozen:
                raise ValueError("snapshot cash reservations do not match open orders")
            frozen_shares: dict[str, int] = {}
            for order in ledger.oms.open_orders():
                if order.side == Side.SELL:
                    frozen_shares[order.symbol] = (
                        frozen_shares.get(order.symbol, 0) + order.frozen_shares
                    )
            for symbol, pos in ledger.account.positions.items():
                if pos.frozen_qty != frozen_shares.get(symbol, 0):
                    raise ValueError("snapshot share reservations do not match open orders")
            if set(frozen_shares) - set(ledger.account.positions):
                raise ValueError("snapshot has an order for a missing position")
        return ledger
