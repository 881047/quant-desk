"""账户面板与账本的一致性：只报告"对不上"，不裁定谁对。"""

from __future__ import annotations

import pytest

from service.trading.account_check import compare, is_empty_book_drift

PANEL = {"ok": True, "total": 4813.37, "cash": 3173.37, "market_value": 1638.0}


def test_matching_book_is_quiet():
    result = compare(panel=PANEL, book_value=1638.0, positions=1, quotes_fresh=True)
    assert result["verdict"] == "match"
    assert result["diff"] == 0.0
    assert "一致" in result["message"]


def test_small_difference_is_tolerance_not_noise():
    """2% 或 30 元取大者：价格时点差不会被当成"你忘了记"。"""
    result = compare(panel=PANEL, book_value=1620.0, positions=1, quotes_fresh=True)
    assert result["verdict"] == "match", "差 18 元在 32.76 元的容差内"


def test_bigger_account_value_means_a_missing_buy():
    result = compare(panel=PANEL, book_value=1000.0, positions=1, quotes_fresh=True)
    assert result["verdict"] == "book_missing"
    assert result["diff"] == 638.0
    assert "可能有买入" in result["message"]


def test_bigger_book_value_means_a_missing_sell():
    result = compare(panel=PANEL, book_value=2600.0, positions=1, quotes_fresh=True)
    assert result["verdict"] == "book_extra"
    assert result["diff"] == -962.0
    assert "可能有卖出" in result["message"]


def test_closed_client_is_not_an_alert():
    """晚上客户端关着是常态，不该每天报一次警。"""
    result = compare(panel={"ok": False, "reason": "没找到窗口"},
                     book_value=1638.0, positions=1, quotes_fresh=True)
    assert result["verdict"] == "unavailable"
    assert result["ok"] is False


def test_non_live_quotes_are_not_compared():
    """行情不是实时的时候，比较等于两个时点相减。"""
    result = compare(panel=PANEL, book_value=1638.0, positions=1, quotes_fresh=False)
    assert result["verdict"] == "stale_quotes"


def test_empty_book_with_a_funded_account_is_drift():
    """账本没有持仓、账户却有市值：把清仓那笔当成了全部。"""
    result = compare(panel=PANEL, book_value=0.0, positions=0, quotes_fresh=True)
    assert result["verdict"] == "book_missing"
    assert is_empty_book_drift(result) is True


def test_empty_book_and_empty_account_is_not_drift():
    result = compare(panel={"ok": True, "total": 0.0, "cash": 0.0, "market_value": 0.0},
                     book_value=0.0, positions=0, quotes_fresh=True)
    assert result["verdict"] == "match"
    assert is_empty_book_drift(result) is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "bad", None])
def test_invalid_book_numbers_never_raise(bad):
    result = compare(panel=PANEL, book_value=bad, positions=1, quotes_fresh=True)
    assert result["ok"] is False and result["verdict"] == "invalid_input"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "bad"])
def test_invalid_panel_numbers_never_raise(bad):
    panel = {**PANEL, "market_value": bad}
    result = compare(panel=panel, book_value=1000.0, positions=1, quotes_fresh=True)
    assert result["ok"] is False and result["verdict"] == "invalid_input"


def test_numeric_strings_do_not_break_message_formatting():
    result = compare(panel=PANEL, book_value="1000", positions=1, quotes_fresh=True)
    assert result["verdict"] == "book_missing"
    assert "1,000.00" in result["message"]
