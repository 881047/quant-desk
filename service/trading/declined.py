"""「我看了，决定不买」——第四种可结算的判断。

提案由 arena 结算（超额 vs 等权市场），人格切换由 scorecard 结算，「这个机制成不
成立」由裁判结算。而**不行动**此前没有任何落点：2026-11-01 到 11-03 三天零成交、
零记录，等于三天没有任何关于判断力的证据，只有一段文字。

这个模块只做两件事：记下你放弃的那几只，和到期后算出它们后来怎么走。判断本身
不在这里——这里不给"你做得对/不对"，只给「如果你买了，会是什么结果」减去同期
等权市场。

**价格不进写入路径。** 调用方只给标的、期限和理由；成交价与结算价由这里的结算
自己从 `daily` 读。否则一条"我不买"的记录可以事后挑一个价格来自证。

口径：
- 入场 = 决策日（或之前最近一个交易日）的收盘。这是"你在收盘时看到了它"的价格。
- 出场 = 之后第 `horizon_days` 个交易日的收盘。
- 收益按逐日 `close / pre_close` 连乘，不直接用两点的 `close` 相除——除权日那天的
  `pre_close` 已经是调整后的昨收，用 `adj_close` 反而不等于你实际付出的价格。
- 超额 = 标的区间收益 − 同期等权市场收益（与 arena 同一套基准）。

与业界「影子信号 / Post-Rejection Follow-up Sampling」那套做法对齐了三点：拒绝
也要留痕、按固定期限跟踪、用同一套行情而不是事后换指标。**故意没对齐两点**：

- 不做「按闸门汇总误杀率」。那套做法假设存在若干命名闸门（流动性、动量、集中度），
  而本台的「不做」不是被闸门拒的——是读的人判断的。硬造一套闸门分类，等于用代码
  定义判断的词汇表，而这正是这一轮在拆的东西。`reason` 保留自由文本，不聚合。
- 不做「按止盈标准判误杀」。判定需要目标价，而目标价是一次判断；用结果反推目标，
  等于给事后找理由开门。
"""

from __future__ import annotations

import math

import pandas as pd

from . import arena

#: 一条记录最多几只票。不是为了性能，是为了让"我看了 60 只都不买"这种记录
#: 别变成一份持仓清单——真正该被结算的是**差一点就买的那几只**。
MAX_SYMBOLS = 30

#: 期限上限。超过一个月的"不做"已经不是判断，是立场。
MAX_HORIZON = 20


