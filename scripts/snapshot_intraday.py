"""Intraday snapshot loop: when to capture, not how.

    python scripts/snapshot_intraday.py            # one snapshot
    python scripts/snapshot_intraday.py --loop 60  # every 60s until 15:00

The capture itself — quotes, three pools, seal tape, sentiment panel — lives in
``service/trading/snapshot.py``. It moved there on 2026-11-05 because the API's
manual-refresh path was importing *this script*: ``scripts/`` had become a
runtime dependency of the service, and the scheduled and manual paths could
drift without either side noticing. This file keeps what is genuinely
scheduling: the window table, the loop interval, and the session guard.

Why archive every snapshot
--------------------------
We have no L2/queue data. The only way to calibrate "would a limit-up buy have
filled?" is to keep our own timestamped quotes and compare them later with the
next day's open. Snapshots are cheap (~1MB) and irreplaceable.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry
from service.kernel.clock import SHANGHAI
from service.notify import alert_log
from service.trading import build_desk, watch
from service.trading import frame as frame_view
from service.trading.market import MarketRepository
from service.trading.snapshot import snapshot


def _session_open(now: datetime, cal: TradingCalendarStore) -> bool:
    """Whether a looping capture may run; one-off close snapshots are exempt."""
    local = now.astimezone(SHANGHAI)
    return (cal.is_trading_day(local.strftime("%Y%m%d"))
            and (local.hour, local.minute, local.second) < (15, 0, 0))


# 打板唯一需要分帧的地方，但"密"和"全"要分开给：
#
# - 09:25-10:00 与 14:30-15:00 按 30 秒抓：首封与尾盘定生死，封单在这两段
#   变化最快，分辨率就是信息本身。
# - 10:00-11:30 与 13:00-14:30 按 3 分钟抓：这两段封单金额确实变化不大，但
#   **封这件事本身在发生**。实测 22 个交易日 544 笔炸板样本，首次封板时间落在
#   10:00-14:30 的占 **50.0%**（295 笔"封一次就炸"里 50.2%）——只抓首尾两段，
#   训练集直接少一半，而少的那一半没有系统性，是随机的。
#   当时写下"中间三小时封单基本不动、抓了只是把磁盘写满"判断时，看的是封单额
#   的斜率，没看炸板样本的出生时刻。
# - 午休 11:30-13:00 不抓：那 90 分钟的池子状态就是 11:30 收盘那一刻，
#   抓 30 帧等于同一行写 30 遍。
#
# 200 帧/天（原来 70+60），约 2 万行，不到派生池总量的 5%。
DEFAULT_WINDOWS = ("09:25-10:00/30,10:00-11:30/180,"
                   "13:00-14:30/180,14:30-15:00/30")


def _parse_windows(text: str) -> list[tuple[tuple[int, int], tuple[int, int], int | None]]:
    """``HH:MM-HH:MM[/SEC]`` → ``[(start, end, interval|None), ...]``.

    间隔是可选的：不写就沿用 ``--loop``，写了这一段就按它自己的节奏抓。
    """
    out: list[tuple[tuple[int, int], tuple[int, int], int | None]] = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        span, _, step = part.partition("/")
        try:
            start, end = span.split("-")
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
            interval = int(step) if step.strip() else None
        except ValueError as exc:
            raise SystemExit(
                f"--windows 需要 HH:MM-HH:MM 或 HH:MM-HH:MM/秒，收到 {part!r}") from exc
        if (sh, sm) >= (eh, em):
            raise SystemExit(f"--windows 的起点要早于终点：{part!r}")
        if interval is not None and interval <= 0:
            raise SystemExit(f"窗口间隔要为正整数秒：{part!r}")
        out.append(((sh, sm), (eh, em), interval))
    if not out:
        raise SystemExit("--windows 解析后为空")
    return out


def _window_state(now: datetime,
                  windows: list[tuple[tuple[int, int], tuple[int, int], int | None]]) -> str:
    """``inside`` / ``between`` / ``after``，按上海时间判。

    用 ``after`` 让进程在最后一个窗口结束时自己退出，而不是靠外部 kill——
    kill 掉的话下一次调度可能撞上一个还在写的 parquet。
    """
    local = now.astimezone(SHANGHAI)
    hhmm = (local.hour, local.minute)
    for start, end, *_ in windows:
        if start <= hhmm < end:
            return "inside"
    if hhmm >= max(end for _, end, *_ in windows):
        return "after"
    return "between"


def _window_interval(now: datetime,
                     windows: list[tuple[tuple[int, int], tuple[int, int], int | None]],
                     default: int) -> int:
    """当前窗口自己配的间隔；没配就用 ``--loop``。"""
    local = now.astimezone(SHANGHAI)
    hhmm = (local.hour, local.minute)
    for start, end, step in windows:
        if start <= hhmm < end and step:
            return int(step)
    return int(default)


def _watch_frame(desk, store, payload: dict, *, base=None) -> str:
    """Judge the paper book on the frame this iteration just archived.

    Never raises. The capture is the irreplaceable half — the archive is the only
    record of what the tape looked like at that second, while the watch can be
    re-run on the next frame. A broken watchdog therefore shows up as the
    heartbeat's ``outcome``, which is the file a reader checks precisely when the
    alerts look quiet.

    Returns that same ``outcome`` so the caller knows whether the last thing that
    happened today was a judgement or a failure.
    """
    at = datetime.now(SHANGHAI)
    day = str(payload.get("trade_date") or at.strftime("%Y%m%d"))
    archived = frame_view.frames(store.root, day)
    if not archived:
        alert_log.beat(day, scope=watch.SCOPE, outcome="no_frame", now=at, base=base)
        return "no_frame"
    try:
        shot = watch.run(desk, archived[-1], day=day, now=at, base=base)
    except Exception as exc:  # noqa: BLE001 - 看盘失败不该弄死抓取进程
        outcome = f"error: {type(exc).__name__}: {exc}"[:160]
        alert_log.beat(day, scope=watch.SCOPE, now=at, base=base, outcome=outcome)
        print(f"[warn] watch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return outcome
    if shot["fired"] or shot["cleared"]:
        changed = ", ".join(
            f"{row['severity']}:{row['rule']}" for row in [*shot["fired"], *shot["cleared"]][:3])
        print(f"[watch] open={shot['open']} fired={len(shot['fired'])} "
              f"cleared={len(shot['cleared'])} {changed}")
    for row in shot.get("acted") or []:
        # 代下单是这台机器唯一会自己花钱的动作，每一笔都要在日志里留一行人工
        # 能读的字——不只是进决策表。卖不掉（T+1、跌停、风控否决）同样打出来。
        print(f"[auto] {row['status']} {row['symbol']} qty={row['qty']} "
              f"entry={row['ticket_id']} {'; '.join(row['reasons'][:2])}")
    for row in shot.get("intent") or []:
        # 入场意图同样要能人工读：触发/未触发/拒绝/兑现失败，一行一个。
        # shadow 轨迹在 reports/shadow，这里只给守着抓取窗口的人看。
        print(f"[intent] {row['status']} {row['symbol']} "
              f"ticket={row['ticket_id']} promoted={row.get('promoted') or '-'} "
              f"{'; '.join(row.get('reasons') or [])[:120]}")
    return "ok"


def _closing_beat(desk, captured: int, last: str, *, base=None) -> None:
    """退出时的心跳，只在今天真的看过**至少一帧**、且最后一帧判完了之后写。

    两种退出都不该被说成"收盘退出"：

    * 节假日任务照样会在 09:25 起来，然后立刻发现不是交易日。那一笔若也算
      ``closed``，事后读心跳的人会以为今天被看过一遍——而"看过，没事"和"没人在看"
      正是这个模块存在的全部理由。
    * 最后一帧崩了却写 ``closed``，是把一次故障改成"今天没事"。心跳本来就只留
      最后一条，这个覆盖是永久的。
    """
    if captured and last == "ok":
        _beat(desk, "closed", base=base)


def _beat(desk, outcome: str, *, base=None) -> None:
    """一个不判行情的节拍：进程还在，只是没有新帧。

    午休的 90 分钟里抓取循环不抓，但每 20 秒写一次 ``idle``。没有这一笔，
    11:30 和 13:00 之间的心跳会"超时"，读者看到的是"没人看盘"——而事实是
    盘本身停了。``closed`` 同理：收盘退出是正常结束，不该在 15:05 之后
    让 ``is_stale`` 恒为真。
    """
    if desk is None:
        return
    at = datetime.now(SHANGHAI)
    alert_log.beat(at.strftime("%Y%m%d"), scope=watch.SCOPE, outcome=outcome,
                   now=at, base=base)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="intraday snapshot")
    parser.add_argument("--loop", type=int, default=0, help="seconds between snapshots")
    parser.add_argument("--windows", default="",
                        help="只在盘口窗口内抓，HH:MM-HH:MM[/秒][,...]。"
                             f"打板的默认是 {DEFAULT_WINDOWS}：首封与尾盘 30 秒，"
                             "盘中 3 分钟（一半的炸板样本封在这三小时里，只抓首尾会少一半训练集）。"
                             "窗口之外只等不抓；最后一个窗口结束后进程自己退出")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--no-watch", action="store_true",
                        help="只抓帧，不看 paper 持仓。默认每一帧都把账本过一遍 tripwire")
    parser.add_argument("--close-snapshot", action="store_true",
                        help="label the snapshot by the quotes' own timestamp (after close / weekend)")
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    em = EastMoneyAdapter()

    basic = store.read("stock_basic")
    if basic.empty:
        print("error: stock_basic missing; run scripts/backfill.py first", file=sys.stderr)
        return 2
    codes = [str(c) for c in basic["ts_code"] if not str(c).endswith(".BJ")]
    if args.max_codes:
        codes = codes[: args.max_codes]
    print(f"[snapshot] codes={len(codes)} loop={args.loop}s")

    # 同一个进程看两件事：抓全市场，和用**刚抓到的这一帧**看自己的账本。
    # 再起一个循环盯持仓会有两个问题：多一个可能悄悄死掉的计划任务，而且它取的
    # 价格会比这一帧晚几秒——同一次判断里出现两个"现价"，读者无法分辨。
    # 只读：这个 desk 不下单、不发邮件，唯一的写入是 reports/paper/ 下的两个日志。
    desk = None
    if args.loop > 0 and not args.no_watch:
        try:
            desk = build_desk(root=ROOT, market=MarketRepository(store))
        except Exception as exc:  # noqa: BLE001 - 账本打不开也要继续抓帧
            print(f"[warn] watch disabled: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print("[watch] paper 账本每帧过一遍 tripwire（--no-watch 关掉）")

    loop_cal = (TradingCalendarStore.from_frame(store.read("trade_cal"))
                if args.loop > 0 else None)
    # 循环抓而不指定窗口时，用打板默认窗口，而不是全天 30 秒。
    # 理由有两层：全天按 30 秒是 670 帧/天，没人需要那个分辨率；而"忘了传
    # --windows"的后果是磁盘被写满，不是少抓——默认值该偏向省，不该偏向多。
    # 真要全天，显式写 --windows 09:15-15:00。
    spec = args.windows or (DEFAULT_WINDOWS if args.loop > 0 else "")
    windows = _parse_windows(spec) if spec else None
    if windows is not None:
        print(f"[snapshot] windows={spec} loop={args.loop}s")
    #: 这个进程今天真的看到过几帧。``closed`` 只能在一帧之后写：节假日任务照样会在
    #: 09:25 起来，然后立刻发现不是交易日——那一笔若也算"收盘退出"，事后读心跳的人
    #: 会以为今天被看过一遍。``idle`` 不受这个限制，它说的正是"进程在、没有窗口"。
    captured = 0
    #: 最后一帧的判读结果。``closed`` 要盖掉它，所以必须是判完了才盖。
    last_outcome = ""
    while True:
        now = datetime.now().astimezone()
        if loop_cal is not None and not _session_open(now, loop_cal):
            _closing_beat(desk, captured, last_outcome)
            print("[snapshot] loop stopped: outside a trading session")
            return 0
        if windows is not None:
            state = _window_state(now, windows)
            if state == "after":
                _closing_beat(desk, captured, last_outcome)
                print("[snapshot] loop stopped: last window closed")
                return 0
            if state == "between":
                # 窗口之间不抓。等 20 秒再看，进程可以跨过午休继续跑。
                _beat(desk, "idle")
                time.sleep(20)
                continue
        payload = snapshot(store, registry, em, codes, label_from_quotes=args.close_snapshot)
        s = payload["sentiment"]
        print(f"[{payload['as_of'][11:19]}] quotes={payload['quotes']} "
              f"zt={s['limit_up']} zb={s['broken_board']} dt={s['limit_down']} "
              f"height={s['max_boards']} tone={s['tone']} q={payload['quality']}")
        if desk is not None:
            last_outcome = _watch_frame(desk, store, payload)
            captured += 1
        if args.loop <= 0:
            return 0
        if loop_cal is not None and not _session_open(datetime.now().astimezone(), loop_cal):
            _closing_beat(desk, captured, last_outcome)
            print("[snapshot] loop stopped: trading session ended")
            return 0
        # 每个窗口自己的节奏：首封与尾盘 30 秒，盘中 3 分钟。
        time.sleep(_window_interval(datetime.now().astimezone(), windows or [], args.loop))


if __name__ == "__main__":
    raise SystemExit(main())
