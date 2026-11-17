"""Both vendors publish pools; either entry point must be able to name the other.

2026-10-30: 东财 said 47 limit-ups / 82 limit-downs, 同花顺 said 49 / 86. A run that
read only `quant_pools` quoted 47 against a 49 close and had no way to know the
gap was a definition difference rather than a change in the tape. `delta` is
always eastmoney minus 同花顺 so the sign reads the same from both sides.
"""

from __future__ import annotations

from types import SimpleNamespace

from service.trading.pool_cross import compare


class _Frame:
    def __init__(self, n: int) -> None:
        self._n = n
        self.empty = n == 0

    def __len__(self) -> int:
        return self._n


def desk_with(sizes: dict[str, int]) -> SimpleNamespace:
    return SimpleNamespace(data=SimpleNamespace(
        read=lambda name, start=None, end=None: _Frame(sizes.get(name, 0))))


EM_SIZES = {"limit_up_pool": 47, "broken_board_pool": 31, "limit_down_pool": 82}
THS_SIZES = {"ths_limit_up": 49, "ths_limit_break": 31, "ths_limit_down": 86}


def test_from_the_eastmoney_side_reads_ths_and_keeps_the_sign():
    desk = desk_with(THS_SIZES)
    out = compare(desk, "20261030",
                  {"limit_up": 47, "broken": 31, "limit_down": 82},
                  source="eastmoney")
    assert out["ok"] is True
    assert out["primary"] == "eastmoney"
    assert out["ths"] == {"limit_up": 49, "broken": 31, "limit_down": 86}
    assert out["eastmoney"] == {"limit_up": 47, "broken": 31, "limit_down": 82}
    assert out["delta_eastmoney_minus_ths"] == {
        "limit_up": -2, "broken": 0, "limit_down": -4}
    assert "同一来源" in out["note"]


def test_from_the_ths_side_reads_eastmoney_with_the_same_sign():
    desk = desk_with(EM_SIZES)
    out = compare(desk, "20261030",
                  {"limit_up": 49, "broken": 31, "limit_down": 86},
                  source="ths")
    assert out["ok"] is True
    assert out["eastmoney"] == {"limit_up": 47, "broken": 31, "limit_down": 82}
    assert out["delta_eastmoney_minus_ths"]["limit_up"] == -2


def test_the_opening_window_is_not_compared():
    """The peer's pools are not ingested yet; a gap here would be a clock, not a definition."""
    desk = desk_with(THS_SIZES)
    out = compare(desk, "20261030", {"limit_up": 0, "broken": 0, "limit_down": 1},
                  source="eastmoney", phase="opening_window")
    assert out["ok"] is False
    assert out["skipped"] == "opening_window"
    assert "抓取时点差" in out["note"]


def test_an_unfetched_peer_is_reported_not_zeroed():
    desk = desk_with({})
    out = compare(desk, "20261030", {"limit_up": 47, "broken": 31, "limit_down": 82},
                  source="eastmoney")
    assert out["ok"] is False
    assert out["skipped"] == "not_ingested"
    assert "唯一真相" in out["note"]


def test_a_failing_read_does_not_raise():
    class Boom:
        def read(self, *a, **k):
            raise KeyError("no such dataset")

    out = compare(SimpleNamespace(data=Boom()), "20261030",
                  {"limit_up": 47, "broken": 31, "limit_down": 82},
                  source="eastmoney")
    assert out["ok"] is False
    assert out["skipped"] == "unreadable"


def test_an_unknown_source_is_refused():
    desk = desk_with(EM_SIZES)
    out = compare(desk, "20261030", {}, source="bloomberg")
    assert out["ok"] is False
    assert "unknown source" in out["note"]
