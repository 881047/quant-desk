"""Turn one stock write-up into a falsifiable memo the Brier ledger can score.

A research report that is never written down as a prediction cannot be wrong,
which also means it cannot be calibrated. This CLI is the small bridge: every
time a per-name write-up is produced, record one mechanically resolvable claim
(the scorer takes the anchor close from the memo date itself) and a falsifier.

    python scripts/log_thesis_memo.py --symbol 002185.SZ \
        --topic "..." --thesis "..." --falsifier "..." \
        --probability 0.5 --horizon 5 --dry-run

A probability of 0.5 is a *climatology baseline*, not a view. It deliberately
contributes 0.25 to Brier whatever happens, so registering a process cannot
make the calibration look better than it is.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8811"


def _token() -> str:
    cfg = Path.home() / ".workbuddy" / "mcp.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return data["mcpServers"]["quant-desk"]["env"]["QUANT_TOKEN"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="登记一条可结算的个股研报断言")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--thesis", required=True)
    ap.add_argument("--mechanism", default="")
    ap.add_argument("--falsifier", default="")
    ap.add_argument("--confidence", type=float, default=0.5)
    ap.add_argument("--probability", type=float, default=0.5)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--claim", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    claim = args.claim or (f"{args.symbol} {args.horizon} 个交易日后收盘价高于"
                          "本 memo 锚定日收盘价")
    body = {
        "topic": args.topic,
        "thesis": args.thesis,
        "mechanism": args.mechanism,
        "falsifier": args.falsifier,
        "confidence": args.confidence,
        "predictions": [{
            "claim": claim,
            "kind": "symbol_up",
            "symbol": args.symbol,
            "horizon": args.horizon,
            "probability": args.probability,
        }],
    }
    if args.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    req = urllib.request.Request(
        BASE + "/memo", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"X-Quant-Token": _token(), "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        out = json.load(response)
    print(f"[memo] id={out.get('id')} status={(out.get('memo') or {}).get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
