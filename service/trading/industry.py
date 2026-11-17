"""申万一级行业的横截面：钱在往哪儿走，而不是"哪个行业涨了"。

`/discover` 里原来有一块 `industries`：东财行业标签、5 日涨跌幅、top5/bottom5。
它回答的是"哪几个行业涨了"，而交易时真正要问的是三件不同的事：

    * 这个行业的**成员**在涨，还是只有一两只大票把它拉起来？→ 宽度
    * 它是**普涨**，还是几只涨停把均值撑住？→ 涨停家数
    * 涨的同时**钱在进还是出**？→ 主力净流入

三件事分开之后，"半导体 -13.6%" 和 "半导体 -13.6%、42 只里 3 只上涨、主力净流出 38 亿"
是两个不同的判断，而后者只能由这一层算出来：它要把日线、申万成员、涨停价、资金流
四张表按成员对齐。

口径固定三件事，免得读者自己去猜：

    * **等权**，不是市值加权——这个台子不做指数，等权才是"随便买一只"的收益；
    * **申万一级**，与 `quant_profile` 的 L1 同一个来源（东财标签有 104 个桶，
      `银行` 一个桶装 42 只，和申万的银行不是同一批）；
    * **rates 是小数**（0.021 = 2.1%），与 `ret_1d` 系列一致。
"""

from __future__ import annotations

import pandas as pd

#: The window the ranking uses. 5 sessions is what a rotation question means in
#: practice; 1 day is noise and 20 days is a story about last month.
RANK_WINDOW = 5


def _f(value, ndigits: int = 4):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _returns(wide: pd.DataFrame) -> dict[int, pd.Series]:
    """Equal-weight trailing returns per name, from the adjusted panel."""
    out: dict[int, pd.Series] = {}
    if wide.empty or len(wide) < 2:
        return out
    last = wide.iloc[-1]
    for n in (1, 5, 20):
        if len(wide) <= n:
            continue
        out[n] = (last / wide.iloc[-1 - n] - 1).replace(
            [float("inf"), float("-inf")], pd.NA)
    return out


