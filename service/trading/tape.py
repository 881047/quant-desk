"""The tape: limit-up structure and the dragon-tiger board.

Two questions, one module:

* **How strong was the market's speculative tape?** Seal rate, break rate, how
  many boards deep the leaders went, and how often a board promoted to the next
  one. A short-horizon trader reads this before anything else, because it sets
  whether chasing strength is being rewarded or punished.
* **Who actually bought?** The dragon-tiger board names the seats — institutions
  or hot money — behind a move, with amounts.

Everything here is arithmetic over stored rows. ``summarize_limit`` is pure so it
can be tested without a store and replayed for any past day.

PIT note that matters more than it looks: the dragon-tiger board is published
after the close of the day it describes. A session that runs at 10:00 can only see
the previous session's board, so ``lhb`` returns ``data_date`` explicitly instead
of quietly serving stale rows as if they were today's.
"""

from __future__ import annotations

import hashlib
import math
import threading
from datetime import datetime, timedelta

import pandas as pd

from service import settings
from service.adapters.store_read import open_sessions, read_or_empty
from service.kernel.clock import SHANGHAI
from service.research.pools import promotion_series

from .knowledge import _json

# The three pools behind a percentile are one partition per session, so "where
# does today sit in its own history" opened 1,684 parquet files on every call —
# 2.4 seconds, on the endpoint the scheduler hits first. The rows do not change
# between ingestions and a manifest is content-addressed, so the set of
# (partition, snapshot_id, status) triples *is* the version of this answer.
# Keying on it costs one registry query (about 14 ms) instead of 1,684 reads.
_HISTORY_DATASETS = ("ths_limit_up", "ths_limit_break", "ths_limit_stat")
_POOL_CACHE: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
_POOL_CACHE_LIMIT = settings.integer("cache.pool_limit")
_POOL_LOCK = threading.Lock()


def _num(value):
    try:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _clean_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep real YYYYMMDD session labels and finite values used by history.

    ``astype(str)`` turns a missing date into the plausible-looking string
    ``"nan"``; it then becomes a session in the percentile baseline. Parsing and
    dropping invalid labels makes that cleaning explicit instead of accidental.
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame() if frame is None else frame.iloc[0:0].copy()
    out = frame.copy()
    parsed = pd.to_datetime(out["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    out["trade_date"] = parsed.dt.strftime("%Y%m%d")
    return out.dropna(subset=["trade_date"])


def _history_digest(frames: dict[str, pd.DataFrame]) -> dict:
    """A content receipt for the rows behind a percentile, not just its end date."""
    digest = hashlib.sha256()
    datasets = {}
    dates = []
    for name, frame in frames.items():
        columns = ["trade_date"] + (["seal_rate"] if name == "ths_limit_stat" else [])
        rows = []
        for values in frame[columns].itertuples(index=False, name=None):
            day = str(values[0])
            dates.append(day)
            rows.append((day,) + tuple("" if pd.isna(v) else str(v) for v in values[1:]))
        rows.sort()
        datasets[name] = len(rows)
        digest.update(name.encode())
        for row in rows:
            digest.update(("\0" + "\x1f".join(row)).encode())
    return {
        "rows": sum(datasets.values()),
        "datasets": datasets,
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "sha256": digest.hexdigest(),
    }


def summarize_limit(stat, up, down, brk, ladder, unknown: frozenset[str] = frozenset(),
                    *, strength: pd.Series | None = None,
                    strength_basis: str = "seal_money",
                    promotion: list[dict] | None = None,
                    breadth: dict | None = None) -> dict:
    """Five tables in, one readable picture of the tape out.

    ``unknown`` names the pools that were never fetched for this date. Those
    counts become ``None`` rather than ``0``: "no limit-downs that day" and "we
    do not have that day's limit-down pool" are different statements, and only
    one of them is safe to act on.

    ``strength`` changes what "the strongest board" means, and that is not a
    cosmetic choice. Ranking by absolute 封单额 answers "which board has the most
    money sitting on it", which is really a question about market cap: across the
    198 sessions of 2026 the absolute top-5 and the 封单/流通市值 top-5 agreed on
    34% of names and never once matched exactly. A 1.15亿 seal on a 246亿 float is
    0.47% and is one of the *weakest* boards of the day; a 6.10亿 seal on 54.6亿
    is 11.2% and is the strongest. ``seal_rank_basis`` says which ranking produced
    the list, so a reader is never left guessing.
    """
    counts = {"limit_up": len(up) if up is not None else 0,
              "limit_down": len(down) if down is not None else 0,
              "broken": len(brk) if brk is not None else 0}
    for key in unknown:
        if key in counts:
            counts[key] = None
    counts_source = "vendor:ths"
    if breadth:
        # 家数和连板高度改用派生池。同花顺不是口径不同，是名单不全：38 天里只有
        # 16 天与交易所限价逐只一致，58% 的日子偏低，平均少 1.6 只，最差一天少 16 只；
        # 2026-09-02 还把 5 板报成 4 板，而 ``max_boards >= 4`` 是情绪闸门的输入。
        # 厂商值不丢，挪进 ``counts.vendor_ths``；名字必须带厂商，否则和
        # `quant_facts.vendor_eastmoney` 撞在一起——同一天同一件事，
        # 东财跌停 82、同花顺 86，两个都叫 "vendor" 的字段会让读者以为有一个是错的。
        counts["vendor_ths"] = {k: counts.get(k) for k in ("limit_up", "broken", "limit_down")}
        counts["limit_up"] = breadth["limit_up"]
        counts["limit_up_ex_bj"] = breadth["limit_up_ex_bj"]
        counts["broken"] = breadth["broken"]
        counts["limit_down"] = breadth["limit_down"]
        counts_source = breadth["source"]
    stat_row = stat.iloc[0] if stat is not None and len(stat) else None
    touched = _num(stat_row.get("touched_limit_up_stock_count")) if stat_row is not None else None
    if breadth and counts["limit_up"] is not None and counts["broken"] is not None:
        # "touched the ceiling" is up + broken by definition, so it has to come from
        # the same pool as those two — a vendor touched count against a derived
        # numerator is the mixed-basis bug this whole module keeps paying for.
        touched = counts["limit_up"] + counts["broken"]
    counts["touched"] = None if touched is None else int(touched)
    if counts["touched"] and counts["broken"] is not None:
        # 4 位，和 `sentiment.break_rate` 一致。同一个指标两个精度（0.387 / 0.3871）
        # 摆在两份报告里，读者会以为是两个数。
        counts["break_rate"] = round(counts["broken"] / counts["touched"], 4)
    counts["source"] = counts_source

    vendor: list[dict] = []
    if stat is not None and len(stat):
        for _, row in stat.iterrows():
            vendor.append({
                "level": row.get("level"),
                "count": None if pd.isna(row.get("stock_count"))
                else int(row.get("stock_count")),
                "promotion_rate": _json(row.get("promotion_rate"), 3),
            })

    # 晋级率优先用派生池算出来的那一版：它和连板高度、涨停家数同源，而且有 250 天
    # 历史；上游那张表只有 5 天。两版都有的日子不取平均，只在差得明显时把分歧摆出来。
    promotion_rows = promotion or [
        {k: r.get(k) for k in ("level", "count", "promotion_rate")} for r in vendor
    ]
    promotion_source = "derived:limit_pool_t0" if promotion else "ths_limit_stat"
    if promotion and all(r.get("reset") for r in promotion):
        # 全是 None 的"晋级率"不是 0，是算不出来。换个 source 名字，读者才不会把它
        # 当成「今天全部断板」。
        promotion_source = "unavailable:derived-reset"
    promotion_spread = None
    if promotion and vendor:
        mine = {r["level"]: r["promotion_rate"] for r in promotion
                if r.get("promotion_rate") is not None}
        theirs = {r["level"]: r["promotion_rate"] for r in vendor
                  if r.get("promotion_rate") is not None}
        gaps = [abs(mine[k] - theirs[k]) for k in mine.keys() & theirs.keys()]
        if gaps and max(gaps) > settings.num("tape.promotion_spread_warn"):
            promotion_spread = {
                "max_abs_diff": round(max(gaps), 4),
                "vendor": vendor,
                "note": ("同日对不上先查``涨停家数``那一条：20261026 的 22 个百分点是因为"
                         "同花顺漏报 16 只涨停（59 vs 45），分子少了 5、分母没变。"
                         "派生池多出的票当天全部收在涨停价上。不取平均——一个漏报的池子"
                         "算出的比率不该被采信，也不该被平均进一个「中性」的数。"),
            }
    seal_rate = None if stat_row is None else _num(stat_row.get("seal_rate"))
    derived = False
    broken_unknown = counts.get("broken") is None
    if seal_rate is None and counts["limit_up"] and not broken_unknown:
        # The vendor only reports seal rate for the current session, so a past
        # day has to derive it: sealed / (sealed + failed boards).
        total = counts["limit_up"] + counts["broken"]
        if total > 0:
            seal_rate = counts["limit_up"] / total
            derived = True

    tiers: dict[str, int] = {}
    streaks: list[int] = []
    if breadth:
        tiers = dict(breadth["tiers"])
    elif up is not None and len(up) and "continue_cnt" in up.columns:
        for value in up["continue_cnt"].dropna():
            number = int(value)
            streaks.append(number)
            key = f"{number}板" if number < 6 else "6板及以上"
            tiers[key] = tiers.get(key, 0) + 1
    max_streak = breadth["max_boards"] if breadth else max(streaks, default=0)
    if ladder is not None and len(ladder) and "board_num" in ladder.columns:
        peak = pd.to_numeric(ladder["board_num"], errors="coerce").max()
        if pd.notna(peak):
            max_streak = max(max_streak, int(peak))

    reasons: list[dict] = []
    if up is not None and len(up) and "reason" in up.columns:
        buckets: dict[str, int] = {}
        for text in up["reason"].dropna():
            for part in str(text).replace("|", "+").split("+"):
                tag = part.strip()
                if tag:
                    buckets[tag] = buckets.get(tag, 0) + 1
        reasons = [{"tag": k, "count": v}
                   for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])[:8]]

    sealed: list[dict] = []
    rank_basis = "seal_money"
    pool_median: float | None = None
    # Zero, not None: an empty pool genuinely has no rows to rank, and the block
    # below is skipped entirely when there is no ``up`` frame at all. Leaving this
    # to the conditional made the return statement raise on every empty-pool day —
    # i.e. exactly the days the guard against "0 limit-ups" matters most.
    pool_n = 0
    if up is not None and len(up) and "seal_money" in up.columns:
        top = up.copy()
        top["seal_money"] = pd.to_numeric(top["seal_money"], errors="coerce")
        top = top.dropna(subset=["seal_money"])
        top = top[top["seal_money"] > 0]
        if strength is not None and len(strength):
            denom = top["ts_code"].astype(str).map(strength)
            ratio = top["seal_money"] / denom.where(denom > 0)
            ranked = top.assign(seal_ratio=ratio).dropna(subset=["seal_ratio"])
            if len(ranked):
                top = ranked.sort_values("seal_ratio", ascending=False)
                rank_basis = strength_basis
                pool_median = round(float(top["seal_ratio"].median()), 4)
        if rank_basis == "seal_money":
            top = top.sort_values("seal_money", ascending=False)
        pool_n = len(top)
        for position, (_, row) in enumerate(top.head(5).iterrows(), start=1):
            ratio = row.get("seal_ratio")
            sealed.append({
                "ts_code": row.get("ts_code"), "name": row.get("name"),
                "seal_money_cny": _json(row.get("seal_money"), 0),
                "seal_ratio": (None if ratio is None or pd.isna(ratio)
                               else round(float(ratio), 4)),
                "rank": position,
                "streak": row.get("continue_text"),
                "seal_time": str(row.get("limit_up_time") or "") or None,
            })

    first_seal = {"before_1000": 0, "1000_1300": 0, "after_1300": 0}
    if up is not None and len(up) and "limit_up_time" in up.columns:
        for stamp in up["limit_up_time"].dropna():
            text = str(stamp)
            if text < "10:00":
                first_seal["before_1000"] += 1
            elif text < "13:00":
                first_seal["1000_1300"] += 1
            else:
                first_seal["after_1300"] += 1

    return {
        "counts": counts,
        "seal_rate": _json(seal_rate, 3),
        "seal_rate_derived": derived,
        "unknown_pools": sorted(unknown),
        "max_streak": max_streak or None,
        "tiers": tiers,
        "promotion": promotion_rows,
        "promotion_source": promotion_source,
        **({"promotion_reset": True} if promotion_source.endswith("derived-reset") else {}),
        **({"promotion_spread": promotion_spread} if promotion_spread else {}),
        "first_seal": first_seal if any(first_seal.values()) else None,
        "top_reasons": reasons,
        "seal_strength": sealed,
        "seal_rank_basis": rank_basis,
        "seal_ratio_pool_median": pool_median,
        "seal_pool_n": pool_n,
    }


