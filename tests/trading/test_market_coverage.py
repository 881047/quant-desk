"""Which feed carries which instrument, and how far each feed reaches.

The desk reads several bar datasets and they are **ingested on separate
schedules**: on the evening the stock partition lands, ``daily`` is already at
today while ``cb_daily`` and ``fund_daily`` still end at the previous session.
"Latest bar" is therefore not one date, and every caller that assumed it was
ended up dating a bond read from the stock calendar — an empty frame, which
reads exactly like "it did not trade".

These pin the two answers separately: which dataset a symbol belongs to, and
what that dataset covers.
"""

from __future__ import annotations


def test_each_feed_answers_for_itself(desk):
    daily = desk.market.dataset_cover_end("daily")
    bond = desk.market.dataset_cover_end("cb_daily")

    for name, day in (("daily", daily), ("cb_daily", bond)):
        assert day and len(day) == 8 and day.isdigit(), f"{name} cover_end={day!r}"

    # One definition, not two: `latest_bar_date()` is the stock feed's answer.
    assert desk.market.latest_bar_date() == daily


def test_an_unknown_dataset_has_no_cover(desk):
    assert desk.market.dataset_cover_end("no_such_feed") is None


def test_the_feed_follows_the_code_not_a_prefix_guess(desk):
    """The same mapping `_date` uses to decide which file must have landed."""
    assert desk.market.quote_dataset("110081.SH") == "cb_daily"
    assert desk.market.quote_dataset("600000.SH") == "daily"
