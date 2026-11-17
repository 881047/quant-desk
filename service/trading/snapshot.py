"""One intraday capture: live quotes, three pools, seal tape, sentiment panel.

Why this is a service module and not a script
---------------------------------------------
The capture used to live only in ``scripts/snapshot_intraday.py``, and the API
reached for it with ``from scripts.snapshot_intraday import snapshot`` when a
person pressed "refresh now". That made ``scripts/`` a runtime dependency of the
service — the one directory nobody thinks of as part of the application — and it
meant the manual path and the scheduled path could drift apart without either
side noticing.

The scheduler stays in the script (windows, loop interval, session guard); the
capture is here, so the API and the daily job call the same function the loop
calls.

Prints vs logging
-----------------
This module still prints. The output is read by the operator watching the
60-second loop, and the daily job greps it; switching to ``logging`` would change
what those two see while fixing nothing about the capture itself.
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.quality import evaluate
from service.adapters.sqlite_store import Registry
from service.kernel.clock import SHANGHAI

POOLS = ("limit_up_pool", "broken_board_pool", "limit_down_pool")
# 池子的数据集名 → 落进 `pool_tape` 的标签。用词与 `/market/pools` 的键一致，
# 免得同一个池子在两处叫两个名字。
POOL_LABEL = {"limit_up_pool": "limit_up", "broken_board_pool": "broken_board",
              "limit_down_pool": "limit_down"}
MAX_FUTURE_SKEW = pd.Timedelta(minutes=5)

# Snapshots kept per session. The 60-second loop writes ~240 a day and the
# directory is an audit trail, not the queryable record (`rt_quote` is).
SNAPSHOT_KEEP = 120


def prune_frames(archive: Path, cold: Path, keep: int = SNAPSHOT_KEEP) -> dict:
    """裁剪 ``rt_snapshots`` 之前，把要删的帧先转存冷存。

    为什么冷存的是**文件**而不是数据行：每个归档文件本身就是那一批 5,561 行
    的完整截面，是 PIT 回放唯一可信的 ground truth。``rt_quote`` 按票存且
    ``snapshot_ts`` 是每行自己的报价时间，还原不出「某一批全市场」；由它
    重建出来的派生状态一旦和冷存不符，没法判断是市场变了还是解析错了。

    三条口径，不许后人当 bug 修：
    1. **复制成功才 unlink**。复制失败就抛出去，由调用方吞掉并保留文件，
       下一轮重试；绝不能先删后复制。
    2. **目标已存在则跳过复制直接删**（幂等）——上一轮复制过、写崩在
       unlink 之前，重跑不该把文件当成新帧留在热目录里。
    3. 冷存是**文件目录**，不是 DatasetStore 数据集：不登记 SPECS、不进
       ``DAILY_JOB_STEPS``，因此也没有 owner 契约要同步。文件名保留
       ``HHMMSS_%f.parquet`` 原名，``frame_time()`` 依赖它取 batch clock。
    """
    kept = sorted(archive.glob("*.parquet"))
    stale_files = kept[:-keep] if keep > 0 else kept
    copied = removed = 0
    for stale in stale_files:
        target = cold / stale.name
        if not target.exists():
            cold.mkdir(parents=True, exist_ok=True)
            shutil.copy2(stale, target)
            copied += 1
        stale.unlink(missing_ok=True)
        removed += 1
    return {"copied": copied, "removed": removed,
            "kept": len(kept) - removed, "cold": str(cold)}


def with_frame_id(quotes: pd.DataFrame, stamp: str) -> pd.DataFrame:
    """给这一批全市场报价打上 batch 外键。纯函数，返回新帧。

    ``stamp`` 唯一来源是 ``snapshot()`` 里的 ``now.strftime("%H%M%S_%f")``，
    它同时也是归档文件名（batch clock）。``snapshot_ts`` 是每行自己的报价
    时间，凭它还原不出「某一批的全市场截面」。

    两天的 ``101530_000001`` 是不同帧，所以跨表关联必须用复合外键
    ``(trade_date, frame_id)``，不能把 day 拼进值里。返回新帧而非原地改，
    是为了让 ``rt_snapshots`` 归档仍写原始 quotes——回放靠文件名，不靠新列。
    """
    return quotes.assign(frame_id=stamp)


def _prepare_quotes(quotes: pd.DataFrame, now: datetime,
                    label_from_quotes: bool) -> tuple[pd.DataFrame, str]:
    """Use each row's quote time and isolate invalid or mixed-session rows."""
    local_now = pd.Timestamp(now).tz_convert(SHANGHAI)
    current_day = local_now.strftime("%Y%m%d")
    if quotes.empty:
        return quotes, current_day
    if "quote_ts" not in quotes.columns:
        print(f"[warn] dropped {len(quotes)} quotes without quote_ts")
        return quotes.iloc[0:0].copy(), current_day

    epoch = pd.to_numeric(quotes["quote_ts"], errors="coerce")
    stamps = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce").dt.tz_convert(SHANGHAI)
    valid = epoch.gt(0) & stamps.notna() & stamps.le(local_now + MAX_FUTURE_SKEW)
    quote_days = stamps.dt.strftime("%Y%m%d")
    day = current_day
    if label_from_quotes:
        candidates = quote_days[valid & quote_days.le(current_day)].dropna()
        if not candidates.empty:
            counts = candidates.value_counts()
            day = max(counts.index, key=lambda value: (counts[value], value))
    keep = valid & quote_days.eq(day)
    clean = quotes.loc[keep].copy()
    clean["snapshot_ts"] = [stamp.isoformat(timespec="seconds") for stamp in stamps.loc[keep]]
    clean["trade_date"] = day
    dropped = len(quotes) - len(clean)
    if dropped:
        print(f"[warn] dropped {dropped} quotes with invalid/future/mixed-session timestamps")
    return clean, day


