"""Backfill 5-minute bars for a small watchlist.

Source choice matters here: Tushare's ``stk_mins`` is throttled to **one call per
minute** on this account, which makes a multi-day, multi-symbol backfill take
hours. Eastmoney (via akshare) serves the same 5-minute bars in ~0.2s, so it is
the primary source and Tushare is the fallback.

    python scripts/backfill_mins.py --days 5
    python scripts/backfill_mins.py --symbols 600000.SH,000001.SZ --days 30
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.trading.state import PaperStore

COLUMNS = {
    "时间": "trade_time", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "vol", "成交额": "amount",
}


def held_symbols(paper_db: Path) -> list[str]:
    if not paper_db.exists():
        return []
    state = PaperStore(paper_db).load_state() or {}
    positions = (state.get("account") or {}).get("positions") or {}
    return [s for s, row in positions.items() if int(row.get("total_qty") or 0) > 0]


def fetch_eastmoney(symbol: str, period: str = "5", retries: int = 2) -> pd.DataFrame:
    import akshare as ak

    code = symbol.split(".")[0]
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ak.stock_zh_a_hist_min_em(symbol=code, period=period, adjust="")
            if raw is None or raw.empty:
                return pd.DataFrame()
            frame = raw.rename(columns=COLUMNS)
            frame["ts_code"] = symbol
            frame["trade_time"] = frame["trade_time"].astype(str)
            frame["trade_date"] = frame["trade_time"].str[:10].str.replace("-", "", regex=False)
            return frame[["ts_code", "trade_date", "trade_time", "open", "high",
                          "low", "close", "vol", "amount"]]
        except Exception as exc:  # noqa: BLE001 - vendor call
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"{symbol}: {type(last).__name__}: {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="comma separated, defaults to holdings")
    parser.add_argument("--days", type=int, default=5, help="calendar days to keep")
    parser.add_argument("--period", default="5", help="1 | 5 | 15 | 30 | 60 minutes")
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    data = DatasetStore(ROOT / "data", registry)
    paper_db = Path(os.getenv("PAPER_DB") or ROOT / "data" / "paper.sqlite")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or held_symbols(paper_db)
    if not symbols:
        print("[mins] no symbols: pass --symbols or hold a position")
        return 0

    cutoff = (pd.Timestamp.now() - pd.Timedelta(days=args.days)).strftime("%Y%m%d")
    written = 0
    failed = 0
    frames = []
    for symbol in symbols:
        try:
            frame = fetch_eastmoney(symbol, args.period)
        except RuntimeError as exc:
            failed += 1
            print(f"[warn] {exc}")
            continue
        if frame.empty:
            print(f"[warn] {symbol}: empty")
            continue
        frame = frame[frame["trade_date"] >= cutoff]
        frames.append(frame)
        written += len(frame)
        print(f"[mins] {symbol}: {len(frame)} bars over {frame['trade_date'].nunique()} sessions")

    # Write each session once, after all symbols have been collected. Merging
    # also preserves symbols stored by an earlier, narrower watchlist run.
    if frames:
        bars = pd.concat(frames, ignore_index=True)
        for day, part in bars.groupby("trade_date"):
            data.write("stk_mins", str(day), part, as_of=str(day), mode="merge",
                       note=f"{args.period}min eastmoney")

    print(f"[mins] rows={written} symbols={len(symbols)} failures={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
