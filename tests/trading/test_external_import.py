"""Broker exports arrive in three shapes under the same `.xls` name."""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.external import _pick_profile, _read_table, rows_from

HTML = """<html><head><meta charset="utf-8"></head><body>
<table><tr><td colspan="8">成交明细</td></tr>
<tr><th>成交日期</th><th>证券代码</th><th>证券名称</th><th>买卖标志</th>
<th>成交数量</th><th>成交价格</th><th>手续费</th><th>合同编号</th></tr>
<tr><td>20261026</td><td>000700</td><td>模塑科技</td><td>买入</td>
<td>100</td><td>13.07</td><td>5.00</td><td>H1</td></tr>
</table></body></html>"""


def _parse(path):
    frame = _read_table(path)
    picked = _pick_profile(frame)
    assert picked is not None, f"没识别出来：{list(frame.columns)[:8]}"
    profile_name, profile, mapping = picked
    rows, _ = rows_from(frame, mapping, profile)
    return rows


def test_html_table_renamed_to_xls(tmp_path):
    """Web 交割单 exports are HTML; `read_excel` refuses them outright."""
    path = tmp_path / "成交明细.xls"
    path.write_text(HTML, encoding="utf-8")
    rows = _parse(path)
    assert rows[0]["symbol"] == "000700.SZ"
    assert rows[0]["qty"] == 100
    assert rows[0]["fee"] == 5.0


def test_html_table_in_gbk(tmp_path):
    """The same page saved by a Chinese Windows client is GBK, not UTF-8."""
    path = tmp_path / "gbk.xls"
    path.write_bytes(HTML.encode("gbk"))
    rows = _parse(path)
    assert rows[0]["price"] == 13.07


def test_real_workbook_with_a_title_row(tmp_path):
    path = tmp_path / "real.xlsx"
    pd.DataFrame([
        ["成交明细"],
        ["成交日期", "证券代码", "买卖标志", "成交数量", "成交价格", "手续费"],
        ["20261026", "000700", "买入", "100", "13.07", "5.00"],
    ]).to_excel(path, header=False, index=False)
    rows = _parse(path)
    assert rows[0]["qty"] == 100


def test_gbk_csv(tmp_path):
    path = tmp_path / "gbk.csv"
    path.write_text("成交明细\n成交日期,证券代码,买卖标志,成交数量,成交价格\n"
                    "20261026,000700,买入,100,13.07\n", encoding="gbk")
    rows = _parse(path)
    assert rows[0]["trade_date"] == "20261026"


def test_an_unrecognisable_file_says_what_it_saw(tmp_path):
    path = tmp_path / "weird.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    try:
        _read_table(path)
    except RuntimeError as exc:
        assert "表头" in str(exc)
    else:  # pragma: no cover - the frame may parse but must not match a profile
        assert _pick_profile(_read_table(path)) is None


# --------------------------------------------------------------- clipboard
def _grid(*rows: str) -> str:
    """Rows of a copied grid. Tabs are written as \\t, never as spaces."""
    return "\n".join(rows)


HOLDINGS_TSV = _grid(
    "证券代码\t证券名称\t股票余额\t可用余额\t参考成本价\t现价\t参考市值\t浮动盈亏",
    "000700\t模塑科技\t100\t100\t13.120\t13.070\t1307.00\t-5.01",
    "002966\t苏州银行\t200\t0\t9.290\t9.360\t1872.00\t14.00",
    "合计\t\t300\t100\t\t\t3179.00\t8.99",
)

TRADES_TSV = _grid(
    "成交日期\t成交时间\t证券代码\t买卖标志\t成交数量\t成交价格\t手续费\t合同编号",
    "20261026\t14:59:03\t000700\t买入\t100\t13.07\t5.01\tHT1",
)


def _from_text(text):
    from scripts.external import _frame_from_text, _promote, known_headers

    known = known_headers()
    frame = _promote(_frame_from_text(text, known, "剪贴板"), known, "剪贴板")
    picked = _pick_profile(frame)
    assert picked is not None, f"没识别出来：{list(frame.columns)[:8]}"
    profile_name, profile, mapping = picked
    rows, skipped = rows_from(frame, mapping, profile)
    return profile_name, rows, skipped


