"""两只（或三只）票并排：同一份档案、同一个窗口、同一个基准。

"对比"最容易做错的地方不是取数，是**两边算的不是同一件事**：
估值分位一个用 750 日另一个用 250 日、财报一个 Q2 一个 Q3、行业强弱一个比 L3 另一个比 L1。
分开调两个 `profile` 再在脑子里 diff 时，这些全都看不出来。

所以这里的实现方式只有一个：**对每只票跑同一个 `profile.build`，用同一个
`valuation_window`、同一天、同一批区块**，然后把字段对齐成一行一行。两边的口径
不是"保证一致"，是**同一段代码算出来的**。

两个显式守卫：

* **报告期不同就不许比财务**。600519 出到 Q2、000858 出到 Q3 时，`or_yoy` 的差
  是"季度不同"而不是"公司不同"，所以那一组 `comparable=false` 并写明原因。
* **行业不同要标出来**。`vs_industry_pp` 是各自对自己行业的强弱——两个不同行业的
  数并排是有意义的，但读者必须知道它们不是同一个参照物。
"""

from __future__ import annotations

from . import profile

#: 对齐出来的指标。顺序就是阅读顺序：先事实，再估值，再相对强弱，最后财务。
#: 每项是 (指标名, 单位, 取值函数)。取值函数拿到一份 profile 的 blocks 字典。
METRICS = (
    ("close", "CNY", lambda b: b["tradability"].get("last")),
    ("change_pct", "%", lambda b: b["tradability"].get("change_pct")),
    ("min_ticket_cny", "CNY", lambda b: b["tradability"].get("min_ticket_cny")),
    ("pe_ttm", "x", lambda b: (b["valuation"].get("current") or {}).get("pe_ttm")),
    ("pe_ttm_pctile", "0-1", lambda b: (b["valuation"].get("percentile_in_window") or {}).get("pe_ttm")),
    ("pb", "x", lambda b: (b["valuation"].get("current") or {}).get("pb")),
    ("pb_pctile", "0-1", lambda b: (b["valuation"].get("percentile_in_window") or {}).get("pb")),
    ("dv_ttm_pct", "%", lambda b: (b["valuation"].get("current") or {}).get("dividend_yield_ttm_pct")),
    ("ret_20d", "%", lambda b: _win(b, "20d", "stock_pct")),
    ("vs_industry_pp_20d", "pp", lambda b: _win(b, "20d", "vs_industry_pp")),
    ("vs_market_pp_20d", "pp", lambda b: _win(b, "20d", "vs_market_pp")),
    ("main_net_5d_cny", "CNY", lambda b: ((b["flow"].get("main_net_cny") or {}).get("5d"))),
    ("price_vs_ma20_pct", "%", lambda b: ((b["technicals"].get("ma") or {}).get("ma20") or {}).get("price_vs_pct")),
    ("atr20", "CNY", lambda b: b["technicals"].get("atr20")),
    ("vol_20d_annual_pct", "%", lambda b: (b["technicals"].get("volatility_annual_pct") or {}).get("20d")),
    ("range_pos_250d", "0-1", lambda b: (b["technicals"].get("range_250d") or {}).get("position")),
    ("from_250d_high_pct", "%", lambda b: (b["technicals"].get("range_250d") or {}).get("from_high_pct")),
)

#: 只在报告期一致时才有意义的指标。
FINANCIAL = (
    ("or_yoy", "%", "or_yoy"),
    ("netprofit_yoy", "%", "netprofit_yoy"),
    ("roe_waa", "%", "roe_waa"),
)

SECTIONS = ("identity", "tradability", "valuation", "relative_strength",
            "fundamentals", "flow", "technicals")


def _win(blocks, horizon: str, key: str):
    windows = (blocks["relative_strength"].get("window") or {})
    return (windows.get(horizon) or {}).get(key)


