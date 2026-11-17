"""Compact data-trust brief for the LLM.

The agent should never guess whether today's data is usable. This module turns
the registry into a small, explicit verdict:

    {"core": {"usable": true, "missing": []},
     "aggressive": {"usable": false, "missing": ["limit_up_pool"]}}

Rules are intentionally about *availability*, not about strategy: the model
decides what to do with the answer, but it cannot invent a fresh dataset.
"""

from __future__ import annotations

from datetime import datetime

from service import settings

from .catalog import SNAPSHOT_DATASETS, coverage_boundary
from .pit import TradingCalendarStore

# `adj_factor` left this set on 2026-10-28. `adj_close` is now built from the
# exchange's `pre_close` (see `MarketRepository._attach_adj`), so nothing in the
# decision path reads the vendor factor any more — keeping it here made a stale
# or corrupt factor block new positions while the prices it guards were fine.
CORE_DATASETS = ("daily", "stk_limit")

# What a hot-money decision needs: the three 东财 pools plus the 同花顺 tape.
# The 同花顺 half was missing here until 2026-10-26, so a sleeve could read
# "usable" while 封板率/晋级率/天梯 were a week stale — the pools come from a
# different vendor and fail independently.
AGGRESSIVE_DATASETS = (
    "limit_up_pool", "broken_board_pool", "limit_down_pool",
    "ths_limit_up", "ths_limit_down", "ths_limit_break",
    "ths_ladder", "ths_limit_stat",
)
# Read when available, never a blocker: 龙虎榜 publishes at 20:00, and the
# auction panel only exists before the open.
AUX_DATASETS = ("ths_lhb", "ths_lhb_seat", "ths_auction", "ths_anomaly")

DECISION_DATASETS = set(CORE_DATASETS) | set(AGGRESSIVE_DATASETS) | set(AUX_DATASETS)

# `cover_end` has two different meanings, and only one of them can be late.
# For a per-session panel it is a coverage boundary: `cb_daily` ending on 10/23
# means three sessions are missing. For a reference table it is the newest
# entity's own date — `stock_basic` ends on the day the last company listed,
# `trade_cal` in 2027 — so lagging it measures nothing. `rt_quote` is a snapshot
# whose currency the `quotes` block already reports, and precisely: feed up,
# fill impossible. Opt-in datasets are excluded for a different reason: they are
# not fetched on purpose, so demanding coverage would be an alarm nobody can act
# on. Lagging any of these would raise a false alarm every evening.
NOT_COVERAGE_BOUNDARY = set(SNAPSHOT_DATASETS)


def _coverage_boundary(name: str) -> bool:
    # 判定搬到 catalog，是因为 `/data/status` 也要回答同一个问题（A3）。两处
    # 各判一次的规则，和两份 `cost_mult` 一样，迟早分叉。
    # 快照那半边在这里再挡一次，是让"不看 cover_end"这条口径留在原来的名字上。
    return name not in NOT_COVERAGE_BOUNDARY and coverage_boundary(name)


def _lag(calendar: TradingCalendarStore, cover_end: str | None, last: str) -> int | None:
    if not cover_end:
        return None
    if cover_end >= last:
        return 0
    days = [d for d in calendar.days if cover_end < d <= last]
    return len(days)


