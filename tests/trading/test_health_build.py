"""``/health`` 要能回答"跑的是哪一份代码"。

长驻服务不会自己重载代码：改了文件不重启，进程就跑旧逻辑。2026-11-01 的
服务日志里有三次 ``NameError: name 'MAX_PAGES' is not defined``——那个 bug 当天
早些时候已经在磁盘上修好了，但服务没重启，接口连着报 500。查代码是好的，看
进程是旧的：**两件事都对，只有对照着看才知道真相**。

所以 ``/health`` 带上：commit、工作区是否脏、进程启动时刻、代码指纹，以及
最关键的那一条——**磁盘上的代码是不是已经比这个进程新了**。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import service.api.build as build_module
from service.api.app import app
from service.api.build import build_info, code_fingerprint


@pytest.fixture
def cold_cache(monkeypatch):
    """A cache that has never been filled, so every build is a cold build."""
    monkeypatch.setattr(build_module, "_CACHE", {})
    monkeypatch.setattr(build_module, "_INFLIGHT", {})
    return build_module._CACHE


def test_health_carries_the_build_fingerprint():
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["ok"] is True
    build = body["build"]
    assert "commit" in build
    assert "dirty" in build
    assert "started_at" in build
    assert "code_fingerprint" in build
    assert build["code_fingerprint"] == code_fingerprint()


def test_health_does_not_need_a_token():
    """``/health`` 是唯一免鉴权的端点，监控与人都靠它。"""
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_the_fingerprint_changes_when_the_source_changes():
    """指纹要真的跟着代码走，否则"没变"这三个字没有意义。"""
    import service.api.build as build_module

    first = build_module.code_fingerprint()
    victim = build_module.ROOT / "service" / "api" / "_fingerprint_probe.py"
    try:
        victim.write_text("# probe\n", encoding="utf-8")
        assert build_module.code_fingerprint() != first, \
            "多了一个源文件，指纹却没变"
    finally:
        victim.unlink(missing_ok=True)


def test_the_fingerprint_covers_the_tool_layer_and_the_config():
    """只扫 `service/**/*.py` 时，改工具层或配置后 `/health` 仍报 false。

    2026-11-04 之前的 17:35 / 18:05 / 18:26 三段都把 `false` 当作"服务已重启生效"
    的确认，而 `plugin/lib/index.js` 与 `configs/params.yaml` 的改动根本不在指纹里。
    """
    import service.api.build as build_module

    for kind in ("code", "tooling", "config"):
        assert build_module.fingerprint(kind), f"{kind} 指纹为空"

    before = build_module.fingerprint("tooling")
    victim = build_module.ROOT / "plugin" / "lib" / "_fingerprint_probe.js"
    try:
        victim.write_text("// probe\n", encoding="utf-8")
        assert build_module.fingerprint("tooling") != before, "工具层改动必须可见"
        assert build_module.code_fingerprint() == build_module.fingerprint("code")
    finally:
        victim.unlink(missing_ok=True)

    before = build_module.fingerprint("config")
    probe = build_module.ROOT / "configs" / "_fingerprint_probe.yaml"
    try:
        probe.write_text("probe: 1\n", encoding="utf-8")
        assert build_module.fingerprint("config") != before, "配置改动必须可见"
    finally:
        probe.unlink(missing_ok=True)


def test_build_info_reports_each_kind_of_drift():
    info = build_info()
    for key in ("code_changed_since_start", "tooling_changed_since_start",
                "config_changed_since_start"):
        assert isinstance(info[key], bool)
    for key in ("tooling_fingerprint", "config_fingerprint"):
        assert info[key]
    assert info["uptime_seconds"] >= 0


def test_dirty_ignores_what_running_wrote():
    """``dirty`` 曾经是 ``bool(git status)``，于是每个交易过的机器上都是 true。

    2026-11-04 唯一的已跟踪脏文件是 ``playbook/meta.json``（每次提案都重写它），
    而 ``data/`` ``reports/`` 一直是未跟踪产物。一个正常运行也恒真的标志位，
    唯一的用处是让人习惯忽略它。
    """
    code, runtime = build_module.split_dirty(
        " M playbook/meta.json\n"
        "?? data/archive/\n"
        " M service/api/build.py\n"
        "?? reports/daily/20261105.json\n"
        "A  playbook/v1.md\n"
    )

    assert code == ["service/api/build.py", "playbook/v1.md"]
    assert runtime == ["playbook/meta.json", "data/archive/",
                       "reports/daily/20261105.json"]


def test_a_rename_is_judged_by_its_new_path():
    code, runtime = build_module.split_dirty(
        "R  data/old.json -> data/new.json\n"
        'R  "service/a.py" -> "service/b.py"\n'
    )
    assert runtime == ["data/new.json"]
    assert code == ["service/b.py"]


def test_git_output_keeps_the_leading_space_that_means_something(monkeypatch):
    """``git status --porcelain`` 的第一行以空格开头，那个空格是暂存区状态。

    ``_git`` 原来用 ``strip()``，于是第一行变成 ``M playbook/meta.json``，
    按 3 个字符切片得到 ``laybook/meta.json``——一个以字母开头的路径，被算成源码。
    ``split_dirty`` 的单元用例一直是绿的，因为它们是手写的字符串；坏的接缝在
    ``_git`` 与它的调用者之间。
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        build_module.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=" M playbook/meta.json\n"))
    assert build_module._git("status", "--porcelain") == " M playbook/meta.json"


