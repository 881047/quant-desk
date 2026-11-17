"""情绪状态转移：今天这种日子，历史上之后几天发生了什么。

`/knowledge/regime` 回答的是**指数**在四种市场状态下的前瞻收益，回答不了
"今天炸板率 41%，明天打板环境会怎样"——那要的是**情绪结构**自己的转移。
2026-11-03 的复盘里，这个问题只能靠人肉比对几天的数字；而 `limit_pool_t0`
从 2010 年起就带 `boards`，4,085 个交易日的派生池足够直接算。

三个指标，全部由派生池自算（不走同花顺：它在 20261026 漏报过 5 只涨停，
把那天的晋级率算低了 22 个百分点）：

* 炸板率 = 炸板 / (涨停 + 炸板)
* 晋级率 = 昨日涨停股今日 board+1 的只数 / 昨日涨停只数
* 最高板 = 当日 `boards` 的最大值

## 样本不独立，这是最容易骗人的地方

连续 20 个退潮日会被算成 20 个样本，实际可能只来自一两段事件。所以每次回答
都同时给 `n_days` 与 `n_episodes`（按连续段切分后的段数）——和 `/knowledge/regime`
的 caveats 是同一条教训：793 个 stressed 交易日只来自 43 段事件。

## 这不是预测

它给的是**条件分布**：历史上处于同一档的日子，之后 1/3 日的指标均值与改善比例。
分布不等于明天的答案；`n_episodes` 小于 8 时，连分布都谈不上。
"""

from __future__ import annotations

import pandas as pd

#: 分档阈值。炸板率沿用闸门口径（`sentiment.break_rate_gate` 默认 0.38），
#: 晋级率取 0.15 —— 2026-11-02 全市场是 0.2245、11-03 是 0.1212，
#: 0.15 正好把这两类日子分开。调这两个数等于改"什么叫同类日子"，需谨慎。
BREAK_GATE = 0.38
PROMOTION_GATE = 0.15

#: 两个交易日间隔超过这么多**自然日**就算新的一段事件（覆盖周末与长假）。
EPISODE_GAP_DAYS = 7


def _read(store, name: str, start: str, end: str, columns: list[str]) -> pd.DataFrame:
    try:
        frame = store.read(name, start=start, end=end, columns=columns)
    except (KeyError, ValueError, FileNotFoundError):
        return pd.DataFrame()
    return frame if frame is not None else pd.DataFrame()


def daily_metrics(store, start: str, end: str) -> pd.DataFrame:
    """逐日的情绪三件套。只有派生池，不掺任何厂商口径。

    返回列：trade_date / limit_up / broken / limit_down / break_rate /
    promotion_rate / max_boards。
    """
    pool = _read(store, "limit_pool_t0", start, end,
                 ["trade_date", "code", "pool", "boards"])
    if pool.empty:
        return pd.DataFrame()
    pool = pool.copy()
    pool["trade_date"] = pool["trade_date"].astype(str)
    pool["boards"] = pd.to_numeric(pool["boards"], errors="coerce").fillna(0.0)

    groups = {day: part for day, part in pool.groupby("trade_date")}
    rows: list[dict] = []
    prev_up: pd.Series | None = None
    for day in sorted(groups):
        part = groups[day]
        up = part[part["pool"].astype(str) == "limit_up"]
        broken = part[part["pool"].astype(str) == "broken_board"]
        down = part[part["pool"].astype(str) == "limit_down"]

        attempts = len(up) + len(broken)
        break_rate = (len(broken) / attempts) if attempts else None

        promotion = None
        if prev_up is not None and len(prev_up) and len(up):
            today_boards = up.set_index("code")["boards"]
            common = prev_up.index.intersection(today_boards.index)
            if len(common):
                advanced = sum(1 for code in common
                               if today_boards[code] == prev_up[code] + 1)
                promotion = advanced / len(prev_up)

        rows.append({
            "trade_date": day,
            "limit_up": len(up),
            "broken": len(broken),
            "limit_down": len(down),
            "break_rate": break_rate,
            "promotion_rate": promotion,
            "max_boards": float(up["boards"].max()) if len(up) else 0.0,
        })
        prev_up = up.set_index("code")["boards"] if len(up) else None

    return pd.DataFrame(rows)