def build_brief(
    *,
    summary: list[dict],
    calendar: TradingCalendarStore,
    today: str,
    rt_age_seconds: float | None,
    sentiment: dict | None,
    stale_after: int | None = None,
    intraday: bool | None = None,
) -> dict:
    """`intraday=True` means today's bar cannot exist yet, so "current" is yesterday.

    Without it the brief demanded today's bar from 09:30 onwards and reported
    `core.usable=false` all session — while `Guard.data_checks` asked the same
    question with `as_of = prev(date)` and allowed the order. The prompt tells the
    model「data.core_ok=false 时不开新仓」, so the false blocker stopped trading
    every morning until 17:30. Two answers to one question again.

    ``intraday=None`` infers it only for the real current session: a historical or
    fixture date must not depend on the hour the test runs.
    """
    if stale_after is None:
        # One source for the freshness bound: ``realtime.stale_after``. This
        # module sits in adapters, so it reads the setting directly instead of
        # importing the trading layer; it is the same number tripwire and
        # stop_gate use, not a sixth literal.
        stale_after = int(settings.num("realtime.stale_after"))
    by = {row["dataset"]: row for row in summary}
    is_trading = calendar.is_trading_day(today)
    # `now` 必须在这里定义，而不是只在 `intraday is None` 分支里：调用方显式传
    # `intraday=True` 时（测试与 /data/brief 都这么做）那个分支不执行，后面判断
    # 「现在是不是有盘口」就会 UnboundLocalError。
    now = datetime.now().astimezone()
    if intraday is None:
        intraday = bool(today == now.strftime("%Y%m%d") and is_trading and now.hour < 16)
    last = (calendar.prev(today) or today) if (is_trading and intraday) else (
        today if is_trading else (calendar.prev(today) or today))

    def info(name: str) -> dict:
        row = by.get(name, {})
        cover_end = row.get("cover_end")
        return {
            "cover_end": cover_end,
            "lag_trading_days": _lag(calendar, cover_end, last),
            "quarantined_partitions": int(row.get("quarantine_partitions") or 0),
            "warn_partitions": int(row.get("warn_partitions") or 0),
            # Errors found by the checks that still ran — the number that moves
            # when a vendor feed breaks, unlike `warn_partitions` which sat at
            # "all of them" for `daily` and therefore meant nothing.
            # 它是**问题条数**，不是坏行数：同一个分区里一条规则命中一万行也只算 1。
            # 名字一起给出去，模型才不用把 18 猜成"18 行坏了"。
            "error_issues": int(row.get("error_issues") or 0),
            "error_codes": [c for c in str(row.get("error_codes") or "").split(",") if c],
            "snapshot_id": row.get("last_snapshot_id"),
            "rows": int(row.get("rows") or 0),
        }

    core = {name: info(name) for name in CORE_DATASETS}
    aggressive = {name: info(name) for name in AGGRESSIVE_DATASETS}
    aux = {name: info(name) for name in AUX_DATASETS}

    # Everything outside the three decision groups: reference tables, research
    # inputs and the text channel. None of it can block a trade, which is exactly
    # why it rots unnoticed — on 2026-10-26 `cb_daily` and `news` were both three
    # sessions behind while every decision-critical dataset was current, and
    # nothing in this brief or on the page said so.
    optional = {name: info(name) for name in sorted(by) if name not in DECISION_DATASETS}
    tracked = [name for name in optional if _coverage_boundary(name)]
    stale_optional = {name: optional[name]["lag_trading_days"] for name in tracked
                      if (optional[name]["lag_trading_days"] or 0) > 0}

    # Without a calendar every lag is zero, so a three-month-old dataset reads as
    # current — the one shape of "green light" this module exists to prevent. If
    # `trade_cal` is empty the freshness question cannot be answered, and an
    # unanswerable question is not a pass.
    calendar_known = bool(getattr(calendar, "days", None))

    core_missing = [
        name for name, item in core.items()
        if item["cover_end"] is None or (item["lag_trading_days"] or 0) > 0
        or item["quarantined_partitions"] > 0
    ]
    aggressive_missing = [
        name for name, item in aggressive.items()
        if item["cover_end"] is None or (item["lag_trading_days"] or 0) > 1
        or item["quarantined_partitions"] > 0
    ]

    # A live quote is a requirement for entering a hot-money position *inside the
    # session*, where the decision is about what the tape is doing right now. It
    # is not a requirement at 19:30, when there is no tape: the order will be
    # queued for tomorrow's open, and the inputs that matter are tonight's pools
    # and bars. Demanding it all evening made `aggressive_ok` false every night,
    # and the prompt reads that flag as a ban on the sleeve — the same false
    # blocker as `core_ok` after 09:30 (P45) and `aggressive_ok` at 09:30 (P2-2).
    quotes_fresh = rt_age_seconds is not None and rt_age_seconds <= stale_after
    # `intraday` 的意思是「今天的日线还没入库」（16:00 前都为真），不等于
    # 「此刻应当产生新报价」。午休与收盘后的年龄增长是正常停帧，不是源故障。
    # 历史回放没有可靠的当时钟点，因此只有真实今天才应用这两个豁免窗口。
    is_today = today == now.strftime("%Y%m%d")
    at_tape = (9, 15) <= (now.hour, now.minute) < (15, 1)
    lunch_today = is_today and (11, 30) <= (now.hour, now.minute) < (13, 0)
    closed_today = is_today and not at_tape
    quotes_required = bool(is_trading and intraday
                           and not lunch_today and not closed_today)
    if quotes_required and not quotes_fresh:
        aggressive_missing.append("rt_quote")

    notes: list[str] = []
    if not is_trading:
        notes.append(f"{today} is not a trading day; last trading day is {last}")
    elif intraday and not closed_today:
        notes.append(f"session in progress: history is required through {last}; "
                     f"today's bar publishes after the close")
    elif intraday:
        notes.append(f"after the close ({now:%H:%M}): today's bar is not published "
                     f"yet (lands 17:30), so 'current' still means {last}")
    if not calendar_known:
        notes.append("trade_cal is empty: dataset freshness cannot be verified")
    if core_missing:
        notes.append("core data incomplete: " + ",".join(core_missing))
    if aggressive_missing:
        notes.append("aggressive sleeve blocked: " + ",".join(aggressive_missing))
    if lunch_today:
        notes.append("午休（11:30–13:00）不产生新的连续竞价快照；"
                     "报价年龄增长不是数据源故障，也不阻断 aggressive 数据可用性。")
    elif closed_today and not quotes_fresh:
        notes.append("rt_quote 在收盘后不再产生快照，age 必然超过阈值；"
                     "这一项不作为收盘后的数据源故障或 aggressive 阻断项。")
    if not aggressive_missing and not intraday and is_trading:
        notes.append("after the close: live quotes are not required for the "
                     "aggressive sleeve; today's pools and bars are the inputs")
    if sentiment is None:
        notes.append("no sentiment panel for today")

    # A partition can be `ok` with warnings or `warn` with a real defect. The
    # quality report wrote those down and nothing read it: `adj_factor` carried
    # 15 flagged rows for 2026, including one that would have turned a flat month
    # into a -99.9% return, and the brief said "usable".
    warned = {name: item["warn_partitions"] for name, item in
              {**core, **aggressive, **aux, **optional}.items()
              if item["warn_partitions"] > 0}
    if warned:
        notes.append(
            "datasets with flagged (not quarantined) partitions: "
            + ",".join(f"{k}({v})" for k, v in sorted(warned.items()))
            + " — warn means usable with a known defect; only `quarantine` blocks")

    return {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "today": today,
        "last_trading_day": last,
        "is_trading_day": is_trading,
        "core": {"usable": not core_missing and calendar_known,
                 "missing": core_missing, "datasets": core},
        "aggressive": {"usable": not aggressive_missing and calendar_known,
                       "missing": aggressive_missing, "datasets": aggressive},
        "aux": {"datasets": aux},
        "optional": {"datasets": optional, "tracked": tracked, "stale": stale_optional},
        "quotes": {"age_seconds": rt_age_seconds, "fresh": quotes_fresh,
                   "required": quotes_required,
                   "pause": ("lunch" if lunch_today else
                             "closed" if closed_today else None),
                   "stale_after": stale_after,
                   # age 为 null 时 `fresh: false` 只说了"不新"，没说"没有"。
                   **({} if rt_age_seconds is not None else {"note":
                      "没有实时报价时间戳：快照任务没跑过，或此刻只能读收盘价"})},
        "sentiment": sentiment,
        "session": {"intraday": bool(intraday), "priced_through": last},
        "quality": {"warned": warned},
        "notes": notes,
    }
