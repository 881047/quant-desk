"""The paper book's watchdog: the captured frame is the frame it judges.

``tripwire`` answers "did anything cross a threshold". This module exists so that
answer arrives while the model is asleep — and arrives *once*: a breach that lasts
all afternoon is one line, not two hundred.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.notify import alert_log
from service.trading import watch
from tests.trading.helpers import pick_symbols, proposal


def _moment(day: str, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.strptime(day, "%Y%m%d").replace(
        hour=hour, minute=minute, second=second, tzinfo=SHANGHAI)


@pytest.fixture
def day(desk) -> str:
    return desk.market.latest_bar_date()


@pytest.fixture
def held(desk) -> tuple[str, Decimal]:
    """One filled position and the stop its ticket came with."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")
    return symbol, Decimal(str(ticket["stop_price"]))


def _frame(tmp_path: Path, *, day: str, at: datetime, symbol: str, last: float,
           pre_close: float | None = None) -> Path:
    """One archived frame with a single name — the shape the capture writes.

    ``pre_close`` defaults to ``last`` so ``pct_chg`` is zero and the only finding
    under test is the stop, not a five-percent move that happens to travel with it.
    """
    directory = tmp_path / "rt_snapshots" / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{at.strftime('%H%M%S')}_000001.parquet"
    pd.DataFrame([{
        "code": symbol.split(".")[0],
        "market": 1 if symbol.endswith(".SH") else 0,
        "name": symbol,
        "last": last,
        "pre_close": last if pre_close is None else pre_close,
        "open": last, "high": last, "low": last,
        "volume": 100_000, "amount": 1_000_000.0,
        "quote_ts": at.timestamp(),
        "source": "eastmoney",
    }]).to_parquet(path, index=False)
    return path


def _alerts(tmp_path: Path, day: str) -> list[dict]:
    path = alert_log.alerts_path(day, tmp_path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_breach_on_the_captured_frame_is_written_once(desk, held, day, tmp_path):
    symbol, stop = held
    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol,
                  last=float(stop) * 0.97)

    first = watch.run(desk, path, day=day, now=_moment(day, 10, 30), base=tmp_path)
    again = watch.run(desk, path, day=day, now=_moment(day, 10, 30, 30), base=tmp_path)

    assert [row["rule"] for row in first["fired"]] == ["stop_breach"]
    assert again["fired"] == []
    assert again["open"] == 1
    assert [row["event"] for row in _alerts(tmp_path, day)] == ["fired"], "一帧一行，不是一秒一行"


def test_a_frame_that_recovers_clears_after_the_grace(desk, held, day, tmp_path):
    """破线之后回到止损上方：先记着，两分钟确认了才写解除。

    停损价常压在整数关口上，价格在它旁边抖一帧不是"解除了又破了"；但一直
    不写解除，13:35 醒来的模型会去卖一个 10:14 就已经好了的仓位。
    """
    symbol, stop = held
    breach = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol,
                    last=float(stop) * 0.97)
    watch.run(desk, breach, day=day, now=_moment(day, 10, 30), base=tmp_path)

    quiet_at = _moment(day, 10, 31)
    quiet = _frame(tmp_path, day=day, at=quiet_at, symbol=symbol,
                   last=float(stop) * 1.10)
    flapping = watch.run(desk, quiet, day=day, now=quiet_at, base=tmp_path)
    assert flapping["cleared"] == []
    assert flapping["open"] == 1

    still_at = _moment(day, 10, 34)
    settled = watch.run(
        desk,
        _frame(tmp_path, day=day, at=still_at, symbol=symbol, last=float(stop) * 1.10),
        day=day, now=still_at, base=tmp_path,
    )
    assert [row["event"] for row in settled["cleared"]] == ["cleared"]
    assert settled["open"] == 0