def _episodes(days: list[str]) -> int:
    """把连续的交易日切成事件段。相隔超过 `EPISODE_GAP_DAYS` 自然日算新的一段。"""
    if not days:
        return 0
    stamps = sorted(pd.to_datetime(days, format="%Y%m%d"))
    count, last = 1, stamps[0]
    for stamp in stamps[1:]:
        if (stamp - last).days > EPISODE_GAP_DAYS:
            count += 1
        last = stamp
    return count


def _streak(hist: pd.DataFrame, column: str, gate: float, below: bool) -> dict:
    """从最新一天往回数，连续多少天满足条件。

    "今天炸板率 41%"和"连续第 4 天过阈值"是两件事：前者是一次读数，后者是
    周期的位置。退潮第 1 天和第 4 天的应对完全不同，而 `pools` 只给前者。
    """
    if column not in hist.columns or hist.empty:
        return {"days": 0, "since": None}
    values = hist[column].tolist()
    days = hist["trade_date"].tolist()
    count = 0
    since = None
    for idx in range(len(values) - 1, -1, -1):
        value = values[idx]
        if value is None or pd.isna(value):
            break
        ok = (value < gate) if below else (value >= gate)
        if not ok:
            break
        count += 1
        since = days[idx]
    return {"days": count, "since": since}


def _surprise(peers: pd.DataFrame, today: pd.Series) -> dict:
    """今天在它自己所属的这一档里，偏强还是偏弱。

    绝对值会骗人：炸板率 41% 在退潮档里算温和，在弱修复档里就是崩了。
    所以把今天放回同类日里比较，给均值、中位数、以及**今天的百分位**。

    两个指标的方向相反，必须写出来：炸板率越低越好，晋级率越高越好。
    只给 percentile 不给方向，读的人会把 0.9 当成好消息。
    """
    out: dict = {"n_peers": len(peers)}
    for column, label, better_when in (
            ("break_rate", "炸板率", "lower"),
            ("promotion_rate", "晋级率", "higher")):
        value = today.get(column)
        series = peers[column].dropna() if column in peers.columns else pd.Series(dtype=float)
        if value is None or pd.isna(value) or series.empty:
            out[column] = None
            continue
        percentile = float((series < value).mean())
        out[column] = {
            "label": label,
            "today": round(float(value), 4),
            "peers_mean": round(float(series.mean()), 4),
            "peers_median": round(float(series.median()), 4),
            "percentile": round(percentile, 4),
            "better_when": better_when,
            "gap_vs_mean": round(float(value - series.mean()), 4),
            # `signed_percentile` 把方向折进符号：+0.24 = 比 62% 的同档日子好，
            # −0.24 = 比 62% 差。原来这里叫 `verdict`，写的是「好于 62% 的同档
            # 日子」——内容一样，但名字说这是一句判词，而它只是把 percentile
            # 和 better_when 两个已知量合起来算了一次。
            "signed_percentile": round(
                (percentile - 0.5) * 2 * (1 if better_when == "higher" else -1), 4),
        }
    return out


