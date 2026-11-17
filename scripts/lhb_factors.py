"""Run the LHB factor family from the command line.

    .venv/Scripts/python.exe scripts/lhb_factors.py
    .venv/Scripts/python.exe scripts/lhb_factors.py --start 20251027 --detail full
    .venv/Scripts/python.exe scripts/lhb_factors.py --json > out.json

Read-only: it opens the dataset store and nothing else. No ledger, no order,
no hypothesis write. The family is ``ashare_lhb`` (16 tests) and its q-values
are NOT comparable with the ``ashare_daily`` 36-test family.
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
from service.research import factors_lhb  # noqa: E402
from service.trading.market import MarketRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="LHB cross-sectional factor read")
    parser.add_argument("--data-root", default=str(ROOT / "data"))
    parser.add_argument("--start", default="20251027")
    parser.add_argument("--end", default=None, help="default: latest daily partition")
    parser.add_argument("--detail", choices=("summary", "full"), default="summary")
    parser.add_argument("--horizons", default=None,
                        help="default 1,3,5,10; dense / 1-5 / comma list")
    parser.add_argument("--no-neutralise", action="store_true")
    parser.add_argument("--json", action="store_true", help="print raw payload")
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
    except Exception:  # noqa: BLE001 - industry neutralisation is optional
        sw = None

    out = factors_lhb.panel_lhb(
        store, start=args.start, end=str(end), sw_table=sw,
        horizons=args.horizons or factors_lhb.HORIZONS,
        detail=args.detail, neutralise=not args.no_neutralise,
    )
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"[{out.get('status')}] {out.get('error')}", file=sys.stderr)
        return 1

    print(f"family={out['family']}  window={out['window']['start']}..{out['window']['end']}")
    print(f"horizons={out['horizons']}  tests={out['multiple_testing']['n_tests']}  "
          f"survivors={out['multiple_testing']['survivors_q05_and_t3']}")
    comp = out.get("sample_composition") or {}
    if comp.get("status") == "measured":
        print(f"sample n={comp['n']}  at_limit_up={comp['share_at_limit_up']:.1%}  "
              f"at_limit_down={comp['share_at_limit_down']:.1%}")
    print()
    header = f"{'factor':<22}{'IC':>9}{'t_hac':>8}{'p':>8}{'n':>6}{'orth_t':>8}{'exUnbuy':>10}{'vsPct':>8}"
    print(header)
    print("-" * len(header))
    for row in out["rows"]:
        if row.get("status") != "measured":
            print(f"{row.get('key', '?'):<22}{row.get('status')}")
            continue
        def fmt(v, width, nd=4):
            return f"{v:>{width}.{nd}f}" if isinstance(v, (int, float)) else f"{'—':>{width}}"
        print(f"{row['key']:<22}"
              f"{fmt(row.get('ic_neutral_mean'), 9)}"
              f"{fmt(row.get('t_stat_hac'), 8, 2)}"
              f"{fmt(row.get('p_value'), 8, 3)}"
              f"{fmt(row.get('n_days'), 6, 0)}"
              f"{fmt(row.get('t_orthogonal_pct'), 8, 2)}"
              f"{fmt(row.get('ic_ex_unbuyable'), 10)}"
              f"{fmt(row.get('ic_vs_pct'), 8, 2)}")
    print()
    print("orth_t = 控制当日涨幅后的 HAC t；exUnbuy = 剔除 T+1 一字板后的 IC；")
    print("vsPct   = 与当日涨幅的截面秩相关（>0.4 说明该因子大半是涨幅的代理）。")
    print("Read-only: no action / verdict / score / ranking / target price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
