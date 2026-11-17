"""这个进程跑的是哪一份代码。

为什么需要它
------------
长驻服务不会自己重载代码。改了文件、不重启，进程就跑着旧逻辑——而症状是
"行为变了，但说不清为什么"。2026-11-01 19:45 的服务日志里有三次
``NameError: name 'MAX_PAGES' is not defined``：那个 bug 当天早些时候已经在
磁盘上修好了，但服务没重启，于是接口连着报 500。查代码是好的，看进程是旧的。

所以 ``/health`` 要带上两样东西：

* **启动时刻** —— 这个进程是什么时候起来的；
* **代码指纹** —— 磁盘上的代码现在是什么样子。

指纹是 ``(相对路径, mtime, size)`` 的哈希，不是文件内容哈希：内容哈希要把
四万个 Python 文件读一遍，而 ``/health`` 是监控唯一能问的地方，不能慢。mtime
加 size 足以回答"进程起来之后代码有没有被改过"。

``dirty`` 只回答"源码有没有未提交的改动"。运行写出来的东西（``data/``、
``reports/``、``playbook/meta.json``）列在 ``dirty_files`` 之外的
``runtime_files`` 里：2026-11-04 那个字段恒为 true，因为每写一次 playbook 草稿
就改一次已跟踪的 meta.json——一个正常运行时也亮着的灯，读起来等于没灯。

    from service.api.build import build_info

    build_info()["code_changed_since_start"]
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What the fingerprint covers, and why each group is separate.
#:
#: It used to cover `service/**/*.py` only, so editing `plugin/lib/index.js`
#: (tool descriptions, input schemas, the shrink ladder, the 60 s timeout) or
#: `configs/params.yaml` (169 keys) left `/health` reporting `false`. That was
#: read as "the process is up to date" while it served an old tool table and old
#: risk limits. Three booleans instead of one, because the remedy differs:
#: restart the API for `code`, restart the MCP process for `tooling`, and
#: decide whether the config change was meant to apply to a running process.
_FINGERPRINT_GROUPS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "code": ("service", ("*.py",), ()),
    "tooling": ("plugin", ("*.js", "*.mjs"), ("node_modules",)),
    "config": ("configs", ("*.yaml", "*.yml", "*.json"), ()),
}

#: Directories that never hold source we care about, wherever they appear.
#:
#: Deliberately short. `data/`, `reports/` and `tests/` sit at the repo root,
#: outside all three groups, so they never need pruning; listing them here would
#: be a landmine, because a future `service/data/` package would silently drop
#: out of the fingerprint and `/health` would report "up to date" for code that
#: had changed.
_SKIP_PARTS = frozenset({"__pycache__", ".venv", "node_modules"})

#: Files at the repo root whose contents change behaviour but live outside the
#: three trees above. `.env` is read by `service.settings`, so a changed token,
#: port or feature flag is a changed runtime.
_ROOT_FILES = (".env",)

#: Paths that change because the thing *ran*, not because someone edited code.
#:
#: `dirty` used to be ``bool(git status --porcelain)``, which is true on any host
#: that has ever traded: `playbook/meta.json` is tracked and rewritten by every
#: playbook proposal, and `data/`/`reports/` fill up with untracked artifacts.
#: A flag that is true in normal operation answers nothing — on 2026-11-04 the
#: only tracked dirty file was that registry, and `dirty: true` was read as
#: "someone changed the code" for two days.
#:
#: Prefixes, not globs: a runtime directory is a place, and everything under it
#: is produced by running. `playbook/v1.md` is deliberately *not* here — that one
#: is written by a person.
_RUNTIME_PREFIXES = (
    "data/",
    "reports/",
    "logs/",
    ".audit-scratch/",
    "htmlcov/",
    "playbook/meta.json",
    "playbook/drafts/",
)


def split_dirty(porcelain: str) -> tuple[list[str], list[str]]:
    """Split ``git status --porcelain`` into ``(code, runtime)`` paths.

    Renames carry two paths (``R  old -> new``); the new one is what exists now.
    Quoted paths are unwrapped so the prefix test sees the real directory.
    """
    code: list[str] = []
    runtime: list[str] = []
    for line in (porcelain or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        path = path.strip('"').strip()
        if not path:
            continue
        (runtime if path.startswith(_RUNTIME_PREFIXES) else code).append(path)
    return code, runtime

#: This module's import time is the process's start time, as near as the
#: interpreter can say. Every module in the app is imported after this one.
PROCESS_STARTED_AT = datetime.now(UTC)

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 30.0

#: Key -> the event the *owner* sets when it is done building that key.
#:
#: `/health` is polled by monitors and by the desktop window's own refresh, and
#: a cold `build_info()` is 57 ms measured — three tree walks (128 Python files,
#: 17 JS files, the config dir) plus two `git` subprocesses. Without a claim,
#: every caller that misses the cache builds its own copy, so a 30 s expiry on a
#: polled endpoint costs N × 57 ms and the last writer wins.
_INFLIGHT: dict[str, threading.Event] = {}

#: How long a non-owner waits before deciding to build it itself.
#:
#: Bounded on purpose. `git` is capped at 5 s inside `_git`, but a wedged
#: filesystem is not, and blocking the one endpoint a monitor uses is worse than
#: doing the same work twice.
_WAIT_TIMEOUT = 10.0


def _source_files(kind: str = "code") -> list[Path]:
    """Files in one group, with skipped directories pruned *before* descent.

    ``rglob`` was measured at 243 ms for the three groups because it walks
    ``plugin/node_modules`` (tens of thousands of files) and filters afterwards.
    `/health` is the endpoint a monitor polls; pruning ``dirnames`` in place
    brings the same answer back to a few milliseconds.
    """
    directory, patterns, excluded = _FINGERPRINT_GROUPS.get(
        kind, _FINGERPRINT_GROUPS["code"])
    base = ROOT / directory
    skip = set(excluded) | _SKIP_PARTS
    files: set[Path] = set()
    if base.exists():
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in skip]
            files.update(Path(dirpath) / name for name in filenames
                         if any(fnmatch(name, pattern) for pattern in patterns))
    if kind == "config":
        files.update(ROOT / name for name in _ROOT_FILES if (ROOT / name).is_file())
    return sorted(files)


def fingerprint(kind: str = "code") -> str:
    """A hash over the shape of one source group: path, mtime, size.

    Content hashing would read 128 Python files and 17 JS files on every
    `/health`; mtime plus size answers the only question that matters — "did the
    disk move after this process started". `/health` is the one endpoint a
    monitor can ask, so it must stay in the millisecond range.
    """
    digest = hashlib.sha256()
    for path in _source_files(kind):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(str(int(stat.st_mtime)).encode("utf-8"))
        digest.update(str(stat.st_size).encode("utf-8"))
    return digest.hexdigest()[:16]


def code_fingerprint() -> str:
    return fingerprint("code")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # `rstrip`, never `strip`: porcelain's first line is `` M path`` and that
    # leading space is the index status. Stripping it turned the first dirty
    # file into ``laybook/meta.json`` — a path that starts with a letter, so it
    # was classified as source and `dirty` stayed true for the wrong reason.
    return out.stdout.rstrip() or None


def _cached(key: str, builder):
    """One build per key per TTL, however many callers arrive at once.

    The previous body read the cache, dropped the lock, built, then wrote back.
    Two things fell out of that:

    * every caller in a cold window built its own copy — three fingerprints and
      two `git` subprocesses each, 57 ms measured, multiplied by the number of
      monitors polling;
    * the last writer won, so a build that started earlier could publish after
      a newer one and reset the age of the entry.

    The builder still runs outside the lock — holding it for 57 ms would
    serialise the other five keys too — only the claim and the publish are in.
    """
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
        claim = _INFLIGHT.get(key)
        if claim is None:
            claim = threading.Event()
            _INFLIGHT[key] = claim
            owner = True
        else:
            owner = False

    if not owner:
        if claim.wait(_WAIT_TIMEOUT):
            with _LOCK:
                hit = _CACHE.get(key)
            if hit is not None:
                return hit[1]
        # 拥有者抛异常或超时了。自己算一遍：`/health` 不该挂在别人身上。
        return builder()

    try:
        value = builder()
    except BaseException:
        # 抛异常也要放掉认领，否则这个键在进程余下的生命里只剩超时等待。
        with _LOCK:
            _INFLIGHT.pop(key, None)
        claim.set()
        raise
    with _LOCK:
        # Stamp at *completion*: an entry is as fresh as the disk it read.
        _CACHE[key] = (time.monotonic(), value)
        _INFLIGHT.pop(key, None)
    claim.set()
    return value


def build_info() -> dict:
    """What is running, and whether the disk still matches it."""
    started = _cached("started", lambda: PROCESS_STARTED_AT.isoformat(timespec="seconds"))
    prints = {kind: _cached(f"fingerprint:{kind}", lambda k=kind: fingerprint(k))
              for kind in _FINGERPRINT_GROUPS}
    commit = _cached("commit", lambda: _git("rev-parse", "--short", "HEAD"))
    code_dirty, runtime_files = _cached(
        "dirty", lambda: split_dirty(_git("status", "--porcelain") or ""))
    return {
        "commit": commit,
        # `dirty` means "the source tree differs from HEAD" — restart to pick it
        # up. Files produced by running (data/, reports/, the playbook registry)
        # are listed separately; they are not a reason to restart anything.
        "dirty": bool(code_dirty),
        "dirty_files": code_dirty[:20],
        "runtime_files": runtime_files[:20],
        "started_at": started,
        "uptime_seconds": round(uptime_seconds(), 1),
        "code_fingerprint": prints["code"],
        "tooling_fingerprint": prints["tooling"],
        "config_fingerprint": prints["config"],
        # The ones that matter: the disk moved under the running process.
        # `code_changed_since_start` means restart the API; the other two say
        # what else is stale even when the Python tree is untouched.
        "code_changed_since_start": prints["code"] != _start_fingerprints["code"],
        "tooling_changed_since_start": prints["tooling"] != _start_fingerprints["tooling"],
        "config_changed_since_start": prints["config"] != _start_fingerprints["config"],
    }


#: The fingerprints at import time — what this process was started from.
_start_fingerprints = {kind: fingerprint(kind) for kind in _FINGERPRINT_GROUPS}


def uptime_seconds() -> float:
    return max(0.0, time.time() - PROCESS_STARTED_AT.timestamp())
