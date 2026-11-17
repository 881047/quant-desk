"""一笔交易的钱从哪儿来：市场 / 行业 / 个股 / 分红 / 两个时点 / 成本。

`arena` 现在只给一个 `excess`——"跑输 1.2%"。这句话没法指导行动：同样的 1.2%，
可能是选股选错了，可能是买在当天最高点，也可能是费用吃掉的。把一笔平仓拆成六项，
读者才知道该改哪一步。

分解是**顺序乘性**的，不是简单相减。原因是钱是按顺序滚的：你先付了一个高于当日
收盘的价，之后的市场涨跌作用在**你实际付的钱**上，而不是作用在"假如买在收盘"上。
所以每一项都要乘以前面所有项的累计因子，最后加总必须**精确还原**真实收益率：

    entry → market/industry/idio → dividend → exit

    r_total = x·k·d·e − 1
             = (x−1) + x(k−1) + xk(d−1) + xkd(e−1)

其中 x = 当日收盘/成交价（入场时点），k = 复权收盘比（持有期总收益），
d = 复权因子比（分红再投），e = 成交价/当日收盘（出场时点）。

`residual` 恒为 0——不是"应该很小"，是恒等式。这正是它可被任意样本检验的原因：
不需要真实平仓记录，随便挑一个标的和一段持有期就能验。
"""

from __future__ import annotations


def decompose(*, entry_px: float, exit_px: float,
              entry_close: float, exit_close: float,
              entry_adj: float, exit_adj: float,
              market_ret: float, industry_ret: float) -> dict:
    """Six components that sum to the realised return, exactly.

    ``market_ret`` / ``industry_ret`` are equal-weight returns over the same
    window. ``industry_ret`` must be on the same basis as ``market_ret`` so that
    ``industry - market`` is the sector's excess and ``stock - industry`` is the
    name's own move.
    """
    if min(entry_px, exit_px, entry_close, exit_close, entry_adj, exit_adj) <= 0:
        return {"ok": False, "error": "价格必须是正数",
                "got": {"entry_px": entry_px, "exit_px": exit_px}}

    total = exit_px / entry_px - 1
    # 持有期总收益（分红再投），与"收盘价之比"的差就是分红因子。
    k = exit_adj / entry_adj
    d = (entry_adj / entry_close) / (exit_adj / exit_close)
    x = entry_close / entry_px
    e = exit_px / exit_close

    r_cc = k - 1
    # 把 r_cc 拆成 市场 + (行业−市场) + (个股−行业)，三项相加恰好等于 r_cc。
    market = market_ret
    industry = industry_ret - market_ret
    idiosyncratic = r_cc - industry_ret

    parts: dict[str, float] = {}
    f = 1.0
    parts["entry_timing"] = f * (x - 1)
    f *= x
    parts["market"] = f * market
    parts["industry"] = f * industry
    parts["idio"] = f * idiosyncratic
    f *= k
    parts["dividend"] = f * (d - 1)
    f *= d
    parts["exit_timing"] = f * (e - 1)
    f *= e

    # 分红因子与复权因子互为倒数关系，浮点误差会让 f 与 1+total 差最后一位。
    # 这里不"修正"，而是把它报出来：residual 应当恒为 0，一旦不是，说明有人
    # 改了上面的顺序或漏了一项。
    return {
        "ok": True,
        "total": total,
        "parts": parts,
        "residual": total - (f - 1),
        "read": ("按顺序：入场时点 → 持有期（市场/行业/个股）→ 分红 → 出场时点。"
                 "每一项都乘以前面各项的累计因子，所以它们相加等于真实收益。"),
        "note": ("market / industry / idio 三项用的是等权基准，不是指数：这个台子不复制"
                 "指数，'随便买一只'的收益才是等权的含义。行业按申万一级、当前归属。"),
    }


