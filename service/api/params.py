"""Query-parameter validation shared by the API routers.

``_day`` existed twice — once in ``service/api/data.py`` and once in
``service/api/trading.py`` — with the same body and the same docstring. Two copies
of a validator is how one router ends up stricter than the other: ``/facts``
rejected ``as_of=../../x`` while ``/external`` kept resolving it into a real path
one directory up, and ``/market/pools`` normalised its date while ``/universe``
did not.

One definition, imported as ``day_param`` and aliased to ``_day`` where a module
already used that name.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from service.adapters.parquet_store import as_yyyymmdd


def day_param(value: str | None, field: str = "day") -> str | None:
    """Canonicalise a date parameter to ``YYYYMMDD``, or refuse it with 422.

    Two separate failures came from passing the raw string through. A hyphenated
    ``2026-10-30`` compares greater than every stored ``20261030``, so
    ``/market/pools`` labelled a 14:45 intraday snapshot ``settled`` and
    ``/data/brief`` reported a month-stale dataset as current. And
    ``POST /facts?as_of=../../x`` reached ``reports/facts/../../x.json`` as a real
    path. Both stop here.
    """
    if value is None:
        return None
    normalised = as_yyyymmdd(value)
    if normalised is None or len(normalised) != 8 or not normalised.isdigit():
        raise HTTPException(
            422, detail={"code": "BAD_DATE",
                         "message": f"{field} must look like 20261030, got {value!r}"})
    try:
        datetime.strptime(normalised, "%Y%m%d")
    except ValueError:
        # Eight digits is not a date: 20261345 normalises cleanly and would then be
        # compared, cached and written like any other session.
        raise HTTPException(
            422, detail={"code": "BAD_DATE",
                         "message": f"{field} is not a calendar date: {value!r}"}) from None
    return normalised
