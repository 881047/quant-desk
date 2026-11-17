"""Did tonight's data run finish, and what did it decide?

Two questions get asked at 18:50, and until now one file answered both of them
badly. ``data/ALERT.txt`` exists only when the gate is ``not_ready``, so a
missing file means *either* "the run finished clean" *or* "the run never
started" — and those are different answers. On 2026-10-29 it was the second:
the scheduled run was missed, the desk read the silence as permission, and a
close report went out over a session that had never been published.

So a run now leaves a positive marker. ``data/daily_done/<day>.json`` is written
last, after the report, so its existence implies the report exists; it carries
the gate and the failed step names, which is what a reader needs to know which
numbers not to quote.

The same run holds ``data/daily_job.lock`` from start to finish. That is the
other half of the fix. The documented fallback for a late session is "再跑一次
``daily_job --days 3``", and two runs merging the same partition race on
read-modify-write: the manifest guard orders the writers by ``ingested_at``, but
ordering is not serialising — the later starter can commit a frame it read
before the earlier writer's rows existed, and those rows are then gone from the
live version. 2026-11-02 recorded exactly that shape ("两个进程同一刻写同一个
``pool_tape`` 分区会撞清单指针"), which is why the snapshot loop was cut down to a
single process. The lock is the same rule, applied to the job that has to be
re-runnable.

A stale lock is not a deadlock: the pid is checked, and a lock older than
``pipeline.stale_after_minutes`` is taken over even if the pid is still alive.
The scheduler's ``ExecutionTimeLimit`` is six hours, so anything older than that
is not a run, it is a leftover.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from service import settings
from service.adapters.jsonio import write_json_atomic
from service.kernel.clock import SHANGHAI

STATE_DIR = "daily_done"
LOCK_NAME = "daily_job.lock"

#: Files kept under ``daily_done/``. One per day plus one per off-hours pass, so
#: sixty is about three weeks of history — enough to see "it has been dead since
#: the 29th" without turning the directory into a slow listing.
KEEP_MARKERS = 60


class PipelineBusy(RuntimeError):
    """Another run holds the lock. ``holder`` says which one, and since when."""

    def __init__(self, holder: dict[str, Any]):
        self.holder = holder
        who = holder.get("pass") or "daily_job"
        pid = holder.get("pid")
        since = holder.get("started_at") or "?"
        super().__init__(
            f"another {who} run is in progress (pid={pid}, started {since}); "
            f"not starting a second writer on the same partitions"
        )


def marker_path(data_root: str | Path, day: str, pass_name: str = "close") -> Path:
    """Where one pass records that it finished. Close keeps the plain name."""
    name = f"{day}.json" if pass_name == "close" else f"{day}_{pass_name}.json"
    return Path(data_root) / STATE_DIR / name


def write_marker(data_root: str | Path, day: str, pass_name: str,
                 payload: dict[str, Any], *, now: datetime | None = None) -> Path:
    """Record that this pass finished, and how it went.

    Written unconditionally — a run that ends ``gate=not_ready`` still finished,
    and "finished badly" is a different fact from "never ran". Atomic, because
    the API reads it while the job may be rewriting it tomorrow.
    """
    stamp = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    record = dict(payload)
    record["day"] = day
    record["pass"] = pass_name
    record["finished_at"] = stamp.isoformat(timespec="seconds")
    path = marker_path(data_root, day, pass_name)
    write_json_atomic(path, record)
    _prune(Path(data_root) / STATE_DIR)
    return path


def read_marker(data_root: str | Path, day: str,
                pass_name: str = "close") -> dict[str, Any] | None:
    """The marker for one day, or ``None`` when nothing was written."""
    path = marker_path(data_root, day, pass_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def latest_marker(data_root: str | Path) -> dict[str, Any] | None:
    """The newest close marker on disk, whatever its day.

    Used only to answer "is this a missed day or a dead pipeline": a single
    missing marker with a healthy one behind it is a bad evening, no marker at
    all for a week is a broken schedule.
    """
    folder = Path(data_root) / STATE_DIR
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    for path in reversed(files):
        if "_" in path.stem and path.stem.rsplit("_", 1)[1] in ("late", "morning"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def status(data_root: str | Path, *, today: str | None = None,
           now: datetime | None = None,
           trading_day: bool | None = None) -> dict[str, Any]:
    """One compact block answering "may I trust tonight's numbers yet?".

    Six states, because they lead to different actions:

    ``running``       a run holds the lock — do not start another, and do not
                      quote half-written numbers
    ``done``          finished, gate ready
    ``done_not_ready`` finished, but named steps failed — quote nothing from
                      those datasets
    ``missing``       a trading day past the due time with no marker: either it
                      never ran or it died halfway
    ``pending``       not due yet (every morning looks like this)
    ``not_required``  not a trading day
    """
    root = Path(data_root)
    stamp = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    day = today or stamp.strftime("%Y%m%d")
    out: dict[str, Any] = {
        "state": "missing", "day": day, "gate": None, "finished_at": None,
        "age_minutes": None, "failed_steps": [], "supplementary_failed": [],
        "note": "",
    }

    holder = lock_holder(root)
    if holder is not None:
        age = _age_minutes(holder.get("started_at"), stamp)
        out.update(state="running", gate=None,
                   finished_at=holder.get("started_at"), age_minutes=round(age, 1),
                   note=(f"{holder.get('pass') or 'daily_job'} 正在跑"
                         f"（pid {holder.get('pid')}，已 {age:.0f} 分钟）。"
                         f"此刻读到的一半数字可能还没落地，不要再起一个。"))
        return out

    marker = read_marker(root, day)
    if marker is not None:
        gate = str(marker.get("gate") or "unknown")
        failed = [str(s) for s in (marker.get("failed_steps") or [])]
        # Supplementary steps are visible but do not change `state`: on
        # 2026-11-12 an `intraday_features` failure turned the whole day red
        # while the close facts were READY. They are still named here so a
        # reader knows which datasets are thinner than usual.
        supplementary = [str(s) for s in (marker.get("supplementary_failed") or [])]
        age = _age_minutes(marker.get("finished_at"), stamp)
        out.update(state="done" if gate == "ready" else "done_not_ready",
                   gate=gate, finished_at=marker.get("finished_at"),
                   age_minutes=round(age, 1), failed_steps=failed,
                   supplementary_failed=supplementary)
        if gate == "ready":
            out["note"] = f"{day} 的日更已结束，gate=ready。"
            if supplementary:
                out["note"] += (f" 补充数据未就绪：{'、'.join(supplementary)}"
                                f"（不影响判定，别把它们当完整）。")
        else:
            out["note"] = (
                f"{day} 的日更结束了，但 gate=not_ready："
                f"{'、'.join(failed) or 'backfill/verify 失败'}。"
                f"这些步骤对应的数据集今天不可信，别从里面取数写结论。")
        return out

    if trading_day is False:
        out.update(state="not_required", note=f"{day} 不是交易日，没有日更要等。")
        return out

    due = str(settings.text("pipeline.daily_due"))
    if stamp.strftime("%H:%M") < due:
        out.update(state="pending",
                   note=f"{day} 的日更 {due} 才到点，现在 {stamp:%H:%M}。")
        return out

    last = latest_marker(root)
    if last is not None and last.get("day"):
        out["last_done"] = {"day": str(last["day"]), "gate": last.get("gate")}
    out["note"] = (
        f"{day} 是交易日，但 {due} 之后没有留下完成标记——要么没跑，要么崩在中途。"
        f"这两种都不是「数据还没出来」，别用盘中快照顶替；"
        f"先看 data/daily_job.log 末 20 行再决定补不补。")
    return out


def lock_holder(data_root: str | Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The run currently holding the lock, or ``None``.

    Dead pid → stale. Alive pid but older than the scheduler's time limit →
    also stale: the task was killed, and a killed process keeps its pid file.
    """
    path = Path(data_root) / LOCK_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        # A lock we cannot parse is still a lock if it is young; refusing here is
        # safer than starting a second writer because a file got truncated.
        try:
            age_s = _seconds_since(path.stat().st_mtime, now)
        except OSError:
            return None
        return {"pid": None, "started_at": None, "pass": "unknown"} \
            if age_s is not None and age_s <= _stale_after_s() else None

    pid = payload.get("pid")
    if isinstance(pid, int) and not _alive(pid):
        return None
    age_s = _seconds_since(path.stat().st_mtime, now)
    if age_s is not None and age_s > _stale_after_s():
        return None
    return payload


