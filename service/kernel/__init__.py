"""Deterministic A-share paper-trading kernel.

Layering, from the outside in:

``Ledger`` + ``BarExecutionModel``
    The production pair, composed by ``trading.book.PaperBook``. The ledger owns
    every mutation of cash and positions; the execution model turns a bar into a
    fill and nothing else.
``OrderManager``
    Validation, reservations, idempotency and the order state machine.
``risk`` / ``engine``
    A reference implementation used by the golden scenarios, deliberately *not*
    wired into production (see ``tests/kernel/test_boundaries.py``).

The package re-exports these entry points so a caller can say what it needs
without knowing the file layout; the modules remain importable on their own.
"""

from .calendar import TradingCalendar
from .costs import CostModel
from .execution import BarExecutionModel, FillResult
from .ledger import Ledger
from .models import (
    Account,
    Bar,
    Board,
    D,
    Fill,
    Instrument,
    Intent,
    Order,
    OrderStatus,
    OrderType,
    Position,
    RejectReason,
    RiskDecision,
    Session,
    Side,
    money,
)
from .oms import CancelResult, FillRejected, OrderManager

__all__ = [
    "Account",
    "Bar",
    "BarExecutionModel",
    "Board",
    "CancelResult",
    "CostModel",
    "D",
    "Fill",
    "FillRejected",
    "FillResult",
    "Instrument",
    "Intent",
    "Ledger",
    "Order",
    "OrderManager",
    "OrderStatus",
    "OrderType",
    "Position",
    "RejectReason",
    "RiskDecision",
    "Session",
    "Side",
    "TradingCalendar",
    "money",
]
