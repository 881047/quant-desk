"""裁判的读数必须都能落库。

`underpowered` 是 `_verdict` 的合法返回值之一（"这个设计看不见这个量"），
但 `update_hypothesis` 的白名单漏了它。后果不是报错那么简单：
`run_research.py` 把报告写完、再更新账本，于是 ValueError 抛在**报告已落盘之后**，
hypothesis 停在旧状态——一种安静的不一致，而且只在"设计的功效不够"这种
恰恰最需要记录的情况下发生。
"""

from __future__ import annotations

import pytest

REFEREE_READINGS = ("testing", "promoted", "rejected", "underpowered")


@pytest.mark.parametrize("status", REFEREE_READINGS)
def test_every_referee_reading_can_be_stored(desk, status):
    hyp_id = desk.paper.add_hypothesis("测试假设", "理由", '{"pool":"limit_up"}')
    updated = desk.paper.update_hypothesis(hyp_id, status, {"gate_reading": status})
    assert updated["status"] == status


def test_a_made_up_status_is_still_refused(desk):
    """放宽白名单不等于不校验：拼错的词仍然要报错，否则账本会混进无法解释的状态。"""
    hyp_id = desk.paper.add_hypothesis("测试假设", "理由", '{"pool":"limit_up"}')
    with pytest.raises(ValueError, match="bad hypothesis status"):
        desk.paper.update_hypothesis(hyp_id, "profitable", {})
