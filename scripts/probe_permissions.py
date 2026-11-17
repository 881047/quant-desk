"""Probe data permissions and coverage before building strategies.

Usage:
    .venv/Scripts/python.exe scripts/probe_permissions.py

It writes data/permission_report.json and prints a compact summary.
No token is printed. Missing TUSHARE_TOKEN is reported, not fatal.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REPORT = {"generated_at": datetime.now().isoformat(timespec="seconds"), "tushare": {}, "eastmoney": {}, "akshare": {}}


def _safe(name: str, fn, bucket: dict, **kwargs):
    try:
        df = fn(**kwargs)
        if hasattr(df, "shape"):
            rows, cols = int(df.shape[0]), list(map(str, df.columns))[:30]
        else:
            rows, cols = 1, []
        bucket[name] = {"ok": True, "rows": rows, "columns": cols}
        print(f"[ok] {name}: rows={rows} cols={len(cols)}")
        return df
    except Exception as exc:  # noqa: BLE001
        bucket[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[fail] {name}: {type(exc).__name__}: {exc}")
        return None


def probe_tushare() -> None:
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    REPORT["tushare"]["token_present"] = bool(token)
    if not token:
        REPORT["tushare"]["skipped"] = "TUSHARE_TOKEN missing in .env"
        print("[skip] TUSHARE_TOKEN missing")
        return
    try:
        import tushare as ts

        pro = ts.pro_api(token)
    except Exception as exc:  # noqa: BLE001
        REPORT["tushare"]["init_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[fail] tushare init: {exc}")
        return

    today = date.today()
    start = (today - timedelta(days=20)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    recent_trade = "20261023"

    calls = {
        "stock_basic_L": (pro.stock_basic, {"exchange": "", "list_status": "L", "fields": "ts_code,name,industry,market,list_date,delist_date,list_status"}),
        "stock_basic_D": (pro.stock_basic, {"exchange": "", "list_status": "D", "fields": "ts_code,name,list_date,delist_date,list_status"}),
        "trade_cal": (pro.trade_cal, {"exchange": "SSE", "start_date": start, "end_date": end}),
        "daily": (pro.daily, {"trade_date": recent_trade}),
        "adj_factor": (pro.adj_factor, {"trade_date": recent_trade}),
        "daily_basic": (pro.daily_basic, {"trade_date": recent_trade}),
        "stk_limit": (pro.stk_limit, {"trade_date": recent_trade}),
        "suspend_d": (pro.suspend_d, {"start_date": start, "end_date": end}),
        "dividend": (pro.dividend, {"ts_code": "000001.SZ"}),
        "namechange": (pro.namechange, {"ts_code": "000001.SZ"}),
        "index_daily": (pro.index_daily, {"ts_code": "000300.SH", "start_date": start, "end_date": end}),
        "index_weight": (pro.index_weight, {"index_code": "000300.SH", "trade_date": "20260930"}),
        "fund_basic": (pro.fund_basic, {"market": "E"}),
        "fund_daily": (pro.fund_daily, {"ts_code": "510300.SH", "start_date": start, "end_date": end}),
        "fund_adj": (pro.fund_adj, {"ts_code": "510300.SH", "start_date": start, "end_date": end}),
        "fund_nav": (pro.fund_nav, {"ts_code": "510300.SH", "start_date": start, "end_date": end}),
        "fund_share": (pro.fund_share, {"ts_code": "510300.SH", "start_date": start, "end_date": end}),
        "fina_indicator": (pro.fina_indicator, {"ts_code": "000001.SZ", "start_date": "20250101", "end_date": end}),
        "moneyflow": (pro.moneyflow, {"trade_date": recent_trade}),
        "moneyflow_hsgt": (pro.moneyflow_hsgt, {"start_date": start, "end_date": end}),
        "margin_detail": (pro.margin_detail, {"trade_date": recent_trade}),
        "limit_list_d": (pro.limit_list_d, {"trade_date": recent_trade}),
        "anns_d": (pro.anns_d, {"start_date": start, "end_date": end}),
        "stk_mins": (pro.stk_mins, {"ts_code": "000001.SZ", "freq": "5min", "start_date": f"{recent_trade} 09:00:00", "end_date": f"{recent_trade} 15:00:00"}),
        "forecast": (pro.forecast, {"ts_code": "000001.SZ", "start_date": "20250101", "end_date": end}),
    }
    for name, (fn, kwargs) in calls.items():
        _safe(name, fn, REPORT["tushare"], **kwargs)


def probe_eastmoney() -> None:
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    base = "https://push2ex.eastmoney.com"
    endpoints = {
        "zt_pool": ("/getTopicZTPool", {"dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "200", "sort": "fbt:asc"}),
        "zb_pool": ("/getTopicZBPool", {"dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "200", "sort": "fbt:asc"}),
        "dt_pool": ("/getTopicDTPool", {"dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "200", "sort": "fund:asc"}),
    }
    for name, (path, params) in endpoints.items():
        try:
            with httpx.Client(timeout=15, headers=headers) as client:
                r = client.get(base + path, params={**params, "ut": "7eea3edcaed734bea9cbfc24409ed989", "date": "20261023"})
                r.raise_for_status()
                payload = r.json()
            data = payload.get("data") or {}
            items = data.get("pool") or data.get("list") or []
            first = items[0] if items else {}
            REPORT["eastmoney"][name] = {
                "ok": True,
                "rows": len(items),
                "keys": list(first.keys())[:40] if isinstance(first, dict) else [],
            }
            print(f"[ok] eastmoney {name}: rows={len(items)}")
        except Exception as exc:  # noqa: BLE001
            REPORT["eastmoney"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"[fail] eastmoney {name}: {exc}")


def probe_akshare() -> None:
    try:
        import akshare as ak
    except Exception as exc:  # noqa: BLE001
        REPORT["akshare"]["init_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[fail] akshare init: {exc}")
        return

    calls = {
        "zt_pool": (ak.stock_zt_pool_em, {"date": "20261023"}),
        "zt_pool_previous": (ak.stock_zt_pool_previous_em, {"date": "20261023"}),
        "zt_pool_strong": (ak.stock_zt_pool_strong_em, {"date": "20261023"}),
        "zt_pool_dtgc": (ak.stock_zt_pool_dtgc_em, {"date": "20261023"}),
        "stock_zh_a_hist": (ak.stock_zh_a_hist, {"symbol": "000001", "period": "daily", "start_date": "20260101", "end_date": "20261023", "adjust": "qfq"}),
        "fund_etf_hist_em": (ak.fund_etf_hist_em, {"symbol": "510300", "period": "daily", "start_date": "20260101", "end_date": "20261023", "adjust": "qfq"}),
        "lhb_detail": (ak.stock_lhb_detail_em, {"start_date": "20261020", "end_date": "20261023"}),
    }
    for name, (fn, kwargs) in calls.items():
        _safe(name, fn, REPORT["akshare"], **kwargs)


def main() -> None:
    probe_tushare()
    probe_eastmoney()
    probe_akshare()
    out = ROOT / "data" / "permission_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
