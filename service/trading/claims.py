"""写下来的数，是不是贴在了它该在的标签上。

`journal_check` 回答的是"这个数在当天 facts 里找不找得到"。找得到还不够：
2026-10-29 的收盘报告写"可买的 6 只"，当日实际是**上限内 23 只、收盘可买 0 只**。
"6" 当然可能出现在 facts 的某个角落（某个比率、某个板块的家数），于是"存在性"
检查放它过去——但这句话是错的，错在把 6 贴到了"可买"这个标签上。

所以这里做的是另一半：**先看标签，再看数**。

* `<数> 家 <标签>` / `<标签> <数> 家`（计数），
* `<标签>…<数>%`（百分比，允许中间隔几个字："等权跌 3.04%"）。

三种判决，没有"通过/不通过"：

* ``ok``        —— 数和这个标签对应的那个事实一致；
* ``misplaced`` —— 数是真的，但它是**另一个**事实的值（"893 家跌停"，跌停其实是 93）；
* ``unbacked``  —— 标签认得出，但 facts 里没有任何数等于它（"涨停 137 家"，137 不存在）。

为什么要"计数词"这道约束：中文散文里"上涨 3 天"和"893 家上涨"长得像，只按窗口
找标签会把前者判成错。要求数字与标签之间只有一个 家/只/个，误报会掉一个量级——
**会误报的检查比没有检查更糟**（这条在 `journal_check` 里已经付过一次学费）。

模糊的标签不判。裸"可买"既可能指 `in_cap`（上限内）也可能指 `buyable_at_close`
（收盘还能买），而这两个数在 10-29 那天是 23 和 0——**把两者混为一谈正是那次事故
本身**。所以它归到一个组：等于组里任何一个数算 ``ok``，否则把两个口径都报出来让
写的人自己选，而不是替它选一个。

判决全部由确定性规则给出，没有模型参与；每条结论都带 ``span``（原文那几个字），
读的人可以回指。分工取自 Reconciliation Ledger：模型负责写，规则负责判。
"""

from __future__ import annotations

import re

#: 计数标签：别名 → 它可能在说的那些事实路径。
#: 一个别名给多个路径的，等于其中任何一个都算对；都对不上时把全部候选报出来。
COUNT_LABELS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("涨停家数", "涨停"), ("pools.limit_up",)),
    (("跌停家数", "跌停"), ("pools.limit_down",)),
    (("上涨家数", "上涨", "红盘"), ("breadth.advancers",)),
    (("下跌家数", "下跌", "绿盘"), ("breadth.decliners",)),
    (("平盘",), ("breadth.flat",)),
    (("炸板",), ("pools.broken",)),
    (("上限内", "涨停价内"), ("affordable.in_cap",)),
    (("收盘可买",), ("affordable.buyable_at_close",)),
    # 裸"可买"是两个口径的混称——10-29 的报告就是在这里把 23 和 0 写成了 6。
    (("可买",), ("affordable.in_cap", "affordable.buyable_at_close")),
    (("持仓",), ("account.positions",)),
)

#: 百分比标签：别名 → 事实路径。中间允许隔几个字（"等权跌 3.04%"）。
PCT_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("等权", "等权收益", "等权涨跌"), "breadth.equal_weight_return"),
    (("中位数", "中位"), "breadth.median_return"),
)

#: 数 ↔ 标签之间只有计数词（和偶尔一个"的"）。多一个字就不认——这是防误报的主要一道闸。
#: 两种语序都要认："893 家上涨" 与 "涨停 49 家"。
_COUNT = r"\s*(?:家|只|个)"
_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_GAP = r"[^，。；、\n\d]{0,6}?"

#: 一个数在三种写法下都算同一个事实：3.04% 对 -0.030363，5,007.51 对 "5007.51"。
REL_TOL = 0.005


def scales(value: float):
    """同一事实在散文里可能的几种量级。

    放量与收量各有前提：×100 只给比率形状的值（|v| ≤ 1.5），÷100 只给足够大的值。
    无条件的 ×100 会让收在 9.12 的票匹配上"912 家"——实测发生过。
    """
    yield value
    if abs(value) <= 1.5:
        yield value * 100.0
    if abs(value) >= 3:
        yield value / 100.0


