/* GENERATED FILE — DO NOT EDIT.
 * Run: .venv/Scripts/python.exe scripts/export_units.py --write
 * Source of truth: service/vocabulary.py (UNIT_CONVENTIONS).
 * scripts/export_units.py --check fails if this file is stale.
 */

export const UNITS = {
  "by_endpoint": {
    "/context_pack": {
      "account.day_pnl_pct": "fraction (0..1)"
    },
    "/discover": {
      "market.breadth_above_ma20": "fraction (0..1)",
      "market.breadth_above_ma60": "fraction (0..1)",
      "market.ew_return_1d": "fraction (0..1)",
      "market.ew_return_20d": "fraction (0..1)",
      "market.ew_return_5d": "fraction (0..1)",
      "market.realised_vol_20d": "fraction (0..1)",
      "market.vol_percentile_1y": "fraction (0..1)"
    },
    "/facts": {
      "account.day_pnl_pct": "fraction (0..1)",
      "breadth.advancers": "count",
      "breadth.decliners": "count",
      "breadth.equal_weight_return": "fraction (0..1)",
      "breadth.flat": "count",
      "breadth.median_return": "fraction (0..1)"
    },
    "/market/pools": {
      "pools.limit_up[].amount": "cny",
      "pools.limit_up[].close": "cny",
      "pools.limit_up[].lot_cost_cny": "cny",
      "pools.limit_up[].pct_chg": "percent (0..100)",
      "pools.limit_up[].pct_of_equity": "percent (0..100)",
      "pools.limit_up[].seal_amount": "cny",
      "pools.limit_up[].seal_ratio": "fraction (0..1)",
      "pools.limit_up[].seal_ratio_pct": "percent (0..100)",
      "pools.limit_up[].seal_window_no": "ordinal (1..4)"
    },
    "/market/pools.sentiment": {
      "sentiment.break_rate": "fraction (0..1)"
    },
    "/market/sector": {
      "industries[].amount_cny": "cny",
      "industries[].main_net_inflow_cny": "cny",
      "industries[].members": "count",
      "industries[].pct_chg": "percent (0..100)",
      "industries[].pct_chg_eq": "percent (0..100)",
      "industries[].traded": "count"
    },
    "/portfolio": {
      "daily_pnl_pct": "fraction (0..1)"
    },
    "/regime": {
      "breadth_above_ma20": "fraction (0..1)",
      "equal_weight_index": "index level (base 1.0)",
      "vol_20d": "fraction (0..1)",
      "vol_percentile_1y": "fraction (0..1)"
    },
    "/research/screen": {
      "candidates[].adv20_cny": "cny",
      "candidates[].close": "cny",
      "candidates[].mom20": "percent (0..100)",
      "candidates[].pct_chg": "percent (0..100)"
    },
    "/risk": {
      "daily_pnl_pct": "fraction (0..1)",
      "drawdown": "fraction (0..1)"
    },
    "/risk/exposure": {
      "cash_weight": "fraction (0..1)",
      "invested_weight": "fraction (0..1)"
    }
  },
  "naming_exceptions": [
    {
      "actual": "fraction (0..1)",
      "endpoints": [
        "/risk",
        "/portfolio"
      ],
      "field": "daily_pnl_pct",
      "why": "内核 engine.py 里是 (equity - day_start_equity)/day_start_equity，名字带 _pct 但值是小数；前端 pct() 会 ×100。改名会动到内核、账本与既有消费者，登记为例外而不是重命名。"
    },
    {
      "actual": "fraction (0..1)",
      "endpoints": [
        "/facts",
        "/context_pack"
      ],
      "field": "day_pnl_pct",
      "why": "daily_pnl_pct 在 facts / context_pack 里的别名，同样不是百分数。"
    }
  ],
  "rule": "字段量纲以接口响应的 units / GET /meta/vocabulary 的 unit_conventions 为准；以 _pct 结尾的字段原则上是百分数（值已 ×100），但 naming_exceptions 里的例外是小数，比例字段是 0..1 的 fraction —— 不要按名字猜。",
  "units_text": "- **数值量纲以接口响应的 `units` / `GET /meta/vocabulary.unit_conventions` 为准，不要按字段名猜**：\n  字段量纲以接口响应的 units / GET /meta/vocabulary 的 unit_conventions 为准；以 _pct 结尾的字段原则上是百分数（值已 ×100），但 naming_exceptions 里的例外是小数，比例字段是 0..1 的 fraction —— 不要按名字猜。\n  /context_pack：小数 day_pnl_pct。\n  /discover：小数 breadth_above_ma20, breadth_above_ma60, ew_return_1d, ew_return_20d, ew_return_5d, realised_vol_20d, vol_percentile_1y。\n  /facts：小数 day_pnl_pct, equal_weight_return, median_return。\n  /market/pools：小数 seal_ratio；百分数 pct_chg, pct_of_equity, seal_ratio_pct。\n  /market/sector：百分数 pct_chg, pct_chg_eq。\n  /portfolio：小数 daily_pnl_pct。\n  /regime：小数 breadth_above_ma20, vol_20d, vol_percentile_1y。\n  /research/screen：百分数 mom20, pct_chg。\n  /risk：小数 daily_pnl_pct, drawdown。\n  /risk/exposure：小数 cash_weight, invested_weight。\n  例外 `daily_pnl_pct`（/risk, /portfolio）名字带 _pct 但值是 fraction (0..1)，web 的 pct() 会 ×100。\n  例外 `day_pnl_pct`（/facts, /context_pack）名字带 _pct 但值是 fraction (0..1)，web 的 pct() 会 ×100。\n  把小数写成百分数（或反过来）是 100 倍的错；web 的 fmtByUnits() 由 units 自动挑格式化函数，不要手写换算。"
};