def test_a_stale_capture_is_reported_as_unmeasured_not_as_a_breach(desk, held, day, tmp_path):
    """抓取停了 30 分钟：那 30 分钟前的价格不能拿来报"已跌破"。

    没测到就是没测到。读者看到的是 `quotes_unavailable`，而不是一个把旧事实
    说成现在的破线——这正是 tripwire 那条"算不出来要说未知，不能说没事"。
    """
    symbol, stop = held
    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol,
                  last=float(stop) * 0.97)

    shot = watch.run(desk, path, day=day, now=_moment(day, 11, 0), base=tmp_path)

    codes = {row["code"] for row in shot["findings"]}
    assert "quotes_unavailable" in codes
    assert "stop_breach" not in codes
    assert shot["live_quotes"] == 0


def test_an_empty_book_writes_no_alert_file_but_still_beats(desk, day, tmp_path):
    """空仓一整天：日志文件不该出现，心跳必须有。"""
    symbol = pick_symbols(desk, 1)[0]
    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol, last=9.0)

    shot = watch.run(desk, path, day=day, now=_moment(day, 10, 30), base=tmp_path)

    assert shot["findings"] == []
    assert shot["open"] == 0
    assert not alert_log.alerts_path(day, tmp_path).exists()
    heart = json.loads(alert_log.heartbeat_path(day, tmp_path).read_text(encoding="utf-8"))
    assert heart["outcome"] == "ok"


def test_the_watch_never_calls_a_vendor(desk, held, day, tmp_path, monkeypatch):
    """价必须来自那一帧。再问一次厂商，同一个瞬间就有了两个"现价"。"""
    symbol, stop = held
    monkeypatch.setattr(desk, "quotes",
                        lambda *_args, **_kwargs: pytest.fail("watch must not fetch"))
    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol,
                  last=float(stop) * 0.97)

    shot = watch.run(desk, path, day=day, now=_moment(day, 10, 30), base=tmp_path)

    assert shot["quotes"] == 1
    assert shot["live_quotes"] == 1


# ---- 脚本侧：看盘失败不能弄死抓取，但必须留下痕迹 -----------------------------


def test_the_watch_reads_the_book_as_it_is_now_not_as_it_was_at_boot(desk, day, tmp_path):
    """巡检进程 09:15 启动，那笔买入是 09:26 在另一个进程里成交的。

    `PaperBook` 只在**写冲突**时自己重读。一个只读的看守不写，于是它的账本永远
    停在开盘前——每天下午都报「无持仓」的哨兵，比没有哨兵更糟：它看起来在工作。
    """
    from service.trading import build_desk

    symbol = pick_symbols(desk, 1)[0]
    loop_desk = build_desk(market=desk.market, paper_db=desk.paper.db_path,
                           initial_cash="5000")
    assert loop_desk.book.summary()["positions"] == [], "这一份是开盘前建的"

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")
    assert desk.book.summary()["positions"], "写者看得见这笔买入"
    stop = Decimal(str(ticket["stop_price"]))

    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol,
                  last=float(stop) * 0.97)
    shot = watch.run(loop_desk, path, day=day, now=_moment(day, 10, 30), base=tmp_path)

    assert [row["rule"] for row in shot["fired"]] == ["stop_breach"]


def test_a_run_writes_all_three_files_into_the_same_directory(desk, day, tmp_path, monkeypatch):
    """一次运行写三份文件：日志、心跳、状态。心跳写到别处 = 巡检永远"没跑过"。

    这个 bug 真发生过：`watch.run` 给 `reconcile` 传了 `scope="paper"`，给 `beat`
    没传，于是状态落在 `reports/paper`、心跳落在 `reports/external`。同一个循环的
    两份文件各说各话——一份说"刚刚判过"，另一份说"今天没跑过"。
    """
    from service.notify import alert_log as log

    monkeypatch.setattr(log, "default_base", lambda scope="external": tmp_path / scope)
    symbol = pick_symbols(desk, 1)[0]
    path = _frame(tmp_path, day=day, at=_moment(day, 10, 30), symbol=symbol, last=9.0)

    watch.run(desk, path, day=day, now=_moment(day, 10, 30))

    assert (tmp_path / "paper" / f"{day}_heartbeat.json").is_file()
    assert (tmp_path / "paper" / f"{day}_watch.json").is_file()
    assert not (tmp_path / "external").exists(), "paper 的心跳不能落在实盘目录"


