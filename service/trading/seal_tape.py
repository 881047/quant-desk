"""封单轨迹：涨停板的封单在盘中怎么变。

为什么需要这一层
----------------
`limit_up_pool` 的主键是 ``(trade_date, code)``，盘中每抓一次就整份覆盖一次，
所以一天只留下**最后一帧**。而"封单从 8 万手掉到 2 万手"这种信号恰恰只存在于
被覆盖掉的那些帧里——它是炸板之前最直接的证据，却在收盘时已经不存在了。

`pool_tape` 把同一批股票按 ``(snapshot_ts, code)`` 逐帧留存，于是两个问题才有
答案：

1. **这一只的封单在减少还是增加**（个股维度）
2. **全场涨停家数在第几帧见顶**（市场维度）

第二条比第一条更要紧。收盘时的"涨停 48 家"是**结局**，而盘中可能是 10:30 见
顶 53 家、14:00 掉到 44 家、尾盘回封到 48 家——同样的收盘数字，过程完全相反。
只看结局会把"尾盘勉强回封"读成"全天强势"。

口径
----
``bid1_volume`` 是**手**（交易所口径），``bid1_value`` 是它换算成的元：
涨停时等于封单额，炸板时是买一承接金额。两者含义不同，所以 ``pool`` 字段必须
一起读，不能把炸板股的买一金额当成封单。

已知边界
--------
这一层从 2026-11-03 起才有数据（当天开始落盘）。早于这一天的日期会返回
``ok=False`` 并说明原因，而不是给一份空轨迹冒充"没有变化"。
"""

from __future__ import annotations

from datetime import datetime
from math import isnan

import pandas as pd

from service.kernel.clock import SHANGHAI

# 封单变化超过这个比例才提一句。阈值取得宽，是因为封单本身有正常的撤挂：
# 一档 5% 的波动在 48 只票里每天都会出现好几次，那不是信号。
DECAY_ALERT = -0.30
BUILD_ALERT = 0.30

# 一帧只有一只票时算不出趋势，但"只有一帧"和"没有数据"是两件事。
MIN_FRAMES = 2


