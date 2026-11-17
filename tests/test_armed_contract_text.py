"""D-17: tool/contract prose must follow the armed-cancel behaviour.

``api_contract --check`` compares the route table and request-body schemas, not
the English/Chinese prose a model actually reads. On 2026-11-07 D-13 made both
``reject`` and ``cancel`` withdraw an armed intent, while three descriptions
still said cancel only touched QUEUED tickets and was not interchangeable with
reject — and the whole suite stayed green. These are static guards against that
class of drift, modelled on ``test_phase3_contract_text.py``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_JS = ROOT / "plugin" / "lib" / "index.js"
SKILL_MD = ROOT / "plugin" / "skills" / "quant-desk" / "SKILL.md"
TRADING_API = ROOT / "service" / "api" / "trading.py"
DECISIONS_PY = ROOT / "service" / "trading" / "decisions.py"
CONTRACT_SCRIPT = ROOT / "scripts" / "api_contract.py"
CONTRACT_DOC = ROOT / "docs" / "API_CONTRACT.md"

#: Every place prose is read by a model, a caller or a maintainer. A second
#: full-repo scan on 2026-11-07 found three more stale lines that the first
#: (four-file) guard missed — SKILL.md, decisions.cancel and the 4xx note.
SCANNED = (INDEX_JS, SKILL_MD, TRADING_API, DECISIONS_PY,
           CONTRACT_SCRIPT, CONTRACT_DOC)

OLD_WORDINGS = (
    "they are not interchangeable",
    "Withdraw a queued order (not a proposal",
    "'Cancel a QUEUED but not-yet-executed ticket (withdraw its remaining quantity). '",
    "`frame_id` 只在当天可回放",
    "Withdraw a queued *order* (not a proposal). Thin over the book.",
    "It does not appear in `tickets`",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cancel_tool_description() -> str:
    """Just the quant_cancel description block, not the whole 2k-line file."""
    text = _text(INDEX_JS)
    start = text.index("      'quant_cancel',")
    end = text.index("      {", start)
    return text[start:end]


def test_no_old_wording_survives_anywhere():
    for path in SCANNED:
        content = _text(path)
        for old in OLD_WORDINGS:
            assert old not in content, f"{old!r} still in {path.name}"


def test_plugin_cancel_tool_names_the_armed_intent():
    description = _cancel_tool_description()
    assert "ARMED intent" in description
    assert 'status="cancelled"' in description
    assert "no money spent" in description


def test_api_docstrings_describe_both_armed_entry_points():
    text = _text(TRADING_API)
    cancel_doc = text[text.index('def cancel(ticket_id'):text.index('def cancel(ticket_id') + 600]
    reject_doc = text[text.index('def reject(ticket_id'):text.index('def reject(ticket_id') + 600]
    assert "armed" in cancel_doc and "both" in cancel_doc
    assert "armed" in reject_doc and "rejected=False" in reject_doc


def test_generated_contract_document_explains_the_lifecycle():
    text = _text(CONTRACT_DOC)
    assert "armed 票可用 reject 或 cancel 主动撤销" in text
    assert "both routes set status=cancelled" in text
    assert "expired_untriggered" in text


def test_trap_row_source_matches_the_generated_document():
    """The prose lives in the generator; changing only the .md would be lost."""
    script = _text(CONTRACT_SCRIPT)
    doc = _text(CONTRACT_DOC)
    assert "cancel withdraws a QUEUED order (and an ARMED intent)" in script
    assert "cancel withdraws a QUEUED order (and an ARMED intent)" in doc


def test_skill_doc_teaches_cross_day_frame_replay():
    """P1 added rt_archive + as_of/frame_id; the old line told models not to use it."""
    skill = _text(SKILL_MD)
    assert "rt_archive/<day>/" in skill
    assert "`as_of`" in skill and "`frame_id`" in skill
    assert "永久不可回补" in skill
    assert "account_is_historical=false" in skill


def test_internal_cancel_docstring_matches_the_implementation():
    text = _text(DECISIONS_PY)
    start = text.index("def cancel(self, ticket_id")
    doc = text[start:start + 900]
    assert "armed intent" in doc
    assert "cancel_armed_ticket" in doc


def test_contract_no_longer_claims_rejections_are_unrecorded():
    """``decisions._reject`` does save_ticket + log_decision; say so."""
    for path in (CONTRACT_SCRIPT, CONTRACT_DOC):
        text = _text(path)
        assert "recorded, not erased" in text
        assert "gets a `reject` row" in text