def build(desk, symbols, day: str | None = None, valuation_window: int = 750) -> dict:
    picked = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if len(picked) < 2:
        return {"ok": False, "error": "至少两个标的才能比较",
                "example": {"symbols": ["600519.SH", "000858.SZ"]}}
    if len(picked) > 3:
        # 三个以上会让"并排"退化成表格，而表格在 16 KB 里放不下四列以上。
        return {"ok": False, "error": "最多三个标的", "got": picked[:5]}

    blocks_by: dict[str, dict] = {}
    missing: list[str] = []
    for symbol in picked:
        out = profile.build(desk, symbol, day, valuation_window=valuation_window,
                            sections=list(SECTIONS))
        if not out.get("ok"):
            missing.append(symbol)
            continue
        blocks_by[symbol] = out
    if len(blocks_by) < 2:
        return {"ok": False, "error": "可比对的标的不足两个", "missing": missing}

    symbols = list(blocks_by)
    day = day or next(iter(blocks_by.values()))["as_of"]

    rows = []
    for name, unit, getter in METRICS:
        rows.append(_row(name, unit, symbols, blocks_by, getter))

    # 报告期守卫：不同季度的财务指标不能并排比。
    periods = {symbol: (blocks_by[symbol]["fundamentals"].get("latest_period") or "-")
               for symbol in symbols}
    period_match = len(set(periods.values())) == 1
    for name, unit, key in FINANCIAL:
        row = _row(name, unit, symbols, blocks_by,
                   lambda b, k=key: (b["fundamentals"].get("metrics") or {}).get(k),
                   comparable=period_match)
        if not period_match:
            row["why"] = (f"报告期不同（{', '.join(f'{k}={v}' for k, v in periods.items())}），"
                          "这一行比的是不同季度。")
        rows.append(row)

    # 行业不同：`vs_industry_pp` 各自对自己行业，参照物不是同一个。
    industries = {symbol: (blocks_by[symbol]["identity"].get("sw") or {}).get("l3")
                  or blocks_by[symbol]["identity"].get("industry") or "-"
                  for symbol in symbols}

    return {
        "ok": True,
        "as_of": day,
        "symbols": symbols,
        "missing": missing,
        "valuation_window": valuation_window,
        "industries": industries,
        "report_period": periods,
        "period_match": period_match,
        "rows": rows,
        # 只在真有 null 时出现：一句常驻的免责声明会被读成套话。
        **({"null_note": ("null_for 里的标的在那一期没有这个数：`fina_indicator` 本身为空"
                          "（部分银行不披露 roe_waa，部分公司不披露毛利率），"
                          "不是 0，也不是这家公司没有这个指标。")}
           if any("null_for" in row for row in rows) else {}),
        "basis": ("每只票跑同一个 profile.build（同 valuation_window、同一天、同一批区块），"
                  "所以口径不是「对齐」出来的，是同一段代码算出来的。"
                  "缺值写 null，不补 0。"),
        "note": ("`vs_industry_pp` 是各自对自己行业的强弱，两个不同行业的数并排有意义，"
                 "但参照物不是同一个；`pe_ttm_pctile` 是各票在自己窗口内的位置，"
                 "窗口相同才可比。报告期不同时财务三项标 comparable=false。"),
    }


def _row(name: str, unit: str, symbols: list[str], blocks: dict,
         getter, *, comparable: bool = True) -> dict:
    values = {}
    for symbol in symbols:
        try:
            value = getter(blocks[symbol])
        except (KeyError, TypeError):
            value = None
        values[symbol] = round(value, 4) if isinstance(value, (int, float)) else None
    numbers = [v for v in values.values() if isinstance(v, (int, float))]
    spread = round(max(numbers) - min(numbers), 4) if len(numbers) >= 2 else None
    row = {"metric": name, "unit": unit, "values": values,
           "spread": spread, "comparable": comparable and len(numbers) >= 2}
    # 哪一边没有数，要说出来。一行两列一空一有时 `comparable` 会变 false，
    # 但读者看不到原因，只能猜"这家公司没有这个指标"——实测 000001 与 300750
    # 的 2026Q3 `roe_waa` 在 fina_indicator 里就是空的，而 600519 有。
    nulls = [symbol for symbol, value in values.items() if value is None]
    if nulls:
        row["null_for"] = nulls
    return row
