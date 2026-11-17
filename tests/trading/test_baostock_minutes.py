"""baostock 适配器：代码映射、单位归一、预算计数，以及"预算用完就别再问"。

这几条都在防同一类错：**看起来抓到了，其实口径不对**。成交量少乘一个 100，总量和 VWAP
就全错；时间戳不转成同一格式，同一张表里两种时间，切片和排序都会错；预算不挡，
跑到一半被拉黑，第二天还是不能用。

网络不参与：`fetch_range` 里是 `import baostock`（调用时），测试把它换成假的。
"""

from __future__ import annotations

import sys
import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from service.adapters import baostock_minutes as mod


def test_the_code_prefix_is_the_whole_protocol():
    assert mod.to_bs("000001.SZ") == "sz.000001"
    assert mod.to_bs("600519.SH") == "sh.600519"
    assert mod.to_bs("430047.BJ") is None, "北交所没有 baostock 代码，问也是错"
    assert mod.from_bs("sz.000001") == "000001.SZ"


def test_the_17_digit_stamp_becomes_the_same_shape_as_tencent_rows():
    """同一条记录两个源写进去，`trade_time` 必须是同一种字符串，否则排序和切片会错。"""
    day, when = mod._stamp("20261030093500000")

    assert day == "20261030"
    assert when == "2026-10-30 09:35"
    assert mod._stamp("garbage") is None


def test_volume_arrives_in_shares_and_is_stored_in_lots():
    """baostock 给股，`stk_mins` 存手，`profile` 的 VWAP 按"手 × 100 = 股"算。"""
    rows = [["2026-10-30", "20261030093500000", "11.53", "11.46", "11.56", "11.45",
             "19748200", "226980324.0"]]

    frame = mod._frame(rows, "000001.SZ")

    assert frame["vol"].iloc[0] == pytest.approx(197482.0)
    assert frame["amount"].iloc[0] == pytest.approx(226980324.0)
    assert frame["source"].iloc[0] == "baostock"
    assert list(frame.columns) == mod.COLUMNS


def test_a_blank_field_becomes_null_not_zero():
    rows = [["2026-10-30", "20261030093500000", "11.53", "11.46", "11.56", "11.45",
             "19748200", ""]]

    frame = mod._frame(rows, "000001.SZ")

    assert pd.isna(frame["amount"].iloc[0]), "空≠0：0 会被 VWAP 当成真的成交量"


def test_the_budget_counts_per_day_and_survives_a_restart(tmp_path):
    path = tmp_path / "budget.json"
    first = mod.Budget(path)
    first.spend(5)
    first.spend(2)

    again = mod.Budget(path)

    assert again.used() == 7, "重启不清零，因为提供方记得"
    assert again.remaining() == mod.BS_USABLE - 7
    assert again.used("20261101") == 0, "换一天从零开始"


def test_the_budget_stops_at_the_cap_not_past_it(tmp_path):
    budget = mod.Budget(tmp_path / "b.json")
    budget.spend(mod.BS_USABLE)

    assert budget.remaining() == 0
    assert budget.remaining() >= 0, "不能是负数——那会让下一步的算术看起来还能跑"


# ---------------------------------------------------------------- 无网络路径
class _Result:
    def __init__(self, rows, error="0"):
        self._rows = rows
        self._at = -1
        self.error_code = error
        self.error_msg = "" if error == "0" else "boom"

    def next(self):
        self._at += 1
        return self._at < len(self._rows)

    def get_row_data(self):
        return self._rows[self._at]


class _FakeBS:
    def __init__(self, rows, error="0"):
        self.rows = rows
        self.error = error
        self.calls = 0
        self.util = SimpleNamespace(socketutil=SimpleNamespace(send_msg=lambda _msg: None))

    def login(self):
        return _Result([], "0")

    def logout(self):
        return None

    def query_history_k_data_plus(self, code, fields, **kwargs):
        self.calls += 1
        return _Result(self.rows if self.error == "0" else [], self.error)


