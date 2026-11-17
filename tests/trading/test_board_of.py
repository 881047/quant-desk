"""板块是一个事实，不是三个模块各自的推断。

`board` 原本有两份实现：`market.instrument` 用三条前缀元组给定价用，
`universe.browse` 用 `.BJ` 后缀给显示用。第二份只对北交所有分支，于是
5,377 只沪深股在 `quant_universe` 里全部是 `board: null` —— 一个只对 4% 的行
携带信息的字段，而板块决定涨跌幅、一手多少股、最小价格变动。

这组测试钉三件事：代码段与板块的对应关系、字符串与 `Board` 枚举同一套词、
以及"字段对所有行都有值"。
"""

from __future__ import annotations

import pytest

from service.adapters.symbols import board_of
from service.kernel.models import Board


@pytest.mark.parametrize("code,expected", [
    # 沪市主板：600/601/603/605
    ("600519.SH", "MAIN"),
    ("601857.SH", "MAIN"),
    ("603703.SH", "MAIN"),
    ("605499.SH", "MAIN"),
    # 科创板：688/689
    ("688981.SH", "STAR"),
    ("689009.SH", "STAR"),
    # 深市主板：000/001/002/003
    ("000001.SZ", "MAIN"),
    ("001979.SZ", "MAIN"),
    ("002594.SZ", "MAIN"),
    ("003816.SZ", "MAIN"),
    # 创业板：300/301
    ("300750.SZ", "CHINEXT"),
    ("301520.SZ", "CHINEXT"),
    # 北交所：43/83/87/88 存量 + 920 新号段
    ("920592.BJ", "BSE"),
    ("430047.BJ", "BSE"),
    ("830799.BJ", "BSE"),
    ("871981.BJ", "BSE"),
    # 基金与转债：板块要先于股票判断，否则 51 开头会被当成沪市主板
    ("510300.SH", "ETF"),
    ("159915.SZ", "ETF"),
    ("113050.SH", "CB"),
    ("123122.SZ", "CB"),
])
def test_the_code_range_decides_the_board(code, expected):
    assert board_of(code) == expected


def test_board_strings_are_the_enum_values():
    """一套词。`board_of` 返回字符串是因为 adapters 不该依赖 trading 层，
    但字符串一旦和 `Board` 的值分叉，`Board(board_of(x))` 就会在某个板块上
    抛 ValueError——而那会是一条只在北交所上出现的崩，最难在回测里遇见。
    """
    for board in Board:
        assert board_of({
            Board.MAIN: "600519.SH", Board.CHINEXT: "300750.SZ",
            Board.STAR: "688981.SH", Board.BSE: "920592.BJ",
            Board.ETF: "510300.SH", Board.CB: "113050.SH",
        }[board]) == board.value


def test_instrument_and_the_display_layer_agree(desk):
    """定价用的板块和显示用的板块必须是同一份推断。

    两份实现在 2026-11-01 之前就存在，而且已经分叉：`instrument` 知道创业板，
    `browse` 只知道北交所。一条断言不够防分叉，所以这里比对的是真实标的。
    """
    for symbol in ("600519.SH", "300750.SZ", "688981.SH", "920592.BJ",
                   "510300.SH", "113050.SH"):
        inst = desk.market.instrument(symbol, desk.market.latest_bar_date())
        assert inst.board is Board(board_of(symbol))
