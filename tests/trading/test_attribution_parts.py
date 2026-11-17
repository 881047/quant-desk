"""一笔交易的钱从哪儿来：六项分解必须**精确**还原真实收益。

这条恒等式不依赖真实平仓记录——随便挑一个标的和一段持有期都能验。
所以验证样本不是"那一条"，而是历史上几百个持有窗口。
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading.attribution_parts import decompose, explain


def _case(**kw):
    base = {"entry_px": 100.0, "exit_px": 110.0, "entry_close": 100.0,
            "exit_close": 110.0, "entry_adj": 100.0, "exit_adj": 110.0,
            "market_ret": 0.05, "industry_ret": 0.04}
    base.update(kw)
    return base


@pytest.mark.parametrize("case", [
    _case(),
    # 买入高于当日收盘 1%（追高），随后市场涨 10%
    _case(entry_px=101.0, market_ret=0.10, industry_ret=0.10),
    # 期间分红 2%：前复权锚定末日，所以入场日复权价高于收盘价
    _case(entry_adj=102.0, market_ret=0.06, industry_ret=0.05),
    # 亏损单：持有期跌 15%，卖在收盘下方 1%
    _case(exit_px=93.5, exit_close=95.0, exit_adj=95.0,
          market_ret=-0.10, industry_ret=-0.12),
])
def test_the_six_parts_add_up_to_the_realised_return(case):
    out = decompose(**case)

    assert out["ok"] is True
    assert sum(out["parts"].values()) == pytest.approx(out["total"], abs=1e-12)
    assert out["residual"] == pytest.approx(0.0, abs=1e-15)


def test_each_part_answers_a_different_question():
    """同一个 +8.4%，拆开之后每一项都能对上号。"""
    out = decompose(**_case(entry_px=101.0, exit_px=109.45, exit_close=110.0,
                            entry_adj=102.0, market_ret=0.06, industry_ret=0.05))
    parts = out["parts"]

    assert parts["entry_timing"] < 0, "买贵了，这一项必须是负的"
    assert parts["market"] > 0.05, "市场涨 6%，作用在本金上应略高于 6%"
    assert parts["industry"] < 0, "行业跑输市场 1%，行业项为负"
    assert parts["idio"] > 0, "个股跑赢行业，异质项为正"
    assert parts["dividend"] > 0, "分红是进账"
    assert parts["exit_timing"] < 0, "卖在收盘下方，出场项为负"


def test_a_regressed_price_is_refused_not_fudged():
    out = decompose(**_case(entry_px=0.0))

    assert out["ok"] is False and "error" in out


def test_replays_hundreds_of_hold_periods_without_a_leak(desk):
    """历史重放：不是一条，是几百个 (标的, 入场日) 组合。

    用一个面板、一次读数据，把每个入场日 × 每只样本票都算一遍——恒等式对
    任何一对日期都要成立，包括跨除权除息的那些窗口。
    """
    day = desk.market.latest_bar_date()
    panel = desk.market.panel(day, 260)
    if panel is None or panel.empty:
        pytest.skip("没有面板")
    col = "adj_close" if "adj_close" in panel.columns else "close"
    px = panel.pivot_table(index="trade_date", columns="ts_code", values="close").sort_index()
    adj = panel.pivot_table(index="trade_date", columns="ts_code", values=col).sort_index()
    px.index = px.index.astype(str)
    adj.index = adj.index.astype(str)

    symbols = [c for c in adj.columns if c in px.columns][:4]
    assert symbols, "面板里没有可比对的行"

    checked = 0
    worst = 0.0
    last = adj.index[-1]
    for offset in range(1, 241):
        if offset >= len(adj.index):
            break
        entry_day = adj.index[-1 - offset]
        window = (adj.iloc[-1] / adj.loc[entry_day] - 1).dropna()
        market_ret = float(window.mean())
        for symbol in symbols:
            entry_close = px.loc[entry_day, symbol]
            exit_close = px.loc[last, symbol]
            entry_adj = adj.loc[entry_day, symbol]
            exit_adj = adj.loc[last, symbol]
            if not all(pd.notna(v) and v > 0 for v in
                       (entry_close, exit_close, entry_adj, exit_adj)):
                continue
            out = decompose(
                entry_px=float(entry_close) * 1.003,   # 模拟滑点
                exit_px=float(exit_close) * 0.998,
                entry_close=float(entry_close), exit_close=float(exit_close),
                entry_adj=float(entry_adj), exit_adj=float(exit_adj),
                market_ret=market_ret, industry_ret=market_ret,
            )
            assert out["ok"] is True
            worst = max(worst, abs(out["residual"]))
            checked += 1

    assert checked >= 600, f"样本只有 {checked} 个，不足以说明问题"
    assert worst < 1e-12, f"最大残差 {worst:.2e}"


def test_a_real_holding_through_an_ex_date_names_the_dividend(desk):
    """跨除权日的真实窗口：分红必须作为独立一项出现，而不是被算成"个股弱"。"""
    try:
        frame = desk.data.read("dividend")
    except (KeyError, FileNotFoundError, ValueError):
        pytest.skip("没有分红表")
    if frame is None or frame.empty or "ex_date" not in frame.columns:
        pytest.skip("分红表为空")
    done = frame[frame["div_proc"].astype(str) == "实施"].copy()
    done["ex_date"] = done["ex_date"].astype(str)
    day = desk.market.latest_bar_date()
    done = done[(done["ex_date"].str.len() == 8) & (done["ex_date"] < day)]
    if done.empty:
        pytest.skip("没有已实施的除权日")
    row = done.sort_values("ex_date").iloc[-1]
    symbol = str(row["ts_code"])
    hist = desk.market.history(symbol, day, 300)
    days = [str(d) for d in hist["trade_date"]]
    try:
        index = days.index(str(row["ex_date"]))
    except ValueError:
        pytest.skip("除权日不在窗口内")
    if index == 0:
        pytest.skip("除权日是最早一天，没有前置窗口")
    entry_day, exit_day = days[index - 1], days[min(index + 3, len(days) - 1)]
    entry = desk.market.snapshot(symbol, entry_day)
    exit_ = desk.market.snapshot(symbol, exit_day)
    if entry is None or exit_ is None:
        pytest.skip("缺行情")

    out = explain(desk.market, symbol, entry_day, exit_day,
                  float(entry.last), float(exit_.last))

    assert out["ok"] is True
    assert out["residual"] == pytest.approx(0.0, abs=1e-12)
    assert abs(out["parts"]["dividend"]) > 1e-6, "跨了除权日，分红项不该是 0"


def _synthetic_case(desk) -> dict:
    """从真实面板里造一笔"平仓记录"，不依赖账本里有没有交易。"""
    day = desk.market.latest_bar_date()
    panel = desk.market.panel(day, 40)
    px = panel.pivot_table(index="trade_date", columns="ts_code", values="close").sort_index()
    px.index = px.index.astype(str)
    symbol = str(px.columns[0])
    days = list(px.index)
    entry_day, exit_day = days[-4], days[-1]
    entry = float(px.loc[entry_day, symbol])
    exit_ = float(px.loc[exit_day, symbol])
    # 账本的净收益：毛收益再扣掉 30bps
    net = exit_ / entry - 1 - 0.003
    return {"case_id": "synthetic", "symbol": symbol,
            "opened_at": f"{entry_day[:4]}-{entry_day[4:6]}-{entry_day[6:]}T10:00:00+08:00",
            "closed_at": f"{exit_day[:4]}-{exit_day[4:6]}-{exit_day[6:]}T10:00:00+08:00",
            "entry": entry, "exit": exit_, "pnl_pct": net}


def test_the_parts_reconcile_with_the_number_the_book_records(desk):
    """账本记的是净收益，价格给的是毛收益——七项之和必须落到净收益上。

    只对毛收益成立是不够的：读者手上那个数是 `pnl_pct`，对不上就会认为分解是错的。
    所以费用必须作为第七项显式出现，而不是留一个 0.59% 的缺口让人自己猜。
    """
    from service.trading.attribution_parts import annotate

    case = _synthetic_case(desk)
    out = annotate(desk.market, [case])

    item = out["rows"][0]["attribution"]
    assert item is not None, out["rows"][0].get("why")
    assert set(item["parts"]) == {"entry_timing", "market", "industry", "idio",
                                  "dividend", "exit_timing", "cost"}
    assert sum(item["parts"].values()) == pytest.approx(item["total"], abs=1e-12)
    assert item["total"] == pytest.approx(case["pnl_pct"], abs=1e-12)
    assert item["residual"] == pytest.approx(0.0, abs=1e-12)
    # 反解出来的费用必须正好是毛与净之差
    assert item["parts"]["cost"] == pytest.approx(case["pnl_pct"] - item["gross"], abs=1e-12)


def test_one_closed_trade_is_not_a_statistic(desk):
    """样本不足时不给汇总——一笔交易的成分均值会读成"平均每次赚 4%"。

    这条规矩本身要能被违反地发现：`summary` 为 None 且 note 说明差多少笔。
    """
    from service.trading.attribution_parts import annotate

    out = annotate(desk.market, [_synthetic_case(desk)], min_for_summary=5)

    assert out["n"] == 1
    assert out["summary"] is None
    assert "不是统计量" in out["note"]


def test_core_etf_status_follows_the_core_universe_flag(monkeypatch):
    """核心盘关掉之后，归因表不能还说它在跑。

    2026-11-01 按账户规模关掉核心盘（一手 511010 要 1.4 万，账上 5 千），当时只改了
    configs/core.json，REGISTRY 里仍写着 live，复盘页就一直把这条从未成交的袖显示成
    「在跑」，而入擂台（/arena）早就把它摘掉了。
    """
    from service.trading import attribution

    monkeypatch.setattr(attribution, "core_retired", lambda: True)
    status, weight = attribution.strategy_status("core_etf")
    assert (status, weight) == ("retired", 0.0)

    monkeypatch.setattr(attribution, "core_retired", lambda: False)
    assert attribution.strategy_status("core_etf") == ("live", 0.80)

    # 别的策略不受这个开关影响
    assert attribution.strategy_status("llm") == ("live", 0.60)
