"""把自动化提示词点名的字段路径，逐条喂给真实响应。

为什么需要它：`docs/automations/*.md` 是模型每天的作业指导书，而它们点名的
字段（`current.tradable`、`session.bars_behind`…）只要和响应差一个词，模型就会
读到 None 并据此做出相反的决定——2026-11-05 一次核对就抓到四条：`current.
sleeve_state`（实际是 `open_sleeves`）、`data.core_ok`（实际是 `core_usable`）、
`quant_facts.recent`（不存在，跨日数在 `quant_limit_structure.trend`）、
`quant_pools` 的裸 `tone`（在 `sentiment.tone`）。提示词在宿主里没有版本历史，
所以正本放在仓库，这个脚本负责让正本与响应不脱节。

    .venv/Scripts/python.exe scripts/audit_prompts.py            # 只查映射完整
    .venv/Scripts/python.exe scripts/audit_prompts.py --live     # 连服务逐条解析

`--live` 需要服务在跑（`/health` 可答）与 `.env` 里的 `QUANT_TOKEN`。四条路径
只能在"写过一笔之后"的响应里出现，它们按源码取证，见 `SOURCE_EVIDENCE`。

除了点路径，还核对「`锚点` 的 `a` / `b` / `c`」这种字段列表里的**裸字段**
（见 `bare_fields`）：它们不带点，正则看不见，而 2026-11-13 的 `opened_intraday`
正是从这里漏过去的。没有锚点的裸名不猜——`pre_open` 那类其实是枚举值。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "docs" / "automations"
BASE = "http://127.0.0.1:8811"

#: 提示词里的一段路径 → 哪个端点能答。写死是有意的：新增路径必须有人决定
#: "它归哪个响应"，否则脚本会把它当已核对。
BY_PREFIX = {
    "session": "/context_pack",
    "data": "/context_pack",
    "market": "/context_pack",
    "pool_state": "/market/pools",
    "sentiment": "/market/pools",
    "gates": "/market/pools",
    "current": "/persona",
    "available": "/persona",
    "eligibility": "/persona",
    "today": "/persona",
    "edge": "/arena",
    "history": "/knowledge/limit_structure",
    "yesterday": "/knowledge/limit_structure",
    "trend": "/knowledge/limit_structure",
    # v2 开工契约直接读这三段
    "risk": "/context_pack",
    "pipeline": "/data/brief",
    # 短线证据分页由 WorkBuddy 串行续读，核对 page.next_offset 的真实契约。
    "page": "/short-alpha/evidence?as_of=20261113&limit=1",
    "book_watch": "/market/frame",
    # 带端点名写全，避免 `counts` 这种在多处出现的裸前缀被错配到唯一端点
    "limit_structure": "/knowledge/limit_structure",
    # `quant_facts.affordable` 的第四问：封后有没有开过板（东财池，分母不同）。
    # 提示词里写 `board_opens.opened`，响应里在 `affordable.board_opens` 下。
    "board_opens": "/facts",
    # 赔率门的三个读数都来自 quant_analog；端点要带一个 symbol 才有响应。
    "net": "/knowledge/analog?symbol=600000.SH",
    "horizons": "/knowledge/analog?symbol=600000.SH",
    "match": "/knowledge/analog?symbol=600000.SH",
}

BY_TOOL = {
    "quant_persona": "/persona",
    "quant_portfolio": "/portfolio",
    "quant_context": "/context_pack",
    "quant_observations": "/observations",
    "quant_facts": "/facts",
    "quant_samples": "/samples",
    "quant_arena": "/arena",
    "quant_pools": "/market/pools",
    "quant_limit_structure": "/knowledge/limit_structure",
    "quant_profile": "/symbol/600000.SH/profile",
    "quant_market_status": "/market/status",
    "market_status": "/market/status",
    "quant_data_health": "/data/brief",
    "quant_duty": "/duty",
    "quant_live": "/market/frame",
    "quant_quote": "/market/quote",
    "quant_analog": "/knowledge/analog",
    # `blind_spots` / `conflicts` 是这里的裸字段（见 `FIELD_ANCHOR_RE`），
    # 以前只按点路径核对，所以这张表里一直没有它。
    "quant_information": "/data/information",
}

#: 这几条只出现在"写过一笔之后"的响应里。离线核对改成读源码：键名在构造函数
#: 里必须出现，否则一样是悬空的。
SOURCE_EVIDENCE = {
    "fillability.queue.required": ("service/trading/fill.py", '"required"'),
    "verification.basis": ("service/trading/journal_check.py", '"basis"'),
    "claims.misplaced": ("service/trading/journal_check.py", "misplaced"),
    "claims.unbacked": ("service/trading/journal_check.py", "unbacked"),
    # `warning` 只在 pre_open / opening_window / intraday / stale_close 四个 phase
    # 里有，收盘后被日更写成 settled 就没有这个键——而 18:50 的复盘正好跑在
    # settled 上。探针看的是一个时刻，字段的存在性是按 phase 分支的，所以这里
    # 按源码取证：键必须还在构造函数里，缺席本身是语义的一部分。
    "pool_state.warning": ("service/api/trading.py", 'state["warning"]'),
    # 票据只有写过一笔之后才存在，探针没有 id 可取；键名在 fill.py 里。
    "quant_ticket.fillability.queue.required": ("service/trading/fill.py", '"required"'),
    # `watcher` 在没跑过一轮时是 null（周末探针就是），outcome 只在心跳里出现。
    "book_watch.watcher.outcome": ("service/notify/alert_log.py", '"outcome"'),
}

#: 端点在响应里多包了一层：提示词写短路径，解析时补上前缀。
KEY_PREFIX = {
    "board_opens": "affordable.",
}

#: 裸名归属需要按整条路径判的例子：`session.label` / `session.in_session` 在 v2
#: 契约里紧跟 `quant_market_status`，而 `/context_pack.session` 只有
#: market_open_today / tradable_now，没有这两个键。
EXACT_ROUTE = {
    "session.label": "/market/status",
    "session.in_session": "/market/status",
    # `counts` 在 pools 与 limit_structure 里都有；这条路径在 1430 里指的是
    # limit_structure（封板率/最高板那一组），按整条路径定归属，不给裸前缀开口子。
    "counts.limit_up": "/knowledge/limit_structure",
}

#: 裸锚点（提示词里用简称）→ 全名。每条都要给理由——否则这里就成了第二个后门。
ANCHOR_ALIAS = {
    # 1850 §七-1 上一行刚写 `quant_facts(date=D)`，下一行接着用简称。
    "affordable": "quant_facts.affordable",
}

#: 反引号里的「路径 + 的/读/取/： + 字段列表」。字段是裸名字、不带点，`PATH_RE`
#: 看不见——2026-11-13 的 `opened_intraday` 就是这样漏过去的：它早在 8ddff76 就
#: 拆成了 `one_word` + `board_opens.opened`，提示词却还写着旧名，模型读到 None 后
#: 会拿 `in_cap` 顶替第三个口径。这里只认「锚点后面紧跟列表」这一种写法：裸名
#: 脱离锚点没有确定归属（`pre_open` / `opening_window` 还可能是枚举值）。
BACKTICK_RE = re.compile(r"`([^`]+)`")
LIST_SEP_RE = re.compile(r"^\s*[/、]\s*$")
FIELD_ANCHOR_RE = re.compile(r"`([A-Za-z_][\w.]*)`\s*(?:的|读|取|：|:)\s*$")

#: 跨行找锚点时只认「的/读/取」。冒号在同一行里是「这个端点给这些东西」，跨行却可能
#: 只是「这个键有这几种取值」——0905 的 `warning`：后面紧跟的就是 `pre_open` /
#: `opening_window` / `missing` 三个枚举值，按字段查会凭空造出三个悬空。
FIELD_ANCHOR_WRAPPED_RE = re.compile(r"`([A-Za-z_][\w.]*)`\s*(?:的|读|取)\s*$")

#: 不是"响应字段路径"的写法，但写在提示词里是对的。每条都必须给理由——
#: 否则这里就成了关掉闸门的后门。
EXEMPT = {
    "quant_duty.upstream_missing": "`kind` 的取值，不是字段；读法是 items[].kind",
    "quant_quote.last": "工具名.字段 简写，实际是 quotes[].last；逐键解析会误判",
}

#: 反引号里可以是纯路径，也可以带尾巴（`` `data.core_usable=false` ``、
#: `` `session.bars_behind>0` ``）。只取前面那段路径——第一版要求整段反引号
#: 都是路径，于是带比较符的两条根本没被检查，`core_ok` 就从网眼里漏了过去。
PATH_RE = re.compile(r"`([a-z_]+(?:\.[a-z_0-9]+)+)[^`]*`")

#: 反引号里也会出现文件名（`decisions.py:264`）。它们不是响应路径，跳过。
FILE_SUFFIXES = (".py", ".md", ".json", ".yaml", ".yml", ".js", ".mjs", ".sqlite")

PLUGIN = ROOT / "plugin" / "lib" / "index.js"

#: 工具定义是 `define('quant_facts', …)`。提示词点名的工具必须在这里出现：
#: 工具改名比字段改名更响（`ToolSearch` 直接载不到），但也没有便宜的办法在一个月后
#: 复查——所以按源码取证，顺带把「只在提示词里存在」的工具名挡在提交前。
TOOL_DEF_RE = re.compile(r"define\(\s*'(quant_[a-z_0-9]+)'")
PUBLIC_BLOCK_RE = re.compile(r"PUBLIC_TOOL_NAMES\s*=\s*Object\.freeze\(\[([\s\S]*?)\]\)")
PUBLIC_DEF_RE = re.compile(r"'(quant_[a-z_0-9]+)'")
TOOL_USE_RE = re.compile(r"\b(quant_[a-z_0-9]+)\b")


def bodies() -> dict[str, str]:
    out = {}
    for path in sorted(PROMPTS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        out[path.name] = text.split("---", 2)[2] if text.startswith("---") else text
    return out


def paths() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, body in bodies().items():
        for match in PATH_RE.finditer(body):
            dotted = match.group(1)
            if dotted.endswith(FILE_SUFFIXES):
                continue
            found.setdefault(dotted, set()).add(name)
    return found


def defined_tools() -> set[str]:
    """Internal handlers plus the four names listed by PUBLIC_TOOL_NAMES."""
    internal = set(TOOL_DEF_RE.findall(PLUGIN.read_text(encoding="utf-8")))
    facade = (PLUGIN.parent / "facade.js").read_text(encoding="utf-8")
    block = PUBLIC_BLOCK_RE.search(facade)
    if not block:
        raise RuntimeError("找不到 PUBLIC_TOOL_NAMES")
    return internal | set(PUBLIC_DEF_RE.findall(block.group(1)))


def tools_used() -> dict[str, set[str]]:
    """提示词里点名过的工具：{工具名: {提示词文件}}。"""
    out: dict[str, set[str]] = {}
    for name, body in bodies().items():
        for match in TOOL_USE_RE.finditer(body):
            out.setdefault(match.group(1), set()).add(name)
    return out


def ghost_tools() -> dict[str, set[str]]:
    """提示词点了名、插件里没有的工具。"""
    defined = defined_tools()
    return {tool: where for tool, where in tools_used().items() if tool not in defined}


def route_of(dotted: str) -> tuple[str, str] | None:
    """(端点, 要走的键路径)。认不出来就返回 None，由调用方报错。

    工具名前缀（`quant_persona.current.key`）要去掉，因为响应的根不是工具名；
    字段前缀（`current.key`）要保留，因为 `current` 就是响应里的一个键。
    """
    if dotted in EXACT_ROUTE:
        return EXACT_ROUTE[dotted], dotted
    head, _, rest = dotted.partition(".")
    if head in BY_TOOL:
        return BY_TOOL[head], rest
    if head in BY_PREFIX:
        return BY_PREFIX[head], KEY_PREFIX.get(head, "") + dotted
    return None


def field_lists() -> list[tuple[str, str, list[str]]]:
    """「锚点 的 字段列表」三元组：(提示词文件, 锚点, [字段…])。

    只认反引号之间只剩分隔符（`/`、`、`）的相邻写法。提示词是硬折行的：列表会在
    任意一个 `/` 后断行，锚点也可能留在上一行末尾（`quant_limit_structure` 的 /
    换行 / 七个字段）。所以按整段扫描，分隔符里允许换行；行首的列表允许到上一行
    末尾找锚点，上一行不是以锚点收尾就不猜。
    没有锚点的列表不猜——它们可能属于任何一个端点，猜错比不查更糟。
    """
    out = []
    for name, body in bodies().items():
        found = list(BACKTICK_RE.finditer(body))
        index = 0
        while index < len(found):
            group = [found[index]]
            cursor = index + 1
            while (cursor < len(found)
                   and LIST_SEP_RE.match(body[found[cursor - 1].end():found[cursor].start()])):
                group.append(found[cursor])
                cursor += 1
            if len(group) >= 2:
                start = group[0].start()
                line_start = body.rfind("\n", 0, start) + 1
                prefix = body[line_start:start]
                anchor = FIELD_ANCHOR_RE.search(prefix)
                if anchor is None and not prefix.strip():
                    previous = body[body.rfind("\n", 0, max(line_start - 1, 0)) + 1:
                                    max(line_start - 1, 0)]
                    anchor = FIELD_ANCHOR_WRAPPED_RE.search(previous)
                if anchor:
                    out.append((name, anchor.group(1), [m.group(1) for m in group]))
            index = cursor
    return out


def bare_fields() -> dict[tuple[str, str], set[str]]:
    """列表里不带点的字段：{(锚点, 字段): {提示词文件}}。"""
    found: dict[tuple[str, str], set[str]] = {}
    for name, anchor, items in field_lists():
        anchor = ANCHOR_ALIAS.get(anchor, anchor)
        for item in items:
            if "." in item or item in BY_TOOL or item.endswith(FILE_SUFFIXES):
                continue
            found.setdefault((anchor, item), set()).add(name)
    return found


def resolve(payload, dotted_key: str):
    """沿键路径走；遇到列表就逐个元素试。走不到就抛 KeyError。"""
    node = payload
    for part in dotted_key.split("."):
        if isinstance(node, list):
            for item in node:
                try:
                    node = item[part] if isinstance(item, dict) else None
                except KeyError:
                    node = None
                if node is not None:
                    break
            if node is None:
                raise KeyError(part)
        elif isinstance(node, dict):
            if part not in node:
                raise KeyError(part)
            node = node[part]
        else:
            raise KeyError(part)
    return node


def field_key(anchor: str, field: str) -> tuple[str, str]:
    """(端点, 响应里的键路径)。锚点决定前缀，字段只是它后面的一段。"""
    routed = route_of(ANCHOR_ALIAS.get(anchor, anchor))
    if routed is None:
        raise KeyError(anchor)
    endpoint, rest = routed
    return endpoint, f"{rest}.{field}" if rest else field


def token() -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("QUANT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def fetch(endpoint: str, tok: str) -> dict:
    request = urllib.request.Request(BASE + endpoint, headers={"X-Quant-Token": tok})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="连服务逐条解析")
    args = parser.parse_args(argv)

    found = paths()
    if len(found) < 20:
        # 元断言：正则或文件搬走之后，"没有悬空路径"会变成"没有路径"。
        print(f"只解析到 {len(found)} 条路径，解析器坏了", file=sys.stderr)
        return 2

    unknown = {p: sorted(where) for p, where in found.items()
               if p not in SOURCE_EVIDENCE and p not in EXEMPT
               and route_of(p) is None}
    for path, where in sorted(unknown.items()):
        print(f"未登记  {path}  ({', '.join(where)})  ← 在 BY_PREFIX / BY_TOOL 里说它归哪个端点")
    if unknown:
        return 1

    fields = bare_fields()
    if len(fields) < 3:
        # 同一道元断言：写法改了之后，"没有悬空字段"不能变成"没有字段"。
        print(f"只解析到 {len(fields)} 个裸字段，列表解析器坏了", file=sys.stderr)
        return 2
    stray = sorted({(anchor, name) for (anchor, _), names in fields.items()
                    for name in names if route_of(anchor) is None})
    for anchor, name in stray:
        print(f"未知锚点 {anchor} 的 {name}  ← 在 ANCHOR_ALIAS / BY_TOOL 里给它一个端点")
    if stray:
        return 1

    if len(defined_tools()) < 40:
        # 同一个道理：插件改了写法之后，"没有幽灵工具"会变成"没有工具"。
        print(f"只从插件里解析到 {len(defined_tools())} 个工具名，解析器坏了", file=sys.stderr)
        return 2
    ghosts = ghost_tools()
    for tool, where in sorted(ghosts.items()):
        print(f"未定义  {tool}  ({', '.join(sorted(where))})  ← 插件里没有 define('{tool}')")
    if ghosts:
        return 1
    print(f"路径 {len(found)} 条，全部有归属；裸字段 {len(fields)} 个挂在可解析的锚点上；"
          f"工具 {len(tools_used())} 个全部存在")

    if not args.live:
        return 0

    tok = token()
    if not tok:
        print("没有 QUANT_TOKEN，跳过后半段", file=sys.stderr)
        return 2

    payloads: dict[str, dict] = {}
    dangling = 0
    for path, where in sorted(found.items()):
        if path in SOURCE_EVIDENCE or path in EXEMPT:
            continue
        endpoint, rest = route_of(path)
        if endpoint not in payloads:
            try:
                payloads[endpoint] = fetch(endpoint, tok)
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"取不到 {endpoint}: {exc}", file=sys.stderr)
                return 2
        try:
            resolve(payloads[endpoint], rest)
        except KeyError as exc:
            print(f"悬空    {path}  ({', '.join(sorted(where))})  ← {endpoint} 里没有 {exc}")
            dangling += 1

    for path, (rel, token_) in SOURCE_EVIDENCE.items():
        if path not in found:
            continue
        source = (ROOT / rel).read_text(encoding="utf-8")
        if token_ not in source:
            print(f"悬空    {path}  ← {rel} 里没有 {token_}")
            dangling += 1

    for (anchor, field), where in sorted(fields.items()):
        endpoint, key = field_key(anchor, field)
        if endpoint not in payloads:
            try:
                payloads[endpoint] = fetch(endpoint, tok)
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"取不到 {endpoint}: {exc}", file=sys.stderr)
                return 2
        try:
            resolve(payloads[endpoint], key)
        except KeyError as exc:
            print(f"悬空    {anchor} 的 {field}  ({', '.join(sorted(where))})"
                  f"  ← {endpoint} 里没有 {key}（缺 {exc}）")
            dangling += 1

    print(f"核对完毕：{len(found)} 条路径 + {len(fields)} 个锚定字段，悬空 {dangling} 条"
          f"（其中 {len(SOURCE_EVIDENCE)} 条按源码取证，{len(EXEMPT)} 条显式豁免）")
    return 1 if dangling else 0


if __name__ == "__main__":
    raise SystemExit(main())
