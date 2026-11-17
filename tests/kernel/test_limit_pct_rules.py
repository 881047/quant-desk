"""涨跌停幅度：代码里的口径必须是交易所的口径。

这里钉住三件事，都是"改了不会报错、只会静默做错决定"的那一类：

1. **主板 ST 是 10%，不是 5%。** 沪深北三大交易所 2026-04-24 修订《交易规则》，
   沪深主板风险警示股票（ST/*ST）的涨跌幅由 5% 调整为 10%，2026-07-06 起施行。
   本仓库曾一直用 5%，于是之后每只主板 ST 都带着半个错的带宽：实盘监控在 -5%
   就喊"跌停"（真跌停在 -10%），盘中撮合算出的涨跌停价也是错的。

2. **未完成股改股恒为 5%。** 上交所《关于对未股改股票实施差异化交易监管的通知》
   （上证发〔2014〕3号，现行有效）：未股改公司简称前冠 "S"，涨跌幅 ±5%，
   **优先于**风险警示规则——佳通轮胎 2023-05-16 公告明写撤销 ST 后"日涨跌幅
   限制不变，仍为 5%"。

3. **历史查询要用当时的规则。** 2026-07-06 之前主板 ST 是 5%，用今天的规则去
   解释 6 月的行情，等于拿新制度解读旧数据。

第二、三条需要交易所自己的数字，来源有两个，缺一不可：

* **``fixtures/limit_bands.csv``**（随代码入库）：几行真实报价，覆盖每个口径分支。
  它让"代码自己走样"在没有 ``data/parquet`` 的环境里也会红——``data/parquet/``
  被 ``.gitignore`` 排除，只靠本地数据的反查在干净 clone 上会整段 skip，而套件
  仍全绿，这正是 C-05 指出的那个洞。
* **``data/parquet/stk_limit``**（本地数据）：全量反查。交易所**改了规则**只有它能
  发现——样本抄的是历史，不会自己更新。
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from pathlib import Path

import pytest

from service.adapters.symbols import is_unreformed, limit_pct
from service.kernel.units import (
    LIMIT_PCT_BSE,
    LIMIT_PCT_CB,
    LIMIT_PCT_GROWTH,
    LIMIT_PCT_MAIN,
    LIMIT_PCT_ST,
    LIMIT_PCT_ST_LEGACY,
    LIMIT_PCT_UNREFORMED,
    ST_BAND_CHANGE_DATE,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).with_name("fixtures") / "limit_bands.csv"


# ── 口径本身 ────────────────────────────────────────────────────────────────
def test_main_board_is_ten_percent():
    assert limit_pct("600000.SH") == LIMIT_PCT_MAIN == Decimal("0.10")
    assert limit_pct("000001.SZ") == Decimal("0.10")


def test_main_board_st_is_ten_percent_since_the_rule_change():
    """主板 ST 自 2026-07-06 起与主板普通股同为 10%。"""
    assert LIMIT_PCT_ST == Decimal("0.10")
    assert limit_pct("600000.SH", is_st=True) == Decimal("0.10")
    assert limit_pct("000001.SZ", is_st=True) == Decimal("0.10")


def test_main_board_st_was_five_percent_before_the_rule_change():
    """历史查询必须用当时的规则。"""
    assert LIMIT_PCT_ST_LEGACY == Decimal("0.05")
    before = "20260703"
    assert limit_pct("600000.SH", is_st=True, as_of=before) == Decimal("0.05")
    assert limit_pct("600000.SH", is_st=True, as_of=ST_BAND_CHANGE_DATE) == Decimal("0.10")
    assert limit_pct("600000.SH", is_st=True, as_of="20260707") == Decimal("0.10")


def test_growth_boards_keep_twenty_percent_even_when_st():
    """创业板/科创板 ST 仍是 20%——这就是 is_st 不能先判的原因。"""
    assert limit_pct("300001.SZ") == Decimal("0.20")
    assert limit_pct("300001.SZ", is_st=True) == Decimal("0.20")
    assert limit_pct("688001.SH") == Decimal("0.20")
    assert limit_pct("688001.SH", is_st=True) == Decimal("0.20")


def test_beijing_and_convertible_bands():
    assert limit_pct("430047.BJ") == Decimal("0.30")
    assert limit_pct("920002.BJ") == Decimal("0.30")
    assert limit_pct("113050.SH") == Decimal("0.20")
    assert LIMIT_PCT_BSE == Decimal("0.30")
    assert LIMIT_PCT_CB == Decimal("0.20")
    assert LIMIT_PCT_GROWTH == Decimal("0.20")


# ── 未股改股：S 前缀，恒为 5%，优先于 ST ───────────────────────────────────
def test_unreformed_names_are_recognised():
    assert is_unreformed("S佳通") is True
    assert is_unreformed("S湘火炬") is True
    assert is_unreformed("SST佳通") is True
    assert is_unreformed("S*ST佳通") is True
    # 风险警示股也以 S 开头，但那是 ST 的 S，不是未股改的标记。
    assert is_unreformed("ST椰岛") is False
    assert is_unreformed("*ST瑞贝卡") is False
    assert is_unreformed("ST云网") is False
    assert is_unreformed("") is False
    assert is_unreformed(None) is False


def test_unreformed_stock_is_five_percent_even_while_it_is_st():
    """S 标记优先于风险警示：佳通 2021–2023 带 ST 时带宽也全是 5%。"""
    assert LIMIT_PCT_UNREFORMED == Decimal("0.05")
    assert limit_pct("600182.SH", name="S佳通") == Decimal("0.05")
    assert limit_pct("600182.SH", name="SST佳通", is_st=True) == Decimal("0.05")
    assert limit_pct("600182.SH", name="S*ST佳通", is_st=True) == Decimal("0.05")
    # 规则与日期无关：未股改不随主板 ST 改 10% 而变。
    assert limit_pct("600182.SH", name="S佳通", as_of="20260701") == Decimal("0.05")
    assert limit_pct("600182.SH", name="S佳通", as_of="20261102") == Decimal("0.05")


def test_a_plain_st_name_is_not_treated_as_unreformed():
    assert limit_pct("600238.SH", name="ST椰岛", is_st=True) == Decimal("0.10")


# ── 没有本地数据时的反查：样本随代码入库 ────────────────────────────────────
def _recorded_bands() -> list[dict[str, str]]:
    """``fixtures/limit_bands.csv``：交易所当天印出来的价格，随代码入库。

    解析用 ``csv`` 而不是 pandas：这条断言的**全部意义**是"没有数据也能跑"，
    多引一个 ``importorskip`` 就多一个能把它整段 skip 掉的理由。

    ``utf-8-sig`` 不是随手写的：Windows 上的编辑器会给 CSV 补一个 BOM，
    而 BOM 会让第一行注释认不出来，最后表现成 ``KeyError: 'expect_pct'``
    ——一个看着像"样本文件坏了"、其实是编码问题的错。
    """
    text = SAMPLE.read_text(encoding="utf-8-sig")
    body = "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.startswith("#"))
    return list(csv.DictReader(io.StringIO(body)))


def test_recorded_exchange_bands_match_the_code_constants():
    """代码常量 vs 交易所印过的价——**不依赖 data/parquet**。

    这一条是 C-05 的正题。原来四条反查全部读本地 parquet，而 ``data/parquet/``
    被 ``.gitignore`` 排除，于是干净 clone 上整段 skip：``units.LIMIT_PCT_*``
    改成什么都不会有人发现，套件仍全绿、覆盖率仍达标。

    它盖不住"交易所改规则"——那需要交易所的新价，只有上面的实时反查能发现。
    它盖的是另一半：**代码自己走样**。两者缺一，防线就有一半是空的。
    """
    rows = _recorded_bands()
    assert rows, f"{SAMPLE.name} 是空的"
    for row in rows:
        band = limit_pct(row["ts_code"], is_st=row["is_st"] == "1",
                         name=row["name"], as_of=row["trade_date"])
        where = f"{row['ts_code']} {row['trade_date']}（{row['note']}）"
        assert band == Decimal(row["expect_pct"]), (
            f"{where}：代码给的档位是 {band}，交易所当时是 {row['expect_pct']}")
        pre = Decimal(row["pre_close"])
        assert _expected_up_limit(pre, band) == Decimal(row["up_limit"]), (
            f"{where}：{pre} 的涨停价按 {band} 算出来是 "
            f"{_expected_up_limit(pre, band)}，交易所印的是 {row['up_limit']}")
        assert _expected_down_limit(pre, band) == Decimal(row["down_limit"]), (
            f"{where}：{pre} 的跌停价按 {band} 算出来是 "
            f"{_expected_down_limit(pre, band)}，交易所印的是 {row['down_limit']}")


def test_the_recorded_sample_covers_every_band_the_code_can_return():
    """样本的**选择**也要有断言，否则它是一张会悄悄变窄的网。

    少一行 = 少覆盖一个分支，而上面那条照样全绿。这是白名单式测试的通病
    （C-06 的教训）：被覆盖的会红，没覆盖的永远绿。所以这里直接问代码：
    你能返回的每个档位，样本里都有吗？新规前后两边的日子都有吗？
    """
    rows = _recorded_bands()
    covered = {Decimal(row["expect_pct"]) for row in rows}
    required = {LIMIT_PCT_MAIN, LIMIT_PCT_ST, LIMIT_PCT_ST_LEGACY,
                LIMIT_PCT_UNREFORMED, LIMIT_PCT_GROWTH, LIMIT_PCT_BSE}
    assert required <= covered, f"样本缺这些档位：{sorted(required - covered)}"

    days = [row["trade_date"] for row in rows]
    assert any(day < ST_BAND_CHANGE_DATE for day in days), (
        f"样本里没有 {ST_BAND_CHANGE_DATE} 之前的行，as_of 的历史分支就没被走过")
    assert any(day >= ST_BAND_CHANGE_DATE for day in days), (
        f"样本里没有 {ST_BAND_CHANGE_DATE} 之后的行，新规分支就没被走过")
    assert any(is_unreformed(row["name"]) for row in rows), (
        "样本里没有未股改（S 开头）的名字，那一支的优先级就没被走过")


def test_the_board_band_table_in_the_js_audit_still_agrees():
    """``plugin/consistency_audit.mjs`` 里还有一份 board→band 表，把它钉在代码上。

    那第二份是**故意的**：审计的价值在于独立复述一遍交易所规则，直接读
    ``units`` 就成了自己跟自己比，永远相等。但独立复述的代价是会腐烂——
    于是这里让它烂不了：代码改了而 JS 没改，这条会红。

    要求句式恰好命中一次（C-06 的教训）：命中 0 次说明那行被改写、断言已
    静默失效，而它仍会继续报绿。
    """
    text = (ROOT / "plugin" / "consistency_audit.mjs").read_text(encoding="utf-8")
    found = re.findall(
        r"\{\s*MAIN:\s*([\d.]+),\s*CHINEXT:\s*([\d.]+),\s*STAR:\s*([\d.]+),"
        r"\s*BSE:\s*([\d.]+)\s*\}", text)
    assert len(found) == 1, (
        f"审计里的档位表句式变了（命中 {len(found)} 次），这条断言已经失效")
    for board, value in zip(("MAIN", "CHINEXT", "STAR", "BSE"), found[0]):
        code = {"MAIN": "600000.SH", "CHINEXT": "300750.SZ",
                "STAR": "688981.SH", "BSE": "920992.BJ"}[board]
        assert Decimal(value) == limit_pct(code), (
            f"审计说 {board} 是 {value}，代码算 {code} 得 {limit_pct(code)}")


# ── 用交易所自己的数据反查 ──────────────────────────────────────────────────
def _exchange_bands(year: str = "2026"):
    """(代码, 交易日, 实际带宽) —— 来自 stk_limit 与 daily 的 pre_close。

    同时保留交易所给的涨停价：带宽是百分比，而交易所先把价格四舍五入到分，
    昨收 0.45 元时"10%"会印成 0.50（+11.1%）。比百分比会撞上这层噪声，
    比价格才是同一把尺子。
    """
    pd = pytest.importorskip("pandas")
    lim_path = ROOT / "data" / "parquet" / "stk_limit" / f"{year}.parquet"
    daily_path = ROOT / "data" / "parquet" / "daily" / f"{year}.parquet"
    if not lim_path.exists() or not daily_path.exists():
        pytest.skip(
            "本地没有 stk_limit/daily 数据：交易所反查跳过。代码常量仍由 "
            "fixtures/limit_bands.csv 那两条守着，但样本抄的是历史，"
            "交易所**改了规则**只有这里有数据时才看得出来")
    lim = pd.read_parquet(lim_path)
    daily = pd.read_parquet(daily_path)
    merged = lim.merge(daily[["ts_code", "trade_date", "pre_close"]],
                       on=["ts_code", "trade_date"], how="left")
    merged = merged[merged["pre_close"] > 0]
    merged["band"] = (merged["up_limit"] / merged["pre_close"] - 1).round(4)
    return merged


def _expected_up_limit(pre_close: float, band: float) -> Decimal:
    """交易所口径的涨停价：昨收 × (1+幅度)，四舍五入到分。"""
    from decimal import ROUND_HALF_UP

    price = Decimal(str(pre_close)) * (Decimal(1) + Decimal(str(band)))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _expected_down_limit(pre_close: float, band: float) -> Decimal:
    """跌停价同一把尺子：昨收 × (1−幅度)，四舍五入到分。

    单独写一份而不是 ``-up`` —— 跌停价的四舍五入方向与涨停不同（12.95 的
    跌停是 11.655 → 11.66，不是 11.65），拿涨停反推会正好差一分钱。
    """
    from decimal import ROUND_HALF_UP

    price = Decimal(str(pre_close)) * (Decimal(1) - Decimal(str(band)))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _share_near(series, target: float, tol: float = 0.005) -> float:
    """一个带宽序列里，落在目标带宽 ±tol 上的占比。

    比百分比要留一点容差：交易所把涨跌停价四舍五入到分，所以"10%"在数据里
    是 0.0997–0.1003 这样的一串值，而不是整齐的 0.1。
    """
    return float((series - target).abs().le(tol).mean())


def test_main_board_bands_match_the_exchange_after_the_rule_change():
    """新规之后，主板绝大多数交易日都是 10%。

    允许的例外只有两类，都不改变结论：新股上市前几日不设涨跌幅（数据里
    up_limit 是个占位值），以及极小价的四舍五入噪声。
    """
    merged = _exchange_bands()
    after = merged[merged["trade_date"] >= ST_BAND_CHANGE_DATE]
    main = after[after["ts_code"].str[:3].isin(
        ["000", "001", "002", "003", "600", "601", "603", "605"])]
    main = main[main["up_limit"] < 100000]  # 去掉新股无涨跌幅的占位行
    assert not main.empty, "主板样本为空，反查没有意义"
    share = _share_near(main["band"], 0.10)
    assert share > 0.999, (
        f"主板 10% 带宽只占 {share:.4%}，交易所规则与本仓库的假设已经不一致")


def test_the_exchange_stopped_using_five_percent_after_the_rule_change():
    """主板 5% 带宽在 2026-07-06 之后基本消失，之前是常态。"""
    merged = _exchange_bands()
    main = merged[merged["ts_code"].str[:3].isin(
        ["000", "001", "002", "003", "600", "601", "603", "605"])]
    main = main[main["up_limit"] < 100000]
    before = main[main["trade_date"] < ST_BAND_CHANGE_DATE]
    after = main[main["trade_date"] >= ST_BAND_CHANGE_DATE]
    if before.empty or after.empty:
        pytest.skip("样本不足以比较")
    assert _share_near(before["band"], 0.05) > 0.01, "新规之前主板本该有 5% 带宽"
    assert _share_near(after["band"], 0.05) < 0.01, "新规之后主板不该再有 5% 带宽"


def test_the_unreformed_listing_is_still_capped_at_five_percent():
    """600182.SH（S佳通）至今 5%——它是未股改，不是 ST。

    这是本仓库唯一一只未股改股票，也是"未股改优先于 ST"这条规则的活样本。
    """
    merged = _exchange_bands()
    rows = merged[merged["ts_code"] == "600182.SH"]
    if rows.empty:
        pytest.skip("本地数据里没有 600182.SH")
    bands = set(rows["band"].round(2).unique())
    assert bands == {0.05}, f"600182.SH 的带宽不再是 5%：{bands}"


def test_st_main_board_names_really_trade_at_ten_percent():
    """当前仍是 ST 的主板票，交易所给的带宽是 10%。

    这一条直接盯住本次修复的那个 bug：代码说 5%、交易所说 10%。只看新规
    生效日之后的日子——在那之前主板 ST 本来就是 5%，拿旧日子来断言新规则
    是拿错尺子量东西。
    """
    pd = pytest.importorskip("pandas")
    nc_path = ROOT / "data" / "parquet" / "namechange" / "all.parquet"
    if not nc_path.exists():
        pytest.skip("本地没有 namechange 数据")
    nc = pd.read_parquet(nc_path)
    st_rows = nc[
        nc["name"].astype(str).str.contains("ST")
        & nc["start_date"].astype(str).str.startswith("2026")
        & nc["ts_code"].str[:3].isin(["000", "001", "002", "003", "600", "601", "603", "605"])
    ]
    if st_rows.empty:
        pytest.skip("2026 年没有新的主板 ST")
    merged = _exchange_bands()
    merged = merged[merged["trade_date"] >= ST_BAND_CHANGE_DATE]
    checked = 0
    for code in sorted(st_rows["ts_code"].unique()):
        rows = merged[merged["ts_code"] == code]
        # 去掉无涨跌幅的占位行（新股上市首日 up_limit 是 999999.999）。
        rows = rows[rows["up_limit"] < 100000]
        if rows.empty:
            continue
        # 比价格，不比百分比：昨收 0.45 元时，交易所印的涨停价 0.50 对应
        # +11.1%，那是四舍五入到分的结果，不是规则变了。
        expected = rows["pre_close"].map(lambda p: _expected_up_limit(p, 0.10))
        drift = (rows["up_limit"].astype(float) - expected.astype(float)).abs()
        assert (drift <= 0.01).all(), (
            f"{code} 是主板 ST，交易所涨停价与 10% 带宽算出来的对不上："
            f"{sorted(set(rows['band']))}")
        checked += 1
    if not checked:
        pytest.skip("新规之后的样本不足以断言")
