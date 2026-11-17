"""Point-in-time views and look-ahead audits.

The single rule: a decision made on ``D`` may only use rows whose
``ann_date <= D`` (financials, name changes) or ``trade_date <= D`` (prices).
Anything else is look-ahead bias and must fail loudly, not silently improve the
backtest.

These helpers are used by both the backtester and the live agent, so the
"universe I see today" and "the universe I would have seen in 2021" come from
the same code path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from service import settings
from service.kernel.calendar import TradingCalendar

from .parquet_store import as_yyyymmdd as _as_yyyymmdd


class LookaheadError(ValueError):
    """Raised when a frame contains information from after the decision date."""


def _s(value: object) -> str:
    return "" if value is None else str(value)


def _shift_days(yyyymmdd: str, days: int) -> str:
    ts = pd.Timestamp(yyyymmdd) + pd.Timedelta(days=days)
    return ts.strftime("%Y%m%d")


# ------------------------------------------------------------------ calendar
@dataclass(frozen=True)
class TradingCalendarStore:
    days: tuple[str, ...]

    def __post_init__(self) -> None:
        # `_index` used to rebuild a 5,587-entry dict on every call, and `shift`
        # calls it recursively. Built once here: the object is frozen, so the map
        # can never disagree with `days`.
        object.__setattr__(self, "_index_map", {d: i for i, d in enumerate(self.days)})
        object.__setattr__(self, "_calendar_cache", None)

    @classmethod
    def from_frame(cls, cal: pd.DataFrame, start: str | None = None,
                   end: str | None = None) -> TradingCalendarStore:
        if cal.empty:
            return cls(())
        df = cal.copy()
        if "is_open" in df.columns:
            df = df[pd.to_numeric(df["is_open"], errors="coerce").fillna(0).astype(int) == 1]
        days = sorted({str(d) for d in df["cal_date"].dropna()})
        if start:
            days = [d for d in days if d >= start]
        if end:
            days = [d for d in days if d <= end]
        return cls(tuple(days))

    @property
    def _index(self) -> dict[str, int]:
        return self._index_map

    def calendar(self) -> TradingCalendar:
        """The ``TradingCalendar`` view, built once per store.

        Every kernel call that needs a session boundary (``submit``, ``cancel``,
        ``execute_ticket``) asks for this. It used to rebuild a frozenset of
        5,587 dates per call — 0.46 ms, paid inside ``PaperBook.lock`` on every
        order, for an object that can never change. A frozen dataclass has no
        lock, so two threads may both build it and one wins; both results are
        equal immutables, so the race is benign.
        """
        cached = self._calendar_cache
        if cached is None:
            cached = TradingCalendar.from_dates(pd.Timestamp(d).date() for d in self.days)
            object.__setattr__(self, "_calendar_cache", cached)
        return cached

    def covers(self, day: str) -> bool:
        """Is ``day`` inside the stored calendar's range?

        ``is_trading_day`` deliberately answers *unknown* days with the weekday
        rule (a scheduler that refuses every holiday is the worse failure), but
        that answer cannot be used to price an order: the kernel's
        ``TradingCalendar`` is a frozen set and calls the same day ``CLOSED``.
        Two calendars disagreeing about one date is how a ticket was accepted at
        propose time and cancelled at settlement with ``MARKET_CLOSED``.

        Callers that are about to create a money obligation should ask this
        first and say "the calendar ends at X" instead.
        """
        if not self.days:
            return False
        return self.days[0] <= str(day) <= self.days[-1]

    def is_trading_day(self, day: str) -> bool:
        if day in self._index:
            return True
        # Outside the stored calendar the honest answer is "unknown". Falling
        # back to the weekday rule beats silently reporting a holiday forever,
        # which is how a scheduler ends up skipping every single day.
        if self.days and (day < self.days[0] or day > self.days[-1]):
            try:
                return pd.Timestamp(day).weekday() < 5
            except ValueError:
                return False
        return False

    def prev(self, day: str) -> str | None:
        idx = self._index
        if day in idx:
            i = idx[day]
            return self.days[i - 1] if i > 0 else None
        prior = [d for d in self.days if d < day]
        return prior[-1] if prior else None

    def next(self, day: str) -> str | None:
        idx = self._index
        if day in idx:
            i = idx[day]
            return self.days[i + 1] if i + 1 < len(self.days) else None
        later = [d for d in self.days if d > day]
        return later[0] if later else None

    def shift(self, day: str, n: int) -> str | None:
        if not self.days:
            return None
        idx = self._index
        if day not in idx:
            base = self.prev(day) if n < 0 else self.next(day)
            if base is None:
                return None
            return self.shift(base, n)
        i = idx[day] + n
        return self.days[i] if 0 <= i < len(self.days) else None

    def between(self, start: str, end: str) -> list[str]:
        return [d for d in self.days if start <= d <= end]

    def last_n(self, as_of: str, n: int) -> list[str]:
        prior = [d for d in self.days if d <= as_of]
        return prior[-n:]


# ------------------------------------------------------------------ universe
@dataclass(frozen=True)
class PitUniverse:
    """As-of universe built from listing status + name history."""

    basic: pd.DataFrame
    namechange: pd.DataFrame
    #: Seconds an ``is_st`` answer stays valid. ``None`` = ``cache.market_seconds``.
    #: This used to be a class constant (``ST_CACHE_TTL = 60.0``) whose comment
    #: said "matches ``MarketRepository.ttl``" — a comment is not an enforcement
    #: mechanism, and the two numbers could drift apart without anything failing.
    #: ``MarketRepository`` now passes its own TTL in, so "the ST flag is as fresh
    #: as the frames it came from" is a structural fact rather than a promise.
    st_ttl: float | None = None

    def __post_init__(self) -> None:
        # ``is_st`` costs 4.0 ms per call: it scans the 13,921-row name-change
        # table for one code, and every ``Instrument`` asks it once — which means
        # every bar, snapshot, preflight and account summary. The frames behind
        # it never change after load, so a per-universe dict removes almost all
        # of that. Entries carry a timestamp because they must NOT live forever:
        # the 17:30 job can mark a name ST, and a service that has been up since
        # morning would keep serving the old band (10% instead of 5%) until
        # restart.
        # Cleared wholesale at the cap: the screen asks about ~2,000 codes,
        # values are one bool each, and an LRU would cost more than it saves.
        object.__setattr__(self, "_st_cache", {})
        object.__setattr__(
            self, "_st_ttl",
            float(settings.num("cache.market_seconds")) if self.st_ttl is None
            else float(self.st_ttl))

    def name_as_of(self, code: str, as_of: str) -> str:
        """The name this code carried on ``as_of``.

        ``namechange`` is a real history, not a snapshot: 13,921 rows covering
        5,922 codes back to 1990, with ``end_date`` marking when each name stopped
        being used. The fallback to ``stock_basic.name`` therefore only applies to
        a code with no row in that table at all — a name that never changed since
        listing, or a code the vendor does not track.

        That fallback is a look-ahead and is worth stating plainly, because the
        table is what makes the difference legible: replaying 2020-01-02 with
        today's names flags 380 ST names where the history says 222. The 158 extra
        are companies that were *not* ST then and are now.
        """
        base = self.basic.loc[self.basic["ts_code"] == code, "name"]
        fallback = str(base.iloc[0]) if len(base) else ""
        if self.namechange.empty:
            return fallback
        rows = self.namechange[
            (self.namechange["ts_code"] == code)
            & (self.namechange["start_date"].astype(str) <= as_of)
        ]
        if rows.empty:
            return fallback
        rows = rows.assign(_end=rows["end_date"].fillna("99999999").astype(str))
        rows = rows[rows["_end"] >= as_of]
        if rows.empty:
            return fallback
        return str(rows.sort_values("start_date").iloc[-1]["name"])

    def names_as_of(self, codes, as_of: str) -> pd.Series:
        """``name_as_of`` for many codes in one filter-then-groupby pass.

        Vectorised for the same reason ``st_map`` is: the scalar version walks
        the whole name-change table once per code, and the screen asks about two
        thousand codes inside the context pack.
        """
        wanted = pd.Index([str(c) for c in codes])
        base = (self.basic.drop_duplicates("ts_code")
                .set_index("ts_code")["name"].reindex(wanted))
        if self.namechange.empty:
            return base.fillna("").astype(str)
        nc = self.namechange
        start = nc["start_date"].astype(str)
        end = nc["end_date"].fillna("99999999").astype(str)
        live = nc[(start <= as_of) & (end >= as_of)
                  & nc["ts_code"].isin(set(wanted))]
        if live.empty:
            return base.fillna("").astype(str)
        latest = live.sort_values("start_date").groupby("ts_code")["name"].last()
        return latest.reindex(wanted).fillna(base).fillna("").astype(str)

    @staticmethod
    def _flags(names: pd.Series) -> pd.Series:
        """The one ST predicate: flagged, delisting, or in a delisting period."""
        upper = names.str.upper().str.replace(" ", "", regex=False)
        return (upper.str.contains("ST", regex=False)
                | upper.str.startswith("退")
                | upper.str.contains("退市", regex=False))

    #: Seconds an ``is_st`` answer stays valid — see ``st_ttl`` above.
    def is_st(self, code: str, as_of: str) -> bool:
        key = (str(code), str(as_of))
        now = time.monotonic()
        cached = self._st_cache.get(key)
        if cached is not None and now - cached[1] < self._st_ttl:
            return cached[0]
        value = bool(self.st_map([code], as_of).get(str(code), False))
        if len(self._st_cache) >= 8192:
            self._st_cache.clear()
        self._st_cache[key] = (value, now)
        return value

    def st_map(self, codes, as_of: str) -> dict[str, bool]:
        """ST status for many codes at once; same answer as ``is_st`` per code.

        ``is_st`` filters the whole name-change table for every code it is asked
        about. The screen asks about two thousand of them, which cost 4.7 seconds
        inside a call that is supposed to answer "what does the desk look like"
        — and it was the single largest cost in the context pack.
        """
        names = self.names_as_of(codes, as_of)
        return dict(zip(names.index, self._flags(names).tolist()))

    def as_of(
        self,
        as_of: str,
        *,
        min_list_days: int = 60,
        exclude_st: bool = True,
        exclude_star: bool = False,
        exclude_bse: bool = True,
    ) -> pd.DataFrame:
        df = self.basic.copy()
        if df.empty:
            return df
        # ``astype(str)`` turns a missing date into the strings "None"/"nan", and
        # both compare *greater* than "2026..." — so a stock with no list_date was
        # silently dropped from the universe, indistinguishable from one excluded
        # for being too new.
        df["list_date"] = df["list_date"].map(_as_yyyymmdd)
        listed = df[df["list_date"].notna() & (df["list_date"] <= as_of)]
        if "delist_date" in listed.columns:
            end = listed["delist_date"].fillna("99999999").astype(str)
            listed = listed[end > as_of]
        if min_list_days > 0:
            cutoff = _shift_days(as_of, -min_list_days)
            listed = listed[listed["list_date"] <= cutoff]
        if exclude_bse and "ts_code" in listed.columns:
            listed = listed[~listed["ts_code"].str.endswith(".BJ")]
        if exclude_star and "ts_code" in listed.columns:
            listed = listed[~listed["ts_code"].str.startswith(("688", "689"))]
        if listed.empty:
            return listed.assign(name_as_of=[], is_st=[]).reset_index(drop=True)

        # Names and flags are computed once for the surviving rows and then
        # filtered together. They used to be computed twice, by two different
        # predicates: `exclude_st` dropped 退市 names via `is_st` while the
        # `is_st` column tested only for the substring "ST", so a delisting
        # stock was excluded from the universe yet labelled `is_st=False` if
        # anyone re-filtered on the column.
        names = self.names_as_of(listed["ts_code"].tolist(), as_of)
        flags = self._flags(names)
        if exclude_st:
            keep = ~flags.to_numpy()
            listed = listed[keep]
            names = names[keep]
            flags = flags[keep]

        out = listed.copy()
        out["name_as_of"] = names.to_numpy()
        out["is_st"] = flags.to_numpy()
        return out.reset_index(drop=True)


# ---------------------------------------------------------------- financials
def pit_financials(
    df: pd.DataFrame,
    as_of: str,
    *,
    code_col: str = "ts_code",
    ann_col: str = "ann_date",
    period_col: str = "end_date",
) -> pd.DataFrame:
    """Latest report per company *as known on* ``as_of``.

    Uses announcement date, never period end. Restatements with a later
    announcement date but the same period win, which is what an investor would
    actually have seen on that day.
    """
    if df.empty:
        return df
    d = df.copy()
    d[ann_col] = d[ann_col].astype(str)
    d = d[d[ann_col].notna() & (d[ann_col] != "") & (d[ann_col] != "nan")]
    d = d[d[ann_col] <= as_of]
    if d.empty:
        return d
    d = d.sort_values([code_col, period_col, ann_col], kind="mergesort")
    d = d.groupby(code_col, as_index=False).tail(1)
    return d.reset_index(drop=True)


def assert_no_lookahead(
    df: pd.DataFrame,
    decision_date: str,
    *,
    ann_col: str = "ann_date",
    frame_name: str = "frame",
) -> None:
    """Fail fast if any row was announced after the decision date."""
    if df.empty or ann_col not in df.columns:
        return
    dates = df[ann_col].astype(str)
    future = df[dates.notna() & (dates != "") & (dates != "nan") & (dates > decision_date)]
    if len(future):
        codes = future.iloc[0].get("ts_code", "?")
        raise LookaheadError(
            f"{frame_name}: {len(future)} rows announced after {decision_date} "
            f"(e.g. {codes} ann_date={future.iloc[0][ann_col]})"
        )


def audit_decision_inputs(
    frames: dict[str, pd.DataFrame], decision_date: str
) -> dict[str, str]:
    """Check every named frame for look-ahead; returns a short audit report."""
    report: dict[str, str] = {}
    for name, frame in frames.items():
        try:
            assert_no_lookahead(frame, decision_date, frame_name=name)
            report[name] = "ok"
        except LookaheadError as exc:
            report[name] = str(exc)
    return report
