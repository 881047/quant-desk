"""Read-only market view for the trading desk.

Unit note: Tushare stores ``vol`` in 手 (100 shares) and ``amount`` in 千元.
Everything this module exposes to the desk is normalised to **shares** and
**CNY**; the raw columns stay untouched in Parquet.

The data layer stores years of bars; a trading decision needs one day. This
module keeps small, TTL-cached slices so a single agent turn can call several
tools without re-reading a 1.3M-row partition each time.

Everything here is read-only and deterministic: no wall clock decisions beyond
"which is the latest trading day", and that comes from the stored calendar.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd

from service import settings
from service.adapters import anns_text
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import PitUniverse, TradingCalendarStore
from service.adapters.symbols import (
    ETF_PREFIXES,
    board_of,
    is_convertible,
    limit_pct,
)
from service.kernel.corp_action import CBCoupon, CBMaturity, CorporateAction
from service.kernel.models import Bar, Board, D, Instrument
from service.kernel.units import CB_PRICE_TICK, PRICE_TICK


# Indices live in their own table, and the code alone cannot say so:
# ``000001.SH`` is 上证指数 while ``000001.SZ`` is 平安银行, so the numeric prefix
# is ambiguous and `A_SHARE_PREFIXES` claims both. The list is declared in
# ``configs/data.json`` because that is also the set the backfiller refreshes
# into ``index_daily`` — one list, two readers, no chance of them disagreeing.
def _index_codes() -> frozenset[str]:
    try:
        raw = json.loads((settings.ROOT / "configs" / "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(code).strip().upper() for code in raw.get("index_codes") or ())


INDEX_CODES = _index_codes()

def _dec(value: object, default: str = "0") -> Decimal:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return D(default)
        return D(str(value))
    except Exception:  # noqa: BLE001 - vendor strings
        return D(default)


_RATE_PERIOD = re.compile(r"(\d{8})-(\d{8})\s*,\s*票面利率[:：]\s*([\d.]+)\s*%")

#: "No token recorded yet" — distinct from ``None``, which means the store
#: cannot answer (no registry, i.e. a test double).
_UNSET = object()


def _cost_rate(key: str) -> Decimal:
    """One fee rate, read from ``configs/costs.yaml`` — or fail loudly.

    ``Instrument`` was built here with ``0.0005`` / ``0.00001`` typed into the
    source, while ``kernel/models.py`` states that reading the config is what
    keeps the defaults honest. The result was two live answers: editing
    ``costs.yaml`` moved the research layer and left the paper book on the old
    rates, with no error anywhere. ``service/settings.decimal`` is the same
    reader ``CostModel`` uses, so the two now come from one place.

    A fallback literal was kept here for a while as a second line of defence.
    It was worse than useless: ``MissingParameter`` is a ``KeyError`` and
    ``BadParameter`` is a ``ValueError``, so a missing / malformed rate / a
    broken invariant was swallowed and silently booked at a guessed fee, while
    ``kernel/costs.CostModel._rate`` on the very same config fails fast. No
    fallback now — a bad schedule must stop the booking, not reprice it.
    """
    return settings.decimal(f"costs.{key}")


def _parse_rate_clause(text: str) -> list[tuple[str, str, Decimal]]:
    """`起-止,票面利率:X%;` repeated — the only place a step-up schedule exists.

    `cb_basic.coupon_rate` carries one number, and for a step-up bond that is
    the *final* period (0.2% -> 2.0%). Pricing every coupon off it overstates
    the early years by an order of magnitude, so the schedule is parsed instead.

    A clause that does not match returns empty rather than a guess: a missing
    coupon is visible in the ledger as zero interest, a fabricated one is not.
    """
    if not text:
        return []
    return [(m.group(1), m.group(2), D(m.group(3)) / D("100"))
            for m in _RATE_PERIOD.finditer(text)]


def _text(value: object) -> str:
    """A vendor cell as text, with NaN/None/NaT folded to an empty string."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    trade_date: str
    last: Decimal
    pre_close: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    up_limit: Decimal
    down_limit: Decimal
    volume: float
    amount: float
    suspended: bool
    source: str


@dataclass
class _Entry:
    """One cached answer, with the two things eviction actually needs.

    ``stamp`` is refreshed on every hit (this is an LRU, not a FIFO — the old
    code only wrote it on insert, so a key read a thousand times still looked
    like the oldest). ``size`` is bytes-as-pandas-sees-them, because the ceiling
    that matters is memory, not row count: a year of `daily` and a 20-row pool
    are both "one entry" and differ by four orders of magnitude.
    """

    stamp: float
    value: object
    size: int
    columns: frozenset[str]


def _frame_bytes(value: object) -> int:
    """Rough resident size of a cached frame, cheap enough to run on insert.

    ``memory_usage(deep=True)`` is the accurate answer and costs ~0.5 s on a
    1.3M-row year of `daily` — measured as a 3.0 s → 3.4 s regression on the cold
    context pack, which is the exact call this cache exists to make fast. So the
    numeric blocks are counted exactly and the object columns are extrapolated
    from a 1,000-row sample: within a few percent for the frames we cache, and
    this number only drives eviction, never a fee or a size decision.
    """
    if not isinstance(value, pd.DataFrame):
        return 1024
    try:
        total = int(value.memory_usage(deep=False, index=False).sum())
        object_cols = [c for c in value.columns if value[c].dtype == object]
        if object_cols and len(value):
            sample = value[object_cols].head(min(1000, len(value)))
            per_row = sum(int(sample[c].astype(str).str.len().sum()) for c in object_cols)
            total += int(per_row / len(sample) * len(value))
        return max(total, 1024)
    except (ValueError, TypeError, AttributeError):
        return 1024


#: One window for "how old may a market fact be before we re-read it".
#: Three places used to hardcode 60 s: this module's frame cache default, its
#: manifest re-read, and ``PitUniverse.ST_CACHE_TTL`` — the last one with a
#: comment saying "matches ``MarketRepository.ttl``". A comment is not a single
#: source: change one literal and the desk keeps a stale ST flag for whichever
#: window is longer, with nothing failing. All three now read this.
_MARKET_TTL = settings.num("cache.market_seconds")


