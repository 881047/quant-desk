"""Two vendors publish the same three pools and never agree on the numbers.

同花顺 and 东财 differ on whether ST names, 北交所 and one-line boards count as a
limit-up. On 2026-10-26 they differed by ten names (45 vs 55).

The original note here said "both are published numbers, so neither is wrong".
That was wrong, and the 2026-09-01~10-30 audit showed it: against the exchange
ceilings (``close == stk_limit.up_limit``), 同花顺 matched per-name on 16 of 38
sessions and 东财 on 4 of 19 — averaging 1.6 and 2.1 missing names per day. A gap
between two publishers is therefore **not** automatically a definition gap; it is
usually one of them having an incomplete list. So this module now reports the
derived pool as a third column: it is the only one whose membership can be
checked stock by stock, and its 60 missing names in that window were 35 沪深 and
25 北交所 — i.e. real omissions, not a different rule about 北交所.

A reader who sees only one number still cannot tell a definition gap from a
change in the tape; a reader who sees three can check.
"""

from __future__ import annotations

EASTMONEY = "eastmoney"
THS = "ths"

_DATASETS = {
    # (limit_up, broken, limit_down)
    EASTMONEY: ("limit_up_pool", "broken_board_pool", "limit_down_pool"),
    THS: ("ths_limit_up", "ths_limit_break", "ths_limit_down"),
}
_LABELS = {EASTMONEY: "东财", THS: "同花顺"}
_KEYS = ("limit_up", "broken", "limit_down")


def _count(desk, name: str, day: str) -> int | None:
    """Rows in one pool for one day, or ``None`` when that day was never fetched.

    The three pools are partitioned by session, so the manifest answers "was this
    day ingested" exactly. Reading an absent partition and calling the emptiness
    zero is how "the 跌停池 was not fetched for that date" became "there were no
    limit-downs" — and, in a comparison, a fabricated gap between two vendors.
    """
    registry = getattr(desk.data, "registry", None)
    try:
        if registry is not None and registry.get_manifest(name, day) is None:
            return None
        frame = desk.data.read(name, day, day)
    except Exception:  # noqa: BLE001 - a cross-check must never fail the main read
        return None
    if frame is None or getattr(frame, "empty", True):
        return 0
    return len(frame)


def _derived(desk, day: str) -> dict | None:
    """The derived pool's counts — the only column here that can be audited.

    Returns ``None`` when the day was never derived, never zeros: a derived table
    that was not rebuilt for this session must not look like "no limit-ups".
    """
    try:
        from service.trading.tape import breadth

        return breadth(desk.data, day)
    except Exception:  # noqa: BLE001 - a cross-check must never fail the main read
        return None


def compare(desk, day: str, mine: dict, *, source: str, phase: str = "settled") -> dict:
    """Read the other vendor for `day` and report the gap against ``mine``.

    ``mine`` carries the counts the caller already computed, keyed
    limit_up / broken / limit_down. ``delta`` is always eastmoney minus 同花顺 so
    that the sign means the same thing whichever side asked.

    During the opening window the comparison is not made: the other vendor's
    pools for the same day are not ingested yet, and comparing today's partial
    read against its zero rows would invent a gap that is really a clock.
    """
    peer = THS if source == EASTMONEY else EASTMONEY
    if source not in _DATASETS:
        return {"ok": False, "note": f"unknown source {source}"}

    if phase == "opening_window":
        return {"ok": False, "skipped": "opening_window",
                "note": ("开盘三十分钟内不比对：另一家的当日池子还没入库，"
                         "此刻比出来的差是抓取时点差，不是口径差。")}
    if phase == "stale_close":
        return {"ok": False, "skipped": "stale_close",
                "note": ("已收盘但分区还是盘中快照，另一方是收盘终值；"
                         "此刻的差混了时点差，等 17:30 日更后再比。")}

    names = _DATASETS[peer]
    theirs = dict(zip(_KEYS, (_count(desk, names[0], day),
                              _count(desk, names[1], day),
                              _count(desk, names[2], day))))
    if all(v is None for v in theirs.values()):
        return {"ok": False, "skipped": "unreadable",
                "note": f"{day} 的{_LABELS[peer]}三池读取失败，无法对账。"}
    if not any(theirs.values()):
        return {"ok": False, "skipped": "not_ingested",
                "note": (f"{day} 的{_LABELS[peer]}三池尚未入库（需 fetch_ths_special.py），"
                         f"此刻只有{_LABELS[source]}一个口径；不要把它当唯一真相。")}
    missing = [key for key in _KEYS if theirs.get(key) is None]
    if missing:
        # Some pools exist for this day, some were never fetched. Reporting a
        # delta for the absent ones would read as "the vendors disagree by N"
        # when in fact one of them was never asked.
        return {"ok": False, "skipped": "partial",
                "missing": missing,
                "note": (f"{day} 的{_LABELS[peer]}缺 {'/'.join(missing)} 池，"
                         f"只在 {'/'.join(k for k in _KEYS if k not in missing)} 上可比。")}

    east = theirs if peer == EASTMONEY else mine
    ths = theirs if peer == THS else mine
    delta = {key: (int(east[key]) - int(ths[key])
                   if isinstance(east.get(key), (int, float))
                   and isinstance(ths.get(key), (int, float)) else None)
             for key in _KEYS}
    out = {
        "ok": True,
        "primary": source,
        "eastmoney": {key: east.get(key) for key in _KEYS},
        "ths": {key: ths.get(key) for key in _KEYS},
        "delta_eastmoney_minus_ths": delta,
        "note": ("两个来源的涨停家数不相等**不等于**两套口径：2026-09-01~10-30 对交易所"
                 "限价逐只核对，同花顺 38 天里只有 16 天完全正确、东财 19 天里只有 4 天。"
                 "差得大时先看 derived（close == up_limit，可逐只复核），不要默认"
                 "两边都对、随便挑一个引用。比较两天时必须用同一来源。"),
    }
    derived = _derived(desk, day)
    if derived:
        # 派生池是唯一可逐只复核的一列。它给出时，另外两家的差就有了参照：
        # 谁离它近，谁的那一天就更可信。
        out["derived"] = {"limit_up": derived["limit_up"],
                          "limit_up_ex_bj": derived["limit_up_ex_bj"],
                          "broken": derived["broken"],
                          "limit_down": derived["limit_down"],
                          "source": derived["source"]}
        out["missing_vs_derived"] = {
            "eastmoney": (None if not isinstance(east.get("limit_up"), (int, float))
                          else derived["limit_up"] - int(east["limit_up"])),
            "ths": (None if not isinstance(ths.get("limit_up"), (int, float))
                    else derived["limit_up"] - int(ths["limit_up"])),
        }
    else:
        out["derived"] = None
        out["note"] += " 这一天没有派生池分区，谁少了几只无法判定。"
    return out
