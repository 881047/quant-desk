"""Trading desk composition: data store + paper book + guard + decisions."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from service import paths, settings
from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

from .book import PaperBook
from .decisions import DecisionService
from .guard import Guard, RiskLimits
from .market import MarketRepository
from .state import PaperStore

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Desk:
    registry: Registry
    data: DatasetStore
    market: MarketRepository
    paper: PaperStore
    book: PaperBook
    guard: Guard
    decisions: DecisionService

    def playbook(self) -> str:
        from .playbook import current

        return current(ROOT)["text"]

    def core_book(self, config=None):
        """The ETF sleeve's book. Constructed on demand: most turns never need it."""
        from .core import CoreBook

        return CoreBook(self.data, self.paper, config)

    def quotes(self, symbols: list[str], *, depth: bool = False) -> dict:
        """Live quotes when the network is allowed, empty mapping otherwise.

        ``depth=True`` 额外补齐五档盘口（东财批量不带，走腾讯一次请求）。
        """
        from .realtime import service_for

        allow = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")
        return service_for(self.market, allow_network=allow).quotes(symbols, depth=depth)

    def quote_health(self) -> dict:
        from .realtime import service_for

        allow = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")
        return service_for(self.market, allow_network=allow).health()

    def cached_quotes(self, symbols: list[str]) -> dict:
        """Quotes already on hand — never a vendor call.

        Used by the fill-time guard, which runs under ``PaperBook.lock``: a
        network round trip there would hold the account lock for the length of
        a vendor timeout. A miss is answered by the stored daily bar downstream.
        """
        from .realtime import service_for

        allow = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")
        return service_for(self.market, allow_network=allow).cached(symbols)

    def session_state(self) -> dict:
        """盘中/集合竞价/休市。日历来自 desk 的行情库，不是墙钟猜出来的。"""
        from .realtime import session_state

        return session_state()

    def limits(self) -> dict:
        cfg = self.guard.limits
        return {
            "max_positions": cfg.max_positions,
            "max_core_weight": cfg.max_core_weight,
            "max_aggressive_weight": cfg.max_aggressive_weight,
            "min_cash_weight": cfg.min_cash_weight,
            "max_core_single": cfg.max_core_single,
            "max_core_etf_single": cfg.max_core_etf_single,
            "max_aggressive_single": cfg.max_aggressive_single,
            "max_trade_risk": cfg.max_trade_risk,
            "industry_cap": cfg.max_industry,
            "daily_loss_no_new": cfg.daily_loss_no_new,
            "daily_loss_reduce_only": cfg.daily_loss_reduce_only,
            "drawdown_half": cfg.drawdown_half,
            "drawdown_stop": cfg.drawdown_stop,
            "max_adv_ratio": cfg.max_adv_ratio,
            "max_actions_per_day": cfg.max_actions_per_day,
            "fill_mode": self.book.fill_mode,
        }


_desk: Desk | None = None
_desk_init_lock = threading.Lock()
#: Why the last attempt to build the desk failed, or ``None``.
#:
#: ``PaperBook.__init__`` refuses a snapshot whose reservations do not reconcile
#: (correctly — better than trading a broken book). Before this, the refusal
#: surfaced as a 500 on every route while ``/health`` kept answering "ok": the
#: only endpoint a monitor can ask had no way to say "the account did not load".
_desk_error: str | None = None


def _quotes_for(market, symbols: list[str]) -> dict:
    """Live quotes when the network is allowed, empty mapping otherwise."""
    from .realtime import service_for

    allow = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")
    return service_for(market, allow_network=allow).quotes(symbols)


def _cached_quotes_for(market, symbols: list[str]) -> dict:
    """Quotes already cached; no vendor call. See ``Desk.cached_quotes``."""
    from .realtime import service_for

    allow = os.getenv("REALTIME_QUOTES", "1") not in ("0", "false", "no")
    return service_for(market, allow_network=allow).cached(symbols)