def _read(desk, name: str, start: str, end: str,
          columns: list[str] | None = None) -> pd.DataFrame:
    return read_or_empty(desk.data, name, start, end, columns)


def _seal_strength(market, day: str) -> tuple[pd.Series | None, str]:
    """流通市值(元) by ts_code, so 封单 can be ranked by strength, not by size.

    The 万元→元 conversion is **not** here — it lives in
    ``MarketRepository.circ_mv``. This function used to carry its own copy, and so
    did ``fill._circ_mv``; both had a comment claiming to be the only one. Two
    copies of a ×10,000 give a number that looks like real float and is off by
    four orders of magnitude once one of them drifts.

    Returns ``(None, "seal_money")`` when the table is missing or empty. The
    caller then keeps the old absolute ranking and *says so* through
    ``seal_rank_basis``, instead of silently mixing two scales.
    """
    try:
        series = market.circ_mv(day)
    except (KeyError, ValueError, FileNotFoundError, OSError):
        return None, "seal_money"
    if series is None or not len(series):
        return None, "seal_money"
    return series, "seal_money_over_circ_mv"


def _today_prices(desk, day: str) -> tuple[pd.DataFrame, str, bool]:
    """今日每只票的 (code, today_open, today_last)。收盘后走日线，盘中走实时快照。

    ``settled`` 决定这份数字的可信度：日线是终值，快照是某一刻的切片。
    调用方必须把这两件事分开说，因为它们对"今天强不强"的回答会在 14:00 与
    15:05 之间变好几次。
    """
    bars = read_or_empty(desk.data, "daily", day, day,
                       columns=["ts_code", "open", "close"])
    if bars is not None and not bars.empty:
        keep = bars.rename(columns={"ts_code": "code", "open": "today_open",
                                    "close": "today_last"})
        return keep[["code", "today_open", "today_last"]], "daily:close", True
    try:
        snap = desk.market.quotes(day)
    except (KeyError, ValueError, OSError):
        snap = None
    if snap is None or snap.empty:
        return pd.DataFrame(), "none", False
    # 一整天会落几百帧。要的是"现在"，所以每只票只留最新那一帧。
    latest = snap.sort_values("snapshot_ts").groupby("code").tail(1)
    keep = latest[["code", "open", "last"]].rename(
        columns={"open": "today_open", "last": "today_last"})
    # 键要统一成带后缀的 ts_code：`rt_quote` 存的是 6 位纯代码（"000037"），
    # 而 `limit_pool_t0` 和 `daily` 用的是 "000037.SZ"。不统一就直接 merge，
    # 交集是 **0 / 132**：2026-11-03 盘中实测，`measured=0`、
    # `open_premium` / `red_ratio` / `today_return` 全为 None，让这个"打板总开关"
    # 在盘中彻底失效——而收盘后走 daily 的 ts_code 分支却是好的，所以这个缺陷
    # 只在盘中暴露，恰好是最需要它的时段。
    from service.adapters.symbols import ts_code as _to_ts_code

    keep["code"] = keep["code"].map(lambda value: _to_ts_code(str(value)))
    return keep, "rt_quote:last", False