@pytest.fixture()
def fake_bs(monkeypatch):
    def install(rows, error="0"):
        fake = _FakeBS(rows, error)
        monkeypatch.setitem(sys.modules, "baostock", fake)
        return fake
    return install


ONE_BAR = [["2026-10-30", "20261030093500000", "11.53", "11.46", "11.56", "11.45",
            "19748200", "226980324.0"]]


def test_a_beijing_name_is_skipped_without_spending_a_request(tmp_path, fake_bs):
    fake = fake_bs(ONE_BAR)
    budget = mod.Budget(tmp_path / "b.json")

    out = mod.fetch_range(["430047.BJ"], "2026-10-30", "2026-10-30", budget=budget)

    assert out[0]["why"] == "unsupported"
    assert fake.calls == 0, "不该为一个注定失败的代码花一次请求"
    assert budget.used() == 0


def test_the_budget_runs_out_for_the_rest_not_for_the_whole_run(tmp_path, fake_bs):
    """预算见底时剩下的票标记 budget，而不是抛异常中断——已经抓到的那些还等着落盘。"""
    fake = fake_bs(ONE_BAR)
    budget = mod.Budget(tmp_path / "b.json")
    budget.spend(mod.BS_USABLE - 1)

    out = mod.fetch_range(["000001.SZ", "000002.SZ", "600519.SH"],
                          "2026-10-30", "2026-10-30", budget=budget)

    assert [item["why"] for item in out] == ["ok", "budget", "budget"]
    assert fake.calls == 1


def test_an_error_is_not_reported_as_an_empty_day(tmp_path, fake_bs):
    """`error:*` 与 `no_bars` 必须分开：前者是源出问题，后者是那天真没交易。"""
    fake_bs([], error="10004011")
    budget = mod.Budget(tmp_path / "b.json")

    out = mod.fetch_range(["000001.SZ"], "2026-10-30", "2026-10-30", budget=budget)

    assert out[0]["why"] == "error:10004011"
    assert budget.used() == 1, "失败也是问过了，要算进预算"


def test_a_suspended_name_is_no_bars_not_an_error(tmp_path, fake_bs):
    fake_bs([])
    budget = mod.Budget(tmp_path / "b.json")

    out = mod.fetch_range(["000001.SZ"], "2026-10-30", "2026-10-30", budget=budget)

    assert out[0]["why"] == "no_bars"


def test_a_network_timeout_preserves_prior_successes_and_marks_the_rest_aborted(
        tmp_path, monkeypatch):
    class TimeoutBS(_FakeBS):
        def __init__(self):
            super().__init__(ONE_BAR)
            self.timeouts = []
            self.logged_out = False

        def login(self):
            self.timeouts.append(socket.getdefaulttimeout())
            return super().login()

        def query_history_k_data_plus(self, code, fields, **kwargs):
            self.timeouts.append(socket.getdefaulttimeout())
            self.calls += 1
            if self.calls == 2:
                raise TimeoutError("upstream stalled")
            return _Result(self.rows, "0")

        def logout(self):
            self.logged_out = True

    fake = TimeoutBS()
    monkeypatch.setitem(sys.modules, "baostock", fake)
    previous = socket.getdefaulttimeout()
    original_send = fake.util.socketutil.send_msg
    budget = mod.Budget(tmp_path / "b.json")
    beats = []

    out = mod.fetch_range(
        ["000001.SZ", "000002.SZ", "600519.SH"], "2026-10-30", "2026-10-30",
        budget=budget, request_timeout=0.25,
        on_symbol=lambda index, total, record: beats.append((index, record["why"])))

    assert [item["why"] for item in out] == ["ok", "error:timeout", "aborted"]
    assert out[0]["frame"] is not None and out[1]["frame"] is None
    assert budget.used() == 2, "超时的那次已经问过，未尝试的第三只不能计预算"
    assert fake.timeouts == [0.25, 0.25, 0.25]
    assert fake.logged_out is True
    assert fake.util.socketutil.send_msg is original_send, "第三方原函数也必须恢复"
    assert socket.getdefaulttimeout() == previous, "异常路径也必须恢复进程原设置"
    assert beats == [(1, "ok"), (2, "error:timeout"), (3, "aborted")]


