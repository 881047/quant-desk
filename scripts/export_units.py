"""把 ``service/vocabulary.py`` 的数值量纲导出给 Node 消费方。

Python 是唯一的手写源；``web/assets/js/contract.units.js`` 与
``plugin/lib/contract.units.js`` 都是生成物，禁止手改。两边各自手写规则时，
``plugin/lib/index.js`` 里那句"profile 里带 _pct 的是百分数"与后来登记的
``daily_pnl_pct`` 小数例外直接冲突（2026-11-08 复查）——生成物 + `--check`
让这种分叉在测试里就红，而不是等一个 100 倍的数字出现在界面上。

用法::

    python scripts/export_units.py --write   # 改词表后重新生成
    python scripts/export_units.py --check   # CI / pytest：过期则退出 1

确定性：``json.dumps(sort_keys=True)`` + 固定缩进 + 固定尾换行，同一份词表在
任何机器上生成字节相同。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直接 ``python scripts/export_units.py`` 时 sys.path[0] 是 scripts/，
# 不含项目根；与 ``scripts/api_contract.py`` 不同，这里不能假设 -m 调用。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service import vocabulary as vocab  # noqa: E402
TARGETS = (
    ROOT / "web/assets/js/contract.units.js",
    ROOT / "plugin/lib/contract.units.js",
)


def render() -> str:
    conv = vocab.UNIT_CONVENTIONS
    payload = {
        "rule": conv["rule"],
        "by_endpoint": conv["by_endpoint"],
        "naming_exceptions": conv["naming_exceptions"],
        "units_text": vocab.units_text(),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    # 中文说明里有反引号和 `${` 吗？没有；用 JSON 字符串而不是模板字面量，
    # 是为了让任何字符都不需要转义，生成物永远是合法 ESM。
    text = vocab.units_text()
    return (
        "/* GENERATED FILE — DO NOT EDIT.\n"
        " * Run: .venv/Scripts/python.exe scripts/export_units.py --write\n"
        " * Source of truth: service/vocabulary.py (UNIT_CONVENTIONS).\n"
        " * scripts/export_units.py --check fails if this file is stale.\n"
        " */\n\n"
        f"export const UNITS = {body};\n\n"
        f"export const UNITS_TEXT = {json.dumps(text, ensure_ascii=False)};\n\n"
        "export function unitsFor(endpoint) {\n"
        "  const fields = UNITS.by_endpoint[endpoint] || {};\n"
        "  const out = {};\n"
        "  for (const [key, scale] of Object.entries(fields)) {\n"
        "    out[key.split('.').pop()] = scale;\n"
        "  }\n"
        "  if (endpoint === '/market/pools') {\n"
        "    for (const [key, scale] of Object.entries(UNITS.by_endpoint['/market/pools.sentiment'])) {\n"
        "      out[key.split('.').pop()] = scale;\n"
        "    }\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate both files")
    mode.add_argument("--check", action="store_true", help="fail if either file is stale")
    args = ap.parse_args()

    content = render()
    stale = [str(p.relative_to(ROOT)) for p in TARGETS
             if not p.exists() or p.read_text(encoding="utf-8") != content]
    if args.write:
        for p in TARGETS:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        print("wrote:", ", ".join(str(p.relative_to(ROOT)) for p in TARGETS))
        return 0
    if stale:
        print("STALE generated contract files:", ", ".join(stale))
        print("run: .venv/Scripts/python.exe scripts/export_units.py --write")
        return 1
    print("contract units in sync:", len(TARGETS), "files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
