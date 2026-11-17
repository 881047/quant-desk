"""Watchdog for the local quant service.

Run with pythonw.exe (no console) by the `quant-desk-service` scheduled task.
Keeps uvicorn alive, appends one UTF-8 log, and never spawns a second instance
when the port is already in use.

Two files, on purpose:

- ``data/service.log``  — supervisor events only (start, exit, restart)
- ``data/access.log``   — uvicorn access log, timestamped, rotated at midnight

They used to share one handle. Because the supervisor holds the file open in
text mode and uvicorn writes to the same descriptor as a subprocess, the two
writers overwrote each other's lines; on 2026-10-26 53% of requests (197 logged
vs 419 counted) never reached the log, and the 241 proposals that motivated this
change left three readable lines.

    pythonw.exe scripts/supervise.py
    python.exe  scripts/supervise.py --once     # debug: run in foreground
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "service.log"
LOG_CONFIG = ROOT / "configs" / "uvicorn_log.json"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
HOST, PORT = "127.0.0.1", 8811

# The supervisor runs under pythonw (no console). A console child would otherwise
# make Windows Terminal open a visible window on the desktop, so hide it.
CREATE_NO_WINDOW = 0x08000000


def uvicorn_cmd() -> list[str]:
    cmd = [str(PYTHON), "-m", "uvicorn", "service.api.app:app",
           "--host", HOST, "--port", str(PORT)]
    if LOG_CONFIG.exists():
        cmd += ["--log-config", str(LOG_CONFIG)]
    return cmd


def spawn_flags() -> dict:
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": info}


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run uvicorn in the foreground")
    args = parser.parse_args()

    if not PYTHON.exists():
        log(f"python not found: {PYTHON}")
        return 2

    if args.once:
        return subprocess.call(uvicorn_cmd(), cwd=ROOT)

    log("supervisor start")
    while True:
        if port_open():
            log(f"port {PORT} already listening; supervisor waits")
            time.sleep(30)
            continue
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] launching uvicorn\n")
            fh.flush()
            # stdout is the supervisor's *startup* channel only; uvicorn routes
            # access lines to data/access.log through --log-config.
            proc = subprocess.Popen(
                uvicorn_cmd(),
                cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                **spawn_flags(),
            )
            code = proc.wait()
        log(f"uvicorn exited with code {code}; restarting in 3s")
        time.sleep(3)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
