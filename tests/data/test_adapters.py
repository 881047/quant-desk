"""Adapter behaviour: quota classification, retry, and vendor payload parsing."""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

import service.adapters.eastmoney_adapter as em_mod
from service.adapters import catalog
from service.adapters.eastmoney_adapter import EastMoneyAdapter, EastMoneyError, _hhmmss
from service.adapters.tencent_adapter import TencentQuoteAdapter
from service.adapters.tushare_adapter import (
    RateLimiter,
    TushareAdapter,
    TushareError,
    TusharePermissionError,
)


class _FakeApi:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def daily(self, **kwargs):
        self.calls += 1
        return self.behaviour(self.calls, kwargs)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_permission_error_is_not_retried():
    def boom(_n, _kw):
        raise RuntimeError("抱歉，您没有访问该接口的权限，权限的具体详情访问：https://tushare.pro")

    api = _FakeApi(boom)
    adapter = TushareAdapter(client=api, per_minute=1000, sleep=lambda _s: None)
    with pytest.raises(TusharePermissionError):
        adapter.call("daily", trade_date="20240102")
    assert api.calls == 1


def test_minute_quota_is_retried_then_succeeds():
    def quota_once(n, _kw):
        if n == 1:
            raise RuntimeError("抱歉，您每分钟最多访问该接口200次")
        return pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"]})

    api = _FakeApi(quota_once)
    slept: list[float] = []
    adapter = TushareAdapter(client=api, per_minute=1000, retries=2, sleep=slept.append)
    df = adapter.call("daily", trade_date="20240102")

    assert len(df) == 1
    assert api.calls == 2
    assert slept and slept[0] >= 60


