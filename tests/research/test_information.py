"""Information coverage must be measured, not assumed.

Every test here corresponds to a way this desk has already been wrong:

* a dataset that spanned 2010-2026 while the last 200 sessions were empty;
* a dataset that covered its one day perfectly and reported a false ladder;
* a young stream that was complete and still could not support a prior;
* two sources disagreeing about the same session, both surfaced to the model.

The score is a decision input, so the arithmetic is pinned rather than described.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research import information as info_mod
from service.research.information import Requirement


class FakeRegistry:
    def __init__(self, manifest: dict[str, dict]):
        self._m = manifest

    def list_manifest(self, dataset: str | None = None):
        class Rec:
            pass

        out = []
        for name, spec in self._m.items():
            if dataset is not None and name != dataset:
                continue
            for part, cover in spec["parts"].items():
                rec = Rec()
                rec.dataset = name
                rec.partition = part
                rec.source = "test"
                rec.trust_tier = "T0"
                rec.as_of = cover[1]
                rec.ingested_at = "2026-10-28T00:00:00+08:00"
                rec.rows = spec["rows"]
                rec.snapshot_id = "x"
                rec.checksum = spec.get("checksum", "x")
                rec.schema_hash = "x"
                rec.cover_start = cover[0]
                rec.cover_end = cover[1]
                rec.status = spec.get("status", "ok")
                rec.path = "nope"
                rec.note = ""
                out.append(rec)
        return out

    def summary(self):
        return [{"dataset": name,
                 "rows": spec["rows"],
                 "partitions": len(spec["parts"]),
                 "cover_start": min(c[0] for c in spec["parts"].values()),
                 "cover_end": max(c[1] for c in spec["parts"].values()),
                 "quarantine_partitions": spec.get("quarantine_partitions", 0),
                 "warn_partitions": spec.get("warn_partitions", 0),
                 "error_issues": spec.get("error_issues", 0),
                 "error_codes": spec.get("error_codes", ""),
                 "warn_codes": spec.get("warn_codes", ""),
                 "trust_tier": "T0", "source": "test"}
                for name, spec in self._m.items()]


class FakeStore:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self._f = frames

    def read(self, dataset, start=None, end=None, columns=None, status=None):
        frame = self._f.get(dataset)
        if frame is None:
            return pd.DataFrame()
        frame = frame.copy()
        if "trade_date" in frame.columns:
            frame["trade_date"] = frame["trade_date"].astype(str)
            if start:
                frame = frame[frame["trade_date"] >= start]
            if end:
                frame = frame[frame["trade_date"] <= end]
        if columns:
            keep = [c for c in columns if c in frame.columns]
            frame = frame[keep]
        return frame.reset_index(drop=True)


def sessions(n: int, start: str = "20250102") -> list[str]:
    return [d.strftime("%Y%m%d") for d in pd.bdate_range(start, periods=n)]


def daily_frame(days: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"ts_code": "600000.SH", "trade_date": days})


def test_an_interior_hole_is_not_hidden_by_a_recent_cover_end():
    """444k rows spanning 2010-2026, last 200 sessions empty. Must not read 100%."""
    days = sessions(300)
    present = days[:-200]

    store = FakeStore({"daily": daily_frame(days),
                       "limit_pool_t0": daily_frame(present)})
    registry = FakeRegistry({
        "daily": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "limit_pool_t0": {"rows": len(present),
                          "parts": {"2010": (present[0], present[-1])}},
    })

    state = info_mod._dataset_state(store, registry, "limit_pool_t0", days, days[-1],
                                    max_lag=0, min_history=120)
    assert state["cover_end"] == present[-1], "manifest still ends in the past"
    assert state["recent"] == 50, "only 50 of the last 250 sessions exist"
    assert state["coverage"] < 0.5, "200 missing sessions must show as a hole"
    assert state["score"] < 0.5


def test_a_complete_but_young_stream_is_young_not_missing():
    """Three sessions, all captured: capture is 100%, depth is not."""
    days = sessions(300)
    young = days[-3:]

    store = FakeStore({"daily": daily_frame(days),
                       "ths_anomaly": daily_frame(young)})
    registry = FakeRegistry({
        "daily": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "ths_anomaly": {"rows": 3, "parts": {"20261028": (young[0], young[-1])}},
    })

    state = info_mod._dataset_state(store, registry, "ths_anomaly", days, days[-1],
                                    max_lag=1, min_history=60)
    assert state["coverage"] == 1.0, "everything available was captured"
    assert state["depth_factor"] < 0.1
    assert state["score"] < 0.1, "three days is not a prior"


def test_missing_dataset_scores_zero_and_names_its_blind_spot():
    days = sessions(260)
    store = FakeStore({"daily": daily_frame(days)})
    registry = FakeRegistry({"daily": {"rows": len(days),
                                       "parts": {"2025": (days[0], days[-1])}}})
    req = Requirement(key="events", label="公告", question="有没有事",
                      datasets=("anns",), weight=0.1, blind="不能说没有公告")
    score = info_mod._dataset_state(store, registry, "anns", days, days[-1])
    assert score["present"] is False
    assert score["score"] == 0.0
    assert req.blind


def test_two_sources_disagreeing_about_the_ladder_is_reported_not_averaged():
    day = "20261028"
    ths = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [day],
                        "continue_cnt": [5]})
    pool = pd.DataFrame({"code": ["000001"], "trade_date": [day], "boards": [1]})
    derived = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [day],
                            "pool": ["limit_up"], "boards": [1]})
    store = FakeStore({"ths_limit_up": ths, "limit_up_pool": pool,
                       "limit_pool_t0": derived, "ths_ladder": pd.DataFrame()})

    facts = {f["fact"]: f for f in info_mod._reconcile(store, day)}
    streak = facts.get("最高连板")
    assert streak is not None, "连板高度必须被对账"
    assert streak["agree"] is False
    assert streak["values"]["ths_limit_up"] == 5
    assert streak["values"]["derived_t0"] == 1


def test_agreement_is_reported_as_agreement():
    day = "20261028"
    ths = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [day],
                        "continue_cnt": [5]})
    pool = pd.DataFrame({"code": ["000001"], "trade_date": [day], "boards": [5]})
    derived = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [day],
                            "pool": ["limit_up"], "boards": [5]})
    store = FakeStore({"ths_limit_up": ths, "limit_up_pool": pool,
                       "limit_pool_t0": derived, "ths_ladder": pd.DataFrame()})
    facts = {f["fact"]: f for f in info_mod._reconcile(store, day)}
    assert facts["最高连板"]["agree"] is True


def probe(stock=True, etf=True, cb=True):
    """A quote probe result with the three classes toggled, for tests."""
    order = [("stock", "600519.SH"), ("etf", "510300.SH"),
             ("cb_sh", "111012.SH"), ("cb_sz", "123122.SZ")]
    flags = {"stock": stock, "etf": etf, "cb_sh": cb, "cb_sz": cb}
    classes = {k: {"symbol": c, "got": flags[k], "source": "test",
                   "age_seconds": 1.0, "is_realtime": flags[k],
                   "stale_in_session": False}
               for k, c in order}
    answered = sum(1 for v in flags.values() if v)
    return {"ok": answered == 4, "in_session": False, "classes": classes,
            "answered": answered, "required": 4,
            "missing": [k for k, v in flags.items() if not v],
            "score": round(answered / 4, 3), "note": ""}


def test_score_is_monotone_in_coverage():
    """Filling a hole must raise the total; a score that can't move is decoration."""
    days = sessions(260)

    def build(with_events: bool):
        frames = {"daily": daily_frame(days),
                  "limit_pool_t0": daily_frame(days),
                  "ths_limit_up": pd.DataFrame({"ts_code": "1", "trade_date": days,
                                                "continue_cnt": 1})}
        manifest = {
            "daily": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
            "adj_factor": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
            "stk_limit": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
            "suspend_d": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
            "stock_basic": {"rows": 1, "parts": {"a": ("19900101", days[-1])}},
            "namechange": {"rows": 1, "parts": {"a": ("19900101", days[-1])}},
            "limit_pool_t0": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
            "ths_limit_up": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
        }
        frames["adj_factor"] = daily_frame(days)
        frames["stk_limit"] = daily_frame(days)
        frames["suspend_d"] = daily_frame(days)
        frames["stock_basic"] = pd.DataFrame({"ts_code": ["600000.SH"],
                                              "list_date": ["19900101"],
                                              "delist_date": [None]})
        frames["namechange"] = pd.DataFrame({"ts_code": ["600000.SH"],
                                             "ann_date": [days[-1]]})
        if with_events:
            frames["anns"] = pd.DataFrame({"ts_code": "600000.SH",
                                           "pub_date": days})
            manifest["anns"] = {"rows": len(days),
                                "parts": {"2025": (days[0], days[-1])}}
        return info_mod.scorecard(FakeStore(frames), FakeRegistry(manifest), days[-1],
                                  quotes=probe())

    without = build(False)
    with_events = build(True)
    assert with_events["score"] > without["score"]
    blind = " ".join(without["blind_spots"])
    assert "公告" in blind or "消息" in blind


