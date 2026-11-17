"""错误提示要能被照着做——照着做要真的有效果。

``StoreBusyError`` 曾经说 "raise ``QUANT_STORE_LOCK_WAIT``"，而全仓没有第二处
提到这个名字：按它做等于没做，操作者下一步只能怀疑这个上限根本不可配。真正
生效的覆盖走 ``settings`` 的 ``QUANT__`` 前缀规则。
"""

from __future__ import annotations

import re
from pathlib import Path

from service import settings
from service.adapters.parquet_store import _lock_busy_message


def test_the_lock_hint_names_a_variable_that_settings_actually_reads(monkeypatch):
    text = _lock_busy_message(Path("20260105.parquet"), 600.0, 600.0)

    # 从提示里把变量名抠出来，而不是在测试里再抄一遍：抄一遍就只是把同一个
    # 错误写进两个文件，改文案时测试照样绿。
    names = re.findall(r"\bQUANT__[A-Z0-9_]+\b", text)
    assert names == ["QUANT__STORAGE__LOCK_WAIT_SECONDS"]
    assert "QUANT_STORE_LOCK_WAIT" not in text

    monkeypatch.setenv(names[0], "123")
    assert settings._env_overrides()["storage.lock_wait_seconds"] == "123"


def test_the_lock_hint_quotes_the_value_in_force():
    """报"等了多久"之外还要报"上限是多少"，否则无法判断该不该调。"""
    text = _lock_busy_message(Path("x.parquet"), 601.0, 600.0)

    assert "601s" in text
    assert "storage.lock_wait_seconds=600" in text
