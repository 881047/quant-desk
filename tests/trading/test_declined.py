"""「看过，没买」必须能被结算，而且不能自选价格。

全台原本只有三种判断会被结算：提案（arena）、人格切换（scorecard）、假设（裁判）。
不行动只存在于复盘散文里——2026-11-01 到 11-03 三天零成交、零记录，等于三天判断力
没有任何证据。

这里钉住四件事：
1. 写入路径没有价格字段。否则一条"我不买"可以事后挑一个价格来自证。
2. 到期才结算，没到期是 pending，不是失败也不是 0。
3. 超额算的是「如果你买了，跑赢还是跑输等权市场」，符号不能反。
4. 日线缺一天就报 blocked，不能用 0% 顶替——"不知道"和"没涨"是两件事。
"""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from service.trading import declined as declined_mod
from service.trading.state import PaperStore

SESSIONS = ["20260105", "20260106", "20260107", "20260108", "20260109"]


class _Store:
    """只实现 `read("daily", ...)`，够 arena 的基准与本模块的结算用。"""

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def read(self, dataset, start=None, end=None, *, columns=None, status=None):
        if dataset != "daily":
            return pd.DataFrame()
        frame = self._frame.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        if start:
            frame = frame[frame["trade_date"] >= str(start)]
        if end:
            frame = frame[frame["trade_date"] <= str(end)]
        if columns:
            keep = [c for c in columns if c in frame.columns]
            frame = frame[keep]
        return frame