def test_rate_limiter_waits_when_budget_is_spent():
    clock = _Clock()
    limiter = RateLimiter(per_minute=2, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # must wait ~60s
    assert clock.now >= 60


@pytest.mark.parametrize("kwargs", [
    {"retries": True}, {"retries": False}, {"retries": -1},
    {"per_minute": True}, {"per_minute": 0},
])
def test_tushare_rejects_bool_and_invalid_integer_limits(kwargs):
    with pytest.raises((TypeError, ValueError)):
        TushareAdapter(client=_FakeApi(lambda _n, _kw: pd.DataFrame()), **kwargs)


def test_tushare_zero_retries_is_not_replaced_by_the_default():
    api = _FakeApi(lambda _n, _kw: pd.DataFrame({"ok": [1]}))
    adapter = TushareAdapter(client=api, retries=0, sleep=lambda _s: None)
    assert adapter.call("daily").iloc[0]["ok"] == 1
    assert adapter.retries == 0
    assert api.calls == 1


def test_tushare_full_last_page_at_page_cap_is_explicitly_incomplete():
    api = _FakeApi(lambda _n, _kw: pd.DataFrame({"row": range(5)}))
    adapter = TushareAdapter(client=api, retries=0, sleep=lambda _s: None)

    with pytest.raises(TushareError, match="incomplete after 2 pages"):
        adapter.call_paged("daily", page_size=5, max_pages=2)
    assert api.calls == 2


def test_tushare_empty_page_after_a_full_page_proves_completion():
    def last_page_empty(n, _kw):
        return pd.DataFrame({"row": range(5)}) if n == 1 else pd.DataFrame()

    api = _FakeApi(last_page_empty)
    adapter = TushareAdapter(client=api, retries=0, sleep=lambda _s: None)
    out = adapter.call_paged("daily", page_size=5, max_pages=2)
    assert len(out) == 5
    assert api.calls == 2


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.last_url = ""
        self.last_params: dict = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.last_url = url
        self.last_params = params or {}
        return _Resp(self.payload)


def test_eastmoney_minutes_use_the_same_unadjusted_basis_as_tencent():
    payload = {"data": {"klines": ["2026-10-28 09:35,10,10.1,10.2,9.9,100,1000,0.1"]}}
    client = _FakeHttp(payload)
    adapter = EastMoneyAdapter(client=client, min_interval=0)

    out = adapter.fetch_minutes("600519.SH", klt=15)
    assert len(out) == 1
    assert client.last_params["fqt"] == "0"
    assert client.last_params["klt"] == "15"


def test_pool_payload_is_normalised():
    payload = {
        "rc": 0,
        "data": {
            "pool": [
                {
                    "c": "600000", "m": 1, "n": "浦发银行", "p": 7250, "zdp": 10.02,
                    "amount": 1.2e8, "ltsz": 1e10, "tshare": 2e10, "hs": 3.1,
                    "lbc": 2, "fbt": 92500, "lbt": 145959, "fund": 8.8e7,
                    "zbc": 1, "hybk": "银行", "zttj": {"days": 3, "ct": 2},
                }
            ]
        },
    }
    adapter = EastMoneyAdapter(client=_FakeHttp(payload))
    df = adapter.fetch_pool("limit_up_pool", "20240102")

    assert len(df) == 1
    row = df.iloc[0]
    assert row["trade_date"] == "20240102"
    assert row["code"] == "600000"
    assert float(row["close"]) == pytest.approx(7.25)
    assert row["first_seal_time"] == "09:25:00"
    assert row["last_seal_time"] == "14:59:59"
    assert int(row["boards"]) == 2
    assert int(row["stat_days"]) == 3
    assert row["source"] == "eastmoney"


def test_empty_pool_returns_typed_empty_frame():
    adapter = EastMoneyAdapter(client=_FakeHttp({"rc": 0, "data": None}))
    df = adapter.fetch_pool("limit_up_pool", "20240102")
    assert df.empty
    assert "code" in df.columns


def test_quotes_payload_is_numeric():
    payload = {
        "data": {
            "diff": [
                {
                    "f12": "600000", "f13": 1, "f14": "浦发银行", "f2": 7.25, "f3": 1.2,
                    "f4": 0.09, "f5": 123456, "f6": 8.9e7, "f7": 2.1, "f8": 0.8,
                    "f10": 1.1, "f15": 7.3, "f16": 7.1, "f17": 7.15, "f18": 7.16,
                    "f20": 2e11, "f21": 1.9e11, "f124": 1700000000,
                }
            ]
        }
    }
    adapter = EastMoneyAdapter(client=_FakeHttp(payload))
    df = adapter.fetch_quotes(["600000.SH"])

    assert len(df) == 1
    assert df.iloc[0]["code"] == "600000"
    assert df.iloc[0]["last"] == pytest.approx(7.25)
    assert "snapshot_ts" in df.columns
    assert adapter._client.last_params["secids"] == "1.600000"


class _TextResp:
    def __init__(self, text: str):
        self.content = text.encode("gbk")

    def raise_for_status(self):
        return None


class _FakeTextClient:
    def __init__(self, text: str):
        self.text = text
        self.last_url = ""

    def get(self, url, headers=None, timeout=None):
        self.last_url = url
        return _TextResp(self.text)


def _tencent_line(code="600000", name="浦发银行", last=9.53, pre=9.62,
                  stamp="20261023150000"):
    parts = ["0"] * 50
    parts[0], parts[1], parts[2] = "1", name, code
    parts[3], parts[4], parts[5] = str(last), str(pre), str(pre)
    parts[6] = "223344"
    parts[30] = stamp
    parts[31], parts[32] = "-0.09", "-0.94"
    parts[33], parts[34] = str(last + 0.12), str(last - 0.03)
    parts[37], parts[38] = "21234", "0.77"
    parts[43], parts[44], parts[45], parts[46] = "1.56", "2800", "2800", "0.55"
    return f'v_sh{code}="' + "~".join(parts) + '";'


def test_tencent_fallback_parses_gbk_payload():
    client = _FakeTextClient(_tencent_line())
    adapter = TencentQuoteAdapter(client=client)
    df = adapter.fetch_quotes(["600000.SH"])

    assert len(df) == 1
    row = df.iloc[0]
    assert row["code"] == "600000"
    assert row["name"] == "浦发银行"
    assert row["last"] == pytest.approx(9.53)
    assert row["pct_chg"] == pytest.approx(-0.94)
    assert row["amount"] == pytest.approx(21234 * 10_000)
    assert row["trade_date"] == "20261023"
    assert row["quote_ts"] > 0
    assert client.last_url.endswith("sh600000")


class _SequenceHttp:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(status, payload=None, headers=None):
    return httpx.Response(
        status, json=payload, headers=headers,
        request=httpx.Request("GET", "https://fake.invalid"),
    )


def test_eastmoney_retries_5xx_honours_retry_after_and_closes(monkeypatch):
    responses = [
        _response(503, headers={"Retry-After": "0.125"}),
        _response(200, {"data": {"pool": []}}),
    ]
    client = _SequenceHttp(responses)
    slept = []
    monkeypatch.setattr(em_mod.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(em_mod.time, "sleep", slept.append)
    adapter = EastMoneyAdapter(client=client, retries=1, min_interval=0, max_seconds=5)

    assert adapter._get("https://fake.invalid", {}) == {"data": {"pool": []}}
    assert client.calls == 2
    assert slept == [0.125], "Retry-After 的单位是秒，不能再乘 1000"
    assert all(response.is_closed for response in responses)


def test_eastmoney_does_not_retry_a_client_error(monkeypatch):
    response = _response(400)
    client = _SequenceHttp([response])
    monkeypatch.setattr(em_mod.time, "sleep", lambda _s: pytest.fail("4xx 不应退避"))
    adapter = EastMoneyAdapter(client=client, retries=3, min_interval=0)

    with pytest.raises(EastMoneyError):
        adapter._get("https://fake.invalid", {})
    assert client.calls == 1
    assert response.is_closed


def test_retry_budget_can_prevent_any_retry(monkeypatch):
    response = _response(503, headers={"Retry-After": "1000"})
    client = _SequenceHttp([response])
    monkeypatch.setattr(em_mod.time, "sleep", lambda _s: pytest.fail("预算为 0 不应睡眠"))
    adapter = EastMoneyAdapter(client=client, retries=3, min_interval=0, max_seconds=0)

    with pytest.raises(EastMoneyError):
        adapter._get("https://fake.invalid", {})
    assert client.calls == 1
    assert response.is_closed


def test_tencent_retries_network_failure_and_closes_response(monkeypatch):
    request = httpx.Request("GET", "https://fake.invalid")
    response = _response(200, {"ok": True})
    client = _SequenceHttp([httpx.ConnectError("offline", request=request), response])
    slept = []
    monkeypatch.setattr(em_mod.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(em_mod.time, "sleep", slept.append)
    adapter = TencentQuoteAdapter(client=client, retries=1)

    assert adapter.get_json("https://fake.invalid") == {"ok": True}
    assert client.calls == 2
    assert slept == [0.25]
    assert response.is_closed


def test_default_tencent_session_disables_env_proxy_and_keeps_timeout(monkeypatch):
    response = _response(200, {"ok": True})
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, _url, **_kwargs):
            return response

    monkeypatch.setattr("service.adapters.tencent_adapter.httpx.Client", Client)
    adapter = TencentQuoteAdapter(timeout=3.5)
    assert adapter.get_json("https://fake.invalid") == {"ok": True}
    assert seen == {"trust_env": False, "timeout": 3.5}
    assert response.is_closed


def test_injected_sessions_cannot_reenable_ambient_proxy():
    class Unsafe:
        trust_env = True

    with pytest.raises(ValueError, match="trust_env=False"):
        EastMoneyAdapter(client=Unsafe())
    with pytest.raises(ValueError, match="trust_env=False"):
        TencentQuoteAdapter(client=Unsafe())
    with pytest.raises(ValueError):
        TencentQuoteAdapter(timeout=float("inf"))


@pytest.mark.parametrize("value", ["93000", 93000, "09:30", "09:30:00"])
def test_hhmmss_is_always_colon_separated(value):
    assert _hhmmss(value) == "09:30:00"


@pytest.mark.parametrize("value", ["25:00:00", "246060", "not-a-time", True])
def test_invalid_hhmmss_is_discarded(value):
    assert _hhmmss(value) is None


def test_invalid_tencent_quote_date_is_discarded():
    line = _tencent_line(stamp="20260230150000")
    assert TencentQuoteAdapter._parse_line(line) is None


def test_catalog_resolution_iterates_a_snapshot_not_the_global_dict(monkeypatch):
    class GuardedSpecs(dict):
        def __iter__(self):
            raise AssertionError("全局 SPECS 被直接迭代")

    guarded = GuardedSpecs(catalog.SPECS)
    monkeypatch.setattr(catalog, "SPECS", guarded)
    assert catalog.resolve(["all"]) == list(guarded.keys())
    assert guarded == catalog.SPECS
