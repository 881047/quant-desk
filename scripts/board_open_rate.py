"""开板率：涨停板当天有没有被打开过，以及它随封单强度怎么变。

**为什么先测这个，而不是"成交概率"。** 打板的全部风险在执行侧，而执行侧的核心量
是 P(成交)。它不可测——逐笔委托不在任何已订阅的行情里。但它有一个可测的上界：

    P(成交) ≤ P(开板)

一个封死的板，排队排到第几位都买不到；只有它开过，队列才有意义。所以开板率是
成交概率的**天花板**，而这个天花板可以用公开数据直接量出来。

**日线回答不了这个问题。** 「当日最低价 < 涨停价」对 91.3% 的涨停股成立，因为
最低价包含封板之前的那些成交——一只 09:35 封死、全天没开过的票，它的最低价是
09:31 的价格。把那个当"开过板"会把开板率算成 0.91，与实际相反。

**5 分钟线 + 首封时间可以回答。** `ths_limit_up.limit_up_time` 给出首次封板时刻，
只要首封之后有一根 5 分钟 bar 的最低价低于涨停价，这个板就开过。判定要考虑 bar 的
边界：一根标 09:35 的 bar 覆盖 09:30–09:35，如果 09:33 才封板，它的最低价天然低于
涨停价。所以只取**起点晚于首封时刻**的 bar（`bar_end - 5min > seal_time`），
代价是首封后第一根可能被跳过——方向是**少算开板**，与"上界"的用法一致。

**两个已知的漏算**，都让开板率偏低，也就是让 P(成交) 的上界更保守：
- 开板只持续几秒、没有成交在涨停价下方打出新低时，5 分钟 bar 的最低价仍是涨停价；
- 14:50 之后才封的板没有可观测窗口，直接剔出分母（不是算作 0）。

数据可以留下来：抓到的 5 分钟线写进 `stk_mins`（按日分区、merge），这样这次标定的
副产品是 30 个交易日的分钟历史——`information` 的 `intraday_series` 维度目前只有
0.022 分，就是缺这块。

    python scripts/board_open_rate.py --sessions 30 --fetch
    python scripts/board_open_rate.py --sessions 30          # 只用库里已有的分钟线
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.jsonio import write_json_atomic
from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.adapters.tencent_adapter import TencentQuoteAdapter

# 5-minute bars end at :35 :40 ... :00. A bar labelled T covers (T-5m, T].
BAR_MINUTES = 5
# One fen: a 5-minute low that touches the ceiling is not a reopened board.
PRICE_EPS = 0.001
# Bars are ~48 per session; a symbol with fewer than this in the window is treated
# as "not collected" rather than as "never opened".
MIN_BARS_PER_SESSION = 40

PROGRESS = Path("data") / "cache" / "board_open_progress.json"


def _minute_of_day(stamp: str) -> int | None:
    """'09:33' or '2026-10-30 09:35:00' -> minutes since midnight."""
    text = str(stamp or "").strip()
    if not text:
        return None
    if " " in text:
        text = text.split(" ")[-1]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _sessions(store, sessions: int) -> list[str]:
    """The last N sessions the limit-up pool actually carries."""
    records = [r for r in store.registry.list_manifest("ths_limit_up")
               if r.status in ("ok", "warn")]
    days = sorted({str(r.partition) for r in records})[-sessions:]
    return days


def load_boards(store, days: list[str]) -> pd.DataFrame:
    """One row per (session, symbol) that the vendor says closed at the ceiling."""
    frames = []
    for day in days:
        try:
            frame = store.read("ths_limit_up", day, day)
        except (KeyError, ValueError, FileNotFoundError):
            continue
        if frame is None or frame.empty:
            continue
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = out["trade_date"].astype(str)
    for column in ("seal_money", "max_seal_money", "continue_cnt", "last_price"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def board_outcomes(store, boards: pd.DataFrame) -> pd.DataFrame:
    """The two prices this question needs: the entry premium and the opening print.

    ``gap`` is close[D] -> open[D+1], the only return a T+1 buyer can collect
    overnight. ``open_d`` is the session's own opening print, which decides
    whether the board was already sealed at 09:30 — a 一字板 never had a queue
    to join, so it must not be lumped in with "sealed later and never reopened".

    A suspended name whose next bar is three weeks later is dropped: that gap is
    not a next session, and crediting it to the board would be a return nobody
    could trade.
    """
    empty = pd.DataFrame({"gap": [], "open_d": []})
    if boards.empty:
        return empty
    codes = sorted(set(boards["ts_code"].astype(str)))
    start = str(boards["trade_date"].min())
    end = (datetime.strptime(str(boards["trade_date"].max()), "%Y%m%d")
           + timedelta(days=15)).strftime("%Y%m%d")
    try:
        daily = store.read("daily", start, end,
                           columns=["ts_code", "trade_date", "open"])
    except (KeyError, ValueError, FileNotFoundError):
        return empty
    if daily.empty:
        return empty

    daily = daily[daily["ts_code"].astype(str).isin(codes)].copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily["open"] = pd.to_numeric(daily["open"], errors="coerce")
    daily = daily[daily["open"] > 0].sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = daily.groupby("ts_code", sort=False)
    daily["next_open"] = grouped["open"].shift(-1)
    daily["next_date"] = grouped["trade_date"].shift(-1)

    merged = boards[["ts_code", "trade_date", "last_price"]].merge(
        daily[["ts_code", "trade_date", "open", "next_open", "next_date"]],
        on=["ts_code", "trade_date"], how="left")
    span = (pd.to_datetime(merged["next_date"], format="%Y%m%d", errors="coerce")
            - pd.to_datetime(merged["trade_date"], format="%Y%m%d", errors="coerce")).dt.days
    gap = merged["next_open"] / merged["last_price"] - 1
    gap = gap.where((span > 0) & (span <= 7))
    out = pd.DataFrame({"gap": gap, "open_d": merged["open"]})
    out.index = boards.index
    return out


def opened_after_seal(bars: pd.DataFrame, seal_time: str, limit_price: float) -> bool | None:
    """Did the ceiling give way after it was first sealed?

    ``None`` means "no window": the board sealed too late in the session for any
    five-minute bar to start after it, so the question is unanswerable rather
    than answered "no". Counting those as 0 would understate the open rate by
    exactly the number of late-session boards, which is the bucket a short-horizon
    buyer is most interested in.
    """
    seal = _minute_of_day(seal_time)
    if seal is None or bars is None or bars.empty or not limit_price:
        return None
    starts = bars["trade_time"].map(_minute_of_day) - BAR_MINUTES
    usable = bars[(starts > seal) & (starts < 15 * 60 - 1)]   # nothing after 14:59
    if usable.empty:
        return None
    lows = pd.to_numeric(usable["low"], errors="coerce")
    return bool((lows < float(limit_price) - PRICE_EPS).any())


def fetch_window(store, boards: pd.DataFrame, days: list[str], *,
                 limit: int | None = None, batch: int = 200,
                 min_sessions: int | None = None,
                 no_fallback: bool = False,
                 tencent_only: bool = False,
                 pause: float = 0.15) -> dict:
    """Fetch 5-minute bars for every symbol on the boards, keeping what we get.

    One request per symbol returns the provider's whole 5-minute window (~31
    sessions), so a 30-session calibration costs one call per name rather than one
    per name per day. Bars are written back into ``stk_mins`` in batches so the
    deep history survives the run — that table is forward-accumulating by design
    and this is the only way it gets thirty sessions at once.
    """
    from service.adapters.minutes import fetch_minutes

    symbols = sorted(set(boards["ts_code"].astype(str)))
    if limit:
        symbols = symbols[:limit]
    progress = {}
    if PROGRESS.exists():
        try:
            progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            progress = {}
    # Keyed by window, not by symbol. Seven sessions of Tencent bars answer a
    # seven-session question and nothing else; marking those symbols "done" for
    # the 30-session window would freeze the shallow sample in place forever.
    windows = progress.get("windows") if isinstance(progress.get("windows"), dict) else {}
    key = f"{days[0]}:{days[-1]}"
    done = set(windows.get(key) or [])

    # Tencent-only exists because 东财 blocks the IP after a few hundred calls and
    # then costs 2.3s per symbol to say no. When the window is 7 sessions the
    # fallback is not a fallback — it is the only source with the right depth.
    em = None if tencent_only else EastMoneyAdapter()
    tg = None if no_fallback else TencentQuoteAdapter()
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    sources: dict[str, int] = {}
    started = time.time()
    fetched = 0
    # 覆盖多少个交易日才算"抓到了"。腾讯兜底只有 320 根 bar ≈ 7 个交易日，
    # 拿它回答 30 日的问题等于用七天的样本说一个月的事。达不到就**不记进度**，
    # 下次东财恢复时会重试；否则一次限流会让那批票永远停在浅数据上。
    need = min_sessions or max(5, len(days) // 2)

    for index, symbol in enumerate(symbols, start=1):
        if symbol in done:
            continue
        try:
            frame, source = fetch_minutes(symbol, em=em, tg=tg)
        except Exception as exc:  # noqa: BLE001 - a free feed must not stop the run
            failures.append(f"{symbol}: {type(exc).__name__}")
            continue
        fetched += 1
        if frame is None or frame.empty:
            failures.append(f"{symbol}: empty")
            continue
        frame = frame[frame["trade_date"].astype(str).isin(days)].copy()
        if frame.empty:
            failures.append(f"{symbol}: no bars in window")
            continue
        covered = int(frame["trade_date"].nunique())
        sources[source] = sources.get(source, 0) + 1
        frame["ts_code"] = symbol
        # 收下已有的，哪怕只覆盖三天。之前是"不够 need 就整只丢掉"，结果 360 条
        # 涨停里 211 条（58.6%）一条 bar 都没有——而那些票并不是没有数据，只是
        # 不够"算抓完"。丢掉的正好是最难抓的那批。
        frames.append(frame)
        if covered >= need:
            done.add(symbol)
        else:
            failures.append(f"{symbol}: {source} only {covered}/{len(days)} sessions")
        if pause:
            # Tencent answers without a rate limiter; a thousand back-to-back
            # requests is how a free endpoint starts closing connections, which
            # is exactly what happened to 东财 today.
            time.sleep(pause)

        if index % 25 == 0 or index == len(symbols):
            elapsed = time.time() - started
            print(f"[open] {index}/{len(symbols)} symbols, {elapsed:.0f}s, "
                  f"{len(failures)} failures", flush=True)
        if len(frames) >= batch:
            _persist(store, frames, days)
            frames = []
            _save_progress(windows, key, done)

    if frames:
        _persist(store, frames, days)
    _save_progress(windows, key, done)
    return {"symbols": len(symbols), "fetched": fetched, "need_sessions": need,
            "skipped": max(0, len(symbols) - fetched),
            "sources": sources,
            "failures": failures[:20], "seconds": round(time.time() - started, 1)}


def _save_progress(windows: dict, key: str, done: set[str]) -> None:
    windows[key] = sorted(done)
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(PROGRESS, {"windows": windows,
                                 "updated": datetime.now().isoformat()})


def _persist(store, frames: list[pd.DataFrame], days: list[str]) -> None:
    """One merge write per session partition, for a batch of symbols."""
    if not frames:
        return
    wanted = {str(day) for day in days}
    bars = pd.concat(frames, ignore_index=True)
    bars = bars.drop_duplicates(subset=["ts_code", "trade_time"], keep="last")
    for day, part in bars.groupby("trade_date"):
        if str(day) not in wanted:
            continue
        columns = [c for c in ("ts_code", "trade_time", "trade_date", "open", "high",
                               "low", "close", "vol", "amount", "source")
                   if c in part.columns]
        try:
            store.write("stk_mins", str(day), part[columns], as_of=str(day),
                        mode="merge", note="5min eastmoney|tencent (board_open_rate)")
        except (KeyError, ValueError, OSError) as exc:
            print(f"[warn] write {day}: {type(exc).__name__}: {exc}")


def mark_opened(store, boards: pd.DataFrame, days: list[str]) -> pd.DataFrame:
    """Attach ``opened`` (True/False/NaN) from whatever 5-minute bars we hold.

    Bars are read once for the whole window rather than per board: 1,700 reads of
    a 57k-row partition is the kind of thing that turns a two-second job into a
    ten-minute one.
    """
    frames = []
    for day in days:
        try:
            frame = store.read("stk_mins", day, day,
                               columns=["ts_code", "trade_time", "low"])
        except (KeyError, ValueError, FileNotFoundError):
            continue
        if frame is not None and not frame.empty:
            frame = frame.copy()
            frame["trade_date"] = str(day)
            frames.append(frame)
    if not frames:
        out = boards.copy()
        out["opened"] = pd.NA
        return out

    bars = pd.concat(frames, ignore_index=True)
    bars["ts_code"] = bars["ts_code"].astype(str)
    # Grouped once, then looked up. The obvious version filtered the whole frame
    # per board — 1,600 boards against 74k bars, then 400k bars for a 30-session
    # window. Same answer, quadratic work.
    groups = {key: part for key, part in
              bars.groupby(["ts_code", "trade_date"], sort=False)}

    flags: list[bool | None] = []
    for row in boards.itertuples(index=False):
        key = (str(row.ts_code), str(row.trade_date))
        part = groups.get(key)
        if part is None or len(part) < MIN_BARS_PER_SESSION:
            flags.append(None)          # not collected: unanswerable, not "never"
            continue
        flags.append(opened_after_seal(part, getattr(row, "limit_up_time", ""),
                                       getattr(row, "last_price", None)))
    out = boards.copy()
    out["opened"] = pd.array(flags, dtype="boolean")
    return out


def _bucket(values: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(values, bins=edges, labels=labels, include_lowest=True)


def summarize(boards: pd.DataFrame) -> dict:
    """Open rate and next-open premium, split by the three things a buyer judges."""
    usable = boards[boards["opened"].notna()].copy()
    usable["opened"] = usable["opened"].astype(bool)
    if usable.empty:
        return {"n": 0, "error": "没有可判定的样本：stk_mins 没覆盖这些交易日"}

    usable["streak"] = pd.to_numeric(usable.get("continue_cnt"), errors="coerce")
    usable["seal_time_min"] = usable["limit_up_time"].map(_minute_of_day)

    def block(frame: pd.DataFrame) -> dict:
        gap = pd.to_numeric(frame["gap"], errors="coerce").dropna()
        return {
            "n": len(frame),
            "open_rate": round(float(frame["opened"].mean()), 4),
            "next_open_gap_mean": round(float(gap.mean()), 4) if len(gap) else None,
            "next_open_gap_n": len(gap),
            "win": round(float((gap > 0).mean()), 4) if len(gap) else None,
        }

    # Three groups, not two. "Never opened" mixes two trades that have nothing in
    # common: a board that opened at the ceiling never offered a queue position at
    # all, while one that sealed at 10:30 and held was a queue the buyer lost. Only
    # the first is unbuyable for a reason no amount of speed can fix, and it is
    # also the group with the largest premium — lumping them together makes
    # "sealed boards pay more" look like one fact when it is two.
    usable["one_word"] = (
        pd.to_numeric(usable.get("open_d"), errors="coerce")
        >= usable["last_price"] - PRICE_EPS)
    ops = usable[usable["opened"]]
    rows = []
    for label, frame in (
        ("开过板（可排队）", ops),
        ("没开·非一字板", usable[~usable["opened"] & ~usable["one_word"]]),
        ("一字板（没排队机会）", usable[~usable["opened"] & usable["one_word"]]),
    ):
        item = block(frame)
        item["bucket"] = label
        rows.append(item)

    by_seal = []
    quantiles: list[float] = []
    ratios = usable.get("seal_ratio")
    if ratios is not None and ratios.notna().any():
        quintiles = ratios.quantile([0.2, 0.4, 0.6, 0.8]).tolist()
        quantiles = [round(float(q), 6) for q in quintiles]
        labels = ["Q1 最弱", "Q2", "Q3", "Q4", "Q5 最强"]
        usable["seal_band"] = _bucket(ratios, [-1e-9] + quintiles + [float("inf")], labels)
        for label, frame in usable.groupby("seal_band", observed=True):
            item = block(frame)
            item["bucket"] = str(label)
            item["seal_ratio_median"] = round(float(frame["seal_ratio"].median()), 4)
            by_seal.append(item)

    by_time = []
    edges = [0, 10 * 60, 11 * 60 + 30, 13 * 60, 14 * 60, 15 * 60]
    labels = ["<10:00", "10:00-11:30", "11:30-13:00", "13:00-14:00", ">14:00"]
    usable["time_band"] = _bucket(usable["seal_time_min"], edges, labels)
    for label, frame in usable.groupby("time_band", observed=True):
        item = block(frame)
        item["bucket"] = str(label)
        by_time.append(item)

    by_streak = []
    bands = pd.cut(usable["streak"].fillna(1).clip(upper=3),
                   bins=[0, 1, 2, 3], labels=["首板", "2连板", "3板及以上"])
    usable["streak_band"] = bands
    for label, frame in usable.groupby("streak_band", observed=True):
        item = block(frame)
        item["bucket"] = str(label)
        by_streak.append(item)

    overall = block(usable)
    # The number this whole exercise exists for: what a buyer who can only be
    # filled on a reopened board would have earned, before any cost.
    implied = None
    if ops["gap"].notna().any() and len(usable):
        implied = {
            "strategy": "只做回封：等开板之后才买",
            "p_open": overall["open_rate"],
            "gap_if_opened": round(float(ops["gap"].mean()), 4),
            "expected_gross": round(overall["open_rate"] * float(ops["gap"].mean()), 4),
            "note": ("开板率 × 开过板后的次日溢价 = 「等回封」这一种打法的毛期望。"
                     "它不等于打板整体的期望：扫板（封板那一刻买）的收益在 "
                     "by_opened 的「没开·非一字板」一档，那个数字成立的前提是"
                     "封板瞬间真的成交，而这件事这里测不了。"
                     "两个数字都未扣 80bps 往返成本，开板率本身还是下界。"),
        }

    return {
        "n": len(usable),
        "sessions": int(usable["trade_date"].nunique()),
        "unanswerable": int(boards["opened"].isna().sum()),
        "overall": overall,
        "by_opened": rows,
        # 三组不是"可成交/不可成交"，而是三种不同的入场方式。它们的收益差得很远，
        # 而日线引擎只能表达其中最后一种——这是本表最容易被读错的地方。
        "reading": {
            "开过板（可排队）": "封板之后才买到（等回封/追开板），价格确定，收益最薄。"
                                "日线的「当日收盘入场」正好落在这一档。",
            "没开·非一字板": "必须在封板那一刻买到（扫板）。买到就是买到，买不到没有第二次机会。",
            "一字板（没排队机会）": "只能在集合竞价里排，开盘就已经封死。",
        },
        "seal_quantiles": quantiles,
        "by_seal_ratio": by_seal,
        "by_first_seal_time": by_time,
        "by_streak": by_streak,
        "implied": implied,
    }


def _print(summary: dict) -> None:
    if not summary.get("n"):
        print("[open] " + str(summary.get("error")))
        return
    print(f"\n可判定样本 {summary['n']} 个板 / {summary['sessions']} 个交易日"
          f"（另有 {summary['unanswerable']} 个无观测窗口）")
    head = f"{'分组':<18}{'n':>6}{'开板率':>9}{'次日溢价':>10}{'胜率':>8}"
    print(head)
    print("-" * len(head))
    for group, label in (("by_opened", "按结果"), ("by_seal_ratio", "按封单比"),
                         ("by_first_seal_time", "按首封时间"), ("by_streak", "按连板")):
        print(f"[{label}]")
        for row in summary.get(group) or []:
            gap = row["next_open_gap_mean"]
            win = row["win"]
            print(f"  {row['bucket']:<16}{row['n']:>6}{row['open_rate']*100:>8.1f}%"
                  f"{(gap*100 if gap is not None else 0):>9.2f}%"
                  f"{(win*100 if win is not None else 0):>7.1f}%")
    implied = summary.get("implied")
    if implied:
        print(f"\n毛期望上界 = 开板率 {implied['p_open']*100:.1f}% × "
              f"开过板后溢价 {implied['gap_if_opened']*100:.2f}% = "
              f"{implied['expected_gross']*100:.2f}%/笔")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=30,
                        help="recent limit-up sessions to cover (5min depth is ~31)")
    parser.add_argument("--fetch", action="store_true",
                        help="pull 5-minute bars for every symbol on the boards")
    parser.add_argument("--limit", type=int, default=None, help="cap symbols (smoke test)")
    parser.add_argument("--min-sessions", type=int, default=None,
                        help="a symbol counts as fetched only with this many sessions "
                             "(default: half the window); Tencent's ~7-session fallback "
                             "cannot answer a 30-session question")
    parser.add_argument("--no-tencent", action="store_true",
                        help="skip the 7-session Tencent fallback entirely")
    parser.add_argument("--tencent-only", action="store_true",
                        help="skip 东财 (2.3s per refused call); use for a 7-session window")
    parser.add_argument("--pause", type=float, default=0.15,
                        help="seconds between symbol fetches (0 disables)")
    parser.add_argument("--out", default=str(ROOT / "reports" / "board_open_rate.json"))
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)

    days = _sessions(store, args.sessions)
    if not days:
        print("[open] ths_limit_up 没有分区")
        return 1
    boards = load_boards(store, days)
    if boards.empty:
        print("[open] 窗口内没有涨停记录")
        return 1
    print(f"[open] 窗口 {days[0]}~{days[-1]}（{len(days)} 个交易日），"
          f"{len(boards)} 条涨停 / {boards['ts_code'].nunique()} 只票")

    # 封单比：唯一能把"封得牢不牢"变成可比数字的分母。
    ratios = []
    for day in days:
        try:
            frame = store.read("daily_basic", day, day,
                               columns=["ts_code", "trade_date", "circ_mv"])
        except (KeyError, ValueError, FileNotFoundError):
            continue
        if frame is not None and not frame.empty:
            frame = frame.copy()
            frame["trade_date"] = frame["trade_date"].astype(str)
            frame["circ_mv"] = pd.to_numeric(frame["circ_mv"], errors="coerce") * 1e4
            ratios.append(frame)
    if ratios:
        circ = pd.concat(ratios, ignore_index=True)
        boards = boards.merge(circ, on=["ts_code", "trade_date"], how="left")
        boards["seal_ratio"] = (boards["seal_money"] /
                                boards["circ_mv"].where(boards["circ_mv"] > 0))
    else:
        boards["seal_ratio"] = pd.NA

    outcomes = board_outcomes(store, boards)
    boards["gap"] = outcomes["gap"]
    boards["open_d"] = outcomes["open_d"]

    if args.fetch:
        stats = fetch_window(store, boards, days, limit=args.limit,
                             min_sessions=args.min_sessions,
                             no_fallback=args.no_tencent,
                             tencent_only=args.tencent_only, pause=args.pause)
        print(f"[open] fetched={stats['fetched']} skipped={stats['skipped']} "
              f"failures={len(stats['failures'])} need>={stats['need_sessions']}sessions "
              f"in {stats['seconds']}s sources={stats['sources']}")
        for line in stats["failures"]:
            print(f"  [warn] {line}")

    boards = mark_opened(store, boards, days)
    summary = summarize(boards)
    summary["window"] = {"start": days[0], "end": days[-1], "sessions": len(days)}
    summary["boards_total"] = len(boards)
    summary["caveats"] = [
        ("开板率是**下界**：只统计能被 5 分钟 bar 观测到的开板。开板几秒、"
         "涨停价下方没有成交打出新低时，bar 的最低价仍是涨停价，算作没开。"),
        "首封后第一根 bar 可能被跳过（bar 起点必须晚于首封时刻），同样让开板率偏低。",
        "14:45 之后封的板没有观测窗口，计为不可判定并剔出分母，不是算作没开。",
        ("次日溢价按 close[D] → open[D+1] 计，与 research/spec.py 的 next_open 一致；"
         "跨停牌（次日超过 7 天）直接剔除。"),
        "这是毛收益，未扣 80bps 往返成本，也未扣滑点。",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, summary)
    # 机器可读的那一份给 desk 用：`service/trading/fill.py` 拿它把"当前封单比"
    # 归进历史档位，让模型在提案时看到"这一档历史上多少板当天开过、开过之后次日
    # 怎样"。与 information.json 同一个约定：派生物进 data/cache，人读的进 reports。
    cache = ROOT / "data" / "cache" / "board_open_rate.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cache, summary)
    _print(summary)
    print(f"\n[open] 写入 {out}\n[open] 写入 {cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