@contextmanager
def hold(data_root: str | Path, pass_name: str, *,
         now: datetime | None = None) -> Iterator[dict[str, Any]]:
    """Take the single-writer lock, or raise :class:`PipelineBusy`.

    ``O_CREAT|O_EXCL`` rather than "check then write": the check-then-write form
    lets two runs that start in the same second both see no file and both take
    it. Exactly once, or not at all.
    """
    stamp = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_NAME
    payload = {"pid": os.getpid(), "pass": pass_name,
               "started_at": stamp.isoformat(timespec="seconds")}

    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = lock_holder(root, now=stamp)
            if holder is not None:
                raise PipelineBusy(holder) from None
            # Stale: nobody owns it any more. One retry, because another process
            # may be clearing it at the same moment.
            if attempt == 2:
                raise PipelineBusy({"pid": None, "pass": "unknown",
                                    "started_at": None}) from None
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        break
    try:
        yield payload
    finally:
        _release(path, payload)


def _release(path: Path, payload: dict[str, Any]) -> None:
    """Drop the lock — but only if it is still ours.

    A run that overran the stale window has had its lock taken over. Deleting
    the successor's lock in that case would let a third run in.
    """
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and current.get("pid") != payload.get("pid"):
        return
    path.unlink(missing_ok=True)


def _alive_windows(pid: int) -> bool:
    """Inspect a process object without sending it or its console a signal.

    ``os.kill(pid, 0)`` is not a no-op probe on Windows. Opening a handle with
    only ``SYNCHRONIZE`` and waiting for zero milliseconds answers the same
    question: a process object is signalled when that process exits.
    """
    import ctypes
    from ctypes import wintypes

    if pid > 0xFFFFFFFF:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    synchronize = 0x00100000
    handle = open_process(synchronize, False, pid)
    if not handle:
        # A protected live process must not make its lock look abandoned. An
        # invalid pid, by contrast, means the previous holder is gone.
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
    try:
        result = wait(handle, 0)
        if result == 0:  # WAIT_OBJECT_0: the process has exited.
            return False
        if result == 0x00000102:  # WAIT_TIMEOUT: still running.
            return True
        # If the answer is inconclusive, retain the lock rather than risking a
        # second writer on the same partitions.
        return True
    finally:
        close(handle)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:  # exists, but not ours to inspect
        return True
    except OSError:
        return False
    return True


def _stale_after_s() -> float:
    return float(settings.num("pipeline.stale_after_minutes")) * 60.0


def _seconds_since(stamp: float, now: datetime | None) -> float | None:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return current.timestamp() - float(stamp)


def _age_minutes(iso: str | None, now: datetime) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(str(iso))
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=SHANGHAI)
    return max(0.0, (now - then.astimezone(SHANGHAI)).total_seconds() / 60.0)


def _prune(folder: Path) -> None:
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in files[:-KEEP_MARKERS]:
        try:
            path.unlink()
        except OSError:
            pass
