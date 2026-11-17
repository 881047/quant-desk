"""Live quotes for the kernel's eyes.

The desk used to answer "what is the price" with yesterday's close. That is fine
for a decision made at 18:00 about tomorrow's open, and wrong for everything an
intraday observer needs: whether a stop is currently breached, what the book is
worth right now, how far a limit price sits from the tape.

This module is a small, honest quote layer:

* one cache with a short TTL, so a session that asks about the same names twenty
  times does not hammer the vendor;
* two sources with fallback (EastMoney bulk, then Tencent batch), and a third that
  is not a source at all — the stored daily bar — used only to keep the API
  answering when both vendors fail;
* every quote carries ``source``, ``quote_ts`` and ``age_seconds``, and
  ``is_realtime`` is False whenever the value came from a stored bar. A stale
  price labelled "live" is worse than no price.

Nothing here decides anything. It answers "what is it now", with the receipt.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from service import settings
from service.kernel.clock import SHANGHAI
from service.kernel.models import D

# Continuous trading plus the opening call auction. Outside this window a vendor
# quote is still the last traded price, but it is not "live" in any useful sense.
SESSIONS = ((dtime(9, 15), dtime(11, 30)), (dtime(13, 0), dtime(15, 5)))
DEFAULT_TTL = settings.num("realtime.default_ttl")
STALE_AFTER = settings.num("realtime.stale_after")   # quote labelled stale after this
THS_BATCH = settings.integer("realtime.batch")
THS_TIMEOUT = settings.num("realtime.timeout")

# A source that has failed this many times in a row is skipped for the cooldown.
# EastMoney's push2 endpoint answered "Server disconnected" on every call for an
# afternoon; each batch paid its timeout before falling through to Tencent, and
# the quote tool took 7.6s to return numbers that were available in 0.2s. The
# breaker is not an optimisation of a working path — it stops paying for a path
# that is known to be down.
BREAKER_THRESHOLD = settings.integer("realtime.breaker_threshold")
BREAKER_COOLDOWN = settings.num("realtime.breaker_cooldown")
BREAKER_FILE = Path(__file__).resolve().parents[2] / "data" / "cache" / "breakers.json"


def quote_freshness(age_seconds: float | None) -> dict:
    """One place that states the *deliberate* layering of quote-age thresholds.

    Three numbers answer three different questions. They are not drift and they
    must not be merged — collapsing them would let one layer's risk appetite
    dictate another's:

    * ``tradable``   — ``live_fill.max_quote_age`` (90 s): may this quote move
      cash? An intraday fill is irreversible, so this is the strictest gate.
    * ``measurable`` — ``realtime.stale_after`` (120 s): may this quote answer
      "is a stop breached"? State judgement has an ``unknown`` / fail-closed
      branch, so it can afford a wider window than an actual fill.
    * frame cadence  — ``frame.FRAME_STALE_AFTER`` (360 s): is the capture loop
      itself alive? That measures the batch rhythm (3-minute batches), not one
      quote's price; ``frame.py`` spells out why it cannot reuse 120.

    An age in (90, 120] is therefore refused for a fill while still being used
    to measure a breach — by design, not by accident. Both consumers attach the
    values below to their answers so the difference is auditable rather than a
    silent contradiction. Read-only: no config is written, no threshold changes,
    no vendor call. ``None`` / negative ages are neither tradable nor measurable.

    The 90 s gate is read straight from config instead of importing
    ``live_fill`` here: ``live_fill`` is on the fill path and this module is on
    the quote path, so a module-level import between them would drag pandas into
    live_fill's import chain (and risk a cycle). Both read the same key.
    """
    max_quote_age = float(settings.num("live_fill.max_quote_age"))
    stale_after = float(STALE_AFTER)
    age = float(age_seconds) if age_seconds is not None else None
    return {
        "age_seconds": age,
        "tradable": bool(age is not None and 0 <= age <= max_quote_age),
        "measurable": bool(age is not None and 0 <= age <= stale_after),
        "max_quote_age": max_quote_age,
        "stale_after": stale_after,
        "basis": ("90s 成交门槛（不可逆资金动作）/ 120s 状态判定门槛"
                  "（有 unknown fail-closed 兜底）/ 360s 帧批次节奏；"
                  "三层不可合并，90~120s 的报价状态判定仍可能视为可测"),
    }


_TIMESTAMP_BASES = {
    "eastmoney": "vendor_quote_time",
    "tencent": "vendor_quote_time",
    "ths": "vendor_response_time",
    "daily": "daily_bar",
}


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    last: Decimal
    pre_close: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume: float
    amount: float
    up_limit: Decimal
    down_limit: Decimal
    quote_ts: str | None
    age_seconds: float | None
    source: str
    is_realtime: bool
    suspended: bool = False
    realtime_reason: str | None = None
    # `quote_ts` 是谁的时间。东财 f124 与腾讯行情串携带逐票行情时间；
    # THS 给的是响应 timestamp；日线给的是交易日。缺省时按 source 标注，
    # 不再把三个实时源统一说成 fetched。
    timestamp_basis: str | None = None
    # 五档盘口。涨停那一刻买一量就是封单量——2026-11-02 判断打板可买性时，
    # 手上只有两小时前的池子快照，而这一手数据当时是拿得到的。
    bid1: Decimal | None = None
    bid1_volume: float | None = None
    ask1: Decimal | None = None
    ask1_volume: float | None = None
    bid_volume_5: float | None = None
    ask_volume_5: float | None = None

    @property
    def limit_state(self) -> str | None:
        """`one_word` / `sealed` / `opened` / `none` —— 对打板只有这四种有意义。

        一字板、封住、封过又开、没碰过涨停价。判据是价格不是时间为准：
        `high == up_limit` 才叫"碰过"，`last == up_limit` 才叫"现在封着"。

        ``source="daily"`` 时返回 ``None``：那一条是库里最后一根日线，`last` 与
        `up_limit` 都是**那一根自己的**收盘与涨停价。2026-11-03 09:26 拿到的
        7.86 是 11-02 的收盘价，也确实是 11-02 的涨停价，于是这个属性算出
        ``sealed``，报告据此把 002343 剔成"买不到"——而 11-03 的涨停价是 8.65，
        该票当日最低 7.87、收 8.30。**昨天的封板状态不是今天的。**
        同理，盘后取到的日线也答不了"现在封不封得住"，所以这里不猜。
        要读收盘封板状态，走 ``/market/pools`` 的 ``pool_state.phase=settled``。
        """
        if self.source == "daily":
            return None
        if self.up_limit <= 0 or self.last is None:
            return "none"

        def near(one, other) -> bool:
            return round(float(one), 2) == round(float(other), 2)

        at_limit = near(self.last, self.up_limit)
        touched = self.high is not None and near(self.high, self.up_limit)
        flat = (self.open is not None and near(self.open, self.up_limit)
                and self.low is not None and near(self.low, self.up_limit))
        if at_limit and flat:
            return "one_word"
        if at_limit:
            return "sealed"
        if touched:
            return "opened"
        return "none"

    @property
    def wb_ratio(self) -> float | None:
        """委比：(五档买量 - 五档卖量) / (买量 + 卖量)。只有盘口在时才有值。"""
        bid = self.bid_volume_5
        ask = self.ask_volume_5
        if bid is None or ask is None or (bid + ask) <= 0:
            return None
        return (bid - ask) / (bid + ask)

    @property
    def pct_chg(self) -> float | None:
        if self.pre_close <= 0:
            return None
        return float(self.last / self.pre_close - 1)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "last": str(self.last),
            "pre_close": str(self.pre_close),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "up_limit": str(self.up_limit),
            "down_limit": str(self.down_limit),
            "pct_chg": None if self.pct_chg is None else round(self.pct_chg * 100, 2),
            # 打板的人第一眼要的是"离板还有多远"，此前只有 `profile.tradability`
            # 给这两个数，而那条路要多读 13 个块。放在报价上，一次请求就够。
            # 指数没有涨跌停，`up_limit` 为 0，此处给 None 而不是 -100%。
            "to_up_limit_pct": (
                None if self.last <= 0 or self.up_limit <= 0
                else round(float(self.up_limit / self.last - 1) * 100, 2)),
            "to_down_limit_pct": (
                None if self.last <= 0 or self.down_limit <= 0
                else round(float(self.down_limit / self.last - 1) * 100, 2)),
            "volume": self.volume,
            "amount": self.amount,
            "quote_ts": self.quote_ts,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "timestamp_basis": (self.timestamp_basis
                                or _TIMESTAMP_BASES.get(self.source, "fetched")),
            "source": self.source,
            "is_realtime": self.is_realtime,
            "realtime_reason": self.realtime_reason,
            "suspended": self.suspended,
            "limit_state": self.limit_state,
            # limit_state 为 None 只有一种原因：这条报价是库里的日线，
            # 不是盘中价。说清是哪一种，读者才不会把"没这个字段"当成"没封板"。
            **({} if self.limit_state is not None else {"limit_state_note":
               f"报价来源是 {self.source}（库里的最后一根日线），不是盘中价；"
               "它描述的是那一根的收盘状态，不是现在。要现在的封板状态得用盘中来源，"
               "要收盘封板状态用 /market/pools 的 pool_state.phase=settled"}),
            # 指数没有涨跌停：`up_limit` 是 0，不是"跌到 0"。
            **({} if self.up_limit > 0 else {"limits_note":
               "指数没有涨跌停，up_limit/down_limit 为 0 表示不适用"}),
            # 盘口只在拿到五档时出现。涨停时 `bid1_volume` 就是**封单量**（手），
            # 这是判断"还能不能买进去"最直接的一手数据，`null` 表示这个源没给，
            # 不是"没有封单"。
            "depth": None if self.bid1 is None else {
                "bid1": str(self.bid1),
                "bid1_volume": self.bid1_volume,
                "ask1": None if self.ask1 is None else str(self.ask1),
                "ask1_volume": self.ask1_volume,
                "bid_volume_5": self.bid_volume_5,
                "ask_volume_5": self.ask_volume_5,
                "wb_ratio_pct": (None if self.wb_ratio is None
                                 else round(self.wb_ratio * 100, 2)),
            },
            **({} if self.bid1 is not None else {"depth_note":
               "这个源没有五档盘口（指数 / 停牌 / 盘后无挂单）；不是'没有封单'"}),
        }


def _session_label(clock, *, trading_day: bool, weekday: int) -> str:
    """把时钟翻译成一个名字，一个窗口一个名字。

    ``in_session`` 回答不了"现在能不能按现价成交"：09:15–09:30 与 14:57–15:00
    都收单但不连续撮合，而 15:00–15:05 之所以还在 ``SESSIONS`` 里，只是为了让
    15:01 抓到的快照仍算这一场的盘。把这些全标成"集合竞价/待开盘"的后果是
    2026-11-05 实测到的：15:01 的 ``/market/status`` 报"集合竞价/待开盘"，读起来
    像"就要开盘"，正好是事实的反面；而 11:30–13:00 报"盘前/盘后"。
    """
    if not trading_day:
        return "周末" if weekday >= 5 else "非交易日"
    if dtime(9, 30) <= clock < dtime(11, 30) or dtime(13, 0) <= clock < dtime(14, 57):
        return "连续竞价"
    if dtime(9, 15) <= clock < dtime(9, 30):
        return "开盘集合竞价"
    if dtime(14, 57) <= clock < dtime(15, 0):
        return "收盘集合竞价"
    if dtime(15, 0) <= clock <= dtime(15, 5):
        return "已收盘"
    if dtime(11, 30) <= clock < dtime(13, 0):
        return "午休"
    return "盘前/盘后"


def session_state(now: datetime | None = None, *, calendar=None) -> dict:
    now = now or datetime.now(SHANGHAI)
    clock = now.time()
    inside = any(start <= clock <= end for start, end in SESSIONS)
    day = now.strftime("%Y%m%d")
    trading_day = bool(now.weekday() < 5
                       and (calendar is None or calendar.is_trading_day(day)))
    return {
        "now": now.isoformat(timespec="seconds"),
        "weekday": now.weekday(),
        "trading_day": trading_day,
        "in_session": bool(inside and trading_day),
        "label": _session_label(clock, trading_day=trading_day,
                                weekday=now.weekday()),
    }


def _num_or_none(value) -> float | None:
    """A count or a volume, where `None` must stay `None` — zero is a real answer."""
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _dec(value, default: str = "0") -> Decimal:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return D(default)
        number = float(value)
        if not math.isfinite(number):
            return D(default)
        return D(str(round(number, 4)))
    except (TypeError, ValueError):
        return D(default)


def _float(value, default: float = 0.0) -> float:
    """Finite vendor numeric input; NaN/inf are missing, not market facts."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@contextmanager