def yesterday_limit_today(desk, day: str | None = None) -> dict:
    """昨天的涨停股今天怎么样了——打板环境的直接度量。

    这是打板者第一眼看的东西：昨天的板今天给不给溢价。给，情绪还在、接力还有
    承接；不给，昨日的买盘正在被埋，今天就不该再往里加。整个链路里此前没有
    任何工具回答这个问题——``promotion_series`` 给的是"昨日 k 板今天晋级了几只"，
    那是收盘口径的计数，不含溢价，也不是盘中的连续量。

    口径：基准价用**昨日的涨停价**（派生池的 ``close``，对涨停股就是上限价），
    所以 ``open_premium`` 正是"昨天打板的人今天开盘赚不赚"。
    """
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "没有日线，无法锚定日期"}
    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    days = open_sessions(desk.data, start, day)
    if len(days) < 2:
        return {"ok": False, "as_of": day,
                "error": "trade_cal 里找不到上一交易日"}
    prev = days[-2]

    pool = read_or_empty(desk.data, "limit_pool_t0", prev, prev,
                       columns=["code", "pool", "boards", "close"])
    if pool is None or pool.empty:
        return {"ok": False, "as_of": day, "prev_day": prev,
                "error": f"{prev} 的派生池为空；先跑日更的 derived_pools"}
    prev_up = pool[pool["pool"].astype(str) == "limit_up"].copy()
    if prev_up.empty:
        return {"ok": False, "as_of": day, "prev_day": prev,
                "error": f"{prev} 派生池里没有涨停股"}

    prices, source, settled = _today_prices(desk, day)
    if prices.empty:
        return {"ok": False, "as_of": day, "prev_day": prev,
                "error": f"{day} 既没有日线也没有实时快照，今天涨跌无法测量"}

    prev_up = prev_up.rename(columns={"close": "prev_close", "boards": "prev_boards"})
    merged = prev_up.merge(prices, on="code", how="left")
    merged["prev_close"] = pd.to_numeric(merged["prev_close"], errors="coerce")
    merged["today_open"] = pd.to_numeric(merged["today_open"], errors="coerce")
    merged["today_last"] = pd.to_numeric(merged["today_last"], errors="coerce")
    merged = merged[merged["prev_close"] > 0]
    merged["open_premium"] = merged["today_open"] / merged["prev_close"] - 1.0
    merged["today_return"] = merged["today_last"] / merged["prev_close"] - 1.0

    # 晋级 = 今天又收在涨停价上。盘中派生池还没建，用东财池顶一次，并说明来源。
    from service.adapters.symbols import ts_code as _to_ts_code

    today_pool = read_or_empty(desk.data, "limit_pool_t0", day, day,
                             columns=["code", "pool"])
    promo_source = "derived:limit_pool_t0"
    if today_pool is None or today_pool.empty:
        vendor = read_or_empty(desk.data, "limit_up_pool", day, day, columns=["code"])
        # 东财池的 code 是 6 位纯代码（"000037"），派生池是带后缀的 ts_code
        # （"000037.SZ"），而 `merged["code"]` 来自派生池。取交集前必须拉齐，
        # 否则**盘中晋级率恒为 0%**——不是"今天一只都没晋级"，是键根本对不上。
        # 2026-11-03 实测：修复前 promoted_ratio=0.0，修复后同一时刻能算出来。
        today_codes = (set() if vendor is None or vendor.empty else
                       {_to_ts_code(str(code)) for code in vendor["code"]})
        promo_source = ("eastmoney:limit_up_pool" if today_codes
                        else "unavailable")
    else:
        today_codes = set(today_pool.loc[
            today_pool["pool"].astype(str) == "limit_up", "code"].astype(str))
    merged["promoted"] = (merged["code"].astype(str).isin(today_codes)
                          if today_codes else None)

    def block(frame: pd.DataFrame) -> dict:
        premium = frame["open_premium"].dropna()
        moved = frame["today_return"].dropna()
        promoted = frame["promoted"].dropna() if "promoted" in frame else pd.Series(dtype=float)
        return {
            "n": len(frame),
            "measured": len(moved),
            # 六位小数够用，且不会把 0.027263656099683637 这种尾巴带进上下文。
            "open_premium_mean": _num(round(float(premium.mean()), 6))
            if len(premium) else None,
            "open_premium_median": _num(round(float(premium.median()), 6))
            if len(premium) else None,
            "open_premium_positive": _num(round(float((premium > 0).mean()), 4))
            if len(premium) else None,
            "today_return_mean": _num(round(float(moved.mean()), 6))
            if len(moved) else None,
            "today_return_median": _num(round(float(moved.median()), 6))
            if len(moved) else None,
            "red_ratio": (_num(round(float((moved > 0).mean()), 4))
                          if len(moved) else None),
            "promoted_ratio": (_num(round(float(promoted.mean()), 4))
                               if len(promoted) else None),
        }

    tiers = []
    for boards, chunk in merged.groupby("prev_boards"):
        if len(chunk) < 3:
            continue
        tiers.append({"prev_boards": int(boards), **block(chunk)})
    tiers.sort(key=lambda row: row["prev_boards"])

    caveats = [
        ("基准价是昨日涨停价（派生池 close），所以 open_premium 是"
         "「昨天打板的人今天开盘赚不赚」，不是相对昨收的涨幅。"),
        "除权日会让基准价失真：派生池的 close 不复权，次日除权的票会被算成跳空。",
    ]
    if not settled:
        caveats.append("今日尚未收盘：today_return 是快照那一刻的值，会变；"
                       "settled=false 时不要把它当成当日结果引用。")
    if promo_source == "unavailable":
        caveats.append("今日涨停池还没建，promoted_ratio 为 null——不是「一只都没晋级」。")

    return {
        "ok": True,
        "as_of": day,
        "prev_day": prev,
        "settled": settled,
        "price_source": source,
        **block(merged),
        "by_prev_boards": tiers,
        "promoted_source": promo_source,
        "basis": ("昨日涨停股（派生池 limit_up）按今日开盘价与最新价对昨日涨停价"
                  "计算溢价；晋级 = 今日仍在涨停池"),
        "caveats": caveats,
        "provenance": {"datasets": ["limit_pool_t0", "daily", "rt_quote",
                                    "limit_up_pool"],
                       "source": source},
    }


def _yesterday_brief(desk, day: str) -> dict:
    """``yesterday_limit_today`` 的瘦身版，塞进 ``limit_structure`` 用。

    完整版约 1.6KB，而 ``limit_structure`` 本身已经不小；这里只留四个能一眼
    读出"昨天打板的人今天赚不赚"的数。要分层、要看溢价分布，走
    ``quant_knowledge(topic=tape_flow)``。
    """
    try:
        block = yesterday_limit_today(desk, day)
    except Exception as exc:  # noqa: BLE001 - 盘面读数失败不该拖垮整个 limit_structure
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
    if not block.get("ok"):
        return {"ok": False, "why": block.get("error")}
    return {
        "ok": True,
        "n": block["n"],
        "settled": block["settled"],
        "open_premium_mean": block["open_premium_mean"],
        "today_return_mean": block["today_return_mean"],
        "red_ratio": block["red_ratio"],
        "promoted_ratio": block["promoted_ratio"],
        "note": "昨涨停股今日相对昨日涨停价的溢价；正数=昨天打板的人还在赚",
    }