def sentiment_panel(pools: dict) -> dict:
    """Delegates to ``service.trading.sentiment``.

    The panel is a daily-frequency fact (pool counts, highest streak, seal
    amount), so it lives in the service and is computed by the 17:30 job too.
    Keeping a copy here meant the label existed only during a session, which is
    exactly when nobody is deciding what to do tomorrow.
    """
    from .sentiment import panel

    return panel(pools)


def pool_tape(pools: dict, now: datetime) -> pd.DataFrame:
    """One frame of order-book depth for whoever is in a pool.

    Why only the pools: 封单 is a concept that exists only while a stock is at
    its limit. 东财's bulk quote (`clist`) carries no depth at all — that is why
    "how much is on the bid" had no answer on that path — and 腾讯's
    ``qt.gtimg.cn`` does carry it but takes ~50 codes per request. Doing that for
    all 5,500 names would be 110 requests every ten minutes; doing it for the
    80-100 names in the three pools is two.

    Why it is worth a separate table: ``limit_up_pool`` is keyed
    ``(trade_date, code)``, so every snapshot overwrites the previous one and the
    day keeps only its last frame. "封单从 8 万手掉到 2 万手"是炸板前最直接的
    信号，而它恰恰只存在于被覆盖掉的那些帧里。This table is keyed
    ``(snapshot_ts, code)`` and written with ``mode="merge"``, so the process is
    kept and the endpoint keeps the outcome.

    ``bid1_volume`` 的单位是**手**（交易所口径）。``bid1_value`` 由它换算成元：
    涨停时它就是封单额，2026-11-03 实测 东方海洋 630,936 手 × 100 × 3.42 =
    2.1578 亿，东财同一时刻的 ``seal_amount`` 是 215,780,207，差 95 元是手数
    取整。炸板时它是买一承接金额，含义不同但仍然有意义。
    """
    wanted: dict[str, dict] = {}
    for name, frame in pools.items():
        if frame is None or frame.empty or "code" not in frame.columns:
            continue
        for record in frame.to_dict("records"):
            code = str(record.get("code") or "").strip()
            if len(code) != 6 or code in wanted:
                continue
            wanted[code] = {
                "pool": POOL_LABEL.get(name, name),
                "boards": record.get("boards"),
                "open_times": record.get("open_times"),
                "first_seal_time": record.get("first_seal_time"),
            }
    if not wanted:
        return pd.DataFrame()

    from service.adapters.tencent_adapter import TencentQuoteAdapter

    try:
        depth = TencentQuoteAdapter().fetch_quotes(sorted(wanted))
    except Exception as exc:  # noqa: BLE001 - 盘口是加分项，不该拖垮整次快照
        print(f"[warn] pool depth failed: {type(exc).__name__}")
        return pd.DataFrame()
    if depth is None or depth.empty:
        return pd.DataFrame()

    fallback = now.astimezone(SHANGHAI).isoformat(timespec="seconds")
    rows: list[dict] = []
    for record in depth.to_dict("records"):
        code = str(record.get("code") or "").zfill(6)
        extra = wanted.get(code)
        if extra is None:
            continue
        stamp = str(record.get("snapshot_ts") or fallback)
        bid1 = _num_or_none(record.get("bid1"))
        bid1_volume = _num_or_none(record.get("bid1_volume"))
        rows.append({
            "snapshot_ts": stamp,
            "trade_date": str(record.get("trade_date") or ""),
            "code": code,
            "name": record.get("name"),
            "pool": extra["pool"],
            "last": _num_or_none(record.get("last")),
            "pct_chg": _num_or_none(record.get("pct_chg")),
            "up_limit": _num_or_none(record.get("up_limit")),
            "bid1": bid1,
            "bid1_volume": bid1_volume,
            "ask1_volume": _num_or_none(record.get("ask1_volume")),
            "bid_volume_5": _num_or_none(record.get("bid_volume_5")),
            "ask_volume_5": _num_or_none(record.get("ask_volume_5")),
            "bid1_value": (bid1_volume * 100.0 * bid1
                           if bid1_volume is not None and bid1 is not None else None),
            "boards": extra["boards"],
            "open_times": extra["open_times"],
            "first_seal_time": extra["first_seal_time"],
            "float_mv": _num_or_none(record.get("float_mv")),
            "source": "tencent",
        })
    return pd.DataFrame(rows).dropna(subset=["trade_date"])


