"""熔断表必须有界：它的 key 由调用方写在 URL 里，而 URL 是免费的。

实测（2026-11-05，旧代码）：500 个不同的纯字母路径留下 **1000** 个 bucket——
匹配与未匹配的 ``!`` 孪生各一个；一天后全部过期，**一个都没删**。

三个洞叠在一起：

1. ``defaultdict`` 的 ``[key]`` 是**写**，不是读。``_breaker_tripped`` 对每个
   请求都跑一次 ``key`` 和 ``unrouted_key``，于是 91 条路由在任何失败发生之前
   就留下 182 个空 bucket。
2. 窗口清理只 ``popleft`` 时间戳，不清空 bucket。而空闲 bucket 永远不会被问，
   懒清理够不着它。
3. ``API_BREAKER=0`` 把 ``_breaker_tripped`` 整段跳过，``_record_failure`` 只增
   不减——"临时关掉熔断"是这张表唯一一种没有上限的运行方式。
"""

from __future__ import annotations

import pytest

from service.api import app as api


def _letters(n: int) -> str:
    """A path segment with no digits and no dash: ``_breaker_key`` keeps it.

    That is the point — an id-like segment collapses to ``*``, but a plain word
    becomes its own bucket, and words are free to invent.
    """
    out = []
    while True:
        out.append(chr(ord("a") + n % 26))
        n //= 26
        if not n:
            return "".join(out)


@pytest.fixture(autouse=True)
def clean_table():
    api._recent_failures.clear()
    if hasattr(api, "_breaker_sweep_at"):
        api._breaker_sweep_at = 0.0
    yield
    api._recent_failures.clear()


def test_a_successful_request_leaves_no_trace():
    """读不建 key：否则每条路由白占两个 bucket，成功与否都一样。"""
    api._breaker_tripped(api._breaker_key("GET", "/portfolio"), 1000.0)
    api._breaker_tripped(api._breaker_key("GET", "!/portfolio"), 1000.0)

    assert api._recent_failures == {}


def test_unknown_paths_cannot_grow_the_table_without_bound():
    """404 不需要 token，所以这张表的增长速率由陌生人决定。"""
    cap = getattr(api, "_BREAKER_MAX_KEYS", None)
    assert cap is not None, "熔断表没有 key 上限：路径由调用方随便编，表会一直长"

    for i in range(cap * 4):
        path = f"/probe{_letters(i)}"
        api._record_failure(api._breaker_key("GET", path), 1000.0)
        api._record_failure(api._breaker_key("GET", "!" + path), 1000.0)

    assert len(api._recent_failures) <= cap


def test_a_cold_key_is_dropped_even_though_nobody_asks_about_it_again():
    """懒清理只在有人再问这个 key 时跑，而没人问的 key 永远不会自己消失。"""
    api._record_failure("GET /ghost", 1000.0)
    assert "GET /ghost" in api._recent_failures

    # 一天后的另一次失败。它问的是 /fresh，但顺带该把 /ghost 扫出去。
    api._record_failure("GET /fresh", 1000.0 + 86400)

    assert "GET /ghost" not in api._recent_failures


def test_recording_failures_without_ever_checking_stays_bounded():
    """``API_BREAKER=0`` 时没有任何读路径，记录自己必须收口。"""
    cap = getattr(api, "_BREAKER_MAX_KEYS", None)
    assert cap is not None

    for i in range(cap * 4):
        api._record_failure(f"GET /fire{_letters(i)}", 1000.0)

    assert len(api._recent_failures) <= cap


def test_one_bucket_cannot_park_thousands_of_timestamps():
    """超过 ``_BREAKER_LIMIT`` 条只说明"早就该 trip"，再多一条信息都没有。"""
    for i in range(2000):
        api._record_failure("GET /same", 1000.0 + i * 0.001)

    assert len(api._recent_failures["GET /same"]) <= api._BREAKER_LIMIT


def test_a_full_table_never_evicts_a_breaker_that_is_working():
    """满了就丢新 key，不淘汰旧 bucket。

    淘汰"最旧"看起来更聪明，实际正好删错：真实端点一 trip，后续请求在中间件
    就被 429 挡掉，永远走不到 ``_record_failure``，它的时间戳停在 trip 那一刻；
    洪泛者的 bucket 反而更新。于是"淘汰最旧"给了洪泛者解除真实熔断的能力。
    """
    cap = getattr(api, "_BREAKER_MAX_KEYS", None)
    assert cap is not None

    now = 1000.0
    for _ in range(api._BREAKER_LIMIT):
        api._record_failure("POST /decision/propose", now)
    assert api._breaker_tripped("POST /decision/propose", now) is True

    flood = now + 1.0
    for i in range(cap * 4):
        api._record_failure(f"GET /junk{_letters(i)}", flood)

    assert api._breaker_tripped("POST /decision/propose", now + 2.0) is True


def test_a_bucket_that_reaches_the_limit_still_trips():
    """夹断 bucket 不能改变判断：够不够 limit 次，答案还是够。"""
    now = 1000.0
    for _ in range(api._BREAKER_LIMIT):
        api._record_failure("GET /busy", now)

    assert api._breaker_tripped("GET /busy", now) is True


def test_entries_older_than_the_window_stop_tripping():
    now = 1000.0
    for _ in range(api._BREAKER_LIMIT):
        api._record_failure("GET /old", now)

    assert api._breaker_tripped("GET /old", now + api._BREAKER_WINDOW + 1) is False
    assert "GET /old" not in api._recent_failures, "过期的 bucket 应该当场删掉"


def test_a_table_below_the_cap_keeps_every_bucket():
    """上限逻辑不能顺手清掉还没过期的记录。"""
    now = 1000.0
    api._record_failure("GET /a", now)
    api._record_failure("GET /b", now)
    api._record_failure("GET /c", now)

    assert set(api._recent_failures) == {"GET /a", "GET /b", "GET /c"}
    assert all(len(queue) == 1 for queue in api._recent_failures.values())


def test_a_successful_read_also_sweeps():
    """清扫不能只挂在失败上。

    实测过：灌满 512 个 bucket 之后停手，70 秒后 ``/health`` 仍报 ``keys=512``
    ——每个 bucket 都空了，但没人失败，就没有任何代码回头看它们。
    """
    api._record_failure("GET /ghost", 1000.0)
    assert "GET /ghost" in api._recent_failures

    # 一次普通的读，问的是别的 key。
    api._breaker_tripped("GET /someone-else", 1000.0 + 86400)

    assert "GET /ghost" not in api._recent_failures


def test_health_reports_how_full_the_table_is():
    """这张表是进程状态，没有出口就等于"有界"只写在注释里。"""
    api._record_failure("GET /a", 1000.0)

    stats = api.health()["breaker"]

    assert stats["keys"] == 1
    assert stats["max_keys"] == api._BREAKER_MAX_KEYS
    assert stats["limit"] == api._BREAKER_LIMIT