class FakeQuote:
    def __init__(self, source="ths", age=2.0, realtime=True):
        self._d = {"symbol": "x", "name": "x", "last": "10.00", "source": source,
                   "quote_ts": "2026-10-28T10:00:00+08:00",
                   "age_seconds": age, "is_realtime": realtime}

    def as_dict(self):
        return dict(self._d)


def test_probe_reports_the_missing_asset_class_not_a_blanket_failure():
    """Shanghai convertibles have no quote path. That must be named, not hidden."""
    def reader(code):
        if code.startswith("111012"):
            return None
        return FakeQuote()

    probe = info_mod.probe_quotes(reader=reader, in_session=False)
    assert probe["missing"] == ["cb_sh"]
    assert probe["answered"] == 3
    assert probe["score"] == pytest.approx(3 / 4, abs=0.01)
    assert probe["ok"] is False


def test_probe_does_not_raise_when_a_channel_throws():
    def reader(code):
        if code.startswith("510300"):
            raise RuntimeError("channel down")
        return FakeQuote()

    probe = info_mod.probe_quotes(reader=reader, in_session=False)
    assert probe["answered"] == 3
    assert "etf" in probe["missing"]
    assert "RuntimeError" in probe["classes"]["etf"]["error"]


def test_a_stale_quote_during_the_session_scores_half():
    """An old price labelled live is the failure mode this dimension exists for."""
    probe = info_mod.probe_quotes(reader=lambda code: FakeQuote(age=900.0),
                                  in_session=True)
    assert all(c["stale_in_session"] for c in probe["classes"].values())
    assert probe["score"] < 0.6

    closed = info_mod.probe_quotes(reader=lambda code: FakeQuote(age=900.0),
                                   in_session=False)
    assert closed["score"] == 1.0, "after the close every quote is hours old"


