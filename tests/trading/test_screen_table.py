"""屏幕读取：真实的 OCR 片段如何变成一行持仓。

夹具是 2026-10-27 12:5x 从 xiadan.exe 的持仓表上实测到的 token（坐标是 2× 放大后的
像素），不是手写的。OCR 会丢掉小数点（9.035 → 9 + 035）、丢掉负号、并把名字两个字的
顺序读反（古汉医药 → 古汉药医），所以这里测的是"重建"而不是"识别"。
"""

from __future__ import annotations

from scripts.read_ths_screen import Token, build_rows, interpret

# 实测：10 个表头单元格被拆成单字，两行数据各自被拆成若干片段。
OCR = [
    (18, 17, '操'),
    (56, 18, '作'),
    (128, 18, '证'),
    (167, 18, '券'),
    (204, 17, '代'),
    (242, 19, '码'),
    (312, 18, '证'),
    (351, 18, '券'),
    (389, 17, '名'),
    (426, 18, '称'),
    (496, 19, '股'),
    (535, 19, '票'),
    (573, 17, '余'),
    (610, 18, '额'),
    (308, 95, '古'),
    (347, 95, '汉'),
    (387, 97, '医'),
    (423, 96, '药'),
    (308, 179, '模'),
    (347, 179, '塑'),
    (384, 179, '科'),
    (422, 179, '技'),
    (700, 18, '实'),
    (740, 19, '际'),
    (776, 18, '数'),
    (814, 19, '量'),
    (904, 19, '可'),
    (943, 19, '用'),
    (981, 17, '余'),
    (1018, 18, '额'),
    (1091, 17, '冻'),
    (1128, 16, '结'),
    (1166, 18, '数'),
    (1204, 19, '量'),
    (127, 99, '000590'),
    (127, 183, '000700'),
    (587, 99, '200'),
    (589, 183, '100'),
    (809, 99, '200'),
    (853, 183, '0'),
    (995, 99, '200'),
    (1039, 183, '0'),
    (1225, 99, '0'),
    (1183, 183, '100'),
    (1286, 17, '成'),
    (1325, 16, '本'),
    (1362, 17, '亻'),
    (1372, 17, '介'),
    (1318, 99, '9'),
    (1340, 124, '，'),
    (1349, 99, '035'),
    (1317, 183, '0'),
    (1349, 183, '000'),
    (1474, 16, '市'),
    (1512, 17, '价'),
    (1486, 99, '8'),
    (1508, 124, '，'),
    (1519, 99, '190'),
    (1465, 183, '1'),
    (1486, 183, '3'),
    (1518, 183, '860'),
    (1668, 19, '盈'),
    (1707, 19, '亏'),
    (1648, 115, '一'),
    (1665, 99, '169'),
    (1730, 124, '，'),
    (1739, 99, '000'),
    (1670, 199, '一'),
    (1687, 183, '17'),
    (1740, 183, '650'),
]

TOKENS = [Token(x=x, y=y, text=text) for x, y, text in OCR]


def test_header_fragments_become_columns():
    rows, problems = build_rows(TOKENS)
    assert rows, problems
    assert set(rows[0]) >= {"证券代码", "实际数量", "成本价", "市价", "盈亏"}


def test_digits_are_reassembled_against_the_panel_total():
    """小数位不是猜的：只有一种排法能让 Σ 数量 × 市价 落在面板的 1,638 上。"""
    rows, _ = build_rows(TOKENS)
    positions, problems, convention = interpret(rows, panel_value=1638.0)
    assert not problems, problems
    assert len(positions) == 1, "000700 实际数量是 0（当日卖出待交收），不该算持仓"
    p = positions[0]
    assert p["symbol"] == "000590.SZ"
    assert p["qty"] == 200
    assert p["cost"] == 9.035
    assert p["market_value"] == 1638.0
    assert p["pnl"] == -169.0
    assert "3位" in convention


def test_the_panel_pins_the_scale_for_better_or_worse():
    """小数位完全由面板决定——所以面板必须自己可信。

    这是这套方法的真正弱点，写进测试而不是写在注释里：同一串数字 `9035` / `8190` /
    `169000` 在 0–3 位小数下都能自洽（盈亏方程两边同比缩放）。把面板换成 163,800，
    它会选「1 位小数」并给出市值 163,800——数字自洽，但是错的。
    因此读取器把面板当锚，而面板本身是另一条独立通道（Static 文本）读来的。
    """
    rows, _ = build_rows(TOKENS)
    pinned, _, _ = interpret(rows, panel_value=1638.0)
    assert pinned[0]["market_value"] == 1638.0
    assert pinned[0]["cost"] == 9.035

    bent, _, _ = interpret(rows, panel_value=163800.0)
    assert bent[0]["market_value"] == 163800.0, "面板被换成 163,800 时它会跟着弯"
    assert bent[0]["cost"] == 903.5, "同一串 9035，放大了一位小数"


def test_empty_tokens_are_reported_not_crashed():
    rows, problems = build_rows([])
    assert rows == []
    assert "没有任何 OCR 结果" in problems[0]
