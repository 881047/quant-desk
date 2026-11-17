"""清点代码里的数字字面量，按"是否属于领域参数"分类。

为什么需要这个脚本：靠 grep 找魔法数字会同时漏掉（写成 float(...) 的、
放在常量里的）和误报（range(3)、切片 [0]、索引 -1）。AST 能拿到
"这个数字出现在哪个函数、哪一行、是不是在比较/赋值里"，才谈得上判断
它是不是一个应该被配置化的领域参数。

分类口径：
  unit      单位换算（100 股/手、10000 元/万、1e8）——不是硬编码，是单位定义
  eps       浮点比较容差（1e-6、0.001）——属于实现细节，但应该集中一处
  policy    策略/风控阈值（0.38、0.60、3 连亏）——应该进配置
  stat      统计显著性阈值（0.05、1.96、0.02425）——应该进配置
  structural 索引、range、维度、切片——不动
  other     需要人看的
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 单位换算的事实值。写在这里是为了让审计脚本认识它们，不是让业务代码引用。
UNIT_VALUES = {100, 1000, 10000, 1e6, 1e8, 60, 365, 12, 52, 24}

STAT_VALUES = {0.05, 0.01, 0.025, 0.02425, 1.96, 1.645, 2.58, 1.28, 0.95, 0.99, 0.90, 0.975}

POLICY_HINTS = (
    "gate", "threshold", "limit", "max_", "min_", "ratio", "weight", "cap",
    "penalty", "buffer", "band", "floor", "ceiling", "cutoff", "window",
    "loss", "stop", "pause", "trip", "ok", "warn", "bad", "strong", "weak",
)

EPS_VALUES = {1e-6, 1e-9, 1e-8, 1e-4, 0.001, 1e-5, 1e-12}


def classify(value: float, context: str, parent_name: str | None) -> str:
    abs_v = abs(value)
    if abs_v in EPS_VALUES:
        return "eps"
    if value in UNIT_VALUES:
        return "unit"
    if value in STAT_VALUES:
        return "stat"
    name = (parent_name or "").lower()
    ctx = context.lower()
    if any(h in name or h in ctx for h in POLICY_HINTS):
        return "policy"
    if isinstance(value, int) and abs_v <= 10:
        return "structural"
    return "other"


class Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.stack: list[str] = []
        self.assignment_targets: dict[int, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant):
                        self.assignment_targets[id(sub)] = target.id
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            for sub in ast.walk(node.value) if node.value else []:
                if isinstance(sub, ast.Constant):
                    self.assignment_targets[id(sub)] = node.target.id
        self.generic_visit(node)

    def _visit_func(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            return
        # 只关心带小数的、或者绝对值够大的整数
        if isinstance(node.value, int) and abs(node.value) < 11 and node.value in (0, 1, 2, -1):
            return
        target = self.assignment_targets.get(id(node))
        kind = classify(node.value, " ".join(self.stack), target)
        if kind == "structural":
            return
        self.rows.append({
            "file": self.filename,
            "line": node.lineno,
            "value": node.value,
            "kind": kind,
            "func": ".".join(self.stack) or "<module>",
            "name": target,
        })
        self.generic_visit(node)


def scan(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"skip {path}: {exc}", file=sys.stderr)
        return []
    collector = Collector()
    collector.filename = str(path.relative_to(ROOT)).replace("\\", "/")
    collector.visit(tree)
    return collector.rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["service", "scripts"])
    parser.add_argument("--kind", help="只看某一类")
    parser.add_argument("--file", help="只看某个文件")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    rows: list[dict] = []
    for raw in args.paths or ["service", "scripts"]:
        target = ROOT / raw
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in files:
            if "__pycache__" in str(path):
                continue
            rows.extend(scan(path))

    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.file:
        rows = [r for r in rows if args.file in r["file"]]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    counts = Counter(r["kind"] for r in rows)
    by_file: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_file[r["file"]][r["kind"]] += 1

    print(f"共 {len(rows)} 个非结构字面量，分类：{dict(counts)}\n")
    if args.summary:
        order = sorted(by_file.items(), key=lambda kv: -sum(kv[1].values()))
        print(f"{'file':46} {'policy':>6} {'stat':>5} {'eps':>5} {'unit':>5} {'other':>6}")
        for name, c in order:
            total = sum(c.values())
            if total < 3:
                continue
            print(f"{name:46} {c['policy']:6} {c['stat']:5} {c['eps']:5} {c['unit']:5} {c['other']:6}")
        return 0

    for r in rows:
        name = f" -> {r['name']}" if r["name"] else ""
        print(f"{r['file']}:{r['line']:<5} {r['kind']:8} {r['value']!r:>12}  {r['func']}{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
