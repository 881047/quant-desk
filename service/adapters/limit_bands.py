"""What does a non-positive limit price mean?

`stk_limit` stores the exchange's own band. A zero is not "the ceiling is
zero" and it is not always a gap: first-day listings have no band, and
delisting-arrangement stocks trade without one. The vendor writes both as
``up=99999.99`` / ``down=0``; only the list date separates them. A genuinely
missing row can wear the same shape, which is why an unexplained zero must stay
``MISSING`` rather than be guessed into a no-limit case.

It is deliberately **not** stored as a column on `stk_limit`: the label is a
function of `stock_basic`'s list/delist dates, and adding it to a 15-million-row
raw table would make a derivable fact into a second source of truth that can
drift. The audit script materialises it on demand; the quality report counts it.
"""

from __future__ import annotations

from datetime import datetime

FIRST_DAY = "no_limit_first_day"
DELISTING = "no_limit_delisting"
MISSING = "missing_or_unclassified"

#: The vendor writes this as the up limit when a name has no ceiling at all.
NO_CEILING = 1e4

#: A delisting-arrangement period is ~15 trading days; 45 calendar days is a
#: generous bound that still cannot reach a normal listing.
DELIST_WINDOW_DAYS = 45


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _gap_days(later: str, earlier: str) -> int | None:
    try:
        return (datetime.strptime(later, "%Y%m%d")
                - datetime.strptime(earlier, "%Y%m%d")).days
    except (TypeError, ValueError):
        return None


def _date(value) -> str:
    """Normalise a pandas date cell; ``nan``/``NaT`` must not read as a date."""
    text = str(value or "").strip()
    return "" if text in ("", "nan", "None", "NaT") else text


def classify_band(up_limit, down_limit, *, list_date=None, trade_date=None,
                  delist_date=None) -> str:
    """``""`` when the row carries a real band, otherwise one of the three codes.

    ``list_date``/``delist_date`` come from `stock_basic`; when they are
    unavailable the row is ``MISSING`` rather than guessed into a no-limit case.
    """
    up, down = _num(up_limit), _num(down_limit)
    if up > 0 and down > 0:
        return ""
    listed, traded, delisted = _date(list_date), _date(trade_date), _date(delist_date)
    # The list date comes first: a first-day listing carries the same
    # ``up=99999.99/down=0`` sentinel as a delisting name (69 of the 70 zero rows
    # in 2026 are first days), so the sentinel alone cannot mean delisting.
    if listed and traded and listed == traded:
        return FIRST_DAY
    if up >= NO_CEILING and down <= 0:
        return DELISTING
    if delisted and traded and delisted > traded:
        gap = _gap_days(delisted, traded)
        if gap is not None and 0 < gap <= DELIST_WINDOW_DAYS:
            return DELISTING
    return MISSING
