"""Migrate current quant-desk instructions from legacy tool names to four facades.

This is deliberately limited to living instructions. It never rewrites source,
reports, inventories, ledgers or frozen short-alpha records. The transformation is
idempotent: newly emitted facade names are not legacy names and are left alone.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "reports/20261115-quant-tool-merger-map.json"


def targets() -> list[Path]:
    files = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "plugin/README.md",
        ROOT / "plugin/mcp/README.md",
        ROOT / "plugin/skills/quant-desk/SKILL.md",
        ROOT / "docs/12-automation-chain.md",
        ROOT / "docs/13-short-alpha-paper-execution.md",
    ]
    files.extend(sorted((ROOT / "docs/automations").glob("*.md")))
    return [path for path in files if path.is_file()]


def mapping() -> tuple[dict[str, str], dict[str, tuple[str, str]], dict[str, str], set[str]]:
    rows = json.loads(MAP_PATH.read_text(encoding="utf-8"))["mapping"]
    reads: dict[str, str] = {}
    orders: dict[str, tuple[str, str]] = {}
    workflows: dict[str, str] = {}
    retired: set[str] = set()
    for row in rows:
        name = row["old_tool"]
        for target in row["new_targets"]:
            if target == "retired":
                retired.add(name)
            elif target.startswith("quant_query."):
                reads[name] = target.split(".", 1)[1]
            elif target.startswith("quant_order."):
                _, book, action = target.split(".")
                orders[name] = (book, action)
            elif target.startswith("quant_workflow.") and "|" not in target:
                workflows[name] = target.split(".", 1)[1]
    for name in ("quant_hypothesis", "quant_memo", "quant_declined"):
        reads.pop(name, None)
    return reads, orders, workflows, retired


def split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def without_action(inner: str) -> tuple[str, str | None]:
    action: str | None = None
    kept: list[str] = []
    for part in split_top_level(inner):
        match = re.match(r"^(\s*)action\s*=\s*['\"]?([a-z_]+)['\"]?\s*$", part, re.S)
        if match:
            action = match.group(2)
        else:
            kept.append(part)
    return ",".join(kept).strip(), action


def closing_parenthesis(text: str, start: int) -> int:
    depth = 1
    quote: str | None = None
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def emit(tool: str, selector: str, operation: str, inner: str, called: bool) -> str:
    if called:
        return f'{tool}({selector}="{operation}", params={{{inner}}})'
    return f'{tool}({selector}="{operation}")'


def migrate(text: str) -> tuple[str, int]:
    reads, orders, workflows, retired = mapping()
    conditional = {"quant_hypothesis", "quant_memo", "quant_declined", "quant_alert_handoff"}
    names = sorted(set(reads) | set(orders) | set(workflows) | conditional | retired, key=len, reverse=True)
    pattern = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_])")
    output: list[str] = []
    cursor = 0
    replacements = 0
    for match in pattern.finditer(text):
        name = match.group(1)
        if name in retired:
            continue
        open_paren = match.end()
        while open_paren < len(text) and text[open_paren].isspace():
            open_paren += 1
        called = open_paren < len(text) and text[open_paren] == "("
        end = match.end()
        inner = ""
        if called:
            close = closing_parenthesis(text, open_paren)
            if close < 0:
                called = False
            else:
                inner = text[open_paren + 1:close].strip()
                end = close + 1
        output.append(text[cursor:match.start()])
        if name in reads:
            replacement = emit("quant_query", "resource", reads[name], inner, called)
        elif name in orders:
            book, action = orders[name]
            replacement = (
                f'quant_order(book="{book}", action="{action}", params={{{inner}}})'
                if called else f'quant_order(book="{book}", action="{action}")'
            )
        elif name in workflows:
            replacement = emit("quant_workflow", "operation", workflows[name], inner, called)
        elif name == "quant_alert_handoff":
            action = "answer" if re.search(r"\b(id|analysis)\s*=", inner) else "list"
            replacement = emit("quant_alert", "action", action, inner, called)
        else:
            cleaned, action = without_action(inner)
            if name == "quant_hypothesis":
                if action in {"add", "result"}:
                    replacement = emit("quant_workflow", "operation", f"hypothesis_{action}", cleaned, called)
                else:
                    replacement = emit("quant_query", "resource", "hypothesis", cleaned, called)
            elif name == "quant_memo":
                if action == "score":
                    replacement = emit("quant_workflow", "operation", "memo_score", cleaned, called)
                elif action == "add" or (not called and not action):
                    replacement = emit("quant_workflow", "operation", "memo_add", cleaned, called)
                else:
                    replacement = emit("quant_query", "resource", "memo", cleaned, called)
            elif name == "quant_declined":
                if action in {"add", "score"}:
                    replacement = emit("quant_workflow", "operation", f"declined_{action}", cleaned, called)
                else:
                    replacement = emit("quant_query", "resource", "declined", cleaned, called)
            else:
                raise AssertionError(name)
        output.append(replacement)
        cursor = end
        replacements += 1
    output.append(text[cursor:])
    return "".join(output), replacements


def repair_examples(text: str) -> str:
    """Repair shorthand that was meaningful in prose but not in the old schema."""
    replacements = {
        'resource="short_alpha"': 'resource="short_alpha_legacy"',
        'resource="quote", params={symbol}': 'resource="quote", params={symbols=symbol}',
        'resource="quote", params={1-2 只}': 'resource="quote", params={symbols=选中的1-2只}',
        'resource="knowledge", params={ledger}': 'resource="knowledge", params={topic=ledger}',
        'resource="compare", params={两个}': 'resource="compare", params={symbols=两个标的}',
        'params={SELL}': 'params={side=SELL}',
        ',qty省略表示清仓': '',
        ', qty 省略表示清仓': '',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    moves = [
        ("hypothesis", "add", "hypothesis_add"),
        ("hypothesis", "result", "hypothesis_result"),
        ("memo", "add", "memo_add"),
        ("memo", "score", "memo_score"),
        ("declined", "add", "declined_add"),
        ("declined", "score", "declined_score"),
    ]
    for resource, action, operation in moves:
        pattern = re.compile(
            rf'quant_query\(resource="{resource}", params=\{{\s*action\s*=\s*["\']?{action}["\']?\s*,?([^}}]*)\}}\)'
        )
        text = pattern.sub(
            lambda match: f'quant_workflow(operation="{operation}", params={{{match.group(1).strip()}}})',
            text,
        )
    return text


def normalize_discovery(text: str) -> str:
    """ToolSearch takes tool names, never facade resource expressions."""
    text = repair_examples(text)
    lines = []
    order = ["quant_query", "quant_order", "quant_workflow", "quant_alert"]
    for line in text.splitlines(keepends=True):
        if "ToolSearch" not in line or "quant_" not in line:
            lines.append(line)
            continue
        names = [name for name in order if name in line]
        if "ToolSearch(" in line:
            start = line.index("ToolSearch(")
            end = line.rfind(")")
            if end > start and names:
                call = "ToolSearch([" + ", ".join(json.dumps(name) for name in names) + "])"
                line = line[:start] + call + line[end + 1:]
        elif "载入" in line and names:
            ending = "\n" if line.endswith("\n") else ""
            prefix = line.split("载入", 1)[0]
            line = prefix + "载入 " + "、".join(names) + "。" + ending
        lines.append(line)
    return "".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="apply changes; otherwise print a dry-run summary")
    args = parser.parse_args()
    summary = []
    for path in targets():
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        changed, count = migrate(text)
        changed = normalize_discovery(changed)
        if changed != text:
            summary.append({"path": str(path.relative_to(ROOT)), "replacements": count})
            if args.write:
                path.write_bytes(changed.encode("utf-8"))
    print(json.dumps({"write": args.write, "files": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
