"""Local API auth.

Single shared token, read from ``QUANT_TOKEN``. The service binds to 127.0.0.1
only; the token exists to stop other local processes (and a confused browser
tab) from touching the paper account.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Header, HTTPException, Request, Response

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DEFAULT_TOKEN = "change-me-local-only"
TOKEN = os.getenv("QUANT_TOKEN", DEFAULT_TOKEN)

if TOKEN == DEFAULT_TOKEN:
    # Only `MODE=remote` refuses the default. In paper mode it is the de-facto
    # credential for the whole account, and it is printed in this file — so an
    # operator who never set QUANT_TOKEN has a published password and no signal.
    logging.getLogger(__name__).warning(
        "QUANT_TOKEN is the built-in default; any local process that has read "
        "this repository can place orders. Set QUANT_TOKEN in .env and restart.")


def token_is_default() -> bool:
    """是否还在用写在仓库里的那个默认口令。

    这个信号以前只写进日志——而日志没人读。做成可查询的状态，`/health` 与
    `/data/status` 才会带着它，看板上也能看见。改它需要同时改 `.env` 和 MCP 客户端
    那份配置（`plugin/mcp/install.mjs` 写的那份），只改一边会让所有工具调用 401。
    """
    return TOKEN == DEFAULT_TOKEN


def _matches(candidate: str) -> bool:
    return bool(TOKEN) and secrets.compare_digest(candidate.encode("utf-8"), TOKEN.encode("utf-8"))


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_token(
    request: Request,
    response: Response,
    x_quant_token: str | None = Header(default=None, alias="X-Quant-Token"),
    authorization: str | None = Header(default=None),
) -> None:
    """Accept the legacy header or Bearer auth; local bypass is explicit opt-in."""
    response.headers["Cache-Control"] = "no-store"
    remote = os.getenv("MODE", "paper").strip().lower() == "remote"
    if not TOKEN or (remote and TOKEN == DEFAULT_TOKEN):
        raise HTTPException(status_code=503, detail={
            "code": "TOKEN_NOT_CONFIGURED",
            "message": "QUANT_TOKEN must be configured",
            "next": "在 .env 里设置 QUANT_TOKEN，重跑 node plugin/mcp/install.mjs，再重启服务与宿主",
        })

    allow_loopback = os.getenv("QUANT_ALLOW_LOOPBACK", "0").lower() in ("1", "true", "yes")
    if allow_loopback and _is_loopback(request):
        return

    candidates = [x_quant_token] if x_quant_token else []
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            candidates.append(credential.strip())
    if any(_matches(candidate) for candidate in candidates):
        return
    # The most common cause is a rotated token that never reached the host's
    # config. `~/.workbuddy/mcp.json` carries its own copy (written by
    # `plugin/mcp/install.mjs`), so changing `.env` alone leaves every tool
    # 401-ing while the service itself is healthy. Say where to fix it — a bare
    # "invalid credentials" sends the reader to the wrong file.
    raise HTTPException(
        status_code=401,
        detail={
            "code": "UNAUTHORIZED",
            "message": "invalid credentials",
            "next": ("改完 .env 的 QUANT_TOKEN 要重跑 node plugin/mcp/install.mjs "
                     "（~/.workbuddy/mcp.json 里那份不会自动更新），再重启宿主；"
                     "只重启服务不够。"),
        },
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )
