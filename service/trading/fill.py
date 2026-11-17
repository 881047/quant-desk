"""Whether an order can fill — stated as a fact, never as a probability.

The desk can price a fill to the fen and still have no idea whether it happens.
For a buy at the ceiling that gap *is* the trade: the premium a limit-up board
pays overnight (2023-2026, 65,065 boards: +2.21% mean, +1.48% median, 65.6% of
them positive) is collected only by whoever was actually filled, and the queue in
front of them is not in any feed this desk subscribes to. ``execution`` says the
same thing from the other end: ``derive_fill_ratio`` exists, no builder calls it,
and a sealed board is refused rather than filled optimistically.

So this module answers the three questions that *are* answerable, and refuses the
fourth:

* **Is the ceiling sealed right now, and did it open that way?** ``board`` — a
  stock that opened sealed has no queue position to compete for; a stock that
  sealed at 13:00 might still open again.
* **Does this order therefore have to queue?** ``queue.required`` — a market
  order cannot cross the ceiling, so "will it fill" stops being a pricing
  question and becomes a queueing one.
* **How strong is the seal?** ``seal`` — 封单 / 流通市值, ranked inside the day's
  pool. This is the measurable half of 打板 strength, and it is *not* the same
  ranking as absolute 封单额: across 198 sessions of 2026 the two top-5 lists
  agreed on 34% of names and never once matched exactly.

And it does **not** answer "what is my chance of filling". ``fill_ratio`` comes
back ``None`` with the reason attached. Inventing 0.3 there would turn an unknown
into a number, and the number would then be traded on — which is precisely the
failure this desk spends most of its code preventing.

Nothing here decides anything. ``queue.required=True`` is not "do not buy"; it is
"if you buy, you are joining a queue whose depth nobody measured".
"""

from __future__ import annotations

import bisect
import json
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from service.kernel.microstructure import PRICE_EPS

QUEUE_WHY = (
    "价格已在涨停价：市价单穿不过涨停价，成交只能靠排队。"
    "队列深度和这条委托在队里的位置都不在任何已订阅行情里，"
    "所以这里不给成交概率——给了也是编的。"
)

NEXT_STEPS = (
    "等开板（回封时才有队列可谈），"
    "或先看 seal.rank_in_pool 判断这个封单在当日池子里算强还是算弱，再决定值不值得排。"
)

# 开板率标定的落点，由 `scripts/board_open_rate.py` 写。绝对路径：服务的 cwd
# 是仓库根，但测试和面板不以它为 cwd，而一个"读不到就当没有"的静默降级会让
# 这块信息永远不出现却没人发现。
CALIBRATION = (Path(__file__).resolve().parents[2]
               / "data" / "cache" / "board_open_rate.json")
_CALIB_CACHE: dict | None = None
_CALIB_STAMP: tuple[int, int] | None = None
_CALIB_LOCK = threading.Lock()

# 档位标签必须和脚本里的一致；放这里是因为运行时要用它把当前封单比归位。
_SEAL_BANDS = ("Q1 最弱", "Q2", "Q3", "Q4", "Q5 最强")


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return number if number.is_finite() else None


def reset_cache() -> None:
    """Drop the calibration table so the next call re-reads it from disk.

    流通市值 的缓存不在这里：它已经并入 ``MarketRepository`` 的按日缓存，
    跟着那次 ``invalidate()`` 一起失效。
    """
    with _CALIB_LOCK:
        global _CALIB_CACHE, _CALIB_STAMP
        _CALIB_CACHE = None
        _CALIB_STAMP = None


def _calibration() -> dict | None:
    """The measured open-rate table, or ``None`` if it has never been built.

    Cached on (mtime, size) rather than an answer, so a re-run of the calibration
    is picked up without restarting the service and a missing file is not
    remembered as "no such thing".
    """
    global _CALIB_CACHE, _CALIB_STAMP
    try:
        stat = CALIBRATION.stat()
    except OSError:
        return None
    stamp = (int(stat.st_mtime), stat.st_size)
    with _CALIB_LOCK:
        if _CALIB_STAMP == stamp and _CALIB_CACHE is not None:
            return _CALIB_CACHE
    try:
        payload = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("by_seal_ratio"):
        return None
    with _CALIB_LOCK:
        _CALIB_CACHE, _CALIB_STAMP = payload, stamp
    return payload


