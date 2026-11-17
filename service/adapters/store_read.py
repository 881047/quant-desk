"""Read a dataset when "not ingested yet" is an expected answer.

``DatasetStore.read`` raises when the dataset has never been written. Most
callers want to degrade instead of fail — a panel with one missing block, not a
500 — so they each wrote the same ``try/except`` and the copies drifted: one
caught ``ValueError`` because of a bad partition, the next did not, and the
third returned ``None`` instead of an empty frame and broke every chained
``.empty`` check.

Two things are shared here, and only two:

* :func:`read_or_empty` — the same four lines everyone was writing;
* :func:`open_sessions` — trading days from ``trade_cal``, which both the tape
  and the facts panel need, and which had three private copies.

This module sits in ``adapters`` on purpose: it touches a store, so ``kernel``
may not own it, and ``research``/``trading`` both sit above it, so neither has
to import the other to get it.
"""

from __future__ import annotations

import pandas as pd


def read_or_empty(store, name: str, start: str, end: str,
                  columns: list[str] | None = None) -> pd.DataFrame:
    """``store.read`` with "absent" mapped to an empty frame.

    Returns a frame, never ``None``: every caller chains ``.empty``/``.columns``
    on the result, and a ``None`` here fails at the *next* line rather than at
    this one.
    """
    try:
        return store.read(name, start=start, end=end, columns=columns)
    except (KeyError, ValueError):
        return pd.DataFrame()


def open_sessions(store, start: str, end: str) -> list[str]:
    """Trading days in ``[start, end]``; empty means "the calendar is missing"."""
    cal = read_or_empty(store, "trade_cal", start, end,
                        columns=["cal_date", "is_open"])
    if cal is None or cal.empty or "is_open" not in cal.columns:
        return []
    opened = pd.to_numeric(cal["is_open"], errors="coerce") == 1
    return sorted(cal.loc[opened, "cal_date"].astype(str).tolist())