def panel(market, day: str, window: int = 20) -> dict:
    """One row per 申万一级 industry, ranked by ``RANK_WINDOW``-session return."""
    daily = market.daily(day)
    if daily.empty:
        return {"as_of": day, "rows": [], "note": "没有当日日线"}

    table = market.sw_table()
    if table is None or table.empty or "l1_name" not in table.columns:
        return {"as_of": day, "rows": [],
                "note": "没有申万分类表（index_member），行业横截面算不出来"}

    frame = daily.copy()
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["close"] = pd.to_numeric(frame.get("close"), errors="coerce")
    frame["pre_close"] = pd.to_numeric(frame.get("pre_close"), errors="coerce")
    # `amount` arrives in 千元 (Tushare); the rest of the desk speaks CNY.
    frame["amount_cny"] = pd.to_numeric(frame.get("amount"), errors="coerce") * 1000

    wide = market.panel(day, max(window, 21) + 1)
    if wide is None or wide.empty:
        return {"as_of": day, "rows": [], "note": "面板为空，算不出多窗口收益"}
    col = "adj_close" if "adj_close" in wide.columns else "close"
    pivot = wide.pivot_table(index="trade_date", columns="ts_code",
                             values=col, aggfunc="last").sort_index()
    rets = _returns(pivot)
    ma20 = pivot.tail(20).mean()
    above = (pivot.iloc[-1] > ma20) if len(pivot) >= 20 else pd.Series(dtype=bool)

    frame = frame.set_index("ts_code")
    for n, series in rets.items():
        frame[f"ret_{n}d"] = series.reindex(frame.index)
    frame["above_ma20"] = above.reindex(frame.index)

    limits = market.limits(day)
    if limits is not None and not limits.empty:
        up = pd.to_numeric(limits.set_index("ts_code")["up_limit"], errors="coerce")
        down = pd.to_numeric(limits.set_index("ts_code")["down_limit"], errors="coerce")
        frame["up"] = up.reindex(frame.index)
        frame["down"] = down.reindex(frame.index)
        frame["at_up"] = frame["close"] >= frame["up"] * 0.999
        frame["at_down"] = frame["close"] <= frame["down"] * 1.001
    else:
        frame["at_up"] = False
        frame["at_down"] = False

    # Money flow is per name and in 万元; industry net is the sum over members.
    try:
        flow = market.store.read("moneyflow", day, day, columns=["ts_code", "net_mf_amount"])
    except (KeyError, FileNotFoundError, ValueError):
        flow = None
    if flow is not None and not flow.empty:
        net = pd.to_numeric(flow.set_index("ts_code")["net_mf_amount"], errors="coerce") * 1e4
        frame["net_mf_cny"] = net.reindex(frame.index)
    else:
        frame["net_mf_cny"] = float("nan")

    mapping = table.drop_duplicates("ts_code").set_index("ts_code")["l1_name"].astype(str)
    frame["l1"] = frame.index.map(mapping)
    unmapped = int(frame["l1"].isna().sum())
    frame = frame[frame["l1"].notna() & (frame["l1"] != "")]

    total_amount = float(frame["amount_cny"].sum())
    rows = []
    for name, group in frame.groupby("l1"):
        advancers = int((group["ret_1d"] > 0).sum()) if "ret_1d" in group else 0
        decliners = int((group["ret_1d"] < 0).sum()) if "ret_1d" in group else 0
        rows.append({
            "l1": name,
            "members": len(group),
            "ret_1d": _f(group["ret_1d"].mean()) if "ret_1d" in group else None,
            "ret_5d": _f(group["ret_5d"].mean()) if "ret_5d" in group else None,
            "ret_20d": _f(group["ret_20d"].mean()) if "ret_20d" in group else None,
            "advancers": advancers,
            "decliners": decliners,
            "flat": int(len(group) - advancers - decliners),
            "above_ma20": _f(group["above_ma20"].mean(), 3),
            "limit_up": int(group["at_up"].sum()),
            "limit_down": int(group["at_down"].sum()),
            "amount_cny": _f(group["amount_cny"].sum(), 0),
            "amount_share": (_f(group["amount_cny"].sum() / total_amount, 3)
                             if total_amount > 0 else None),
            "net_mf_cny": (None if group["net_mf_cny"].isna().all()
                           else _f(group["net_mf_cny"].sum(), 0)),
        })

    key = f"ret_{RANK_WINDOW}d"
    rankable = [row for row in rows if row.get(key) is not None]
    rankable.sort(key=lambda row: row[key], reverse=True)
    for index, row in enumerate(rankable, start=1):
        row["rank"] = index
    # Industries with no forward window still belong in the table; they go last
    # with an explicit null rank rather than a fabricated 0.
    rest = [row for row in rows if row.get(key) is None]
    for row in rest:
        row["rank"] = None
    rows = rankable + rest

    spread = (None if len(rankable) < 2
              else _f(rankable[0][key] - rankable[-1][key]))
    return {
        "as_of": day,
        "rows": rows,
        "industries": len(rows),
        "members_covered": len(frame),
        "unmapped": unmapped,
        "rank_by": key,
        "spread_top_minus_bottom": spread,
        "basis": ("申万一级（index_member.l1_name）；行业收益是成员等权平均；"
                  "rates 是小数（0.021 = 2.1%）；net_mf_cny 是主力净流入（超大单+大单）"),
        "note": ("宽度（advancers/decliners/above_ma20）和涨停家数说明这个行业是普涨还是"
                 "被几只票拉起来；net_mf_cny 说明钱是在进还是在出。三者要一起读——"
                 "只有涨幅的行业名次会把'一枝独秀'读成'板块启动'。"
                 f"未映射到申万一级的标的 {unmapped} 只，不计入任何行业。"
                 "全量 31 个一级行业都返回，不按 limit 截断——截断会把最弱的几个行业"
                 "切掉，而轮动问题里最弱的那一端和最强的一端一样重要。"),
    }