def pool_health(store, day: str) -> dict:
    """派生池今天到底有没有建、建得对不对——让人能看见，而不是等数字悄悄换源。

    家数、连板、晋级率现在都读派生池。它有三种失败方式，而三种都不会报错：

    * **没建**（当天分区缺失）——`breadth()` 返回 ``None``，上层退回厂商口径。
      数字照样出来，只是从"可核对"变成"少 1.6 只"，而读的人看不出来。
    * **建了但没预热**（单日重建，每只票都算 1 板）——`max_boards` 会安静地变成 1，
      于是情绪标签从主升掉到修复。2026-10-28 就是这样。
    * **停在几天前**——上周的池子回答今天的问题，看起来一切正常。

    所以这个函数不返回布尔值，返回 ``status`` 加一句 ``impact``：告诉读者**现在这个数
    是从哪来的、差多少**。``sessions_behind`` 用交易日历算，不是自然日——停牌周里
    "落后 3 天"和"落后 3 个交易日"是两件事。
    """
    frame = read_or_empty(store, "limit_pool_t0", day, day,
                        columns=["trade_date", "pool", "boards"])
    year_start = f"{day[:4]}0101"
    year = read_or_empty(store, "limit_pool_t0", year_start, day, columns=["trade_date"])
    last_derived = None
    if year is not None and not year.empty:
        last_derived = str(year["trade_date"].astype(str).max())

    out: dict = {"day": day, "last_derived": last_derived, "limit_up": None,
                 "max_boards": None, "sessions_behind": None}
    if frame is None or frame.empty:
        # Not "zero limit-ups": the partition for this session was never built.
        behind = _sessions_between(store, last_derived, day)
        out.update({
            "status": "missing",
            "sessions_behind": behind,
            "impact": ("家数、连板、晋级率会退回厂商口径（同花顺日均漏 1.6 只），"
                       "封单与首封时间不受影响。要修："
                       f"python scripts/derive_pools.py --start {day} --end {day} --warmup-days 30"),
        })
        return out

    frame = frame[frame["pool"].astype(str) == "limit_up"]
    boards = pd.to_numeric(frame.get("boards"), errors="coerce").dropna()
    count = len(frame)
    peak = int(boards.max()) if len(boards) else 0
    out.update({"limit_up": count, "max_boards": peak})

    if count >= 5 and peak <= 1:
        # Every streak restarting at 1 is the signature of a no-warm-up rebuild.
        # It is not proof — a genuinely all-first-board day looks the same — so the
        # status says "suspect" and the caller decides.
        out.update({
            "status": "unheated",
            "impact": ("最高板只有 1，而当天涨停 ≥5 只：这是没带预热重建派生池的典型形状"
                       "（2026-10-28 就是这样，情绪标签因此从主升掉到修复）。"
                       "不一定是错的（真可能全是首板），但连板高度不能直接引用。"
                       "要修：scripts/derive_pools.py --warmup-days 30"),
        })
        return out

    behind = _sessions_between(store, last_derived, day)
    out["sessions_behind"] = behind
    out["status"] = "fresh"
    out["impact"] = "家数/连板/晋级率来自派生池，可逐只核对到 close == up_limit。"
    return out


def _sessions_between(store, after: str | None, day: str) -> int | None:
    """``after`` 之后到 ``day``（含）之间有几个交易日。停牌周不算“落后”。"""
    if not after or after >= day:
        return 0
    try:
        cal = store.read("trade_cal", after, day, columns=["cal_date"],
                         filters=[("is_open", "==", 1)])
    except (KeyError, ValueError):
        return None
    if cal is None or cal.empty:
        return None
    days = cal["cal_date"].astype(str)
    return int((days > after).sum())


def breadth(store, day: str) -> dict | None:
    """今天几只涨停、最高几板——从派生池算，与晋级率同源。

    为什么不用同花顺的家数：它不是口径不同，是会漏。2026-09-01 至 10-30 的 38 个
    交易日里，同花顺的涨停家数只有 16 天与交易所限价逐只一致，55% 的日子偏低，
    平均每天少 1.6 只（2.8%），最差 2026-10-26 漏 16 只、多认 2 只，净 45 vs 59。漏掉的 60 条
    里有 35 条是沪深主板/创业板，25 条是北交所——所以这不是「算不算北交所」的口径
    问题，是名单本身不全。连板高度也受影响：2026-09-02 它把 5 板报成 4 板。

    派生池的定义是 ``close >= up_limit``（``service/adapters/derive.py``），输入是
    不复权日线和交易所限价，逐只可复核；这 19 天与基准 **19/19 逐只一致**。

    ``limit_up_ex_bj`` 单独给出，因为把 30% 涨跌幅的北交所算不算进「涨停家数」是
    一个**选择**（约 0.66 只/天），而漏报不是。读者可以按自己的口径取数。
    """
    frame = read_or_empty(store, "limit_pool_t0", day, day,
                        columns=["pool", "code", "boards"])
    if frame is None or frame.empty:
        return None
    frame = frame.copy()
    frame["pool"] = frame["pool"].astype(str)
    frame["boards"] = pd.to_numeric(frame["boards"], errors="coerce").fillna(0).astype(int)

    up = frame[frame["pool"] == "limit_up"]
    broken = frame[frame["pool"] == "broken_board"]
    down = frame[frame["pool"] == "limit_down"]
    if up.empty and broken.empty and down.empty:
        return None

    tiers: dict[str, int] = {}
    for number in up["boards"]:
        key = f"{number}板" if number < 6 else "6板及以上"
        tiers[key] = tiers.get(key, 0) + 1

    codes = up["code"].astype(str)
    return {
        "source": "derived:limit_pool_t0",
        "limit_up": len(up),
        "limit_up_ex_bj": int((~codes.str.endswith(".BJ")).sum()),
        "broken": len(broken),
        "limit_down": len(down),
        "max_boards": int(up["boards"].max()) if len(up) else 0,
        "tiers": tiers,
        "break_rate": (round(len(broken) / (len(up) + len(broken)), 4)
                       if (len(up) + len(broken)) else None),
    }


def _pool_token(desk) -> str | None:
    """A version stamp for the three pools, from the registry alone.

    ``snapshot_id`` is derived from the content checksum, so an unchanged pool
    re-ingested produces the same id and an edited one produces a different id.
    That makes this a correctness-preserving cache key rather than a TTL: the
    answer can only be reused when the rows behind it are byte-identical.

    ``None`` means "cannot be versioned" — a store without a registry, or a test
    double. Those get no cache at all. An unversioned token would otherwise be a
    single shared constant, so two different stores would read each other's
    frames, and an in-place edit would never be seen.
    """
    digest = hashlib.sha256()
    for name in _HISTORY_DATASETS:
        digest.update(name.encode())
        try:
            records = desk.data.registry.list_manifest(name)
        except (AttributeError, ValueError, OSError, TypeError):
            return None
        for rec in sorted(records, key=lambda r: str(r.partition)):
            digest.update(f"{rec.partition}:{rec.snapshot_id}:{rec.status}\x1f".encode())
    return digest.hexdigest()


def _pool_frames(desk) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the pools once per store version.

    The window is applied by the caller, so a question about any past session
    reuses the same frames. Reading them unbounded is not a look-ahead: every
    consumer filters to ``trade_date <= day`` before it counts anything.
    """
    token = _pool_token(desk)
    if token is not None:
        with _POOL_LOCK:
            cached = _POOL_CACHE.get(token)
        if cached is not None:
            return cached

    up = _clean_history(_read(desk, "ths_limit_up", None, None, columns=["trade_date"]))
    brk = _clean_history(_read(desk, "ths_limit_break", None, None, columns=["trade_date"]))
    stat = _clean_history(
        _read(desk, "ths_limit_stat", None, None, columns=["trade_date", "seal_rate"]))
    frames = (up, brk, stat)

    if token is not None:
        with _POOL_LOCK:
            if len(_POOL_CACHE) >= _POOL_CACHE_LIMIT:
                _POOL_CACHE.clear()
            _POOL_CACHE[token] = frames
    return frames


def _in_window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Same rows a bounded ``read`` would have returned."""
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return frame
    keys = frame["trade_date"].astype(str)
    return frame[(keys >= start) & (keys <= end)]