def _f(value, digits: int = 2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if isnan(number):  # NaN 是"这个源没给"，不是 0
        return None
    return round(number, digits)


def _in_session(stamps: pd.Series) -> pd.Series:
    """哪些帧落在交易时段内（09:15–15:00）。

    封单只在有人在挂单时有意义。收盘后再抓的帧仍然有用——它记下了收工那一刻
    的封单额，是明天的基线——但它**不是一次盘中观察**，不能拿来算"封单在减少
    还是增加"。两次收盘快照的差值恒为零，排出来会像"全部平稳"，那是把"没有
    交易"读成"没有变化"。
    """
    local = pd.to_datetime(stamps, errors="coerce", utc=True).dt.tz_convert(SHANGHAI)
    minute_of_day = local.dt.hour * 60 + local.dt.minute
    return minute_of_day.between(9 * 60 + 15, 15 * 60)


#: 收盘集合竞价从 14:57 开始（沪深两市都是）。这段里连续竞价停了，盘口买一被
#: 清空/重置，`bid1_value` 不再是封单。
CLOSING_AUCTION_MINUTE = 14 * 60 + 57


def _comparable(stamps: pd.Series) -> pd.Series:
    """哪些帧的 `bid1_value` 还能和别的帧相比（09:15–14:57）。

    这条是 2026-11-04 用当天真实分帧发现的，不是想出来的：`seal_tape` 原来把
    14:57–15:00 的帧也算进"封单在减少还是增加"，于是 141 只涨停股里 131 只
    （93%）触发"衰减 ≥30%"告警，衰减榜前几名全是 −100%。

    查一只就清楚了——000096 在 14:56:28 封单还是 4.24 亿，14:57:10 变成 0，
    接着 86 手。一只仍封着的票不可能在 42 秒里把 4 亿封单吃干净，那是收盘集合
    竞价把盘口清了。全市场中位数从 5,570 万掉到 119 万，98.3% 的票在 14:57
    那一刻"跌破三成"——一个每天对几乎所有股票都成立的告警，等于没有告警。

    和收盘后的帧一样处理：留着（它是收盘那一刻的读数），但不参与趋势。
    """
    local = pd.to_datetime(stamps, errors="coerce", utc=True).dt.tz_convert(SHANGHAI)
    minute_of_day = local.dt.hour * 60 + local.dt.minute
    return minute_of_day.between(9 * 60 + 15, CLOSING_AUCTION_MINUTE - 1)


def _frames(desk, day: str) -> pd.DataFrame | None:
    try:
        frame = desk.data.read("pool_tape", day, day)
    except (KeyError, ValueError, OSError):
        return None
    if frame is None or frame.empty:
        return None
    frame = frame.copy()
    frame["snapshot_ts"] = frame["snapshot_ts"].astype(str)
    frame["bid1_value"] = pd.to_numeric(frame["bid1_value"], errors="coerce")
    frame["bid1_volume"] = pd.to_numeric(frame["bid1_volume"], errors="coerce")
    frame["_in_session"] = _in_session(frame["snapshot_ts"])
    frame["_comparable"] = _comparable(frame["snapshot_ts"])
    return frame.sort_values(["code", "snapshot_ts"])


def _market_curve(frame: pd.DataFrame) -> list[dict]:
    """每帧的涨停/炸板/跌停家数。收盘那个数是结局，这条才是过程。"""
    counts = (frame.groupby(["snapshot_ts", "pool"]).size()
              .unstack(fill_value=0))
    rows: list[dict] = []
    for stamp, row in counts.iterrows():
        rows.append({
            "ts": stamp,
            "limit_up": int(row.get("limit_up", 0)),
            "broken_board": int(row.get("broken_board", 0)),
            "limit_down": int(row.get("limit_down", 0)),
        })
    return rows


def _stock_series(frame: pd.DataFrame, pool: str,
                  stamps: list[str]) -> list[dict]:
    """个股的封单起止与极值。只取同一种 pool，避免拿炸板后的买一金额冒充封单。

    ``seen_ratio`` 是这一只出现在该池里的帧数占全部帧数的比例。它在 1 和 0 之间
    时说明这只票**中途进池或出池**——涨停股中途出去就是炸板，这个信息比封单
    减少更早、更硬。与 ``pool_tape`` 的分帧一起才有意义，单帧数据下恒为 1。
    """
    subset = frame[frame["pool"] == pool]
    total = max(len(stamps), 1)
    ending = stamps[-1] if stamps else None
    out: list[dict] = []
    for code, group in subset.groupby("code"):
        group = group.dropna(subset=["bid1_value"])
        if group.empty:
            continue
        first = group.iloc[0]
        last = group.iloc[-1]
        start = _f(first["bid1_value"])
        end = _f(last["bid1_value"])
        change = None
        # 只在这一帧确实观察过两次时才算变化。单帧的"起止差"恒为 0，把它排进
        # 榜单会读成"封单纹丝没动"，而真相是"没有观察过第二次"——同一只票在
        # 别的票有多帧时尤其容易混进去（它中途炸板，只留下了早期那几帧）。
        observed_twice = str(first["snapshot_ts"]) != str(last["snapshot_ts"])
        if observed_twice and start and start > 0 and end is not None:
            change = (end - start) / start
        peak = _f(group["bid1_value"].max())
        # 「相对自己的峰值还剩多少」是全天可比的读数：一只票无论几点封板，它的
        # 峰值都是一个有意义的基准；而"第一帧"在 135/141 的票上根本不是基准
        # （那是封板那一瞬间，见下面 change_pct 的注释）。
        from_peak = None
        # 和 change_pct 同一道约束：只观察过一次就没有"回落"可言。一只 10:00 封板、
        # 14:00 炸板的票在 limit_up 池里只留下一帧，峰值等于末值，算出 0% 会让它
        # 以"封单没动"的身份混进榜单——而它其实早就炸了。
        if observed_twice and peak and peak > 0 and end is not None:
            from_peak = (end - peak) / peak
        out.append({
            "code": code,
            "name": str(first.get("name") or ""),
            "boards": _f(first.get("boards"), 1),
            "frames": len(group),
            "seen_ratio": round(len(group) / total, 2),
            "first_value": start,
            "last_value": end,
            "min_value": _f(group["bid1_value"].min()),
            "max_value": peak,
            "from_peak_pct": None if from_peak is None else round(from_peak * 100, 1),
            "change_pct": None if change is None else round(change * 100, 1),
            # 最后一帧仍在这个池里 = 到收工时状态没变。
            "at_last_frame": str(last["snapshot_ts"]) == ending,
            "first_ts": str(first["snapshot_ts"]),
            "last_ts": str(last["snapshot_ts"]),
        })
    return out


def tape(desk, day: str | None = None, *, top: int = 10,
         offset: int = 0, limit: int = 20) -> dict:
    """今天的封单轨迹。``top`` 控制各榜单、告警与进出池明细的返回条数。

    ``curve`` 按时间正序分页，``offset`` 从 0 开始；峰值、回吐、个股榜单和
    告警仍基于整天全量计算。默认只给一页，避免宿主裁掉早盘曲线。
    """
    if day is None:
        # 默认给「最近一个有交易的日子」，和 knowledge 的其它 topic 一致：周六问
        # 「昨天的封单轨迹」应该拿到周五的，而不是一句"今天没有分帧数据"。盘中
        # 仍然用今天——那时今天的日线还没入库，latest_bar_date 会指向前一天。
        try:
            day = desk.market.last_trading_day()
        except Exception:  # noqa: BLE001 - a desk double may not carry a calendar
            day = None
        day = day or datetime.now(SHANGHAI).strftime("%Y%m%d")
    frame = _frames(desk, day)
    if frame is None:
        return {
            "ok": False,
            "as_of": day,
            "error": f"{day} 没有分帧盘口数据",
            "hint": ("`pool_tape` 从 2026-11-03 起才落盘（每个快照周期一帧）；"
                     "早于这一天的日期没有轨迹可算，不是当天没有涨停。"),
        }

    all_stamps = sorted(frame["snapshot_ts"].unique())
    # 趋势只在**可比**的帧之间算：收盘后的帧（两次快照差值恒为零）和收盘集合
    # 竞价期（盘口被清空）都要排除，理由见 `_comparable`。它们仍留在 `curve`
    # 里，因为"家数"不受盘口清零影响，那是收盘那一刻的真实结构。
    session = frame[frame["_in_session"]]
    comparable = session[session["_comparable"]]
    stamps = sorted(comparable["snapshot_ts"].unique())
    session_stamps = sorted(session["snapshot_ts"].unique())
    auction_stamps = [s for s in session_stamps if s not in set(stamps)]
    enough = len(stamps) >= MIN_FRAMES
    base = comparable if enough else frame
    horizon = stamps if enough else all_stamps

    curve = _market_curve(frame)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 200))
    curve_total = len(curve)
    curve_page = curve[offset:offset + limit]
    curve_next = offset + len(curve_page)
    next_offset = curve_next if curve_next < curve_total else None
    curve_pagination = {
        "offset": offset,
        "limit": limit,
        "returned": len(curve_page),
        "total": curve_total,
        "next_offset": next_offset,
        "remaining": max(curve_total - curve_next, 0),
        "truncated": offset > 0 or next_offset is not None,
        "order": "snapshot_ts_ascending",
    }
    sealed = _stock_series(base, "limit_up", horizon)
    broken = _stock_series(base, "broken_board", horizon)

    caveats: list[str] = []
    if not enough:
        caveats.append(
            f"盘中只有 {len(stamps)} 帧，算不出变化趋势——封单的起止差值在这种样本下"
            "只是两次抓取的随机误差，不是趋势。")
    post_close = len(all_stamps) - len(session_stamps)
    if auction_stamps:
        caveats.append(
            f"另有 {len(auction_stamps)} 帧落在 14:57 之后的收盘集合竞价里：那段"
            "连续竞价已停、盘口买一被清空，封单看起来'掉到最后'是撮合机制，"
            "不是有人在撤单。它们不参与趋势计算。")
    if post_close:
        caveats.append(
            f"另有 {post_close} 帧是收盘后抓的，只用来记录收工时的封单额，"
            "不参与趋势计算。")
    tracked = frame["code"].nunique()
    caveats.append(
        f"覆盖 {tracked} 只（三个池的并集），不是全市场。东财批量行情不带五档，"
        "腾讯一次只给约 50 个代码，所以只对池内个股补盘口。")

    # 家数曲线的形状：见顶在第几帧、之后回吐多少。收盘数相同、形状可以完全相反。
    # 一帧的时候这两件事都没有定义——峰值就是终点，回吐恒为 0，那不是"没有
    # 回吐"，是"没有过程可看"。宁可给 None，也不给一个读起来像好消息的 0。
    # 峰值与回吐都只看盘中帧：把收盘那一帧算进"收盘相对峰值的回吐"，等于把
    # 收盘后不再变化当成"止跌"。
    session_curve = _market_curve(session) if enough else []
    peak = max(session_curve, key=lambda row: row["limit_up"]) if session_curve else None
    erosion = None
    if peak is not None and peak["limit_up"]:
        closing = session_curve[-1]["limit_up"]
        erosion = round((closing - peak["limit_up"]) / peak["limit_up"] * 100, 1)

    # 榜单按 `from_peak_pct` 排，不按 `change_pct`：后者拿"第一帧"当基准，
    # 而 2026-11-04 实测 141 只涨停股里只有 6 只从第一帧就在池里，其余 135 只的
    # 首帧是封板那一瞬——002821 首帧 17.9 万、下一帧 5.2 亿，"增长"算出来是
    # +131,135%（1,316 倍）。那不是信号，是封板瞬间被当成了起点。
    ranked = [row for row in sealed if row["from_peak_pct"] is not None] if enough else []
    decay = sorted(ranked, key=lambda row: row["from_peak_pct"])[:top]
    # "封单在增加"只对**全天都在池里**的票成立（seen_ratio=1）：中途封板的票
    # 首帧是封板瞬间，任何首末比较都不可信。样本会很小，这是老实话。
    # 还要 change_pct > 0：全天在池、但一路被吃的票不该出现在"增强榜"里排第一
    # （它只是跌得最少）。名额不足时给空列表，比给一个名字叫"增加"的递减序列诚实。
    build = sorted(
        [row for row in sealed
         if row["change_pct"] is not None and row["change_pct"] > 0
         and row["seen_ratio"] >= 1.0],
        key=lambda row: row["change_pct"], reverse=True)[:top]

    alerts = [
        {"code": row["code"], "name": row["name"],
         "from_peak_pct": row["from_peak_pct"], "change_pct": row["change_pct"],
         "boards": row["boards"]}
        for row in ranked
        if row["from_peak_pct"] / 100.0 <= DECAY_ALERT
    ]
    # 一个每天对大多数股票都成立的告警等于没有告警。把发生率摆在榜单旁边，
    # 读者才知道 100 条告警是"今天全场都在掉封单"还是"真有 100 只异常"。
    decay_base_rate = round(len(alerts) / len(ranked), 3) if ranked else None
    alert_total = len(alerts)
    alerts = alerts[:top]
    left_early = [
        {"code": row["code"], "name": row["name"],
         "boards": row["boards"], "last_ts": row["last_ts"],
         "seen_ratio": row["seen_ratio"]}
        for row in sealed if not row["at_last_frame"]
    ]
    sealed_late = [
        {"code": row["code"], "name": row["name"],
         "boards": row["boards"], "first_ts": row["first_ts"]}
        for row in sealed
        if row["at_last_frame"] and stamps and row["first_ts"] != stamps[0]
    ]

    return {
        "ok": True,
        "as_of": day,
        "data_as_of": day,
        "time_basis": "pool_tape.snapshot_ts",
        "pit_safe": True,
        "pit_scope": "only the requested partition; no future frame enters full-day summaries",
        "frames": len(stamps),
        "frames_total": len(all_stamps),
        "frames_closing_auction": len(auction_stamps),
        "window": ({"first": stamps[0], "last": stamps[-1]} if stamps
                   else ({"first": all_stamps[0], "last": all_stamps[-1]}
                         if all_stamps else None)),
        "tracked": int(tracked),
        "market": {
            "curve": curve_page,
            "pagination": curve_pagination,
            "peak": peak if enough else None,
            "erosion_pct": erosion,
            "enough_frames": enough,
            "note": ("curve / peak 是**盘中逐帧**的池家数（rt_snapshots，末帧约 14:59，"
                     "仍不是终值）；收盘家数以 quant_facts.pools 为准（派生 "
                     "close == up_limit），两者会差 1–3 只——2026-11-13 末帧 37/21/38，"
                     "派生收盘 38/24/39。erosion_pct 是末帧相对当日峰值的回吐比例："
                     "峰 53 掉到 48 和峰 44 回到 48 是两回事。"),
            "curve_note": ("curve 是本页；pagination.next_offset 续读下一页。"
                           "peak / erosion_pct / alerts 始终按全量帧计算，不受分页影响；"
                           "alert 与进出池明细只给前 top 条，total/truncated 标明全量。"),
        },
        "sealed": {
            "count": len(sealed),
            "decay_top": decay,
            "build_top": build,
            # 收工时不在池里的，就是盘中炸掉的那些——它们不会出现在收盘的
            # `limit_up_pool` 里，但在轨迹里留了痕。
            "left_early": left_early[:top],
            "left_early_total": len(left_early),
            "left_early_truncated": len(left_early) > top,
            # 最后一帧才进池的：尾盘偷袭封板，持续性通常最差。
            "sealed_late": sealed_late[:top],
            "sealed_late_total": len(sealed_late),
            "sealed_late_truncated": len(sealed_late) > top,
        },
        "broken": {
            "count": len(broken),
            # 炸板股的买一金额不是封单，只给"掉出去时还剩多少承接"这一个读数。
            "residual_bid": sorted(
                [row for row in broken if row["last_value"] is not None],
                key=lambda row: row["last_value"], reverse=True)[:top],
        },
        "alerts": alerts,
        "alerts_total": alert_total,
        "alerts_truncated": alert_total > len(alerts),
        "alerts_note": "按 from_peak_pct 最严重的前 top 条；total 与 base_rate 仍按全量计算",
        "decay_base_rate": decay_base_rate,
        "thresholds": {"decay": DECAY_ALERT, "build": BUILD_ALERT},
        "caveats": caveats,
    }