def test_clipboard_positions_are_recognised_and_totals_skipped():
    """A 同花顺 holdings copy: tab separated, plus a 合计 row that is not a stock."""
    profile_name, rows, skipped = _from_text(HOLDINGS_TSV)
    assert profile_name == "positions"
    assert [r["symbol"] for r in rows] == ["000700.SZ", "002966.SZ"]
    assert rows[1]["qty"] == 200
    assert rows[1]["available"] == 0, "T+1: bought today cannot be sold today"
    assert rows[0]["cost"] == 13.12
    assert skipped == [], "the 合计 footer is not a data row or a partial-read error"


def test_clipboard_trades_are_distinguished_from_holdings():
    """Same grid, different columns: the profile is chosen by required fields."""
    profile_name, rows, _ = _from_text(TRADES_TSV)
    assert profile_name == "trades"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["qty"] == 100
    assert rows[0]["trade_date"] == "20261026"


def test_clipboard_that_is_not_a_table_says_so():
    """Copying a paragraph must fail with the text it saw, not import garbage."""
    from scripts.external import _frame_from_text, known_headers

    try:
        _frame_from_text("今天大盘跌了，我的持仓还好\n明天再看吧", known_headers(), "剪贴板")
    except RuntimeError as exc:
        assert "分隔符" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("纯文本不该被当成表格")


def test_paste_writes_a_snapshot_and_reconciles(tmp_path, monkeypatch):
    """End to end: paste writes a snapshot, and a position with no trades shows up."""
    from scripts import external

    monkeypatch.setattr(external, "DB", tmp_path / "external.sqlite")
    external.main(["add", "--symbol", "000700", "--side", "BUY", "--qty", "100",
                   "--price", "13.07", "--date", "20261026", "--fee", "5.01"])
    rc = external.main(["paste", "--text", HOLDINGS_TSV, "--as-of", "20261027"])
    assert rc == 0

    from service.trading.external import ExternalStore, reconcile

    store = ExternalStore(tmp_path / "external.sqlite")
    snap = store.snapshot("20261027")
    assert set(snap) == {"000700.SZ", "002966.SZ"}
    gap = reconcile(store, as_of="20261027")
    assert gap["ok"] is False
    assert [m["symbol"] for m in gap["mismatches"]] == ["002966.SZ"], \
        "002966 appears in the statement but not in the trade ledger"

    only_a = "\n".join(HOLDINGS_TSV.splitlines()[:2])
    assert external.main(["paste", "--text", only_a, "--as-of", "20261027"]) == 0
    assert set(store.snapshot("20261027")) == {"000700.SZ"}, \
        "A+B followed by a complete A-only snapshot must remove B"


def test_a_partial_position_paste_cannot_replace_the_snapshot(tmp_path, monkeypatch):
    from scripts import external
    from service.trading.external import ExternalStore

    db = tmp_path / "external.sqlite"
    monkeypatch.setattr(external, "DB", db)
    store = ExternalStore(db)
    store.replace_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 100}])
    before = store.snapshot("20261027")

    partial = "\n".join(HOLDINGS_TSV.splitlines()[:-1] + [
        "BAD\t坏行\t100\t100\t13.120\t13.070\t1307.00\t0",
    ])
    assert external.main(["paste", "--text", partial, "--as-of", "20261027"]) == 2
    assert store.snapshot("20261027") == before

    parsed = {"kind": "positions", "profile": "positions",
              "rows": [{"symbol": "002966.SZ", "qty": 200}], "skipped": ["bad row"]}
    result = external.apply_parsed(store, parsed, source="test", as_of="20261027")
    assert result["ok"] is False
    assert store.snapshot("20261027") == before


@pytest.mark.parametrize("flag,value", [
    ("--qty", "0"),
    ("--qty", "-100"),
    ("--price", "0"),
    ("--price", "-13.07"),
    ("--price", "nan"),
    ("--fee", "-0.01"),
    ("--fee", "nan"),
])
def test_add_rejects_non_positive_amounts_without_creating_a_ledger(
        tmp_path, monkeypatch, capsys, flag, value):
    from scripts import external

    db = tmp_path / "external.sqlite"
    monkeypatch.setattr(external, "DB", db)
    values = {"--qty": "100", "--price": "13.07", "--fee": "0"}
    values[flag] = value
    args = ["add", "--symbol", "000700", "--side", "BUY"]
    for name, item in values.items():
        args.extend([name, item])
    assert external.main(args) == 2
    assert "[fail]" in capsys.readouterr().out
    assert not db.exists()
