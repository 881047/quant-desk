"""每个预设都必须是 `parse_spec` 认得的 spec。

这个模块没有逻辑，只有九张配置。它以前没有测试——而"没有逻辑"不等于"不会
坏"：`parse_spec` 收紧一个取值域（比如把 `cb_ret` 从 `_RANKS` 拿掉），预设仍然
原样躺在字典里，要等到有人真的跑那条研究时才炸，而那时离改动已经过去很多天。

更贵的一课来自这个文件自己的 docstring：`scripts/recost.py` 曾经从保存的判决里
"重建" spec，而判决是有损的 —— `link_pool` 掉回默认值，`正股跌停→转债` 静默变成
`正股涨停→转债`。所以预设要在这里、在库内，并且要能被验证。
"""

from __future__ import annotations

import pytest

from service.research.presets import PRESETS
from service.research.spec import T0_POOLS, EventStudy, parse_spec


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_parses(name):
    spec = parse_spec(dict(PRESETS[name]))

    assert isinstance(spec, EventStudy)
    assert spec.pool in {"limit_up", "broken_board", "limit_down", "losers",
                         "winners", "cb", "cb_link"}
    # label 是审计链里出现的那个字符串，生成不出来就等于没有名字。
    assert spec.name()


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_a_same_day_exit_only_exists_for_t0_pools(name):
    """``hold=0`` 只在 T+0 品种上合法；股票当日买不能当日卖。"""
    raw = PRESETS[name]
    if int(raw.get("hold", 1)) == 0:
        assert raw["pool"] in T0_POOLS, f"{name} 是 hold=0 但 pool 不是 T+0"


def test_every_preset_is_next_open():
    """九个预设全是次日开盘入场——这本身就是一条要看见的事实。

    当日收盘入场不在 ``_ENTRIES`` 里，因为日线引擎表达不了「封板那一刻成交」：
    加上它会让回测给出 +2.21%/笔的漂亮曲线，而那 2.2% 属于「已经成交的人」，
    不属于「想去成交的人」。这条断言不是风格检查，是把这个决定钉在这里，
    免得哪天有人顺手加一个 `same_close` 而没人发现入场语义变了。
    """
    entries = {PRESETS[name].get("entry", "next_open") for name in PRESETS}

    assert entries == {"next_open"}
