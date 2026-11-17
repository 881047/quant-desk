"""A JSON response that cannot be turned into a 500 by one missing number.

Starlette renders with ``allow_nan=False`` because ``NaN`` is not JSON. That is
the right default — a JSON document claiming to contain ``NaN`` is not readable
by anything else — but it makes every payload that *might* carry one a 500: a
suspended stock whose ``close`` came back as ``"-"`` and was coerced to NaN, a
metrics row whose mean is over an empty window, a percentile of a one-row
series. The observable failure is that a market-data gap takes down the whole
endpoint, and then the 4xx/5xx breaker disables the route for a minute, so
"one pool has a dirty row" becomes "the desk cannot be read".

The fix is to convert non-finite floats to ``null`` at the boundary: absent is
a value a consumer can handle, and it is what the number actually is. The fast
path is unchanged — the sanitising pass only runs after ``json.dumps`` has
already refused the payload.
"""

from __future__ import annotations

import math

from fastapi.responses import JSONResponse

# Types that json.dumps already handles; walking them is pure cost.
_SCALARS = (str, int, bool, type(None))


def finite(value: object) -> object:
    """Recursively replace non-finite floats with ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(item) for item in value]
    if isinstance(value, set):
        return [finite(item) for item in value]
    # numpy scalars and Decimal reach here as their own types; ``float()`` is the
    # conversion json.dumps would attempt anyway.
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return value
    if math.isfinite(number):
        return value
    return None


class SafeJSONResponse(JSONResponse):
    """``JSONResponse`` that degrades a non-finite number to ``null``."""

    def render(self, content: object) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            return super().render(finite(content))
