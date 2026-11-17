"""How many bytes and tokens a tool call actually hands the model.

Every tool answer competes for the same context window, and the desk has fifty
of them. This measures what each one costs on a real day, so two questions can be
answered with numbers instead of intuition:

* which call is the expensive one (and whether its payload earns the space);
* what a full decision chain costs, versus the context the model has to work in.

Token estimate is deliberately crude: one token per CJK character, one per four
ASCII characters. It over-counts Chinese JSON slightly and under-counts numbers,
which is the right direction for a budget — being wrong here should make the
estimate conservative, not flattering.

    python scripts/tool_bandwidth.py
    python scripts/tool_bandwidth.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = os.getenv("QUANT_BASE", "http://127.0.0.1:8811")
DAY = "20261028"

QUOTE_20_SYMBOLS = [
    "600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "600036.SH",
    "000858.SZ", "002594.SZ", "601012.SH", "600030.SH", "000333.SZ",
    "600276.SH", "601888.SH", "002415.SZ", "600887.SH", "601166.SH",
    "000651.SZ", "600009.SH", "601398.SH", "600028.SH", "601668.SH",
]

# (label, path, what it is for). Ordered by the order a session would call them.
CALLS: list[tuple[str, str, str]] = [
    ("context_pack", "/context_pack", "开工第一枪：账户/风控/候选/情绪"),
    ("observations", "/observations", "约束、偏离、风险预算"),
    ("duty", "/duty", "待办"),
    ("discover", "/discover", "大势：宽度/波动/状态桶/涨停结构"),
    ("regime", "/regime", "regime 标签"),
    ("market_status", "/market/status", "有没有开盘"),
    # 带插件实际传的 limit：不带的话测的是服务端默认 30 行（20 KB），
    # 而模型通过工具只会拿到 15 行（11 KB）。测的不是同一件事。
    ("market_pools", "/market/pools?limit=15", "三池 + 情绪面板（插件默认行数）"),
    ("limit_structure", "/knowledge/limit_structure", "封板率/连板/晋级"),
    ("quote_1", "/market/quote?symbols=600519.SH", "单只报价"),
    ("quote_20", f"/market/quote?symbols={','.join(QUOTE_20_SYMBOLS)}",
     "20 只报价（工具上限）"),
    ("bars_60", "/market/bars?symbol=600519.SH&limit=60", "60 根日线"),
    ("profile", "/symbol/600519.SH/profile", "单票九块档案"),
    ("liquidity", "/knowledge/liquidity?symbol=600519.SH", "冲击成本与容量"),
    ("analog", "/knowledge/analog?symbol=600519.SH", "历史类比 300 次"),
    ("universe", "/universe", "可交易全集"),
    ("screen", "/research/screen", "选股结果"),
    ("exposure", "/risk/exposure", "敞口与集中度"),
    ("risk", "/risk", "风控状态"),
    ("portfolio", "/portfolio", "持仓明细"),
    ("account", "/account", "账本"),
    ("core", "/core", "核心盘规则权重"),
    ("tickets", "/tickets", "票据"),
    ("cases", "/cases", "已平仓案例"),
    ("attribution", "/attribution", "策略归因"),
    ("strategies", "/strategies", "策略清单"),
    ("decay", "/decay", "衰减监控"),
    ("playbook", "/playbook", "手册"),
    ("arena", "/arena", "擂台"),
    ("persona", "/persona", "人格"),
    ("watch", "/watch", "自选"),
    ("memo", "/memo", "备忘录"),
    ("journal", "/journal", "日记"),
    ("data_brief", "/data/brief", "数据能不能用"),
    ("data_information", "/data/information", "信息完整度"),
]


# The chains a session actually walks, so "how much does one decision cost" has
# an answer instead of a range. Keys must exist in CALLS.
CHAINS: dict[str, tuple[str, ...]] = {
    "最小开工": ("context_pack", "observations", "duty", "discover"),
    "研究一票": ("context_pack", "observations", "discover", "profile",
                 "analog", "liquidity", "bars_60"),
    "完整决策": ("context_pack", "observations", "duty", "discover", "limit_structure",
                 "screen", "quote_20", "risk", "exposure", "tickets"),
    "决策+复盘": ("context_pack", "observations", "duty", "discover", "limit_structure",
                  "screen", "quote_20", "risk", "exposure", "tickets",
                  "journal", "memo", "cases"),
}


def fixed_overhead() -> dict | None:
    """Tool schemas and the prompt, measured by the Node side that owns them.

    Fifty definitions ride along with every request and never appear in a
    per-call byte count, so a budget that only measures responses overstates the
    room left for thinking.
    """
    script = ROOT / "plugin" / "tools_size.mjs"
    if not script.exists():
        return None
    try:
        out = subprocess.run(["node", str(script), "--json"],
                             check=False, capture_output=True, text=True, timeout=60, cwd=ROOT)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def token_estimate(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max(0, (len(text) - cjk)) // 4


def rows_in(payload) -> int:
    """Deepest-array width, to tell 'large because verbose' from 'large because many'."""
    best = 0
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            best = max(best, len(node))
            stack.extend(node[:50])
        elif isinstance(node, dict):
            stack.extend(node.values())
    return best


def fetch(path: str, token: str, timeout: float = 120.0) -> tuple[int, bytes, float]:
    req = urllib.request.Request(BASE + path, headers={"X-Quant-Token": token})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.time() - started
    except Exception as exc:  # noqa: BLE001 - a dead service is a result too
        return 0, str(exc).encode(), time.time() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure per-tool payload size")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--token", default=os.getenv("QUANT_TOKEN", ""))
    args = parser.parse_args(argv)

    token = args.token or _token_from_env()
    if not token:
        print("error: QUANT_TOKEN missing", file=sys.stderr)
        return 2

    out = []
    for label, path, purpose in CALLS:
        status, body, elapsed = fetch(path, token)
        text = body.decode("utf-8", "replace")
        rows = 0
        try:
            rows = rows_in(json.loads(text))
        except ValueError:
            pass
        out.append({
            "tool": label,
            "path": path,
            "purpose": purpose,
            "status": status,
            "bytes": len(body),
            "tokens": token_estimate(text),
            "rows": rows,
            "seconds": round(elapsed, 2),
            "bytes_per_row": round(len(body) / rows, 1) if rows else None,
        })

    total_bytes = sum(r["bytes"] for r in out)
    total_tokens = sum(r["tokens"] for r in out)
    ok = [r for r in out if r["status"] == 200]

    if args.json:
        print(json.dumps({"calls": out, "total_bytes": total_bytes,
                          "total_tokens": total_tokens}, ensure_ascii=False, indent=1))
        return 0

    print(f"{'tool':<20}{'状态':>4}{'字节':>9}{'≈token':>8}{'最深行数':>9}{'字节/行':>9}{'秒':>6}")
    print("-" * 68)
    for r in sorted(out, key=lambda x: -x["bytes"]):
        bpr = "-" if r["bytes_per_row"] is None else f"{r['bytes_per_row']:.0f}"
        print(f"{r['tool']:<20}{r['status']:>4}{r['bytes']:>9,}{r['tokens']:>8,}"
              f"{r['rows']:>9}{bpr:>9}{r['seconds']:>6.1f}")
    print("-" * 68)
    print(f"全部 {len(out)} 个调用：{total_bytes:,} 字节 ≈ {total_tokens:,} token，"
          f"其中成功 {len(ok)} 个")
    print(f"最大的单个：{max(out, key=lambda r: r['bytes'])['tool']}")

    by_tool = {r["tool"]: r for r in out}
    print()
    print("一次会话的上下文预算")
    print("-" * 68)
    fixed = fixed_overhead()
    fixed_tokens = 0
    if fixed:
        fixed_tokens = int(fixed.get("fixed_tokens") or 0)
        print(f"固定开销（{fixed['tools']} 个工具定义 + 提示词）"
              f"  ≈ {fixed_tokens:,} token")
    else:
        print("固定开销：跳过（未找到 node 或 plugin/tools_size.mjs）")
    for label, names in CHAINS.items():
        known = [n for n in names if n in by_tool]
        cost = sum(by_tool[n]["tokens"] for n in known)
        seconds = sum(by_tool[n]["seconds"] for n in known)
        print(f"  {label:<8}{len(known):>2} 次  {cost:>7,} token  "
              f"{seconds:>6.1f}s  → 连同固定开销 {fixed_tokens + cost:,}")
    print(f"  全部     {len(out):>2} 次  {total_tokens:>7,} token")
    print("-" * 68)
    print("固定开销每次请求都要付；链路只在真的调用时付。两者相加才是窗口压力。")
    return 0


def _token_from_env() -> str:
    env = ROOT / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("QUANT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