def _normalise(symbols: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for raw in symbols or []:
        code = str(raw or "").strip().upper()
        if not code:
            continue
        if "." not in code:
            raise ValueError(f"标的要带交易所后缀，例如 600000.SH；收到 {raw!r}")
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("至少要写一只标的——没有标的的「不做」无法结算。"
                         "写下你差一点就买的那几只，哪怕只有一只")
    if len(out) > MAX_SYMBOLS:
        raise ValueError(f"最多 {MAX_SYMBOLS} 只；只写差一点就买的那几只")
    return out


def add(paper, day: str, symbols: list[str], reason: str, *,
        invalidation: str = "", horizon_days: int = 3) -> dict:
    """记一条「看过、没买」。返回写进去的那一行，含它的到期口径。"""
    codes = _normalise(symbols)
    if not str(reason or "").strip():
        raise ValueError("reason 不能为空：不写为什么不买，这条记录结算完也读不懂")
    horizon = int(horizon_days)
    if not 1 <= horizon <= MAX_HORIZON:
        raise ValueError(f"horizon_days 要在 1..{MAX_HORIZON}；更长的'不做'不是判断，是立场")
    day = str(day)
    if len(day) != 8 or not day.isdigit():
        raise ValueError(f"day 要 YYYYMMDD；收到 {day!r}")

    # 同一只票一天只记一次。结算按行算，重复记＝把同一个判断算两遍，命中率会被
    # 少数几只反复出现的票带偏；09:26 记过的票，14:30 再权衡一次不该新增一行。
    # 2026-11-13 的复盘把「避免同日两笔」写成规矩，但那时只靠模型自觉。
    recorded: dict[str, int] = {}
    for row in paper.list_declined(limit=200):
        if str(row.get("day")) != day:
            continue
        for code in row.get("symbols") or []:
            recorded.setdefault(code, int(row["id"]))
    fresh = [code for code in codes if code not in recorded]
    skipped = [code for code in codes if code in recorded]
    if not fresh:
        return {"ok": True, "id": recorded[codes[0]], "declined": None,
                "skipped": skipped,
                "note": (f"{day} 已经记过 {', '.join(skipped)}：同一只票一天只记一次。"
                         "重复记会让结算把它算两遍——要补理由就写进报告或 journal，"
                         "不必新增一行。")}

    declined_id = paper.add_declined(day, fresh, str(reason).strip(),
                                     invalidation=str(invalidation or "").strip(),
                                     horizon_days=horizon)
    row = next((r for r in paper.list_declined(limit=50) if r["id"] == declined_id), None)
    out = {"ok": True, "id": declined_id, "declined": row,
           "will_settle": (f"{day} 之后第 {horizon} 个交易日收盘后可以结算；"
                           f"在那之前它一直是 pending，不是失败")}
    if skipped:
        out["skipped"] = skipped
        out["note"] = (f"{', '.join(skipped)} 今天已经记过，没有重复写入；"
                       "结算按行算，同一只票一天只算一次。")
    return out


def _sessions(store, start: str) -> list[str]:
    frame = store.read("daily", start, None, columns=["trade_date"])
    if frame.empty:
        return []
    return sorted({str(d) for d in frame["trade_date"]})


def _symbol_returns(store, symbols: list[str], window: list[str]) -> dict[str, float]:
    """逐日 close/pre_close 连乘，得到窗口内的区间收益。"""
    frame = store.read("daily", window[0], window[-1],
                       columns=["ts_code", "trade_date", "close", "pre_close"])
    if frame.empty:
        return {}
    frame = frame[frame["ts_code"].isin(symbols)].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame = frame[frame["trade_date"].isin(window)]
    if frame.empty:
        return {}
    close = pd.to_numeric(frame["close"], errors="coerce")
    prev = pd.to_numeric(frame["pre_close"], errors="coerce")
    frame["ret"] = (close / prev.where(prev > 0) - 1)
    frame["ret"] = frame["ret"].replace([float("inf"), float("-inf")], float("nan"))
    out: dict[str, float] = {}
    for code, part in frame.groupby("ts_code"):
        series = part.sort_values("trade_date")["ret"].dropna()
        # 一天的 bar 缺失会让连乘把那段当成 0%——那不是"没涨"，是"不知道"。
        # 要求窗口内每个交易日都有这一只，才给它的区间收益。
        if len(series) < len(window):
            continue
        product = float((1 + series).prod())
        if math.isfinite(product):
            out[str(code)] = product - 1
    return out


def score(store, paper, *, dry_run: bool = False) -> dict:
    """把到期的「不做」结算掉。到期 = 之后已经有 `horizon_days` 个交易日。

    没到期的原样留着并报 `pending`——它不是失败，是还没到能判的时候。
    """
    rows = paper.list_declined(limit=500, settled=False)
    if not rows:
        return {"scored": [], "pending": 0, "blocked": [],
                "note": "没有待结算的记录。盘后决定不做时，用 add 记下你放弃的那几只"}

    sessions = _sessions(store, min(r["day"] for r in rows))
    if not sessions:
        return {"scored": [], "pending": len(rows), "blocked": [],
                "note": "daily 里没有可用的交易日，结算不了"}

    market = arena._market_returns(store, start=min(r["day"] for r in rows))

    scored: list[dict] = []
    blocked: list[dict] = []
    pending = 0

    # 先把每行的窗口定下来，**按窗口分组再读**。原来每行读一次 `daily`：
    # 60 条积压要 3.0 秒，500 条就是 25 秒，而这个函数跑在一个 HTTP 请求里。
    # 同一天记的几行共享同一个窗口，读一次就够；跨月的积压也只会多几次读。
    plan: list[tuple[dict, str, list[str]]] = []
    for row in rows:
        day = str(row["day"])
        horizon = int(row["horizon_days"])
        entry = max((d for d in sessions if d <= day), default=None)
        if entry is None:
            blocked.append({"id": row["id"], "day": day,
                            "error": f"{day} 之前没有交易日数据"})
            continue
        after = [d for d in sessions if d > entry]
        if len(after) < horizon:
            pending += 1
            continue
        plan.append((row, entry, after[:horizon]))

    groups: dict[tuple[str, str], list[tuple[dict, str, list[str]]]] = {}
    for item in plan:
        key = (item[2][0], item[2][-1])
        groups.setdefault(key, []).append(item)

    for (start_day, end_day), group in groups.items():
        window = [d for d in sessions if start_day <= d <= end_day]
        wanted = sorted({code for row, _entry, _w in group for code in row["symbols"]})
        per_symbol_all = _symbol_returns(store, wanted, window)
        benchmark = arena._window_return(market, start_day, end_day)
        for row, entry, _window in group:
            per_symbol = {c: v for c, v in per_symbol_all.items() if c in row["symbols"]}
            if not per_symbol:
                blocked.append({"id": row["id"], "day": str(row["day"]),
                                "error": "窗口内没有这些标的的完整日线"})
                continue
            if benchmark is None:
                blocked.append({"id": row["id"], "day": str(row["day"]),
                                "error": "同期等权市场算不出来（基准缺失）"})
                continue

            detail = [{"symbol": code, "return": round(ret, 5),
                       "excess": round(ret - float(benchmark), 5)}
                      for code, ret in sorted(per_symbol.items())]
            missing = [c for c in row["symbols"] if c not in per_symbol]
            result = {
                "entry_date": entry,
                "end_date": window[-1],
                "sessions": len(window),
                "market_return": round(float(benchmark), 5),
                "symbols": detail,
                "missing": missing,
                "mean_return": round(sum(d["return"] for d in detail) / len(detail), 5),
                "mean_excess": round(sum(d["excess"] for d in detail) / len(detail), 5),
                "n": len(detail),
            }
            scored.append({"id": row["id"], "day": str(row["day"]),
                           "reason": row["reason"], **result})
            if not dry_run:
                paper.settle_declined(row["id"], result)

    return {"scored": scored, "pending": pending, "blocked": blocked,
            "note": ("mean_excess 是「如果你买了，会跑赢还是跑输等权市场」。"
                     "为负表示这些票后来比市场差——那次不买是对的；"
                     "为正表示它们比市场好——克制在那一次是成本。"
                     "这是算术，不是评语：要不要据此调整标准，是读的人的判断。")}


def summary(paper, *, limit: int = 1000) -> dict:
    """把已结算的「不做」汇总。没有判词，只有分布。"""
    rows = paper.list_declined(limit=limit, settled=True)
    observations: list[dict] = []
    for row in rows:
        for item in (row.get("result") or {}).get("symbols") or []:
            observations.append({"id": row["id"], "day": row["day"],
                                 "symbol": item["symbol"],
                                 "excess": item["excess"]})
    if not observations:
        return {"n_decisions": 0, "n_observations": 0,
                "mean_excess": None, "underperformed_share": None,
                "decisions": [],
                "note": "还没有结算过的「不做」记录"}

    excesses = [o["excess"] for o in observations]
    return {
        "n_decisions": len(rows),
        "n_observations": len(observations),
        "mean_excess": round(sum(excesses) / len(excesses), 5),
        # 负超额占多少：这是"没买是对的"的比例，不是胜率——分母是标的数不是天数。
        "underperformed_share": round(sum(1 for x in excesses if x < 0) / len(excesses), 3),
        "decisions": [{"id": r["id"], "day": r["day"], "reason": r["reason"][:60],
                       "mean_excess": (r.get("result") or {}).get("mean_excess"),
                       "n": (r.get("result") or {}).get("n")}
                      for r in rows],
        "caveats": [
            "观测不独立：同一天放弃的几只票同涨同跌，n_observations 远大于有效样本量。",
            "只统计结算过的；`pending` 的不在这里，缺它们会让早期记录占更大权重。",
            "mean_excess 为负 = 放弃的票跑输市场 = 那次不买是对的。措辞容易反，读的时候对一遍。",
        ],
    }

