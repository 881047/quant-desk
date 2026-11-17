"""分段请求：在 16 KB 之内把每一块给全，而不是把所有块都切薄。

`profile` 全长 15.7 KB，离宿主上限只有两百多字节。任何一次"再加一个字段"都会
把它顶成 L2 降级（每块 6 行、字符串 120 字）——那时候读者拿到的不是"少一点"，
是**每一块都被截断**。分段把这件事变成"多调一次"，而不是"少拿到一半"。
"""

from __future__ import annotations

from service.trading.profile import BLOCK_ORDER, build


def test_asking_for_one_block_returns_exactly_that_block(desk):
    part = build(desk, "600519.SH", sections=["events"])

    assert part["ok"] is True
    present = [name for name in BLOCK_ORDER if name in part]
    assert present == ["events"]
    # 省略的要列出来，否则读者会把"没请求"读成"没有这一块"
    assert len(part["omitted_sections"]) == len(BLOCK_ORDER) - 1
    assert "events" not in part["omitted_sections"]


def test_a_block_is_identical_whether_or_not_it_was_asked_for_alone(desk):
    """分块不能改内容。同一块单独取和整套取，必须一字不差。"""
    full = build(desk, "600519.SH")
    part = build(desk, "600519.SH", sections=["events", "valuation"])

    assert part["events"] == full["events"]
    assert part["valuation"] == full["valuation"]


def test_splitting_saves_bytes_not_depth(desk):
    full = build(desk, "600519.SH")
    part = build(desk, "600519.SH", sections=["events"])
    import json

    assert len(json.dumps(part, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False)) / 2


def test_intraday_works_alone_even_though_it_needs_the_position_block(desk):
    """`intraday` 要用 `position` 的止损价，但单独请求时不该把 position 也塞回来。"""
    part = build(desk, "600519.SH", sections=["intraday"])

    assert part["ok"] is True
    assert "intraday" in part
    assert "position" not in part


def test_an_unknown_section_names_the_valid_ones(desk):
    out = build(desk, "600519.SH", sections=["event"])

    assert out["ok"] is False
    assert out["sections"] == list(BLOCK_ORDER)
    assert "event" in out["error"]


def test_readings_are_filtered_but_the_global_one_survives(desk):
    part = build(desk, "600519.SH", sections=["events"])
    lines = part["how_to_read"]

    assert any("没有一个结论" in line for line in lines), "总纲永远保留"
    assert not any(line.startswith("valuation") for line in lines), "没问的块不该带读法"


def test_fundamentals_deducted_restores_single_quarter_from_ytd(monkeypatch):
    import pandas as pd

    from service.trading import profile

    frame = pd.DataFrame({
        "ts_code": ["X.SZ"] * 3,
        "end_date": ["20260331", "20260630", "20260930"],
        "ann_date": ["20260429", "20260822", "20261031"],
        "profit_dedt": [1.0e8, 2.5e8, 6.4320764819e8],
        "extra_item": [0.1e8, 1.5e8, 4.5728489825e8],
        "roe": [1.0, 2.0, 3.0],
    })
    monkeypatch.setattr(profile, "_read_cached", lambda desk, ds, **kw: frame)
    out = profile._fundamentals(object(), "X.SZ", "20261110")
    d = out["deducted"]
    assert d["profit_dedt_ytd_cny"] == 6.4320764819e8
    assert abs(d["q_profit_dedt_cny"] - 3.9320764819e8) < 1.0
    assert d["q_basis"].startswith("本期 YTD")
    assert d["annualization_factor"] == 1.3333
    assert d["nonrecurring_share_pct"] is not None


def test_fundamentals_deducted_refuses_to_subtract_across_a_year_boundary(monkeypatch):
    import pandas as pd

    from service.trading import profile

    frame = pd.DataFrame({
        "ts_code": ["X.SZ", "X.SZ"],
        "end_date": ["20251231", "20260331"],
        "ann_date": ["20260331", "20260429"],
        "profit_dedt": [8.0e8, 1.0e8],
        "extra_item": [0.5e8, 0.1e8],
    })
    monkeypatch.setattr(profile, "_read_cached", lambda desk, ds, **kw: frame)
    out = profile._fundamentals(object(), "X.SZ", "20261110")
    d = out["deducted"]
    assert d["q_basis"] == "Q1 的 YTD 即单季，无需还原"
    assert d["q_profit_dedt_cny"] == 1.0e8


def test_a_section_plan_never_leaks_to_a_collected_desk():
    """The plan cache lives 60 s and decides which blocks are *dropped*.

    ``_plan_key`` used ``id(desk)`` as the desk's identity; CPython reuses the
    address as soon as the object is collected, and every test builds a new desk.
    Forty desks produced one id, so a case could inherit the previous case's
    plan — and with it, its omissions. That is a missing block, not a smaller
    answer.
    """
    from types import SimpleNamespace

    from service.trading import profile

    seen = []
    for _ in range(40):
        desk = SimpleNamespace()
        seen.append(profile._plan_key(desk, "600519.SH", "20261113", ("events",), 750)[0])
        del desk

    assert len(set(seen)) == len(seen), (
        f"{len(seen) - len(set(seen))} of {len(seen)} distinct desks shared a plan key")
