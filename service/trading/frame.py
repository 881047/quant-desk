"""此刻的全市场：把最新一帧盘口快照翻译成模型能读的键。

和 ``facts.py`` 的分工：

* ``facts`` 回答"这个交易日**收盘后**有什么"，数字来自日线，写进快照后不再变；
* ``frame`` 回答"**最近一帧**盘口里是什么"，数字来自 ``rt_snapshots``。

盘中这两个答案差一整天。``context_pack`` 的候选票、``facts`` 的 breadth 都按
``latest_bar_date()`` 算，而 10:30 时那一天是**昨天**——模型醒来读它们，会把昨天
当成现在。这个模块存在的理由，是让"现在"有一个可引用的入口。

键名刻意和 ``facts`` 对齐（``breadth`` / ``affordable``），这样 ``changes.diff`` 的
``MATERIAL`` 表不用维护第二份。

四条实现约定：

* **报 ``frame_age_seconds``，不报"实时"。** 快照 30 秒到 3 分钟一批，抓到之后
  就不再变。读者必须能看见"这一帧多旧"，否则会把 10:15 的数字当成 10:30 的。
* **不给判断。** 没有 signal、没有 score、没有排好序的"推荐"。它比 ``facts`` 快，
  不比 ``facts`` 聪明——要不要动手是读者的事。
* **``frame_id`` 的跨日有效性取决于冷存。** 热目录 ``rt_snapshots`` 只保留最近
  ``SNAPSHOT_KEEP``（120）个文件；更早的帧从 2026-11-07 起由
  ``snapshot.prune_frames`` 在裁剪前转存到 ``rt_archive/<day>/``。
  ``replay_frames()`` 合并两处才是完整序列。**冷存上线之前采集的日子不可回补**，
  那是永久缺口，不许用其它数据源假装补上了。

* **快照没有方向，变化只能由两帧相减给出。** 15 分钟后醒来看到"涨停 61 家"，
  读不出它是从 82 掉下来的还是从 40 涨上来的——同一个数字，两件相反的事。把同一套
  逻辑再跑一遍不会得到变化，只有拿两帧比才有。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from service import vocabulary as vocab
from service.adapters.symbols import ts_code as canonical_ts_code
from service.kernel.clock import SHANGHAI
from service.trading.realtime import STALE_AFTER, session_state
from service.trading.screen import affordable_price

#: ``HHMMSS_microseconds.parquet``。微秒是写入方为了"同一秒两帧不互相覆盖"加的。
FRAME_RE = re.compile(r"^(\d{6})_(\d{6})\.parquet$")

#: 拼 ``ts_code`` 用的市场号。``rt_snapshots`` 只存 6 位代码，``stk_limit`` 用带后缀的。
_MARKET_SUFFIX = {0: ".SZ", 1: ".SH"}

#: 帧的"太旧"阈值。不能直接复用 ``realtime.STALE_AFTER``（120 秒）——那个阈值是按
#: **单只报价**定的，而抓取进程盘中是 3 分钟一批（首封/尾盘 30 秒），正常 age 就能到
#: 180 秒。用 120 判会把每一个正常批次都标成 stale，于是这个字段永远为真——永远响的
#: 告警等于没有告警。三倍覆盖住最慢的批次，再慢就真的是抓取停了。
FRAME_STALE_AFTER = STALE_AFTER * 3


def frames(root: Path | str, day: str) -> list[Path]:
    """One day's archived frames, oldest first. Empty when nothing was captured."""
    directory = Path(root) / "rt_snapshots" / str(day)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.parquet") if FRAME_RE.match(path.name))


def replay_frames(root: Path | str, day: str) -> list[Path]:
    """一天的完整帧序列：冷存 + 热目录，按文件名（= 实际帧时间）排序。

    ``frames()`` 只读热目录，被 ``SNAPSHOT_KEEP`` 裁掉的早盘帧永远消失；
    冷存把它们补回来。同名时**以冷存为准**——那是 ``copy2`` 出来的原始字节，
    是 PIT 回放的 ground truth；热目录那份只是还没删掉的副本。

    返回路径列表而不是时间戳序列：人工 ``force_refresh`` 会插入非周期帧，
    所以排序可以按文件名（``HHMMSS_%f`` 零填充，字典序即时间序），但**任何
    按序号推算时刻的做法都是错的**。时刻一律用 ``frame_time(day, name)`` 现取。
    """
    root = Path(root)
    hot = frames(root, day)
    cold_dir = root / "rt_archive" / str(day)
    cold = (sorted(p for p in cold_dir.glob("*.parquet") if FRAME_RE.match(p.name))
            if cold_dir.is_dir() else [])
    by_name = {p.name: p for p in hot}
    by_name.update({p.name: p for p in cold})
    return sorted(by_name.values(), key=lambda p: p.name)


