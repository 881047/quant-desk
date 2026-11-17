"""龙虎榜与异动归因：三个暴露给模型的工具，此前一条测试都没有。

覆盖率闸 2026-11-04 实测：`tape.lhb` / `tape.seat_record` / `tape.anomaly`
整块未覆盖（1137-1192、1366-1394、1425-1460）。而 `quant_lhb` 是说明里让模型
"看昨天强不强"时用的工具之一，`seat=` 还被写成"榜面引出但从不回答的那个后续问题"。

没测试的工具不是"大概能用"，是"坏了没人知道"。这里钉住四件容易错的事：

1. `data_date` 取榜单里最大的交易日，不是请求日——榜单收盘后才发布，
   盘中请求 11-04 拿到的是 11-03 的榜。
2. 排序是净买额降序，`limit` 在排序之后截，不是先截后排。
3. 席位的"次日开盘溢价"必须落到**下一个交易日**：停牌两天再赶上周末，
   自然日跨度也会落在 7 天内，旧口径会把 T+2 的价当成次日溢价。
4. 异动归因永远带 caveats（上游是 AI 写的散文，不是公告）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from service.trading import tape


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None):
        frame = self.frames.get(name, pd.DataFrame()).copy()
        if frame.empty:
            return frame
        if columns:
            keep = [c for c in columns if c in frame.columns]
            frame = frame[keep]
        return frame


def _desk(frames) -> SimpleNamespace:
    return SimpleNamespace(data=FakeData(frames))


def _lhb_frame():
    return pd.DataFrame([
        {"trade_date": "20261103", "ts_code": "600000.SH", "name": "浦发银行",
         "pct": 3.21, "net_value": 50_000_000.0, "net_rate": 5.5,
         "buy_value": 80_000_000.0, "sell_value": 30_000_000.0,
         "org_net_value": 10_000_000.0, "hot_money_net_value": 40_000_000.0,
         "hot_rank": 3.0, "range_days": float("nan"), "reason": "日涨幅偏离值达 7%",
         "concepts": "银行"},
        {"trade_date": "20261103", "ts_code": "000001.SZ", "name": "平安银行",
         "pct": 9.98, "net_value": 120_000_000.0, "net_rate": 8.1,
         "buy_value": 200_000_000.0, "sell_value": 80_000_000.0,
         "org_net_value": -5_000_000.0, "hot_money_net_value": 125_000_000.0,
         "hot_rank": 1.0, "range_days": 3.0, "reason": "连续三个交易日涨幅偏离",
         "concepts": "银行"},
        # 更早的一行：不能混进最新榜单
        {"trade_date": "20261102", "ts_code": "600519.SH", "name": "贵州茅台",
         "pct": -2.0, "net_value": -9_000_000.0, "net_rate": -1.0,
         "buy_value": 1.0, "sell_value": 1.0,
         "org_net_value": 0.0, "hot_money_net_value": 0.0,
         "hot_rank": float("nan"), "range_days": float("nan"),
         "reason": "日跌幅偏离值达 7%", "concepts": "白酒"},
    ])


def test_lhb_reports_the_board_date_not_the_request_date():
    """盘中请求今天，拿到的是上一交易日的榜——这个差别必须写在 data_date 里。

    否则读者会以为看过的是今天的资金，而那张榜还没发布。
    """
    desk = _desk({"ths_lhb": _lhb_frame()})

    out = tape.lhb(desk, day="20261104")

    assert out["ok"] is True
    assert out["as_of"] == "20261104"
    assert out["data_date"] == "20261103", "榜单日期取表里最大的那个交易日"
    assert out["count"] == 2, "11-02 那行不能混进来"
    assert {i["ts_code"] for i in out["items"]} == {"600000.SH", "000001.SZ"}


def test_lhb_sorts_by_net_value_before_applying_the_limit():
    """先排后截。反过来的话 `limit=1` 返回的是表里第一行，不是买得最多的那只。"""
    desk = _desk({"ths_lhb": _lhb_frame()})

    out = tape.lhb(desk, day="20261104", limit=1)

    assert len(out["items"]) == 1
    assert out["items"][0]["ts_code"] == "000001.SZ"
    assert out["items"][0]["net_value_cny"] == pytest.approx(120_000_000.0)
    # 整数型字段里的 NaN 要变成 null，不能是 NaN（JSON 里非法）
    assert out["items"][0]["range_days"] == 3


def test_lhb_for_a_symbol_that_never_made_the_board_says_so():
    """空列表 + 一句解释，比空列表可信。"""
    desk = _desk({"ths_lhb": _lhb_frame()})

    out = tape.lhb(desk, day="20261104", symbol="600519.SH")

    assert out["ok"] is True
    assert out["items"] == []
    assert "未上" in out["note"]


def test_lhb_without_a_fetch_names_the_command_instead_of_an_empty_board():
    """表空是"还没抓"，不是"今天没人上榜"。两者对一个交易者是完全不同的结论。"""
    desk = _desk({"ths_lhb": pd.DataFrame()})

    out = tape.lhb(desk, day="20261104")

    assert out["ok"] is False
    assert "fetch_ths_special" in out["error"]


def _seat_frames():
    seats = pd.DataFrame([
        {"trade_date": "20261103", "ts_code": "000001.SZ", "seat": "T王",
         "net_value": 65_000_000.0},
        {"trade_date": "20261103", "ts_code": "600000.SH", "seat": "T王",
         "net_value": 12_000_000.0},
        {"trade_date": "20261103", "ts_code": "600000.SH", "seat": "别的席位",
         "net_value": 99_000_000.0},
    ])
    # 交易日历：11-03 之后是 11-04，再之后 11-05
    cal = pd.DataFrame({"cal_date": ["20261102", "20261103", "20261104", "20261105"],
                        "is_open": [1, 1, 1, 1]})
    daily = pd.DataFrame([
        # 000001.SZ：11-03 收 10，11-04 开 10.5 / 收 10.8
        {"ts_code": "000001.SZ", "trade_date": "20261103", "open": 9.8, "close": 10.0},
        {"ts_code": "000001.SZ", "trade_date": "20261104", "open": 10.5, "close": 10.8},
        # 600000.SH：11-03 收 8，11-04 开 8.1 / 收 7.9
        {"ts_code": "600000.SH", "trade_date": "20261103", "open": 7.9, "close": 8.0},
        {"ts_code": "600000.SH", "trade_date": "20261104", "open": 8.1, "close": 7.9},
    ])
    return {"ths_lhb_seat": seats, "trade_cal": cal, "daily": daily}


def test_a_seat_record_measures_from_the_next_open_not_the_board_close():
    """榜单 20:00 才发布，那天的收盘价已经买不到了。

    所以次日开盘溢价 = 收盘[D] → 开盘[D+1]；持有到收盘 = 收盘[D] → 收盘[D+1]。
    """
    desk = _desk(_seat_frames())

    out = tape.seat_record(desk, "T王", day="20261104")

    assert out["ok"] is True, out.get("error")
    assert out["appearances"] == 2, "只算这个席位，别的席位不算"
    assert out["gap_basis"] == "next_session"
    assert out["next_open_gap"]["mean"] == pytest.approx((0.05 + 0.0125) / 2, abs=1e-4)
    assert out["next_close"]["mean"] == pytest.approx((0.08 - 0.0125) / 2, abs=1e-4)


def test_a_suspended_name_cannot_donate_a_t_plus_two_price_to_the_next_open():
    """停牌两天再赶上周末，自然日跨度也只有 4 天。

    旧口径（0 < 天数 ≤ 7）会把 T+2 的开盘价当成"次日开盘溢价"——一条本该
    只含隔夜的统计里混进了两天。按交易日历对齐之后，这一行必须被丢掉。
    """
    frames = _seat_frames()
    # 把 000001.SZ 的 11-04 拿掉，让它的"下一根 bar"变成 11-05
    kept = frames["daily"][
        ~((frames["daily"]["ts_code"] == "000001.SZ")
          & (frames["daily"]["trade_date"] == "20261104"))
    ]
    frames["daily"] = pd.concat([kept, pd.DataFrame([
        {"ts_code": "000001.SZ", "trade_date": "20261105", "open": 11.0, "close": 11.2},
    ])], ignore_index=True)
    desk = _desk(frames)

    out = tape.seat_record(desk, "T王", day="20261104")

    assert out["gap_basis"] == "next_session"
    # 只剩 600000.SH 那一笔：11-04 确实是 11-03 的下一个交易日
    assert out["next_open_gap"]["n"] == 1
    assert out["next_open_gap"]["mean"] == pytest.approx(0.0125, abs=1e-4)


def test_an_unknown_seat_lists_zero_appearances_rather_than_an_empty_result():
    desk = _desk(_seat_frames())

    out = tape.seat_record(desk, "不存在的席位", day="20261104")

    assert out["ok"] is True
    assert out["appearances"] == 0
    assert "一次都没上榜" in out["note"]


def test_anomaly_always_carries_the_caveat_that_it_is_not_a_filing():
    """上游是机器写的散文。引用成公告是最贵的一种误读，所以提醒必须跟着数据走。"""
    frame = pd.DataFrame([
        {"ts_code": "000001.SZ", "name": "平安银行", "tag": "涨停",
         "keywords": "银行,息差", "analysis": "受行业消息影响" * 40},
        {"ts_code": "600000.SH", "name": "浦发银行", "tag": "大涨",
         "keywords": "银行", "analysis": "资金推动"},
    ])
    desk = _desk({"ths_anomaly": frame})

    out = tape.anomaly(desk, day="20261104")

    assert out["ok"] is True
    assert out["count"] == 2
    assert out["by_tag"] == {"涨停": 1, "大涨": 1}
    assert any("不是公告原文" in c for c in out["caveats"])
    # 列表里每条 analysis 截到 200 字，避免一条把整屏吃掉
    assert all(len(i["analysis"]) <= 201 for i in out["items"])


def test_anomaly_for_one_symbol_returns_that_row_or_says_it_did_not_fire():
    frame = pd.DataFrame([
        {"ts_code": "000001.SZ", "name": "平安银行", "tag": "涨停",
         "keywords": "银行", "analysis": "受行业消息影响"},
    ])
    desk = _desk({"ths_anomaly": frame})

    hit = tape.anomaly(desk, day="20261104", symbol="000001.SZ")
    miss = tape.anomaly(desk, day="20261104", symbol="600000.SH")

    assert hit["items"][0]["tag"] == "涨停"
    assert miss["items"] == []
    assert "没有异动归因" in miss["note"]


def test_without_a_calendar_the_guard_degrades_and_says_so():
    """日历取不到时退回自然日口径，T+2 那一笔会混进来。

    这不是"可接受的近似"，是一条已知会算错的退路。留着它的理由是：没有日历
    时另一条路是返回空，而空结果会让读者以为"这个席位没上过榜"。所以退路保留，
    但必须自报家门——`gap_basis` 就是那句话。
    """
    frames = _seat_frames()
    kept = frames["daily"][
        ~((frames["daily"]["ts_code"] == "000001.SZ")
          & (frames["daily"]["trade_date"] == "20261104"))
    ]
    frames["daily"] = pd.concat([kept, pd.DataFrame([
        {"ts_code": "000001.SZ", "trade_date": "20261105", "open": 11.0, "close": 11.2},
    ])], ignore_index=True)
    del frames["trade_cal"]          # 日历缺失
    desk = _desk(frames)

    out = tape.seat_record(desk, "T王", day="20261104")

    assert out["gap_basis"] == "calendar_days<=7", "退路要标出来"
    assert out["next_open_gap"]["n"] == 2, "自然日口径挡不住 11-03 → 11-05 这两天跨度"


def _auction_frames():
    auction_rows = pd.DataFrame([
        {"ts_code": "000001.SZ", "name": "平安银行", "warn_name": "竞价抢筹",
         "bid_pct": 3.2, "bid_amount": 90_000_000.0, "previous_amount": 30_000_000.0},
        {"ts_code": "600000.SH", "name": "浦发银行", "warn_name": "竞价砸盘",
         "bid_pct": -2.1, "bid_amount": 40_000_000.0, "previous_amount": 25_000_000.0},
        # 同一只票触发两种告警：by_type 会重复计数，这是已知的口径
        {"ts_code": "000001.SZ", "name": "平安银行", "warn_name": "大幅低开",
         "bid_pct": -1.0, "bid_amount": 10_000_000.0, "previous_amount": 5_000_000.0},
    ])
    limit_up = pd.DataFrame([{"ts_code": "000001.SZ"}])
    return {"ths_auction": auction_rows, "ths_limit_up": limit_up}


def test_auction_separates_grabbers_from_dumpers_and_flags_the_crossover():
    """竞价抢筹、竞价砸盘是两个方向的问题，混在一个列表里就没法读。

    交叉表才是重点：09:25 被抢筹、15:00 还在涨停池里的票，和抢了但没封住的
    是两种东西。所以 `grabbed_then_sealed` 只说"进了池"，不说"收盘封住"。
    """
    desk = _desk(_auction_frames())

    out = tape.auction(desk, day="20261104")

    assert out["ok"] is True
    assert out["alerts"] == 3, "告警条数按行算"
    assert out["by_type"]["竞价抢筹"] == 1
    assert [r["ts_code"] for r in out["grabbed_top"]] == ["000001.SZ"]
    assert [r["ts_code"] for r in out["dumped_top"]] == ["600000.SH"]
    assert out["grabbed_then_sealed"]["count"] == 1
    assert out["grabbed_then_sealed"]["symbols"] == ["000001.SZ"]
    assert "不等于收盘仍封住" in out["grabbed_then_sealed"]["note"]
    assert out["grabbed_top"][0]["amount_to_previous"] == 3.0
    assert out["grabbed_top"][0]["event_count"] is None
    assert out["capture"]["raw_events"] is None


def test_auction_exposes_repeat_process_and_capture_completeness():
    frames = _auction_frames()
    auction = frames["ths_auction"].copy()
    auction["event_count"] = [2, 1, 1]
    auction["first_warn_at"] = ["2026-11-04T09:25:01+08:00"] * 3
    auction["last_warn_at"] = ["2026-11-04T09:25:14+08:00"] * 3
    auction["capture_complete"] = True
    auction["capture_truncated"] = False
    frames["ths_auction"] = auction

    out = tape.auction(_desk(frames), day="20261104")

    assert out["capture"] == {
        "complete": True, "truncated": False, "raw_events": 4,
        "repeated_alert_groups": 1,
        "note": "旧分区没有重复过程字段时对应值为 null，不按 1 次或完整覆盖补写",
    }
    assert out["grabbed_top"][0]["event_count"] == 2
    assert out["grabbed_top"][0]["last_warn_at"].endswith("09:25:14+08:00")


def test_auction_says_sealed_is_unknown_when_the_pool_is_missing():
    """涨停池没数据时 `sealed` 必须是 null，不能是 false。

    false 读作"没封住"，而真相是"不知道"——两个结论相反。
    """
    frames = _auction_frames()
    del frames["ths_limit_up"]
    desk = _desk(frames)

    out = tape.auction(desk, day="20261104")

    assert out["grabbed_top"][0]["sealed"] is None
    assert out["grabbed_then_sealed"]["count"] == 0


# ---------------------------------------------------------------- 取不到数时
#
# 下面几条走的是"供应商没给数据"的分支。它们在生产里比正常分支更常被命中：
# fetch_ths_special 没跑、接口当天没返回、盘中查当天。每一条都必须给出
# **能照着做的下一步**，而不是一个空列表。

def _desk_no_bars(frames) -> SimpleNamespace:
    return SimpleNamespace(data=FakeData(frames),
                           market=SimpleNamespace(latest_bar_date=lambda: None))


def test_every_ths_tool_refuses_to_invent_a_date():
    """没有 bar 又没给日期时，三个工具都要停下来，不能拿"今天"顶上。

    异动归因是个例外——它只返回当日，所以用日历日期是对的，见下一条。
    """
    empty = _desk_no_bars({})

    assert tape.lhb(empty)["ok"] is False
    assert tape.seat_record(empty, "T王")["ok"] is False
    assert tape.auction(empty)["ok"] is False


def test_auction_and_anomaly_name_the_fetch_command_when_empty():
    """空表是"还没抓"，不是"今天没有异动"。对一个交易者这是两个结论。"""
    desk = _desk({"ths_auction": pd.DataFrame(), "ths_anomaly": pd.DataFrame()})

    auction = tape.auction(desk, day="20261104")
    anomaly = tape.anomaly(desk, day="20261104")

    assert auction["ok"] is False
    assert "fetch_ths_special" in auction["error"]
    assert anomaly["ok"] is False
    assert "只返回当日" in anomaly["error"]


def test_lhb_top_seats_are_aggregated_per_seat_and_scoped_to_the_symbol():
    """`top_seats` 是当日席位净买额前五。给了 symbol 就只看这只票的席位，
    否则会把全市场席位混进来——那回答的是"今天谁在买"，不是"谁在买这只"。"""
    frames = {
        "ths_lhb": _lhb_frame(),
        "ths_lhb_seat": pd.DataFrame([
            {"trade_date": "20261103", "ts_code": "600000.SH", "seat": "T王",
             "net_value": 20_000_000.0},
            {"trade_date": "20261103", "ts_code": "600000.SH", "seat": "T王",
             "net_value": 5_000_000.0},
            {"trade_date": "20261103", "ts_code": "600000.SH", "seat": "机构专用",
             "net_value": 50_000_000.0},
        ]),
    }
    desk = _desk(frames)

    whole = tape.lhb(desk, day="20261104")
    one = tape.lhb(desk, day="20261104", symbol="600000.SH")

    assert whole["top_seats"][0] == {"seat": "机构专用", "net_value_cny": 50_000_000.0}
    # 同一席位两笔要合并
    assert {"seat": "T王", "net_value_cny": 25_000_000.0} in whole["top_seats"]
    assert one["top_seats"][0]["seat"] == "机构专用", "限定标的后席位仍按净额排"


def test_asking_for_a_seat_record_through_lhb_attaches_it_to_the_board():
    """`seat=` 是"榜面引出但从不回答的问题"，所以它随榜一起返回，不是另一趟。

    这条也钉住 `if seat:` 那个分支：不给就不算，给了才多读 60 个交易日。
    """
    frames = _seat_frames()
    frames["ths_lhb"] = _lhb_frame()
    desk = _desk(frames)

    without = tape.lhb(desk, day="20261104")
    with_seat = tape.lhb(desk, day="20261104", seat="T王")

    assert "seat_record" not in without, "没问就不要多读那 60 个交易日"
    assert with_seat["seat_record"]["seat"] == "T王"
    assert with_seat["seat_record"]["appearances"] == 2


def test_auction_returns_an_empty_side_rather_than_omitting_it():
    """只有砸盘、没有抢筹的一天，`grabbed_top` 是空列表而不是缺字段。

    缺字段会让读者以为工具没实现这一侧；空列表说的是"今天没有这一侧"。
    """
    frame = pd.DataFrame([
        {"ts_code": "600000.SH", "name": "浦发银行", "warn_name": "竞价砸盘",
         "bid_pct": -2.1, "bid_amount": 40_000_000.0, "previous_amount": 25_000_000.0},
    ])
    desk = _desk({"ths_auction": frame})

    out = tape.auction(desk, day="20261104")

    assert out["grabbed_top"] == []
    assert len(out["dumped_top"]) == 1
