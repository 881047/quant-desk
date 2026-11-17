"""Zero is three facts, not one: first day, delisting, or a missing row."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.limit_bands import (
    DELISTING,
    FIRST_DAY,
    MISSING,
    classify_band,
)


def test_a_real_band_is_not_labelled():
    assert classify_band(11.0, 9.0, list_date="20200101", trade_date="20261112") == ""


def test_first_day_listing_has_no_band():
    # The vendor writes first-day and delisting names with the same sentinel;
    # the list date is the only thing that separates them.
    assert classify_band(99999.99, 0, list_date="20261112",
                         trade_date="20261112") == FIRST_DAY
    assert classify_band(0, 0, list_date="20261112",
                         trade_date="20261112") == FIRST_DAY


def test_the_vendor_no_ceiling_sentinel_is_a_delisting_session():
    # 920305.BJ 2026-07-09: 云创退, fell 80.7% that day.
    assert classify_band(99999.99, 0, list_date="20210826",
                         trade_date="20260709") == DELISTING


def test_a_delisting_date_close_ahead_is_a_delisting_session():
    assert classify_band(0, 0, list_date="20100101", trade_date="20260709",
                         delist_date="20260730") == DELISTING


def test_zero_without_a_dated_explanation_is_missing_not_no_limit():
    """The dangerous case: looks like a first day, is actually an absent row."""
    assert classify_band(0, 0, list_date="20100101",
                         trade_date="20260709") == MISSING


def test_nan_is_missing():
    assert classify_band(float("nan"), float("nan"),
                         list_date="20100101", trade_date="20260709") == MISSING