def build_desk(root: Path | None = None, paper_db: Path | None = None,
               initial_cash: str | None = None, registry_db: Path | None = None,
               limits: RiskLimits | None = None,
               market: MarketRepository | None = None,
               data_root: Path | None = None) -> Desk:
    """Compose one desk.

    ``market`` is injectable so a test session can share one read-only
    repository across cases instead of re-reading year partitions per test. The
    store and registry are then taken from that repository, because ``Desk.data``
    and ``Desk.market.store`` must be the same object: two DatasetStores over one
    root work, but they keep separate validation caches and the desk would read
    some partitions twice.

    ``data_root`` overrides ``root / "data"``. It exists because
    ``QUANT_DATA_ROOT`` is read as *the data directory* by ``service/api/data.py``
    and used to be read as *the parent of it* here: unless the value happened to
    end in ``data``, the API and the desk opened two different registries and two
    different parquet trees, with no error anywhere.
    """
    root = root or ROOT
    directory = Path(data_root) if data_root is not None else (root / "data")
    if market is not None:
        registry = market.store.registry
        data = market.store
    else:
        registry = Registry(registry_db or (directory / "registry.sqlite"))
        data = DatasetStore(directory, registry)
        market = MarketRepository(data)
    # 账本和数据集住同一个根。以前这里写死 ``root / "data"``，而 ``data_root``
    # 只管数据集：``QUANT_DATA_ROOT`` 指到不以 ``data`` 结尾的目录时两者就分家，
    # 而 API 的请求计数还写在第三个地方（见 service/paths.py）。
    paper = PaperStore(paper_db or paths.paper_db(directory))
    # 显式参数（测试、样本账本）或配置，两档。这里曾经还有第三档
    # ``os.getenv("INITIAL_CASH")``，而它排在配置前面：``.env`` 里有
    # ``INITIAL_CASH=5000``、``security.py`` 又在 import 时就把它灌进进程环境，
    # 所以改 ``risk.initial_cash`` 从来没生效过——只是两个值恰好都是 5000。
    # 临时改账户规模请用 ``QUANT__RISK__INITIAL_CASH``，它会出现在
    # ``/health.config.overridden`` 里；旧的 ``INITIAL_CASH`` 会被
    # ``settings.legacy_env()`` 点名，不再静默压过配置。
    cash = initial_cash or settings.initial_cash_text()
    # The book gets the quote service as a callable, not as an import: the fill
    # path should be able to ask "what is this worth now" without knowing whether
    # the answer comes from a vendor, a cache or a test double.
    # The guard is built first so the book can re-run it at fill time: a queued
    # ticket is executed by tomorrow's account, not by the one that proposed it.
    guard = Guard(market, registry, limits or RiskLimits())
    book = PaperBook(paper, market, cash, quotes=lambda symbols: _quotes_for(market, symbols),
                     guard=guard,
                     quotes_cached=lambda symbols: _cached_quotes_for(market, symbols))
    decisions = DecisionService(book, market, paper, registry, guard)
    return Desk(registry, data, market, paper, book, guard, decisions)


# ---- 研究样本账本 -----------------------------------------------------------
# A second book with its own cash, its own sqlite file and its own limits. The
# main account's equity, arena verdict and case history stay untouched: mixing a
# one-day sampling run into the discretionary book would make both unreadable.
# What this book produces is *settled records* — every fill carries a thesis and
# an invalidation — so an idea can be falsified in a month instead of a year.
# It is a research cost, not a profit centre. Its size is config
# (``research.samples_cash``): a module-level ``os.getenv("SAMPLES_CASH", "2000")``
# was read once at import, so no test could move it and ``provenance()`` never
# showed it.
_samples: Desk | None = None
_samples_lock = threading.Lock()


def samples_limits() -> RiskLimits:
    """One position, fully invested, one buy and one sell a day.

    These are mechanical constraints for a one-day holding period, not a style:
    the 30% single-name cap and the 20% cash floor would make a daily rotation
    physically impossible on a 2,000 account.
    """
    return RiskLimits(
        max_positions=1,
        max_core_weight=1.0,
        min_cash_weight=0.0,
        max_core_single=1.0,
        max_trade_risk=1.0,
        max_actions_per_day=2,
        max_industry=1.0,
    )


def build_samples_desk(root: Path | None = None,
                       market: MarketRepository | None = None) -> Desk:
    """The sample ledger: its own money, its own rules, its own file.

    ``market`` is injectable for the same reason it is on :func:`build_desk` —
    a test session should share one read-only market repository instead of
    re-reading year partitions per case. Without it the only way to test this
    ledger was against the real ``data/samples.sqlite``, which is the account
    that counts the 20 settled trades; so it had no tests at all.
    """
    root = root or ROOT
    return build_desk(root=root,
                      paper_db=root / "data" / "samples.sqlite",
                      initial_cash=f"{settings.num('research.samples_cash'):.2f}",
                      limits=samples_limits(),
                      market=market)


def get_samples_desk() -> Desk:
    global _samples
    if _samples is None:
        with _samples_lock:
            if _samples is None:
                _samples = build_samples_desk()
    return _samples


def get_desk() -> Desk:
    global _desk, _desk_error
    if _desk is None:
        with _desk_init_lock:
            if _desk is None:
                registry_override = os.getenv("QUANT_REGISTRY_DB")
                # 数据根、账本、注册表三处路径都由 ``service.paths`` 解析：
                # ``PAPER_DB`` 在 ``paths.paper_db`` 里读，这里再读一遍就是
                # 第二个答案（也正是账本与遥测分家的原因）。
                try:
                    _desk = build_desk(
                        data_root=paths.data_dir(),
                        registry_db=Path(registry_override) if registry_override else None,
                    )
                    _desk_error = None
                except Exception as exc:
                    _desk_error = f"{type(exc).__name__}: {exc}"
                    raise
    return _desk


def desk_error() -> str | None:
    """Why the desk could not be built, for ``/health``.

    A read-only answer: it does not try to rebuild or repair. Its only job is to
    let a monitor say "the book did not load, here is the sentence" instead of
    showing a green health check next to a wall of 500s.
    """
    return _desk_error


def current_desk() -> Desk | None:
    """The desk if this process already built one, else None.

    `/health` needs cache numbers but must not *construct* a desk: the watchdog
    polls it every few seconds, and building one opens sqlite and reads the
    calendar. "Not built yet" is a legitimate answer, not an error.
    """
    return _desk


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
