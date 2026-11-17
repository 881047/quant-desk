"""Facts exist so a report cites the session instead of its own sample.

The distinction these tests protect: **null is not zero**. A missing pool means
"not published"; an empty one means "measured, nothing there". On 2026-10-29 the
two collapsed into each other and "no data" read as "nothing to buy".
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading.facts import build_facts


class _Data:
    def __init__(self, frames):
        self._frames = frames

    def read(self, name, start=None, end=None, columns=None):
        # Signature must match DatasetStore: `breadth()` asks for three columns,
        # and a double that rejects the kwarg turns a real read into a TypeError
        # that has nothing to do with the behaviour under test.
        if name not in self._frames:
            raise KeyError(name)
        frame = self._frames[name]
        if columns and not frame.empty:
            return frame[[c for c in columns if c in frame.columns]]
        return frame


class _Market:
    def __init__(self, rows, last_bar="20261029"):
        self._rows = rows
        self._last_bar = last_bar
        self.calendar = type("C", (), {"days": ["20261028", "20261029"]})()

    def latest_bar_date(self):
        return self._last_bar

    def daily(self, day):
        return self._rows.get(day, pd.DataFrame())


class _Paper:
    def list_tickets(self, limit=20):
        return [{"ticket_id": "T1", "symbol": "600000.SH", "side": "BUY",
                 "status": "filled", "trade_date": "20261029"},
                {"ticket_id": "T0", "symbol": "600001.SH", "side": "BUY",
                 "status": "filled", "trade_date": "20261028"}]


class _Book:
    def summary(self):
        return {"equity": 5000.0, "cash_available": 5000.0, "market_value": 0.0,
                "positions": [], "daily_pnl_pct": 0.0, "drawdown": 0.0}


class _Desk:
    def __init__(self, frames, rows, last_bar="20261029"):
        self.data = _Data(frames)
        self.market = _Market(rows, last_bar)
        self.paper = _Paper()
        self.book = _Book()


def _rows(n=100):
    return {"20261028": pd.DataFrame({"close": [10.0] * n,
                                      "pre_close": [10.0] * n}),
            "20261029": pd.DataFrame({"close": [10.0] * n,
                                      "pre_close": [10.0] * n})}


def _pool(prices, open_times, seals):
    return pd.DataFrame({"code": [f"S{i}" for i in range(len(prices))],
                         "close": prices, "open_times": open_times,
                         "last_seal_time": seals})


def _derived(prices, opens, lows, limits):
    n = len(prices)
    return pd.DataFrame({"code": [f"S{i}" for i in range(n)],
                         "pool": ["limit_up"] * n,
                         # breadth() 也从这张表数连板，缺列会让 facts 整包抛错
                         "boards": [1] * n,
                         "close": prices, "open": opens, "low": lows,
                         "limit_price": limits})


def test_the_buyable_counts_are_not_interchangeable():
    """in_cap ≠ one_word ≠ buyable_at_close，而且它们同源。

    ``one_word`` 日线能严格判定（open == low == limit）；「封板后有没有开过板」
    日线看不出来，只能挂靠东财的 open_times，所以它单独成块、单独写覆盖。
    """
    desk = _Desk({
        "limit_pool_t0": _derived([10.0, 10.0, 10.0, 10.0],
                                  [10.0, 9.5, 10.0, 9.8],
                                  [10.0, 9.4, 10.0, 9.7],
                                  [10.0, 10.0, 10.0, 10.0]),
        "limit_up_pool": _pool([10.0, 10.0, 10.0, 10.0], [0, 3, 0, 1],
                               ["09:30:00", "14:20:00", "09:30:00", "13:05:00"]),
    }, _rows())
    got = build_facts(desk, "20261029")["affordable"]

    assert got["in_cap"] == 4
    assert got["one_word"] == 2                     # S0 与 S2：开盘即封、全天一个价
    # 三分法：S1、S3 开盘低于涨停价 → 盘中封板（封板前可买）
    assert got["fill_kind"] == {"one_word": 2, "opened_after_seal": 0,
                                "sealed_intraday": 2}
    assert got["board_opens"]["opened"] == 2        # 东财 open_times > 0
    assert got["board_opens"]["never_opened"] == 2
    assert got["buyable_at_close"] == 0
    assert got["source"] == "derived:limit_pool_t0"


def test_a_low_below_the_cap_does_not_mean_the_board_opened():
    """000037.SZ 2026-11-02：low=8.74 < 涨停价 8.78，但 09:30 就封死、全天未开。

    ``low`` 包含封板之前的成交，用它判「开过板」会把几乎所有早盘封板的票
    都算成开过板。开板次数只有东财的 ``open_times`` 能给。
    """
    desk = _Desk({
        "limit_pool_t0": _derived([8.78], [8.77], [8.74], [8.78]),
        "limit_up_pool": _pool([8.78], [0], ["09:30:00"]),
    }, _rows())
    got = build_facts(desk, "20261029")["affordable"]

    assert got["one_word"] == 0                     # open 8.77 ≠ 涨停价，不是一字板
    assert got["board_opens"]["never_opened"] == 1  # 但它全天没开过板


def test_names_above_the_cap_are_excluded():
    """One lot at 50% of a ¥5,000 account caps the price at ¥25.

    2026-11-16：权重由 0.30 改为 0.50（CORE 退役后单票上限统一到
    ``max_aggressive_single``）。25.0 正好落在边界上，用来钉住闭区间语义：
    它必须在界内，40.0 必须在界外。
    """
    desk = _Desk({"limit_pool_t0": _derived([10.0, 15.0, 25.0, 40.0],
                                            [10.0, 15.0, 25.0, 40.0],
                                            [10.0, 15.0, 25.0, 40.0],
                                            [10.0, 15.0, 25.0, 40.0])}, _rows())
    got = build_facts(desk, "20261029")["affordable"]

    assert got["cap"] == 25.0
    assert got["in_cap"] == 3          # 10.0 / 15.0 / 25.0 在界内，40.0 在外
    assert got["board_opens"]["available"] is False   # 当天没有东财池


def test_a_missing_pool_is_null_not_zero():
    """The failure that started this: absence reported as a measured zero."""
    desk = _Desk({}, _rows())
    got = build_facts(desk, "20261029")["affordable"]

    assert got["in_cap"] is None
    assert got["one_word"] is None
    assert got["buyable_at_close"] is None
    assert "不可复现" in got["note"]


def test_facts_carry_the_readiness_verdict():
    desk = _Desk({"limit_up_pool": _pool([10.0], [0], ["09:31:00"])}, _rows())
    facts = build_facts(desk, "20261029")

    assert facts["readiness"]["code"] == "READY"
    assert facts["readiness"]["ok"] is True


def test_tickets_are_filtered_to_the_day():
    desk = _Desk({"limit_up_pool": _pool([10.0], [0], ["09:31:00"])}, _rows())
    facts = build_facts(desk, "20261029")

    assert [t["id"] for t in facts["tickets_today"]] == ["T1"]


def test_breadth_counts_every_name_in_the_frame():
    desk = _Desk({"limit_up_pool": _pool([10.0], [0], ["09:31:00"])}, _rows(50))
    got = build_facts(desk, "20261029")["breadth"]

    assert got["rows"] == 50
    assert got["flat"] == 50
    assert got["equal_weight_return"] == 0.0


def test_breadth_states_the_unit_of_its_returns():
    """`equal_weight_return: -0.0304` 没写量纲时，读者会写"跌 0.03%"。

    facts / discover / industries 用小数，profile 的 `*_pct` 用百分数——
    同一个产品两种写法，量纲必须跟着数一起走，不能只写在文档里。
    """
    from service.trading.facts import _breadth

    class FakeMarket:
        def daily(self, day):
            return pd.DataFrame({
                "ts_code": ["600000.SH", "000001.SZ"],
                "close": [9.7, 9.6], "pre_close": [10.0, 10.0],
            })

    desk = type("D", (), {"market": FakeMarket()})()
    out = _breadth(desk, "20261030")

    # 两只票各跌 3% 与 4%，等权均值 -3.5%——写成小数是 -0.035
    assert out["equal_weight_return"] == pytest.approx(-0.035, abs=1e-9)
    assert "-3.04%" in out["units"] or "-0.0304" in out["units"]
    assert "_pct" in out["units"], "要指出另一套写法在哪，否则读者不知道有两套"


def test_facts_carry_no_judgement_fields():
    """内核是信息源，不是建议引擎：facts 里不该出现判断字段。

    这条纪律本来就有（``test_observation.py`` 的 ``FORBIDDEN_KEYS``），但只覆盖了
    observation。于是 ``persona.suggest``（推介人格）与 ``screen.score``（0.65/0.35
    加权综合分）一直漏着——两个都是内核替模型做的判断，而且都进了模型读的 payload。
    这里把同一把尺子铺到 facts 上。
    """
    desk = _Desk({"limit_up_pool": _pool([10.0], [0], ["09:31:00"])}, _rows())
    facts = build_facts(desk, "20261029")

    forbidden = {"action", "recommendation", "recommend", "suggest", "rating",
                 "score", "should", "pick", "best"}

    def walk(node, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                where = f"{path}.{key}" if path else str(key)
                assert str(key).lower() not in forbidden, f"出现了判断字段：{where}"
                walk(value, where)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(facts)


def test_intraday_facts_are_returned_but_not_frozen(tmp_path, monkeypatch):
    """盘中快照可以回答"现在多少"，但不能成为当天收盘报告的核对基准。

    Regression, 2026-11-12: 17:56 的 `report_check` 读到的是一份午间写下的
    facts（readiness=SESSION_OPEN、in_cap=None），于是把一份收盘报告判成
    FAIL 并写进 `reports/daily/20261112.json`；18:38 写下的收盘 facts 是
    READY、in_cap=26。盘中数照给，落盘只发生在 readiness.ok 的时候。
    """
    import service.trading.facts as facts_mod

    intraday = {"day": "20261112",
                "readiness": {"ok": False, "code": "SESSION_OPEN"},
                "affordable": {"in_cap": None}}
    closing = {"day": "20261112",
               "readiness": {"ok": True, "code": "READY"},
               "affordable": {"in_cap": 26}}

    monkeypatch.setattr(facts_mod, "build_facts", lambda desk, day: dict(intraday))
    written: list[dict] = []
    monkeypatch.setattr(
        facts_mod, "write_facts",
        lambda desk, day, root=None, payload=None: written.append(payload))

    got = facts_mod.facts_for(object(), "20261112", root=tmp_path)
    assert got == intraday, "盘中调用仍要拿到刚算出来的数"
    assert written == [], "盘中快照不得冻结成当天权威"
    assert not (tmp_path / "reports" / "facts" / "20261112.json").exists()

    monkeypatch.setattr(facts_mod, "build_facts", lambda desk, day: dict(closing))
    got = facts_mod.facts_for(object(), "20261112", root=tmp_path)
    assert written == [got], "收盘后落盘的必须是返回给调用方的那一份"
    assert written[0] is got, "同一份事实只算一次"
    assert written[0]["readiness"]["code"] == "READY"


def test_a_past_days_facts_are_refused_not_backfilled(tmp_path):
    """账户半边是"现在"的，倒填过去的日子就是造一份当天不存在的事实。

    2026-11-05 的 facts 是 11-06 14:05 写的；那几天账户恰好空仓 5,000，
    差额没露出来。写不进去比写进去更接近真相。
    """
    import service.trading.facts as facts_mod

    desk = _Desk({}, _rows(), last_bar="20261029")
    with pytest.raises(ValueError, match="account half"):
        facts_mod.write_facts(desk, "20261028", root=tmp_path)
    assert not (tmp_path / "reports" / "facts" / "20261028.json").exists()

    path = facts_mod.write_facts(desk, "20261029", root=tmp_path)
    assert path.exists()
    written = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert written["account_as_of"] == "20261029"
    assert written["day"] == "20261029"


def test_a_past_day_says_the_account_half_is_not_its_own():
    """2026-11-10 的 facts 里躺着 11-13 的账户余额——`account_as_of` 是字段，
    但读者（尤其是模型）不会自己去比两个日期。

    补写过去某天的报告时，一句说明比一个字段更难被忽略：可引用的只有 market 半边。
    """
    desk = _Desk({"limit_pool_t0": _derived([10.0], [10.0], [10.0], [10.0])}, _rows())

    today = build_facts(desk, "20261029")
    past = build_facts(desk, "20261028")

    assert "account_note" not in today
    assert past["account_as_of"] == "20261029"
    assert "20261028" in past["account_note"]
    assert "20261029" in past["account_note"]
    assert "market 半边" in past["account_note"]