def explain(market, symbol: str, entry_day: str, exit_day: str,
            entry_px: float, exit_px: float, *,
            market_ret: float | None = None,
            industry_ret: float | None = None) -> dict:
    """Data wrapper: fetch the closes/adjusteds and the two benchmarks.

    ``market_ret`` / ``industry_ret`` may be passed in when the caller already
    has a panel loaded (a replay over many symbols should read the panel once,
    not once per trade).
    """
    entry = market.snapshot(symbol, entry_day)
    exit_ = market.snapshot(symbol, exit_day)
    if entry is None or exit_ is None:
        return {"ok": False, "symbol": symbol,
                "error": f"缺少 {entry_day} 或 {exit_day} 的行情"}
    hist = market.history(symbol, exit_day, 300)
    if hist is None or hist.empty or "adj_close" not in hist.columns:
        return {"ok": False, "symbol": symbol, "error": "没有复权价，算不出持有期总收益"}
    by_day = hist.set_index(hist["trade_date"].astype(str))
    try:
        entry_adj = float(by_day.loc[entry_day, "adj_close"])
        exit_adj = float(by_day.loc[exit_day, "adj_close"])
    except KeyError:
        return {"ok": False, "symbol": symbol, "error": "持有期不在 300 日窗口内"}

    if market_ret is None or industry_ret is None:
        market_ret = market_ret if market_ret is not None else _benchmark(
            market, entry_day, exit_day, None)
        industry_ret = industry_ret if industry_ret is not None else _benchmark(
            market, entry_day, exit_day, market.sw_of(symbol).get("l1") or None)
    if market_ret is None or industry_ret is None:
        return {"ok": False, "symbol": symbol, "error": "基准算不出来（面板缺这一天）"}

    out = decompose(entry_px=entry_px, exit_px=exit_px,
                    entry_close=float(entry.last), exit_close=float(exit_.last),
                    entry_adj=entry_adj, exit_adj=exit_adj,
                    market_ret=market_ret, industry_ret=industry_ret)
    out.update({"symbol": symbol, "entry_day": entry_day, "exit_day": exit_day,
                "entry_px": entry_px, "exit_px": exit_px})
    return out


def _benchmark(market, entry_day: str, exit_day: str, l1: str | None) -> float | None:
    """等权收益：全员，或某申万一级的成员。"""
    hist = market.panel(exit_day, 260)
    if hist is None or hist.empty:
        return None
    col = "adj_close" if "adj_close" in hist.columns else "close"
    px = hist.pivot_table(index="trade_date", columns="ts_code", values=col,
                          aggfunc="last").sort_index()
    px.index = px.index.astype(str)
    if entry_day not in px.index:
        return None
    rets = (px.iloc[-1] / px.loc[entry_day] - 1).replace(
        [float("inf"), float("-inf")], None).dropna()
    if l1:
        table = market.sw_table()
        if table is not None and not table.empty and "l1_name" in table.columns:
            members = set(table[table["l1_name"].astype(str) == l1]["ts_code"].astype(str))
            rets = rets[[c for c in rets.index if str(c) in members]]
    if rets.empty:
        return None
    return float(rets.mean())


def _pivot(market, day: str) -> tuple:
    """一次读面板，所有窗口共用。逐笔去读会退化成 N 次 250 行 pivot。"""

    hist = market.panel(day, 260)
    if hist is None or hist.empty:
        return None, None
    col = "adj_close" if "adj_close" in hist.columns else "close"
    px = hist.pivot_table(index="trade_date", columns="ts_code", values="close",
                          aggfunc="last").sort_index()
    adj = hist.pivot_table(index="trade_date", columns="ts_code", values=col,
                           aggfunc="last").sort_index()
    px.index = px.index.astype(str)
    adj.index = adj.index.astype(str)
    return px, adj