def _num_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN 是"这个源没给这个字段"，不是 0。`number == number` 也能判，但读起来
    # 像笔误（ruff PLR0124 会当成自比较），写成 isnan 意图才没有歧义。
    return None if math.isnan(number) else number


def snapshot(store: DatasetStore, registry: Registry, em: EastMoneyAdapter,
             codes: list[str], *, label_from_quotes: bool = False,
             with_tape: bool = True) -> dict:
    """One capture.

    ``with_tape=False`` 关掉封单时序的写入。存在的理由只有一个：``/market/pools``
    的 ``force_refresh`` 也会走到这里，而那条路径的目的是"把池子刷新一下"，不是
    "补一帧轨迹"。分帧数据由定时的抓取周期独占，否则一次人工刷新会在任何时刻
    插入一帧，把"两次观察之间封单怎么变"搅浑。
    """
    now = datetime.now().astimezone()
    quotes = pd.DataFrame()
    try:
        quotes = em.fetch_quotes(codes)
    except Exception as exc:  # noqa: BLE001 - vendor throttle
        print(f"[warn] eastmoney quotes failed: {type(exc).__name__}; trying tencent")
    if quotes.empty:
        from service.adapters.tencent_adapter import TencentQuoteAdapter

        quotes = TencentQuoteAdapter().fetch_quotes(codes)
        if not quotes.empty:
            print(f"[info] tencent fallback returned {len(quotes)} quotes")

    quotes, day = _prepare_quotes(quotes, now, label_from_quotes)
    # 这一批的 batch clock。三处写入（rt_quote / rt_snapshots 归档 / pool_tape）
    # 共用同一个 stamp，才是唯一可信的跨表批次键。pool_tape.snapshot_ts 是
    # 腾讯源每批请求时刻，与东财批次不是同一个时钟；拿时间戳近似对齐会让
    # 「一个瞬间」变成两个数，正是 P0 诊断里那条断点。裁定 D-9。
    stamp = now.strftime("%H%M%S_%f")

    pools: dict[str, pd.DataFrame] = {}
    cal = TradingCalendarStore.from_frame(store.read("trade_cal"))
    if cal.days and not cal.is_trading_day(day):
        # EastMoney happily returns the previous session's pool for a weekend
        # date; storing that under today would poison every date-based query.
        print(f"[skip] {day} is not a trading day; pools not written")
    else:
        for name in POOLS:
            try:
                pools[name] = em.fetch_pool(name, day)
            except Exception as exc:  # noqa: BLE001 - vendor throttle
                print(f"[warn] pool {name} failed: {type(exc).__name__}")
                pools[name] = pd.DataFrame()

    if not quotes.empty:
        # 只作复合外键 (trade_date, frame_id)：不改 pk、不进 SPECS.required，
        # 旧分区读到缺列就是 NaN，不回填、不抛错。归档文件本身仍写原始
        # quotes（21 列），frame_id 由文件名提供，冷存/回放不依赖新列。
        #
        # 2026-11-16：这一块原来是裸调用。当天 09:33 之后 480 余轮抓取
        # 全部「quotes=5561 q=ok」打印正常，但 rt_quote 的 manifest 停在
        # 09:33:19、归档目录只剩 11 个文件——落盘失败没有留下任何一行
        # 日志，事后只能从帧数倒推。包一层 try 不改变成功路径的语义，
        # 只保证失败一定在 stderr 上留字；抓取进程不因一次落盘失败而死。
        try:
            store.write("rt_quote", day, with_frame_id(quotes, stamp), as_of=day,
                        mode="merge", note=f"snapshot {day}")
            archive = store.root / "rt_snapshots" / day
            archive.mkdir(parents=True, exist_ok=True)
            # Microseconds, because two snapshots can land in the same second (a
            # manual ``--once`` during the 60-second loop) and the second one silently
            # replaced the first. This archive exists precisely because the print is
            # not reproducible; losing one to a filename collision defeats it.
            quotes.to_parquet(archive / f"{stamp}.parquet", index=False)
            # 60-second loop writes ~240 files a day and nothing ever pruned them.
            # Keep the most recent SNAPSHOT_KEEP; the partitions in `rt_quote` are the
            # queryable record, this is only the audit trail. 被裁掉的帧先转存冷存，
            # 否则 09:25–09:42 的早盘帧永远消失，PIT 回放无从复现。
            try:
                prune_frames(archive, store.root / "rt_archive" / day, SNAPSHOT_KEEP)
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - 帧丢了要留证据，但不能弄死抓取
            print(f"[error] frame persist failed rt_quote[{day}] {stamp}: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    for name, df in pools.items():
        if df is None or df.empty:
            continue
        store.write(name, day, df, as_of=day, mode="replace", status="ok")

    # 池子写完再补盘口：这里要的正是"这一帧的封单量"，而池子那份是覆盖写的，
    # 只有最后一份。两件事共用同一个 `now`，所以两边的时间戳对得上。
    # 更重要的是 D-9：tape 也打同一个 frame_id，跨表关联用 (trade_date,
    # frame_id) 而不是时间戳近似——五档量必须来自同一批快照。
    tape_rows = 0
    if pools and with_tape:
        try:
            tape = pool_tape(pools, now)
            if not tape.empty:
                store.write("pool_tape", day, tape.assign(frame_id=stamp),
                            as_of=day, mode="merge", note=f"seal tape {day}")
                tape_rows = len(tape)
        except Exception as exc:  # noqa: BLE001 - 时序是加分项，不该拖垮快照
            print(f"[warn] pool_tape failed: {type(exc).__name__}: {exc}")

    panel = sentiment_panel(pools)
    quality = evaluate("rt_quote", day, quotes) if not quotes.empty else None
    if quality is not None:
        registry.record_quality("rt_quote", day, quality.status,
                                quality.rows, [i.as_dict() for i in quality.issues])

    payload = {
        "as_of": now.isoformat(timespec="seconds"),
        "trade_date": day,
        "quotes": 0 if quotes.empty else len(quotes),
        "pools": {k: 0 if v is None else len(v) for k, v in pools.items()},
        "tape": tape_rows,
        "sentiment": panel,
        "quality": None if quality is None else quality.status,
    }
    out = store.root / "sentiment" / f"{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload
