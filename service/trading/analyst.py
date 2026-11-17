"""Who interprets an alert, and what happens when nobody can.

Rules decide *what happened*; the reading happens in the **quant-desk workspace**,
where the model that already reads this desk every day — the cheap fast one — is
asked one compact question. That is the default, and it is the default *because*
it is cheap: a scheduled wake-up that spawns a full agent or calls a second
provider is paying a strong model to paraphrase nine lines.

``build_prompt`` is therefore a size constraint as much as a prompt: numbered
facts, quotations with their source, one optional background block, and an
explicit instruction to say what is missing rather than fill it in. A question a
small model cannot answer in one pass is a question it will answer badly.

Backends:

* ``handoff`` — **the default, and the only one the desk needs.** Write the
  question to ``data/external/handoff/<id>.json`` and return. The host answers it
  (``scripts/handoff.py answer``, or ``/monitor/handoff``) and the mail goes out
  with the reading. Nothing is invented in the meantime, and no host is assumed:
  whatever agent owns this desk reads its own workspace.
* ``llm`` — any OpenAI-compatible endpoint. Opt-in only, and the honest reason to
  avoid it is that it is a *different* model reading a desk it never saw before.

The old ``harness`` backend (spawn ``dsh headless`` for three sentences) was
removed when the desk moved hosts: a kernel that hard-codes one agent CLI is
hosted by that CLI, not by the account.

A pipeline that silently drops the analysis step is worse than one that admits it
could not take it, so the handoff is written before any backend runs.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.request
from datetime import datetime
from pathlib import Path

from service.adapters.jsonio import write_json_atomic

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "data" / "external" / "handoff"

PROMPT_HEAD = """你是持仓提醒的解读段。只能使用下面给出的事实与数字，不得引入任何未出现的价格、
比例、事件或公司信息。用三句话说明：① 触发了什么；② 对这笔持仓意味着什么；
③ 接下来最该确认哪一件事。不要给买卖建议，不要写免责声明，不要重复表格。"""

OUTRO = """输出要求：
- 三句话，每句都必须引用上面出现过的编号或数字（例如「第 2 项」「−4.8%」）。
- 若上面的事实不足以支撑第 ②③ 句，直接说缺什么，不要用常识补齐。
- 不要提出上面没有出现过的价位、事件或指标。"""


def build_prompt(alerts, contexts, *, background: dict | None = None) -> str:
    """The one question the workspace is asked. Small on purpose.

    Six numbered facts, their quotations, the book, and at most a short
    background block. Everything here is either a number or a labelled quote;
    the reading is the answer. The size ceiling is a design constraint, not an
    accident: a question that needs a tool loop to answer is a question a cheap
    model answers badly, and this desk would rather ask a good small question.
    """
    lines = [PROMPT_HEAD, "", "触发项："]
    for i, alert in enumerate(alerts[:6], start=1):
        lines.append(f"{i}. [{alert.severity}] {alert.title}：" + "；".join(alert.facts))
        if getattr(alert, "ref", ""):
            lines.append(f"   引用 {alert.ref}")
    if contexts:
        lines += ["", "持仓："]
        for c in contexts[:6]:
            lines.append(
                f"- {c.symbol} {c.qty} 股，现价 {c.last:.2f}，成本 {c.cost:.3f}，"
                f"止损 {c.stop:.2f}，竞价线 {c.auction_line:.2f}，"
                f"个股今日 {c.pct * 100:+.2f}%，浮盈亏 {c.pnl:+.0f} 元")
    if background:
        lines += ["", "背景（只用于校准，不可据此推导新结论）："]
        for key, value in background.items():
            if value in (None, "", {}):
                continue
            lines.append(f"- {key}：{value}")
    lines += ["", OUTRO]
    return "\n".join(lines)


def write_handoff(alerts, contexts, prompt: str, *, source: str = "") -> Path:
    """Write the facts and the prompt. Status starts as `pending` so the agent in
    this workspace can answer it and have the mail sent from the answer."""
    HANDOFF.mkdir(parents=True, exist_ok=True)
    # A short random suffix, because two handoffs can be built in the same second
    # (one ``evaluate`` firing a stop *and* a cap alert, or the monitor racing a
    # manual run). A second resolution meant the second document silently replaced
    # the first: one pending question disappeared and one mail was never sent.
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    path = HANDOFF / f"{stamp}.json"
    write_json_atomic(path, {
        "id": stamp,
        "status": "pending",
        "analyst": source,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "prompt": prompt,
        "alerts": [{"rule": a.rule, "severity": a.severity, "symbol": a.symbol,
                    "title": a.title, "facts": a.facts,
                    "ref": getattr(a, "ref", "")} for a in alerts],
        "positions": [{"symbol": c.symbol, "qty": c.qty, "cost": round(c.cost, 4),
                       "entry": round(c.entry, 4), "last": c.last,
                       "prev_close": c.prev_close, "stop": c.stop,
                       "auction_line": c.auction_line, "pct": round(c.pct * 100, 2),
                       "pnl": c.pnl} for c in contexts],
    })
    return path


def _via_llm(prompt: str, timeout: float) -> tuple[str | None, str]:
    key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None, "没有 LLM_API_KEY / DEEPSEEK_API_KEY"
    base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "deepseek-chat")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2, "max_tokens": 260,
    }).encode()
    request = urllib.request.Request(
        f"{base}/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
        text = (body["choices"][0]["message"]["content"] or "").strip()
        # The label says what this is: a model that has never read this desk,
        # reached around the workspace. If it ever shows up in a mail, it should
        # be recognisable at a glance rather than looking like the usual path.
        return (text or None), ("llm（外部模型，非工作区）" if text else "模型返回空内容")
    except Exception as exc:  # noqa: BLE001 - prose is optional, the alert is not
        return None, f"{type(exc).__name__}: {exc}"


def note(alerts, contexts, *, prefer: str | None = None,
         timeout: float = 180.0) -> tuple[str | None, str]:
    """Return (text, source). `source` always explains, including on failure."""
    if not alerts:
        return None, "no_alerts"
    prompt = build_prompt(alerts, contexts)
    # Default is to leave the question, not to answer it here. See the module
    # docstring: the reading belongs to the workspace, and a scheduled job that
    # spawns an agent to produce three sentences is paying a lot for packaging.
    chain = [prefer] if prefer else ["handoff"]
    reasons: list[str] = []
    for backend in chain:
        if backend == "handoff":
            return None, "handoff（问题已留在 data/external/handoff，等工作区回答）"
        if backend == "llm":
            text, why = _via_llm(prompt, timeout)
        else:
            return None, f"未知解读后端：{backend}"
        if text:
            return text, why
        reasons.append(why)
    return None, "；".join(reasons)
