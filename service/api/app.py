"""Minimal FastAPI surface for the paper account and context pack.

P0 is in-memory: one ledger, one account. P1 will load from SQLite.
Run:
    .venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from service import paths, settings
from service.api import data as data_api
from service.api import trading as trading_api
from service.api.build import build_info
from service.api.responses import SafeJSONResponse
from service.api.security import require_token, token_is_default
from service.kernel.clock import SHANGHAI, LiveClock
from service.trading.state import ExecutionExists, PaperStore, StateConflict, StoreBusy


def mode() -> str:
    """``paper`` / ``remote``, read at call time.

    It used to be a module constant. `.env` is loaded by ``service/__init__``
    now, but a constant would still freeze whatever the environment said at
    import — and ``require_token`` reads ``MODE`` per request. Two readers of
    one setting must not disagree because one of them ran earlier.
    """
    return (os.getenv("MODE", "paper") or "paper").strip().lower()



# ------------------------------------------------------------------ 启动预热
# 进程刚起来时 /persona 要读二十年日线（~1.9 s）、/symbol/profile 要读年分区
# （~4.3 s）。这两个数字只出现一次，但恰好是重启后第一个打开面板的人看到的。
# FastAPI 官方 lifespan 文档同时提醒：别把加载放在模块顶层，否则 import 这个
# app 的自动化测试也要付一遍。所以这里用 lifespan 起一个 daemon 线程，
# 不阻塞 startup，进程内只跑一次；测试进程（sys.modules 里有 pytest）直接跳过。
_WARMUP_LOCK = threading.Lock()
_WARMUP_STARTED = False
_WARMUP_STOP = threading.Event()
#: 必须小于 cache.market_seconds（60 s）。``_cached`` 命中时会把 entry.stamp
#: 刷成当前时间，所以每 40 s 轻触一次就能让这批只读帧一直保持"新鲜"；
#: 不做这件事的话，预热只在启动后 60 s 内有效（实测第 109 秒的 profile
#: 又回到 3819 ms）。日更改 manifest 仍会 invalidate，不会把昨天的帧保活到今天。
_WARMUP_INTERVAL = 40.0


def _warmup(keep_alive: bool = True) -> None:
    """Fill the shared frames. Read-only: no order, no ticket, no state change.

    ``keep_alive=False`` runs one pass and returns — used by a direct check or
    a test that wants to measure the warmup without starting the refresh loop.
    """
    logger = logging.getLogger(__name__)
    started = time.monotonic()
    try:
        desk = trading_api.get_desk()
        day = desk.market.latest_bar_date()
        if not day:
            logger.info("warmup skipped: no bar date")
            return
        market = desk.market
        market.basic()
        market.names()
        market.sw_table()
        market.limits(day)
        market.daily(day)
        market.basic_day(day)
        market.panel(day, 61)          # relative_strength / regime / screen 共用
        from service.trading import profile as profile_mod

        # 上面那几个是"已知的大表"，但 /persona 还要 regime 的 300 日面板、
        # profile 还要 fina_indicator / news / margin / 5 分钟线等十来张表。
        # 与其维护一张会漂移的清单，不如在进程内跑一遍两条读取路径本身：
        # 它们填的是同一批缓存键，后续真实请求直接命中。只读，不产生委托。
        trading_api.persona()
        names = market.names()
        sample = next(iter(names), None)
        if sample:
            profile_mod.build(desk, sample, day)
        logger.info("warmup done for %s in %.1fs", day, time.monotonic() - started)
    except Exception:
        logger.exception("warmup failed (non-fatal)")
        return
    if keep_alive:
        # 两个线程分开：40 s 保活必须立刻开始，不能排在 15 s 的慢端点预热
        # 后面。第六轮实测把慢端点放在保活之前时，首屏 /watch 撞上
        # information 的磁盘缓存重算，等了 32 s。
        threading.Thread(target=_keep_warm, args=(desk, sample), daemon=True,
                         name="desk-keep-warm").start()
        threading.Thread(target=_slow_warmup, args=(day,), daemon=True,
                         name="desk-slow-warmup").start()


def _slow_warmup(day: str) -> None:
    """慢端点预热单独成线程；失败只记日志，绝不连累保活。

    /discover 与 /data/information 是插件真实消费的端点，首次各 17.99 s /
    15.06 s（2026-11-08 实测）。60 s 插件超时只是"容忍"，不是解决。
    /watch 放第一位：它是 web 首屏唯一会碰到的冷端点。
    """
    logger = logging.getLogger(__name__)
    try:
        trading_api.watch()
        _warm_slow_endpoints(day)
        logger.info("slow endpoint warmup done for %s", day)
    except Exception:
        logger.warning("slow endpoint warmup failed (non-fatal)", exc_info=True)


def _touch_discover() -> None:
    """直接走 handler，缓存键与真实请求逐字一致。

    手算 ``f"discover:{latest_bar_date()}"`` 看着一样，实际 ``_day(None)``
    可能回落到 last_trading_day；两个键差一个日期，预热就白做（第六轮实测
    首请求仍 2011 ms，第二次才 9 ms）。这种"预热与请求不是同一份缓存"
    的坑第二轮已经踩过一次，别再抄一份键。
    """
    trading_api.discover()


def _warm_slow_endpoints(day: str) -> None:
    """填慢端点缓存；只读，不产生委托、票据或审计写入。"""
    from service.api import data as data_api

    _touch_discover()
    # information 的仓库部分按 manifest digest 落到磁盘，一次之后长期有效；
    # 每次调用仍会重新探实时报价，所以不放进 40 s 保活循环。
    data_api.information(day=day)


def _keep_warm(desk, sample: str | None) -> None:
    """Touch the same read paths often enough that the 60 s frame TTL never lapses.

    Both calls are cache hits once the first warmup is done (~0.3 s per cycle,
    every 40 s, on a daemon thread). A store change invalidates the cache and
    the next cycle rebuilds from the new manifest instead of pinning stale data.
    """
    logger = logging.getLogger(__name__)
    from service.trading import profile as profile_mod

    while not _WARMUP_STOP.wait(_WARMUP_INTERVAL):
        try:
            day = desk.market.latest_bar_date()
            if not day:
                continue
            trading_api.persona()
            if sample:
                profile_mod.build(desk, sample, day)
            _touch_discover()  # 60 s TTL，40 s 触一次即可
        except Exception:
            logger.exception("warmup refresh failed (non-fatal)")


def _start_warmup_once() -> None:
    global _WARMUP_STARTED
    if os.getenv("QUANT_DESK_WARMUP", "1") == "0":
        return
    if "pytest" in sys.modules:
        return
    with _WARMUP_LOCK:
        if _WARMUP_STARTED:
            return
        _WARMUP_STARTED = True
    threading.Thread(target=_warmup, name="desk-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _warn_on_multiple_workers()
    _start_warmup_once()
    try:
        yield
    finally:
        _WARMUP_STOP.set()


def _configured_workers() -> int:
    raw = os.getenv("WEB_CONCURRENCY") or os.getenv("UVICORN_WORKERS") or "1"
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _warn_on_multiple_workers() -> None:
    """Say out loud that this app is single-writer by construction.

    The book lives in process memory and is guarded by an in-process RLock; the
    SQLite revision only turns a *write* race into a 409. A second worker would
    therefore serve its own stale account until it happens to conflict — reads
    would disagree with no error anywhere. The supervisor starts one process;
    this is the guard against someone "helpfully" adding ``--workers 4``.
    """
    workers = _configured_workers()
    if workers > 1:
        logging.getLogger(__name__).error(
            "WEB_CONCURRENCY=%s: quant-desk keeps the paper book in process "
            "memory; more than one worker serves stale accounts (only writes "
            "conflict). Run a single worker — see docs/03-runbook.md.", workers)


app = FastAPI(title="quant-desk", version="0.3.0",
              default_response_class=SafeJSONResponse, lifespan=lifespan)

# Usage counters live in the same SQLite file as the paper account — both resolve
# through ``service/paths.py``, which is the only way that stays true. Construction
# is deferred until the first counted request so importing the ASGI app is
# read-only and test environments are honoured even when set after import.
# ---------------------------------------------------------------- 冲突不是故障
#
# 账本是 sqlite + 乐观锁（``paper_state.revision``）。服务、定时脚本和手工补录
# 都会写它，所以"读到之后、写回之前有人改过"是设计内的情形。以前这类冲突一路
# 冒泡成 500，调用方只能去查自己的参数——而参数是对的，它需要的是"重读账本"。

@app.exception_handler(Exception)
async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    """A crash is a 500 with a code, not a bare Starlette page.

    Before this, `AttributeError`, `KeyError`, `sqlite3.Error` and `ValueError`
    all surfaced as an unlabelled 500 — a caller could not tell "the desk is
    broken" from "my request was wrong", and the tool layer rendered both as
    `quant service 500: ...`. The traceback stays in the log; the body says what
    the caller can do.
    """
    logging.getLogger(__name__).exception("unhandled error on %s", _request.url.path)
    return JSONResponse(status_code=500, content={
        "code": "INTERNAL",
        "detail": f"{type(exc).__name__}: {exc}",
        "next": "这次是服务端错误，重试一次；仍失败就查 data/service.log 的 traceback。",
    })


@app.exception_handler(StateConflict)
def _state_conflict(_request: Request, exc: StateConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "code": "STATE_CONFLICT",
        "detail": str(exc),
        "next": ("账本已自动重载到最新版本（见 PaperBook.reload），直接重试即可；"
                 "这次请求没有产生任何写入。"),
    })


@app.exception_handler(StoreBusy)
def _store_busy(_request: Request, exc: StoreBusy) -> JSONResponse:
    # Deliberately not the STATE_CONFLICT text: nothing was reloaded, the other
    # writer simply would not let go. Saying "已自动重载" here would be a lie the
    # model could act on, and this whole route exists to be read by a model.
    return JSONResponse(status_code=409, content={
        "code": "STORE_BUSY",
        "detail": str(exc),
        "next": ("账本正被另一个写入者占用；这次请求没有产生任何写入，"
                 "等一两秒重试同一请求即可。"),
    }, headers={"Retry-After": "2"})


@app.exception_handler(ExecutionExists)
def _execution_exists(_request: Request, exc: ExecutionExists) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "code": "EXECUTION_EXISTS",
        "detail": str(exc),
        "next": "这一笔已经记过账；去账本或 /cases 确认，不要重复提交。",
    })


class _UsageStore(PaperStore):
    """请求计数用的 store：写不进去就算了，绝不替账本等锁。

    ``PaperStore.BUSY_TIMEOUT`` 是 30 秒，因为一笔"等不到锁就放弃"的委托看起来
    和"下单失败"一样。一个计数不是这样：它只回答"这个端点有没有被调过"，晚三十
    秒写进去和没写进去没有区别，而代价是调用方为了一条计数器陪着等三十秒——
    遥测写在 ``call_next`` 之后，响应体早就好了。

    It also keeps one connection per worker thread. A fresh connection per
    request cost 3.4 ms measured (connect + pragmas + commit) on *every* route,
    paid before the response is returned; the same upsert on a warm connection
    is 0.02 ms. The thread-local map is bounded by the thread pool, and the
    connection is only ever touched through ``record_usage``'s own try/except.
    """

    BUSY_TIMEOUT = 1.0

    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        self._local = threading.local()

    def _warm_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=self.BUSY_TIMEOUT)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def record_usage(self, endpoint: str, method: str, status: int = 200,
                     day: str | None = None) -> None:
        conn = self._warm_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._record_usage_conn(conn, endpoint, method, status, day)
            conn.commit()
        except sqlite3.OperationalError as exc:
            with suppress(sqlite3.Error):
                conn.rollback()
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                # Same translation ``PaperStore._conn`` does: contention is a
                # retryable state, not a broken service, and the caller treats
                # StoreBusy as "this count was not written".
                raise StoreBusy(
                    f"usage database busy for {self.BUSY_TIMEOUT:.0f}s ({exc})") from exc
            raise
        except Exception:
            with suppress(sqlite3.Error):
                conn.rollback()
            raise


_usage: PaperStore | None = None
_usage_init_lock = threading.Lock()

#: 本进程对 usage 表做过什么。``/usage`` 读的是库里的历史，读不出"这个进程根本
#: 没写成功过"——两种情况都显示成 0 次调用。这份计数让 ``/health`` 能分开它们。
_usage_stats: dict[str, object] = {
    "written": 0, "failed": 0, "last_write_at": None, "last_error": None,
}
_usage_stats_lock = threading.Lock()


def _get_usage() -> PaperStore:
    """计数用的 store，和 desk 的账本是同一个文件，但不是同一个对象。

    复用它就得先建 desk：中间件也数 404，而那些请求的处理器根本不会碰 desk，
    让它们为了记一条计数去开一个账本不划算。两个对象各有各的 ``_lock``，但计数
    是一条 upsert，SQLite 自己会串行化；``_UsageStore`` 把等待夹到 1 秒，写不
    进去就记 ``failed``——比让调用方陪着等 30 秒好。
    """
    global _usage
    if _usage is None:
        with _usage_init_lock:
            if _usage is None:
                # 和 desk 用同一个解析器，所以在哪个数据根下都指向同一份账本。
                _usage = _UsageStore(paths.paper_db())
    return _usage


def usage_stats() -> dict:
    """What this process has done to the usage table — for ``/health``.

    ``db`` is part of the answer on purpose: "counters are being written" and
    "counters are being written to the file ``/usage`` reads" are two different
    claims, and only the first one used to be true.
    """
    with _usage_stats_lock:
        snapshot = dict(_usage_stats)
    # 报 store 的路径，不是"现在重新解析会得到的路径"：两者只在环境变量中途被
    # 改过时才不同，而那时候要说的是写到了哪里。
    store = _usage
    snapshot["db"] = str(store.db_path if store is not None else paths.paper_db())
    return snapshot


def _write_usage(endpoint: str, method: str, status: int) -> None:
    """Run SQLite telemetry in a worker, never while holding the breaker lock."""
    _get_usage().record_usage(endpoint, method, status)
    with _usage_stats_lock:
        _usage_stats["written"] = int(_usage_stats["written"]) + 1
        _usage_stats["last_write_at"] = datetime.now(SHANGHAI).isoformat(
            timespec="seconds")


def _record_usage_failure(exc: BaseException) -> None:
    """遥测坏了要留痕：静默吞掉和"没人用过"在 ``/usage`` 里长得一模一样。

    日志按第一条 + 每 100 条打一次，并带上总数。故障期间每个请求一条 traceback
    会把 service.log 变成只有一种内容的文件——永远在响的告警等于没有告警。
    """
    with _usage_stats_lock:
        count = int(_usage_stats["failed"]) + 1
        _usage_stats["failed"] = count
        _usage_stats["last_error"] = f"{type(exc).__name__}: {exc}"
    if count == 1 or count % 100 == 0:
        logging.getLogger(__name__).warning(
            "telemetry write failed (%d so far; /usage will under-count)", count,
            exc_info=exc)


# A caller that guesses the route is expected to be told once, not 241 times.
# On 2026-10-26 a single session produced 241 proposals against paths that did
# not exist (POST /decision/approve, /risk/stops/enforce, /core/rebalance) while
# creating zero tickets. The counter now splits ok from fail, and the breaker
# below stops the loop so the log stays readable.
_BREAKER_WINDOW = settings.num("api.breaker_window")
_BREAKER_LIMIT = settings.integer("api.breaker_limit")
#: How many route shapes the table may hold. The 91 routes account for ~182 keys
#: (one matched, one ``!``-prefixed twin per route), so 512 is ~2.8× headroom for
#: misspellings and history while still bounding a caller that invents paths.
_BREAKER_MAX_KEYS = settings.integer("api.breaker_max_keys")
#: Cold keys need a sweep of their own: lazy trimming only runs when somebody
#: asks about that key again, and a key nobody asks about never expires. One
#: window is the natural period — a key older than that cannot trip anything.
_BREAKER_SWEEP_INTERVAL = _BREAKER_WINDOW
_breaker_sweep_at = 0.0
_recent_failures: dict[str, deque[float]] = defaultdict(deque)
_breaker_lock = threading.Lock()
# `/dashboard.json` used to be here, but it is a token-guarded, expensive route:
# excluding it meant the heaviest read endpoint never appeared in usage or the
# breaker. Only the truly free endpoints stay uncounted.
_UNCOUNTED = ("/health", "/", "/usage", "/openapi.json", "/docs", "/redoc")


def _breaker_prune_locked(now: float) -> None:
    """Drop buckets that can no longer trip. Caller holds ``_breaker_lock``.

    ``queue[-1]`` is the newest timestamp in a bucket, so a bucket whose newest
    entry has aged out cannot reach the limit again and carries no information.
    Deliberately *not* an eviction-by-age: the buckets belonging to real routes
    stop updating the moment they trip (the breaker answers 429 before anything
    reaches the handler), so "oldest" would single out exactly the endpoints
    whose breaker is doing its job. A caller inventing paths could then lift a
    real circuit breaker by flooding the table.
    """
    for key in [k for k, queue in _recent_failures.items()
                if not queue or now - queue[-1] > _BREAKER_WINDOW]:
        del _recent_failures[key]


def _breaker_tick_locked(now: float) -> None:
    """Sweep at most once per window. Caller holds ``_breaker_lock``.

    Hung off *any* request, not just a failure. A caller that fills the table
    and then stops keeps no live bucket — every entry has aged out — but nothing
    would ever look at them again, and ``/health`` would keep reporting a full
    table long after the flood ended. Empty buckets are small, so this is about
    telling the truth rather than about memory.
    """
    global _breaker_sweep_at
    if now >= _breaker_sweep_at:
        _breaker_prune_locked(now)
        _breaker_sweep_at = now + _BREAKER_SWEEP_INTERVAL


def _breaker_tripped(key: str, now: float) -> bool:
    with _breaker_lock:
        _breaker_tick_locked(now)
        # ``.get`` rather than ``[key]``: a defaultdict answers a read by
        # *creating* the entry, so every successful request used to leave two
        # empty deques behind — one for its route, one for the "unrouted" twin
        # that only a 404 would ever fill. 91 routes made that 182 permanent
        # keys before anything failed at all.
        queue = _recent_failures.get(key)
        if queue is None:
            return False
        while queue and now - queue[0] > _BREAKER_WINDOW:
            queue.popleft()
        if not queue:
            del _recent_failures[key]
            return False
        return len(queue) >= _BREAKER_LIMIT


def _record_failure(key: str, now: float) -> None:
    with _breaker_lock:
        queue = _recent_failures.get(key)
        if queue is None:
            # Cold keys need a sweep of their own: lazy trimming only runs when
            # somebody asks about that key again, and a key nobody asks about
            # never expires. Once per window is enough — nothing older can trip.
            #
            # This also runs with ``API_BREAKER=0``, where no read path executes
            # at all; without it, turning the breaker off was the one setting
            # under which the table grew without any bound.
            _breaker_tick_locked(now)
            if len(_recent_failures) >= _BREAKER_MAX_KEYS:
                # Full means somebody is inventing paths faster than they age
                # out. Dropping this entry is the safe side of the trade: every
                # bucket already present keeps working (a route that is failing
                # has one), and the next sweep frees room. Evicting instead
                # would let the flood silence a live breaker.
                return
            queue = deque()
            _recent_failures[key] = queue
        # Trim here too, not only in ``_breaker_tripped``: ``API_BREAKER=0`` skips
        # the check entirely, and that used to be the one configuration where the
        # table grew without any bound at all.
        while queue and now - queue[0] > _BREAKER_WINDOW:
            queue.popleft()
        queue.append(now)
        # Past ``_BREAKER_LIMIT`` entries add nothing: the only question asked is
        # "did this shape fail often enough", and the answer is already yes. The
        # cap also stops a sustained storm from parking thousands of floats in
        # one bucket while ``API_BREAKER=0`` keeps every request appending.
        while len(queue) > _BREAKER_LIMIT:
            queue.popleft()


def _breaker_stats() -> dict:
    """How full the breaker table is — the only way to see it from outside.

    The table is process state: an operator watching memory cannot tell "grew
    because the desk is busy" from "grew because something is inventing paths",
    and after the fix they cannot see that it is bounded either. ``keys`` against
    ``max_keys`` answers both.
    """
    with _breaker_lock:
        return {"keys": len(_recent_failures), "max_keys": _BREAKER_MAX_KEYS,
                "limit": _BREAKER_LIMIT, "window_seconds": _BREAKER_WINDOW}


def _breaker_key(method: str, path: str) -> str:
    """Bucket by *route shape*, not by literal path.

    ``f"{method} {path}"`` gave every ticket id its own bucket, so
    ``POST /decision/td-...-aaaa/approve`` and ``.../bbbb/approve`` were counted
    separately and the breaker could never fire on a parameterised route — the
    one place a retry loop actually happens. Segments that look like identifiers
    are collapsed, which keeps the coarser signal and still separates genuinely
    different routes.
    """
    parts = []
    for segment in path.split("/"):
        looks_like_id = bool(segment) and (
            any(ch.isdigit() for ch in segment)
            or ("-" in segment and len(segment) >= 8)
        )
        parts.append("*" if looks_like_id else segment)
    return f"{method} {'/'.join(parts)}"


# Statuses that say "this caller keeps getting the shape wrong". Excluded on
# purpose: 401/403 (a token misconfiguration must not lock the operator out of
# their own desk), 422 (schema errors), and 409 — which this desk returns for
# entirely legitimate states like "stop breached, no new buys" or "ticket expired".
# Counting 409s meant a correctly-behaving agent could disable /decision/propose
# by asking while a position sat below its stop.
#
# 404 and 405 are counted, but into their own bucket: they describe a *path that
# does not exist*, and 404s need no token. Bucketed together with the matched
# route, any local process could send 30 requests to a misspelled path that
# normalises to the same shape and stop a real endpoint for a minute. A wrong
# path now throttles only itself.
_UNROUTED_FAILURES = (404, 405)


def _counts_toward_breaker(status: int) -> bool:
    return status >= 500


@app.middleware("http")
async def count_usage(request: Request, call_next):
    path = request.url.path
    # ``/desk`` 是静态前端：一个页面加载会打十几个资源，计进 usage 只会把
    # ``/usage`` 那份"模型调了什么"的读数冲淡成"浏览器取了几张 CSS"。
    if path in _UNCOUNTED or path.startswith("/desk"):
        response = await call_next(request)
        # 静态前端没有内容哈希文件名，默认的启发式缓存会让"改了 JS、浏览器还在跑
        # 旧代码"。no-cache 不是不缓存：每次都拿 ETag 回来问一次，没变 304、变了
        # 立刻生效；对本地台子这点条件请求的代价，远小于"改完没反应"的排查成本。
        if path.startswith("/desk"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    now = time.monotonic()
    key = _breaker_key(request.method, path)
    unrouted_key = _breaker_key(request.method, f"!{path}")
    if os.getenv("API_BREAKER", "1") != "0":
        for candidate in (key, unrouted_key):
            if _breaker_tripped(candidate, now):
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "RATE_LIMITED",
                        "detail": f"{candidate} failed {_BREAKER_LIMIT}+ times in "
                                  f"{int(_BREAKER_WINDOW)}s; refusing to retry blind",
                        "next": (f"同一个错误在 {int(_BREAKER_WINDOW)} 秒内重复了 "
                                 f"{_BREAKER_LIMIT} 次，先停下：这不是偶发失败，"
                                 f"按 fix 改请求，别原样重放。"),
                        "fix": "GET /openapi.json and use the exact path and body schema",
                    },
                    headers={"Retry-After": str(int(_BREAKER_WINDOW))},
                )

    response = await call_next(request)
    try:
        route = request.scope.get("route")
        template = getattr(route, "path", path)
        if template not in _UNCOUNTED:
            await asyncio.to_thread(_write_usage, template, request.method, response.status_code)
        if response.status_code in _UNROUTED_FAILURES:
            _record_failure(unrouted_key, now)
            response.headers["X-Api-Hint"] = (
                "GET /openapi.json for the exact route and body schema")
        elif _counts_toward_breaker(response.status_code):
            _record_failure(key, now)
            response.headers["X-Api-Hint"] = (
                "GET /openapi.json for the exact route and body schema")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a request
        # 吞掉可以，不留痕不行：以前这里什么都不做，于是上游一挂，/usage 显示
        # "这个端点从没被调用过"，而真相是"计数写失败了"。
        _record_usage_failure(exc)
        return response
    return response
app.include_router(data_api.router)
app.include_router(trading_api.router)

# 交易台前端（静态 SPA）。
#
# 为什么不挂在 ``/``：``/`` 是免鉴权的那一条（见 ``_UNCOUNTED`` 与
# tests/test_api_contract_doc.py），把它换成 3000 行前端只会让"哪个端点不需要
# 口令"这件事更难回答。``/desk`` 是普通静态目录，不进 OpenAPI，也不动契约文档。
#
# 挂载而不是新路由：StaticFiles 不产生 OpenAPI operation，
# ``tests/test_api_contract_doc.py`` 的"文档必须列全路由"因此不会被这次改动打红。
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class _NoCacheStatic(StaticFiles):
    """前端静态文件不让缓存。

    窗口是长期开着的 WebView2：改完 CSS 重启面板，它照样拿磁盘缓存里的旧
    app.css —— 2026-11-14 在这上面白跑了一轮（服务端已经是新样式，窗口里
    还是旧布局）。本地文件重新校验的成本可以忽略，省下的是"改了看不见"。
    """

    def file_response(self, *args, **kwargs):  # 只加一个响应头
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


if _WEB_DIR.is_dir():
    app.mount("/desk", _NoCacheStatic(directory=str(_WEB_DIR), html=True), name="desk")

clock = LiveClock()


@app.get("/health")
def health() -> dict:
    # 默认口令是写在仓库里的。以前只有一个 import 时的 warning——没人读日志。
    # 放进 /health 是因为它是唯一不需要鉴权的端点，监控和人都能看见。
    #
    # ``config`` 回答的是"这份进程现在按哪套参数在跑"：多少项、来自哪些文件、
    # 有没有被环境变量覆盖、有没有缺项。少了它，改完 yaml 只能靠"下单结果
    # 看起来不一样"来判断到底有没有生效——而那是最贵的一种验证方式。
    values = settings.provenance()
    sources: dict[str, int] = {}
    for item in values.values():
        sources[item["source"]] = sources.get(item["source"], 0) + 1
    # Cache pressure, but only when a desk already exists — see current_desk().
    # 这一段必须包住：`/health` 是唯一免鉴权的端点，也是监控与看板唯一能问的地方。
    # 让一个统计（还是"缓存压力"这种可有可无的东西）把整个端点变成 500，
    # 等于在最需要它的时候把它关掉。2026-11-02 全量跑时就是 desk 处于半初始状态
    # 让它红了，而单独跑同一条测试却是绿的。
    from service.trading import current_desk, desk_error

    try:
        desk = current_desk()
        cache = desk.market.cache_stats() if desk is not None else None
        snapshots = desk.market.snapshot_stats() if desk is not None else None
    except Exception as exc:  # noqa: BLE001 - 统计失败不等于进程不健康
        cache = {"error": f"{type(exc).__name__}: {exc}"}
        snapshots = None
    return {"ok": True, "mode": mode(), "as_of": clock.now().isoformat(),
            "security": {"token_is_default": token_is_default()},
            # A book that failed to load is not a statistic: it means every
            # route that touches the account is a 500. `/health` stays up on
            # purpose (it is the one endpoint a monitor can ask), so it has to
            # be able to say *why* the account is missing.
            "book_error": desk_error(),
            # 单写者约束：多个 worker 会各自持有一份内存账本，读彼此不可见。
            "workers": {"expected": 1, "configured": _configured_workers()},
            # 跑的是哪一份代码。改了文件不重启，进程就跑旧逻辑——而症状是
            # "行为变了但说不清为什么"。这一块回答"该重启了吗"。
            "build": build_info(),
            "cache": cache,
            # 这一块回答"进程里的日历/股票池/更名表是哪一刻的"。日更之后它们
            # 必须自己重建：`universe_built_at` 停在早上，就是 ST 带宽还按早上
            # 那份更名表在算。
            "market": snapshots,
            "config": {
                "params": len(values),
                "sources": sources,
                "missing": settings.missing(),
                "overridden": sorted(k for k, v in values.items() if v["source"] == "env"),
                # 进程环境里还设着、但已经不再生效的旧变量（``INITIAL_CASH``、
                # ``SAMPLES_CASH``）。它们曾经压过配置，所以有人会继续设；
                # 删掉读取之后如果连提都不提，就只是把静默换了个方向。
                "legacy_env_ignored": settings.legacy_env(),
            },
            # 计数这件事本身也要能被问。`/usage` 的 0 次调用有两种含义——没人用，
            # 或者一次都没写成功——只有这里能分开；`db` 说明记到了哪个文件，
            # 而 `/usage` 报的是它自己读的那个文件。
            "usage": usage_stats(),
            # 熔断表按路径形状分 bucket，而路径由调用方写。`/health` 是唯一
            # 免鉴权端点，所以这里报的是"有多少、上限多少"——没有它，这张表
            # 涨到多少都只能靠读源码猜。
            "breaker": _breaker_stats()}


@app.get("/dashboard.json", dependencies=[Depends(require_token)])
def dashboard_json(slim: bool = False) -> dict:
    """Today's desk as data: timeline, account, queue, arena, persona, data.

    This is the machine-readable half of the desktop window — same blocks, same
    builder. The HTML page that used to sit next to it was removed: one human
    interface is enough, and two of them drift apart at the first schema change.

    ``?slim=1`` trims the blocks that duplicate their own endpoints, for hosts
    whose tool layer refuses an oversized body outright. Default stays full so
    the frozen evening snapshot and the desktop window keep their shape.
    """
    from service.api.dashboard import collect

    return collect(trading_api.get_desk(), slim=slim)


@app.get("/account", dependencies=[Depends(require_token)])
def account() -> dict:
    """The ledger, read from the same view ``/portfolio`` and ``/risk`` use.

    This route used to compute equity, drawdown and the position list itself. Two
    implementations of one account is how ``drawdown`` ended up a 20-digit string
    here (``-0.00228731278603862530110``) and a rounded float on the other two
    routes: same number, different type, and a reader had no way to tell which was
    the value and which was a formatting artefact. The position list here also
    carried no ``name``, no ``weight`` and no ``sleeve``, so a caller reading only
    this route could not see concentration at all.

    ``snapshot`` stays because it answers a different question: which orders and
    fills produced these balances. ``/orders`` gives it on its own.
    """
    desk = trading_api.refreshed_book(trading_api.get_desk())
    # One lock, two reads. ``summary`` takes it on its own, but ``snapshot`` used
    # to be read after the lock was released: a fill landing in between produced
    # an account block from one moment and an orders/fills block from another,
    # and the two disagreed about which order produced which balance. ``RLock``
    # makes the nesting a no-op.
    with desk.book.lock:
        summary = desk.book.summary()
        snapshot = desk.book.ledger.snapshot()
    return {
        **summary,
        "as_of": clock.now().isoformat(),
        "mode": mode(),
        "snapshot": snapshot,
    }


@app.get("/context", dependencies=[Depends(require_token)])
def context() -> dict:
    """The one-screen brief. Superseded by ``/context_pack`` but kept working.

    This route used to answer three questions with placeholders: ``regime`` was
    the string ``"unknown"`` (a P2 note left in production), ``sentiment`` was
    ``None`` on any day without a same-day snapshot, and ``memory`` was an empty
    list tagged ``"P3"``. ``regime.compute`` had been shipped for weeks and was
    answering ``stressed``; the previous session's sentiment block was on disk.
    A caller that trusted this route was reading blanks where the desk already
    knew the answer, which is worse than a missing field — a missing field at
    least prompts a second question.
    """
    desk = trading_api.get_desk()
    # Read the account once, under the book lock. The three fields below used to
    # be taken straight off the live `Account` while a fill could be running —
    # `positions.values()` raises "dictionary changed size during iteration" if a
    # new name is bought mid-read, which is a 500 on a route the model calls
    # every session.
    account = desk.book.summary()
    brief = data_api.brief()
    day = brief.get("last_trading_day")

    regime = None
    if day:
        try:
            from service.trading.regime import compute as regime_compute

            regime = regime_compute(desk.market, day).as_dict()
        except (KeyError, ValueError, FileNotFoundError, OSError):
            regime = None

    recent_cases = []
    try:
        for row in desk.paper.list_cases(limit=3):
            recent_cases.append({
                "symbol": row.get("symbol"),
                "closed_at": row.get("closed_at"),
                "pnl": row.get("pnl"),
                # 6 decimals: a rate, not a float printout. `0.008083874231709023`
                # reads as precision the reader can use and is not — every
                # consumer multiplies it by 100 and rounds to two places anyway.
                "pnl_pct": (None if row.get("pnl_pct") is None
                            else round(float(row["pnl_pct"]), 6)),
                "strategy": row.get("strategy"),
            })
        journal_count = len(desk.paper.search_journal("", 50))
    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
        # ``sqlite3.Error`` is **not** an ``OSError``; leaving it out meant a
        # locked or corrupt paper DB turned the whole brief into a 500. Memory is
        # additive: the question this route answers is "may I trade", and that
        # answer does not depend on the journal being readable.
        journal_count = None
        recent_cases = [{"error": f"{type(exc).__name__}: {exc}"}]

    return {
        "as_of": clock.now().isoformat(),
        "mode": mode(),
        "market": {
            # The label is what a strategy gates on; the detail carries the four
            # measured components and the plain-language permission that goes
            # with it. "unknown" here would hide a stress regime.
            "regime": regime["label"] if regime else "unknown",
            "regime_detail": regime,
            # `data_ok` was a third name for `brief.core.usable` (the pack calls
            # it `core_usable`, the brief calls it `usable`), and next to
            # `regime_detail.permission` it read as a permission. One property,
            # one name: what these two booleans measure is whether the datasets
            # behind a sleeve are present, not whether the sleeve may be used.
            "core_usable": brief["core"]["usable"],
            "core_missing": brief["core"]["missing"],
            "aggressive_usable": brief["aggressive"]["usable"],
            "aggressive_missing": brief["aggressive"]["missing"],
            "last_trading_day": day,
        },
        # Flat keys plus the date they belong to, so a weekend or a missed
        # snapshot shows the last real tape instead of "unknown".
        "sentiment": brief["sentiment"] or {"stage": "unknown",
                                            "note": "没有可读的情绪快照；先跑 snapshot_intraday"},
        "data_age_seconds": brief["quotes"]["age_seconds"],
        "portfolio": {
            "cash_available": account["cash_available"],
            "equity": account["equity"],
            "positions": account["position_count"],
        },
        "risk": {
            # Read from the book, not hardcoded. These two fields tell the model
            # whether it may still take risk; reporting False while the operator
            # has pulled the kill switch is exactly the failure mode the switch
            # exists to prevent. Taken from the same snapshot as the portfolio
            # above, so the two blocks cannot disagree.
            "kill_switch": bool(account["kill_switch"]),
            "safe_mode": bool(account["safe_mode"]),
            "note": "来自账本状态；/risk 是同一份值的只读视图",
        },
        "memory": {
            # Scale, not search results: this route is a glance. The point is
            # whether there is anything to remember at all, and the last few
            # outcomes, which is what stops "this time is different".
            "closed_cases": recent_cases,
            "journal_entries": journal_count,
            "note": "用 quant_memory_search 查具体内容；这里只给最近结果与规模",
        },
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>quant-desk · paper account</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #0f1115; color: #e6e6e6; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: .02em; }
  .sub { color: #8b949e; margin-bottom: 24px; }
  .bar { display: flex; gap: 8px; margin-bottom: 16px; }
  input { flex: 1; background: #161b22; border: 1px solid #30363d; color: #e6e6e6; padding: 8px 10px; border-radius: 6px; }
  button { background: #d4a017; color: #0f1115; border: 0; padding: 8px 14px; border-radius: 6px; font-weight: 700; cursor: pointer; }
  pre { background: #161b22; border: 1px solid #30363d; padding: 16px; border-radius: 8px; overflow: auto; white-space: pre-wrap; }
  .hint { color: #8b949e; font-size: 12px; margin-top: 8px; }
  .notice { margin: 0 0 20px; padding: 14px 16px; border: 1px solid #d4a017;
            border-radius: 8px; background: #1c1a12; line-height: 1.8; }
  .notice b { color: #ffd166; }
  .notice code { color: #e8c97a; }
  .notice a { color: #ffd166; }
</style>
</head>
<body>
<div class="wrap">
  <h1>quant-desk · paper account</h1>
  <div class="sub">P1 · partitioned Parquet + lineage · 127.0.0.1 only · no real broker</div>
  <div class="notice">
    这是遗留的调试页；<b>量化台前端在 <a href="/desk/">/desk/</a></b>。<br>
    桌面窗口：运行根目录 <code>panel.bat</code>，会自动注入令牌。<br>
    浏览器手动打开：<code>/desk/#/today?t=&lt;QUANT_TOKEN&gt;</code>，令牌取自 <code>.env</code> 的
    <code>QUANT_TOKEN</code>；直接开 <code>/desk/</code> 而未带令牌会得到 401，
    页面显示「口令失效了」是鉴权提示，不是前端故障。<br>
    午休或收盘时 <code>is_realtime=false</code>、<code>rt_quote.stale=true</code> 是时点的正常表现。
  </div>
  <div class="bar">
    <input id="token" placeholder="X-Quant-Token" autocomplete="off" />
    <button onclick="load()">Load</button>
  </div>
  <pre id="out">Enter the QUANT_TOKEN from .env, then click Load.</pre>
  <div class="hint">API: /health, /account, /context, /data/status, /data/quality. All but /health require X-Quant-Token.</div>
</div>
<script>
async function load() {
  const out = document.getElementById('out');
  const token = document.getElementById('token').value.trim();
  out.textContent = 'loading...';
  try {
    const r = await fetch('/account', { headers: { 'X-Quant-Token': token } });
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    out.textContent = String(e);
  }
}
</script>
</body>
</html>"""