@pytest.mark.parametrize("row", [
    {"last": float("nan"), "quote_ts": "2026-10-28", "age_seconds": 1},
    {"last": 10, "quote_ts": None, "age_seconds": 1},
    {"last": 10, "quote_ts": "2026-10-28", "age_seconds": float("inf")},
    {"last": 10, "quote_ts": "2026-10-28", "age_seconds": -1},
    {"last": 0, "quote_ts": "2026-10-28", "age_seconds": 1},
])
def test_invalid_quotes_do_not_count_as_answers(row):
    out = info_mod.probe_quotes(reader=lambda code: dict(row), in_session=True)
    assert out["answered"] == 0
    assert out["score"] == 0.0
    assert all(not c["got"] for c in out["classes"].values())


def test_after_close_a_stored_close_with_null_age_is_not_a_dead_channel():
    """Regression, 2026-11-09.

    Outside the session the quote tool deliberately sets ``age_seconds=None``:
    the last price IS the day's close, and calling it "4 seconds old" would be a
    lie. ``probe_quotes`` used to demand a finite age unconditionally, which
    marked the THS stock channel dead after every close, dropped the quotes
    dimension to 0.75 and emitted a false "实时报价取不到：股票" blind spot.
    The old "closed" test passed a numeric age (900.0), so it never saw this.
    """
    class ClosingQuote:
        def as_dict(self):
            return {"symbol": "x", "name": "x", "last": "1444.99",
                    "source": "ths", "quote_ts": "2026-11-09T17:01:15+08:00",
                    "age_seconds": None, "is_realtime": False}

    closed = info_mod.probe_quotes(reader=lambda code: ClosingQuote(),
                                   in_session=False)
    assert closed["answered"] == 4
    assert closed["score"] == 1.0
    assert closed["missing"] == []
    assert closed["classes"]["stock"]["got"] is True
    assert closed["classes"]["stock"]["age_basis"] == "stored_close"
    assert closed["classes"]["stock"]["stale_in_session"] is False

    # The same null age DURING the session is a real defect: without an age the
    # desk cannot tell a live price from a stale one.
    live = info_mod.probe_quotes(reader=lambda code: ClosingQuote(),
                                 in_session=True)
    assert live["answered"] == 0
    assert "age_seconds" in live["classes"]["stock"]["error"]