def parse_as_of(value: str | None, day: str) -> datetime | None:
    """把 ``as_of`` 解析成上海时区时刻。ISO 8601 / ``HHMMSS`` / ``HH:MM:SS``。

    解析不了就返回 None，由调用方明确报 ``bad_as_of``；不许默默退回"最新一帧"，
    那会把一次历史查询伪装成实时查询。
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        digits = text.replace(":", "")
        if len(digits) != 6 or not digits.isdigit():
            return None
        try:
            parsed = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]),
                              int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
        except ValueError:
            return None
    # 秒级 as_of 规范化到「该秒末」。文件名带微秒只是写入方为了同秒两帧不撞名，
    # 不是可分辨的经济时刻：帧内报价时间是秒级，decision_ts 也是秒级。若拿
    # 10:00:00.000000 去比 100000_000001（10:00:00.000001），「查 10:00:00 的帧」
    # 会拿到 09:30 那一帧——既反直觉也不更严谨，因为同一秒内系统分辨不出先后。
    # 晚于该秒的帧仍一律不载入，零前视不变；带显式微秒的 as_of 原样保留。
    if parsed.microsecond == 0:
        parsed = parsed.replace(microsecond=999999)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def load(path: Path | str) -> pd.DataFrame:
    """Read one archived frame.

    One place knows a frame is a parquet file, so the reader that displays it
    (``build``) and the watchdog that judges the book on it (``watch.run``)
    cannot drift into reading it differently.
    """
    return pd.read_parquet(path)


def frame_time(day: str, name: str) -> datetime | None:
    """When the capture ran, read off the filename.

    The file also carries a ``snapshot_ts`` column, but that is **per row** — each
    quote's own timestamp. A halted name keeps ``08:00:00`` all day, so a max()
    over that column answers "which quote is freshest", not "when did this batch
    run". The filename is the batch clock; the column is the quote clock.
    """
    match = FRAME_RE.match(name)
    if not match:
        return None
    hhmmss = match.group(1)
    try:
        return datetime(
            int(day[:4]),
            int(day[4:6]),
            int(day[6:8]),
            int(hhmmss[:2]),
            int(hhmmss[2:4]),
            int(hhmmss[4:6]),
            int(match.group(2)),
            tzinfo=SHANGHAI,
        )
    except ValueError:
        return None


def _ts_codes(frame: pd.DataFrame) -> pd.Series:
    """Canonical ``ts_code`` for both vendor frame shapes.

    Some snapshots carry a numeric ``market`` column; others only carry a six-
    digit ``code``. Falling back to a fixed exchange, or to ``.NA`` when
    ``market`` is absent, makes a healthy ``stk_limit`` table match zero rows.
    Exchange inference already lives in ``service.adapters.symbols`` (including
    the 920xxx Beijing exception), so use that single definition here.
    """
    code = frame["code"].astype(str).str.strip()

    def inferred(value: str) -> str:
        digits = value.split(".")[0]
        if len(digits) == 6 and digits.isdigit():
            return canonical_ts_code(value)
        return value.upper()

    fallback = code.map(inferred)
    if "market" not in frame.columns:
        return fallback
    suffix = pd.to_numeric(frame["market"], errors="coerce").map(_MARKET_SUFFIX)
    explicit = code.str.split(".").str[0].str.zfill(6) + suffix
    return explicit.where(suffix.notna(), fallback)


def _limit_table(day: str, root: Path | str) -> tuple[pd.DataFrame | None, str]:
    """当日限价表，读**一次**给多帧用；返回 ``(表, 不可用原因)``。

    两帧相减要算两次涨跌停家数，而限价表是同一张。分开读会把这个模块最贵的
    一次 IO 付两遍，也让「两帧用的是不是同一天的限价」变成一个没人验的假设。
    """
    try:
        table = pd.read_parquet(
            Path(root) / "parquet" / "stk_limit" / "2026.parquet",
            filters=[("trade_date", "==", str(day))],
        )
    except (OSError, ValueError, KeyError):
        return None, "stk_limit 当日分区不可读，涨跌停家数未计算"
    if table.empty:
        return None, f"stk_limit 没有 {day} 的行"
    return table[["ts_code", "up_limit", "down_limit"]], ""


def _limits_from(frame: pd.DataFrame, table: pd.DataFrame) -> dict:
    """涨跌停家数，靠 ``stk_limit`` 的当日限价判。

    不在这里定义"几个点算涨停"：主板 10%、创业板/科创 20%、ST 5%、北交所 30%，
    那张表已经把每只票的限价算好了，再写一遍幅度就是第二份定义，两处一定会分叉。
    退市/PT 股 ``last`` 为空，会落在未命中里——它们本来也不该计入涨跌家数。
    """
    # 实时快照帧自己也可能带 up_limit / down_limit（rt_quote 就有这两列）。
    # 直接 merge 同名列会得到 up_limit_x / up_limit_y，于是 joined["up_limit"]
    # 抛 KeyError，整个 /market/frame 500。重命名后再合并，既避开冲突，也
    # 保证判涨停用的是 stk_limit 的当日限价，不是快照里那份可能过期的口径。
    limits = table.rename(columns={"up_limit": "_stk_up_limit",
                                   "down_limit": "_stk_down_limit"})
    joined = frame.merge(limits, on="ts_code", how="left")
    last = pd.to_numeric(joined["last"], errors="coerce")
    up = pd.to_numeric(joined["_stk_up_limit"], errors="coerce")
    down = pd.to_numeric(joined["_stk_down_limit"], errors="coerce")
    # 0.001 是报价的最小变动单位：不设这个容差，浮点误差会把"正好封在涨停价"
    # 的票算成没封上。
    return {
        "available": True,
        "up": ((last >= up - 0.001) & up.notna()).sum().item(),
        "down": ((last <= down + 0.001) & down.notna()).sum().item(),
        "matched": up.notna().sum().item(),
        "basis": "last >= stk_limit.up_limit（限价来自当日分区，不按幅度重算）",
        "note": (
            "逐票可检的口径；quant_pools 走派生池，两边可能差 1-3 家——"
            "要权威数字读 quant_pools.counts。"
        ),
    }


def _breadth(frame: pd.DataFrame) -> dict:
    """涨跌家数与两个均值，算法和 ``facts._breadth`` 一致（``last/pre_close - 1``）。"""
    last = pd.to_numeric(frame["last"], errors="coerce")
    pre = pd.to_numeric(frame["pre_close"], errors="coerce")
    ret = (last / pre - 1.0).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if not len(ret):
        return {"rows": 0, "note": "这一帧没有可计算的报价"}
    return {
        "rows": len(ret),
        "advancers": (ret > 0).sum().item(),
        "decliners": (ret < 0).sum().item(),
        "flat": (ret == 0).sum().item(),
        "equal_weight_return": round(float(ret.mean()), 6),
        "median_return": round(float(ret.median()), 6),
        "units": vocab.note("returns"),
    }


def _movers(frame: pd.DataFrame, limit: int) -> dict:
    """涨跌幅榜两端各 ``limit`` 条，只带名字和数字。"""
    work = frame.copy()
    work["_ret"] = (
        pd.to_numeric(work["last"], errors="coerce")
        / pd.to_numeric(work["pre_close"], errors="coerce")
        - 1.0
    )
    work = work.replace([float("inf"), float("-inf")], pd.NA).dropna(subset=["_ret"])

    def rows(side: str) -> list[dict]:
        picks = work.nlargest(limit, "_ret") if side == "up" else work.nsmallest(limit, "_ret")
        out = []
        for _, row in picks.iterrows():
            amount = row.get("amount")
            out.append(
                {
                    "symbol": str(row["ts_code"]),
                    "name": str(row.get("name") or ""),
                    "last": float(row["last"]),
                    "pct": round(float(row["_ret"]), 6),
                    "amount_cny": float(amount) if pd.notna(amount) else None,
                }
            )
        return out

    return {"up": rows("up"), "down": rows("down")}


def _affordable(frame: pd.DataFrame, equity: float) -> dict:
    """价格上限之内有几种票，用**这一帧的价格**算，不是收盘价。"""
    cap = affordable_price(float(equity))
    last = pd.to_numeric(frame["last"], errors="coerce").dropna()
    last = last[last > 0]
    return {
        "price_cap": round(cap, 2),
        "in_cap": (last <= cap).sum().item(),
        "rows": len(last),
        "basis": "一手（100 股）落在 screen.affordable_weight 的目标权重内",
        "note": "和 facts.affordable.in_cap 是两个时刻的数：这里是这一帧，那里是收盘。",
    }


def _turnover(frame: pd.DataFrame) -> float | None:
    """全市场这一帧的累计成交额。两帧相减就是这段窗口新增的量。"""
    if "amount" not in frame.columns:
        return None
    amount = pd.to_numeric(frame["amount"], errors="coerce").dropna()
    return round(float(amount.sum()), 2) if len(amount) else None


def _market_snapshot(frame: pd.DataFrame, table: pd.DataFrame | None) -> dict:
    """一帧的六个标量。当前帧和历史帧都走这里，两边口径不会分叉。"""
    breadth = _breadth(frame)
    limits = _limits_from(frame, table) if table is not None else {}
    return {
        "limits_up": limits.get("up"),
        "limits_down": limits.get("down"),
        "advancers": breadth.get("advancers"),
        "decliners": breadth.get("decliners"),
        "equal_weight_return": breadth.get("equal_weight_return"),
        "turnover_cny": _turnover(frame),
    }


def _pick_before(
    archived: list[Path], day: str, latest_at: datetime | None, lookback_min: int
) -> tuple[Path, datetime, bool] | None:
    """最近、且**不晚于** ``latest_at - lookback`` 的那一帧。

    找不到就退回最早一帧并标记 ``short``：开盘五分钟时问"这 15 分钟"没有答案，
    但"开盘这 90 秒"仍然是答案，只是窗口比问的窄。把窄窗口说成宽窗口才是错的，
    所以 ``elapsed_seconds`` 永远报真实间隔，``short_of_lookback`` 单独标出来。
    """
    if latest_at is None or len(archived) < 2:
        return None
    target = latest_at - timedelta(minutes=max(1, int(lookback_min)))
    chosen: tuple[Path, datetime] | None = None
    for path in archived[:-1]:
        stamp = frame_time(day, path.name)
        if stamp is not None and stamp <= target:
            chosen = (path, stamp)
    if chosen is not None:
        return chosen[0], chosen[1], False
    stamp = frame_time(day, archived[0].name)
    if stamp is None:
        return None
    return archived[0], stamp, True


def _delta(
    archived: list[Path],
    latest: Path,
    day: str,
    *,
    frame: pd.DataFrame,
    table: pd.DataFrame | None,
    lookback: int,
) -> dict:
    """两帧相减：这一段里市场往哪走了。

    快照本身没有方向。15 分钟后醒来看到"涨停 61 家"，不知道它是从 82 掉下来的
    还是从 40 涨上来的——同一个数字，两件相反的事。**变化只能由两帧给出**，
    把同一套逻辑再跑一遍不会得到它。
    """
    latest_at = frame_time(day, latest.name)
    picked = _pick_before(archived, day, latest_at, lookback)
    if picked is None:
        return {
            "available": False,
            "lookback_minutes": lookback,
            "note": "今天只有一帧，还没有可比的对象。等第二帧落地再问。",
        }
    path, stamp, short = picked
    try:
        before = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        return {
            "available": False,
            "lookback_minutes": lookback,
            "since_frame_id": path.stem,
            "note": f"上一帧读不出来（{type(exc).__name__}），没有可比的数字。",
        }
    before = before.assign(ts_code=_ts_codes(before))
    then = _market_snapshot(before, table)
    current = _market_snapshot(frame, table)
    change = {
        key: (
            round(value - then[key], 6)
            if isinstance(value, (int, float)) and isinstance(then.get(key), (int, float))
            else None
        )
        for key, value in current.items()
    }
    elapsed = round((latest_at - stamp).total_seconds()) if latest_at else None
    if short:
        note = (
            f"今天还没有 {lookback} 分钟前的帧，退回最早一帧 "
            f"{stamp:%H:%M:%S}（间隔 {elapsed} 秒）——窗口比你问的窄，"
            f"别把 `change` 当成 {lookback} 分钟的变动。"
        )
    else:
        note = (
            f"对比 {stamp:%H:%M:%S} → {latest_at:%H:%M:%S}（{elapsed} 秒）。"
            f"`change` 是差值不是百分比，方向由符号给。"
        )
    return {
        "available": True,
        "since_frame_id": path.stem,
        "since_at": stamp.isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed,
        "lookback_minutes": lookback,
        "short_of_lookback": short,
        "before": then,
        "now": current,
        "change": change,
        "units": vocab.note("frame_delta"),
        "note": note,
    }


def _account(desk, frame: pd.DataFrame) -> dict:
    """账户的**实时**市值：持仓股数 × 这一帧的价格。

    收盘价算出来的 ``facts.account`` 在盘中是昨天的。这里给"按现在价平仓值多少"，
    并如实说明几只票真的定到了价——一只都没定时，这个数不比收盘价更有意义。
    """
    summary = desk.book.summary()
    prices = dict(zip(frame["ts_code"], pd.to_numeric(frame["last"], errors="coerce")))
    priced, missing = 0, []
    value = 0.0
    for position in summary.get("positions") or []:
        symbol = str(position.get("symbol") or "")
        qty = float(position.get("qty") or 0)
        price = prices.get(symbol)
        if price is None or pd.isna(price) or float(price) <= 0:
            missing.append(symbol)
            # 退回账本价，这样总额不会少一只票；`priced` 与 `positions` 的差额
            # 才是"这个数有多少是真实时"的答案。
            value += qty * float(position.get("last_price") or position.get("avg_cost") or 0)
            continue
        priced += 1
        value += qty * float(price)
    cash = float(summary.get("cash_free") or summary.get("cash_available") or 0)
    count = len(summary.get("positions") or [])
    if not count:
        note = "当前没有持仓，equity 就是可用现金。"
    elif missing:
        note = "market_value 用这一帧的价格；unpriced 里的票退回了账本价，它们让这个数只部分有效。"
    else:
        note = "全部持仓都定到了价。"
    return {
        "positions": count,
        "priced": priced,
        "unpriced": missing,
        "market_value": round(value, 2),
        "cash_free": round(cash, 2),
        "equity": round(value + cash, 2),
        "note": note,
    }


def build(
    desk,
    *,
    day: str | None = None,
    root: Path | str | None = None,
    now: datetime | None = None,
    movers: int = 5,
    lookback: int = 15,
    as_of: str | None = None,
    frame_id: str | None = None,
) -> dict:
    """What a frame says, and how it got there. Pure: no writes, no orders, no judgement.

    ``as_of`` / ``frame_id`` 开启**显式历史回放**：帧序列改用 ``replay_frames``
    （冷存 + 热目录），只载入不晚于目标时刻的帧，``now`` 被中和（age/stale
    不适用）。不传这两个参数时行为与改造前逐字一致。
    """
    now = now or datetime.now(SHANGHAI)
    day = str(day or now.strftime("%Y%m%d"))
    root = Path(root) if root is not None else Path(desk.market.store.root)
    replay = as_of is not None or frame_id is not None

    archived_all = replay_frames(root, day) if replay else frames(root, day)
    if not archived_all:
        return {
            "ok": False,
            "error": "no_frames",
            "day": day,
            "note": (
                f"{day} 没有盘口快照。抓取进程是 scripts/snapshot_intraday.py "
                '--loop 30 --windows；没有它，盘中读不到任何"现在"的数字。'
            ),
        }

    if replay:
        if frame_id is not None:
            matched = [p for p in archived_all if p.stem == str(frame_id)]
            if not matched:
                return {"ok": False, "error": "frame_not_found", "day": day,
                        "frame_id": str(frame_id),
                        "note": "这一帧在热目录和冷存里都不存在；冷存上线前的日子不可回补。"}
            latest = matched[0]
        else:
            cutoff = parse_as_of(as_of, day)
            if cutoff is None:
                return {"ok": False, "error": "bad_as_of", "day": day,
                        "note": "as_of 要写成 ISO 8601、HHMMSS 或 HH:MM:SS。"}
            epoch = datetime.min.replace(tzinfo=SHANGHAI)
            prior = [p for p in archived_all
                     if (frame_time(day, p.name) or epoch) <= cutoff]
            if not prior:
                return {"ok": False, "error": "no_frame_before_as_of", "day": day,
                        "as_of": cutoff.isoformat(timespec="seconds"),
                        "note": "该时刻之前没有已采集的帧；不许用之后的帧代替。"}
            latest = prior[-1]
        # 零前视：delta 的基线只能来自回放帧及其之前的帧。传全量序列会让
        # ``_pick_before`` 遍历到「当时还没发生」的帧，把未来写进 change。
        archived = archived_all[:archived_all.index(latest) + 1]
    else:
        archived = archived_all
        latest = archived[-1]

    frame = load(latest)
    if frame.empty:
        return {
            "ok": False,
            "error": "empty_frame",
            "day": day,
            "frame_id": latest.stem,
            "note": "这一帧是空的，抓取可能刚失败。",
        }

    frame = frame.assign(ts_code=_ts_codes(frame))
    # 限价表读一次：当前帧、历史帧、"涨跌停家数"三处用的必须是同一天、同一份表。
    table, limit_why = _limit_table(day, root)
    at = frame_time(day, latest.name)
    summary = desk.book.summary()
    equity = float(summary.get("equity") or 0)
    if replay:
        # 中和 now：历史帧今天回放会算出几十万秒 age 并被误判成 stale。时段按
        # **帧时刻**判，不按今天的时钟。账本仍是当前状态——这点必须写进 note，
        # 否则读者会以为整个盘面（含持仓）都回到了过去。
        age, stale = None, False
        state = session_state(at or now,
                              calendar=getattr(desk.market, "calendar", None))
        note = (
            f"历史回放帧 {latest.stem}：frame_age_seconds 与 frame_is_stale 不适用"
            "（没有当前时钟）。帧时刻见 frame_at；delta 只用了该帧及之前的帧。"
            "account 仍是**当前**账本，不是历史账本。"
        )
    else:
        # round 而不是 int：文件名带微秒（写入方为了同秒两帧不撞），截断会让
        # "整 30 秒"变成 29，测试和读者都会以为差了一秒。
        age = round((now - at).total_seconds()) if at is not None else None
        state = session_state(now, calendar=getattr(desk.market, "calendar", None))
        # 收盘后快照本来就停在最后一帧，age 只会一直涨——那不是"数据坏了"。
        # 盘中两分钟没新帧才是真问题。判 stale 要带时段，否则 15:05 之后恒真。
        stale = bool(state["in_session"] and age is not None
                     and age > FRAME_STALE_AFTER)
        note = _verdict_note(at, age, state, stale)

    return {
        "ok": True,
        "day": day,
        "frame_id": latest.stem,
        "frame_at": at.isoformat(timespec="seconds") if at else None,
        "frame_age_seconds": age,
        "frame_is_stale": stale,
        "historical": replay,
        "as_of": str(as_of) if replay and as_of else None,
        "as_of_cutoff": (parse_as_of(as_of, day).isoformat(timespec="microseconds")
                         if replay and as_of else None),
        "frames_today": len(archived_all),
        "frames_visible": len(archived),
        "session": state,
        "breadth": _breadth(frame),
        "limits": (
            _limits_from(frame, table)
            if table is not None
            else {"available": False, "note": limit_why}
        ),
        "delta": _delta(archived, latest, day, frame=frame, table=table, lookback=lookback),
        "affordable": _affordable(frame, equity),
        "movers": _movers(frame, max(1, int(movers))),
        "account": _account(desk, frame),
        # replay 里 account 仍是**当前**账本，不是历史账本。note 已经写了给人看，
        # 这两个字段是给机器读的：下游归因脚本不该把 current book 当成当时的
        # 持仓。默认模式不加任何字段，保证旧响应逐字不变。
        **({"account_is_historical": False,
            "account_basis": "current_book_at_read_time"} if replay else {}),
        "note": note,
    }


def _verdict_note(at: datetime | None, age: int | None, state: dict, stale: bool) -> str:
    """一句话说清这个响应有多"现在"，以及为什么。"""
    if age is None or at is None:
        return "帧名无法解析出时刻，frame_age_seconds 为空——不要把它当实时。"
    clock = at.strftime("%H:%M:%S")
    if not state.get("in_session"):
        return (
            f"最后一帧在 {clock}（距现在 {age} 秒）。当前不在连续竞价时段，"
            f"快照本来就停了——这不是 stale，是没有新帧可读。"
        )
    if stale:
        return (
            f"这一帧是 {clock} 的，已经 {age} 秒没有更新。抓取进程可能挂了，"
            f"先看 quant_data_health 再决定要不要相信这些数字。"
        )
    return (
        f"这是 {clock} 那一批抓取的数字，距现在 {age} 秒。判断不在这个响应里："
        f"要不要动手、动多少，是读者的事。"
    )