def limit_history(desk, day: str, *, back: int = 1500) -> dict:
    """Where today's tape sits in its own history.

    Twenty sessions is not a distribution, it is a month. Without a longer
    baseline "82 stocks limited up, seal rate 0.87" has no meaning: it could be a
    euphoric day or an ordinary Tuesday. This computes the percentile of today's
    readings against every session the store actually has.

    The two series have deliberately different sample sizes and the response says
    so: limit-up counts go back to when the pool endpoint starts serving history
    (late 2020), while seal rate needs the *broken board* pool too, which the
    vendor only keeps for a rolling year.
    """
    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=int(back * 1.6))
             ).strftime("%Y%m%d")
    # Read the columns the aggregation needs — 1,400 daily partitions carry
    # every column the vendor sent, and projecting is the difference between a
    # 2.4 second answer and a 0.2 second one. Frames come from a cache keyed on
    # the manifests, so the second question in a process costs nothing.
    up_all, brk_all, stat_all = _pool_frames(desk)
    up = _in_window(up_all, start, day)
    brk = _in_window(brk_all, start, day)
    stat = _in_window(stat_all, start, day)
    if up.empty:
        return {"ok": False, "error": "no limit-up pool history"}

    keys = up["trade_date"].astype(str)
    counts = keys.value_counts().sort_index()
    broken = (brk["trade_date"].astype(str).value_counts()
              if not brk.empty else pd.Series(dtype=int))

    reported: dict[str, float] = {}
    if not stat.empty and "seal_rate" in stat.columns:
        for date, chunk in stat.groupby(stat["trade_date"].astype(str)):
            values = pd.to_numeric(chunk["seal_rate"], errors="coerce").dropna()
            values = values[values.map(math.isfinite)]
            if len(values):
                reported[str(date)] = float(values.iloc[0])

    seal: dict[str, float] = {}
    derived_days = 0
    for date, up_count in counts.items():
        key = str(date)
        # One series, one definition. The vendor's ``seal_rate`` exists for five
        # sessions; the other 241 were derived as up/(up+broken). Mixing them put
        # five vendor readings inside a 246-day percentile — a median computed
        # across two definitions of the same word. Derive whenever the broken
        # pool can answer, and fall back to the vendor only when it cannot.
        rate = None
        if key in broken.index:
            total = int(up_count) + int(broken[key])
            if total > 0:
                rate = int(up_count) / total
                derived_days += 1
        if rate is None:
            rate = reported.get(key)
        if rate is not None:
            seal[key] = rate

    def _pct(series: dict, today_value):
        values = sorted(float(v) for v in series.values() if v is not None)
        if not values or today_value is None:
            return None, None
        below = sum(1 for v in values if v <= today_value)
        return round(below / len(values), 3), len(values)

    today_up = int(counts.get(day, 0)) if day in counts.index else None
    today_seal = seal.get(day)
    up_pct, up_n = _pct(counts.to_dict(), today_up)
    seal_pct, seal_n = _pct(seal, today_seal)

    def _spread(values: dict, quantiles=(0.1, 0.5, 0.9)):
        ordered = sorted(float(v) for v in values.values() if v is not None)
        if not ordered:
            return {}
        out = {}
        for q in quantiles:
            idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
            out[f"p{int(q * 100)}"] = round(ordered[idx], 3)
        return out

    return {
        "ok": True,
        "as_of": day,
        # 这一整块是同花顺口径的历史序列（分位数只有在一个连续的序列上才有意义，
        # 而库里的连续序列只有厂商那份）。它给出的 today 因此可能和
        # `counts.limit_up`（派生池，逐只可复核）差一两只——2026-11-02 是 131 对
        # 132。两个数都对，说的是两件事，所以这里必须自己说清是哪一份：
        # 不写，读者就会以为其中一个算错了。
        "basis": "ths",
        "basis_note": ("limit_up/seal_rate 的 today 与分位数都取自同花顺序列；"
                       "counts.limit_up 是派生池（close == up_limit），"
                       "两者相差几只属正常，比较不同日期时必须用同一份。"),
        "limit_up": {"today": today_up, "percentile": up_pct, "sessions": up_n,
                     **_spread(counts.to_dict())},
        "seal_rate": {"today": _json(today_seal, 3), "percentile": seal_pct,
                      "sessions": seal_n, **_spread(seal),
                      "derived_days": derived_days},
        "window": {"start": str(counts.index.min()), "end": str(counts.index.max())},
        "digest": _history_digest({"ths_limit_up": up, "ths_limit_break": brk,
                                   "ths_limit_stat": stat}),
        "caveats": [
            "分位数的分母是库里实际有的交易日，不是全部历史；样本量写在 sessions 里。",
            "封板率需要炸板池，而上游只保留一年，所以它的样本比涨停家数短得多。",
            ("封板率只在上游记录了炸板股的交易日上计算。完全没有炸板的交易日不会出现在"
             "炸板池里，因而被排除——这类日子极少，但会让分位略微偏高。"),
            "percentile=0.9 表示历史上 90% 的日子比今天弱，不代表明天会怎样。",
        ],
    }


def _coverage(desk, name: str) -> tuple[str, str] | None:
    """The date range a dataset actually holds, from the manifest."""
    try:
        rows = [r for r in desk.data.registry.summary() if r.get("dataset") == name]
    except (KeyError, ValueError, AttributeError):
        return None
    starts = [str(r.get("cover_start")) for r in rows if r.get("cover_start")]
    ends = [str(r.get("cover_end")) for r in rows if r.get("cover_end")]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def limit_structure(desk, day: str | None = None, recent: int = 5) -> dict:
    """How strong, how wide, how deep — and whether it is getting better."""
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars to anchor the date"}
    start = (datetime.strptime(day, "%Y%m%d")
             - timedelta(days=max(recent, 1) * 3)).strftime("%Y%m%d")

    stat = _read(desk, "ths_limit_stat", start, day)
    up = _read(desk, "ths_limit_up", start, day)
    down = _read(desk, "ths_limit_down", start, day)
    brk = _read(desk, "ths_limit_break", start, day)
    ladder = _read(desk, "ths_ladder", start, day)
    if up.empty and stat.empty:
        return {"ok": False, "as_of": day,
                "error": ("没有涨停结构数据。先运行 scripts/fetch_ths_special.py；"
                          "该数据源需要 THS_API_KEY。")}

    def on_day(frame):
        if frame is None or frame.empty:
            return frame
        return frame[frame["trade_date"].astype(str) == day]

    # Which pools were never fetched for this date. The limit-down and
    # broken-board endpoints only serve a rolling year, so a 2023 question has
    # no answer for them - and answering "0" would be a fabrication.
    unknown: set[str] = set()
    for key, name in (("limit_down", "ths_limit_down"), ("broken", "ths_limit_break")):
        span = _coverage(desk, name)
        if span is None or not (span[0] <= day <= span[1]):
            unknown.add(key)

    strength, strength_basis = _seal_strength(desk.market, day)
    # 晋级率由派生池自己算（250 天），上游那张 5 天的表降级为对账源。
    promo = promotion_series(desk.data, [start, day]).get(day)
    # 家数、连板高度也走派生池：与晋级率同源，而且能逐只核对到 close >= up_limit。
    # 同花顺的名单会漏，漏了就是漏了，不是另一种口径（见 ``breadth``）。
    pool_breadth = breadth(desk.data, day)
    summary = summarize_limit(on_day(stat), on_day(up), on_day(down),
                              on_day(brk), on_day(ladder), unknown=frozenset(unknown),
                              strength=strength, strength_basis=strength_basis,
                              promotion=promo, breadth=pool_breadth)

    notes = []
    if unknown:
        still = sorted(k for k in unknown if summary["counts"].get(k) is None)
        summary["unknown_pools"] = still
        if still:
            notes.append(f"{day} 的 {'/'.join(still)} 池不在已抓取范围内"
                         f"（上游只保留最近一年）；这些字段为 null 而不是 0。")
        else:
            notes.append(f"{day} 的同花顺 {'/'.join(sorted(unknown))} 池不在已抓取范围内，"
                         f"家数已改由派生池给出（逐只可核对），不是 0。")

    if on_day(up).empty:
        if not pool_breadth:
            # A seal-rate row without a pool means the pool for that date was never
            # ingested (typical before the evening fetch). Reporting zeros would look
            # like "nothing limited up", which is a different and wrong claim.
            summary["counts"] = {k: (None if k in ("limit_up", "limit_down", "broken")
                                     else v) for k, v in summary["counts"].items()}
            notes.append(f"{day} 的涨停/跌停/炸板池尚未入库；先跑 fetch_ths_special.py。")
        else:
            notes.append(f"{day} 的同花顺涨停池尚未入库；家数/连板/晋级率由派生池给出，"
                         f"封单、首封时间、涨停原因这些只有同花顺才有的字段为 null。")

    # 两个来源的家数差得明显就说明白。现在 counts.limit_up 是派生池的，
    # counts.vendor_ths.limit_up 是同花顺的；差多少、为什么差，读者要能一眼看到。
    #
    # 厂商那一半的键名是 ``vendor_ths``——它在别处被改过一次（原来叫
    # ``vendor``，与东财的 ``quant_facts.vendor_eastmoney`` 撞名，于是加了厂商
    # 前缀），而这一行没跟着改。它读 ``counts.get("vendor")``，拿到的永远是
    # None，于是这条对账说明**从来就没有出现过**，``tape.count_gap_warn``
    # 这个参数也跟着变成死代码——``scripts/audit_config_keys.py`` 报它"没被
    # 读过"，报的是实话：那个 `settings.num` 调用永远到不了。
    mine = summary["counts"].get("limit_up")
    theirs = (summary["counts"].get("vendor_ths") or {}).get("limit_up")
    if mine and theirs:
        gap = abs(mine - theirs) / max(mine, theirs)
        if gap > settings.num("tape.count_gap_warn"):
            notes.append(
                f"{day} 涨停家数两个来源差 {gap:.1%}：派生池 {mine} 只、同花顺 {theirs} 只。"
                f"派生池多的票当天确实收在涨停价上（close == up_limit）；"
                f"引用家数时写明用哪一版。")

    trend = []
    if not up.empty and "trade_date" in up.columns:
        # 逐日家数也从派生池取。否则「今天 59、昨天 55」会是两个来源拼出的序列，
        # 而中间那一天也许只是同花顺漏了几只——一条趋势线最不该有的就是换口径。
        hist = read_or_empty(desk.data, "limit_pool_t0", start, day,
                           columns=["trade_date", "pool"])
        trend_source = "derived:limit_pool_t0"
        if hist is not None and not hist.empty:
            h = hist[hist["pool"].astype(str) == "limit_up"]
            by_day = h.groupby(h["trade_date"].astype(str)).size()
        else:
            by_day = up.groupby(up["trade_date"].astype(str)).size()
            trend_source = "vendor:ths"
        seal_by_day: dict[str, float] = {}
        if not stat.empty and "seal_rate" in stat.columns:
            for date, chunk in stat.groupby(stat["trade_date"].astype(str)):
                values = pd.to_numeric(chunk["seal_rate"], errors="coerce").dropna()
                if len(values):
                    seal_by_day[date] = float(values.iloc[0])
        broken_by_day = (brk.groupby(brk["trade_date"].astype(str)).size()
                         if not brk.empty and "trade_date" in brk.columns else {})
        for date in sorted(by_day.index)[-recent:]:
            up_count = int(by_day[date])
            rate = seal_by_day.get(date)
            derived_trend = False
            if rate is None:
                broken = int(broken_by_day.get(date, 0))
                if up_count + broken > 0:
                    rate = up_count / (up_count + broken)
                    derived_trend = True
            trend.append({"trade_date": date, "limit_up": up_count,
                          "limit_up_source": trend_source,
                          "seal_rate": _json(rate, 3),
                          "seal_rate_derived": derived_trend})

    try:
        history = limit_history(desk, day)
    except Exception as exc:  # noqa: BLE001 - a baseline failure must not kill the tape read
        history = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "as_of": day,
        **summary,
        "cross_check": _cross_check(desk, day, summary),
        "trend": trend,
        "history": history,
        # 打板环境的第一个问题不是"今天几家涨停"，是"昨天涨停的那批今天怎么样"。
        # 放在这里是因为读 tape 的人一定需要它；完整版在 tape_flow。
        "yesterday": _yesterday_brief(desk, day),
        # 各身位"扣完成本后还有没有边际"。放在这里而不是新开一个工具：
        # 读 tape 的人下一步一定是在几个身位之间做取舍，而这件事此前只能靠
        # 模型自己拿 conditional 的均值心算——算错的代价是整笔方向。
        # 冷启动要读 6 年日线（约 5 秒，之后按日缓存），所以第一次调用会慢。
        "setup_thresholds": _setup_thresholds(desk, day),
        "notes": notes,
        "caveats": [
            "盘中调用得到的是不完整的当日数据：涨停会开板，封单会变，收盘才是终值。",
            "连板天梯每个板位最多展示 4 只；tiers 用涨停池全量计数，两者口径不同。",
            "涨停原因由上游标注，同一只票可能挂多个标签，计数会重复。",
            ("家数、连板、晋级率来自派生池（close == up_limit，逐只可核对）；"
             "封单额、首封时间、涨停原因只有同花顺有。一块响应里混了两个来源，"
             "counts.vendor 给出同花顺自己的家数，方便对账。"),
        ],
        "provenance": {"datasets": ["limit_pool_t0", "ths_limit_up", "ths_limit_down",
                                    "ths_limit_break", "ths_limit_stat", "ths_ladder"],
                       "source": "derived+ths", "trust_tier": "T1"},
    }