def test_closed_session_blind_spot_names_the_real_limit():
    """After the close the limit is "no realtime price", not "channel down"."""
    probe_ok = {"ok": True, "in_session": False, "classes": {}, "missing": [],
                "score": 1.0, "answered": 4, "required": 4}
    blind: list[str] = []
    req = next(r for r in info_mod.REQUIREMENTS if r.key == "quotes")
    info_mod._quote_row(req, probe_ok, blind)
    assert any("盘后没有实时价" in b for b in blind)
    assert not any("取不到" in b for b in blind)

    probe_down = dict(probe_ok, missing=["stock"], ok=False)
    blind_down: list[str] = []
    info_mod._quote_row(req, probe_down, blind_down)
    assert any("连收盘价都没取到" in b for b in blind_down)
    assert not any("盘中实时报价取不到" in b for b in blind_down)


def test_lunch_blind_spot_is_not_labelled_as_after_close():
    probe = info_mod.probe_quotes(
        reader=lambda code: FakeQuote(age=900.0, realtime=False),
        in_session=False, session_label="午休",
    )
    assert probe["session_label"] == "午休"
    assert probe["score"] == 1.0, "午休停帧不能把通道可达性扣分"

    blind: list[str] = []
    req = next(r for r in info_mod.REQUIREMENTS if r.key == "quotes")
    row = info_mod._quote_row(req, probe, blind)

    assert row["session_label"] == "午休"
    assert any("午休没有连续竞价新报价" in item for item in blind)
    assert not any("盘后没有实时价" in item for item in blind)
    assert any("不能说「现在」" in item for item in blind)


