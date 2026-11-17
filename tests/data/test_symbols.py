"""Exchange prefix is a property of the security, not of the adapter.

The bug this pins: three adapters each decided SH/SZ on their own, and none of
them had a branch for convertibles. An SSE convertible went out as ``sz111012``
and ``0.111012``, both nonexistent, the vendor answered nothing, and the desk
concluded the data did not exist. Nothing raised. The only symptom was a missing
row — which is exactly what "this instrument has no quote" also looks like.
"""

from __future__ import annotations

import pytest

from service.adapters import symbols


@pytest.mark.parametrize("code,expected", [
    ("600519", "SH"), ("688981", "SH"), ("510300", "SH"), ("511010", "SH"),
    ("110076", "SH"), ("111012", "SH"), ("113050", "SH"), ("118075", "SH"),
    ("132018", "SH"), ("145001", "SH"), ("900901", "SH"),
    ("000001", "SZ"), ("300750", "SZ"), ("159915", "SZ"),
    ("123122", "SZ"), ("127045", "SZ"), ("128145", "SZ"),
    ("430047", "BJ"), ("920001", "BJ"),
])
def test_exchange_of(code, expected):
    assert symbols.exchange_of(code) == expected


def test_bare_special_prefix_uses_static_fast_path(monkeypatch):
    monkeypatch.setattr(
        symbols, "clean_code",
        lambda _code: pytest.fail("标准六位代码不应重复完整归一化"),
    )
    assert symbols.exchange_of("111012") == "SH"
    assert symbols.exchange_of("920001") == "BJ"


def test_suffixed_and_bare_codes_agree():
    assert symbols.ts_code("600519.SH") == "600519.SH"
    assert symbols.ts_code("600519") == "600519.SH"
    assert symbols.ts_code("000001.SZ") == "000001.SZ"


def test_convertibles_reach_the_right_vendor_prefix():
    """The exact call the vendor saw before the fix was ``sz111012``."""
    assert symbols.tencent_symbol("111012.SH") == "sh111012"
    assert symbols.tencent_symbol("123122.SZ") == "sz123122"
    assert symbols.eastmoney_market("111012.SH") == 1
    assert symbols.eastmoney_market("123122.SZ") == 0


def test_beijing_is_not_shanghai_despite_the_leading_nine():
    """900xxx is a Shanghai B share; 920xxx is Beijing. Both start with 9."""
    assert symbols.exchange_of("900901") == "SH"
    assert symbols.exchange_of("920001") == "BJ"


def test_every_prefix_helper_agrees_with_exchange_of():
    samples = ["600519", "688981", "510300", "110076", "111012", "118075",
               "132018", "000001", "300750", "159915", "123122", "127045",
               "430047", "920001"]
    for code in samples:
        want = symbols.exchange_of(code)
        assert symbols.ts_code(code).endswith("." + want)
        assert symbols.tencent_symbol(code)[:2] == want.lower()
        assert symbols.eastmoney_market(code) == (1 if want == "SH" else 0)


@pytest.mark.parametrize("code", [
    "100001.SH", "110076.SH", "111012.SH", "113050.SH", "118075.SH",
    "132018.SH", "145001.SH", "123122.SZ", "127045.SZ", "128145.SZ", "120001.SZ",
])
def test_convertibles_are_identified_as_convertibles(code):
    assert symbols.is_convertible(code)


@pytest.mark.parametrize("code", [
    "600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "510300.SH",
    "159915.SZ", "511010.SH", "430047.BJ", "920001.BJ", "900901.SH",
])
def test_other_instruments_are_not_convertibles(code):
    assert not symbols.is_convertible(code)