def test_a_failing_watch_is_recorded_and_does_not_kill_the_capture(tmp_path, monkeypatch):
    from scripts import snapshot_intraday as loop

    day = "20261105"
    (tmp_path / "rt_snapshots" / day).mkdir(parents=True)
    (tmp_path / "rt_snapshots" / day / "103000_000001.parquet").write_bytes(b"")
    monkeypatch.setattr(loop.watch, "run",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    loop._watch_frame(object(), SimpleNamespace(root=tmp_path), {"trade_date": day},
                      base=tmp_path)

    heart = json.loads(alert_log.heartbeat_path(day, tmp_path).read_text(encoding="utf-8"))
    assert heart["outcome"].startswith("error: RuntimeError")
    assert "boom" in heart["outcome"]


def test_a_capture_that_wrote_no_frame_says_so(tmp_path):
    from scripts import snapshot_intraday as loop

    day = "20261105"
    loop._watch_frame(object(), SimpleNamespace(root=tmp_path), {"trade_date": day},
                      base=tmp_path)

    heart = json.loads(alert_log.heartbeat_path(day, tmp_path).read_text(encoding="utf-8"))
    assert heart["outcome"] == "no_frame"


def test_a_run_that_never_saw_a_frame_leaves_no_closed_beat(monkeypatch):
    """节假日 09:25 起来、立刻发现不是交易日：那一笔不是"收盘退出"。

    ``closed`` 的语义是"今天被看过一遍，然后正常收工"。空跑也写它，事后读心跳
    的人就把"没人在看"读成了"看过，没事"——而这两件事分不开，整个看守就没有
    意义了。同一条守卫也要挡住 ``desk is None``（账本没打开）。
    """
    from scripts import snapshot_intraday as loop

    written: list[str] = []
    monkeypatch.setattr(
        loop.alert_log, "beat",
        lambda day, *, scope, outcome="ok", now=None, base=None: written.append(outcome))

    loop._closing_beat(None, 5, "ok")    # build_desk 失败 / --no-watch，没有账本可交代
    loop._closing_beat(object(), 0, "")  # 起来了，但一帧都没抓
    assert written == [], "没看过任何东西的退出不能留下 closed"

    loop._closing_beat(object(), 1, "ok")
    assert written == ["closed"]


def test_a_failed_last_frame_is_not_relabelled_as_a_normal_close(monkeypatch):
    """最后一帧崩了，退出时再写 ``closed`` 就把故障永久盖掉了。

    心跳只留最后一条，而 ``closed`` 在读者眼里等于"今天看过、没事"。所以退出的
    那一笔只能盖在**判完了**的一帧上；``error`` / ``no_frame`` 留着，读者看到的是
    "这一轮没跑完"，不是平静的一天。
    """
    from scripts import snapshot_intraday as loop

    written: list[str] = []
    monkeypatch.setattr(
        loop.alert_log, "beat",
        lambda day, *, scope, outcome="ok", now=None, base=None: written.append(outcome))

    loop._closing_beat(object(), 3, "error: RuntimeError: boom")
    loop._closing_beat(object(), 3, "no_frame")
    assert written == [], "失败之后的退出不能改写心跳"


def test_the_last_frame_outcome_survives_the_exit(tmp_path, monkeypatch):
    """端到端：最后一帧崩溃 → 心跳停在 error，而不是被改写。"""
    from scripts import snapshot_intraday as loop

    day = "20261105"
    (tmp_path / "rt_snapshots" / day).mkdir(parents=True)
    (tmp_path / "rt_snapshots" / day / "103000_000001.parquet").write_bytes(b"")
    monkeypatch.setattr(loop.watch, "run",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    outcome = loop._watch_frame(object(), SimpleNamespace(root=tmp_path),
                                {"trade_date": day}, base=tmp_path)
    loop._closing_beat(object(), 1, outcome, base=tmp_path)

    heart = json.loads(alert_log.heartbeat_path(day, tmp_path).read_text(encoding="utf-8"))
    assert heart["outcome"].startswith("error: RuntimeError"), "退出那一笔不能盖掉故障"