def test_runtime_prefixes_never_shadow_source():
    """前缀写错一个字母，源码改动就会被算成"运行时噪声"而永远不报警。"""
    for prefix in build_module._RUNTIME_PREFIXES:
        assert not prefix.startswith(
            ("service/", "plugin/", "configs/", "scripts/", "tests/")), prefix


def test_the_live_tree_separates_the_two_buckets():
    info = build_info()
    assert isinstance(info["dirty_files"], list)
    assert isinstance(info["runtime_files"], list)
    assert bool(info["dirty"]) == bool(info["dirty_files"])
    assert not [p for p in info["dirty_files"]
                if p.startswith(build_module._RUNTIME_PREFIXES)]


def test_health_names_a_legacy_env_var_that_no_longer_works(monkeypatch):
    """``.env`` 里曾写着 ``INITIAL_CASH=5000``：它压过配置，却不在 provenance 里。

    删掉读取之后，这个变量如果连提都不提，就只是把静默换了个方向——从
    "改了没反应"变成"设了没人理"。``/health`` 是唯一免鉴权、也是唯一会被
    人真的打开看一眼的端点，所以名单放这里。
    """
    monkeypatch.setenv("INITIAL_CASH", "9999")

    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["config"]["legacy_env_ignored"] == {
        "INITIAL_CASH": "risk.initial_cash",
    }


def test_a_cold_key_is_built_once_however_many_callers_arrive(cold_cache):
    """``/health`` 是监控轮询的端点，冷窗口里 N 个请求各自全树 stat 一遍。

    一次冷 ``build_info()`` 实测 57 ms：三个目录树（128 个 py、17 个 js、配置目录）
    加两次 ``git`` 子进程。旧实现先在锁内查、在锁外建、再写回，于是每个未命中的
    调用者都建一份，最后写回的那份覆盖先写的。
    """
    calls = []
    gate = threading.Barrier(8)

    def builder():
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return "value"

    results: list[str] = []
    lock = threading.Lock()

    def worker():
        gate.wait(timeout=5)
        value = build_module._cached("cold", builder)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1, f"8 个并发调用者建了 {len(calls)} 次"
    assert results == ["value"] * 8


def test_a_failing_builder_releases_the_claim(cold_cache):
    """builder 抛异常不释放认领，这个键就在进程余下的生命里只剩超时等待。

    `_git` 会吞掉自己的失败，但 `fingerprint` 的 `stat` 之外还有 `os.walk`
    ——一个读不了的目录可以抛在任何一步。留着一个永远不 set 的 event，
    后来者每次都要等满超时才自己动手。
    """
    attempts = []

    def builder():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("disk went away")
        return "second time"

    with pytest.raises(OSError):
        build_module._cached("flaky", builder)

    assert build_module._INFLIGHT == {}, "异常之后认领还挂在字典里"
    assert build_module._cached("flaky", builder) == "second time"
    assert len(attempts) == 2


def test_a_stalled_builder_does_not_wedge_other_callers(cold_cache, monkeypatch):
    """等不到就自己算：``/health`` 不该挂在别人身上。

    ``_WAIT_TIMEOUT`` 是有限的，因为 ``git`` 有 5 秒超时、卡住的文件系统没有。
    """
    monkeypatch.setattr(build_module, "_WAIT_TIMEOUT", 0.05)
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "owner"

    owner = threading.Thread(target=lambda: build_module._cached("slow", slow))
    owner.start()
    assert started.wait(5), "拥有者没开始"

    begin = time.monotonic()
    value = build_module._cached("slow", lambda: "waiter")
    elapsed = time.monotonic() - begin
    release.set()
    owner.join(timeout=5)

    assert value == "waiter", "等不到就该自己算，而不是把 None 返回给 /health"
    assert elapsed < 1.0, f"等了 {elapsed:.2f} 秒，说明超时没生效"