def same_value(written: float, stored: float) -> bool:
    if written == stored:
        return True
    return any(abs(abs(written) - abs(s)) <= max(abs(s) * REL_TOL, 1e-9)
               for s in scales(stored))


def matches(written: float, stored: float) -> bool:
    """``same_value`` plus the rule that keeps 1 家 from matching 1.0002.

    整数只与整数比。`readiness.coverage = 1.0002` 让"跌停 1 家"匹配上过——
    于是"数是真的、标签错了"这句话指向了一个覆盖率。家数不带小数，价格都带，
    这条线 `journal_check` 已经画过一次。
    """
    if float(written).is_integer() and not float(stored).is_integer():
        return False
    return same_value(written, stored)


def _at(facts: dict, path: str):
    node = facts
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, bool) or node is None:
        return None
    if isinstance(node, (int, float)):
        return float(node)
    if isinstance(node, str):
        try:
            return float(node.replace(",", ""))
        except ValueError:
            return None
    return None


def _leaves(facts: dict) -> dict[str, float]:
    """Every number in the payload, keyed by the path that produced it.

    路径比数值有用得多：读者看到 "893 家跌停" 需要知道 893 是
    `breadth.advancers`，否则"数是真的但贴错标签"这句话没法动手改。
    """
    out: dict[str, float] = {}

    def walk(node, prefix: str):
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            out[prefix] = float(node)
        elif isinstance(node, str):
            for token in re.findall(_NUM, node):
                try:
                    out.setdefault(prefix, float(token.replace(",", "")))
                except ValueError:
                    pass
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{prefix}[{index}]")

    walk(facts or {}, "")
    return out


def _alternation(aliases: tuple[str, ...]) -> str:
    # 长别名在前：`涨停家数` 不能被 `涨停` 抢先匹配掉一半。
    return "|".join(sorted((re.escape(alias) for alias in aliases), key=len, reverse=True))


def _count_pattern(aliases: tuple[str, ...]) -> re.Pattern:
    joined = _alternation(aliases)
    return re.compile(
        # 数在标签前：`893 家上涨`（中间可以有一个"的"：`可买的 6 只`）
        rf"(?:(?P<n1>{_NUM}){_COUNT}\s*的?\s*(?P<l1>{joined})"
        # 标签在数前：`涨停 49 家`
        rf"|(?P<l2>{joined})\s*的?\s*(?P<n2>{_NUM}){_COUNT})"
    )


def _bare_count_pattern(aliases: tuple[str, ...]) -> re.Pattern:
    """`涨停家数 49` —— 计量词已经在标签里，后面不必再跟一个"家"。"""
    joined = _alternation(aliases)
    return re.compile(rf"(?P<l>{joined})\s*[:：]?\s*(?P<n>{_NUM})(?![\d.])")


def _pct_pattern(aliases: tuple[str, ...]) -> re.Pattern:
    return re.compile(rf"(?P<l>{_alternation(aliases)}){_GAP}(?P<n>{_NUM})\s*%")


COUNT_PATTERNS = []
for _aliases, _paths in COUNT_LABELS:
    # "涨停家数" 自带计量词：`涨停家数 49` 是对的，不需要再来一个"家"。
    _bare = tuple(alias for alias in _aliases if alias.endswith("家数"))
    _rest = tuple(alias for alias in _aliases if alias not in _bare)
    if _rest:
        COUNT_PATTERNS.append((_count_pattern(_rest), _rest, _paths, ("l1", "l2")))
    if _bare:
        COUNT_PATTERNS.append((_bare_count_pattern(_bare), _bare, _paths, ("l",)))
PCT_PATTERNS = [(_pct_pattern(aliases), aliases, path) for aliases, path in PCT_LABELS]


def _first(match: re.Match, *names: str) -> str | None:
    for name in names:
        value = match.groupdict().get(name)
        if value:
            return value
    return None


