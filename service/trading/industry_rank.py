"""这只票在它自己行业里的位置。

短线判断"龙头"不能只看绝对涨幅：涨 5% 在全市场算强，但在半导体板块里可能
排在后半段。同样的涨幅，处在第一梯队和吊车尾是两种命运——而 `quote` 只给
绝对涨跌，`sector` 只给行业整体，中间这一层（**个股在行业内的相对位置**）
此前是空的。

数据全部本地：`index_member`（申万三级，5,922 行）+ `daily` / `rt_quote`。
收盘后走日线，盘中走实时快照，两条路都换算成同一单位（金额千元）。
"""

from __future__ import annotations

import pandas as pd


def _bars(desk, day: str) -> tuple[pd.DataFrame, str]:
    """当日行情，收盘走 daily、盘中走快照，与 `sector` 同口径。"""
    from service.trading.sector import _intraday_snapshot

    daily = desk.data.read("daily", day, day)
    if daily is not None and not daily.empty:
        keep = [c for c in ("ts_code", "pct_chg", "amount", "close")
                if c in daily.columns]
        return daily[keep].copy(), "daily:close"
    snap = _intraday_snapshot(desk, day)
    if snap.empty:
        return pd.DataFrame(), "none"
    keep = [c for c in ("ts_code", "pct_chg", "amount", "close")
            if c in snap.columns]
    return snap[keep].copy(), "intraday_snapshot"


def _rank(series: pd.Series, value: float, higher_is_better: bool = True) -> dict:
    """名次与百分位。并列时按「不优于」计，避免给出虚高的名次。"""
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return {"rank": None, "total": 0, "percentile": None}
    total = len(clean)
    if higher_is_better:
        better = int((clean > value).sum())
    else:
        better = int((clean < value).sum())
    return {
        "rank": better + 1,
        "total": total,
        "percentile": round(1 - better / total, 4),
    }


def _name_of(desk, code: str, day: str) -> str:
    """`sw_table()` 只返回分类字段（没有 name），名称要从 instrument 取。"""
    try:
        return desk.market.instrument(code, day).name or ""
    except Exception:  # noqa: BLE001 - 名字缺失不该让整个排名失败
        return ""


def in_industry(desk, symbol: str, day: str | None = None, *,
                level: str = "l2", top: int = 3) -> dict:
    """个股在申万 `level` 行业内的涨跌与成交排名，外加板块龙头。

    `level` 默认 L2：L1 太粗（42 家银行算一类），L3 常常只有 3-5 只
    （`index_member` 里最小的三级分类只有 2 只），排名没有意义。
    """
    from service.adapters.symbols import ts_code as _to_ts_code

    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "symbol": symbol, "error": "没有可用交易日"}

    text = str(symbol).strip().upper()
    ts = text if "." in text else _to_ts_code(text)

    sw = desk.market.sw_table()
    if sw is None or sw.empty or "ts_code" not in sw.columns:
        return {"ok": False, "symbol": ts, "error": "index_member 为空"}
    mine = sw[sw["ts_code"].astype(str) == ts]
    if mine.empty:
        return {"ok": False, "symbol": ts,
                "error": f"{ts} 不在申万分类里（可能已退市或北交所未收录）"}
    mine = mine.iloc[0]

    column = f"{level}_name"
    if column not in sw.columns:
        return {"ok": False, "symbol": ts, "error": f"未知层级 {level}"}
    industry = str(mine[column])
    members = set(sw[sw[column].astype(str) == industry]["ts_code"].astype(str))

    bars, source = _bars(desk, day)
    if bars.empty:
        return {"ok": False, "symbol": ts, "as_of": day,
                "error": f"{day} 既没有日线也没有快照"}
    bars["ts_code"] = bars["ts_code"].astype(str)
    peer = bars[bars["ts_code"].isin(members)].copy()
    peer["pct_chg"] = pd.to_numeric(peer.get("pct_chg"), errors="coerce")
    peer["amount"] = pd.to_numeric(peer.get("amount"), errors="coerce")
    own = peer[peer["ts_code"] == ts]
    if own.empty:
        return {"ok": False, "symbol": ts, "as_of": day,
                "error": f"{ts} 当日无行情（停牌？），无法与同行业比较"}

    pct = float(own.iloc[0]["pct_chg"]) if pd.notna(own.iloc[0]["pct_chg"]) else None
    amount = float(own.iloc[0]["amount"]) if pd.notna(own.iloc[0]["amount"]) else None

    rank_pct = _rank(peer["pct_chg"], pct) if pct is not None else {
        "rank": None, "total": len(peer), "percentile": None}
    rank_amount = _rank(peer["amount"], amount) if amount is not None else {
        "rank": None, "total": len(peer.dropna(subset=['amount'])),
        "percentile": None}

    median = peer["pct_chg"].median()
    leaders = (peer.dropna(subset=["pct_chg"])
                   .sort_values("pct_chg", ascending=False)
                   .head(top))
    leader_rows = []
    for _, row in leaders.iterrows():
        code = str(row["ts_code"])
        leader_rows.append({
            "symbol": code,
            "name": _name_of(desk, code, day),
            "pct_chg": round(float(row["pct_chg"]), 2),
        })

    return {
        "ok": True,
        "symbol": ts,
        "name": _name_of(desk, ts, day),
        "as_of": day,
        "price_source": source,
        "level": level,
        "industry": {
            "l1": str(mine.get("l1_name") or ""),
            "l2": str(mine.get("l2_name") or ""),
            "l3": str(mine.get("l3_name") or ""),
            "group": industry,
            "members": len(members),
            "priced": len(peer.dropna(subset=["pct_chg"])),
        },
        "pct_chg": None if pct is None else round(pct, 2),
        "industry_median_pct": (None if pd.isna(median) else round(float(median), 2)),
        # 超额：这只票相对同行业中位数的差。正数=跑赢同行
        "vs_industry_pp": (None if (pct is None or pd.isna(median))
                           else round(pct - float(median), 2)),
        "rank_pct": rank_pct,
        "rank_amount": rank_amount,
        "leaders": leader_rows,
        "caveats": [
            ("排名基于当日涨跌幅，不含此前的趋势——它回答「今天在同行里强不强」，"
             "不回答「它是不是这轮行情的龙头」。后者要看连板数与首封时间。"),
            "停牌股没有涨跌幅，已从分母剔除；`industry.priced` 是实际参与排名的只数。",
        ],
    }
