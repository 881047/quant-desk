"""申万一级行业榜：哪条线有钱，用自己库里的数回答。

2026-11-02 的实盘审计里，12:41 那句"传媒 +6.69% / +29.99 亿、非银 +3.02% / +27.94 亿"
全部来自外部数据商——因为内核没有行业榜。板块层面的"哪条线有资金"这个判断，
当时只能靠检索，而检索来的数没有口径、没有 `as_of`、也无法复核。

这里用的全是本地已入库的三张表：

* `index_member` —— 申万（2021）三级分类，5,922 行，一票一行
* `daily` / `daily_basic` —— 当日涨跌幅、成交额、流通市值（千元 / 千元）
* `moneyflow` —— 大单与超大单的买卖额（**万元**），T+1 口径

## 两个涨跌幅，不是一个

市值加权（`pct_chg`）像指数，等权（`pct_chg_eq`）像"这个板块里随便挑一只能拿到什么"。
两者在题材股扎堆的行业里能差好几个点，只给一个数就是把口径藏起来。

## 资金流必须标日期

`moneyflow` 是收盘后发布的，盘中拿不到当日。所以每行带 `flow_as_of`，
没有当日数据的行 `main_net_inflow_cny` 为 `null` 并且 `flow_missing` 计数——
"没有资金流"和"资金流是零"是两件事，而它们在这一列上长得一样。
"""

from __future__ import annotations

import pandas as pd

#: tushare `daily.amount` / `daily_basic.circ_mv` 的单位是千元。上游是万元，这里统一成元。
QIAN_TO_CNY = 1_000.0
WAN_TO_CNY = 10_000.0


