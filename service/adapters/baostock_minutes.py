"""5-minute bars from baostock: one connection, one query per symbol, a daily cap.

baostock 是这里最好的分钟线源——**能按日期区间查**，所以历史是补出来的，不是攒出来的：

| 源 | 5 分钟历史 | 成交额 | 复权 |
|---|---|---|---|
| baostock | 至少回到 2023（实测 44,400 根/票） | 有 | 1/2/3 三档 |
| 东财 `push2his` | ~32 个交易日 | 有 | 不复权 |
| 腾讯 `ifzq` | 320 根 ≈ 7 个交易日 | 无 | 不复权 |

代价是两条硬规则，违反就不是慢，是被拉黑：

1. **每日 API 请求不超过 50,000 次**，超了进黑名单。这条决定了调用形状：
   一次 `query_history_k_data_plus` 拉**一个代码的一整段区间**，而不是一天一天拉。
   全市场一天 = 5,571 次请求；全市场 20 天也还是 5,571 次——行数是流式读的，
   时间随行数涨，请求数不涨。
2. **不能并发连接**。所以本模块只持有一个 login，串行跑完；`intraday_sweep.py`
   另加进程锁，防止两次运行撞在一起。

两个源的数据不能混着看，落库前要归一：

* **成交量单位**：baostock 给的是**股**，而 `stk_mins` 存的是**手**（腾讯的口径），
  `profile` 的 VWAP 正是按"手 × 100 = 股"算的。差 100 倍混进同一张表，总量和 VWAP 全错。
* **bar 边界**：同一天 09:35 那根，腾讯收 11.45、baostock 收 11.46——两家切分不同，
  不是谁错了。所以一个 symbol-day 只该有一个源；`source` 逐行写着。

请求预算落在 `data/baostock_budget.json`：按日期计数，重启不清零。留 20% 余量，
因为重试和别的手工查询也要算进去。
"""

from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

#: 提供方的硬上限（2026-11 的规则）。
BS_DAILY_CAP = 50_000
#: 只用八成：重试、手工查询、以及"今天已经用过多少"都要算在同一天里。
BS_USABLE = int(BS_DAILY_CAP * 0.8)

COLUMNS = ["ts_code", "trade_time", "trade_date", "open", "close", "high", "low",
           "vol", "amount", "source"]


def to_bs(symbol: str) -> str | None:
    """`000001.SZ` → `sz.000001`. 北交所没有 baostock 代码（实测报"代码未标识sh或sz"）。"""
    code, _, suffix = symbol.partition(".")
    suffix = suffix.upper()
    if suffix == "SH":
        return f"sh.{code}"
    if suffix == "SZ":
        return f"sz.{code}"
    return None


def from_bs(code: str) -> str:
    prefix, _, digits = code.partition(".")
    return f"{digits}.{prefix.upper()}"


def _stamp(raw: str) -> tuple[str, str] | None:
    """`20261030093500000` → (`20261030`, `2026-10-30 09:35`).

    17 位：8 位日期 + 6 位时分秒 + 3 位毫秒。存成和腾讯那套一样的
    `YYYY-MM-DD HH:MM`，否则同一张表里两种时间格式，排序和切片都会错。
    """
    text = str(raw)
    if len(text) < 12 or not text.isdigit():
        return None
    day = text[:8]
    return day, f"{day[:4]}-{day[4:6]}-{day[6:8]} {text[8:10]}:{text[10:12]}"


@dataclass
class Budget:
    """当天已用多少次请求，按日期落盘。重启不清零，因为提供方记得。"""

    path: Path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def used(self, today: str | None = None) -> int:
        today = today or datetime.now().strftime("%Y%m%d")
        return int(self._load().get(today, 0))

    def remaining(self, today: str | None = None) -> int:
        return max(BS_USABLE - self.used(today), 0)

    def spend(self, count: int = 1, today: str | None = None) -> None:
        today = today or datetime.now().strftime("%Y%m%d")
        state = self._load()
        state[today] = int(state.get(today, 0)) + count
        # 只留最近几天：这是一份计数器，不是账本。
        for stale in sorted(state)[:-7]:
            del state[stale]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _frame(rows: list[list[str]], symbol: str) -> pd.DataFrame:
    out = []
    for row in rows:
        stamp = _stamp(row[1])
        if stamp is None:
            continue
        day, when = stamp
        out.append({
            "ts_code": symbol,
            "trade_time": when,
            "trade_date": day,
            "open": _num(row[2]),
            "close": _num(row[3]),
            "high": _num(row[4]),
            "low": _num(row[5]),
            # 股 → 手：`stk_mins` 全表是手，VWAP 按"手 × 100 = 股"算。
            "vol": None if _num(row[6]) is None else _num(row[6]) / 100.0,
            "amount": _num(row[7]),
            "source": "baostock",
        })
    return pd.DataFrame(out, columns=COLUMNS)


