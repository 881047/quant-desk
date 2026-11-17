"""Print the exact HTTP contract the service exposes, and the paths it does not.

    python scripts/api_contract.py                 # markdown to stdout
    python scripts/api_contract.py --write         # refresh docs/API_CONTRACT.md
    python scripts/api_contract.py --json          # machine-readable

Why this exists
---------------
On 2026-10-26 a session made 241 proposals and created zero tickets. The log
fragments show why: it called ``POST /decision/approve`` (the real route needs a
ticket id), ``POST /core/rebalance`` and ``POST /risk/stops/enforce`` (both
deleted), and sent bodies the schema rejected. Nothing was wrong with the
engine; the caller was working from memory of an older surface.

A caller must not guess. This script turns ``app.openapi()`` into the two lists
that matter: routes that exist, and routes that used to exist.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.api.app import app

DOCS = ROOT / "docs" / "API_CONTRACT.md"

# Removed or never-existed paths that a model has actually called. Keep the
# reason next to each one: "404" alone does not stop the next attempt.
#
# The last four are not hypothetical: they are in ``data/access.log`` with a
# timestamp. A model guessed the path, got a 404, and (at least once) retried.
# The point of this table is to stop the *next* guess, so a path that was
# actually guessed belongs here.
TRAPS = {
    "POST /decision/approve": {
        "use": "POST /decision/{ticket_id}/approve",
        "why": "the ticket id is a path segment, not a body field",
    },
    "POST /decision/cancel": {
        "use": "POST /decision/{ticket_id}/cancel",
        "why": "the ticket id is a path segment, not a body field; cancel withdraws a QUEUED order (and an ARMED intent), while reject closes a PROPOSAL; on an armed intent both routes set status=cancelled without spending money",
    },
    "POST /core/rebalance": {
        "use": "POST /core/plan then POST /core/execute",
        "why": "P19 removed automatic rebalancing; the plan is advisory until executed",
    },
    "POST /risk/stops/enforce": {
        "use": "POST /decision/propose with side=SELL",
        "why": "P15 removed the /risk/stops/enforce route; selling still goes through /decision/propose. The kernel places no new-judgement orders, but a stop written on the entry ticket is executed by the capture loop through propose + approve once a breach has persisted autostop.confirm_seconds (configs/params.yaml autostop.enabled turns it off)",
    },
    "GET /advice": {
        "use": "GET /strategies (its `allocation` block)",
        "why": "no advice route exists; the table is an orphan, see below",
    },
    "POST /advice/collect": {
        "use": "GET /strategies (its `allocation` block)",
        "why": "the advice table has no DDL and no code path; nothing writes it",
    },
    # 这条即使已经补了别名也不能删：陷阱表记的是"模型确实猜错过什么"，
    # 不是"现在还有没有效"。删掉它，下一个人会以为从没人在这里摔倒过。
    "GET /data/health": {
        "use": "GET /data/status（2026-11-03 起 /data/health 也已作为别名可用，"
               "两条都通）",
        "why": "called 2026-11-02 15:25 and got a 404; the health route was /health, "
               "and the data layer's own status is /data/status",
    },
    "GET /profile?symbol=": {
        "use": "GET /symbol/{symbol}/profile",
        "why": "called 2026-11-02 15:41 and got a 404; the profile route takes the "
               "symbol as a path segment, not a query parameter",
    },
    "GET /knowledge?topic=": {
        "use": "GET /knowledge/{topic}",
        "why": "called 2026-11-02 15:45 and got a 404; the topic is a path segment",
    },
    "GET /quote?symbols=": {
        "use": "GET /market/quote?symbols=",
        "why": "called 2026-11-02 15:47 and got a 404; quotes live under /market",
    },
}

# Tables that exist in the account file but not in `state.DDL`. Nearly always a
# deleted feature whose rows were left behind, not a hand-written table: P14
# created `advice`, P15 (13333ee, 2026-10-26 10:57) deleted the module, the DDL,
# the four store methods and the routes, but never ran `DROP TABLE`. The last row
# was written at 10:24 that morning, 33 minutes before the feature died.
ORPHANS = {
    "advice": "P15 removed the DDL and all four store methods but never dropped "
              "the table; the last row (10:24) is a pending SET_BUDGET",
}


def _body_schema(op: dict) -> str:
    body = op.get("requestBody") or {}
    content = body.get("content") or {}
    for media, spec in content.items():
        if media.startswith("application/json"):
            schema = spec.get("schema") or {}
            ref = schema.get("$ref")
            if ref:
                return ref.rsplit("/", 1)[-1]
            return schema.get("type", "object")
    return ""


#: 约束按键的固定顺序输出：先是范围，再是长度/条数，最后正则与默认值。
#: 顺序固定是为了让 ``--check`` 的差异只反映真实变化，不随字典序抖动。
_CONSTRAINT_KEYS = ("gt", "ge", "lt", "le", "minLength", "maxLength",
                    "minItems", "maxItems", "minProperties", "maxProperties")


def _json_type(schema: dict) -> str:
    """一个字段的类型，写成调用者能照抄的短串。

    Pydantic 把 ``int | None`` 渲染成 ``anyOf: [integer, null]``。把联合类型
    原样写出来，是因为"这个字段能不能给 null"正是猜错就 422 的那类细节。
    """
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(_json_type(s) for s in schema["anyOf"])
    if "enum" in schema:
        return "enum(" + ", ".join(
            json.dumps(v, ensure_ascii=False) for v in schema["enum"]) + ")"
    kind = schema.get("type") or "?"
    if kind == "array":
        return f"{_json_type(schema.get('items') or {})}[]"
    if kind == "object" and isinstance(schema.get("additionalProperties"), dict):
        return f"object<string, {_json_type(schema['additionalProperties'])}>"
    fmt = schema.get("format")
    return f"{kind} ({fmt})" if fmt else kind


def _constraints(schema: dict) -> str:
    parts = [f"{key}={schema[key]}" for key in _CONSTRAINT_KEYS if key in schema]
    if "pattern" in schema:
        parts.append(f"pattern={schema['pattern']}")
    if "default" in schema:
        parts.append("default=" + json.dumps(schema["default"], ensure_ascii=False))
    # 单元格里的 `|` 会把这一行切成多列，后面每个值都错位一格——表格看起来
    # 还在，读起来全是错的。正则里出现 `|` 是常事。
    return ", ".join(parts).replace("|", "\\|")


def _field_doc(schema: dict) -> str:
    """字段说明，只保留"人写的"那部分。

    Pydantic 会自动把 ``title`` 填成 ``Client Request Id`` 这种字段名变体；
    把它也抄进文档只会撑宽表格。``description`` 为空、或与 ``title`` 相同时
    当作没有说明。
    """
    doc = (schema.get("description") or "").strip()
    title = (schema.get("title") or "").strip()
    if not doc or doc == title:
        return ""
    return " ".join(doc.split())


def _body_detail(op: dict, schemas: dict) -> tuple[str, bool, list[dict]]:
    """模型名、是否拒收未声明字段、字段清单（名/类型/必填/边界）。

    字段清单是这份文档以前缺的那一半：``body`` 列只写 ``ProposeBody``，
    而正文让调用者"只发 documented fields"——却没有任何地方列出字段。
    """
    name = _body_schema(op)
    model = schemas.get(name) or {}
    required = set(model.get("required") or ())
    fields = [
        {
            "name": field,
            "type": _json_type(schema),
            "required": field in required,
            "constraints": _constraints(schema),
            "doc": _field_doc(schema),
        }
        for field, schema in (model.get("properties") or {}).items()
    ]
    return name, model.get("additionalProperties") is False, fields


def _requires_token(op: dict) -> bool:
    """这条路由要不要 ``X-Quant-Token``。

    不从 ``op["security"]`` 判：``require_token`` 是以路由依赖挂上去的，
    OpenAPI 里没有对应的 security scheme，按 security 判会让 91 条路由**全部**
    标成"免鉴权"——而真实情况是只有 ``/health`` 是开的。

    它作为 ``Header`` 依赖出现在 ``parameters`` 里，那是公开、稳定的位置，
    也正是调用者要照抄的那个头。走 ``app.routes`` 的依赖树则要碰 FastAPI 的
    私有 ``_IncludedRouter``（一条 include 包一层），版本一换就静默归零。
    """
    return any(p.get("in") == "header"
               and p.get("name", "").lower() == "x-quant-token"
               for p in op.get("parameters") or ())


def _params(op: dict) -> str:
    items = []
    for p in op.get("parameters") or []:
        flag = "" if p.get("required") else "?"
        items.append(f"{p['name']}{flag}:{p.get('in')}")
    return ", ".join(items)


def collect() -> list[dict]:
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    rows: list[dict] = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            body, strict, fields = _body_detail(op, schemas)
            rows.append({
                "method": method.upper(),
                "path": path,
                "summary": (op.get("summary") or "").strip(),
                "params": _params(op),
                "body": body,
                "body_strict": strict,
                "fields": fields,
                "auth": _requires_token(op),
            })
    return rows


def _orphan_tables() -> dict[str, str]:
    """Tables present in the account file but absent from ``state.DDL``.

    A schema that only exists in the file is a schema nothing enforces: no
    migration, no guard, no audit row. Detected rather than hard-coded so the
    next hand-made table surfaces without an edit here.
    """
    import re
    import sqlite3

    from service.trading.state import DDL

    known = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", DDL))
    db = Path(__file__).resolve().parents[1] / "data" / "paper.sqlite"
    if not db.exists():
        return {}
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    found = {}
    for name in sorted(tables - known):
        if name.startswith(("sqlite_", "journal_fts")):
            continue
        found[name] = ORPHANS.get(name, "no DDL in state.py; written outside the API")
    return found


def _bodies_section(rows: list[dict]) -> list[str]:
    """请求体字段表：``body`` 列里的模型名到底收什么。

    没有这一节时，正文写着"只发 documented fields"，而字段清单在文档里一次
    都没出现过——调用者只能凭记忆发，拼错一个名字就是一个 422，看起来像
    "这个接口不存在"。
    """
    grouped: dict[str, dict] = {}
    for row in rows:
        name = row.get("body") or ""
        if not name or not row.get("fields"):
            continue
        entry = grouped.setdefault(name, {
            "routes": [], "fields": row["fields"], "strict": row["body_strict"]})
        entry["routes"].append(f"`{row['method']} {row['path']}`")
    if not grouped:
        return []

    lines = [
        "",
        "## Request bodies",
        "",
        "`body` 列只写了模型名，字段在这里展开。这张表由 `app.openapi()` 生成，",
        "不是手写：改一个字段、动一个边界，`--check` 就会红。",
        "",
    ]
    if all(entry["strict"] for entry in grouped.values()):
        lines += [
            '这些模型都是 `extra="forbid"`：**多一个未声明的字段就 422**，',
            "少一个必填字段也 422。`required=yes` 的行必须给值。",
            "",
        ]
    else:
        lines += [
            "标了「忽略多余字段」的模型不拒收未声明字段——拼错的字段不会报错，",
            "只会安静地不生效。",
            "",
        ]

    for name in sorted(grouped):
        entry = grouped[name]
        note = "" if entry["strict"] else "（忽略多余字段）"
        lines += [
            f"### `{name}`{note}",
            "",
            "用于 " + "、".join(sorted(entry["routes"])) + "。",
            "",
            "| field | type | required | constraints |",
            "|---|---|---|---|",
        ]
        for field in entry["fields"]:
            # 类型里的 `|`（`integer | null`）必须转义：表格按 `|` 切列，
            # 不转义就是把 4 列写成 5 列，后面每个值都错位一格。
            kind = field["type"].replace("|", "\\|")
            lines.append(
                f"| `{field['name']}` | {kind} | "
                f"{'yes' if field['required'] else 'no'} | "
                f"{field['constraints'] or '—'} |")
        # 说明单独列在表下，不塞进第五列：有的是 "600000.SH" 这种格式提示，
        # 有的是两百字的幂等规则——挤进单元格只会两样都读不清。
        notes = [f for f in entry["fields"] if f["doc"]]
        if notes:
            lines += ["", "字段说明：", ""]
            lines += [f"- `{f['name']}`：{f['doc']}" for f in notes]
        lines.append("")
    return lines


def render(rows: list[dict]) -> str:
    lines = [
        "# API contract",
        "",
        "Generated by `python scripts/api_contract.py --write` from `app.openapi()`.",
        "**Do not call an endpoint from memory.** If a path is not in the table below,",
        "it does not exist, and repeating it does not help: a miss is counted on its",
        "own path shape, and past `api.breaker_limit` hits inside",
        "`api.breaker_window` the answer is 429. 401/403/409/422 never count.",
        "",
        f"{len(rows)} routes.",
        "",
        "`auth=token` 表示要带口令，两种头任选其一：`X-Quant-Token: <token>` 或",
        "`Authorization: Bearer <token>`。`open` 只有 `/health` 与 `/`——前者是",
        "监控和人的入口，后者是看板首页，两者都不该要求口令。",
        "",
        "| method | path | auth | params | body | summary |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['method']}` | `{r['path']}` | "
            f"{'token' if r['auth'] else 'open'} | {r['params'] or ''} | "
            f"{('`' + r['body'] + '`') if r['body'] else ''} | {r['summary']} |")

    lines += _bodies_section(rows)

    lines += [
        "",
        "## Paths that do not exist (and what to use instead)",
        "",
        "Every row below has been called by a model at least once, returned 404,",
        "and been retried. They are listed so the next attempt stops here.",
        "",
        "| wrong | use | why |",
        "|---|---|---|",
    ]
    for wrong, fix in TRAPS.items():
        lines.append(f"| `{wrong}` | `{fix['use']}` | {fix['why']} |")

    lines += [
        "",
        "## 4xx means stop",
        "",
        "| status | meaning | what to do |",
        "|---|---|---|",
        "| 401 | missing or wrong `X-Quant-Token` | read `.env`, do not retry |",
        "| 404 | the path does not exist | look it up in the table above |",
        ("| 409 | the guard refused the decision | read `reasons`; changing the "
         "body will not help |"),
        ("| 422 | the body has fields the schema does not accept | send only the "
         "fields listed under Request bodies |"),
        "| 429 | this path failed 30+ times in 60s | stop, read `/openapi.json` |",
        "",
        "A rejected proposal is recorded, not erased: `tickets` keeps",
        "`status=\"rejected\"` and `decisions` gets a `reject` row. But it is not",
        "a live order — there is nothing to approve, and the same",
        "`client_request_id` keeps returning that refusal instead of running",
        "the guard again.",
        "",
    ]

    orphans = _orphan_tables()
    lines += [
        "## Tables outside the schema",
        "",
        "These exist in `data/paper.sqlite` but not in `state.DDL`. Almost always a",
        "deleted feature whose table was never dropped: the code is gone, the rows",
        "stayed. Nothing reads them, nothing expires them, and no guard applies —",
        "which is exactly why they are listed here instead of being ignored.",
        "",
    ]
    if orphans:
        lines += ["| table | why it is suspicious |", "|---|---|"]
        lines += [f"| `{name}` | {why} |" for name, why in orphans.items()]
    else:
        lines.append("None. Every table in the file is declared in `state.DDL`.")
    lines.append("")
    return "\n".join(lines)


def _diff(current: str, generated: str, limit: int = 40) -> list[str]:
    """``--check`` 失败时的差异清单（只给前若干行）。

    以前只说一句 "is out of date"：读者知道要重跑 ``--write``，却不知道
    **哪一条变了**。而真正要判断的恰恰是"这个变化是我想要的吗"。
    """
    lines = list(difflib.unified_diff(
        current.splitlines(), generated.splitlines(),
        fromfile=DOCS.name, tofile="generated", n=0, lineterm=""))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... 另有 {len(lines) - limit} 行差异"]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="print the live HTTP contract")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help=f"write {DOCS}")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the document is out of date (CI gate)")
    args = parser.parse_args(argv)

    rows = collect()
    if args.json:
        print(json.dumps({"routes": rows, "traps": TRAPS}, ensure_ascii=False, indent=1))
        return 0

    text = render(rows)
    if args.check:
        # The document is generated, so it is only useful if it is regenerated.
        # Without this gate it drifts silently: the table said 69 routes while
        # the app served 85, and thirteen routes — including the one the model
        # uses most (``/symbol/{symbol}/profile``) — were simply missing.
        current = DOCS.read_text(encoding="utf-8") if DOCS.exists() else ""
        if current != text:
            print(f"[api_contract] {DOCS} is out of date; "
                  f"run: python scripts/api_contract.py --write", file=sys.stderr)
            for line in _diff(current, text):
                print(line, file=sys.stderr)
            return 1
        print(f"[api_contract] {DOCS} is current ({len(rows)} routes)")
        return 0

    if args.write:
        DOCS.parent.mkdir(parents=True, exist_ok=True)
        DOCS.write_text(text, encoding="utf-8")
        print(f"[api_contract] {len(rows)} routes -> {DOCS}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