def test_weak_dimensions_are_exposed_without_changing_the_score():
    """A weighted 89 must not hide a 0.061 drill-down. Facts, not a verdict."""
    days = sessions(260)
    frames = {"daily": daily_frame(days),
              "limit_pool_t0": daily_frame(days),
              "ths_limit_up": pd.DataFrame({"ts_code": "1", "trade_date": days,
                                            "continue_cnt": 1})}
    manifest = {
        "daily": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "adj_factor": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "stk_limit": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "suspend_d": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "stock_basic": {"rows": 1, "parts": {"a": ("19900101", days[-1])}},
        "namechange": {"rows": 1, "parts": {"a": ("19900101", days[-1])}},
        "limit_pool_t0": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
        "ths_limit_up": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
    }
    frames["adj_factor"] = daily_frame(days)
    frames["stk_limit"] = daily_frame(days)
    frames["suspend_d"] = daily_frame(days)
    frames["stock_basic"] = pd.DataFrame({"ts_code": ["600000.SH"],
                                          "list_date": ["19900101"],
                                          "delist_date": [None]})
    frames["namechange"] = pd.DataFrame({"ts_code": ["600000.SH"],
                                         "ann_date": [days[-1]]})
    out = info_mod.scorecard(FakeStore(frames), FakeRegistry(manifest), days[-1],
                             quotes=probe())

    assert "weak_dimensions" in out
    assert "score_floor" in out
    dims = {d["key"]: d for d in out["dimensions"]}
    assert out["score_floor"] == min(d["score"] for d in dims.values())
    assert out["score_floor_key"] in out["score_floor_keys"]
    assert all(dims[k]["score"] == out["score_floor"]
               for k in out["score_floor_keys"])
    # Every tied minimum is listed, not just the first one.
    assert set(out["score_floor_keys"]) == {
        k for k, d in dims.items() if d["score"] == out["score_floor"]}
    # Every listed weak dimension really is below the published ceiling, and
    # carries the blind assertion plus the command that closes the hole.
    for weak in out["weak_dimensions"]:
        assert weak["score"] < out["weak_dimensions_ceiling"]
        assert weak["question"]
        assert dims[weak["key"]]["score"] == weak["score"]
    assert any(w["key"] == "intraday_series" for w in out["weak_dimensions"]), \
        "the thin intraday stream must be visible without reading all 12 rows"
    # The headline is untouched by the drill-down: it is still the weighted mean.
    weighted = sum(dims[r.key]["score"] * r.weight for r in info_mod.REQUIREMENTS)
    assert out["score"] == round(100 * weighted / info_mod.TOTAL_WEIGHT, 1)


def test_master_freshness_uses_ingest_manifest_not_old_list_dates():
    days = sessions(260)
    day = "20261028"
    store = FakeStore({"stock_basic": pd.DataFrame({
        "ts_code": ["600000.SH"], "list_date": ["19900101"], "delist_date": [None]})})
    registry = FakeRegistry({"stock_basic": {
        "rows": 1, "parts": {"master": ("19900101", "19900101")}}})
    out = info_mod._dataset_state(store, registry, "stock_basic", days, day)
    assert out["basis"] == "manifest"
    assert out["lag"] == 0
    assert out["score"] == 1.0


def test_information_cache_digest_includes_manifest_checksum():
    registry = FakeRegistry({"daily": {
        "rows": 1, "parts": {"2026": ("20261028", "20261028")}, "checksum": "a"}})
    before = info_mod._digest(registry)
    registry._m["daily"]["checksum"] = "b"
    assert info_mod._digest(registry) != before


def test_information_cache_digest_includes_the_scoring_implementation():
    assert info_mod._schema_digest(b"implementation-v1") != info_mod._schema_digest(
        b"implementation-v2")


class _SlaStore:
    """Only the two reads the semantic SLA needs."""

    def __init__(self, fina, basic):
        self.fina, self.basic = fina, basic

    def read(self, name):
        if name == "fina_indicator":
            return self.fina
        if name == "stock_basic":
            return self.basic
        raise KeyError(name)


def _fina_frame(periods, codes, ann_dates):
    return pd.DataFrame({"ts_code": codes, "end_date": periods,
                         "ann_date": ann_dates})


def test_fundamental_sla_measures_ingest_coverage_not_calendar_lag():
    fina = _fina_frame(
        ["20260630", "20260630", "20260930", "20260930"],
        ["A.SZ", "B.SZ", "A.SZ", "B.SZ"],
        ["20260822", "20260822", "20261031", "20261031"])
    basic = pd.DataFrame({"ts_code": [f"{c}.SZ" for c in "ABCD"],
                          "list_status": ["L"] * 4})
    out = info_mod._fundamental_sla(_SlaStore(fina, basic), "20261110")
    assert out["available"] is True
    assert out["latest_period"] == "20260930"
    assert out["latest_announced_on"] == "20261031"
    assert out["days_since_announcement"] == 10
    assert out["universe"] == 4
    assert out["symbols_with_latest_period"] == 2
    assert out["coverage"] == 0.5
    assert out["sla_breached"] is False  # exactly at the configured floor