def transition(desk, day: str | None = None, *, recent: int = 750,
               horizon: tuple[int, ...] = (1, 3),
               series: int = 15) -> dict:
    """今天这档情绪，历史上之后 `horizon` 天的指标怎么走。

    `recent` 是回看的交易日数：默认 750（约三年）。16 年全历史也只要 0.1 秒，
    但越往前市场结构越不像今天，留三年是在样本量与可比性之间的取舍。
    """
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "没有可用交易日"}

    calendar = desk.market.calendar.last_n(day, recent + 1) or []
    if len(calendar) < 30:
        return {"ok": False, "as_of": day,
                "error": "trade_cal 覆盖不足，无法回看历史情绪"}
    start, end = calendar[0], calendar[-1]

    hist = daily_metrics(desk.data, start, end)
    if hist.empty:
        return {"ok": False, "as_of": day,
                "error": f"{start}~{end} 没有派生池；先跑日更的 derived_pools"}

    hist = hist.dropna(subset=["break_rate"]).reset_index(drop=True)
    today = hist[hist["trade_date"] == day]
    if today.empty:
        return {"ok": False, "as_of": day,
                "error": f"{day} 的派生池还没建（盘中或收盘后未日更）"}
    t = today.iloc[0]

    today_promo = t["promotion_rate"]
    # 晋级率缺失（昨日无涨停）时只按炸板率分档，并把这件事实写进 rule。
    if today_promo is None or pd.isna(today_promo):
        mask = hist["break_rate"] >= BREAK_GATE if t["break_rate"] >= BREAK_GATE \
            else hist["break_rate"] < BREAK_GATE
        rule = f"炸板率 {'>=' if t['break_rate'] >= BREAK_GATE else '<'} {BREAK_GATE}（晋级率缺失）"
    else:
        same_break = ((hist["break_rate"] >= BREAK_GATE)
                      == (t["break_rate"] >= BREAK_GATE))
        same_promo = ((hist["promotion_rate"] >= PROMOTION_GATE)
                      == (today_promo >= PROMOTION_GATE))
        mask = same_break & same_promo
        rule = (f"炸板率 {'>=' if t['break_rate'] >= BREAK_GATE else '<'} {BREAK_GATE}"
                f" 且 晋级率 {'>=' if today_promo >= PROMOTION_GATE else '<'} {PROMOTION_GATE}")

    peers = hist[mask].reset_index(drop=True)
    # 今天自己不算自己的历史样本。
    peers = peers[peers["trade_date"] != day]

    forward: dict[str, dict] = {}
    for step in horizon:
        rows = []
        for offset in range(len(peers)):
            pos = hist.index[hist["trade_date"] == peers.loc[offset, "trade_date"]][0]
            target = pos + step
            if target < len(hist):
                rows.append(hist.iloc[target])
        if not rows:
            forward[f"{step}d"] = {"n": 0}
            continue
        frame = pd.DataFrame(rows)
        break_rate = frame["break_rate"].dropna()
        promo = frame["promotion_rate"].dropna()
        forward[f"{step}d"] = {
            "n": len(frame),
            "break_rate_mean": (round(float(break_rate.mean()), 4)
                                if len(break_rate) else None),
            "break_rate_below_gate": (round(float((break_rate < BREAK_GATE).mean()), 4)
                                      if len(break_rate) else None),
            "promotion_rate_mean": (round(float(promo.mean()), 4)
                                    if len(promo) else None),
            "promotion_above_gate": (round(float((promo >= PROMOTION_GATE).mean()), 4)
                                     if len(promo) else None),
        }

    return {
        "ok": True,
        "as_of": day,
        "today": {
            "break_rate": round(float(t["break_rate"]), 4),
            "promotion_rate": (None if today_promo is None or pd.isna(today_promo)
                               else round(float(today_promo), 4)),
            "limit_up": int(t["limit_up"]),
            "broken": int(t["broken"]),
            "limit_down": int(t["limit_down"]),
            "max_boards": int(t["max_boards"]),
        },
        "cohort": {
            "rule": rule,
            "n_days": len(peers),
            # 样本不独立：连续退潮日会重复计数。这个数比 n_days 更接近真实证据量。
            "n_episodes": _episodes(peers["trade_date"].tolist()),
            "window": [start, end],
        },
        "forward": forward,
        # 预期差：今天在它自己那一档里的相对位置。绝对值会说谎——
        # 炸板率 41% 放在退潮档里算温和，放在弱修复档里就是崩了。
        "surprise": _surprise(peers, t),
        # 序列比单点有用：今天炸板率 41% 是一次读数，连续 4 天过阈值才是
        # 周期的位置。退潮第 1 天与第 4 天的应对完全不同。
        "recent": {
            "days": series,
            "rows": [
                {
                    "date": str(row["trade_date"]),
                    "break_rate": (round(float(row["break_rate"]), 4)
                                   if pd.notna(row["break_rate"]) else None),
                    "promotion_rate": (round(float(row["promotion_rate"]), 4)
                                       if pd.notna(row["promotion_rate"]) else None),
                    "limit_up": int(row["limit_up"]),
                    "limit_down": int(row["limit_down"]),
                    "max_boards": int(row["max_boards"]),
                }
                for _, row in hist.tail(series).iterrows()
            ],
            "break_rate_above_gate_streak": _streak(
                hist, "break_rate", BREAK_GATE, below=False),
            "promotion_below_gate_streak": _streak(
                hist, "promotion_rate", PROMOTION_GATE, below=True),
        },
        "caveats": [
            "这是条件分布，不是预测：它说的是历史上同类日子之后发生过什么。",
            ("样本不独立：n_days 里连续的日子会重复计入，n_episodes 才是证据量。"
             " 段数小于 8 时，连分布都谈不上。"),
            "分档只有两档（炸板率 0.38、晋级率 0.15），调阈值等于改「同类」的定义。",
            "含 2015-2016 的极端波动期，recent 参数可缩到更近的窗口。",
        ],
    }
