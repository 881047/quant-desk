"""The day's quotable numbers, computed once, so a report cannot invent them.

On 2026-10-29 a close report said "可买的 6 只全部封在涨停价". The stored session
says 23 names sat inside the same price cap, all of them sealed before 14:38.
Both statements are about the same day; only one is reproducible. The difference
was not arithmetic — it was that the report's number came from the names the
model happened to have priced, not from the session.

So this module produces one dict of facts with stable keys, plus a JSON copy on
disk. A report is expected to cite keys from it. `scripts/verify_report.py`
then checks that a close report exists only for a `READY` day and that the
numbers it states appear in the facts.

The rule the keys encode: **null means "not published", 0 means "measured zero"**.
Conflating them is how "no data" became "nothing to buy".
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from service import vocabulary as vocab
from service.adapters.jsonio import write_json_atomic
from service.kernel.clock import SHANGHAI

from .readiness import check as check_readiness
from .screen import affordable_price

# A close report quotes the session; the session is the stored bar. Anything
# computed from a snapshot is a different object and does not belong here.


def _num(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pool(desk, name: str, day: str) -> pd.DataFrame | None:
    try:
        frame = desk.data.read(name, day, day)
    except (KeyError, FileNotFoundError, ValueError):
        return None
    if frame is None or frame.empty:
        return None
    return frame


def _breadth(desk, day: str) -> dict:
    daily = desk.market.daily(day)
    if daily is None or daily.empty:
        return {"rows": None, "note": f"{day} 无日线，涨跌家数不可计算"}
    close = pd.to_numeric(daily["close"], errors="coerce")
    pre = pd.to_numeric(daily["pre_close"], errors="coerce")
    ret = (close / pre - 1.0).replace([float("inf"), float("-inf")], pd.NA).dropna()
    return {
        "rows": len(daily),
        "advancers": int((ret > 0).sum()),
        "decliners": int((ret < 0).sum()),
        "flat": int((ret == 0).sum()),
        "equal_weight_return": round(float(ret.mean()), 6) if len(ret) else None,
        "median_return": round(float(ret.median()), 6) if len(ret) else None,
        "units": vocab.note("returns"),
    }


def _derived_limit_up(desk, day: str) -> pd.DataFrame | None:
    """当日的涨停股，取派生池（``close == up_limit``）。

    与 ``_pools`` 同一个来源：那里也走 ``tape.breadth``，读的就是这张表。
    此前 ``_affordable`` 读东财的 ``limit_up_pool``，于是同一个 payload 里
    ``pools.limit_up`` 的分母是派生池、``affordable.in_cap`` 的分母是东财：
    2026-11-02 是 132 与 125，63 只"可负担"写在 132 旁边像 48%，实际是 50.4%。

    **``low`` 不能用来判断"开过板"。** 它包含封板之前的成交，几乎总是低于
    涨停价：000037.SZ 在 2026-11-02 的 low=8.74 < 涨停价 8.78，但它
    09:30:00 就封死、全天没开过板——那个 8.74 是开盘一瞬的价格。开板次数只有
    东财的 ``open_times`` 能给，见 ``_board_opens``。
    """
    frame = _pool(desk, "limit_pool_t0", day)
    if frame is None or "pool" not in frame.columns:
        return None
    up = frame[frame["pool"].astype(str) == "limit_up"]
    return None if up.empty else up


def _board_opens(desk, day: str, cap: float) -> dict:
    """开板次数：只有东财有，且只回溯约 20 个交易日。

    这是"能不能买到"唯一可靠的来源。日线看不出来——见 ``_derived_limit_up``
    的说明——所以派生池再全，这一维也只能挂靠东财，并且必须写明覆盖了多少只。
    """
    vendor = _pool(desk, "limit_up_pool", day)
    if vendor is None:
        return {"source": "eastmoney:limit_up_pool", "available": False,
                "note": "东财涨停池未发布；开板次数只有这家有，日线看不出来"}
    if "close" not in vendor.columns or "open_times" not in vendor.columns:
        return {"source": "eastmoney:limit_up_pool", "available": False,
                "note": "东财池缺 close 或 open_times 列"}
    vclose = pd.to_numeric(vendor["close"], errors="coerce")
    within = vendor[vclose <= cap]
    opens = pd.to_numeric(within["open_times"], errors="coerce")
    latest = None
    if "last_seal_time" in vendor.columns:
        values = vendor["last_seal_time"].astype(str)
        values = values[values.str.strip().ne("") & values.ne("nan")]
        if len(values):
            latest = str(values.max())
    return {
        "source": "eastmoney:limit_up_pool",
        "available": True,
        "pool_size": len(vendor),
        "in_cap": len(within),
        "opened": int((opens > 0).sum()),
        "never_opened": int((opens == 0).sum()),
        "latest_seal": latest,
        "note": ("只有东财有开板次数，且只能回溯约 20 个交易日。"
                 "opened + never_opened = 东财池里的 in_cap，不是派生池的 in_cap。"),
    }


def _affordable(desk, day: str, equity: float) -> dict:
    """What the price cap left buyable *at the close*, counted from the session.

    Three different questions hide in "can I buy it", and mixing them produced
    the bad number:

    * ``in_cap`` — how many limit-up names cost no more than one lot at the
      target weight. Counted on the derived pool, the same source ``pools`` uses;
    * ``one_word`` — how many of those were sealed at the open and never left the
      cap, i.e. had no fillable moment at all. This one *is* readable from daily
      bars: open == low == limit;
    * ``buyable_at_close`` — how many could be bought at 14:57. That is zero by
      construction for a limit-up name: the pool is defined by closing at the
      cap, so a market order queues behind the seal.

    ``board_opens`` carries the fourth question — did it open after sealing — and
    that one comes from a different vendor with a much shorter history.
    """
    cap = affordable_price(float(equity))
    frame = _derived_limit_up(desk, day)
    if frame is None:
        return {
            "cap": round(cap, 2),
            "in_cap": None,
            "one_word": None,
            "board_opens": None,
            "buyable_at_close": None,
            "note": f"{day} 的涨停池未发布，任何'可买几只'的数字此时都不可复现",
        }

    # ``frame.get("close")`` is None when the column is absent, and
    # ``pd.to_numeric(None)`` returns None — assigning that to a column then
    # makes ``<= cap`` raise TypeError, so one missing vendor column took down
    # the whole facts payload instead of reporting "cannot price this pool".
    if "close" not in frame.columns:
        return {
            "cap": round(cap, 2),
            "in_cap": None,
            "one_word": None,
            "board_opens": None,
            "buyable_at_close": None,
            "note": f"{day} 的涨停池没有 close 列，'可买几只'无法复现",
        }
    close = pd.to_numeric(frame["close"], errors="coerce")
    within = frame[close <= cap]

    # 空 within 是「测到了 0」，不是「没数据」——池子已经存在，只是没有一只
    # 落在价格上限内。所以初值是 0，只有判定所需的列缺了才退回 None。
    one_word: int | None = 0
    one_word_basis: str | None = None
    fill_kind: dict | None = None
    low = pd.to_numeric(within["low"], errors="coerce") if "low" in within.columns else None
    limit = (pd.to_numeric(within["limit_price"], errors="coerce")
             if "limit_price" in within.columns else None)
    open_ = pd.to_numeric(within["open"], errors="coerce") if "open" in within.columns else None
    if low is not None and limit is not None and open_ is not None:
        # 一字板 = 开盘即涨停且全天只有一个价。这是日线唯一能严格说清的一维：
        # 涨停股的 close 必然等于 limit，所以只需再确认 open 与 low。
        at_open = open_ >= limit * 0.999
        never_left = low >= limit * 0.999
        one_word = int((at_open & never_left).sum())
        one_word_basis = "open >= limit_price 且 low >= limit_price：开盘即封、全天只有一个价"
        # 三分法。第三种（盘中封板）占多数，而且它**封板之前就能买**——所以
        # "打板必须排队"对这批票根本不成立。只报一个 one_word，读者会把
        # "其余都是开过板"当结论，而实际上一大半是盘中慢慢封上去的。
        fill_kind = {
            "one_word": one_word,
            "opened_after_seal": int((at_open & ~never_left).sum()),
            "sealed_intraday": int((~at_open).sum()),
        }
    else:
        one_word = None
        one_word_basis = "派生池缺 open/low/limit_price，一字板无法判定"

    return {
        "cap": round(cap, 2),
        "in_cap": len(within),
        "one_word": one_word,
        "one_word_basis": one_word_basis,
        "fill_kind": fill_kind,
        "board_opens": _board_opens(desk, day, cap),
        # Not a measurement: a limit-up pool is *defined* by closing at the cap,
        # so a market order at 15:00 queues behind the seal and fills nothing.
        "buyable_at_close": 0,
        "source": "derived:limit_pool_t0",
        "basis": ("in_cap = 收盘价 <= cap 的涨停股（派生池，与 pools.limit_up 同源）；"
                  "one_word = 开盘即涨停且全天未离开涨停价（日线可严格判定）；"
                  "board_opens = 封板后有没有开过板（只有东财的 open_times 能给，"
                  "回溯约 20 个交易日，覆盖数见 board_opens.pool_size）；"
                  "buyable_at_close 对涨停股恒为 0——池子按收盘封板定义，市价单要排队。"
                  "这几个数不能互相替代。"),
    }


def _pools(desk, day: str) -> dict:
    """三个池子的家数，按两个来源各数一遍。

    家数以派生池为准（``close == up_limit``，逐只可核对）；东财的行数留在
    ``vendor_eastmoney``。这里原来只报东财，附一句「同花顺与东财对涨停的认定不同」——
    那句话听起来像两种口径都对，但 2026-09-01~10-30 逐只核对下来，两家都会漏：
    东财平均每天少 2.1 只、同花顺少 1.6 只。差 10 只不一定是谁的口径不同，
    很可能是一家没抓全。收盘报告引用的数不能建在这种差额上。
    """
    from .tape import breadth as pool_breadth

    vendor: dict = {}
    for key, name in (("limit_up", "limit_up_pool"), ("broken", "broken_board_pool"),
                      ("limit_down", "limit_down_pool")):
        frame = _pool(desk, name, day)
        vendor[key] = None if frame is None else len(frame)

    derived = pool_breadth(desk.data, day)
    out: dict = {}
    if derived:
        out["limit_up"] = derived["limit_up"]
        out["limit_up_ex_bj"] = derived["limit_up_ex_bj"]
        out["broken"] = derived["broken"]
        out["limit_down"] = derived["limit_down"]
        out["source"] = derived["source"]
        out["vendor_eastmoney"] = vendor
    else:
        out.update(vendor)
        out["source"] = "eastmoney"
    out["note"] = ("家数来自派生池（close == up_limit），limit_up_ex_bj 剔掉 30% 涨跌幅的"
                   "北交所；vendor_eastmoney 是同一天东财自己的行数。两家都会漏名字，"
                   "跨日比较必须同源，引用时写明用哪一版。"
                   "同花顺那一版在 quant_knowledge(topic=limit_structure) 的 "
                   "counts.vendor_ths——三个数同一天会不一样，因为它们来自三家、"
                   "抓取深浅也不同，不是谁算错了。")
    return out


def build_facts(desk, day: str | None = None) -> dict:
    """Every number a close report is allowed to quote, keyed and dated."""
    day = day or desk.market.latest_bar_date()
    ready = check_readiness(desk.market, day=day)
    summary = desk.book.summary()
    payload = {
        "day": day,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        # 账户半边（equity / cash / positions，以及 `affordable` 用的净值）
        # 读的是**现在**的账本，不是 `day` 那一刻的。日线还在那一天时两者相同；
        # 补写过去的日子时不同——把今天余额当成那天的事实在引用，是
        # `facts_for(compute=False)` 存在的原因。这里把它写成字段，读者不用
        # 靠文档猜。
        "account_as_of": desk.market.latest_bar_date(),
        "readiness": ready.as_dict(),
        "breadth": _breadth(desk, day) if day else {"rows": None},
        "pools": _pools(desk, day) if day else {},
        "affordable": (_affordable(desk, day, summary["equity"])
                       if day else {"in_cap": None}),
        "account": {
            "equity": summary["equity"],
            "cash": summary["cash_available"],
            "market_value": summary["market_value"],
            "positions": len(summary["positions"]),
            "day_pnl_pct": summary["daily_pnl_pct"],
            "drawdown": summary["drawdown"],
        },
        "tickets_today": [
            {"id": t.get("ticket_id"), "symbol": t.get("symbol"),
             "side": t.get("side"), "status": t.get("status")}
            for t in desk.paper.list_tickets(limit=20)
            if str(t.get("trade_date") or "") == str(day)
        ],
    }
    # `account_as_of` 是字段，但读者（尤其是模型）不会自己去比两个日期。补写过去
    # 某天的报告时，`account.equity` 是**现在**的余额：2026-11-10 的 facts 里
    # 躺着 11-13 的账户，不写明就会被当成那天收盘的账。
    if day and str(payload.get("account_as_of")) != str(day):
        payload["account_note"] = (
            f"账户半边是 {payload['account_as_of']} 的现状，不是 {day} 的；"
            f"{day} 可引用的只有 market 半边（breadth / pools / affordable）。"
            "要把那天的账写进报告，得先找回当天的账本快照。")
    return payload


def facts_for(desk, day: str | None = None, root: Path | None = None,
              compute: bool = True) -> dict | None:
    """The day's facts: the snapshot on disk if there is one, else compute and store.

    The file is authoritative when present — it is what a close report was
    checked against, and recomputing would let a later data refresh rewrite the
    numbers a report was verified against. A missing file is computed once
    (0.4 s) and written **only if the session it describes is complete**;
    before that, every journal entry came back "无法核对". A check that is
    skipped on the days it matters is not a check — but a noon snapshot frozen
    under the day's name is worse than no snapshot, because the close verifier
    then reads it as the day's fact.

    ``compute=False`` is for the backfill, and it is not a convenience flag. The
    account half of `facts` (`equity`, `cash`, `positions`) is read from the book
    as it stands **now**; computed for a past day it would carry today's balance
    under yesterday's date, and `write_facts` would persist that file — poisoning
    the numbers a close report for that day gets verified against. So a past day
    with no snapshot answers "not on disk, do not invent one".

    Two callers, one implementation. It used to live only in the API route, so
    the backfill script would have needed its own copy — and this project has
    already paid twice for two implementations of one fact (P68 `board_of`,
    P79 正文路径).
    """
    if not day:
        return None
    root = root or Path(__file__).resolve().parents[2]
    path = root / "reports" / "facts" / f"{day}.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if not compute:
        return None
    try:
        payload = build_facts(desk, day)
    except Exception:  # noqa: BLE001 - a note must still be writable without facts
        return None
    # 盘中调用（readiness=SESSION_OPEN）算得出数，但那不是收盘后的事实：
    # `in_cap` 还没有涨停池、`buyable_at_close` 还没有收盘价。把这份快照落盘，
    # 它就会被 `verify_report` 当成"当天权威"去核收盘报告——2026-11-12 的
    # 17:56 就是这样拿一份午间快照核出了 SESSION_OPEN 与 in_cap=None 两条
    # FAIL/WARN，而 18:38 写下的收盘事实已经 READY、in_cap=26。盘中照样返回
    # 刚算出来的数，只是不冻结它；收盘后的日任务会写那一份。
    if bool((payload.get("readiness") or {}).get("ok")):
        # 同一份 payload 落盘，不再算第二遍：重算一次可能让账户数字在两次
        # 计算之间变化，而这里只需要一个一致的事实。
        with contextlib.suppress(Exception):
            write_facts(desk, day, root=root, payload=payload)
    return payload


def market_facts(desk, day: str | None = None) -> dict | None:
    """Only the blocks that are true **as of that day**, for a day already past.

    `facts` has two halves. Breadth, pools and readiness come from stored bars
    and pool rows — recomputing them for 20261023 today gives the same numbers
    the day itself would have given. `account`, `affordable` and `tickets_today`
    come from the book as it stands **now**: `equity` is today's balance, so a
    facts file computed for a past day would carry today's money under
    yesterday's date, and `write_facts` would persist it.

    So a past day gets this filtered view, and it is only good enough for the
    label check (`claims`), which reads counts like 涨停 49 家. The number check
    still has nothing to compare against — an account total from that day was
    never recorded — and says so rather than passing.

    One implementation, not two: the four blocks are read from `build_facts`
    so this cannot drift from the artifact the daily job writes.
    """
    if not day:
        return None
    try:
        full = build_facts(desk, day)
    except Exception:  # noqa: BLE001 - no bars for that day means no verdict
        return None
    keep = ("day", "readiness", "breadth", "pools")
    return {key: full.get(key) for key in keep}


def write_facts(desk, day: str | None = None, root: Path | None = None, *,
                payload: dict | None = None) -> Path:
    """Persist the facts next to the reports, so a later reader can diff them.

    ``payload`` lets a caller that has just computed the facts write **that**
    snapshot instead of computing a second one. The two computations can differ
    by the account balance if a fill lands between them, and the file a close
    report is verified against must be the same object the caller read.
    """
    root = root or Path(__file__).resolve().parents[2]
    payload = payload if payload is not None else build_facts(desk, day)
    latest = None
    try:
        latest = desk.market.latest_bar_date()
    except Exception:  # noqa: BLE001 - a desk double in a test may not carry a market
        latest = None
    if latest and str(payload.get("day")) != str(latest):
        # 倒填过去的日子只能填市场半边。账户半边是"现在"的账本，
        # 写下去就把今天的余额标成了那天的权威事实——2026-11-05 的文件
        # 是 11-06 14:05 写的，只是那几天恰好空仓 5,000 才没有露出差额。
        raise ValueError(
            f"cannot write facts for {payload.get('day')}: the account half is read "
            f"as it stands now and the latest bar is {latest}; a past day has no "
            f"account snapshot to recover (use market_facts() for its market half)")
    out = root / "reports" / "facts" / f"{payload['day']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, payload)
    return out
