"""The account panel, disambiguated by one identity instead of by guesswork."""

from __future__ import annotations

from service.trading import ths_account
from service.trading.ths_account import disambiguate

# 实测自 xiadan.exe：A股与港股两套面板叠在同一棵窗口树里，
# 同一个标签出现两次、值不同（港股那套全是 0）。
TWO_PANELS = [
    ("总 资 产", 133, 259), ("4813.37", 214, 259),
    ("总资产", 525, 294), ("4813.37", 591, 294),
    ("总资产", 133, 299), ("4813.37", 214, 299),
    ("可用金额", 133, 199), ("3173.37", 214, 199),
    ("可用金额", 348, 294), ("3173.37", 414, 294),
    ("可用金额", 133, 219), ("1882.02", 214, 219),
    ("股票市值", 133, 239), ("1638.00", 214, 239),
    ("股票市值", 348, 270), ("1638.00", 414, 270),
    ("股票市值", 133, 219), ("0.00", 214, 219),
]


def test_identity_picks_the_a_share_panel():
    solved, reason = disambiguate(TWO_PANELS)
    assert reason == ""
    assert solved["total"] == 4813.37
    assert solved["cash"] == 3173.37
    assert solved["market_value"] == 1638.0
    assert solved["residual"] == 2.0, "可用 + 市值 差 2 元，属于利息/在途，应当容忍"
    assert solved["cash_ratio"] == 0.6593


def test_zero_panel_is_not_selected():
    """港股那套全是 0：1882.02 + 0 ≠ 4813.37，必须被排除。"""
    solved, _ = disambiguate(TWO_PANELS)
    assert solved["market_value"] != 0.0


def test_inconsistent_panel_refuses_instead_of_rounding():
    """读不出自洽的组合时什么都不返回 —— 错的总额会让下游每个百分比都偏。"""
    broken = [
        ("总 资 产", 133, 259), ("4813.37", 214, 259),
        ("可用金额", 133, 199), ("900.00", 214, 199),
        ("股票市值", 133, 239), ("100.00", 214, 239),
    ]
    solved, reason = disambiguate(broken)
    assert solved is None
    assert "恒等式不成立" in reason


def test_missing_field_says_which_one():
    items = [("总 资 产", 133, 259), ("4813.37", 214, 259)]
    solved, reason = disambiguate(items)
    assert solved is None
    assert "缺字段" in reason


def test_missing_field_counts_include_zero():
    items = [
        ("可用金额", 133, 199), ("100.00", 214, 199),
        ("股票市值", 133, 239), ("100.00", 214, 239),
    ]
    solved, reason = disambiguate(items)
    assert solved is None
    assert "总资产0" in reason
    assert "可用1" in reason and "市值1" in reason


def test_non_finite_panel_numbers_are_not_accepted():
    items = [
        ("总 资 产", 133, 259), ("1e309", 214, 259),
        ("可用金额", 133, 199), ("100.00", 214, 199),
        ("股票市值", 133, 239), ("100.00", 214, 239),
    ]
    solved, reason = disambiguate(items)
    assert solved is None
    assert "缺字段" in reason


def test_partial_account_payload_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(ths_account, "find_windows", lambda: [123])
    monkeypatch.setattr(ths_account, "_statics", lambda _top: [("资金", 0, 0)])
    monkeypatch.setattr(ths_account, "disambiguate", lambda _items: ({"total": 100.0}, ""))

    result = ths_account.read_account()
    assert result["ok"] is False
    assert "缺输出字段" in result["reason"]
    assert "cash" in result["reason"]


# ------------------------------------------------------- 读不到时的每一句话
#
# `read_account()` 的失败分支产出的 `reason` 是人在客户端没开、停在登录框、
# 或者面板正在刷新时唯一会看到的东西。这些句子如果写成"读取失败"，值班的人
# 就得从零开始查；写成"没找到窗口（客户端没开或停在登录框）"，他下一步做什么
# 是确定的。所以每条失败路径都单独钉住，而不是让覆盖率凑到就算。

def test_no_window_says_the_client_is_probably_not_open(monkeypatch):
    """找不到窗口和窗口里没数字是两件事，修法也不同。"""
    monkeypatch.setattr(ths_account, "find_windows", list)

    out = ths_account.read_account()

    assert out["ok"] is False
    assert "没找到" in out["reason"]
    assert "登录框" in out["reason"], "要说出最常见的那种情况，而不只是'失败'"


