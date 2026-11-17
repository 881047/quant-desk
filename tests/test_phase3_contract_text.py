"""F1 regression: the contract text must describe the autostop switch that exists.

Before this fix nine places said the desk would never sell for you, while
``configs/params.yaml:136`` had ``autostop.enabled: true`` and the capture loop
really did execute the stop written on the entry ticket. A reader who believed
the docs would not look for those orders. This case is a static guard: every
old wording must be gone, the switch and its confirmation window must be named
where the model reads, and the TRAPS entry must survive with an updated ``why``.

It is red on ``000aba3`` because all of those old strings were present and none
of the new ones were.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

INDEX_JS = ROOT / "plugin" / "lib" / "index.js"
CONTRACT_DOC = ROOT / "docs" / "API_CONTRACT.md"

#: Exact fragments removed by the F1 edit. Scoped to the five target files;
#: deliberately does not include the unrelated "不会自愈" comment in duty.py:296,
#: which is about priority ordering, not about stops.
OLD_WORDINGS = (
    "卖不卖由你决定",
    "系统不替你卖",
    "不会替你卖",
    "没有代码写的止损",
    "明确不替你卖",
    "而且不会自愈",
    "model places the sell",
    "runs before any new decision",
    "records who sold and why",
    "selling is a decision",
    "P15 removed automatic stop enforcement",
)


def _prompt_text() -> str:
    """Just the exported PROMPT template literal, not the rest of index.js."""
    lines = INDEX_JS.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("export const PROMPT"))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].rstrip().endswith("`"))
    return "\n".join(lines[start:end + 1])


def test_contract_text_matches_autostop_switch():
    from api_contract import TRAPS

    prompt = _prompt_text()
    # The two knobs the model can actually change must be visible in the PROMPT.
    assert "autostop.enabled" in prompt
    assert "autostop.confirm_seconds" in prompt
    assert "/decision/propose + approve" in prompt
    assert "新判断" in prompt
    for phrase in ("不会替你卖", "没有代码写的止损"):
        assert phrase not in prompt, f"PROMPT 仍写着旧措辞：{phrase}"

    targets = {
        "AGENTS.md": ROOT / "AGENTS.md",
        "scripts/stop_check.py": ROOT / "scripts" / "stop_check.py",
        "service/trading/stops.py": ROOT / "service" / "trading" / "stops.py",
        "service/trading/duty.py": ROOT / "service" / "trading" / "duty.py",
        "service/kernel/models.py": ROOT / "service" / "kernel" / "models.py",
        # F1-2: the ST docstring is a test file, but it is part of the same
        # contract text. Guard it here so the wording cannot fork again.
        "tests/kernel/test_st_sell_allowed.py": (
            ROOT / "tests" / "kernel" / "test_st_sell_allowed.py"),
    }
    for label, path in targets.items():
        text = path.read_text(encoding="utf-8")
        for phrase in OLD_WORDINGS:
            assert phrase not in text, f"{label} 仍残留旧措辞：{phrase}"
        assert ("autostop.enabled" in text
                or "autostop.confirm_seconds" in text), (
            f"{label} 没有写清自动止损的开关 / 确认窗")

    # The TRAPS entry is a *map of guessed-wrong paths*, not a claim that the
    # feature is gone. It must stay, and its ``why`` must describe the loop.
    trap = TRAPS["POST /risk/stops/enforce"]
    assert trap["use"] == "POST /decision/propose with side=SELL"
    assert "autostop.confirm_seconds" in trap["why"]
    assert "autostop.enabled" in trap["why"]
    assert "P15 removed automatic stop enforcement; selling is a decision" \
        not in trap["why"]

    doc = CONTRACT_DOC.read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines()
               if "`POST /risk/stops/enforce`" in line)
    assert "autostop.confirm_seconds" in row, "生成文档的 why 没跟上 api_contract.py"
    assert "autostop.enabled" in row
    assert "P15 removed automatic stop enforcement; selling is a decision" \
        not in row