def test_fundamental_sla_breaches_when_only_a_few_names_have_the_new_period():
    fina = _fina_frame(
        ["20260630"] * 9 + ["20260930"],
        [f"{c}.SZ" for c in "ABCDEFGHI"] + ["A.SZ"],
        ["20260822"] * 9 + ["20261031"])
    basic = pd.DataFrame({"ts_code": [f"{c}.SZ" for c in "ABCDEFGHIJ"],
                          "list_status": ["L"] * 10})
    out = info_mod._fundamental_sla(_SlaStore(fina, basic), "20261110")
    assert out["coverage"] == 0.1
    assert out["sla_breached"] is True
    assert "quarterly_fundamentals.py" in out["note"]


def test_fundamental_sla_degrades_instead_of_breaking_during_a_rewrite():
    class Broken:
        def read(self, name):
            raise OSError("partition being rewritten")

    out = info_mod._fundamental_sla(Broken(), "20261110")
    assert out["available"] is False
    assert "OSError" in out["reason"]


def test_scorecard_exposes_the_sla_without_changing_the_score(monkeypatch):
    base = {"ok": True, "as_of": "20261110", "sessions": [], "rows": [],
            "blind": [], "weighted": 0.0, "conflicts": [],
            "declared_not_held": []}
    quote_row = {"key": "quotes", "score": 1.0, "status": "ok"}
    monkeypatch.setattr(info_mod, "_coverage", lambda store, registry, day: base)
    monkeypatch.setattr(info_mod, "_quote_row", lambda *a: quote_row)
    monkeypatch.setattr(info_mod, "_fundamental_sla",
                        lambda store, day: {"available": True, "sla_breached": True})
    out = info_mod.scorecard(object(), object(), "20261110", quotes={})
    assert out["fundamental_sla"]["sla_breached"] is True
    assert out["score"] > 0  # the SLA is observational, it changes no arithmetic


def test_text_coverage_counts_cached_filings(tmp_path):
    class Store:
        root = tmp_path

        def read(self, name):
            return pd.DataFrame({
                "pub_date": ["20261109"] * 3,
                "announcement_id": ["1225610434", "1225610435", "1225610436"],
            })

    cache = tmp_path / "cache" / "anns_text"
    cache.mkdir(parents=True)
    (cache / "1225610434.json").write_text("{}", encoding="utf-8")
    out = info_mod._text_coverage(Store(), "20261110")
    assert out["available"] is True
    assert out["announcements"] == 3
    assert out["with_text"] == 1
    assert out["coverage"] == 0.3333


def test_error_issues_are_excluded_from_quality_adjusted_coverage():
    """Full but demonstrably wrong coverage is not verified information."""
    days = sessions(260)
    frames = {"daily": daily_frame(days), "adj_factor": daily_frame(days)}
    manifest = {
        "daily": {"rows": len(days), "parts": {"2025": (days[0], days[-1])}},
        "adj_factor": {
            "rows": len(days), "parts": {"2025": (days[0], days[-1])},
            "error_issues": 2, "error_codes": "adj_factor_decrease",
        },
    }
    out = info_mod.scorecard(FakeStore(frames), FakeRegistry(manifest), days[-1],
                             quotes=probe())
    price = next(row for row in out["dimensions"] if row["key"] == "price_volume")

    assert price["score"] == 1.0, "the legacy coverage score stays comparable"
    assert price["quality_score"] == 0.0
    assert out["quality_adjusted_score"] < out["score"]
    assert out["quality"]["has_errors"] is True
    issue = next(row for row in out["quality"]["datasets"]
                 if row["dataset"] == "adj_factor")
    assert issue["error_issues"] == 2
    assert issue["error_codes"] == ["adj_factor_decrease"]


def test_information_cache_digest_includes_quality_issue_changes():
    registry = FakeRegistry({"adj_factor": {
        "rows": 1, "parts": {"2026": ("20261028", "20261028")}}})
    before = info_mod._digest(registry)
    registry._m["adj_factor"]["error_issues"] = 1
    registry._m["adj_factor"]["error_codes"] = "adj_factor_decrease"
    assert info_mod._digest(registry) != before


