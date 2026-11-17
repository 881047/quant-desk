"""Stops for the read-only live account — the book the kernel could not see.

2026-11-02: 账户 79.4% 仓位持有一只票，`quant_stops` 返回"没有持仓，所以没有止损"，
从 13:00 到收盘没有任何自动告警。原因不是 stop 算错了，是它**只读模拟盘**：
`paper.sqlite` 里没有那 300 股，而实盘账本 `external.sqlite` 是另一套只读体系。

这本账和模拟盘不是同一件事，所以不硬塞进 `open_stops`：

* 模拟盘的仓位带着 ticket，止损价在开票时就定了（`stop_price_final`）；
* 实盘的仓位只有成交与成本，止损是一个**规则**（成本 × 0.95）或一个人**钉下的价位**
  （"11.74" 是照风险预算反算出来的，不是任何百分比算得出来的）。

所以这里只做两件事：按规则算出止损，以及**说出这本账有多旧**。后者比前者重要——
账本里最后一条成交是 10-30、今天是 11-02，那么"没有止损"的真实含义是
"今天买了什么我不知道"，而不是"没有仓位"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from service.kernel.clock import SHANGHAI

#: 默认规则：成本下 5%。给一个数是因为"没有止损"比"一个粗止损"更危险。
DEFAULT_PCT = 0.05
#: `atr` 规则的倍数。
ATR_MULTIPLE = 2.5
ATR_WINDOW = 14


@dataclass
class ExternalStop:
    symbol: str
    qty: int
    cost: float
    last: float | None
    stop: float | None
    rule: str
    distance_pct: float | None
    breached: bool
    price_source: str
    price_ts: str | None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "cost": round(self.cost, 4),
            "last": None if self.last is None else round(self.last, 4),
            "stop": None if self.stop is None else round(self.stop, 4),
            "rule": self.rule,
            "distance_pct": (None if self.distance_pct is None
                             else round(self.distance_pct, 4)),
            "breached": self.breached,
            "price_source": self.price_source,
            "price_ts": self.price_ts,
            "note": self.note,
        }


def load_overrides(root: Path | str | None = None) -> dict[str, dict]:
    """`configs/stops.json`：一个人钉下的价位，优先于任何规则。

    ```json
    {"002602.SZ": {"stop": 11.74, "note": "风险预算反算：亏 2% 账户"}}
    ```
    """
    path = Path(root or Path(__file__).resolve().parents[2]) / "configs" / "stops.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): dict(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _atr(desk, symbol: str, day: str) -> float | None:
    """ATR over the last 14 sessions, from stored daily bars. None when short."""
    try:
        frame = desk.market.daily(day, columns=["ts_code", "trade_date", "high", "low", "close"])
    except (ValueError, KeyError):
        return None
    if frame is None or frame.empty:
        return None
    mine = frame[frame["ts_code"].astype(str) == symbol].sort_values("trade_date")
    if len(mine) < ATR_WINDOW + 1:
        return None
    high = mine["high"].astype(float).to_numpy()
    low = mine["low"].astype(float).to_numpy()
    close = mine["close"].astype(float).to_numpy()
    ranges = []
    for index in range(len(mine) - ATR_WINDOW, len(mine)):
        prev = close[index - 1]
        ranges.append(max(high[index] - low[index],
                          abs(high[index] - prev), abs(low[index] - prev)))
    return sum(ranges) / len(ranges)


def external_stops(desk, *, root: Path | str | None = None, rule: str = "pct5",
                   pct: float = DEFAULT_PCT, quotes: dict | None = None) -> dict:
    """Every live position with a stop, plus how old the book that produced it is."""
    from service.trading.external import ExternalStore

    store = ExternalStore(Path(root or Path(__file__).resolve().parents[2])
                          / "data" / "external.sqlite")
    held = {symbol: position for symbol, position in store.holdings().items()
            if position.qty > 0}
    trades = store.trades()
    book_as_of = max((row["trade_date"] for row in trades), default=None)
    day = desk.market.latest_bar_date()
    overrides = load_overrides(root)

    rows: list[ExternalStop] = []
    for symbol, position in sorted(held.items()):
        quote = (quotes or {}).get(symbol) or {}
        last = quote.get("last")
        price_source = "quote" if last else "unavailable"
        if not last:
            snap = desk.market.snapshot(symbol, day) if day else None
            if snap is not None:
                last, price_source = float(snap.last), "daily"
        pinned = overrides.get(symbol) or {}
        if pinned.get("stop"):
            stop, used = float(pinned["stop"]), "custom"
            note = str(pinned.get("note") or "")
        elif rule == "atr" and day and (atr := _atr(desk, symbol, day)):
            stop, used, note = position.cost - ATR_MULTIPLE * atr, "atr", ""
        elif rule == "atr":
            # 数据不够就别假装算得出来：这条要显示"没算出来"，不是"没有止损"。
            stop, used, note = None, "atr", f"不足 {ATR_WINDOW + 1} 根日线，ATR 算不出来"
        else:
            stop, used, note = position.cost * (1 - pct), f"pct{int(pct * 100)}", ""
        distance = None if (stop is None or not last) else (last / stop - 1) * 100
        rows.append(ExternalStop(
            symbol=symbol, qty=int(position.qty), cost=float(position.cost),
            last=last, stop=stop, rule=used,
            distance_pct=distance,
            breached=bool(stop is not None and last is not None and last <= stop),
            price_source=price_source, price_ts=quote.get("quote_ts") or day,
            note=note,
        ))

    views = [row.as_dict() for row in rows]
    # 账本比"今天"旧 = 今天的成交没记进来。这不是"没有仓位"，是"我不知道今天的仓位"。
    # 用今天（交易日）比，不用最新日线比：11-02 当天日线还没落库，拿它比永远是 0 天，
    # 而那次事故恰恰发生在盘中。
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    trading_today = bool(desk.market.calendar.is_trading_day(today))
    lag = 0
    if book_as_of and trading_today and str(book_as_of) < today:
        lag = 1 if str(book_as_of) < today else 0
    return {
        "account": "external",
        "as_of": day,
        "book_as_of": book_as_of,
        "book_lag_days": lag,
        "stale_book": bool(lag),
        "rule": rule if not overrides else f"{rule}+custom",
        "stops": views,
        "breached": [view["symbol"] for view in views if view["breached"]],
        "unpriced": [view["symbol"] for view in views if view["last"] is None],
        "note": ("实盘账本最后一条成交在 " + str(book_as_of)
                 + "，今天还没有记录。若今天买卖过，必须先用 "
                   "`python scripts/external.py add` 记进来——否则这里的仓位不是你的仓位，"
                   "`stops` 里也不会有它。今天没交易则可以忽略这句。"
                 if lag else
                 "实盘（只读）：来自 data/external.sqlite 的成交重放，不下单。"),
    }
