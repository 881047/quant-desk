"""覆盖率的闸：分层设阈值，不看总数。

为什么不用一个总阈值：2026-11-02 的审查发现总覆盖率 85% 看着挺好，而真正
被模型调用的那一层——``service/api/``——只有 64%，71 个端点里 49 个有未覆盖
分支。总数字被内核与研究层的高覆盖拉起来了，接口层的洞藏在里面。

所以这里按层设阈值：

* ``service/api/`` 不低于 80%——它是模型的唯一入口；
* ``service/kernel/`` 不低于 90%——钱的算法在这里；
* 全仓不低于 85%。

    python scripts/coverage_gate.py            # 跑测试 + 出报告 + 判阈值
    python scripts/coverage_gate.py --report   # 只出报告（不给结论）

``coverage`` 没装就说清楚没装，而不是假装通过。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (包前缀, 最低覆盖率)。``None`` 的前缀表示整个 service。
THRESHOLDS: list[tuple[str, float]] = [
    ("service/api/", 0.80),
    ("service/kernel/", 0.90),
    ("service/", 0.85),
]


def _have_coverage() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import coverage"],
        capture_output=True, cwd=ROOT, check=False,
    ).returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the suite under coverage and check thresholds")
    parser.add_argument("--report", action="store_true",
                        help="print the report but do not enforce the thresholds")
    args = parser.parse_args(argv)

    if not _have_coverage():
        print("[coverage_gate] coverage 没装："
              "pip install 'quant-desk[dev]' 或 uv pip install coverage",
              file=sys.stderr)
        return 2

    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", "tests", "-q",
         "--tb=short", "-p", "no:cacheprovider"],
        cwd=ROOT, check=False,
    )
    if run.returncode != 0:
        print("[coverage_gate] 测试没全绿，先修测试", file=sys.stderr)
        return run.returncode

    subprocess.run([sys.executable, "-m", "coverage", "report", "-m"],
                   cwd=ROOT, check=False)

    if args.report:
        return 0

    data = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if data.returncode != 0:
        print("[coverage_gate] 读不到覆盖率数据", file=sys.stderr)
        return 2

    import json

    payload = json.loads(data.stdout)
    files = {k.replace("\\", "/"): v for k, v in payload.get("files", {}).items()}

    failures: list[str] = []
    for prefix, floor in THRESHOLDS:
        group = {k: v for k, v in files.items() if k.startswith(prefix)}
        if not group:
            continue
        total = sum(v["summary"]["num_statements"] for v in group.values())
        covered = sum(v["summary"]["covered_lines"] for v in group.values())
        ratio = covered / total if total else 1.0
        mark = "ok " if ratio >= floor else "低 "
        print(f"[coverage_gate] {mark} {prefix:<18} {ratio:6.1%} (门槛 {floor:.0%})")
        if ratio < floor:
            failures.append(f"{prefix} {ratio:.1%} < {floor:.0%}")

    if failures:
        print("[coverage_gate] 未达门槛：" + "; ".join(failures), file=sys.stderr)
        return 1
    print("[coverage_gate] 全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
