"""Live fills: the same matcher, a bar built from the current quote.

Why a separate module. The desk filled orders at two prices only, the session
close and the next open. Both are reproducible: replay the day, get the same
fill. An intraday fill is not. It depends on the quote that happened to be on
screen at 10:31, and no replay can recover it. That difference is not a detail
to be smoothed over - it changes what the track record means - so it gets its
own module, its own ``fill_basis`` label, and its own set of refusals.

The bar is assembled from the vendor's session snapshot: open, high, low, last,
cumulative volume. The matcher then applies exactly the rules it applies to a
historical bar - price band, suspension, the 10% volume cap - because it never
knew where its Bar came from. That is the whole design: reuse the execution
model, change only the provenance.

Three things are refused outright, and each refusal is a sentence rather than a
silent fallback:

* **The quote is a stored close.** ``source="daily"`` means the vendors did not
  answer. Filling at yesterday's close while claiming "intraday" would be a
  fabricated timestamp.
* **The quote is stale.** Ninety seconds is already generous for a decision;
  beyond that the number on screen is history.
* **No quote at all.** Nothing to fill against.

Being outside the session is *not* a refusal here - it is handled in
``PaperBook`` by queueing for the next open, which is what the desk did before
this existed. A model woken at 20:00 should still be able to place an order; it
just cannot get a 20:00 price.

The volume cap deserves one note. ``quote.volume`` is cumulative for the session,
so at 10:00 it is a fraction of the day's turnover and the 10% cap is stricter
than it will be at 14:50. That errs on the side of refusing, which is the right
direction for a limit whose purpose is to stop a paper account from buying more
than the market could have offered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from service import settings
from service.kernel.clock import SHANGHAI
from service.kernel.models import Bar, D, Instrument

MAX_QUOTE_AGE = settings.num("live_fill.max_quote_age")


@dataclass(frozen=True)
class LiveBar:
    """A bar assembled from a live quote, with its provenance attached."""

    bar: Bar
    instrument: Instrument
    source: str
    quote_ts: str | None
    age_seconds: float | None

    @property
    def age_label(self) -> str:
        return "<1s" if not self.age_seconds else f"{self.age_seconds:.0f}s"

    def as_dict(self) -> dict:
        return {
            "symbol": self.bar.symbol,
            "last": str(self.bar.close),
            "pre_close": str(self.bar.pre_close),
            "up_limit": str(self.bar.up_limit),
            "down_limit": str(self.bar.down_limit),
            "volume": self.bar.volume,
            "source": self.source,
            "quote_ts": self.quote_ts,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "bar_ts": self.bar.ts.isoformat(),
        }


@dataclass(frozen=True)
class Refusal:
    """Why a live fill cannot happen right now, with the next step attached."""

    code: str
    message: str
    # Structured freshness detail, attached only where the 90/120 layering is
    # the reason for the refusal. Keeps the two thresholds assertable on the
    # wire instead of leaving them implicit in a prose sentence.
    freshness: dict | None = None

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.freshness is not None:
            out["freshness"] = self.freshness
        return out


def _timestamp(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def build(market, symbol: str, quote, *, now: datetime | None = None,
          max_age: float = MAX_QUOTE_AGE) -> tuple[LiveBar | None, Refusal | None]:
    """Turn one live quote into a Bar, or explain why that cannot be honest.

    ``market`` is used only for the instrument (board, ST flag, limit rules); the
    price fields come exclusively from the quote, so a stored bar can never leak
    into a supposedly live fill.
    """
    now = now or datetime.now(SHANGHAI)
    if quote is None:
        return None, Refusal("NO_QUOTE", f"{symbol} 没有报价；先看 /market/status 哪个源在应答")

    if not quote.is_realtime:
        return None, Refusal(
            "NOT_LIVE",
            f"{symbol} 当前来源是 {quote.source}，不是实时源；"
            f"用这个价格成交等于把收盘价伪装成盘中价",
        )

    quote_dt = _timestamp(quote.quote_ts)
    if quote_dt is None:
        return None, Refusal(
            "BAD_QUOTE_TS", f"{symbol} 实时报价缺有效 quote_ts，拒绝无时间戳成交")
    day = now.strftime("%Y%m%d")
    calendar = getattr(market, "calendar", None)
    is_trading_day = getattr(calendar, "is_trading_day", None)
    if not is_trading_day or not is_trading_day(day) or quote_dt.strftime("%Y%m%d") != day:
        return None, Refusal(
            "NON_TRADING_DAY",
            f"{symbol} quote_ts={quote.quote_ts} 与有效交易日 {day} 不一致",
        )

    if not _finite_number(quote.age_seconds):
        return None, Refusal("BAD_QUOTE_AGE", f"{symbol} 报价 age_seconds 缺失或非有限数")
    age = float(quote.age_seconds)
    actual_age = (now - quote_dt).total_seconds()
    if age < 0 or actual_age < 0:
        return None, Refusal(
            "BAD_QUOTE_AGE", f"{symbol} 报价时间不能晚于当前时间（age={age}）")
    effective_age = max(age, actual_age)
    # Delayed import: this module is on the fill path and realtime pulls in
    # pandas, so keep it out of the module-level import chain.
    from .realtime import quote_freshness

    freshness = quote_freshness(effective_age)
    # ``max_age`` is an upper-bound OVERRIDE (its default is MAX_QUOTE_AGE = 90).
    # Judge against the bound this call actually uses, NOT the helper's
    # config-derived ``tradable``: that gate is fixed at 90, so OR-ing it in
    # could only tighten the bound and never loosen it — passing max_age=200
    # still refused a 105s quote while the message said "limit 200". Report both
    # thresholds so the annotation matches the decision: ``max_quote_age`` is the
    # configured default, ``effective_max_age`` is what this call applied, and
    # ``tradable`` follows the effective bound.
    limit = float(max_age)
    # Same quantity the helper and the message report: ``freshness["age_seconds"]``
    # is ``effective_age = max(age, actual_age)``, so judge that — using the
    # quote's self-reported ``age`` alone left ``age_seconds=118, limit=90,
    # tradable=True`` in the refusal, which reads as "within the limit yet
    # refused". Decision logic below is unchanged.
    tradable = 0 <= effective_age <= limit
    freshness["effective_max_age"] = limit
    freshness["tradable"] = tradable
    if not tradable or actual_age > limit:
        return None, Refusal(
            "STALE_QUOTE",
            f"{symbol} 报价已过期 {effective_age:.0f} 秒"
            f"（成交口径上限 {limit:.0f} 秒）；状态判定用的是另一条门槛 "
            f"stale_after={freshness['stale_after']:.0f} 秒，"
            f"{'该报价仍可能被视为可测（measurable）' if freshness['measurable'] else '两侧都已过期'}。"
            "90/120 是有意分层：成交不可逆收得紧，状态判定有 unknown fail-closed 兜底，"
            "不是同一个结论。",
            freshness=freshness,
        )

    numeric_fields = (quote.last, quote.pre_close, quote.high, quote.low,
                      quote.up_limit, quote.down_limit, quote.volume, quote.amount)
    if not all(_finite_number(value) for value in numeric_fields):
        return None, Refusal("BAD_QUOTE", f"{symbol} 报价含 NaN/Inf 或非数值字段")
    if (quote.last <= 0 or quote.pre_close <= 0 or quote.up_limit <= 0
            or quote.down_limit <= 0 or quote.volume < 0 or quote.amount < 0):
        return None, Refusal(
            "BAD_QUOTE",
            f"{symbol} 报价缺有效价格/成交量（last={quote.last} pre_close={quote.pre_close} "
            f"volume={quote.volume} amount={quote.amount}）",
        )

    instrument = market.instrument(symbol, day)
    low = quote.low if quote.low > 0 else quote.last
    # Capacity at a live fill uses the *previous* session's turnover for the same
    # reason a historical bar does: today's volume is still being printed, and
    # what an order could absorb must not grow with the clock. A missing
    # partition is 0 — the matcher reads that as "the builder had nothing" and
    # falls back to today's volume, labelled ``+VOLUME_CAP_TODAY`` when it binds.
    prior_volume = 0
    prev_of = getattr(calendar, "prev", None)
    traded_volume = getattr(market, "traded_volume", None)
    prev_day = prev_of(day) if callable(prev_of) else None
    if prev_day and callable(traded_volume):
        try:
            prior_volume = int(traded_volume(symbol, prev_day))
        except (KeyError, ValueError, OSError):
            prior_volume = 0
    # ``open`` is the current price, not the session open. The matcher fills a
    # marketable limit buy at ``min(bar.open, limit)``, which is correct for a
    # historical bar and a look-back fill for a live one: at 10:30 you cannot buy
    # at the 09:30 print. The high/low stay as the session's range, because they
    # only bound where the price has been.
    bar = Bar.production(
        symbol=symbol,
        ts=now,
        open=quote.last,
        high=max(quote.high, quote.last),
        low=min(low, quote.last),
        close=quote.last,
        pre_close=quote.pre_close,
        volume=int(quote.volume or 0),
        amount=D(str(quote.amount or 0)),
        up_limit=quote.up_limit,
        down_limit=quote.down_limit,
        suspended=quote.suspended,
        prior_volume=prior_volume,
        # ``open`` above is the price now, not the auction print. The matcher
        # has to know that: a ceiling reached at 13:00 is a queue, not a day
        # that opened sealed, and the two call for different decisions.
        priced_at="live",
    )
    return LiveBar(bar=bar, instrument=instrument, source=quote.source,
                   quote_ts=quote.quote_ts, age_seconds=quote.age_seconds), None
