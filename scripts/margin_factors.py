"""Run the margin factor family (``ashare_margin``) from the command line.

    .venv/Scripts/python.exe scripts/margin_factors.py
    .venv/Scripts/python.exe scripts/margin_factors.py --detail full
    .venv/Scripts/python.exe scripts/margin_factors.py --json > out.json

Read-only. Family is 16 tests; its q-values are NOT comparable with
``ashare_daily`` (36), ``ashare_lhb`` (16) or ``ashare_intraday`` (12).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore  # noqa: E402
from service.adapters.sqlite_store import Registry  # noqa: E402
from service.research import factors_margin  # noqa: E402
from service.trading.market import MarketRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Margin cross-sectional factor read")
    parser.add_argument("--data-root", default=str(ROOT / "data"))
    parser.add_argument("--start", default="20250102",
                        help="margin_detail starts 20250102")
    parser.add_argument("--end", default=None)
    parser.add_argument("--detail", choices=("summary", "full"), default="summary")
    parser.add_argument("--horizons", default=None)
    parser.add_argument("--no-neutralise", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = Path(args.data_root)
    store = DatasetStore(data, Registry(data / "registry.sqlite"))
    market = MarketRepository(store)
    end = args.end or market.latest_bar_date()
    if not end:
        print("no daily partition found", file=sys.stderr)
        return 2
    try:
        sw = market.sw_table()
    except Exception:  # noqa: BLE001
        sw = None

    out = factors_margin.panel_margin(
        store, start=args.start, end=str(end), sw_table=sw,
        horizons=args.horizons or factors_margin.HORIZONS,
        detail=args.detail, neutralise=not args.no_neutralise,
    )
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"[{out.get('status')}] {out.get('error')}", file=sys.stderr)
        return 1

    avail = out.get("availability") or {}
    print(f"family={out['family']}  window={out['window']['start']}..{out['window']['end']}")
    print(f"days={avail.get('margin_days')}  codes_median={avail.get('margin_codes_median')}  "
          f"tests={out['multiple_testing']['n_tests']}")
    print(f"survivors={out['multiple_testing']['survivors_q05_and_t3']}")
    print("（幸存判定在 h=3/5；下表是主期限 h=1，两者不是同一行数据。"
          "逐期限见 --detail full）")
    print()
    header = (f"{'factor':<22}{'IC':>9}{'t_hac':>8}{'p':>8}{'n':>6}"
              f"{'minIC@t3':>10}{'ratio':>7}{'vsPct':>7}")
    print(header)
    print("-" * len(header))

    def fmt(v, width, nd=4):
        return f"{v:>{width}.{nd}f}" if isinstance(v, (int, float)) else f"{'—':>{width}}"

    for row in out["rows"]:
        if row.get("status") != "measured":
            print(f"{row.get('key', '?'):<22}{row.get('status')}")
            continue
        print(f"{row['key']:<22}"
              f"{fmt(row.get('ic_neutral_mean'), 9)}"
              f"{fmt(row.get('t_stat_hac'), 8, 2)}"
              f"{fmt(row.get('p_value'), 8, 3)}"
              f"{fmt(row.get('n_days'), 6, 0)}"
              f"{fmt(row.get('min_ic_at_t3'), 10)}"
              f"{fmt(row.get('observed_over_required'), 7, 2)}"
              f"{fmt(row.get('ic_vs_pct'), 7, 2)}")
    print()
    print("minIC@t3 = 本样本在 |t|>3 下能分辨的最小 IC；ratio = 实测 / 该门槛。")
    print("ratio < 1 表示这一族的『不显著』不能读成『没有效应』。")
    print("Read-only: no action / verdict / score / ranking / target price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