def _band_of(ratio: float, edges: list[float]) -> str:
    """Which quintile a current ratio falls in, using the measured edges."""
    index = bisect.bisect_right([float(e) for e in edges], float(ratio))
    return _SEAL_BANDS[min(index, len(_SEAL_BANDS) - 1)]


def _seal_history(ratio: float | None) -> dict | None:
    """What this seal-strength bucket did historically — measured, with its own n.

    The number worth having is not the open rate alone. It is the pair: a strong
    seal rarely opens (so it cannot be bought) *and* pays the most when it does
    not; a weak seal opens often and pays little. Reporting only one half would
    make either "always queue" or "never queue" look obviously right.

    Returns ``None`` when there is no calibration on disk. Silently returning a
    default would put an invented prior in every ticket.
    """
    if ratio is None:
        return None
    payload = _calibration()
    if payload is None:
        return None
    edges = payload.get("seal_quantiles") or []
    if len(edges) != len(_SEAL_BANDS) - 1:
        return None
    band = _band_of(ratio, edges)
    row = next((r for r in payload["by_seal_ratio"] if r.get("bucket") == band), None)
    if row is None:
        return None
    window = payload.get("window") or {}
    out = {
        "bucket": band,
        "open_rate": row.get("open_rate"),
        "gap_if_opened": row.get("next_open_gap_mean"),
        "n": row.get("n"),
        "as_of": window.get("end"),
    }
    end = window.get("end")
    if end:
        try:
            age = (date.today() - datetime.strptime(str(end), "%Y%m%d").date()).days
            out["age_days"] = age
            if age > 30:
                out["note"] = (f"标定窗口截止 {end}（{age} 天前）。"
                               f"开板率随情绪周期变，隔一个月只能当量级看。")
        except ValueError:
            pass
    return out


def board_state(snap) -> str:
    """What the ceiling is doing right now, in five words or fewer.

    ``unknown`` is a real answer: a suspended name has no ceiling to test, and a
    snapshot without limits cannot say whether the price is *at* one.
    """
    if snap is None:
        return "unknown"
    if getattr(snap, "suspended", False):
        return "suspended"
    last = _dec(getattr(snap, "last", None))
    up = _dec(getattr(snap, "up_limit", None))
    down = _dec(getattr(snap, "down_limit", None))
    if last is None or last <= 0:
        return "unknown"
    if up is not None and up > 0 and last >= up - PRICE_EPS:
        return "sealed_up"
    if down is not None and down > 0 and last <= down + PRICE_EPS:
        return "sealed_down"
    if up is None or up <= 0:
        return "unknown"
    return "trading"