def test_a_window_without_the_cash_panel_is_skipped_not_reported(monkeypatch):
    """窗口存在但里面没有 Label/Static：跳过它，继续看下一个窗口。

    这台机器上可能同时开着 A股 和 港股 两个客户端窗口。
    """
    monkeypatch.setattr(ths_account, "find_windows", lambda: [111, 222])
    calls = []

    def fake_statics(top):
        calls.append(top)
        return [] if top == 111 else [
            ("总 资 产", 0, 0), ("1000.00", 60, 0),
            ("可用金额", 0, 20), ("400.00", 60, 20),
            ("股票市值", 0, 40), ("600.00", 60, 40),
        ]

    monkeypatch.setattr(ths_account, "_statics", fake_statics)

    out = ths_account.read_account()

    assert calls == [111, 222], "第一个窗口要试过才知道是空的"
    assert out["ok"] is True
    assert out["hwnd"] == 222
    assert out["total"] == 1000.0


def test_each_window_that_fails_leaves_its_own_reason(monkeypatch):
    """两个窗口都不成立时，理由要**都**带上，不能只留最后一句。

    只有一个句子的报错会让人以为另一个窗口没问题。
    """
    monkeypatch.setattr(ths_account, "find_windows", lambda: [111, 222])
    monkeypatch.setattr(ths_account, "_statics", lambda _top: [("资金", 0, 0)])
    monkeypatch.setattr(ths_account, "disambiguate",
                        lambda _items: (None, "恒等式不成立"))

    out = ths_account.read_account()

    assert out["ok"] is False
    assert out["reason"].count("hwnd=") == 2, "两个窗口各留一句"


def test_a_window_that_yields_nothing_readable_says_so(monkeypatch):
    """所有窗口都被跳过（连资金面板都没找到）时的兜底句子。"""
    monkeypatch.setattr(ths_account, "find_windows", lambda: [111])
    monkeypatch.setattr(ths_account, "_statics", lambda _top: [])

    out = ths_account.read_account()

    assert out["ok"] is False
    assert "没有可用的资金面板" in out["reason"]


def test_a_value_that_is_not_a_number_is_dropped_not_zeroed():
    """标签右边贴着"停牌"之类的文字时，这个数不能当 0 用。

    0 会让恒等式 `总资产 = 可用 + 市值` 恰好成立在某些组合上，于是给出一个
    看起来自洽、实际缺一项的账户。
    """
    items = [
        # 带数字但转不成 float：不能被当成 0，也不能被当成有效值
        ("可用金额", 0, 0), ("待更新2", 60, 0),
        ("股票市值", 0, 20), ("100.00", 60, 20),
    ]

    assert ths_account._candidates(items, ths_account.LABEL_CASH) == []
    assert ths_account._candidates(items, ths_account.LABEL_MARKET) == [100.0]


def test_a_zero_total_is_refused_with_the_number_in_the_reason():
    """总资产算成 0 时，任何 `x / 总资产` 都会炸或被兜成 0。

    单独给一句带数字的理由，比让下游除零更容易追。
    """
    items = [
        ("总 资 产", 0, 0), ("0.00", 60, 0),
        ("可用金额", 0, 20), ("0.00", 60, 20),
        ("股票市值", 0, 40), ("0.00", 60, 40),
    ]

    solved, reason = ths_account.disambiguate(items)

    assert solved is None
    assert reason == "总资产为 0.0", reason


def test_non_numeric_output_fields_are_not_reported_as_success(monkeypatch):
    """恒等式成立但字段不是数字时，仍然要判失败。

    这条防的是"把能算出来的形状当成能用的数"：`total` 是字符串 "1000"
    算术上能对上，但下游 `float()` 之外任何运算都会炸。
    """
    monkeypatch.setattr(ths_account, "find_windows", lambda: [123])
    monkeypatch.setattr(ths_account, "_statics", lambda _top: [("资金", 0, 0)])
    monkeypatch.setattr(ths_account, "disambiguate", lambda _items: ({
        "total": "一千", "cash": 400.0, "market_value": 600.0,
        "residual": 0.0, "cash_ratio": 0.4,
    }, ""))

    out = ths_account.read_account()

    assert out["ok"] is False
    assert "不是有限数字" in out["reason"]
