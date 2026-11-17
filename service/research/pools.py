"""Pool statistics that both the trading layer and the facts panel read.

``promotion_series`` used to live in ``service/trading/tape.py`` and
``information.py`` imported it *inside a function* to dodge a cycle —
``trading.arena`` already imports ``research.stats`` at module level, so
``research`` importing ``trading`` at module level would close the loop.

Dodging it that way makes the dependency invisible: hoisting the import to the
top of the file during any refactor turns a working service into an ImportError
at startup. This function reads stored pools and the trading calendar and
nothing else, so it belongs below both callers. ``tape`` imports it from here
(trading → research is the direction that already exists); ``information``
imports it as a sibling.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

import pandas as pd

from service.adapters.store_read import open_sessions, read_or_empty

# 档位名与上游一致，因为下游（以及人）按这几个字读。
PROMOTION_LEVELS = {1: "two", 2: "three", 3: "four", 4: "five_plus"}


def promotion_series(store, days: list[str]) -> dict[str, list[dict]]:
    """晋级率：昨日 k 板 → 今日 k+1 板，全部由派生池自己算。

    上游的 ``ths_limit_stat`` 也有这个数，但它只留 5 个交易日——用它在
    「这一维有多少历史」上打满分是假的。分母是**昨日 k 板的只数**、分子是
    昨天在 k 板且今天在 k+1 板的只数。

    两版在 20261030 差 0.3 个百分点，在 20261026 差 **22 个百分点**。那天查到底了：
    不是口径分歧，是**同花顺当天漏报**——它只给了 45 只涨停，派生池 59 只，东财 55 只，
    而派生池多出来的那 5 只（``000700`` ``002931`` ``603901`` ``002535`` ``301502``）
    当天全部收在涨停价上（含一只 20% 板的 ``301502``），且前一天都在同花顺自己的榜上。
    漏掉它们之后，分子少了 5、分母还是它前一日那个数，战绩自然算低。

    所以这里不用上游的分子分母，也不需要「取平均」这种和稀泥：一个漏报的池子算出来的
    比率，正确的做法是不用。两版的分歧由 ``information`` 的对账摆出来，读者自己看。

    ``limit_pool_t0`` 从 2010 年起有 ``boards``，所以这个序列的深度是 250 个交易日，
    而不是 5 个。派生池是这张表自己的口径，晋级率、连板高度、涨停家数从此同源。
    """
    if not days:
        return {}
    start = (datetime.strptime(min(days), "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    frame = read_or_empty(store, "limit_pool_t0", start, max(days),
                          columns=["trade_date", "code", "pool", "boards"])
    if frame is None or frame.empty:
        return {}
    frame = frame[frame["pool"].astype(str) == "limit_up"].copy()
    if frame.empty:
        return {}
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["boards"] = pd.to_numeric(frame["boards"], errors="coerce").fillna(0).astype(int)

    by_day = {day: chunk.set_index("code")["boards"]
              for day, chunk in frame.groupby("trade_date")}
    out: dict[str, list[dict]] = {}
    # 配对必须按**交易日历**，不能按"池子里出现过的日期"。一个全天没有涨停的交易日
    # 在派生池里没有行，按出现日期配对会把那一天跳过，于是"昨日 k 板"指向了前天——
    # 分母、分子、连板增量同时错位，而结果看起来完全正常。4084 个交易日里恰好没有
    # 这样的日子，所以这个 bug 一直没被触发，但不该靠这个。
    sessions = open_sessions(store, start, max(days)) or sorted(by_day)
    empty = pd.Series(dtype="int64")
    # 区间里每一对相邻交易日都给一行，不只是请求里点名的那些天：调用方常用
    # ``promotion_series(store, [start, day])`` 问一段窗口，然后按天取。
    for previous, current in pairwise(sessions):
        before = by_day.get(previous, empty)
        today = by_day.get(current, empty)
        # 重置检测：同一只票两天都封在板上，它的 boards 必须**恰好 +1**。
        #
        # 先前用的是「今天最高板 ≤1 而昨天 ≥2」——那个形状在 4,083 个交易日对里有
        # 73 天出现，而修好数据后它们**全是真实的全场首板日**，不是重置。改成看重叠票
        # 之后，同一个窗口命中 0 天；而在没预热的旧分区上它每天都会命中（那些票
        # 今天会被算成 1 板，昨天是 k 板）。
        #
        # 局限：重叠为空时看不出来（今天全是一批新票）。那时不报重置——宁可漏报，
        # 不要把一个正常的交易日标成算不出来。
        both = before.index.intersection(today.index)
        reset = bool(len(both)) and bool((today[both] <= before[both]).any())
        rows = []
        for k, name in PROMOTION_LEVELS.items():
            base_codes = before[before == k].index
            today_codes = today[today == k + 1].index
            base = len(base_codes)
            hit = len(base_codes.intersection(today_codes))
            row = {
                "level": name,
                "board": k + 1,
                "count": len(today_codes),
                "promoted": hit,
                "base": base,
                "promotion_rate": None if (base == 0 or reset) else round(hit / base, 4),
            }
            if reset:
                row["reset"] = True
                row["note"] = ("这一天派生池的连板全部重算成 1 板（分区边界没有预热），"
                               "所以晋级率不可计算；不是「全部断板」。")
            rows.append(row)
        out[current] = rows
    return out
