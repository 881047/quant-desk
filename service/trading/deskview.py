"""桌面窗口要的数据，一次装配好。

窗口本身（``scripts/panel.pyw``）不做任何计算：它只会画。所有判断——对账、
闸门、数据是否就绪——都在这里，因为同一件事如果窗口算一遍、服务端再算一遍，
第一次出现分歧就没人能说清哪个是对的。

三块分开返回，不是一次返回全部，是因为它们花的时间差一个数量级：账户是一
次 SQLite 读加一次行情，事件是三次表扫描，系统块要读数据集与注册表。窗口先
把账户画出来，剩下的到了再填，比转三秒钟白屏要好。
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from service import settings
from service.kernel.clock import SHANGHAI

from . import timeline as timeline_mod
from .panel import account_panel

ROOT = Path(__file__).resolve().parents[2]


def _plain(value: Any, quality: list[str] | None, path: str) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v, quality, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, quality, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            if quality is not None:
                quality.append(path)
            return None
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def plain(value: Any) -> Any:
    """Return JSON-safe values, converting non-finite numbers to ``null``."""
    return _plain(value, None, "$")


_DATASET_LABEL = {
    "rt_quote": "实时报价",
    "pools": "涨停池",
    "limit_up_pool": "涨停池",
    "limit_pool_t0": "涨停池（当日）",
    "limit_down_pool": "跌停池",
    "break_board_pool": "炸板池",
    "ths_pools": "同花顺涨停池",
    "ths_ladder": "连板天梯",
    "ths_seal_rate": "封板率",
    "ths_auction": "竞价面板",
    "sentiment": "情绪面板",
}


def _blocked_text(brief: dict, probe: dict) -> list[str]:
    """Why the hot-money sleeve cannot trade, in words rather than dataset keys.

    "游资袖不可用：rt_quote" is a variable name shown to a person. The useful
    sentence is whether the market is closed, the feed is down, or a dataset is
    a week old — those are three different situations and only one of them is
    fixed by waiting for the open.
    """
    missing = ((brief or {}).get("aggressive") or {}).get("missing") or []
    out: list[str] = []
    for key in missing:
        if key == "rt_quote":
            rt = (probe or {}).get("rt_quote") or {}
            if rt.get("tradable"):
                out.append("实时报价可用")
            elif rt.get("feed_available"):
                label = rt.get("session_label") or "盘后"
                out.append(f"现在是{label}，行情源只回收盘价，不能作为成交价——"
                           f"开盘后自动恢复")
            else:
                out.append("行情源不可用（断网或缺少密钥）")
        else:
            out.append(f"{_DATASET_LABEL.get(key, key)}落后或缺失")
    return out


def day_of(desk) -> str:
    return desk.market.latest_bar_date() or datetime.now(SHANGHAI).strftime("%Y%m%d")


def account(desk, *, root: Path | None = None, read_client: bool = False,
            with_quotes: bool = True, quote_timeout: float = 6.0) -> dict | None:
    return account_panel(desk, root=root or ROOT, read_client=read_client,
                         with_quotes=with_quotes, quote_timeout=quote_timeout)


def events(desk, day: str, limit: int = 60) -> list[dict]:
    try:
        return plain(timeline_mod.collect(desk, day, limit=limit))
    except Exception:  # noqa: BLE001 - a broken table must not blank the window
        return []


# These blocks read dataset freshness and the registry; they change once a day,
# and they take 7-8 seconds. A 60-second cache keeps "刷新" instant without ever
# showing a stale *position* — positions come from `account()`, which is not
# cached.
_SYSTEM_CACHE: dict[tuple, tuple[float, dict]] = {}
# The window is a webview whose JS API calls run on worker threads: the refresh
# button and the 60 s auto-refresh can land at once. ``sorted(_SYSTEM_CACHE)``
# while the other thread inserted raised "dictionary changed size during
# iteration" — a 500 ms red toast for a cache that is fine. The lock covers the
# dict operations only, never the 7-8 s build below it.
_SYSTEM_LOCK = threading.Lock()
_SYSTEM_TTL = settings.num("cache.system_ttl")


# How many day-payloads to keep. One per session is the natural working set
# (today plus an audit of yesterday); beyond that the cache is just a leak.
_SYSTEM_CACHE_LIMIT = settings.integer("cache.system_limit")


def _cache_key(desk, day: str, base: Path) -> tuple:
    """Keyed on the databases, not on ``id(desk)``.

    ``id()`` is a memory address, and a rebuilt desk is very likely to be handed
    the same address — so a fresh desk read the previous one's arena scoreboard.
    On the desktop panel that is a stale page that looks current. Two paths that
    are the same database are the same desk for caching purposes.
    """
    return (day, str(base),
            str(getattr(desk.paper, "db_path", "")),
            str(getattr(desk.registry, "db_path", "")))


def system(desk, day: str, *, root: Path | None = None, ttl: float | None = None) -> dict:
    """The blocks that answer "why did it do nothing", each failing on its own."""
    base = Path(root) if root else ROOT
    key = _cache_key(desk, day, base)
    limit = _SYSTEM_TTL if ttl is None else ttl
    # `monotonic`, not `time.time()`: this TTL measures elapsed seconds, and a
    # clock correction (NTP step, DST) made a fresh payload look hours old —
    # rebuilding the 7-8 s block for nothing — or an old one look fresh.
    with _SYSTEM_LOCK:
        hit = _SYSTEM_CACHE.get(key)
        if hit and limit > 0 and time.monotonic() - hit[0] < limit:
            return hit[1]
        if len(_SYSTEM_CACHE) >= _SYSTEM_CACHE_LIMIT:
            # A day-payload is tens of kilobytes; a process that runs for a year
            # accumulated one per session and never dropped one.
            for stale in sorted(_SYSTEM_CACHE, key=lambda k: _SYSTEM_CACHE[k][0]
                                )[: len(_SYSTEM_CACHE) // 2]:
                _SYSTEM_CACHE.pop(stale, None)

    non_finite: list[str] = []

    def guard(fn, fallback, path):
        try:
            return _plain(fn(), non_finite, path)
        except Exception as exc:  # noqa: BLE001
            if isinstance(fallback, dict):
                return {**fallback, "error": f"{type(exc).__name__}: {exc}"}
            return fallback

    def regime_label() -> str:
        from .regime import compute

        return compute(desk.market, day).label

    def probe_map() -> dict:
        from .persona import probe

        return probe(desk.market, desk.registry)

    def persona_block():
        from . import persona as persona_mod

        return {"current": persona_mod.current(desk.paper, regime_block["label"],
                                               probe_block),
                "available": persona_mod.describe(regime_block["label"], probe_block)}

    def brief_block():
        from service.adapters.brief import build_brief

        # Passing None here said "no quote at all", and `build_brief` answers that
        # by adding `rt_quote` to the aggressive sleeve's missing list — so the
        # window reported "游资袖不可用：rt_quote" every single time, including
        # at 15:01 with a live feed answering. The probe already knows whether a
        # price can be filled at; that answer is what belongs here.
        rt = (probe_block.get("rt_quote") or {}) if isinstance(probe_block, dict) else {}
        rt_age = 0.0 if rt.get("tradable") else None
        return build_brief(summary=desk.registry.summary(), calendar=desk.market.calendar,
                           today=day, rt_age_seconds=rt_age, sentiment=sentiment)

    def arena_block():
        from .arena import scoreboard

        return scoreboard(desk)

    def duty_block():
        from .duty import build as build_duty

        return build_duty(desk, day)

    sentiment = None
    panel_path = base / "data" / "sentiment" / f"{day}.json"
    try:
        if panel_path.exists():
            sentiment = (json.loads(panel_path.read_text(encoding="utf-8")) or {}).get(
                "sentiment")
    except (OSError, json.JSONDecodeError):
        sentiment = None

    label = guard(regime_label, "", "regime")
    regime_block = {"label": label}
    probe_block = guard(probe_map, {}, "probe")
    if not isinstance(probe_block, dict):
        probe_block = {}

    brief_payload = guard(brief_block, {}, "brief")
    persona_payload = guard(persona_block, None, "persona")
    if isinstance(persona_payload, dict):
        for item in persona_payload.get("available") or []:
            if isinstance(item, dict):
                item["missing_labels"] = [
                    _DATASET_LABEL.get(key, key)
                    for key in item.get("missing_requirements") or []]

    payload = {
        "day": day,
        "aggressive_blocked": _blocked_text(brief_payload, probe_block),
        "paper": guard(lambda: desk.book.summary(), {}, "paper"),
        "arena": guard(arena_block, {}, "arena"),
        "duty": guard(duty_block, {"items": []}, "duty"),
        "persona": persona_payload,
        "regime": label,
        "brief": brief_payload,
        "sentiment": _plain(sentiment, non_finite, "sentiment"),
        "probe": probe_block,
        "usage": guard(lambda: desk.paper.usage_summary(days=1), {}, "usage"),
        "quotes": guard(lambda: desk.quote_health(), {}, "quotes"),
        "quality": {"non_finite_fields": non_finite},
    }
    with _SYSTEM_LOCK:
        _SYSTEM_CACHE[key] = (time.monotonic(), payload)
    return payload