def _seal_block(market, symbol: str, day: str) -> dict:
    """封单额 and 封单/流通市值 for this symbol on this session, or why not.

    Both halves matter and neither is enough alone. 6.10亿 of sealed bids sounds
    overwhelming until the float is 54.6亿 (11.2%, genuinely strong); 1.15亿
    sounds similar until the float is 246亿 (0.47%, the weakest board on the
    list). Ranking by the absolute number therefore points at large caps, which
    is the opposite of what a short-horizon buyer is looking for.
    """
    frame = market.store.read("ths_limit_up", day, day)
    if frame is None or frame.empty:
        return {
            "basis": "unknown",
            "why": (f"{day} 的涨停池还没入库。封单是收盘后的数据，"
                    f"盘中问「封得牢不牢」本来就没有可引用的数字。"),
        }
    rows = frame[frame["ts_code"].astype(str) == symbol]
    if rows.empty:
        # In the pool table but not in it. Two very different causes.
        return {
            "basis": "absent",
            "why": (f"{symbol} 不在 {day} 的同花顺涨停池里："
                    f"要么没涨停，要么封板后又打开（炸板池才有它）。"
                    f"先查 board.state，别把这个缺失当成「没封住」。"),
        }

    row = rows.iloc[0]
    seal = _dec(row.get("seal_money"))
    pool = frame.copy()
    pool["seal_money"] = pd.to_numeric(pool["seal_money"], errors="coerce")
    pool = pool.dropna(subset=["seal_money"])
    pool = pool[pool["seal_money"] > 0]

    # 唯一一份 万元→元 的换算在 ``MarketRepository.circ_mv``：这里曾经也有
    # 一份，和 tape 里那份各自写着"只在这里换算"。
    circ = market.circ_mv(day)
    ratios = None
    if len(circ):
        denom = pool["ts_code"].astype(str).map(circ)
        usable = denom.where(denom > 0)
        if usable.notna().any():
            ratios = (pool["seal_money"] / usable).dropna()
            pool = pool.loc[ratios.index]
            pool["seal_ratio"] = ratios

    out = {
        "basis": "observed",
        "cny": None if seal is None else float(seal),
        "source": "ths_limit_up",
        "seal_time": str(row.get("limit_up_time") or "") or None,
        "streak": None if pd.isna(row.get("continue_cnt")) else int(row.get("continue_cnt")),
    }
    if ratios is None or not len(ratios):
        out["ratio"] = None
        out["ratio_basis"] = "seal_money_only"
        out["why"] = "缺 daily_basic.circ_mv，只报封单绝对额；不要拿它跨股票比强弱。"
        return out

    pool = pool.sort_values("seal_ratio", ascending=False).reset_index(drop=True)
    hit = pool.index[pool["ts_code"].astype(str) == symbol]
    rank = int(hit[0]) + 1 if len(hit) else None
    this = float(pool.loc[hit[0], "seal_ratio"]) if len(hit) else None
    history = _seal_history(this)
    if history:
        out["history"] = history
    out.update({
        "ratio": None if this is None else round(this, 4),
        "ratio_basis": "seal_money_over_circ_mv",
        "rank_in_pool": rank,
        "pool_n": len(pool),
        "pool_median_ratio": round(float(pool["seal_ratio"].median()), 4),
    })
    return out


def feasibility(market, symbol: str, *, day: str, side: str = "BUY",
                snapshot=None) -> dict:
    """Everything the desk knows about *whether* this order can fill, and nothing more.

    Read ``queue.required`` for the decision, ``seal`` for the strength and
    ``fill_ratio.value is None`` for the part nobody can measure. The block is
    deliberately cheap: one 49-row partition plus a cached 流通市值 series.
    """
    side = str(side).upper()
    snap = snapshot if snapshot is not None else market.snapshot(symbol, day)
    if snap is None:
        return {"ok": False, "symbol": symbol, "as_of": day,
                "error": f"{symbol} 在 {day} 没有可用的价格快照，成交可行性无法判断"}

    state = board_state(snap)
    last = _dec(snap.last)
    up = _dec(getattr(snap, "up_limit", None))
    opened = _dec(getattr(snap, "open", None))
    at_ceiling = state == ("sealed_up" if side == "BUY" else "sealed_down")
    queue_required = bool(at_ceiling)

    out = {
        "ok": True,
        "symbol": symbol,
        "as_of": day,
        "side": side,
        "board": {
            "state": state,
            "last": None if last is None else str(last),
            "up_limit": None if up is None else str(up),
            "down_limit": None if _dec(getattr(snap, "down_limit", None)) is None
            else str(_dec(snap.down_limit)),
            "opened_at_ceiling": bool(
                opened is not None and up is not None and up > 0
                and opened >= up - PRICE_EPS),
            "source": str(getattr(snap, "source", "") or ""),
        },
        "queue": {
            "required": queue_required,
            "why": QUEUE_WHY if queue_required else
                   "价格没在涨跌停上，市价单可以成交；成交价由撮合器给出，仍受滑点与成交量上限约束。",
        },
        "fill_ratio": {
            "value": None,
            "why": ("没有队列数据：封单额是 15:00 的数，用到 09:30 上就是前视；"
                    "逐笔委托不在已订阅的行情里。所以这里是 null，不是 0，也不是 1。"),
        },
    }

    if side == "BUY" and state in ("sealed_up", "trading", "unknown"):
        # Only a buy at a ceiling has a seal worth measuring. A sell into a
        # sealed ceiling has no queue at all - it clears immediately.
        out["seal"] = _seal_block(market, symbol, day)
    else:
        out["seal"] = {"basis": "not_applicable",
                       "why": "只有「在涨停价上买入」才有封单可谈"}

    if queue_required:
        out["next_step"] = NEXT_STEPS
    return out