#: 带时刻的**读数**是另一个口径（`跌停 14 家（11:30）→ 62 家（14:58）`），拿收盘的
#: 93 去核它反而是错的。豁免只给 `TIME_VARYING` 里的读数，且**不能因此丢掉它**——
#: 10-29 那条"可买的 6 只"夹在一个 14:57:17 的句子里，它恰恰是本项目最贵的那次错误。
_INTRADAY = re.compile(r"\d{1,2}:\d{2}|盘中|午间|午报|午盘|上午|下午|中午|早盘|早报|"
                       r"盘前|尾盘|开盘|收盘前|收盘报|半日")
#: 会随时刻变的"读数"。涨停家数 11:30 是 14 家、14:58 是 62 家，都真；而"上限内可买"
#: 是**当日口径**，14:57 和收盘是同一个数——10-29 的"可买的 6 只"就是在这里错的，
#: 给它时刻豁免等于把本项目最贵的那次错误放过去。
TIME_VARYING = frozenset({
    "pools.limit_up", "pools.limit_down", "pools.broken",
    "breadth.advancers", "breadth.decliners", "breadth.flat",
})

#: 写明来源的口径差异（"47 家涨停…eastmoney 全量口径"）不是错，是交代过了。
_DISCLOSED = re.compile(r"东财|东方财富|eastmoney|同花顺|ths|通达信|曾封|全量口径", re.IGNORECASE)
#: "自列 5 只"说的是我那张清单，不是市场——这正是 P0 事故里被混淆的那件事，
#: 但写清楚"自列"之后它就不再是错。跟着它出现的等权收益同理（那是我那篮子的收益）。
_SAMPLE = re.compile(r"自列|自选|我取|自取|清单|样本")
#: 说昨天/前一日的话，不是今天的错。
_OTHER_DAY = re.compile(r"昨日|昨天|前日|上一交易日|此前|上一版")
#: 引用另一份报告的口径（"午报可买 13 只"）。那句话的主题是那份报告，不是收盘 facts。
_QUOTED = re.compile(r"午报|早报|收盘报|开盘报|盘中报|快报")
#: facts 里的等权/中位数都是**1 日**的。"20 日等权 -9.87%"、"5 日中位数 -0.81%"
#: 是累计值与样本前瞻收益——同一个词，不同的量。带窗口的百分比断言不判。
_WINDOW = re.compile(r"\d+\s*[日天]")
#: facts 存的是**水平值**，散文常写**变化量**："午后新增 7 只炸板"里的 7 是增量，
#: 当天的炸板总数是 26。混起来会得到"7 是编的"这种错误结论。
_DELTA = re.compile(r"新增|增加|净增|多出|上升|回落|收缩|扩大|减少|减少到|环比|较前")
#: 百分比还常出现在**比较与回测**里："同期等权市场 −1.56%" 说的是历史上那一段，
#: "四大行等权 +2.21%" 说的是一个子集。facts 里只有全市场当日的一个数。
_COMPARISON = re.compile(r"同期|区间|样本|回测|历史|超额|相对|对比|篮子|四大行|板块内|vs|VS")
_CLAUSE_BREAK = re.compile(r"[。！？\n]")
_CLAUSE_RADIUS = 120


def _clause(text: str, start: int, end: int) -> str:
    """The sentence this claim sits in, capped so one long paragraph cannot swallow it.

    逐字窗口不够用：`下午 47 家涨停伴随 31 家炸板、62 家跌停` 里三个数共用开头那个
    "下午"，离第三个已经 18 个字。而句读才是真正的边界——同一句里的时间是共享的，
    下一句里的"收盘 93 家"不该被上一句的"上午"带走。
    """
    left = max(0, start - _CLAUSE_RADIUS)
    for match in _CLAUSE_BREAK.finditer(text, left, start):
        left = match.end()
    right = min(len(text), end + _CLAUSE_RADIUS)
    match = _CLAUSE_BREAK.search(text, end, right)
    if match:
        right = match.start()
    return text[left:right]


def _near(pattern: re.Pattern, text: str, start: int, end: int) -> bool:
    return bool(pattern.search(_clause(text, start, end)))


#: 不判、但照报的三种情形。为什么"照报"：一个被静默丢弃的错句，等于没检查。
PROVISIONAL = (
    ("intraday", _INTRADAY),    # 盘中读数，收盘 facts 管不着
    ("sample", _SAMPLE),        # 我那张清单的口径，不是市场口径
    ("elsewhere", _OTHER_DAY),  # 说的是别的交易日
    ("quoted", _QUOTED),        # 引用的是另一份报告的口径
    ("delta", _DELTA),          # 说的是变化量，不是水平值
)