def _num(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _network_why(exc: OSError) -> str:
    if isinstance(exc, TimeoutError):
        return "error:timeout"
    return f"error:network:{type(exc).__name__}"


def _strict_send_msg(msg: str, socketutil) -> str:
    """baostock 的可靠收发边界：不吞异常，也不对关闭连接空转。

    第三方 ``send_msg`` 会捕获所有异常并返回 None；分页 ``ResultData.next``
    随后可能把 None 当成正常结束。这里保留其线上协议，但让超时/断开向上
    传递，并用 sendall 防止短写。仅在本模块一次 fetch 生命周期内临时安装。
    """
    default_socket = getattr(socketutil.context, "default_socket", None)
    if default_socket is None:
        raise ConnectionError("baostock socket is not connected")
    default_socket.sendall((msg + "\n").encode("utf-8"))
    receive = bytearray()
    delimiter = b"<![CDATA[]]>\n"
    while not receive.endswith(delimiter):
        chunk = default_socket.recv(8192)
        if not chunk:
            raise ConnectionError("baostock connection closed before message terminator")
        receive.extend(chunk)
    cons = socketutil.cons
    head_text = bytes(receive[:cons.MESSAGE_HEADER_LENGTH]).decode()
    head_parts = head_text.split(cons.MESSAGE_SPLIT)
    if head_parts[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
        inner_length = int(head_parts[2])
        start = cons.MESSAGE_HEADER_LENGTH
        body = socketutil.zlib.decompress(receive[start:start + inner_length]).decode()
        return head_text + body
    return bytes(receive).decode()


_NETWORK_ERROR_CODES = frozenset({
    "10001001",  # no login: the current connection cannot continue
    "10002001", "10002002", "10002003", "10002004",
    "10002005", "10002006", "10002007", "10002008",
})


def fetch_range(symbols: list[str], start: str, end: str, *,
                budget: Budget, freq: str = "5", adjust: str = "3",
                on_symbol=None, request_timeout: float | None = 120.0) -> list[dict]:
    """One login, one query per symbol, sequential — the shape the rules require.

    ``start``/``end`` are ``YYYY-MM-DD``. Returns one record per symbol:
    ``{"symbol", "rows", "frame"|None, "why"}``. ``why`` is ``ok`` or the reason
    there is nothing to store, and the two cases are kept apart on purpose:
    ``no_bars`` (suspended, or the name did not trade) is not ``unsupported``
    (北交所) and neither is ``budget``.

    baostock exposes no per-request timeout. Its blocking socket therefore gets a
    bounded default timeout, restored on every exit. A timeout during streaming
    discards that symbol's partial rows, returns earlier successes to the caller,
    and marks unattempted names ``aborted`` so a later coverage sweep can resume.
    """
    import baostock as bs

    fields = "date,time,open,close,high,low,volume,amount"
    out: list[dict] = []

    def append(index: int, record: dict) -> None:
        out.append(record)
        if on_symbol is not None:
            on_symbol(index, len(symbols), record)

    def abort(index: int, symbol: str, why: str) -> None:
        append(index, {"symbol": symbol, "rows": 0, "frame": None, "why": why})
        for rest_at in range(index + 1, len(symbols) + 1):
            append(rest_at, {"symbol": symbols[rest_at - 1], "rows": 0,
                             "frame": None, "why": "aborted"})

    socketutil = bs.util.socketutil
    original_send_msg = socketutil.send_msg
    socketutil.send_msg = lambda msg: _strict_send_msg(msg, socketutil)
    previous_timeout = socket.getdefaulttimeout()
    if request_timeout is not None and request_timeout > 0:
        socket.setdefaulttimeout(float(request_timeout))
    try:
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
        try:
            for index, symbol in enumerate(symbols, start=1):
                code = to_bs(symbol)
                if code is None:
                    append(index, {"symbol": symbol, "rows": 0, "frame": None,
                                   "why": "unsupported"})
                    continue
                if budget.remaining() <= 0:
                    append(index, {"symbol": symbol, "rows": 0, "frame": None,
                                   "why": "budget"})
                    continue
                try:
                    result = bs.query_history_k_data_plus(
                        code, fields, start_date=start, end_date=end,
                        frequency=freq, adjustflag=adjust)
                except (TimeoutError, OSError) as exc:
                    budget.spend(1)  # the request was attempted, even without a response
                    abort(index, symbol, _network_why(exc))
                    break
                budget.spend(1)
                rows: list[list[str]] = []
                try:
                    while result.error_code == "0" and result.next():
                        rows.append(result.get_row_data())
                except (TimeoutError, OSError) as exc:
                    # Never persist half a symbol-range and mistake it for full coverage.
                    abort(index, symbol, _network_why(exc))
                    break
                if result.error_code in _NETWORK_ERROR_CODES:
                    abort(index, symbol, f"error:{result.error_code}")
                    break
                if result.error_code != "0":
                    record = {"symbol": symbol, "rows": 0, "frame": None,
                              "why": f"error:{result.error_code}"}
                elif not rows:
                    record = {"symbol": symbol, "rows": 0, "frame": None,
                              "why": "no_bars"}
                else:
                    frame = _frame(rows, symbol)
                    record = {"symbol": symbol, "rows": len(frame), "frame": frame,
                              "why": "ok"}
                append(index, record)
        finally:
            try:
                bs.logout()
            except (TimeoutError, OSError):
                pass  # logout failure must not discard already fetched frames
    finally:
        context = getattr(socketutil, "context", None)
        default_socket = getattr(context, "default_socket", None)
        if default_socket is not None:
            try:
                default_socket.close()
            except OSError:
                pass
        socketutil.send_msg = original_send_msg
        socket.setdefaulttimeout(previous_timeout)
    return out