class MarketRepository:
    #: How often the store's manifest is re-read to answer "did the data move".
    #: The frame cache expires on its own after ``ttl``, but the three snapshots
    #: built once per process (calendar / universe / dividends) had no expiry at
    #: all — a service up since 09:00 kept the morning's ``namechange`` table and
    #: priced a newly-flagged ST name at the old 10% band until a restart.
    DATA_CHECK_TTL = _MARKET_TTL

    def __init__(self, store: DatasetStore, ttl_seconds: float | None = None,
                 max_entries: int = 512, max_bytes: int | None = None):
        self.store = store
        self.ttl = _MARKET_TTL if ttl_seconds is None else float(ttl_seconds)
        # Bounded on purpose. TTL alone does not bound anything: entries are only
        # checked for expiry when they are read again, so every symbol ever asked
        # about kept its frame alive for the life of the process. A profile, a
        # screen and a handful of watchlists over a few thousand names reached
        # hundreds of megabytes on a service that runs for weeks.
        #
        # Count alone does not bound it either. `max_entries=512` was nominally
        # 512 x one year of `daily` (272 MB) — 139 GB — while the measured RSS of
        # the service was 1.6 GB. Both ceilings are enforced now, and bytes is
        # the one that fires first.
        self.max_entries = max(16, int(max_entries))
        # 1.5 GB, not 512 MB. Measured 2026-11-02: one `quant_profile` needs ~15
        # year-partitions of `daily`/`daily_basic`/`adj_factor` alive at once —
        # a 1.26 GB working set. Under a 512 MB ceiling every section evicted the
        # one before it and the profile took 3.8 s *warm*; with the room it takes
        # 0.84 s. Overridable because this is a memory trade on the operator's box:
        # `QUANT_MARKET_CACHE_MB=512` trades speed back for RAM.
        env_mb = os.getenv("QUANT_MARKET_CACHE_MB")
        budget = int(max_bytes) if max_bytes is not None else int(
            float(env_mb) * 1024 * 1024) if env_mb else 1536 * 1024 * 1024
        self.max_bytes = max(32 * 1024 * 1024, budget)
        self._cache: dict[str, _Entry] = {}
        self._versions: dict[str, int] = {}
        self._bytes = 0
        self._evictions = 0
        self._epoch = 0
        self._refreshing: set[str] = set()
        self._cache_condition = threading.Condition()
        self._calendar: TradingCalendarStore | None = None
        self._calendar_lock = threading.Lock()
        self._calendar_built_at: str | None = None
        self._universe: PitUniverse | None = None
        self._universe_lock = threading.Lock()
        self._universe_built_at: str | None = None
        self._dividends: pd.DataFrame | None = None
        #: Guards the "has the store moved" probe. Deliberately a *separate*
        #: lock: the probe ends in ``invalidate()``, which takes the three
        #: snapshot locks, so it must never be acquired while holding one.
        self._data_check_lock = threading.Lock()
        self._data_checked_at = 0.0
        self._data_checked_wall: str | None = None
        self._data_token: object = _UNSET

    # ------------------------------------------------------------------ cache
    def _evict_oldest(self) -> None:
        """Drop the stalest entries until both ceilings are satisfied.

        Least-recently-*used*, not least-recently-inserted: hits refresh the
        stamp, so a symbol asked about every minute is not evicted ahead of one
        inserted yesterday and never read again. Keys mid-refresh are skipped
        unless nothing else is left — dropping one lets a second builder run for
        the same key, which is safe but wasteful.
        """
        removable = [(entry.stamp, key) for key, entry in self._cache.items()
                     if key not in self._refreshing]
        if not removable:
            removable = [(entry.stamp, key) for key, entry in self._cache.items()]
        removable.sort()
        for _, key in removable:
            if len(self._cache) <= self.max_entries and self._bytes <= self.max_bytes:
                break
            entry = self._cache.pop(key, None)
            if entry is not None:
                self._bytes -= entry.size
                self._evictions += 1
            self._versions.pop(key, None)

    def cache_stats(self) -> dict:
        """What /health reports: is the ceiling doing anything at all?

        `evictions` is the number that matters. A cache that evicts as fast as it
        fills looks exactly like a cold cache on every call — that is what a
        3.8 s `quant_profile` on a "warm" process turned out to be.
        """
        stats = {"entries": len(self._cache), "bytes": self._bytes,
                 "max_entries": self.max_entries, "max_bytes": self.max_bytes,
                 "evictions": self._evictions}
        if self._evictions > 50:
            stats["thrashing"] = True
            stats["note"] = ("淘汰次数 > 50：工作集超过上限，每次调用都在重建。"
                             "调大 QUANT_MARKET_CACHE_MB 或减小请求窗口。")
        return stats

    def _is_fresh(self, entry: _Entry | None, key: str, epoch: int, now: float,
                  columns: frozenset[str] | None) -> bool:
        """Is this entry usable for a request that needs ``columns``?

        Must be called with ``_cache_condition`` held: every input it reads
        (``_cache``, ``_versions``, ``entry.stamp``) is mutated under that
        condition. Both the slow path and ``_year_frame``'s fast path go through
        here so there is exactly one definition of "fresh" — the two used to be
        written out separately, which is how the fast path ended up checking
        without the lock.
        """
        return bool(entry is not None and self._versions.get(key) == epoch
                    and now - entry.stamp < self.ttl
                    and (columns is None or columns <= entry.columns))

    def _lookup(self, key: str,
                wanted: frozenset[str]) -> tuple[pd.DataFrame | None, frozenset[str]]:
        """A hit, or the columns of the entry that was there (for the union).

        Takes the condition even on the hit path. The old code read ``_cache`` /
        ``_versions`` and refreshed ``entry.stamp`` outside it while
        ``invalidate()`` and ``_evict_oldest()`` were clearing or popping the
        same dict inside it: a frame read after an ``invalidate()`` could be
        stamped as fresh and returned, which is exactly the "changed the config,
        got yesterday's frame" symptom the epoch exists to prevent.
        """
        with self._cache_condition:
            now = time.monotonic()
            epoch = self._epoch
            entry = self._cache.get(key)
            if self._is_fresh(entry, key, epoch, now, wanted):
                entry.stamp = now
                return entry.value, entry.columns
            return None, (entry.columns if entry is not None else frozenset())

    def _cached(self, key: str, builder, *, columns: frozenset[str] | None = None):
        # Every frame read goes through here, which makes it the one place that
        # can notice a daily write without a restart. It must run before the
        # condition is taken: the probe ends in ``invalidate()``.
        self._maybe_invalidate()
        owns_refresh = False
        try:
            with self._cache_condition:
                while True:
                    now = time.monotonic()
                    epoch = self._epoch
                    hit = self._cache.get(key)
                    # `columns` is a *requirement*, not a preference: a frame
                    # cached for six columns must not satisfy a request that
                    # needs `high` as well. Keying by `dataset:year` made that
                    # possible, and the first symptom was a KeyError three
                    # frames downstream rather than a cache miss here.
                    if self._is_fresh(hit, key, epoch, now, columns):
                        hit.stamp = now
                        return hit.value
                    if key not in self._refreshing:
                        self._refreshing.add(key)
                        owns_refresh = True
                        break
                    self._cache_condition.wait()

            value = builder()
            size = _frame_bytes(value)
            with self._cache_condition:
                # An invalidate during the read must not be followed by a stale
                # write. The caller still gets its consistent builder result.
                if epoch == self._epoch:
                    previous = self._cache.pop(key, None)
                    if previous is not None:
                        self._bytes -= previous.size
                    self._cache[key] = _Entry(
                        stamp=time.monotonic(), value=value, size=size,
                        columns=columns or frozenset())
                    self._bytes += size
                    self._versions[key] = epoch
                    if len(self._cache) > self.max_entries or self._bytes > self.max_bytes:
                        self._evict_oldest()
            return value
        finally:
            # Exceptions and task cancellation must both let a waiter retry.
            if owns_refresh:
                with self._cache_condition:
                    self._refreshing.discard(key)
                    self._cache_condition.notify_all()

    def invalidate(self) -> None:
        with self._cache_condition:
            self._epoch += 1
            self._cache.clear()
            self._versions.clear()
            self._bytes = 0
            self._dividends = None  # the daily job rewrites the table
        # These three are not entries in ``_cache``: they are snapshots built on
        # first use and kept for the life of the process. Clearing only the
        # frames left a service up since the morning pricing a newly-flagged ST
        # name at the old band, and holding last year's ``trade_cal`` after the
        # new one had been published. ``invalidate()`` is the one lever that
        # means "the store moved, rebuild everything derived from it".
        with self._calendar_lock:
            self._calendar = None
            self._calendar_built_at = None
        with self._universe_lock:
            self._universe = None
            self._universe_built_at = None

    # ----------------------------------------------------------- data version
    def _manifest_token(self):
        """A cheap fingerprint of what the store currently holds.

        ``Registry.summary()`` already aggregates per dataset: partition count,
        row count, coverage, last ingest. Any daily write moves at least one of
        them, and a single read-only query answers "has anything changed" for
        the whole store — which is what the frame TTL was pretending to do.
        """
        registry = getattr(self.store, "registry", None)
        if registry is None:
            return None
        return tuple(
            (row.get("dataset"), row.get("partitions"), row.get("rows"),
             row.get("cover_start"), row.get("cover_end"), row.get("last_ingested"))
            for row in registry.summary()
        )

    def _maybe_invalidate(self) -> None:
        """Drop the process-wide snapshots when the store has moved on.

        ``daily_job`` runs in its own process, so calling ``invalidate()`` there
        cannot reach a running API. The manifest is the only cross-process
        signal both sides already agree on, and this probes it once a minute.

        Cheap enough for the hot path: one ``time.monotonic()`` per call and one
        aggregate query per minute. A store that cannot answer (SQLite busy, a
        test double with no registry) keeps serving what it has — being unable
        to check is not evidence of change.
        """
        interval = min(self.ttl, self.DATA_CHECK_TTL)
        now = time.monotonic()
        if now - self._data_checked_at < interval:
            return
        with self._data_check_lock:
            now = time.monotonic()
            if now - self._data_checked_at < interval:
                return
            self._data_checked_at = now
            self._data_checked_wall = datetime.now().isoformat(timespec="seconds")
            try:
                token = self._manifest_token()
            except (sqlite3.Error, OSError):
                return
            if token is None:
                return
            previous, self._data_token = self._data_token, token
            if previous is not _UNSET and token != previous:
                self.invalidate()

    def snapshot_stats(self) -> dict:
        """When each process-wide snapshot was built; ``/health`` reports it.

        ``built_at`` is what makes "the ST band is stale" visible before it
        becomes a wrong fill: a universe built at 09:00 and a ``trade_cal``
        built last December are both normal at 09:00 and both suspicious at
        18:00.
        """
        return {
            "universe_built_at": self._universe_built_at,
            "calendar_built_at": self._calendar_built_at,
            "data_checked_at": self._data_checked_wall,
        }

    def cached_answer(self, key: str, builder):
        """Cache one computed answer that is not a frame.

        The panel in ``/discover`` is a plain dict: a breadth table, a regime
        read, a data-health block. It costs ~1.4 s to rebuild and it is a pure
        function of (as_of, stored data), so rebuilding it per call was pure
        waste — and it was the reason a ten-way fan-out took 21.7 s instead of
        1.4 s.

        This shares the frame cache's machinery, so it also shares its
        single-flight behaviour: ten concurrent callers wait for one builder
        instead of running ten of them.
        """
        return self._cached(key, builder)

    # --------------------------------------------------------------- calendar
    @property
    def calendar(self) -> TradingCalendarStore:
        # ``trade_cal`` is read directly rather than through ``_cached``, so the
        # probe has to be repeated here; otherwise a process that only prices
        # bars (and never reads a frame) would keep last year's calendar.
        self._maybe_invalidate()
        calendar = self._calendar
        if calendar is not None:
            return calendar
        with self._calendar_lock:
            if self._calendar is None:
                self._calendar = TradingCalendarStore.from_frame(
                    self.store.read("trade_cal"))
                self._calendar_built_at = datetime.now().isoformat(timespec="seconds")
            return self._calendar

    def last_trading_day(self, as_of: str | None = None) -> str | None:
        as_of = as_of or datetime.now().strftime("%Y%m%d")
        if self.calendar.is_trading_day(as_of):
            return as_of
        return self.calendar.prev(as_of)

    def recent_days(self, as_of: str, n: int) -> list[str]:
        return self.calendar.last_n(as_of, n)

    def dataset_cover_end(self, dataset: str) -> str | None:
        """Last session a dataset actually covers, by its own manifest.

        The desk reads four bar datasets and they are **ingested on separate
        schedules**: on the evening the stock partition lands, ``daily`` is
        already at today while ``cb_daily`` and ``fund_daily`` still end at the
        previous session. Anything that dates a bond or a fund read from the
        stock calendar therefore asks for a session that dataset does not have
        — an empty frame, which reads exactly like "it did not trade".

        One resolver rather than a per-call-site scan: two copies of this
        predicate is how "latest bar" and "latest *this* bar" drifted apart.
        """
        ends = [str(row["cover_end"]) for row in self.store.registry.summary()
                if row.get("dataset") == dataset and row.get("cover_end")]
        return max(ends) if ends else None

    def latest_bar_date(self) -> str | None:
        """Last date for which a daily bar actually exists.

        ``last_trading_day()`` answers "what date is it in the calendar"; this
        answers "what date can I price a decision at". They differ every day
        between 15:00 and the moment the vendor publishes, and conflating them
        made the desk show zero candidates while the market was simply not
        reported yet.
        """
        return self.dataset_cover_end("daily")

    # ------------------------------------------------------------------ frames
    @staticmethod
    def _project(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        if df.empty:
            return df
        keep = [c for c in columns if c in df.columns]
        return df[keep] if keep else df

    def _year_frame(self, dataset: str, year: str, columns: list[str]) -> pd.DataFrame:
        """One year of a dataset, projected to ``columns``.

        The projection happens in the parquet reader, not after it: a year of
        `daily_basic` is ~1M rows x 18 columns, and a per-symbol question needs
        ten of them for one symbol. Reading the file whole and slicing afterwards
        is what made a per-name profile cost seconds.

        The cache key is ``dataset:year`` — **not** ``dataset:year:columns``.
        Hashing the projection into the key meant one cold `context_pack` held
        two copies of `daily/2026` (measured: 11 distinct keys / 17 calls,
        `daily:2026` twice with 9 columns and with 6) and read the same 1.09M-row
        partition twice. A cached frame is now reused as long as it covers the
        requested columns; a request for new columns rebuilds with the **union**,
        so the entry grows monotonically instead of forking.

        The hit check lives in ``_lookup`` so it happens under
        ``_cache_condition``; returning a frame here without that lock is what
        let a post-``invalidate()`` read look fresh.
        """
        wanted = frozenset(columns)
        key = f"{dataset}:{year}"
        value, have = self._lookup(key, wanted)
        if value is not None:
            return self._project(value, columns)

        cols = sorted(wanted | have)

        def build():
            try:
                return self.store.read_partition(dataset, year, columns=cols)
            except (KeyError, ValueError):
                # Older partitions were written without every column; fall back
                # to a full read so a schema addition cannot break the reader.
                return self.store.read_partition(dataset, year)

        df = self._cached(key, build, columns=frozenset(cols))
        return self._project(df, columns)

    def _panel_day(self, dataset: str, date: str, columns: list[str]) -> pd.DataFrame:
        """One session of ``dataset``, projected to ``columns``.

        ``trade_date`` must be among the requested columns: the slice is applied
        after the projection, and a projection that drops the key turns a missing
        column into a KeyError three frames later.
        """
        if "trade_date" not in columns:
            raise ValueError(f"{dataset}: trade_date is required to slice a session")
        df = self._year_frame(dataset, date[:4], columns)
        if df.empty:
            return df
        return df[df["trade_date"].astype(str) == date]

    def daily(self, date: str, columns: list[str] | None = None) -> pd.DataFrame:
        cols = columns or ["ts_code", "trade_date", "open", "high", "low", "close",
                           "pre_close", "vol", "amount"]
        # 一次个股 profile 实测把同一天的横截面读三遍（56 ms/遍）：``_peers``
        # 里两遍、别的段一遍。日线在收盘落库后当天不会再变，按 (日期, 列集合)
        # 放进现有缓存即可——列集合进 key，免得只要 6 列的请求拿到 9 列帧或反过来。
        # 日更改 manifest 会触发 invalidate，不会把昨天的截面端到今天。
        key = f"day:daily:{date}:{','.join(sorted(cols))}"
        return self._cached(key, lambda: self._panel_day("daily", date, cols))

    def cb_daily(self, date: str, columns: list[str] | None = None) -> pd.DataFrame:
        """Convertible-bond bars for one session.

        A separate dataset, not a filter on ``daily``: the vendor keeps bonds in
        ``cb_daily`` and a bond simply has no row in ``daily``. Reading the wrong
        one returns an empty frame, and an empty frame is indistinguishable from
        "this bond did not trade" — the failure mode `symbols.py` was created for.
        ``vol`` is already in bonds (张) here, not in 手, so the caller must not
        apply the stock multiplier.
        """
        cols = columns or ["ts_code", "trade_date", "open", "high", "low", "close",
                           "pre_close", "vol", "amount"]
        return self._panel_day("cb_daily", date, cols)

    def fund_daily(self, date: str, columns: list[str] | None = None) -> pd.DataFrame:
        """Fund bars for one session.

        Units match ``daily`` — measured on 2026-10-29, ``amount*1000/(vol*close)``
        is 101 for a fund and 99 for a stock, so ``vol`` is 手 in both and the
        share multiplier applies unchanged. Only the core ETF universe lives here
        (18 names); ``daily`` holds none of them, so the two never overlap.
        """
        cols = columns or ["ts_code", "trade_date", "open", "high", "low", "close",
                           "pre_close", "vol", "amount"]
        return self._panel_day("fund_daily", date, cols)

    def quote_dataset(self, symbol: str) -> str:
        """Which file carries this instrument's daily bars.

        Three namespaces, three datasets, and reading the wrong one returns an
        empty frame — indistinguishable from "this instrument did not trade".
        That is the failure `symbols.py` exists to prevent, so the mapping is
        derived from the code rather than guessed per call site.
        """
        if is_convertible(symbol):
            return "cb_daily"
        if str(symbol)[:2] in ETF_PREFIXES:
            return "fund_daily"
        return "daily"

    def _quotes(self, date: str, symbol: str) -> pd.DataFrame:
        dataset = self.quote_dataset(symbol)
        if dataset == "cb_daily":
            return self.cb_daily(date)
        if dataset == "fund_daily":
            return self.fund_daily(date)
        return self.daily(date)

    def limits(self, date: str) -> pd.DataFrame:
        # 与 daily() 同一个问题：整年分区按 object dtype 的 trade_date 切片，
        # 每次 ~49 ms，而涨跌停价在收盘落库后当天不变。guard 的价格带判定、
        # snapshot 和个股页都会读到，缓存一次全受益。
        def build():
            df = self._year_frame("stk_limit", date[:4],
                                  ["ts_code", "trade_date", "up_limit", "down_limit"])
            if df.empty:
                return df
            return df[df["trade_date"].astype(str) == date]

        return self._cached(f"day:stk_limit:{date}", build)

    def _attach_adj(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ``adj_close``: the close series with every ex-rights gap removed.

        Unadjusted close is the right number for an order, a limit or a mark —
        those are prices someone can actually pay. It is the wrong number for
        *comparing two dates*: on an ex-date the exchange prints a reference
        price below yesterday's close, so `close[-1] / close[-n]` books a dividend
        or a split as a loss. Measured on 2026-10-27, that error exceeds 2% for
        20% of the market over a 200-day window and flips the sign of the move
        for 2.1% of it — the window MA200, the 52-week range and the one-year
        volatility percentile all live in.

        The adjustment is built from ``pre_close``, not from ``adj_factor``:

        * ``pre_close`` is the price the exchange itself publishes as the
          reference for that session. On an ex-date it already nets out the cash,
          the bonus and the split, which is why ``close / pre_close - 1`` is the
          true overnight return on every day including ex-dates. Measured against
          4,611 dividend plans in 2026, it agrees 4,610 times.
        * ``adj_factor`` is a vendor-maintained cumulative series and it is not
          always right. On 2026-10-26 the factor for 920227.BJ fell from 1073.15
          to 1.1441 while the price moved 47.96 -> 47.91; anchoring on it turned
          a flat month into a -99.9% return. The quality report had already
          flagged 15 such rows for 2026 and nothing read the report.

        The series is **anchored to the last row**, so the final ``adj_close``
        equals the raw ``close`` — the number on the screen. Ratios inside the
        window are exact; the level stays quotable.
        """
        if df.empty or "close" not in df.columns:
            return df
        out = df.copy()
        out["trade_date"] = out["trade_date"].astype(str)
        out = out.sort_values(["ts_code", "trade_date"], kind="mergesort")

        close = pd.to_numeric(out["close"], errors="coerce")
        if "pre_close" in out.columns:
            pre = pd.to_numeric(out["pre_close"], errors="coerce")
        else:
            pre = pd.Series(float("nan"), index=out.index)
        ret = close / pre.where(pre > 0) - 1
        # The first row of a window has no information about the step before it,
        # so it is flat. Without this the whole series shifts by that first day's
        # move and "no corporate action" would no longer mean "adj == close".
        first = ~out["ts_code"].duplicated(keep="first")
        ret = ret.mask(first, 0.0).fillna(0.0)

        level = (1.0 + ret).groupby(out["ts_code"], sort=False).cumprod()
        last_level = level.groupby(out["ts_code"], sort=False).transform("last")
        anchor = close.groupby(out["ts_code"], sort=False).transform("last")
        scale = (anchor / last_level.where(last_level > 0)).fillna(0.0)

        out["adj_close"] = level * scale
        ratio = out["adj_close"] / close.where(close > 0)
        for col in ("adj_high", "adj_low", "adj_open"):
            src = col.replace("adj_", "")
            if src in out.columns:
                out[col] = pd.to_numeric(out[src], errors="coerce") * ratio
        return out.reset_index(drop=True)

    def history(self, symbol: str, as_of: str, days: int = 60) -> pd.DataFrame:
        """Bars up to and including ``as_of`` — never after it.

        The upper bound is not decoration. Without it a replay at 2023-01-20
        reads the whole 2023 partition and ``tail(days)`` happily returns April,
        which turns ATR, momentum and every risk number computed from this frame
        into a look-ahead. Live use never noticed because ``as_of`` is always the
        last stored session; historical replays and the analog engine do.

        The frame carries both price series: ``close`` (what an order pays) and
        ``adj_close`` (what a return is measured on). See ``_attach_adj``.
        """
        key = f"hist:{symbol}:{as_of}:{days}"
        value = self._cached(key, lambda: self._history_uncached(symbol, as_of, days))
        return value.copy()

    def _history_uncached(self, symbol: str, as_of: str, days: int) -> pd.DataFrame:
        start = self.calendar.last_n(as_of, days)
        if not start:
            return pd.DataFrame()
        frames = []
        dataset = self.quote_dataset(symbol)
        # An index has bars in `index_daily`, not in `daily`; `quote_dataset`
        # only knows the three traded namespaces (bond / fund / stock), so an
        # index fell through to `daily`, came back empty, and the endpoint
        # answered 404 NO_BARS for a symbol whose history was on disk the whole
        # time (measured 2026-10-31 on 000001.SH). Routed here rather than in
        # `quote_dataset` because nothing else reads an index: `snapshot` and
        # `instrument` would start building price bands for something that is
        # not tradable.
        if dataset == "daily" and symbol in INDEX_CODES:
            dataset = "index_daily"
        for year in sorted({d[:4] for d in start}):
            df = self._year_frame(dataset, year,
                                  ["ts_code", "trade_date", "open", "high", "low",
                                   "close", "pre_close", "vol", "amount"])
            if df.empty:
                continue
            stamp = df["trade_date"].astype(str)
            frames.append(df[(df["ts_code"] == symbol)
                             & (stamp >= start[0]) & (stamp <= as_of)])
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values("trade_date").tail(days).reset_index(drop=True)
        return self._attach_adj(out)

    def basic(self) -> pd.DataFrame:
        return self._cached("stock_basic", lambda: self.store.read("stock_basic"))

    # daily_basic columns a per-name question actually reads. Kept in one place so
    # the parquet projection and the profile agree on what exists.
    BASIC_COLUMNS = ("ts_code", "trade_date", "pe", "pe_ttm", "pb", "ps_ttm",
                     "dv_ttm", "total_mv", "circ_mv", "total_share", "float_share",
                     "free_share", "turnover_rate", "turnover_rate_f", "volume_ratio")

    def basic_history(self, symbol: str, as_of: str, days: int = 750) -> pd.DataFrame:
        """`daily_basic` rows for one symbol, up to and including ``as_of``.

        This is the valuation series: PE/PB/PS/dividend yield/market cap/turnover.
        It has been on disk since the P1 backfill and was read by nothing, which
        meant the desk could tell you a stock's price but not whether that price
        was expensive.
        """
        key = f"basichist:{symbol}:{as_of}:{days}"
        value = self._cached(
            key, lambda: self._basic_history_uncached(symbol, as_of, days))
        return value.copy()

    def _basic_history_uncached(self, symbol: str, as_of: str, days: int) -> pd.DataFrame:
        start = self.calendar.last_n(as_of, days)
        if not start:
            return pd.DataFrame()
        frames = []
        for year in sorted({d[:4] for d in start}):
            df = self._year_frame("daily_basic", year, list(self.BASIC_COLUMNS))
            if df.empty:
                continue
            stamp = df["trade_date"].astype(str)
            frames.append(df[(df["ts_code"] == symbol)
                             & (stamp >= start[0]) & (stamp <= as_of)])
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values("trade_date").reset_index(drop=True)

    def basic_day(self, date: str) -> pd.DataFrame:
        """The whole cross-section of `daily_basic` for one session.

        Cached: the per-name profile reads it once for the symbol and once for
        the industry comparison, and a year partition is not free.
        """
        key = f"basic_day:{date}"

        def build():
            df = self._year_frame("daily_basic", date[:4], list(self.BASIC_COLUMNS))
            if df.empty:
                return df
            return df[df["trade_date"].astype(str) == date]

        return self._cached(key, build)

    def basic_row(self, symbol: str, date: str) -> dict:
        """One day's `daily_basic` row for one symbol, as a plain dict."""
        df = self.basic_day(date)
        if df.empty:
            return {}
        row = df[df["ts_code"] == symbol]
        return {} if row.empty else row.iloc[0].to_dict()

    def circ_mv(self, date: str) -> pd.Series:
        """Float market cap in CNY for one session, indexed by ``ts_code``.

        ``daily_basic.circ_mv`` is quoted in 万元, and the ×10,000 lives **here and
        only here**. It used to live in two places — ``fill._circ_mv`` and
        ``tape._seal_strength`` — and both carried a comment claiming to be the
        only one. A factor of 10,000 applied twice is indistinguishable from a
        stock whose float is a hundredth of its real size, and that number then
        gets *ranked* (封单/流通市值) and quoted as a strength reading.

        Empty series when the cross-section is missing; callers report that they
        fell back rather than treating it as "no float".
        """
        key = f"circmv:{date}"

        def build() -> pd.Series:
            try:
                frame = self.store.read("daily_basic", date, date,
                                        columns=["ts_code", "circ_mv"])
            except (KeyError, ValueError, FileNotFoundError):
                return pd.Series(dtype="float64")
            if frame is None or frame.empty or "circ_mv" not in frame.columns:
                return pd.Series(dtype="float64")
            series = pd.to_numeric(frame["circ_mv"], errors="coerce") * 1e4
            series.index = frame["ts_code"].astype(str)
            series = series[(series > 0) & series.notna()]
            return series.sort_index()

        return self._cached(key, build)

    def flow_history(self, symbol: str, as_of: str, days: int = 20) -> pd.DataFrame:
        """Per-symbol money flow (主力/超大单/大单 net) for the last ``days`` rows."""
        cols = ["ts_code", "trade_date", "buy_elg_amount", "sell_elg_amount",
                "buy_lg_amount", "sell_lg_amount", "buy_md_amount", "sell_md_amount",
                "buy_sm_amount", "sell_sm_amount", "net_mf_amount"]
        key = f"flow:{symbol}:{as_of}:{days}"
        value = self._cached(key, lambda: self._flow_uncached(symbol, as_of, days, cols))
        return value.copy()

    def _flow_uncached(self, symbol: str, as_of: str, days: int,
                       cols: list[str]) -> pd.DataFrame:
        start = self.calendar.last_n(as_of, days)
        if not start:
            return pd.DataFrame()
        frames = []
        for year in sorted({d[:4] for d in start}):
            try:
                df = self._year_frame("moneyflow", year, cols)
            except (KeyError, FileNotFoundError):
                return pd.DataFrame()
            if df.empty:
                continue
            stamp = df["trade_date"].astype(str)
            frames.append(df[(df["ts_code"] == symbol)
                             & (stamp >= start[0]) & (stamp <= as_of)])
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values("trade_date").reset_index(drop=True)

    def margin_row(self, symbol: str, date: str) -> dict:
        """融资融券余额 for one symbol — the leverage side of the same crowd.

        Falls back to the most recent session that has a row. The exchanges
        publish the margin detail after the close and often not until the evening,
        so between 15:00 and publication a same-day lookup returns nothing — and
        "nothing" was being rendered as "not a margin target", which is a
        different fact about a different thing. A balance is a stock, not a flow:
        yesterday's number is still the right number if it is labelled as such.

        The returned dict carries `_lag_sessions` so the caller can say how old it
        is rather than presenting it as current.
        """
        for year in (date[:4], str(int(date[:4]) - 1)):
            try:
                df = self._year_frame("margin_detail", year,
                                      ["ts_code", "trade_date", "rzye", "rzmre",
                                       "rzche", "rqye", "rzrqye"])
            except (KeyError, FileNotFoundError):
                continue
            if df.empty:
                continue
            mine = df[(df["ts_code"] == symbol)
                      & (df["trade_date"].astype(str) <= date)].copy()
            if mine.empty:
                continue
            mine["trade_date"] = mine["trade_date"].astype(str)
            mine = mine.sort_values("trade_date")
            row = mine.iloc[-1].to_dict()
            row["_lag_sessions"] = len(
                [d for d in self.calendar.between(str(row["trade_date"]), date)
                 if d != str(row["trade_date"])]
            ) if self.calendar else None
            return row
        return {}

    def namechange(self) -> pd.DataFrame:
        return self._cached("namechange", lambda: self.store.read("namechange"))

    # ------------------------------------------------------------ announcements
    def announcements(self, symbol: str, day: str, limit: int = 6) -> list[dict]:
        """Latest filings by ``symbol`` on or before ``day``, newest first.

        Day precision, and that is the interface's limit rather than a choice:
        cninfo returns `announcementTime` as midnight for most filers. An
        announcement dated today may therefore have arrived after the close, so a
        replay at 14:00 using this table can look ahead by hours. The block says
        `pit_precision: "day"` so the reader knows which side of that it is on.
        """
        # One cached frame, sliced per symbol. Keying the cache by symbol made
        # every new name re-read the whole table — 230k rows for six filings.
        frame = self._anns_frame()
        if frame is None or frame.empty:
            return []
        mine = frame[(frame["ts_code"] == symbol) & (frame["pub_date"] <= str(day))]
        if mine.empty:
            return []
        mine = mine.sort_values(["pub_date", "announcement_id"], ascending=[False, False])
        return mine.head(limit)[self._anns_columns(mine)].to_dict("records")

    def _anns_frame(self) -> pd.DataFrame:
        """The announcement table, read once and cached for every caller."""
        def build():
            try:
                df = self.store.read("anns")
            except (KeyError, FileNotFoundError, ValueError):
                return pd.DataFrame()
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.copy()
            df["pub_date"] = df["pub_date"].astype(str)
            df["ts_code"] = df["ts_code"].astype(str)
            df["announcement_id"] = df["announcement_id"].astype(str)
            return df

        return self._cached("anns_all", build)

    @staticmethod
    def _anns_columns(frame: pd.DataFrame) -> list[str]:
        return [c for c in ("announcement_id", "pub_date", "title", "category",
                            "kind", "stage", "paperwork", "pdf_url", "size_kb")
                if c in frame.columns]

    def announcement(self, announcement_id: str) -> dict | None:
        """One filing's metadata by id, or None when the table has no such row.

        Needed because the reader arrives holding an id — from a profile, from a
        news hit — and not a symbol and a date. Same cached frame as
        ``announcements``; a 230k-row table is not re-read to find one row.
        """
        if not isinstance(announcement_id, str) or not announcement_id.isdigit():
            return None
        frame = self._anns_frame()
        if frame is None or frame.empty:
            return None
        mine = frame[frame["announcement_id"] == announcement_id]
        if mine.empty:
            return None
        row = mine.iloc[0]
        out = {c: row.get(c) for c in self._anns_columns(frame)}
        out["ts_code"] = str(row.get("ts_code") or "")
        out["name"] = str(row.get("name") or "") if "name" in mine.columns else ""
        return out

    def minutes(self, symbol: str, day: str) -> pd.DataFrame:
        """5-minute bars for one name on one session, oldest first.

        Empty is a normal answer: the minute table is filled forward for the names
        the daily job watches, so a stock nobody follows has no rows. Readers must
        treat empty as "not collected", never as "no trading".
        """
        try:
            df = self.store.read_partition("stk_mins", day)
        except (KeyError, FileNotFoundError, ValueError):
            return pd.DataFrame()
        if df is None or df.empty or "ts_code" not in df.columns:
            return pd.DataFrame()
        mine = df[df["ts_code"].astype(str) == symbol].copy()
        if mine.empty:
            return pd.DataFrame()
        return mine.sort_values("trade_time").reset_index(drop=True)

    def announcement_text(self, announcement_id: str) -> dict | None:
        """Cached full text for one filing, or None when it was never downloaded.

        Text lives beside the data rather than inside it: a 200-page annual report
        is 500 KB of prose, and rewriting a 200 MB parquet to append it would cost
        more than the text is worth. `scripts/fetch_anns_text.py` fills the cache,
        and `service.adapters.anns_text` owns the layout so that the filler and
        the reader cannot disagree about where it went.
        """
        return anns_text.read(self.store.root, announcement_id)

    @property
    def universe(self) -> PitUniverse:
        """Listing status + name history, rebuilt when the store moves.

        It used to be built once and cached forever, on the assumption that the
        frames behind it do not change. They change every evening: the 17:30
        job rewrites ``stock_basic`` and ``namechange``, and a name can be
        flagged ST then. ``is_st`` has a 60 s TTL, but expiring it only
        re-filtered the *same* frame — so the band stayed 10% until restart,
        which is precisely the failure the TTL's comment says it prevents.
        """
        self._maybe_invalidate()
        universe = self._universe
        if universe is not None:
            return universe
        # Read the frames before taking ``_universe_lock``: ``basic()`` goes
        # through ``_cached``, which takes ``_cache_condition``, and
        # ``invalidate()`` takes these two in the opposite order.
        basic = self.basic()
        namechange = self.namechange()
        with self._universe_lock:
            if self._universe is None:
                # Pass ``self.ttl`` rather than letting ``PitUniverse`` read the
                # config: a test that pins the repository TTL (conftest uses
                # 3600 s so frames do not expire mid-suite) must get the same
                # window for the ST flags, or the two halves disagree only in
                # the tests — the one place where nobody notices.
                self._universe = PitUniverse(basic, namechange, st_ttl=self.ttl)
                self._universe_built_at = datetime.now().isoformat(timespec="seconds")
            return self._universe

    def industry_of(self, symbol: str) -> str:
        def build():
            basic = self.basic()
            if basic.empty or "industry" not in basic.columns:
                return {}
            return dict(zip(basic["ts_code"].astype(str), basic["industry"].astype(str)))

        return self._cached("industry", build).get(symbol, "未知")

    def names(self) -> dict[str, str]:
        def build():
            basic = self.basic()
            return dict(zip(basic["ts_code"].astype(str), basic["name"].astype(str))) \
                if not basic.empty else {}

        return self._cached("names", build)

    def fund_meta(self) -> dict[str, tuple[str, str]]:
        """``ts_code -> (name, fund_type)`` for the ETF universe.

        The ETF opportunity set is small (the core file lists 18 exposures) and
        its settlement rule differs by product, not by exchange: a bond, money,
        cross-border or gold ETF settles T+0, a domestic equity ETF settles T+1.
        The vendor's ``fund_type`` is the only field that states this, so it is
        read once and cached rather than guessed from the code prefix — 159920
        is a Hong Kong ETF while 159915 is ChiNext, and both start with 159.
        """
        def build():
            try:
                frame = self.store.read("fund_basic")
            except (FileNotFoundError, KeyError):
                return {}
            if frame.empty or "fund_type" not in frame.columns:
                return {}
            # ``frame.get("name", "")`` returns a *scalar* when the column is
            # absent, and ``zip(..., "")`` is exhausted immediately — so a store
            # whose fund_basic carries no name column produced an empty map, and
            # every bond/money/QDII/gold ETF silently became T+1. A missing name
            # is a missing label; it is not a missing settlement rule.
            names = (frame["name"] if "name" in frame.columns
                     else pd.Series([""] * len(frame), index=frame.index))
            return {str(code): (str(name or ""), str(kind or ""))
                    for code, name, kind in zip(frame["ts_code"], names, frame["fund_type"])}

        return self._cached("fundmeta", build)

    # Products whose units can be sold on the day they are bought. Equity ETFs
    # (including sector and dividend ETFs) are T+1, which is why this cannot be
    # "all ETFs".
    T0_FUND_TYPES = frozenset({"债券型", "货币型", "QDII"})

    def is_t0_fund(self, symbol: str) -> bool:
        meta = self.fund_meta().get(symbol)
        if meta is None:
            return False
        name, kind = meta
        if kind in self.T0_FUND_TYPES:
            return True
        # The vendor labels several cross-border ETFs 股票型 and puts QDII in the
        # name; 518xxx is the gold range, which settles T+0 on both exchanges.
        upper = name.upper()
        return "QDII" in upper or "黄金" in name or symbol.startswith("518")

    # ------------------------------------------------------- industry taxonomy
    def sw_table(self) -> pd.DataFrame:
        """申万 L1/L2/L3 per listed name, as one frame (5,922 rows, one file)."""
        def build():
            try:
                df = self.store.read("index_member")
            except (KeyError, FileNotFoundError, ValueError):
                return pd.DataFrame()
            if df is None or df.empty:
                return pd.DataFrame()
            keep = [c for c in ("ts_code", "l1_code", "l1_name", "l2_code", "l2_name",
                                "l3_code", "l3_name", "in_date") if c in df.columns]
            out = df[keep].copy()
            for col in ("ts_code", "l1_code", "l2_code", "l3_code"):
                if col in out.columns:
                    out[col] = out[col].astype(str)
            return out.drop_duplicates(subset=["ts_code"], keep="last")

        return self._cached("sw_table", build)

    def sw_of(self, symbol: str) -> dict:
        """申万 three levels for one name, or empty when the table has no row."""
        table = self.sw_table()
        if table.empty or "ts_code" not in table.columns:
            return {}
        row = table[table["ts_code"] == symbol]
        if row.empty:
            return {}
        row = row.iloc[0]
        return {
            "l1": str(row.get("l1_name") or ""),
            "l2": str(row.get("l2_name") or ""),
            "l3": str(row.get("l3_name") or ""),
            "in_date": str(row.get("in_date") or "") or None,
        }

    def industry_peers(self, symbol: str, min_size: int = 8,
                       max_size: int = 60) -> dict:
        """The finest 申万 level that still has enough names to be a benchmark.

        L3 is the honest comparison — `城商行Ⅲ` (17 names) says more than `银行`
        (42): joint-stock banks and city commercial banks do not trade alike. But
        77 of the 337 L3 buckets hold five names or fewer, and a median over four
        companies is not a statistic. So the level is picked per name: the most
        specific one whose membership lands in [min_size, max_size], with L1 as
        the stated fallback rather than a silent swap.

        Membership is today's classification. `index_member_all` carries `in_date`
        but no `out_date` rows and no history, so a name that changed sector is
        measured against its current one, not the one it had at the time.
        """
        table = self.sw_table()
        if table.empty or "ts_code" not in table.columns:
            return {"available": False, "reason": "index_member 尚未回填"}
        row = table[table["ts_code"] == symbol]
        if row.empty:
            return {"available": False, "reason": f"{symbol} 不在申万分类表里"}
        row = row.iloc[0]

        skipped: list[dict] = []
        for level in ("l3", "l2", "l1"):
            name = str(row.get(f"{level}_name") or "")
            if not name:
                continue
            members = table[table[f"{level}_name"].astype(str) == name]
            size = len(members)
            if min_size <= size <= max_size:
                return {
                    "available": True,
                    "level": level.upper(),
                    "name": name,
                    "size": size,
                    "members": set(members["ts_code"].astype(str)),
                    # A narrower bucket exists but was too small to summarise.
                    # Naming it keeps "peers" from looking like a choice the
                    # reader would have made: 工商银行 is one of six state-owned
                    # banks, and the median it is shown against is forty-two banks.
                    "skipped": skipped,
                    "reason": None,
                }
            skipped.append({"level": level.upper(), "name": name, "size": size,
                            "why": "太小" if size < min_size else "太大"})
        # Nothing fits the window: fall back to L1 and say so, because a silent
        # swap to a broader bucket is how "peers" quietly becomes "the market".
        l1 = str(row.get("l1_name") or "")
        members = table[table["l1_name"].astype(str) == l1] if l1 else table.iloc[0:0]
        return {
            "available": bool(l1),
            "level": "L1",
            "name": l1,
            "size": len(members),
            "members": set(members["ts_code"].astype(str)),
            "skipped": skipped,
            "reason": f"没有一级落在 {min_size}..{max_size} 之间，已退回 L1",
        }

    # --------------------------------------------------------------- instruments
    def instrument(self, symbol: str, as_of: str) -> Instrument:
        name = self.names().get(symbol, "")
        # One derivation, in `adapters.symbols`: the board decides the band, the
        # lot, the tick and the settlement, so it is a property of the security.
        # This method and `universe.browse` each had their own copy, and the
        # display copy collapsed every 沪深 name to `null`.
        board = Board(board_of(symbol))
        is_cb = board is Board.CB
        is_etf = board is Board.ETF
        is_st = False if is_cb else self.universe.is_st(symbol, as_of)
        # One definition of the band, in `symbols.py`, so a board rule cannot be
        # re-derived (and quietly re-derived differently) at a call site.
        # ``as_of`` goes with it: the main-board ST band was 5% before
        # 2026-07-06 and 10% after, so a historical query has to be answered
        # with the rule that was in force on the day it asks about.
        band = limit_pct(symbol, is_st=is_st, name=name, as_of=as_of)
        limit_pct_value = D(str(band))
        return Instrument(
            symbol=symbol,
            name=name or self.names().get(symbol, ""),
            board=board,
            is_etf=is_etf,
            # Bonds trade in 10-unit lots with a 0.001 tick, return T+0, and pay
            # neither stamp duty nor transfer fee.
            min_qty=10 if is_cb else 200 if board == Board.STAR else 100,
            qty_step=10 if is_cb else 1 if board in (Board.STAR, Board.BSE) else 100,
            price_tick=CB_PRICE_TICK if is_cb else PRICE_TICK,
            # Bond / money / cross-border / gold ETFs settle T+0; domestic equity
            # ETFs do not. Treating the whole class as T+1 overstated the
            # overnight risk of five of the core book's eighteen exposures,
            # including the defensive leg.
            t_plus=0 if (is_cb or (is_etf and self.is_t0_fund(symbol))) else 1,
            limit_pct=limit_pct_value,
            stamp_duty=(D("0") if (is_etf or is_cb)
                        else _cost_rate("stamp_duty_sell")),
            transfer_fee=(D("0") if (is_etf or is_cb)
                          else _cost_rate("transfer_fee")),
            is_st=is_st,
        )

    def snapshot(self, symbol: str, as_of: str | None = None) -> Snapshot | None:
        date = as_of or self.last_trading_day()
        if not date:
            return None
        dataset = self.quote_dataset(symbol)
        frame = self._quotes(date, symbol)
        row = frame[frame["ts_code"] == symbol]
        if row.empty:
            return None
        r = row.iloc[0]
        lim = self.limits(date)
        lrow = lim[lim["ts_code"] == symbol]
        inst = self.instrument(symbol, date)
        pre_close = _dec(r.get("pre_close"))
        up = _dec(lrow.iloc[0]["up_limit"]) if not lrow.empty else D("0")
        down = _dec(lrow.iloc[0]["down_limit"]) if not lrow.empty else D("0")
        if up <= 0 or down <= 0:
            # One implementation of "band from the previous close":
            # ``Instrument.band`` quantises to the instrument's own tick with
            # half-up rounding, which is what the exchange prints. A second copy
            # here had drifted once already (half-even on a 9.95 close turns a
            # 10.95 ceiling into 10.94, and a buy the market allowed is refused
            # by one fen). A bond's 0.001 tick comes from the instrument too.
            derived_up, derived_down = inst.band(pre_close)
            up = up if up > 0 else derived_up
            down = down if down > 0 else derived_down
        close = _dec(r.get("close"))
        suspended = bool(r.get("vol") is not None and float(r.get("vol") or 0) <= 0
                         and close == pre_close)
        return Snapshot(
            symbol=symbol, trade_date=date, last=close, pre_close=pre_close,
            open=_dec(r.get("open")), high=_dec(r.get("high")), low=_dec(r.get("low")),
            up_limit=up, down_limit=down,
            # ``daily`` and ``fund_daily`` count 手 (100 shares/units);
            # ``cb_daily`` already counts bonds (张).
            volume=float(r.get("vol") or 0) * (1.0 if dataset == "cb_daily" else 100.0),
            amount=float(r.get("amount") or 0) * 1000.0,
            suspended=suspended, source=dataset,
        )

    # ------------------------------------------------------- convertible events
    def cb_master(self) -> dict[str, dict]:
        """``ts_code -> {delist_date, maturity_date, par}`` from the bond master.

        The master is the only place the desk can learn when a bond stops
        existing. A forced-redemption announcement is not in the store (the
        announcement feed carries titles, not a redemption schedule), so the
        delist date is the trigger and the last two weeks before it are blocked
        for new buys rather than guessed at.
        """
        def build():
            try:
                frame = self.store.read("cb_basic")
            except (FileNotFoundError, KeyError):
                return {}
            if frame.empty:
                return {}
            out: dict[str, dict] = {}
            for _, row in frame.iterrows():
                code = _text(row.get("ts_code"))
                if not code:
                    continue
                out[code] = {
                    "delist_date": _text(row.get("delist_date")),
                    "maturity_date": _text(row.get("maturity_date")),
                    "par": _dec(row.get("par"), "100"),
                    "coupons": _parse_rate_clause(_text(row.get("rate_clause"))),
                }
            return out

        return self._cached("cbmaster", build)

    def cb_coupons(self, day: str) -> list[CBCoupon]:
        """Bonds paying interest on ``day``.

        The pay date is the **end** of a rate period: a clause reading
        `19980803-19990802,票面利率:1.00%` pays on 19990802, not on the start
        date. Using the start would credit a year of interest a year early, and
        every later coupon would be shifted too.
        """
        out: list[CBCoupon] = []
        target = str(day)
        for code, info in self.cb_master().items():
            for _start, end, rate in info["coupons"]:
                if end == target:
                    out.append(CBCoupon(
                        symbol=code,
                        pay_date=datetime.strptime(target, "%Y%m%d").date(),
                        par=info["par"],
                        rate=rate,
                    ))
        return out

    def cb_maturities(self, day: str) -> list[CBMaturity]:
        """Bonds whose last trading session is ``day``."""
        out: list[CBMaturity] = []
        for code, info in self.cb_master().items():
            target = info["delist_date"] or info["maturity_date"]
            if target and target == str(day) and len(target) == 8:
                out.append(CBMaturity(
                    symbol=code,
                    delist_date=datetime.strptime(target, "%Y%m%d").date(),
                    price=info["par"],
                ))
        return out

    def cb_days_to_delist(self, symbol: str, day: str) -> int | None:
        """Sessions from ``day`` to the last session, inclusive. None if unknown."""
        info = self.cb_master().get(symbol)
        if not info:
            return None
        target = info["delist_date"] or info["maturity_date"]
        if not target or len(target) != 8:
            return None
        if target <= str(day):
            return 0
        return len(self.calendar.between(str(day), target))

    # ------------------------------------------------------- corporate actions
    def _dividend_frame(self) -> pd.DataFrame:
        """The whole dividend table, loaded once per process.

        It is one small file (a few 10k rows for four years) and the alternative
        is re-reading it on every rollover, which happens on every API call that
        touches the book. "Once per process" is why the store probe runs here
        too: ``invalidate()`` is what makes it once per data version.
        """
        self._maybe_invalidate()
        if self._dividends is None:
            try:
                frame = self.store.read("dividend")
            except (FileNotFoundError, KeyError):
                frame = pd.DataFrame()
            if not frame.empty and "ex_date" in frame.columns:
                frame = frame.assign(ex_date=frame["ex_date"].astype(str))
            self._dividends = frame
        return self._dividends

    def actions(self, day: str) -> list[CorporateAction]:
        """Implemented dividend / bonus plans whose ex-date is ``day``.

        Only ``div_proc == "实施"`` rows count: a plan that was merely proposed
        never moved the price. Rights issues (``stk_co_rate``) are excluded — they
        require paying in cash, which this account never does.
        """
        from datetime import date as _date

        frame = self._dividend_frame()
        if frame.empty or "ex_date" not in frame.columns:
            return []
        rows = frame[frame["ex_date"] == str(day)]
        out: list[CorporateAction] = []
        # One physical event, sometimes two rows. Tushare carries the same
        # ex-date twice when a plan is announced against two reporting periods
        # (000908.SZ on 2026-03-11: end_date 20250429 and 20260113, both 10转10).
        # Applying both mints shares: the tape-implied ratio is computed once per
        # action, so two actions hand out 1.46x twice instead of once. Collapse
        # the rows and keep the largest leg of each kind.
        seen: dict[str, dict] = {}
        for _, row in rows.iterrows():
            if "div_proc" in rows.columns and str(row.get("div_proc")) != "实施":
                continue
            symbol_key = str(row.get("ts_code") or "")
            entry = seen.setdefault(symbol_key, {"cash": D("0"), "stock": D("0"),
                                                 "transfer": D("0")})
            entry["cash"] = max(entry["cash"], _dec(row.get("cash_div")))
            entry["stock"] = max(entry["stock"], _dec(row.get("stk_div")))
            entry["transfer"] = max(entry["transfer"], _dec(row.get("stk_bo_rate")))
        for symbol_key, legs in seen.items():
            cash = legs["cash"]
            stock = legs["stock"]
            transfer = D("0") if stock > 0 else legs["transfer"]
            if cash <= 0 and stock <= 0 and transfer <= 0:
                continue
            out.append(CorporateAction(
                symbol=symbol_key,
                ex_date=_date(int(day[:4]), int(day[4:6]), int(day[6:8])),
                cash_div_per_share=cash,
                stock_div_per_share=stock,
                transfer_per_share=transfer,
            ))
        return out

    def traded_volume(self, symbol: str, date: str) -> int:
        """Units traded on ``date`` — shares for a stock, bonds for a convertible.

        One dictionary per session, cached: `bar()` needs the previous session's
        turnover for every candidate it prices, and filtering the year frame per
        symbol is an O(rows) operation that profile/observation turn into a
        multi-minute scan over five thousand names.
        """
        dataset = self.quote_dataset(symbol)
        key = f"vol:{dataset}:{date}"

        def build() -> dict[str, int]:
            df = self._panel_day(dataset, date, ["ts_code", "trade_date", "vol"])
            if df.empty:
                return {}
            scale = 1 if dataset == "cb_daily" else 100
            out: dict[str, int] = {}
            for code, vol in zip(df["ts_code"], df["vol"]):
                try:
                    number = float(vol)
                except (TypeError, ValueError):
                    number = 0.0
                out[str(code)] = 0 if math.isnan(number) else int(number) * scale
            return out

        return self._cached(key, build).get(symbol, 0)

    def bar(self, symbol: str, as_of: str | None = None) -> tuple[Bar, Instrument] | None:
        snap = self.snapshot(symbol, as_of)
        if snap is None:
            return None
        inst = self.instrument(symbol, snap.trade_date)
        # Capacity at the open can only be judged on liquidity that was already
        # printed: the previous session's volume. Today's volume is a 15:00 fact.
        prev_day = self.calendar.prev(snap.trade_date)
        prior_volume = self.traded_volume(symbol, prev_day) if prev_day else 0
        bar = Bar.production(
            symbol=symbol,
            ts=datetime.strptime(snap.trade_date, "%Y%m%d").replace(hour=15),
            open=snap.open, high=snap.high, low=snap.low, close=snap.last,
            pre_close=snap.pre_close, volume=int(snap.volume),
            amount=D(str(snap.amount)),
            up_limit=snap.up_limit, down_limit=snap.down_limit,
            suspended=snap.suspended,
            prior_volume=prior_volume,
            # A stored bar's ``open`` is the 09:30 auction print, so "at the
            # ceiling" means the board opened sealed.
            priced_at="open",
        )
        return bar, inst

    # ------------------------------------------------------------- indicators
    def atr(self, symbol: str, as_of: str, days: int = 20) -> Decimal:
        hist = self.history(symbol, as_of, days + 1)
        if len(hist) < 5:
            return D("0")
        # Adjusted series: `adj_close` ends at today's close, so the ATR is still
        # a price in today's terms, and a split inside the window no longer shows
        # up as a two-day doubling of true range.
        hi_col = "adj_high" if "adj_high" in hist.columns else "high"
        lo_col = "adj_low" if "adj_low" in hist.columns else "low"
        px_col = "adj_close" if "adj_close" in hist.columns else "close"
        high = hist[hi_col].astype(float)
        low = hist[lo_col].astype(float)
        close = hist[px_col].astype(float)
        prev = close.shift(1)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        return D(str(round(float(tr.tail(days).mean()), 4)))

    def adv(self, symbol: str, as_of: str, days: int = 20) -> float:
        """Mean daily traded value in CNY, matching :meth:`adv_map`."""
        hist = self.history(symbol, as_of, days)
        if hist.empty:
            return 0.0
        return float(pd.to_numeric(hist["amount"], errors="coerce").mean() * 1000.0)

    def pct_change(self, symbol: str, as_of: str, days: int = 20) -> float | None:
        """Return over ``days``, measured on the adjusted series.

        Two dates, so the ex-rights gap has to be removed — otherwise a stock
        that paid a dividend reads as a loser for no reason the model can see.
        """
        hist = self.history(symbol, as_of, days + 1)
        if len(hist) < days + 1:
            return None
        col = "adj_close" if "adj_close" in hist.columns else "close"
        first = float(hist[col].iloc[0])
        last = float(hist[col].iloc[-1])
        if first <= 0:
            return None
        return last / first - 1

    # ---------------------------------------------------------------- panels
    def _panel(self, dataset: str, date: str, days: int) -> pd.DataFrame:
        window = self.calendar.last_n(date, days)
        if not window:
            return pd.DataFrame()
        frames = []
        for year in sorted({d[:4] for d in window}):
            df = self._year_frame(dataset, year,
                                  ["ts_code", "trade_date", "close", "pre_close",
                                   "amount", "vol"])
            if not df.empty:
                frames.append(df[df["trade_date"].astype(str).isin(window)])
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def panel(self, date: str, days: int) -> pd.DataFrame:
        """Daily rows for the last ``days`` trading sessions ending at ``date``.

        Carries ``adj_close`` as well: any cross-sectional comparison of price
        *levels* over the window (momentum, breadth, moving averages) has to be
        on the adjusted series or the ex-dates in the window become fake moves.

        Cached by ``(date, days)``: building the 60-session cross-section costs
        ~156 ms (year-partition read + sort + cumulative adjustment), and the
        profile, regime, screen, exposure and attribution layers all ask for the
        same window in the same minute. Every caller above treats the returned
        frame as read-only (they copy before mutating); a caller that edits it
        in place would poison the cache, so do not start doing that.
        """
        key = f"panel:daily:{date}:{days}"
        return self._cached(
            key, lambda: self._attach_adj(self._panel("daily", date, days)))

    def adv_map(self, date: str, days: int = 20) -> pd.Series:
        key = f"adv:{date}:{days}"

        def build():
            p = self.panel(date, days)
            if p.empty:
                series = pd.Series(dtype="float64")
            else:
                # amount is 千元 in the vendor feed; return CNY.
                series = p.groupby("ts_code")["amount"].mean() * 1000.0
            # Bonds live in their own dataset. Without this line every bond has
            # ADV 0, and `guard.liquidity` reads that as "unknown liquidity" and
            # blocks the buy — the ticket path was closed one layer below the
            # instrument rules.
            bonds = self._panel("cb_daily", date, days)
            if not bonds.empty:
                cb = bonds.groupby("ts_code")["amount"].mean() * 1000.0
                series = pd.concat([series, cb[~cb.index.isin(series.index)]])
            # Funds are the third namespace. `daily` carries none of the core
            # ETFs, so without this every ETF has ADV 0 and guard.liquidity
            # reads that as "unknown" and blocks the buy — the same one-layer-
            # below failure that shut the convertible path.
            funds = self._panel("fund_daily", date, days)
            if not funds.empty:
                fd = funds.groupby("ts_code")["amount"].mean() * 1000.0
                series = pd.concat([series, fd[~fd.index.isin(series.index)]])
            return series

        return self._cached(key, build)

    # ---------------------------------------------------------------- quotes
    def quotes(self, date: str | None = None) -> pd.DataFrame:
        date = date or self.last_trading_day()
        if not date:
            return pd.DataFrame()

        def build():
            return self.store.read_partition("rt_quote", date)

        return self._cached(f"rt_quote:{date}", build)