def _span(text: str, start: int, end: int, width: int = 24) -> str:
    """The verbatim quote a verdict points back at — trimmed, never paraphrased.

    只在左边补几个字（主语通常在那里），右边不补：右边补出来的往往是下一句话的
    开头（"涨停 137 家，跌停"），会让引用比原句还难读。左边也不跨句读——
    "家，可买的 6 只" 这种引用读起来像错位。
    """
    left = text[max(0, start - 3):start]
    # 空格不算句读："今天 893 家跌停" 的前三个字里那个空格不是边界。
    for mark in "，。；、,.;：:":
        left = left.rsplit(mark, 1)[-1]
    # 左边那截如果带着数字或字母，说明它切在词中间（"20 日等权" 会被切成 "0 日"），
    # 引用出来比不引用更糊涂。整截丢掉。
    if any(char.isdigit() or char.isascii() and char.isalpha() for char in left):
        left = ""
    quote = re.sub(r"\s+", " ", left + text[start:end]).strip()
    quote = quote.rstrip("，。；、,.;：: ")
    return quote if len(quote) <= width else "…" + quote[-width:]


def _label_of(aliases: tuple[str, ...], matched: str) -> str:
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in matched:
            return alias
    return matched


def _vendor_hint(facts: dict, path: str) -> dict | None:
    """Same-day numbers from the other vendor, when `facts` carries them.

    `pools.limit_up` is 74 by the derived pool, 72 by 东财 and something else
    again by 同花顺 — the block's own note says three vendors disagree and the
    citation must name one. So "should be 74" is half the fix; "or 72 if you
    meant 东财" is the other half, and it is already on disk.
    """
    parts = path.split(".")
    if len(parts) < 2:
        return None
    parent = facts
    for part in parts[:-1]:
        parent = parent.get(part) if isinstance(parent, dict) else None
    if not isinstance(parent, dict):
        return None
    leaf = parts[-1]
    found = {}
    for key, block in parent.items():
        if key.startswith("vendor_") and isinstance(block, dict) and leaf in block:
            value = block[leaf]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found[key.removeprefix("vendor_")] = _plain(float(value))
    return found or None


def _plain(value: float):
    return int(value) if float(value).is_integer() else round(value, 4)


def _provisional(item: dict, target: list, text: str) -> str | None:
    """Why this claim is not adjudicated, or ``None`` if it should be.

    ``intraday`` 只对会随时刻变的读数生效：`跌停 62 家（14:58）` 是另一口径，
    `可买的 6 只（14:57）` 不是——后者当天任何时刻都该是同一个集合。
    """
    # 百分比只在"1 日、全市场"这个口径上与 facts 可比；先于其它标记判，因为
    # "5 日中位数"里那个窗口是标签本身的一部分，比"这句提到昨天"更根本。
    if item["kind"] == "pct":
        if _near(_WINDOW, text, item["start"], item["end"]):
            return "window"
        if _near(_COMPARISON, text, item["start"], item["end"]):
            return "comparison"
    for reason, pattern in PROVISIONAL:
        if reason == "intraday" and not any(path in TIME_VARYING for path, _ in target):
            continue
        if _near(pattern, text, item["start"], item["end"]):
            return reason
    return None