def annotate(market, cases: list[dict], *, day: str | None = None,
             min_for_summary: int = 5) -> dict:
    """给每笔平仓补一个六项分解，样本够才给汇总。

    `min_for_summary` 是这条的一个硬规矩：一笔交易的成分均值不是统计量。
    样本不足时 `summary` 直接不出现，而不是给一个 n=1 的"平均"。
    """
    rows: list[dict] = []
    if not cases:
        return {"n": 0, "rows": [], "summary": None,
                "note": "还没有平仓记录，没有可分解的东西。"}
    px, adj = _pivot(market, day or market.latest_bar_date())
    if px is None or px.empty:
        return {"n": 0, "rows": [], "summary": None, "note": "面板为空，算不出基准"}

    table = market.sw_table()
    membership: dict[str, set[str]] = {}
    if table is not None and not table.empty and "l1_name" in table.columns:
        for name, group in table.groupby(table["l1_name"].astype(str)):
            membership[str(name)] = set(group["ts_code"].astype(str))

    for case in cases:
        symbol = str(case.get("symbol") or "")
        entry_day = str(case.get("opened_at") or "")[:10].replace("-", "")
        exit_day = str(case.get("closed_at") or "")[:10].replace("-", "")
        entry_px, exit_px = case.get("entry"), case.get("exit")
        if symbol not in adj.columns or entry_day not in adj.index or exit_day not in adj.index:
            rows.append({"case_id": case.get("case_id"), "symbol": symbol,
                         "attribution": None, "why": "窗口不在这 260 个交易日里"})
            continue
        try:
            entry_close = float(px.loc[entry_day, symbol])
            exit_close = float(px.loc[exit_day, symbol])
            entry_adj = float(adj.loc[entry_day, symbol])
            exit_adj = float(adj.loc[exit_day, symbol])
            entry_px = float(entry_px)
            exit_px = float(exit_px)
        except (TypeError, ValueError, KeyError):
            rows.append({"case_id": case.get("case_id"), "symbol": symbol,
                         "attribution": None, "why": "缺价格"})
            continue

        window = (adj.loc[exit_day] / adj.loc[entry_day] - 1).dropna()
        market_ret = float(window.mean()) if len(window) else None
        industry_ret = market_ret
        l1 = market.sw_of(symbol).get("l1") or ""
        members = membership.get(l1)
        if members:
            subset = window[[c for c in window.index if str(c) in members]]
            if len(subset):
                industry_ret = float(subset.mean())
        if market_ret is None:
            rows.append({"case_id": case.get("case_id"), "symbol": symbol,
                         "attribution": None, "why": "基准算不出来"})
            continue

        out = decompose(entry_px=entry_px, exit_px=exit_px,
                        entry_close=entry_close, exit_close=exit_close,
                        entry_adj=entry_adj, exit_adj=exit_adj,
                        market_ret=market_ret, industry_ret=industry_ret)
        out["l1"] = l1 or None
        out["held_days"] = _trading_days(adj, entry_day, exit_day)
        # 前六项加起来是**毛**收益（成交价之比），而账本记的是**净**收益（含费用）。
        # 这笔差额不是估算：账本自己给得出 net，价格给得出 gross，两者之差就是
        # 费用与滑点的合计。不把它单列，读者会拿毛收益去对 pnl_pct 然后对不上。
        net = case.get("pnl_pct")
        if isinstance(net, (int, float)):
            gross = out["total"]
            out["gross"] = gross
            out["parts"]["cost"] = float(net) - gross
            out["total"] = float(net)
            out["residual"] = out["total"] - sum(out["parts"].values())
        rows.append({"case_id": case.get("case_id"), "symbol": symbol,
                     "attribution": out})

    good = [row["attribution"] for row in rows if row["attribution"]]
    summary = None
    if len(good) >= min_for_summary:
        keys = ("entry_timing", "market", "industry", "idio", "dividend",
                "exit_timing", "cost")
        summary = {"n": len(good),
                   "mean_parts": {k: round(sum(g["parts"][k] for g in good) / len(good), 6)
                                  for k in keys},
                   "mean_total": round(sum(g["total"] for g in good) / len(good), 6),
                   "note": "各项均值相加等于均值总收益（分解是线性的）。"}
    return {
        "n": len(good),
        "rows": rows,
        "summary": summary,
        "note": (f"只有 {len(good)} 笔可分解的平仓，不到 {min_for_summary} 笔不给汇总——"
                 "一笔交易的成分均值不是统计量。"
                 if summary is None else
                 "各项是同一笔钱的不同来路，相加等于该笔的真实收益。"),
    }


def _trading_days(pivot, entry_day: str, exit_day: str) -> int | None:
    try:
        lo = list(pivot.index).index(entry_day)
        hi = list(pivot.index).index(exit_day)
        return max(0, hi - lo)
    except ValueError:
        return None
