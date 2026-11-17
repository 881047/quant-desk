"""May the desk draw a conclusion about this session yet?

The question is not "is the process alive". It is: **would a verdict written
now be reproducible tomorrow from stored bytes?** Those are different questions,
and on 2026-10-29 the system answered only the first.

That day the daily job never published 10-29 (the scheduled run was missed and
the catch-up collided with a manual backfill). `daily` still ended at 10-28, so
`context_pack` said `data_ready: false` — and that was just a *field*. Nothing
read it. A close report was written anyway, off an intraday snapshot the model
had fetched itself, and claimed 6 affordable names where the stored session says
23. Every number in it was internally consistent; the input was not the session.

So this module returns a verdict about verdicts, and callers must honour `ok`.
Three states drive the code:

* ``SESSION_OPEN``  — the session has not closed; intraday observation only.
* ``AWAITING_PUBLISH`` — the session closed but the bar is not stored yet.
  This is the 15:00–17:30 window, and it is exactly where the bad report was
  written. Waiting is cheap; a wrong verdict is not.
* ``THIN_COVERAGE`` — a bar exists but covers materially fewer names than the
  previous session. A truncated feed is more dangerous than a missing one,
  because it looks like data.

`READY` is the only state that permits a written conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from service import settings
from service.kernel.clock import SHANGHAI

# The close is 15:00, but a "close" report written at 15:01 reads an intraday
# snapshot, not the session. The bar is the session; the snapshot is a moment.
CLOSE = time.fromisoformat(settings.text("readiness.close_time"))

# Below this share of the previous session's row count, the feed is treated as
# truncated rather than quiet. A real session never loses 10% of its names.
MIN_COVERAGE = settings.num("readiness.min_coverage")

# Datasets whose absence changes a *verdict* rather than a chart. `daily` prices
# the outcome; `stk_limit` decides whether a fill was legal at all. The ths
# ladder is deliberately excluded: it explains structure, it does not settle
# P&L, and a vendor outage there must not block "did this trade make money".
VERDICT_DATASETS = ("daily", "stk_limit")


@dataclass(frozen=True)
class Readiness:
    ok: bool
    code: str
    target: str
    today: str
    is_trading_day: bool
    session_closed: bool
    last_bar: str | None
    bars_behind: int
    coverage: float | None = None
    missing: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "target": self.target,
            "today": self.today,
            "is_trading_day": self.is_trading_day,
            "session_closed": self.session_closed,
            "last_bar": self.last_bar,
            "bars_behind": self.bars_behind,
            "coverage": None if self.coverage is None else round(self.coverage, 4),
            "missing": list(self.missing),
            "detail": self.detail,
            "blocks": [] if self.ok else self._blocks(),
        }

    def _blocks(self) -> list[str]:
        if self.code == "SESSION_OPEN":
            return ["verdict", "settlement"]
        if self.code in ("AWAITING_PUBLISH", "THIN_COVERAGE"):
            return ["verdict", "settlement", "close_report"]
        if self.code == "NO_CALENDAR":
            return ["verdict", "settlement", "close_report", "pricing"]
        return []


def _calendar_days(market) -> list[str]:
    return list(getattr(market.calendar, "days", ()) or ())


def _coverage(market, target: str, previous: str | None) -> float | None:
    """Today's row count against the previous session's.

    Row counts, not prices: the failure this catches is a truncated feed, which
    shows up as a short frame long before it shows up as a wrong number.
    """
    if not previous:
        return None
    try:
        today_rows = len(market.daily(target))
        prev_rows = len(market.daily(previous))
    except (KeyError, FileNotFoundError, ValueError):
        return None
    if prev_rows <= 0:
        return None
    return today_rows / prev_rows


def check(market, *, day: str | None = None, now: datetime | None = None) -> Readiness:
    """Decide whether ``day`` (default: today) may carry a written conclusion."""
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = now.strftime("%Y%m%d")
    target = day or today
    days = _calendar_days(market)
    if not days:
        return Readiness(
            ok=False, code="NO_CALENDAR", target=target, today=today,
            is_trading_day=False, session_closed=False, last_bar=None,
            bars_behind=0, detail="交易日历为空，无法判断任何一天是否该有数据",
        )

    is_trading = target in days
    last_bar = market.latest_bar_date()
    closed = now.time() >= CLOSE if target == today else True

    if not is_trading:
        return Readiness(
            ok=True, code="NON_TRADING_DAY", target=target, today=today,
            is_trading_day=False, session_closed=True, last_bar=last_bar,
            bars_behind=0, detail=f"{target} 不是交易日，没有需要结算的会话",
        )

    if not closed:
        return Readiness(
            ok=False, code="SESSION_OPEN", target=target, today=today,
            is_trading_day=True, session_closed=False, last_bar=last_bar,
            bars_behind=max(0, len([d for d in days if d < target and last_bar and d > last_bar])),
            detail=f"{target} 尚未收盘，此刻写下的只能算盘中观察，不能当对错判定",
        )

    behind = [d for d in days if d <= target and (last_bar is None or d > last_bar)]
    if last_bar is None or last_bar < target:
        return Readiness(
            ok=False, code="AWAITING_PUBLISH", target=target, today=today,
            is_trading_day=True, session_closed=True, last_bar=last_bar,
            bars_behind=len(behind),
            detail=(f"{target} 已收盘但日线未落库（last_bar={last_bar or '无'}，"
                    f"落后 {len(behind)} 个交易日）。用实时快照写的数字明天无法复现，"
                    f"等 daily_job 发布后再判定。"),
        )

    prev = next((d for d in reversed(days) if d < target), None)
    coverage = _coverage(market, target, prev)
    missing = _missing_datasets(market, target)
    if coverage is not None and coverage < MIN_COVERAGE:
        return Readiness(
            ok=False, code="THIN_COVERAGE", target=target, today=today,
            is_trading_day=True, session_closed=True, last_bar=last_bar,
            bars_behind=0, coverage=coverage, missing=tuple(missing),
            detail=(f"{target} 只有 {coverage:.0%} 的上一交易日行数，疑似数据截断。"
                    f"缺行的市场均值会被少数股票代表，据此判定的对错不可信。"),
        )
    if missing:
        return Readiness(
            ok=False, code="AWAITING_PUBLISH", target=target, today=today,
            is_trading_day=True, session_closed=True, last_bar=last_bar,
            bars_behind=0, coverage=coverage, missing=tuple(missing),
            detail=f"{target} 的 {'/'.join(missing)} 尚未发布，涨跌停合法性无法复核",
        )
    return Readiness(
        ok=True, code="READY", target=target, today=today,
        is_trading_day=True, session_closed=True, last_bar=last_bar,
        bars_behind=0, coverage=coverage, missing=(),
        detail=f"{target} 日线已落库，覆盖率 {coverage:.0%}，可以写入判定" if coverage
        else f"{target} 日线已落库，可以写入判定",
    )


def _missing_datasets(market, target: str) -> list[str]:
    """Which verdict-critical datasets have not covered ``target`` yet."""
    registry = getattr(getattr(market, "store", None), "registry", None)
    if registry is None:
        return []
    try:
        records = registry.list_manifest()
    except (OSError, ValueError):
        return []
    covered = {r.dataset for r in records
               if r.cover_end and r.cover_end >= target and r.status in ("ok", "warn")}
    return [dataset for dataset in VERDICT_DATASETS if dataset not in covered]