def scan(text: str, facts: dict | None) -> dict:
    """Which labelled numbers in ``text`` the day's facts do not support.

    ``facts`` is the whole facts payload. ``None`` means the day has none, and
    that is reported as "cannot check" rather than "all clear" — the same
    distinction every other block in this project makes.
    """
    if not facts:
        return {"labelled": 0, "ok": 0, "misplaced": [], "unbacked": [],
                "skipped": True, "note": "当天没有 facts 快照，无法核对——不是通过"}

    known = _leaves(facts)
    found: list[dict] = []
    for pattern, aliases, paths, label_groups in COUNT_PATTERNS:
        for match in pattern.finditer(text):
            label = _first(match, *label_groups) or ""
            found.append({
                "start": match.start(), "end": match.end(),
                "number": _first(match, "n1", "n2", "n"),
                "label": _label_of(aliases, label), "label_len": len(label),
                "paths": paths, "kind": "count",
            })
    for pattern, aliases, path in PCT_PATTERNS:
        for match in pattern.finditer(text):
            label = match.group("l")
            found.append({
                "start": match.start(), "end": match.end(), "number": match.group("n"),
                "label": _label_of(aliases, label), "label_len": len(label),
                "paths": (path,), "kind": "pct",
            })

    # 同一段文字被两个标签抢：留匹配更长的那个（"收盘可买 0 只" 里 `收盘可买` 胜过
    # `可买`），长度相同才真的分不清——两个都不判。替写的人猜是最坏的一种"帮忙"。
    drop: set[int] = set()
    ambiguous = 0
    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            a, b = found[i], found[j]
            if a["start"] < b["end"] and b["start"] < a["end"]:
                if a["label_len"] == b["label_len"]:
                    drop.update((i, j))
                    ambiguous += 1
                else:
                    drop.add(i if a["label_len"] < b["label_len"] else j)

    ok = 0
    provisional: list[dict] = []
    misplaced: list[dict] = []
    variant: list[dict] = []
    unbacked: list[dict] = []
    seen: set[tuple] = set()
    for index, item in enumerate(found):
        if index in drop:
            continue
        try:
            written = float(item["number"].replace(",", ""))
        except ValueError:
            continue
        target = [(path, _at(facts, path)) for path in item["paths"]]
        target = [(path, value) for path, value in target if value is not None]
        if not target:
            continue
        if any(matches(written, value) for _, value in target):
            ok += 1
            continue
        key = (item["label"], item["number"])
        if key in seen:
            continue
        seen.add(key)
        report = {
            "span": _span(text, item["start"], item["end"]),
            "label": item["label"],
            "wrote": item["number"],
            "should_be": {path: _plain(value) for path, value in target},
        }
        vendors = {}
        for path, _ in target:
            hint = _vendor_hint(facts, path)
            if hint:
                vendors.update(hint)
        if vendors:
            report["vendors"] = vendors
        reason = _provisional(item, target, text)
        if reason:
            provisional.append({**report, "why": reason})
            continue
        elsewhere = sorted((path, value) for path, value in known.items()
                           if matches(written, value))
        if elsewhere:
            path, value = elsewhere[0]
            report["but_that_is"] = {"path": path, "value": _plain(value)}
            # 末段同名 = 同一个数的另一个口径（`pools.vendor_eastmoney.limit_up` 之于
            # `pools.limit_up`）。那不是"贴错标签"，是"没说清来源"，改法完全不同：
            # 一个要换数，一个只要写一句"东财口径"。
            same_leaf = any(path.split(".")[-1] == wanted.split(".")[-1]
                            for wanted, _ in target)
            if same_leaf and _near(_DISCLOSED, text, item["start"], item["end"]):
                ok += 1
                continue
            (variant if same_leaf else misplaced).append(report)
        else:
            unbacked.append(report)

    out: dict = {"labelled": ok + len(misplaced) + len(variant) + len(unbacked)
                 + len(provisional),
                 "ok": ok, "misplaced": misplaced, "unbacked": unbacked}
    if provisional:
        out["provisional"] = provisional
        out["provisional_note"] = ("这四种不判、但照报：intraday 是盘中读数（收盘 facts 管不着），"
                                   "sample 说的是「我那几只」而不是市场，elsewhere 说的是别的交易日，"
                                   "quoted 引用的是另一份报告。它们都不是收盘的数。")
    if variant:
        out["variant"] = variant
    if ambiguous:
        out["ambiguous"] = ambiguous
    if misplaced or unbacked or variant:
        out["note"] = ("misplaced 是「数是真的、贴错了标签」（893 家跌停，跌停其实是 93）；"
                       "variant 是「同一个数的另一个口径」（东财 47 家 vs 同花顺 49 家），"
                       "改成哪个都行，但要写明来源；unbacked 是「这个标签下没有这个数」。"
                       "三种都比「查不到」更该改：前两种读者会顺手引用一个错的因果。")
    return out