def test_intraday_split_uses_real_archive_start_without_changing_weight(monkeypatch):
    days = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-11-02", periods=10)]
    req = Requirement(key="intraday_series", label="盘中序列", question="q",
                      datasets=("rt_quote", "stk_mins"), weight=0.06,
                      min_history=120)
    monkeypatch.setattr(info_mod, "REQUIREMENTS", [req])
    monkeypatch.setattr(info_mod, "_reconcile", lambda *_args: [])
    store = FakeStore({
        "daily": daily_frame(days),
        "rt_quote": daily_frame(days),
        "stk_mins": daily_frame(days),
    })
    registry = FakeRegistry({
        "daily": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
        "rt_quote": {"rows": len(days), "parts": {"2026": ("20261023", days[-1])}},
        "stk_mins": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
    })

    out = info_mod._coverage(store, registry, days[-1])
    row = out["rows"][0]
    components = {part["key"]: part for part in row["components"]}

    assert row["weight"] == 0.06 and row["score"] == round(5 / 120, 3)
    assert components["minute_history"]["recoverable"] is True
    assert components["minute_history"]["depth"] == 10
    assert components["tape_replay"]["recoverable"] is False
    assert components["tape_replay"]["depth"] == 5
    assert components["tape_replay"]["since"] == "20261107"
    assert "--days 120" in components["minute_history"]["fix"]


def test_minute_history_requires_symbol_breadth_not_only_partition_presence():
    """A two-symbol canary in every partition must not score as full-market history."""
    days = sessions(120)
    daily = pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"] * len(days),
        "trade_date": [day for day in days for _ in range(2)],
    })
    minutes = pd.DataFrame({"ts_code": ["000001.SZ"] * len(days),
                            "trade_date": days})
    store = FakeStore({"daily": daily, "stk_mins": minutes})
    registry = FakeRegistry({
        "daily": {"rows": len(daily), "parts": {"2025": (days[0], days[-1])}},
        "stk_mins": {"rows": len(minutes), "parts": {"2025": (days[0], days[-1])}},
    })

    state = info_mod._dataset_state(store, registry, "stk_mins", days, days[-1],
                                    min_history=120)

    assert state["recent"] == 120 and state["depth_factor"] == 1.0
    assert state["universe_coverage"] == 0.5
    assert state["universe_min"] == 0.5
    assert state["score"] == 0.5, "the old partition-only implementation returned 1.0"


def test_auction_components_expose_backfillable_and_today_only_sources(monkeypatch):
    days = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-05-21", periods=120)]
    auction_days, anomaly_days = days[-60:], days[-15:]
    req = Requirement(key="auction", label="竞价与异动", question="q",
                      datasets=("ths_auction", "ths_anomaly"), weight=0.04,
                      min_history=120)
    monkeypatch.setattr(info_mod, "REQUIREMENTS", [req])
    monkeypatch.setattr(info_mod, "_reconcile", lambda *_args: [])
    store = FakeStore({
        "daily": daily_frame(days),
        "ths_auction": daily_frame(auction_days),
        "ths_anomaly": daily_frame(anomaly_days),
    })
    registry = FakeRegistry({
        "daily": {"rows": len(days), "parts": {"2026": (days[0], days[-1])}},
        "ths_auction": {"rows": len(auction_days),
                        "parts": {"2026": (auction_days[0], auction_days[-1])}},
        "ths_anomaly": {"rows": len(anomaly_days),
                        "parts": {"2026": (anomaly_days[0], anomaly_days[-1])}},
    })

    row = info_mod._coverage(store, registry, days[-1])["rows"][0]
    components = {part["dataset"]: part for part in row["components"]}

    assert row["weight"] == 0.04 and row["score"] == 0.125
    assert components["ths_auction"]["recoverable"] is True
    assert components["ths_auction"]["depth"] == 60
    assert components["ths_anomaly"]["recoverable"] is False
    assert components["ths_anomaly"]["depth"] == 15
    assert "TODAY_ONLY" in components["ths_anomaly"]["fix"]