export const UNITS_TEXT = "- **数值量纲以接口响应的 `units` / `GET /meta/vocabulary.unit_conventions` 为准，不要按字段名猜**：\n  字段量纲以接口响应的 units / GET /meta/vocabulary 的 unit_conventions 为准；以 _pct 结尾的字段原则上是百分数（值已 ×100），但 naming_exceptions 里的例外是小数，比例字段是 0..1 的 fraction —— 不要按名字猜。\n  /context_pack：小数 day_pnl_pct。\n  /discover：小数 breadth_above_ma20, breadth_above_ma60, ew_return_1d, ew_return_20d, ew_return_5d, realised_vol_20d, vol_percentile_1y。\n  /facts：小数 day_pnl_pct, equal_weight_return, median_return。\n  /market/pools：小数 seal_ratio；百分数 pct_chg, pct_of_equity, seal_ratio_pct。\n  /market/sector：百分数 pct_chg, pct_chg_eq。\n  /portfolio：小数 daily_pnl_pct。\n  /regime：小数 breadth_above_ma20, vol_20d, vol_percentile_1y。\n  /research/screen：百分数 mom20, pct_chg。\n  /risk：小数 daily_pnl_pct, drawdown。\n  /risk/exposure：小数 cash_weight, invested_weight。\n  例外 `daily_pnl_pct`（/risk, /portfolio）名字带 _pct 但值是 fraction (0..1)，web 的 pct() 会 ×100。\n  例外 `day_pnl_pct`（/facts, /context_pack）名字带 _pct 但值是 fraction (0..1)，web 的 pct() 会 ×100。\n  把小数写成百分数（或反过来）是 100 倍的错；web 的 fmtByUnits() 由 units 自动挑格式化函数，不要手写换算。";

export function unitsFor(endpoint) {
  const fields = UNITS.by_endpoint[endpoint] || {};
  const out = {};
  for (const [key, scale] of Object.entries(fields)) {
    out[key.split('.').pop()] = scale;
  }
  if (endpoint === '/market/pools') {
    for (const [key, scale] of Object.entries(UNITS.by_endpoint['/market/pools.sentiment'])) {
      out[key.split('.').pop()] = scale;
    }
  }
  return out;
}
