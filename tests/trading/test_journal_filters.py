"""跨日交接要真的能接住：按 kind 取、按日期取、单条打开、以及读回报告全文。

2026-11-03 之前的形态有四处在漏水，每一处都让"上一轮写了什么"读不回来：

1. ``search_journal`` 只有 query 与 limit。``duty.py`` 于是取最近 20 条再在
   Python 里筛 ``kind == "review"``——而 note 的产出速度是 review 的 4 倍，
   窗口一旦滑过去，这个检查就会在真有复盘的日子里报"还没有复盘记录"。
2. 摘要只给 ``text[:120]``。复盘是"战果在前、行动项在后"写的，于是
   "下次改一条：…" 从来没进过任何摘要。
3. 没有按 id 单取。想读一条 700 字的复盘只能用 ``full=true``，那是整页全文
   （约 5 倍、硬顶 8 条）。
4. ``reports/`` 下 119 份 markdown 没有任何路由能打开——而 journal 里明确写着
   "全文见 reports/review/20261103.md"。那个指针下一轮是断的。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def _seed(desk, rows):
    """Write entries then pin their timestamps — the filter is on ts, not on write order."""
    ids = []
    for row in rows:
        row_id = desk.paper.add_journal(row["text"], kind=row["kind"],
                                        symbol=row.get("symbol"))
        with desk.paper._conn() as conn:
            conn.execute("UPDATE journal SET ts=? WHERE id=?", (row["ts"], row_id))
        ids.append(row_id)
    return ids


def test_kind_filter_does_not_depend_on_how_many_notes_came_first(desk, client):
    """一条 review 埋在 20 条 note 后面，仍然要取得到。

    这就是 duty.py 原来的写法失效的方式：先取 20 条再筛选，窗口满了就返回空，
    而调用方分不清"今天没写复盘"和"我没看到"。
    """
    _seed(desk, [{"kind": "review", "ts": "2026-11-01T19:30:00+08:00",
                  "text": "11-01 复盘：对 6 / 错 3。"}])
    _seed(desk, [{"kind": "note", "ts": f"2026-11-02T{9 + i % 8:02d}:00:00+08:00",
                  "text": f"11-02 巡检第 {i} 条：无异常。"} for i in range(24)])

    body = client.get("/journal", params={"kind": "review"}, headers=TOKEN).json()

    assert body["count"] == 1
    assert body["entries"][0]["kind"] == "review"
    assert body["filters"]["kind"] == "review"


def test_date_bounds_answer_did_i_already_review_today(desk, client):
    _seed(desk, [
        {"kind": "review", "ts": "2026-11-02T19:33:00+08:00", "text": "11-02 复盘。"},
        {"kind": "review", "ts": "2026-11-03T19:35:00+08:00", "text": "11-03 复盘。"},
    ])

    hits = client.get("/journal", params={"kind": "review", "since": "20261103",
                                          "until": "20261103"},
                      headers=TOKEN).json()
    assert hits["count"] == 1
    entry = hits["entries"][0]
    assert "11-03" in (entry.get("text") or entry.get("summary") or "")

    # 断点日：11-04 还没写
    none_yet = client.get("/journal", params={"kind": "review", "since": "20261104",
                                              "until": "20261104"},
                          headers=TOKEN).json()
    assert none_yet["count"] == 0


def test_a_bad_date_is_refused_rather_than_ignored(desk, client):
    """静默丢掉一个解析不了的日期，等于回答了一个别的问题而调用方看不出来。"""
    r = client.get("/journal", params={"since": "11月3日"}, headers=TOKEN)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BAD_DATE"


def test_id_opens_exactly_one_entry_at_full_length(desk, client):
    """读一条长复盘不该把整页都拉成全文。"""
    long_text = "19:30 收盘对账：对 9 / 错 2。" + "差在哪：① 002343 昨收当现价。" * 40
    ids = _seed(desk, [
        {"kind": "review", "ts": "2026-11-03T19:35:00+08:00", "text": long_text},
        {"kind": "note", "ts": "2026-11-03T20:00:00+08:00", "text": "20:00 补记。"},
    ])

    body = client.get("/journal", params={"id": ids[0]}, headers=TOKEN).json()

    assert body["count"] == 1
    assert body["entries"][0]["text"] == long_text
    assert "summary" not in body["entries"][0]


def test_an_id_that_does_not_exist_says_so(desk, client):
    body = client.get("/journal", params={"id": 999999}, headers=TOKEN).json()
    assert body["count"] == 0
    assert body["missing"] == 999999


# --------------------------------------------------------------------- reports


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    root = tmp_path / "reports"
    (root / "review").mkdir(parents=True)
    (root / "close").mkdir()
    (root / "review" / "20261103.md").write_text(
        "# 11-03 复盘\n" + "正文。" * 4000, encoding="utf-8")
    (root / "review" / "20261102.md").write_text("# 11-02 复盘\n短。", encoding="utf-8")
    (root / "close" / "20261103.md").write_text("# 11-03 收盘\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("# 不该被读到\n", encoding="utf-8")
    monkeypatch.setattr(trading_api, "REPORTS_DIR", root)
    return root


def test_reports_are_listable_without_reading_their_bodies(reports_dir, client):
    body = client.get("/reports", headers=TOKEN).json()

    assert body["count"] == 3
    assert set(body["folders"]) == {"close", "review"}
    paths = {row["path"] for row in body["reports"]}
    assert "review/20261103.md" in paths
    # 列表只读首行：标题在，正文不在
    row = next(r for r in body["reports"] if r["path"] == "review/20261103.md")
    assert row["title"] == "# 11-03 复盘"
    assert "正文" not in str(row)


def test_a_report_reads_back_in_pages_instead_of_being_dropped_whole(reports_dir, client):
    """8,258 字的复盘是 14.4 KB；宿主对超过 16 KB 的响应是整包丢弃，不是截断。"""
    first = client.get("/reports/file", params={"path": "review/20261103.md"},
                       headers=TOKEN).json()

    assert first["chars"] > first["returned_chars"]
    assert first["truncated"] is True
    assert first["next_offset"] == first["returned_chars"]
    assert first["text"].startswith("# 11-03 复盘")

    second = client.get("/reports/file", params={"path": "review/20261103.md",
                                                 "offset": first["next_offset"]},
                        headers=TOKEN).json()
    assert second["offset"] == first["next_offset"]
    assert second["text"] != first["text"]

    # 拼起来必须与原文一致；中间不能有洞
    whole = client.get("/reports/file",
                       params={"path": "review/20261102.md"}, headers=TOKEN).json()
    assert whole["truncated"] is False
    assert whole["text"] == "# 11-02 复盘\n短。"


def test_a_path_outside_reports_is_refused(reports_dir, client):
    """name 是模型给的，而这个文件的历史里同类漏洞已经出现过两次。

    检查必须落在**解析后**的路径上：``..`` 和符号链接都能绕过字符串匹配。
    """
    for bad in ("../secret.md", "review/../../secret.md", "/etc/passwd"):
        r = client.get("/reports/file", params={"path": bad}, headers=TOKEN)
        assert r.status_code in (400, 404), bad
        assert "不该被读到" not in r.text


def test_only_markdown_under_reports_is_readable(reports_dir, client):
    (reports_dir / "review" / "notes.txt").write_text("x", encoding="utf-8")
    r = client.get("/reports/file", params={"path": "review/notes.txt"}, headers=TOKEN)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BAD_PATH"


def test_a_missing_report_is_404_not_an_empty_string(reports_dir, client):
    r = client.get("/reports/file", params={"path": "review/20260101.md"}, headers=TOKEN)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NO_REPORT"