def _file_lock(path: Path):
    """Serialize one read-merge-write across processes on Windows and POSIX."""
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class QuoteService:
    """TTL-cached live quotes with explicit provenance and graceful decay."""

    def __init__(self, market, *, ttl: float = DEFAULT_TTL, allow_network: bool = True,
                 breaker_file: Path | None = None):
        # Persistence is a deployment concern, not a property of the class: the
        # default is in-memory, and `service_for` is what opts into a shared file.
        # Unit tests therefore never touch the workspace cache.
        self.market = market
        self.ttl = ttl
        self.allow_network = allow_network
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._stats = {"calls": 0, "hits": 0, "ths": 0, "eastmoney": 0,
                       "tencent": 0, "daily": 0, "errors": 0,
                       "last_error": None, "last_success": None}
        # name -> {"fails": int, "until": monotonic deadline, "last_error": str}
        self._breaker: dict[str, dict] = {}
        self._breaker_file = breaker_file
        self._load_breakers()

    # ----------------------------------------------------------------- breaker
    def _breaker_state(self, name: str) -> dict:
        return self._breaker.setdefault(
            name, {"fails": 0, "until": 0.0, "last_error": None})

    def _breaker_open(self, name: str, now: float) -> bool:
        return self._breaker_state(name)["until"] > now

    def _record_source(self, name: str, *, ok: bool, now: float,
                       detail: str | None = None) -> None:
        state = self._breaker_state(name)
        if ok:
            # One clean answer closes the breaker: the point is to stop paying
            # for a dead endpoint, not to punish it after it recovers.
            changed = state["fails"] or state["until"]
            state["fails"] = 0
            state["until"] = 0.0
            if changed:
                self._save_breakers(name)
            return
        state["fails"] += 1
        state["last_error"] = detail
        if state["fails"] >= BREAKER_THRESHOLD:
            state["until"] = now + BREAKER_COOLDOWN
            # Only on the transition. A retry that fails again while open does
            # not reach here (the breaker is skipped), so this is one write per
            # outage, not one per call.
            self._save_breakers(name)

    # Persistence exists for one reason: the failure is a *vendor outage*, and a
    # restart does not cure it. Without this, every deploy paid three more
    # timeouts (7.9s each) learning what the last process already knew.
    #
    # ``until`` is monotonic and meaningless across processes, so it is written
    # as wall-clock and converted back on load.
    def _load_breakers(self) -> None:
        path = self._breaker_file
        if path is None:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now_mono, now_wall = time.monotonic(), time.time()
        for name, state in (raw or {}).items():
            try:
                until_wall = float(state.get("until_wall") or 0.0)
                remaining = until_wall - now_wall
                self._breaker[str(name)] = {
                    "fails": int(state.get("fails") or 0),
                    "until": now_mono + remaining if remaining > 0 else 0.0,
                    "last_error": state.get("last_error"),
                }
            except (TypeError, ValueError):
                continue

    def _save_breakers(self, name: str) -> None:
        path = self._breaker_file
        if path is None:
            return
        target = Path(path)
        now_mono, now_wall = time.monotonic(), time.time()
        state = self._breaker_state(name)
        encoded = {"fails": state["fails"],
                   "until_wall": (now_wall + (state["until"] - now_mono)
                                  if state["until"] > now_mono else 0.0),
                   "last_error": state["last_error"]}
        tmp = target.with_name(
            f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Each instance updates only the source it just measured. Rewriting
            # the whole file from an old in-memory snapshot used to erase another
            # process's newly opened breaker; the lock covers read + merge + replace.
            with _file_lock(target.with_suffix(".lock")):
                try:
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        payload = {}
                except (OSError, ValueError):
                    payload = {}
                payload[name] = encoded
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, target)
        except OSError:
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def breakers(self) -> dict:
        now = time.monotonic()
        return {name: {"open": state["until"] > now,
                       "consecutive_failures": state["fails"],
                       "retry_in_seconds": round(max(0.0, state["until"] - now), 1),
                       "last_error": state["last_error"]}
                for name, state in self._breaker.items()}

    # ------------------------------------------------------------------ public
    def quotes(self, symbols: list[str], *, depth: bool = False) -> dict[str, Quote]:
        # One cache lookup and one fallback per symbol. Duplicates are common at
        # call sites that concatenate holdings and pending orders; keeping them
        # made a daily fallback increment its source counter twice for one answer.
        wanted = list(dict.fromkeys(
            s.strip().upper() for s in symbols if s and s.strip()))
        if not wanted:
            return {}
        now = time.monotonic()
        out: dict[str, Quote] = {}
        missing: list[str] = []
        waiting: list[tuple[str, threading.Event]] = []
        with self._lock:
            self._stats["calls"] += 1
            for symbol in wanted:
                hit = self._cache.get(symbol)
                if hit and now - hit[0] < self.ttl:
                    out[symbol] = hit[1]
                    self._stats["hits"] += 1
                    continue
                event = self._inflight.get(symbol)
                if event is not None:
                    waiting.append((symbol, event))
                else:
                    self._inflight[symbol] = threading.Event()
                    missing.append(symbol)

        if missing:
            try:
                fetched = self._fetch(missing)
                with self._lock:
                    for symbol, quote in fetched.items():
                        if symbol not in missing:
                            continue
                        self._cache[symbol] = (time.monotonic(), quote)
                        out[symbol] = quote
                # Anything the vendors did not return falls back to the stored bar so
                # the caller always gets a number plus the reason it is not live.
                for symbol in missing:
                    if symbol not in out:
                        fallback = self._from_daily(symbol)
                        if fallback is not None:
                            with self._lock:
                                self._cache[symbol] = (time.monotonic(), fallback)
                            out[symbol] = fallback
            finally:
                with self._lock:
                    for symbol in missing:
                        event = self._inflight.pop(symbol, None)
                        if event is not None:
                            event.set()

        # A concurrent caller for the same symbol shares this answer rather than
        # paying for an identical vendor request. A failed leader yields no new
        # quote; it never turns an expired cache row into a fresh one.
        for symbol, event in waiting:
            event.wait()
            with self._lock:
                hit = self._cache.get(symbol)
                if hit and time.monotonic() - hit[0] < self.ttl:
                    out[symbol] = hit[1]
        return self._with_depth(out, depth) if depth else out

    def from_frame(self, frame: pd.DataFrame, *, symbols: list[str] | None = None,
                   now: datetime | None = None) -> dict[str, Quote]:
        """Quote objects from a frame the caller already captured.

        The snapshot loop fetched the whole market 200 ms ago and wrote it to
        ``rt_snapshots``. Asking a vendor the same two questions again would make
        the watchdog's stop distance and ``quant_live``'s price two measurements
        of one instant, differing by a few seconds — and the reader would have no
        way to tell which one the alert was judged on.

        ``symbols`` filters **before** the expensive part: a book of two names
        does not need 5,217 instrument lookups, and converting the frame as-is
        cost 41 seconds when this was first measured (2026-11-05 19:25).

        Translation only: no network, no cache write, no fallback. ``now`` is the
        clock the *judgement* runs at, not the capture's: a frame from twenty
        minutes ago must come back with a large ``age_seconds``, or a dead
        capture would look like a live one.
        """
        return self._to_quotes(
            frame, symbols=symbols, now=None if now is None else pd.Timestamp(now))

    def _with_depth(self, quotes: dict[str, Quote], wanted: list[str]) -> dict[str, Quote]:
        """Fill the order book for whoever is missing it, from the one source that has it.

        东财的批量行情不带五档（`clist` 的 f19/f20 是精度和总市值，不是买一），
        所以"封单多少"这个问题在那条路径上没有答案。腾讯的 `qt.gtimg.cn` 带五档，
        而且是同一个接口一次能给 50 个代码——所以只对**缺盘口的那几只**补一次。

        `bid1_volume` 等单位是**手**（和交易所盘口一致），不是股：封单量说的是手数。
        """
        need = [symbol for symbol in quotes if quotes[symbol].bid1 is None]
        if not need:
            return quotes
        try:
            frame = self._fetch_tencent(need)
        except Exception:  # noqa: BLE001 - 盘口是加分项，拿不到不影响价格
            return quotes
        if frame is None or frame.empty or "code" not in frame.columns:
            return quotes
        rows = {}
        for record in frame.to_dict("records"):
            rows[str(record.get("code") or "").zfill(6)] = record
        out = dict(quotes)
        for symbol, quote in quotes.items():
            record = rows.get(symbol.split(".")[0].zfill(6))
            if not record:
                continue
            out[symbol] = replace(
                quote,
                bid1=_dec(record.get("bid1")) or None,
                bid1_volume=_num_or_none(record.get("bid1_volume")),
                ask1=_dec(record.get("ask1")) or None,
                ask1_volume=_num_or_none(record.get("ask1_volume")),
                bid_volume_5=_num_or_none(record.get("bid_volume_5")),
                ask_volume_5=_num_or_none(record.get("ask_volume_5")),
            )
        return out

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes([symbol]).get(symbol.strip().upper())

    def cached(self, symbols: list[str]) -> dict[str, Quote]:
        """Quotes already in the cache and still inside the TTL. **No network.**

        The fill path re-runs the stop gate while holding ``PaperBook.lock``
        (``book._preflight``). Letting that call go to a vendor meant one
        settlement could block every reader of the account for the length of
        three source timeouts — ``mark_prices`` goes out of its way to release
        the lock before asking for quotes, and the gate must not reintroduce
        the same wait from another door.

        A miss is not an error: ``stop_gate`` falls back to the stored daily bar
        (labelled ``is_realtime=False``) exactly as it does when a vendor is
        down, and the caller can still see the freshness that was used.
        """
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        now = time.monotonic()
        out: dict[str, Quote] = {}
        with self._lock:
            for symbol in wanted:
                hit = self._cache.get(symbol)
                if hit and now - hit[0] < self.ttl:
                    out[symbol] = hit[1]
        return out

    def health(self) -> dict:
        state = session_state(calendar=getattr(getattr(self, "market", None),
                                            "calendar", None))
        with self._lock:
            stats = dict(self._stats)
        return {
            "session": state,
            "cache": {"entries": len(self._cache), "ttl_seconds": self.ttl},
            "sources": {"ths": stats["ths"], "eastmoney": stats["eastmoney"],
                        "tencent": stats["tencent"], "daily_fallback": stats["daily"]},
            "errors": stats["errors"],
            "last_error": stats["last_error"],
            "last_success": stats["last_success"],
            # Which sources are being skipped right now, and for how much longer.
            # "Configured" and "answering" are different claims; this is the one
            # that says a channel is currently being ignored on purpose.
            "breakers": self.breakers(),
            "configured": {"network_allowed": self.allow_network,
                           "ths_key": bool(os.getenv("THS_API_KEY", "").strip())},
            # "Configured" is not "working": the flag says a live path exists,
            # while sources/last_success say whether it has actually answered.
            "realtime_available": bool(self.allow_network),
            "note": ("source=daily 表示这条价格来自已存储的日线，不是实时；"
                     "age_seconds 是行情时间戳到现在的时间差。"),
        }

    # ----------------------------------------------------------------- sources
    def _fetch(self, symbols: list[str]) -> dict[str, Quote]:
        """Tonghuashun first, then EastMoney, then Tencent — per symbol.

        Order matters: THS is the official vendor with published field semantics
        and no cumulative call quota, so it is the source of record. The other two
        stay as fallbacks because a single vendor outage must not blind the desk.

        The fallback is per symbol, not per batch. Stopping at the first
        non-empty answer meant a mixed request ("one stock, one ETF, one
        convertible") was satisfied by the stock alone: THS returned one row, the
        loop broke, and the other two were silently dropped and replaced by
        yesterday's close. Same shape as the prefix bug — nothing raised, the
        answer was just quietly smaller.
        """
        if not self.allow_network:
            return {}

        remaining = list(symbols)
        out: dict[str, Quote] = {}
        now = time.monotonic()
        for name, fetcher in (("ths", self._fetch_ths),
                              ("eastmoney", self._fetch_eastmoney),
                              ("tencent", self._fetch_tencent)):
            if not remaining:
                break
            if self._breaker_open(name, now):
                # Known down. Skipping is the whole point: the fallback below
                # answers, and the caller gets a live Tencent number instead of
                # waiting out a dead endpoint's timeout first.
                continue
            try:
                frame = fetcher(remaining)
            except Exception as exc:  # noqa: BLE001 - a vendor failing must degrade
                detail = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = f"{name}: {detail}"
                    self._record_source(name, ok=False, now=now, detail=detail)
                continue
            # An empty frame is not a failure: a vendor legitimately covers a
            # different set of instruments. Only an exception counts toward the
            # breaker — otherwise every ETF request would trip the A-share source.
            if not frame.empty:
                with self._lock:
                    self._record_source(name, ok=True, now=now)
            if frame.empty:
                continue
            with self._lock:
                self._stats[name] += 1
                self._stats["last_success"] = datetime.now(SHANGHAI).isoformat(
                    timespec="seconds")
            # Label with the fetcher that actually answered, not with whatever
            # the frame claims: attribution is the whole point of `source`.
            # A vendor response is evidence only for symbols in this request.
            # Otherwise a stale/mis-keyed batch can insert an unrelated quote in
            # the result and cache it under a key the caller never asked for.
            parsed = {sym: replace(q, source=name)
                      for sym, q in self._to_quotes(frame).items() if sym in remaining}
            out.update(parsed)
            remaining = [s for s in remaining if s not in out]
        return out

    def _fetch_ths(self, symbols: list[str]) -> pd.DataFrame:
        """Official Tonghuashun snapshot. Units: shares and CNY, no name field.

        A-share only: the endpoint is ``/api/a-share/prices/snapshot``, so an ETF
        or a convertible comes back empty after a full round trip. Filtering here
        keeps the fallback chain from paying that timeout on every mixed request.

        北交所也要滤掉：它算 A 股（``is_a_share`` 返回 True），但同花顺这个
        snapshot 没有它的数据——2026-11-03 实测 ``430047.BJ`` 直接返回
        ``code=3001 Target not found``。留着它只会让每一批都白跑一次。
        """
        key = os.getenv("THS_API_KEY", "").strip()
        if not key:
            return pd.DataFrame()
        from service.adapters.symbols import is_a_share

        symbols = [s for s in symbols
                   if is_a_share(s) and not s.endswith(".BJ")]
        if not symbols:
            return pd.DataFrame()
        base = os.getenv("THS_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/")
        import httpx

        rows: list[dict] = []
        missing_batches = 0
        for start in range(0, len(symbols), THS_BATCH):
            chunk = symbols[start:start + THS_BATCH]
            response = httpx.get(
                f"{base}/api/a-share/prices/snapshot",
                params={"thscodes": ",".join(chunk)},
                headers={"X-api-key": key},
                timeout=THS_TIMEOUT,
            )
            payload = response.json()
            if payload.get("code") != 0:
                # 3001 "Target not found" 是**批次级**的"这一批代码我一个都不认识"，
                # 不是源挂了。2026-11-03 实测：ETF(510300.SH)、北交所(430047.BJ)、
                # 已退市、写错的代码都返回 3001；而一批里混进一个正常 A 股时
                # code=0 并只返回好的那几条。所以整批 3001 只说明这批该跳过。
                # 旧代码在这里直接 raise，一个坏批次就把后面所有批次连同
                # 东财、腾讯两个兜底源一起废掉——`/market/status` 里那条
                # `ths: RuntimeError: code=3001 Target not found` 就是这么来的。
                if str(payload.get("code")) == "3001":
                    missing_batches += 1
                    continue
                raise RuntimeError(f"code={payload.get('code')} {payload.get('message')}")
            data = payload.get("data") or {}
            stamp = data.get("timestamp")
            for item in data.get("item") or []:
                rows.append({
                    "code": str(item.get("ticker") or "").zfill(6),
                    "name": "",
                    "last": item.get("last_price"),
                    "pre_close": item.get("prev_price"),
                    "open": item.get("open_price"),
                    "high": item.get("high_price"),
                    "low": item.get("low_price"),
                    "volume": item.get("volume"),          # already shares
                    "amount": item.get("turnover"),        # already CNY
                    "quote_ts": None if stamp is None else float(stamp) / 1000.0,
                    "source": "ths",
                })
        if missing_batches and not rows:
            # 每一批都 3001：这时不能再当作"这批没有"，是真的没有数据可给。
            # 抛出去让调用方落到下一个源，而不是返回一个空表冒充"查过了"。
            raise RuntimeError("code=3001 全部批次 Target not found")
        return pd.DataFrame(rows)

    def _fetch_eastmoney(self, symbols: list[str]) -> pd.DataFrame:
        from service.adapters.eastmoney_adapter import EastMoneyAdapter

        frame = EastMoneyAdapter().fetch_quotes(symbols)
        if frame.empty:
            return frame
        out = frame.copy()
        out["volume"] = pd.to_numeric(out.get("volume"), errors="coerce").fillna(0) * 100.0
        out["source"] = "eastmoney"
        return out

    def _fetch_tencent(self, symbols: list[str]) -> pd.DataFrame:
        from service.adapters.tencent_adapter import TencentQuoteAdapter

        frame = TencentQuoteAdapter().fetch_quotes(symbols)
        if frame.empty:
            return frame
        out = frame.copy()
        out["volume"] = pd.to_numeric(out.get("volume"), errors="coerce").fillna(0) * 100.0
        out["source"] = "tencent"
        return out

    def _freshness(self, quote_ts: str | None, age: float | None,
                   now: pd.Timestamp) -> tuple[bool, str | None]:
        """A vendor price is live only at its own fresh, same-session timestamp."""
        if not quote_ts or age is None:
            return False, "missing_timestamp"
        if age < 0:
            return False, "future_timestamp"
        try:
            stamp = pd.Timestamp(quote_ts)
        except (TypeError, ValueError):
            return False, "invalid_timestamp"
        if stamp.date() != now.date():
            return False, "cross_session"
        if age > STALE_AFTER:
            return False, "stale_quote"
        calendar = getattr(self.market, "calendar", None)
        if not session_state(stamp.to_pydatetime(), calendar=calendar)["in_session"]:
            return False, "outside_session"
        return True, None

    def _to_quotes(self, frame: pd.DataFrame, *, now: pd.Timestamp | None = None,
                   symbols: list[str] | None = None) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        now = now or pd.Timestamp.now(tz=SHANGHAI)
        wanted = None if symbols is None else {str(s).strip().upper() for s in symbols}
        if wanted is not None and "code" in frame.columns:
            # Cheap vector pre-filter so a two-name book over a whole-market frame
            # does not pay the per-row work 5,217 times. The exact match happens
            # below, after the market suffix is known.
            codes = {s.split(".")[0] for s in wanted}
            frame = frame[frame["code"].astype(str).str.zfill(6).isin(codes)]
        # Hoisted: `latest_bar_date()` is ~5 ms and was being called once per row,
        # so a 500-symbol batch paid 2.5 s of pure repeated lookups.
        bar_day = self.market.latest_bar_date() or ""
        for _, row in frame.iterrows():
            try:
                code = str(row.get("code") or "").zfill(6)
                # 厂商回了市场号就用它：`1.000001` 是上证指数，`0.000001` 是平安银行。
                symbol = self._symbol_of(code, row.get("market"))
                if not symbol or (wanted is not None and symbol not in wanted):
                    continue
                last = _dec(row.get("last"))
                pre_close = _dec(row.get("pre_close"))
                if last <= 0 or pre_close <= 0:
                    continue
                quote_ts, age = self._timestamp(row.get("quote_ts"), now)
                live, reason = self._freshness(quote_ts, age, now)
                if reason == "outside_session":
                    # 盘后这条 last 是当日收盘价。quote_ts 是刚才取数的那一刻，
                    # 于是 age 会显示成"4 秒前"——一个 6 小时前的价格看起来像刚
                    # 成交的。价格有多旧不可知（厂商不给逐条时间），所以这里写
                    # 未知，而不是写一个算得出来但不成立的数。is_realtime 已经是
                    # false，reason 也已经说了 outside_session，两个一起读才完整。
                    age = None
                inst = self._instrument(symbol, bar_day)
                up, down = self._limits(symbol, pre_close, inst)
                volume = _float(row.get("volume"))
                amount = _float(row.get("amount"))
                bid1 = _dec(row.get("bid1")) or None
                ask1 = _dec(row.get("ask1")) or None
                # 成交量为 0 不等于停牌。集合竞价（09:15–09:30）本来就没有连续
                # 成交，而竞价参考价一直在动；全天停牌的票才是真的没有量。
                # 区分靠盘口：竞价中的票挂着买卖单，停牌的票五档全空。
                # 2026-11-03 的实测：09:17 分众 volume=0 被标成 suspended=true，
                # 而同一份响应里价格已从 12.42 走到 12.55——停牌的票不会动。
                # 这个字段还接着 guard.py 的买入检查（Check("suspended", ...)），
                # 误判不是显示问题，是直接把单拒掉。
                suspended = bool(volume <= 0 and bid1 is None and ask1 is None)
                out[symbol] = Quote(
                    symbol=symbol,
                    name=str(row.get("name") or inst.name or ""),
                    last=last, pre_close=pre_close,
                    open=_dec(row.get("open")) or pre_close,
                    high=_dec(row.get("high")) or last,
                    low=_dec(row.get("low")) or last,
                    volume=volume,
                    amount=amount,
                    up_limit=up, down_limit=down,
                    quote_ts=quote_ts, age_seconds=age,
                    source=str(row.get("source") or "eastmoney"),
                    timestamp_basis=_TIMESTAMP_BASES.get(
                        str(row.get("source") or "eastmoney"), "fetched"),
                    is_realtime=live,
                    realtime_reason=reason,
                    suspended=suspended,
                    bid1=bid1,
                    bid1_volume=_num_or_none(row.get("bid1_volume")),
                    ask1=ask1,
                    ask1_volume=_num_or_none(row.get("ask1_volume")),
                    bid_volume_5=_num_or_none(row.get("bid_volume_5")),
                    ask_volume_5=_num_or_none(row.get("ask_volume_5")),
                )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                # One malformed vendor row must not abort the remaining symbols
                # or prevent the next source from answering them.
                continue
        return out

    def _from_daily(self, symbol: str) -> Quote | None:
        snap = self.market.snapshot(symbol, self.market.latest_bar_date())
        if snap is None or snap.last <= 0:
            return None
        with self._lock:
            self._stats["daily"] += 1
        return Quote(
            symbol=symbol, name=self._instrument(symbol).name,
            last=snap.last, pre_close=snap.pre_close, open=snap.open,
            high=snap.high, low=snap.low, volume=snap.volume, amount=snap.amount,
            up_limit=snap.up_limit, down_limit=snap.down_limit,
            quote_ts=snap.trade_date, age_seconds=None,
            source="daily", is_realtime=False, suspended=snap.suspended,
            timestamp_basis="daily_bar",
            realtime_reason="daily_fallback",
        )

    # ----------------------------------------------------------------- helpers
    def _instrument(self, symbol: str, day: str | None = None):
        """The instrument record. ``day`` is passed in by batched callers.

        ``latest_bar_date()`` reads the registry every time (~5 ms). Calling it per
        row made a whole-market frame cost 26 seconds of lookups that all had the
        same answer.
        """
        return self.market.instrument(symbol, day if day is not None
                                      else (self.market.latest_bar_date() or ""))

    def _limits(self, symbol: str, pre_close: Decimal, inst) -> tuple[Decimal, Decimal]:
        """Exchange band, derived from the previous close and the board rule.

        Vendor snapshots do not carry limit prices; ``stk_limit`` is the official
        source but it only exists for the daily bar. The derived value matches it
        for every board except the rare post-IPO no-limit days, which a 5,000 CNY
        account will not be trading on day one.
        """
        if pre_close <= 0:
            return D("0"), D("0")
        # 指数没有涨跌停。给它算一个 ±10% 的"区间"会得到一个看起来很像价格、
        # 实际不存在的数（上证 3638 → 3958.92 / 3239.12），而下游会拿它判"封板"。
        from service.trading.market import INDEX_CODES

        if symbol in INDEX_CODES:
            return D("0"), D("0")
        pct = inst.limit_pct
        # Half-up: the exchange prints 四舍五入 on the band. Half-even (the
        # Decimal default) turns 9.95 x 1.10 into 10.94 instead of 10.95, and a
        # band one fen too low lets `blocked_at_open` accept a limit-down fill
        # that could not have printed.
        # Quantise to the instrument's own tick. Convertible bonds price in
        # 0.001, and a hard-coded 0.01 here disagreed with ``market.snapshot``
        # (which uses ``inst.price_tick``) by up to half a fen — two sources
        # answering "is this inside the band" differently for the same bond.
        tick = getattr(inst, "price_tick", None) or D("0.01")
        if tick <= 0:
            tick = D("0.01")
        up = (pre_close * (D("1") + pct)).quantize(tick, rounding=ROUND_HALF_UP)
        down = (pre_close * (D("1") - pct)).quantize(tick, rounding=ROUND_HALF_UP)
        return up, down

    @staticmethod
    def _symbol_of(code: str, market=None) -> str | None:
        if len(code) != 6 or not code.isdigit():
            return None
        if str(market) == "1":
            # 上交所。指数的六位数和深市的票可能完全一样，只能靠厂商给的市场号分开。
            return f"{code}.SH"
        from service.adapters.symbols import ts_code

        return ts_code(code)

    @staticmethod
    def _timestamp(value, now: pd.Timestamp) -> tuple[str | None, float | None]:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None, None
        try:
            epoch = float(value)
            if not math.isfinite(epoch) or epoch <= 0:
                return None, None
            stamp = pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(SHANGHAI)
        except (ValueError, TypeError, OSError, OverflowError):
            return None, None
        age = float((now - stamp).total_seconds())
        return stamp.isoformat(timespec="seconds"), age