def _industry_frame(desk, day: str) -> tuple[pd.DataFrame, bool]:
    """One row per stock with its 申万 L1, price move, turnover and float cap.

    第二个返回值是 ``intraday``：True 表示这份数来自盘中快照而不是收盘日线。
    调用方必须把它写进响应——同一张表在两段时段的可信度不一样。

    收盘后走 `daily` + `daily_basic`；盘中这两张表当天还没有，改走 `rt_quote`
    的最新快照，并**换算成与 daily 相同的千元口径**。不换算的后果是板块成交额
    在盘中比收盘后大 1000 倍，而两边的字段名一模一样，谁也看不出来。
    """
    sw = desk.market.sw_table()
    if sw.empty or "l1_name" not in sw.columns:
        return pd.DataFrame(), False
    daily = desk.data.read("daily", day, day)
    intraday = False
    if daily is None or daily.empty:
        daily = _intraday_snapshot(desk, day)
        intraday = True
    if daily is None or daily.empty:
        return pd.DataFrame(), intraday
    keep = [c for c in ("ts_code", "pct_chg", "amount", "close", "circ_mv")
            if c in daily.columns]
    frame = daily[keep].copy()
    if "circ_mv" not in frame.columns:
        basic = desk.data.read("daily_basic", day, day)
        if basic is not None and not basic.empty and "circ_mv" in basic.columns:
            frame = frame.merge(basic[["ts_code", "circ_mv"]], on="ts_code", how="left")
    frame = frame.merge(sw[["ts_code", "l1_name"]].dropna(), on="ts_code", how="inner")
    for col in ("pct_chg", "amount", "circ_mv"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame, intraday


def _intraday_snapshot(desk, day: str) -> pd.DataFrame:
    """今日实时快照，换算成 `daily` / `daily_basic` 的千元口径。

    2026-11-03 之前这个函数不存在，于是盘中 `/market/sector` 只能返回上一交易日
    的榜——短线问"现在哪条线强"，答案永远是昨天的。
    """
    from service.adapters.symbols import ts_code as _to_ts_code

    try:
        snap = desk.data.read("rt_quote", day, day)
    except (KeyError, FileNotFoundError, ValueError):
        return pd.DataFrame()
    if snap is None or snap.empty:
        return pd.DataFrame()
    latest = snap.sort_values("snapshot_ts").groupby("code").tail(1)

    def col(name: str) -> pd.Series:
        if name not in latest.columns:
            return pd.Series([None] * len(latest), index=latest.index)
        return pd.to_numeric(latest[name], errors="coerce")

    return pd.DataFrame({
        "ts_code": [_to_ts_code(str(code)) for code in latest["code"]],
        "pct_chg": col("pct_chg"),
        # 元 → 千元：daily.amount 与 daily_basic.circ_mv 都是千元
        "amount": col("amount") / QIAN_TO_CNY,
        "close": col("last"),
        "circ_mv": col("float_mv") / QIAN_TO_CNY,
    })


def _flow_by_industry(desk, day: str, l1_of: pd.DataFrame) -> tuple[pd.Series, str | None]:
    """Main-force net inflow per 申万 L1, in CNY, plus the date it is actually from.

    主力 = 超大单 + 大单的净额。**这是机构口径**：它按单笔金额分类，不含营业部席位，
    所以不能用它判断游资行为——同一天的涨停家数与封单/成交比才是那条线的事。
    """
    try:
        flow = desk.data.read("moneyflow", day, day)
    except (KeyError, FileNotFoundError, ValueError):
        return pd.Series(dtype=float), None
    if flow is None or flow.empty:
        # 当日还没发布（盘中或收盘后未入库）：往前找最近一天，并如实报日期。
        earlier = desk.data.read("moneyflow", _previous_day(desk, day), day)
        if earlier is None or earlier.empty:
            return pd.Series(dtype=float), None
        flow, day = earlier, str(earlier["trade_date"].max())
    need = [c for c in ("buy_elg_amount", "buy_lg_amount",
                        "sell_elg_amount", "sell_lg_amount") if c in flow.columns]
    if len(need) < 4:
        return pd.Series(dtype=float), day
    frame = flow[["ts_code", *need]].copy()
    for col in need:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    frame["main_cny"] = ((frame["buy_elg_amount"] + frame["buy_lg_amount"])
                         - (frame["sell_elg_amount"] + frame["sell_lg_amount"])) * WAN_TO_CNY
    merged = frame.merge(l1_of, on="ts_code", how="inner")
    return merged.groupby("l1_name")["main_cny"].sum(), day


def _previous_day(desk, day: str) -> str:
    prior = desk.market.calendar.last_n(day, 2) or []
    return prior[-2] if len(prior) >= 2 else day


def sectors(desk, day: str | None = None, *, limit: int = 31) -> dict:
    """31 个申万一级行业，按当日涨跌幅降序。"""
    # 默认取**已入库**的最后一个交易日，不是日历上的今天：16:00 调用时今天还没有日线，
    # 报错不如给最近一天的榜并标明 as_of。要今天就必须显式传日期，那时再报"还没入库"。
    day = day or desk.market.latest_bar_date()
    frame, intraday = _industry_frame(desk, day)
    if frame.empty:
        latest = desk.market.latest_bar_date() or ""
        # "今天还没入库"和"这一天从来就没有数据"是两件事，报错要说清是哪一件：
        # 16:03 写一份"收盘后 17:30 才有"比让模型以为表缺了要有用得多。
        if latest and str(day) > str(latest):
            why = (f"{day} 的日线还没入库（日更 17:30 才写）；"
                   f"最近一个已入库交易日是 {latest}，先看这一天")
        else:
            why = (f"{day} 没有申万分类或行情：需要 index_member 与 daily 都覆盖这一天，"
                   f"先跑 scripts/backfill.py --datasets index_member daily")
        return {"ok": False, "as_of": day, "latest_bar": latest or None, "error": why}

    frame["weight"] = frame["circ_mv"].fillna(0.0)
    frame["amount_cny"] = frame["amount"].fillna(0.0) * QIAN_TO_CNY
    rows = []
    for name, part in frame.groupby("l1_name"):
        # 停牌股没有涨跌幅。把它算进分母会把这一行的涨幅稀释掉，而它当天根本
        # 没交易——权重只在**有价的那部分**上归一。
        traded = part.dropna(subset=["pct_chg"])
        weight = traded["weight"].sum()
        cap_weighted = (((traded["pct_chg"] * traded["weight"]).sum() / weight)
                        if weight > 0 else None)
        pct = traded["pct_chg"]
        rows.append({
            "sw_l1": str(name),
            "members": len(part),
            "traded": len(traded),
            # 市值加权像指数；等权像"随手买一只"。两个都给，不让读者猜口径。
            "pct_chg": None if cap_weighted is None else round(float(cap_weighted), 2),
            "pct_chg_eq": None if pct.notna().sum() == 0 else round(float(pct.mean()), 2),
            "amount_cny": round(float(part["amount_cny"].sum()), 0),
        })

    l1_of = frame[["ts_code", "l1_name"]].drop_duplicates()
    flows, flow_day = _flow_by_industry(desk, day, l1_of)
    missing = 0
    for row in rows:
        value = flows.get(row["sw_l1"]) if len(flows) else None
        if value is None or pd.isna(value):
            row["main_net_inflow_cny"] = None
            row["flow_as_of"] = None
            missing += 1
        else:
            row["main_net_inflow_cny"] = round(float(value), 0)
            row["flow_as_of"] = flow_day

    rows.sort(key=lambda r: (r["pct_chg"] is not None, r["pct_chg"] or 0), reverse=True)
    up = sum(1 for r in rows if (r["pct_chg"] or 0) > 0)
    return {
        "ok": True,
        "as_of": day,
        "industries": rows[:limit],
        "count": len(rows),
        "breadth": {"up": up, "down": len(rows) - up},
        "flow_as_of": flow_day,
        "flow_missing": missing,
        "price_source": "intraday_snapshot" if intraday else "daily:close",
        "basis": ("涨跌幅按流通市值加权（pct_chg），另有等权（pct_chg_eq）；"
                  "成交额单位元；资金流单位元；"
                  + ("行情来自盘中快照（rt_quote 最新一帧），未收盘、会继续变"
                     if intraday else "行情来自收盘日线")),
        "note": ("资金流是**机构口径**：超大单 + 大单净额，按单笔金额分类，"
                 "**不含营业部席位**，不能用来判断游资行为——那条线看连板高度、"
                 "首封时间、封单/成交比（quant_pools / quant_limit_structure）。"
                 "moneyflow 收盘后发布，盘中读到的是上一交易日的数，"
                 "每行 flow_as_of 写明它来自哪一天。"),
    }
