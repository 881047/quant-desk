"""Plain helpers for data-layer tests. **Not a conftest, never a pytest plugin.**

Split out of ``tests/data/conftest.py`` on 2026-11-07 for the same reason as
``tests/trading/helpers.py``: a conftest that is also imported as a helper
library by test modules can be loaded twice under two names, and fixture
registration then depends on collection order. See that module's docstring for
the measured reproduction.
"""

from __future__ import annotations

import pandas as pd


def make_daily(days, code="600000.SH", base=10.0) -> pd.DataFrame:
    rows = []
    for i, day in enumerate(days):
        pre = base + i * 0.10
        close = pre + 0.05
        rows.append(
            {
                "ts_code": code,
                "trade_date": day,
                "open": pre + 0.01,
                "high": close + 0.03,
                "low": pre - 0.02,
                "close": close,
                "pre_close": pre,
                "vol": 1000.0 + i,
                "amount": 1_000_000.0 + i,
                "up_limit": round(pre * 1.1, 2),
                "down_limit": round(pre * 0.9, 2),
            }
        )
    return pd.DataFrame(rows)


def make_adj(days, code="600000.SH", start=1.0, step=0.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": code,
            "trade_date": days,
            "adj_factor": [start + step * i for i in range(len(days))],
        }
    )