def _bars(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """(date, code, pre_close, close) → daily 面板。"""
    return pd.DataFrame(
        [{"trade_date": d, "ts_code": c, "pre_close": p, "close": q}
         for d, c, p, q in rows]
    )


def _paper(tmp_path) -> PaperStore:
    return PaperStore(tmp_path / "paper.sqlite")


def _flat(code: str, price: float = 10.0) -> list[tuple[str, str, float, float]]:
    return [(d, code, price, price) for d in SESSIONS]


def test_a_declined_record_carries_no_price(tmp_path):
    """请求体里多写一个 price 就该被拒，不是被静默忽略。

    静默忽略最糟：写的人会以为自己锁定了成交价，而结算用的是它自己读的收盘价。
    """
    from service.api.trading import DeclinedBody

    with pytest.raises(ValidationError):
        DeclinedBody(symbols=["600000.SH"], reason="涨幅透支", price=10.0)


def test_add_rejects_what_cannot_be_settled(tmp_path):
    paper = _paper(tmp_path)

    with pytest.raises(ValueError, match="交易所后缀"):
        declined_mod.add(paper, "20260105", ["600000"], "没后缀")
    with pytest.raises(ValueError, match="至少要写一只"):
        declined_mod.add(paper, "20260105", [], "什么都没写")
    with pytest.raises(ValueError, match="reason"):
        declined_mod.add(paper, "20260105", ["600000.SH"], "   ")
    with pytest.raises(ValueError, match="horizon_days"):
        declined_mod.add(paper, "20260105", ["600000.SH"], "期限太长", horizon_days=99)


def test_a_record_is_pending_until_the_horizon_has_sessions(tmp_path):
    """还没到期时不结算、不当成 0。`pending` 是"还没到能判的时候"。"""
    paper = _paper(tmp_path)
    store = _Store(_bars(_flat("600000.SH")))
    declined_mod.add(paper, "20260107", ["600000.SH"], "再看两天", horizon_days=5)

    out = declined_mod.score(store, paper)

    assert out["scored"] == []
    assert out["pending"] == 1
    assert paper.list_declined(settled=False)[0]["result"] is None


def test_excess_is_measured_against_the_equal_weight_market(tmp_path):
    """符号必须对：放弃的票跑输市场 → mean_excess 为负 → 那次不买是对的。

    构造：决策日 01-05，期限 2（窗口 01-06、01-07）。
    600000 在 01-07 跌 10%，600001 全程不动 → 等权市场 −5%。
    于是 600000 的超额是 −5%（跑输），这正是"不买是对的"的读数。
    """
    paper = _paper(tmp_path)
    rows = [
        *[(d, "600000.SH", 10.0, 10.0) for d in SESSIONS[:2]],
        ("20260107", "600000.SH", 10.0, 9.0),
        *[(d, "600000.SH", 9.0, 9.0) for d in SESSIONS[3:]],
        *_flat("600001.SH"),
    ]
    store = _Store(_bars(rows))
    declined_mod.add(paper, "20260105", ["600000.SH"], "位置太高", horizon_days=2)

    out = declined_mod.score(store, paper)

    assert len(out["scored"]) == 1
    item = out["scored"][0]
    assert item["entry_date"] == "20260105"
    assert item["end_date"] == "20260107"
    assert item["mean_return"] == pytest.approx(-0.10, abs=1e-6)
    assert item["market_return"] == pytest.approx(-0.05, abs=1e-6)
    assert item["mean_excess"] == pytest.approx(-0.05, abs=1e-6)

    # 落库了，而且第二次跑不会再算一遍
    assert paper.list_declined(settled=True)[0]["result"]["mean_excess"] == pytest.approx(-0.05)
    assert declined_mod.score(store, paper)["scored"] == []


def test_a_missing_bar_blocks_instead_of_counting_as_zero(tmp_path):
    """少一天的票不能按 0% 计：那会把"不知道"写成"没涨"，而两者结论相反。"""
    paper = _paper(tmp_path)
    rows = [
        ("20260105", "600000.SH", 10.0, 10.0),
        ("20260106", "600000.SH", 10.0, 10.0),
        # 01-07 缺这一只
        ("20260108", "600000.SH", 10.0, 10.0),
        *_flat("600001.SH"),
    ]
    store = _Store(_bars(rows))
    declined_mod.add(paper, "20260105", ["600000.SH"], "等回踩", horizon_days=2)

    out = declined_mod.score(store, paper)

    assert out["scored"] == []
    assert out["blocked"] and "完整日线" in out["blocked"][0]["error"]
    assert paper.list_declined(settled=False), "被挡下的仍然是待结算，不是已结算"


def test_partial_data_still_settles_the_symbols_it_has(tmp_path):
    """两只里有一只数据不全：结算那一只，另一只进 `missing`，不整条作废。"""
    paper = _paper(tmp_path)
    store = _Store(_bars(_flat("600000.SH") + _flat("600001.SH")))
    declined_mod.add(paper, "20260105", ["600000.SH", "600999.SH"], "两只都不买",
                     horizon_days=2)

    out = declined_mod.score(store, paper)

    item = out["scored"][0]
    assert item["n"] == 1
    assert item["missing"] == ["600999.SH"]


def test_summary_reports_the_distribution_not_a_verdict(tmp_path):
    paper = _paper(tmp_path)
    rows = [
        *[(d, "600000.SH", 10.0, 10.0) for d in SESSIONS[:2]],
        ("20260107", "600000.SH", 10.0, 9.0),
        *[(d, "600000.SH", 9.0, 9.0) for d in SESSIONS[3:]],
        *_flat("600001.SH"),
    ]
    store = _Store(_bars(rows))
    declined_mod.add(paper, "20260105", ["600000.SH"], "位置太高", horizon_days=2)
    declined_mod.score(store, paper)

    out = declined_mod.summary(paper)

    assert out["n_decisions"] == 1
    assert out["n_observations"] == 1
    assert out["mean_excess"] == pytest.approx(-0.05, abs=1e-6)
    assert out["underperformed_share"] == 1.0
    assert "verdict" not in out, "汇总是分布，不是评语"
    assert out["caveats"], "不独立这件事必须写出来"


def test_a_matured_record_shows_up_in_the_work_queue(desk):
    """到期不提醒，它就一直 pending——而 pending 不进 summary，等于又变回散文。

    判据按交易日数，不按自然日：11-03 记的、期限 1 天，11-04 收盘前不该提醒。
    """
    from service.trading import duty

    day = desk.market.latest_bar_date()
    # 判据是"已经过去的交易日数"，和 memo_due 一样按有 bar 的日子数、不按自然日。
    # 所以今天记的、期限 1 天的那条今天不该被催；几个月前记的那条才该。
    desk.paper.add_declined(day, ["600000.SH"], "今天刚记的", horizon_days=1)
    desk.paper.add_declined("20260105", ["600001.SH"], "早就该结算了", horizon_days=1)

    out = duty.build(desk, day=day, today=day)
    due = [i for i in out["items"] if i["kind"] == "declined_due"]

    assert len(due) == 1, "只有过期的那条该被催"
    assert "20260105" in due[0]["detail"]
    # 用 ["blocks"] 不用 .get()：缺字段时 .get() 也给 None，
    # "声明了不挡"和"忘了声明"就分不开。
    assert due[0]["blocks"] is None, "催结算不是阻塞项"


def test_the_same_symbol_is_recorded_once_a_day(desk):
    """09:26 记过的票，14:30 再权衡一次不该新增一行。

    结算按行算：同一只票一天两行，等于把同一个判断算两遍，命中率会被少数几只
    反复出现的票带偏。2026-11-13 的复盘把「避免同日两笔」写成规矩，但那时只靠
    模型自觉；现在第二行写不进去。
    """
    day = desk.market.latest_bar_date()
    first = declined_mod.add(desk.paper, day, ["600000.SH"], "首窗权衡后不买")
    again = declined_mod.add(desk.paper, day, ["600000.SH"], "末窗又看了一次")

    assert again["id"] == first["id"]
    assert again["skipped"] == ["600000.SH"]
    assert again["declined"] is None
    assert "同一只票一天只记一次" in again["note"]
    rows = [r for r in desk.paper.list_declined(limit=50) if r["day"] == day]
    assert len(rows) == 1, "同一天同一只票写了两行"


def test_only_the_new_names_go_into_a_second_record(desk):
    """部分重复时写进去的只有新的那几只，重复的不静默丢掉，而是回一个 skipped。"""
    day = desk.market.latest_bar_date()
    declined_mod.add(desk.paper, day, ["600000.SH"], "先记一只")

    out = declined_mod.add(desk.paper, day, ["600000.SH", "600001.SH"], "再权衡两只")

    assert out["skipped"] == ["600000.SH"]
    assert out["declined"]["symbols"] == ["600001.SH"]
    assert "600000.SH" in out["note"]