def test_a_streaming_timeout_discards_partial_symbol_rows(tmp_path, monkeypatch):
    class StreamingTimeoutResult(_Result):
        def next(self):
            if self._at >= 0:
                raise TimeoutError("stream stalled after one row")
            self._at += 1
            return True

    class StreamingTimeoutBS(_FakeBS):
        def query_history_k_data_plus(self, code, fields, **kwargs):
            self.calls += 1
            return StreamingTimeoutResult(self.rows, "0")

    fake = StreamingTimeoutBS(ONE_BAR)
    monkeypatch.setitem(sys.modules, "baostock", fake)
    previous = socket.getdefaulttimeout()

    out = mod.fetch_range(["000001.SZ"], "2026-10-30", "2026-10-30",
                          budget=mod.Budget(tmp_path / "b.json"), request_timeout=1.0)

    assert out[0]["why"] == "error:timeout"
    assert out[0]["frame"] is None, "半份历史落库会被 coverage 当成完整，必须丢弃"
    assert socket.getdefaulttimeout() == previous


def _socket_util(fake_socket):
    return SimpleNamespace(
        context=SimpleNamespace(default_socket=fake_socket),
        cons=SimpleNamespace(MESSAGE_HEADER_LENGTH=3, MESSAGE_SPLIT=",",
                             COMPRESSED_MESSAGE_TYPE_TUPLE=()),
        zlib=None,
    )


def test_strict_socket_send_uses_sendall_and_reads_to_the_terminator():
    class Socket:
        def __init__(self):
            self.sent = b""
            self.chunks = [b"0,1,0pay", b"load<![CDATA[]]>\n"]

        def sendall(self, payload):
            self.sent += payload

        def recv(self, _size):
            return self.chunks.pop(0)

    fake_socket = Socket()

    out = mod._strict_send_msg("request", _socket_util(fake_socket))

    assert fake_socket.sent == b"request\n"
    assert out == "0,1,0payload<![CDATA[]]>\n"


def test_strict_socket_recv_never_spins_on_close_and_propagates_timeout():
    class ClosedSocket:
        def sendall(self, _payload):
            return None

        def recv(self, _size):
            return b""

    class TimeoutSocket(ClosedSocket):
        def recv(self, _size):
            raise TimeoutError("recv timed out")

    with pytest.raises(ConnectionError, match="closed"):
        mod._strict_send_msg("request", _socket_util(ClosedSocket()))
    with pytest.raises(TimeoutError):
        mod._strict_send_msg("request", _socket_util(TimeoutSocket()))


def test_a_library_level_network_error_aborts_the_remaining_connection(
        tmp_path, fake_bs):
    fake = fake_bs([], error="10002007")
    budget = mod.Budget(tmp_path / "b.json")

    out = mod.fetch_range(["000001.SZ", "000002.SZ", "600519.SH"],
                          "2026-10-30", "2026-10-30", budget=budget)

    assert [item["why"] for item in out] == ["error:10002007", "aborted", "aborted"]
    assert fake.calls == 1, "断开的连接不能继续对剩余股票逐个空问"
    assert budget.used() == 1


def test_logout_failure_preserves_frames_and_still_closes_the_socket(
        tmp_path, monkeypatch):
    class LogoutFailureBS(_FakeBS):
        def logout(self):
            raise ConnectionError("logout connection lost")

    class Socket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = LogoutFailureBS(ONE_BAR)
    fake_socket = Socket()
    fake.util.socketutil.context = SimpleNamespace(default_socket=fake_socket)
    monkeypatch.setitem(sys.modules, "baostock", fake)
    original_send = fake.util.socketutil.send_msg

    out = mod.fetch_range(["000001.SZ"], "2026-10-30", "2026-10-30",
                          budget=mod.Budget(tmp_path / "b.json"))

    assert out[0]["why"] == "ok" and out[0]["rows"] == 1
    assert fake_socket.closed is True
    assert fake.util.socketutil.send_msg is original_send