def _setup_thresholds(desk, day: str) -> dict:
    """身位 × 边际。失败时不拖垮整块 tape：它是附加信息，不是主结论。"""
    try:
        from .board import setup_table

        return setup_table(desk, day)
    except Exception as exc:  # noqa: BLE001 - 附加块坏了不该让 tape 读不出来
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _cross_check(desk, day: str, summary: dict) -> dict:
    """The other thermometer, measured on the same day.

    同花顺 and 东财 count limit-ups differently — whether ST, 北交所 and one-line
    boards are included — and on 2026-10-26 they differed by ten names (45 vs 55).
    Both are published numbers, so neither is wrong; but an answer that quotes one
    while the sentiment panel gates on the other reads as if there were a single
    truth. Worse, taking today's 同花顺 count against yesterday's 东财 count turns a
    ten-name definition gap into a 45% "退潮".

    The comparison itself lives in ``pool_cross`` so that ``quant_pools`` can make
    the same statement from its own side; this wrapper only keeps the shape the
    knowledge topic has always returned.
    """
    from .pool_cross import compare

    # `summary["counts"]` 里的 limit_up/broken/limit_down 此刻**已经是派生池的值**
    # ——同花顺的原值被 `summarize_limit` 挪进了 `counts.vendor_ths`。直接把它当
    # "我方（同花顺）"传给 compare，"ths" 那一列印出来的就是派生数，于是同一个
    # 响应里出现两套同花顺家数，而 delta 算的其实是"东财 vs 派生"。
    # 实测 2026-11-02：vendor_ths 131/28/10、派生 132/34/11，cross_check 报
    # ths=132/34/11、delta=-7/-6/-3；真值是 ths=131/28/10、delta=-6/0/-2。
    counts = dict(summary.get("counts") or {})
    vendor = counts.get("vendor_ths")
    if isinstance(vendor, dict):
        counts.update({key: vendor.get(key, counts.get(key))
                       for key in ("limit_up", "broken", "limit_down")})

    out = compare(desk, day, counts, source="ths")
    if not out.get("ok"):
        reason = str(out.get("note") or "")
        if "无法对账" not in reason:
            reason = f"无法对账：{reason}" if reason else "无法对账"
        return {"ok": False, "note": reason}
    return {"ok": True,
            "eastmoney": out["eastmoney"],
            "ths": out["ths"],
            "delta": out["delta_eastmoney_minus_ths"],
            # note 里让人"差得大时先看 derived"，那就得把 derived 一起给出来。
            # 派生池是唯一能逐只复核的一列，藏起来的话这句提醒等于叫人去猜。
            "derived": out.get("derived"),
            "missing_vs_derived": out.get("missing_vs_derived"),
            "note": out["note"]}