#: Markets a quote service was attached to, so ``reset()`` can detach it.
#:
#: The service used to live in a module-level ``dict`` keyed by ``id(market)``.
#: ``id`` is an address: every ``build_desk`` (each test session, the samples
#: book, a script) added an entry that could never be removed, because the
#: attached service itself holds the market alive. Attaching to the market makes
#: the lifetime follow the owner instead of a process-global table.
_MARKETS: weakref.WeakSet = weakref.WeakSet()


def service_for(market, *, ttl: float = DEFAULT_TTL,
                allow_network: bool = True) -> QuoteService:
    """One service per market repository, so the cache is shared."""
    found = getattr(market, "_quote_service", None)
    if (found is not None and found.ttl == ttl
            and found.allow_network == allow_network):
        return found
    found = QuoteService(market, ttl=ttl, allow_network=allow_network,
                         breaker_file=BREAKER_FILE)
    try:
        market._quote_service = found
        _MARKETS.add(market)
    except (AttributeError, TypeError):
        # A stub market with slots, or an unhashable one: keep the service but
        # do not remember it. Correctness does not depend on the cache.
        pass
    return found


def reset() -> None:
    """Detach every quote service this process handed out (tests, reloads)."""
    for market in list(_MARKETS):
        try:
            del market._quote_service
        except AttributeError:
            pass
    _MARKETS.clear()
