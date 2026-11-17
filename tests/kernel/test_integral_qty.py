"""One definition of "is this a share count", shared by the OMS and sizing.

``OrderManager`` had the careful version and ``DecisionService`` did not: sizing
called ``int(qty)``, so 100.7 shares became 100 with no error and no note in the
response, and ``10**400`` reached ``float(qty)`` and raised ``OverflowError`` out
of ``/decision/propose`` as a 500. The ticket path runs *before* submit, so a
quantity that survives sizing must be one submit would also accept — otherwise
the desk hands out tickets the kernel will refuse.

The table below is the contract both callers now share.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from service.kernel.models import MAX_ORDER_QTY, integral_qty


@pytest.mark.parametrize(("value", "expected"), [
    (100, 100),
    (0, 0),
    (100.0, 100),                       # a JSON round trip may hand one back
    (Decimal(100), 100),
    (Decimal("100.000"), 100),
    (MAX_ORDER_QTY, MAX_ORDER_QTY),
    (100.5, None),
    (Decimal("100.00000000000000001"), None),  # a float would have rounded it
    (Decimal("NaN"), None),
    (float("inf"), None),
    (float("nan"), None),
    (True, None),                       # a bool is a flag, not a quantity
    (-1, None),
    (MAX_ORDER_QTY + 1, None),
    (10 ** 400, None),                  # float(10**400) raises OverflowError
    ("100", None),
    (None, None),
])
def test_the_contract(value, expected):
    assert integral_qty(value) == expected


def test_the_order_manager_no_longer_keeps_its_own_copy():
    """Meta-assertion: a private copy is how the two paths diverged."""
    source = Path("service/kernel/oms.py").read_text(encoding="utf-8")
    assert "_integral_qty" not in source
    assert "integral_qty(qty)" in source