def lhb(desk, day: str | None = None, symbol: str | None = None,
        limit: int = 20, days: int = 10, seat: str | None = None) -> dict:
    """Who bought: net amounts, institutions versus hot money, and the seats.

    ``seat`` asks the follow-up question the board raises but never answers:
    *has this seat's buying meant anything?* It costs one extra read over 60
    sessions, so it only runs when asked.
    """
    record = None
    if seat:
        record = seat_record(desk, seat, day=day)
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars",
                **({"seat_record": record} if record else {})}
    start = (datetime.strptime(day, "%Y%m%d")
             - timedelta(days=max(days, 1) * 2)).strftime("%Y%m%d")
    stocks = _read(desk, "ths_lhb", start, day)
    seats = _read(desk, "ths_lhb_seat", start, day)
    if stocks.empty:
        return {"ok": False, "as_of": day,
                "error": ("没有龙虎榜数据。先运行 scripts/fetch_ths_special.py；"
                          "榜单收盘后发布，盘中只能看到上一交易日。"),
                **({"seat_record": record} if record else {})}

    data_date = str(stocks["trade_date"].astype(str).max())
    frame = stocks[stocks["trade_date"].astype(str) == data_date]
    if symbol:
        code = symbol.upper()
        frame = frame[frame["ts_code"].astype(str) == code]
        if frame.empty:
            return {"ok": True, "as_of": day, "data_date": data_date, "symbol": code,
                    "items": [], "note": f"{code} 未上 {data_date} 的龙虎榜",
                    **({"seat_record": record} if record else {})}

    frame = frame.sort_values("net_value", ascending=False).head(limit)
    items = []
    for _, row in frame.iterrows():
        items.append({
            "ts_code": row.get("ts_code"), "name": row.get("name"),
            "pct": _json(row.get("pct"), 2),
            "net_value_cny": _json(row.get("net_value"), 0),
            "net_rate_pct": _json(row.get("net_rate"), 2),
            "buy_cny": _json(row.get("buy_value"), 0),
            "sell_cny": _json(row.get("sell_value"), 0),
            "org_net_cny": _json(row.get("org_net_value"), 0),
            "hot_money_net_cny": _json(row.get("hot_money_net_value"), 0),
            "hot_rank": None if pd.isna(row.get("hot_rank")) else int(row.get("hot_rank")),
            "range_days": None if pd.isna(row.get("range_days")) else int(row.get("range_days")),
            "reason": row.get("reason"), "concepts": row.get("concepts"),
        })

    seat_rows = []
    if not seats.empty:
        pool = seats[seats["trade_date"].astype(str) == data_date]
        if symbol:
            pool = pool[pool["ts_code"].astype(str) == symbol.upper()]
        if len(pool):
            agg = (pool.groupby("seat")["net_value"].sum()
                   .sort_values(ascending=False).head(5))
            seat_rows = [{"seat": str(k), "net_value_cny": _json(v, 0)}
                         for k, v in agg.items()]

    return {
        "ok": True, "as_of": day, "data_date": data_date,
        "count": len(frame), "items": items, "top_seats": seat_rows,
        "caveats": [
            f"用的是 {data_date} 的榜单。龙虎榜在收盘后发布，盘中看不到当日。",
            "range_days=3 是三日内累计上榜，与当日榜混在同一张表里，不要重复计数。",
            "净买额为正不等于次日上涨；这是资金痕迹，不是预测。",
        ],
        **({"seat_record": record} if record else {}),
        "provenance": {"datasets": ["ths_lhb", "ths_lhb_seat"], "source": "ths",
                       "trust_tier": "T1"},
    }


def seat_record(desk, seat: str, *, day: str | None = None, days: int = 60) -> dict:
    """What happened after this seat appeared on the board.

    The board tells you who bought. It does not tell you whether that seat's
    buying has meant anything, and "T王 net-bought 654万" is a name, not a fact
    about the future. This closes the loop in the only currency that matters:
    the return from the first price the information was actually tradeable at.

    That price is the **next open**, not that day's close. The board is published
    around 20:00, hours after the close it describes, so a close entry would be
    buying a price that no longer exists. Same reasoning as
    ``research/spec.py`` refusing a same-day entry, applied to a different fact.

    Two horizons are reported because they answer different questions:

    * ``next_open_gap`` — close[D] → open[D+1]. This is the overnight premium the
      board is usually quoted for, and the only part a T+1 buyer can collect by
      selling into the open.
    * ``next_close`` — close[D] → close[D+1]. What is left if you hold the session.

    No confidence interval. Daily returns here are fat-tailed and 20 observations
    of one seat are not a distribution, so the quartiles travel with the mean and
    ``usable`` says whether there is enough to read at all.
    """
    seat = str(seat or "").strip()
    if not seat:
        return {"ok": False, "error": "seat 不能为空"}
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars"}

    start = (datetime.strptime(day, "%Y%m%d")
             - timedelta(days=max(days, 1) * 2)).strftime("%Y%m%d")
    rows = _read(desk, "ths_lhb_seat", start, day)
    if rows.empty or "seat" not in rows.columns:
        return {"ok": False, "seat": seat, "as_of": day,
                "error": ("没有席位数据。先运行 scripts/fetch_ths_special.py --only lhb；"
                          "该表与个股榜同源，收盘后发布。")}

    rows = rows[rows["seat"].astype(str).str.strip() == seat].copy()
    if rows.empty:
        return {"ok": True, "seat": seat, "as_of": day, "appearances": 0,
                "window_days": days,
                "note": f"{start}~{day} 这个席位一次都没上榜；换个席位名，或把 days 放大。"}

    rows["trade_date"] = rows["trade_date"].astype(str)
    rows = rows.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")
    codes = sorted(set(rows["ts_code"].astype(str)))

    horizon = (datetime.strptime(day, "%Y%m%d") + timedelta(days=15)).strftime("%Y%m%d")
    daily = _read(desk, "daily", start, horizon,
                  columns=["ts_code", "trade_date", "open", "close"])
    if daily.empty:
        return {"ok": False, "seat": seat, "as_of": day,
                "error": "daily 为空，无法计算上榜之后的收益"}
    daily = daily[daily["ts_code"].astype(str).isin(codes)].copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    for column in ("open", "close"):
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
        daily.loc[daily[column] <= 0, column] = pd.NA
    daily = daily.dropna(subset=["open", "close"]).sort_values(
        ["ts_code", "trade_date"], kind="mergesort")

    grouped = daily.groupby("ts_code", sort=False)
    daily["next_open"] = grouped["open"].shift(-1)
    daily["next_close"] = grouped["close"].shift(-1)
    daily["next_date"] = grouped["trade_date"].shift(-1)

    merged = rows.merge(
        daily[["ts_code", "trade_date", "close", "next_open", "next_close", "next_date"]],
        on=["ts_code", "trade_date"], how="left")
    # "次日开盘"必须是**下一个交易日**的 open。
    #
    # 原来用自然日跨度（0 < 天数 ≤ 7）挡停牌。停两天再赶上周末正好跨 4 天，照样通过，
    # 于是那个 open 是两天后的价——一条"次日溢价"里混进了 T+2。改成对交易日历：
    # 只有 next_date 恰好等于该日的下一个交易日才算数。日历取不到时退回旧口径并标注。
    sessions = open_sessions(desk.data, start, horizon)
    if sessions:
        expected_next = {day: sessions[i + 1] for i, day in enumerate(sessions[:-1])}
        aligned = (merged["next_date"].astype(str)
                   == merged["trade_date"].map(expected_next).astype(str))
        merged = merged[aligned].copy()
        basis = "next_session"
    else:
        span = (pd.to_datetime(merged["next_date"], format="%Y%m%d", errors="coerce")
                - pd.to_datetime(merged["trade_date"], format="%Y%m%d", errors="coerce")).dt.days
        merged = merged[(span > 0) & (span <= 7)].copy()
        basis = "calendar_days<=7"

    def summarize(column: str) -> dict:
        values = pd.to_numeric(merged[column], errors="coerce").dropna()
        if not len(values):
            return {"n": 0}
        return {
            "n": len(values),
            "mean": round(float(values.mean()), 4),
            "median": round(float(values.median()), 4),
            "p25": round(float(values.quantile(0.25)), 4),
            "p75": round(float(values.quantile(0.75)), 4),
            "win": round(float((values > 0).mean()), 4),
        }

    merged["gap"] = merged["next_open"] / merged["close"] - 1
    merged["next_close_ret"] = merged["next_close"] / merged["close"] - 1

    latest = merged.sort_values("trade_date").tail(1)
    latest_row = None
    if len(latest):
        row = latest.iloc[0]
        latest_row = {
            "day": str(row.get("trade_date")),
            "ts_code": str(row.get("ts_code")),
            "name": row.get("name"),
            "net_value_cny": _json(row.get("net_value"), 0),
            "next_open_gap": _json(row.get("gap"), 4),
        }

    gap = summarize("gap")
    return {
        "ok": True,
        "seat": seat,
        "as_of": day,
        "window_days": days,
        "appearances": len(merged),
        "stocks": int(merged["ts_code"].nunique()),
        "next_open_gap": gap,
        "next_close": summarize("next_close_ret"),
        "latest": latest_row,
        "gap_basis": basis,
        "usable": bool(gap.get("n", 0) >= 20),
        "caveats": [
            "入场点取次日开盘：龙虎榜 20:00 才发布，用当日收盘价入场属于回看。",
            "席位名是上游的昵称（如 T王/Asking），不是券商营业部全称，同一游资换席位就断档。",
            ("有些「席位」是一类而不是一个人：T王、机构专用、沪股通这类标签会把很多参与者"
             "加在一起，几十个交易日内出现上千次。这类标签的记录是「这一类人平均怎样」，"
             "不能读成「某个人买了这只票」。"),
            ("样本是「这个席位出现在榜上的所有股票」，不是「跟着它买的所有股票」；"
             "榜上只显示买卖前五，净买额不是它的全部仓位。"),
            (f"窗口 {start}~{day}，共 {len(merged)} 个可结算样本；"
             f"{'够读' if gap.get('n', 0) >= 20 else '不足 20 个，只能看方向不能下结论'}。"),
        ],
        "provenance": {"datasets": ["ths_lhb_seat", "daily", "trade_cal"], "source": "ths",
                       "trust_tier": "T1"},
    }


# ------------------------------------------------------------------ auction
def auction(desk, day: str | None = None, limit: int = 10) -> dict:
    """What the call auction was saying before the open.

    Two readings matter and they are different questions:

    * **who was being grabbed** — 竞价抢筹 with a large bid amount relative to the
      previous session's turnover. That is attention arriving before the bell.
    * **who was being dumped** — 竞价砸盘 / 大幅低开. That is the crowd leaving.

    The cross-tab against the limit-up pool is the useful part: a stock that was
    grabbed at 09:25 and still sealed at 15:00 is a different animal from one that
    was grabbed and failed.
    """
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars"}
    frame = _read(desk, "ths_auction", day, day)
    if frame.empty:
        return {"ok": False, "as_of": day,
                "error": ("没有竞价数据。先运行 scripts/fetch_ths_special.py --only auction；"
                          "该接口按 yyyy-MM-dd 支持历史。")}

    counts = frame["warn_name"].value_counts().to_dict()
    grabbed = frame[frame["warn_name"].astype(str).str.contains("抢筹")]
    dumped = frame[frame["warn_name"].astype(str).str.contains("砸盘")]

    up = _read(desk, "ths_limit_up", day, day)
    codes_up = set(up["ts_code"].astype(str)) if not up.empty else set()
    converted = [str(c) for c in grabbed["ts_code"] if str(c) in codes_up] if codes_up else []

    def _rows(sub, n):
        if sub.empty:
            return []
        pick = sub.sort_values("bid_amount", ascending=False).head(n)
        rows = []
        for _, r in pick.iterrows():
            previous = pd.to_numeric(pd.Series([r.get("previous_amount")]), errors="coerce").iloc[0]
            bid = pd.to_numeric(pd.Series([r.get("bid_amount")]), errors="coerce").iloc[0]
            rows.append({
                "ts_code": r.get("ts_code"), "name": r.get("name"),
                "alert": r.get("warn_name"), "bid_pct": _json(r.get("bid_pct"), 2),
                "bid_amount_cny": _json(r.get("bid_amount"), 0),
                "prev_amount_cny": _json(r.get("previous_amount"), 0),
                "amount_to_previous": (_json(bid / previous, 3)
                                       if pd.notna(previous) and previous > 0 and pd.notna(bid)
                                       else None),
                "event_count": _json(r.get("event_count"), 0),
                "first_warn_at": (None if "first_warn_at" not in r or pd.isna(r.get("first_warn_at"))
                                  else str(r.get("first_warn_at"))),
                "last_warn_at": (None if "last_warn_at" not in r or pd.isna(r.get("last_warn_at"))
                                 else str(r.get("last_warn_at"))),
                "sealed": str(r.get("ts_code")) in codes_up if codes_up else None,
            })
        return rows

    event_counts = (pd.to_numeric(frame["event_count"], errors="coerce")
                    if "event_count" in frame.columns else pd.Series(dtype=float))
    complete = None
    if "capture_complete" in frame.columns and frame["capture_complete"].notna().any():
        complete = bool(frame["capture_complete"].fillna(False).all())
    truncated = None
    if "capture_truncated" in frame.columns and frame["capture_truncated"].notna().any():
        truncated = bool(frame["capture_truncated"].fillna(False).any())

    return {
        "ok": True, "as_of": day,
        "alerts": len(frame),
        "capture": {
            "complete": complete, "truncated": truncated,
            "raw_events": (int(event_counts.sum()) if event_counts.notna().any() else None),
            "repeated_alert_groups": (int((event_counts > 1).sum())
                                      if event_counts.notna().any() else None),
            "note": "旧分区没有重复过程字段时对应值为 null，不按 1 次或完整覆盖补写",
        },
        "by_type": {str(k): int(v) for k, v in counts.items()},
        "grabbed_top": _rows(grabbed, limit),
        "dumped_top": _rows(dumped, limit),
        "grabbed_then_sealed": {"count": len(converted), "symbols": converted[:10],
                                "note": "竞价抢筹且当日进入涨停池；不等于收盘仍封住"},
        "caveats": [
            "竞价异动是供应商的规则告警，不是全市场竞价快照：没触发的票这里看不到。",
            "bid_amount 是竞价成交额，不是封单额；amount_to_previous 只是上游同名字段之比，不解释为收益率或封单强度。",
            "event_count/first_warn_at/last_warn_at 描述重复告警过程；旧分区未保留这些过程时显示 null。",
            "一只票可能同时触发多种告警，by_type 的计数会大于去重股票数。",
        ],
        "provenance": {"datasets": ["ths_auction", "ths_limit_up"], "source": "ths",
                       "trust_tier": "T1"},
    }


# ------------------------------------------------------------------ anomaly
def anomaly(desk, day: str | None = None, symbol: str | None = None,
            limit: int = 10) -> dict:
    """Why it moved, as a lead to verify — not as a fact.

    The upstream text is machine-written prose summarising public information. It
    is genuinely useful for "what is the story here" and genuinely dangerous if
    quoted as a filing. Every response says so.
    """
    # Anomaly explanations belong to the session they describe, and the vendor
    # only serves today's. Anchoring to the latest *bar* would silently look at
    # the wrong day whenever the bar lags the calendar (e.g. before the evening
    # fetch), so the calendar date wins and the bar date is the fallback.
    # 这里原来有一句 `if not day: return {"ok": False, "error": "no date"}`。
    # 上一行刚用 `datetime.now()` 赋过值，它永远不可能成立——一个永不触发的
    # 分支、一条无法写的测试，看起来像防御，实际是让人以为这条路径被想过。
    day = day or datetime.now(SHANGHAI).strftime("%Y%m%d")
    frame = _read(desk, "ths_anomaly", day, day)
    if frame.empty:
        return {"ok": False, "as_of": day,
                "error": ("当日没有异动归因数据。该接口只返回当日，历史不可回补；"
                          "每天收盘后跑 scripts/fetch_ths_special.py --only anomaly 积累。")}

    caveats = [
        "analysis 是上游 AI 归纳的公开信息，不是公告原文；引用前必须回原文核对。",
        "该接口只返回当日，因此本库的异动归因是从开始采集那一天起累积的。",
        "归因文本可能把相关当因果；它回答'市场在讲什么故事'，不回答'故事是真的'。",
    ]
    if symbol:
        code = symbol.upper()
        row = frame[frame["ts_code"].astype(str) == code]
        if row.empty:
            return {"ok": True, "as_of": day, "symbol": code, "items": [],
                    "note": f"{code} 当日没有异动归因（未触发大涨/大跌/涨跌停阈值）"}
        item = row.iloc[0]
        return {"ok": True, "as_of": day, "symbol": code,
                "items": [{"ts_code": item.get("ts_code"), "name": item.get("name"),
                           "tag": item.get("tag"), "keywords": item.get("keywords"),
                           "analysis": item.get("analysis")}],
                "caveats": caveats,
                "provenance": {"datasets": ["ths_anomaly"], "source": "ths",
                               "trust_tier": "T2"}}

    by_tag = frame["tag"].astype(str).value_counts().to_dict()
    items = []
    for _, row in frame.head(limit).iterrows():
        items.append({"ts_code": row.get("ts_code"), "name": row.get("name"),
                      "tag": row.get("tag"), "keywords": row.get("keywords"),
                      "analysis": (row.get("analysis") or "")[:200]})
    return {"ok": True, "as_of": day, "count": len(frame),
            "by_tag": {str(k): int(v) for k, v in by_tag.items()},
            "items": items, "caveats": caveats,
            "provenance": {"datasets": ["ths_anomaly"], "source": "ths",
                           "trust_tier": "T2"}}
