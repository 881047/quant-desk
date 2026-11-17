"""复盘里写的数字，能不能在当天的 `facts` 里找到。

`facts` 存在的理由是一条具体的失败：2026-10-29 的收盘报告写"可买的 6 只全部封在涨停价"，
而当日实际是上限内 23 只、收盘可买 0 只。于是有了那张表，也有了 `verify_report.py`。

但那条链只覆盖**收盘报告**，而且是事后跑。复盘日志走的是另一条路：
`quant_journal_append` 逐字入库，没有任何核对，而它会被以后的会话读回来
（`quant_memory_search` / `quant_journal_search`）——**错的数字在这里不是一次性的，
它会变成记忆**，然后被下一次引用。

所以这里做的事很小：把文本里的数字抽出来，和当天的 facts 对一遍，**不拦、只报**。
拦会教模型把数字删掉，而复盘的价值一半在数字上。报出来，写的人自己决定。

三种口径都要认，因为同一件事在文本里有三种写法：

* `3.04%` 对 `-3.0363`（小数）——比值 100
* `5,007.51` 对 `"5007.51"`（字符串）——去掉千分位
* `49 家` 对 `49`（整数）——原样

对不上不等于错：可能是"我买了 3 只"这类别处来的数。所以输出分两栏——
`matched` 给了多少个、`unmatched` 是哪些——而不是一个"通过/不通过"。

核完就完了吗？没有。核的结论存在条目旁边（`stored`），读回来时跟着条目一起出
（`GET /journal` 与 `/memory/search` 的 `verified`）。不这么做的话，第二次引用
这条结论的人看到的仍是那段文字，而"当初核过没有"只有写的人知道——**记忆的
可信度不能只存在于写它的那个会话里**。
"""

from __future__ import annotations

import re

import pandas as pd

from . import claims

#: 文本里的数字。允许负号与千分位；`%` 由调用方决定怎么解释，不从 token 里剥。
_TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

#: 先抹掉时间与日期，再抽数字。复盘里"19:30 看盘""11-01 复查""10月30日"是标签，
#: 不是事实——不抹掉的话它们会各自贡献两个对不上的 token，把真正的编造淹掉。
#: 实测一篇真实的收盘复盘：53 个 token 里 35 个对不上，其中大半是时间和日期。
_NOISE = (
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),            # 19:30 / 14:55:03
    re.compile(r"\d{4}-\d{2}-\d{2}"),                    # 2026-10-30
    re.compile(r"\d{1,2}月\d{1,2}日"),                    # 10月30日
    re.compile(r"(?<![\d.])\d{1,2}-\d{1,2}(?![\d.])"),     # 11-01
    re.compile(r"\d{4}年"),                                # 2026年
    re.compile(r"\d{1,3}\s*日"),                            # 5 日窗口
    re.compile(r"第\s*\d+"),                                # 第 3 条
    re.compile(r"\d{8}"),                                  # 20261030
)

# 容差与"同一事实的三种量级"都来自 ``claims``：报告写 3.04、facts 存 0.030363
# 是同一个数，这套缩放和相对容差只该有一份。此前这里另有一份 ``_scales`` 与
# ``REL_TOL``，与 claims 逐行等价——两份一旦分叉，同一句话在 claims 里算命中、
# 在 journal 核对里算编造。


def numbers_in(text: str) -> list[str]:
    """Every number token in the text, in order, deduplicated.

    Six-digit bare tokens are dropped: in this desk's prose they are stock codes
    (600519), not facts, and they would otherwise be reported as unmatched on
    every single entry — a warning that always fires is a warning nobody reads.
    """
    cleaned = str(text or "")
    for pattern in _NOISE:
        cleaned = pattern.sub(" ", cleaned)
    seen: list[str] = []
    for token in _TOKEN.findall(cleaned):
        number = token.replace(",", "")
        if _looks_like_code(number):
            continue
        if number not in seen:
            seen.append(number)
    return seen


def _looks_like_code(token: str) -> bool:
    return len(token) == 6 and token.isdigit()


def _token_pattern(token: str) -> re.Pattern[str]:
    """把去噪后的裸 token 还原成能在**原文**里定位的正则。

    `numbers_in` 会去掉千分位（"5,007.51" → "5007.51"），所以不能拿 token
    直接 `text.find()`。数字之间允许一个逗号，两侧用边界挡住"5"命中"50"。
    """
    chars = list(str(token))
    body = []
    for i, char in enumerate(chars):
        body.append(re.escape(char))
        if char.isdigit() and i + 1 < len(chars) and chars[i + 1].isdigit():
            body.append(",?")
    return re.compile(r"(?<![\d.,])" + "".join(body) + r"(?![\d.,])")


def _number_field(text: str, start: int, end: int) -> str:
    """这个数在句子里大概是价格 / 百分比 / 股数 / 家数，还是看不出来。

    是启发式，不是事实：它只用于把"待修"清单排成可读的顺序，不参与核对判定。
    看不出来就返回 `unknown`，不猜一个像模像样的角色。
    """
    after = text[end:end + 4]
    before = text[max(0, start - 4):start]
    if "%" in after or "％" in after or "百分点" in after:
        return "percent"
    if any(char in after for char in "股手"):
        return "shares"
    if any(char in after for char in "元块") or "¥" in before:
        return "cny"
    if any(char in after for char in "家只条笔次个"):
        return "count"
    if "." in text[start:end] or any(char in before for char in "收价"):
        return "price"
    return "unknown"


def locate(text: str, tokens: list[str], *, window: int = 20) -> list[dict]:
    """裸数字 → 坐标清单：值、所在句子片段、字符偏移、数字角色。

    `verified.unmatched=["5","84","4"]` 只告诉读者"有三个数对不上"，却不说
    5 是哪一句话里的 5——核对等于只报警不给坐标（docs/14 B1）。原文一直存在
    （`GET /journal?id=N&full=1`），缺的是把 token 定位回去的那一步。
    """
    source = str(text or "")
    out: list[dict] = []
    for token in tokens:
        pattern = _token_pattern(token)
        matches = list(pattern.finditer(source))
        hit = matches[0] if matches else None
        start = hit.start() if hit else None
        end = hit.end() if hit else None
        excerpt = ""
        if hit is not None:
            left = max(0, start - window)
            right = min(len(source), end + window)
            excerpt = ("…" if left > 0 else "") + source[left:right] + (
                "…" if right < len(source) else "")
        out.append({
            "value": str(token),
            "excerpt": excerpt,
            "field": _number_field(source, start, end) if hit else "unknown",
            "start": start,
            "end": end,
            "occurrences": len(matches),
            "field_basis": "heuristic: inferred from adjacent characters, not a fact",
        })
    return out


def detail(text: str, numbers: dict) -> dict:
    """给裸 token 列表补上下文；写入时与读回时共用这一条路。

    历史条目入库时只有 `unmatched` / `sign_from_words` 两个裸列表，没有 excerpt。
    读回时按原文重算，比回填数据库更稳：句子本身没变，坐标就不会过期。
    """
    out = dict(numbers)
    unmatched = list(out.get("unmatched") or [])
    if unmatched:
        out["unmatched_detail"] = locate(text, unmatched)
    signs = list(out.get("sign_from_words") or [])
    if signs:
        out["sign_hints"] = [
            dict(row, direction_unverified=True,
                 meaning="绝对值命中事实、符号未核：方向只写在词里（跌/减），不在符号里")
            for row in locate(text, signs)
        ]
    return out


def values_in(node, out: set[float]) -> set[float]:
    """Every number a facts dict contains, at any depth (strings included)."""
    if isinstance(node, bool) or node is None:
        return out
    if isinstance(node, (int, float)):
        out.add(float(node))
    elif isinstance(node, str):
        for token in _TOKEN.findall(node):
            try:
                out.add(float(token.replace(",", "")))
            except ValueError:
                continue
    elif isinstance(node, dict):
        for value in node.values():
            values_in(value, out)
    elif isinstance(node, list):
        for value in node:
            values_in(value, out)
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * claims.REL_TOL, 1e-9)


def market_numbers(desk, day: str) -> set[float]:
    """The day's own prices and turnover, as numbers a review may legitimately quote.

    `facts` carries counts and account totals, not prices — so a review saying
    "600519 收 1392.05" produced an unmatched token every single time. A check
    that flags correct prose is worse than no check: it teaches the reader to
    ignore the field. This is the cheap half of "what the session can
    reproduce": every close, previous close and turnover in the day's bars.

    Integral prices are dropped on purpose: quotes carry decimals (1392.05),
    counts do not (912). A name closing at exactly 912.00 would otherwise let
    "今天 912 家上涨" pass — it did, on the first live write.

    Still permissive rather than authoritative: it is a prompt to look, not a
    verdict. Measured on three real reviews of this desk: 10/10, 54/54, 42/42.
    """
    out: set[float] = set()
    try:
        frame = desk.market.daily(day)
    except Exception:  # noqa: BLE001 - no bars means nothing to add
        return out
    if frame is None or frame.empty:
        return out
    for column in ("close", "pre_close", "amount"):
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        # 整数值的价格不进集合：报价都带小数（1392.05），而家数是整数（912）。
        # 一只恰好收在 912.00 的票会让"今天 912 家上涨"判成对得上——实测发生过。
        out.update(float(v) for v in values if float(v) != float(int(v)))
    return out


def check(text: str, facts: dict | None, extra: set[float] | None = None) -> dict:
    """Which numbers in ``text`` are findable in ``facts``.

    ``facts`` is the whole facts payload; ``None`` means the day has none, and
    that is reported as "cannot check" rather than "everything is fine" — the
    same distinction every other block in this project makes.
    """
    tokens = numbers_in(text)
    # 三个分支返回同一组键。少一个键，读的人就要写 if 才能取到 counts——
    # 而"没有数字"和"没法核对"都不是异常，不该逼调用方分情况。
    if not tokens:
        return {"checked": 0, "matched": 0, "unmatched": [], "unmatched_count": 0,
                "note": "文本里没有数字"}
    if not facts:
        return {"checked": 0, "matched": 0, "unmatched": [], "unmatched_count": 0,
                "skipped": True,
                "note": "当天没有 facts 快照（reports/facts/<day>.json），无法核对——不是通过"}

    known = values_in(facts or {}, set())
    if extra:
        known |= extra
    matched: list[str] = []
    magnitude_only: list[str] = []
    unmatched: list[str] = []
    for token in tokens:
        try:
            value = float(token)
        except ValueError:
            continue
        kind = _match(value, known)
        if kind == "exact":
            matched.append(token)
        elif kind == "magnitude":
            matched.append(token)
            magnitude_only.append(token)
        else:
            unmatched.append(token)

    out = {"checked": len(tokens), "matched": len(matched),
           "unmatched": unmatched[:20], "unmatched_count": len(unmatched)}
    if magnitude_only:
        # 方向写在词里（跌 3.04%），不在符号里。数对上了，符号没核。
        out["sign_from_words"] = magnitude_only[:10]
    if unmatched:
        # 说得具体一点：这句话该去哪儿找。不写"请检查数字"，那等于没说。
        out["note"] = ("对不上的数不等于编的。实测这个台子的复盘里约三成数字本来就不在 facts："
                       "自评条数、单笔金额、别的工具的口径。要看的是**描述今天市场的那几个数**对不对得上"
                       "——查不到就换成查得到的那个，或者说明它从哪儿来。")
    return detail(text, out)


def stored(verdict: dict, day: str, *, scope: str | None = None) -> dict:
    """The part of a verdict worth keeping next to the entry.

    ``note`` is not kept: it is the same paragraph on every entry, and ten rows
    of it is 1.1 KB of duplicated advice — it belongs once on the page, not
    once per row. ``sign_from_words`` is kept, because it is evidence rather
    than advice: it says the number matched but the sign was never checked.

    Absence is meaningful. An entry written before this existed has no
    ``numbers`` key at all, which reads as "never checked" — not as "checked
    and fine". That distinction is the whole point; a verdict that collapses
    them would be worse than no verdict.
    """
    keep = ("checked", "matched", "unmatched", "unmatched_count", "sign_from_words")
    numbers = {key: verdict[key] for key in keep if key in verdict}
    if verdict.get("skipped"):
        # 有数字但当天没有快照：存"没核成"，不存"核过了没问题"。
        numbers["skipped"] = True
    out: dict = {"day": day}
    if numbers:
        out["numbers"] = numbers
    claims = verdict.get("claims") or {}
    if claims:
        # 只存计数：贴错标签的那条明细属于写的人（他要改），读的人只需要知道
        # "这篇有 1 个贴错了"。明细留在 POST 的响应里。
        # `labelled: 0` 也存——那是"看过了，没有带标签的数"，和数据里"0 是量出来的
        # 零"同一条规矩；不存的话，一页里有的行有 claims、有的没有，读的人会以为
        # 后者没查过。
        # 每个计数都留，`labelled == ok + 四类` 才在读取端可以自洽复核。
        counts = {"labelled": claims.get("labelled", 0), "ok": claims.get("ok", 0)}
        for key in ("misplaced", "variant", "unbacked", "provisional"):
            if claims.get(key):
                counts[key] = len(claims[key])
        if scope:
            # 「只核了市场那半边」必须跟着结论走，否则读的人会以为数字也核过了。
            counts["scope"] = scope
        out["claims"] = counts
    return out


def verify(desk, text: str, day: str, *, root=None, facts: dict | None = None,
           compute: bool = True) -> dict:
    """One composition point for "check this text against its day".

    Two callers need this exact answer: the write route (a note must be checked
    before it becomes memory) and the backfill (the entries already in the book
    were written before the check existed). If each composed it itself, the
    two would drift — this project has paid for that twice already.

    ``compute=False`` is the backfill's: it must read the day's snapshot, never
    synthesise one, because the account half of `facts` is read from the book as
    it stands now (see `facts.facts_for`).
    """
    if facts is None:
        from service.trading.facts import facts_for

        facts = facts_for(desk, day, root=root, compute=compute)
    verdict = check(text, facts, market_numbers(desk, day))
    verdict["claims"] = claims.scan(text, facts)

    # 盘中写的笔记，`facts` 里没有当天的日线：它是一份**某一时刻的快照**，而笔记
    # 叙述的是一条时间线（11:29 的涨停 31 家、13:25 的 36 家）。两处对不上是结构
    # 决定的，不是写错了。2026-11-04 实测：13:30 那条巡检 14 个数里 12 个"未匹配"，
    # 模型为此又写了一条 journal 去解释——一天四条盘中笔记就要写四条解释。
    #
    # 判不了就说判不了，写清什么时候能判。这和"库坏了"、"数编的"是三件事。
    latest = desk.market.latest_bar_date()
    if latest and str(day) > str(latest):
        verdict["basis"] = "intraday"
        verdict["basis_note"] = (
            f"{day} 的日线还没落库（最近一根是 {latest}），核对用的是写入当刻的快照。"
            "未匹配不代表写错：盘中笔记记的是一条时间线，快照只有一个时点。"
            "不需要为它补写解释，收盘后由 19:30 对账重核。")
    else:
        verdict["basis"] = "close"
    return verdict


def verify_labels(desk, text: str, day: str) -> dict:
    """The half of the check that survives a day with no snapshot.

    `facts` for a past day cannot be rebuilt (the account half reads today's
    book), so the numbers in an old note stay unverifiable — but **counts are
    not**: 涨停 49 家 came from bars that never changed. Returns the same shape
    as `verify(...)["claims"]`; the caller records ``scope="market_only"`` so a
    reader knows a green label check here is not a green number check.

    ``{}`` means that day has no market data either — nothing to check against.
    """
    from .facts import market_facts

    facts = market_facts(desk, day)
    if facts is None:
        return {}
    return claims.scan(text, facts)


def _match(value: float, known: set[float]) -> str | None:
    """``"exact"``, ``"magnitude"``, or None.

    The three scales are not a guess: the same session fact is written 3.04 in a
    report, 0.0304 in `facts.breadth.equal_weight_return`, and 5007.51 versus
    "5007.51" for equity. Checking all three is what keeps this from flagging
    correct prose.

    Sign is a separate question, and it is answered separately rather than
    ignored: 跌 3.04% against a stored −0.030363 is the same fact (the direction
    is a word, not a minus sign), so it matches — but it is reported as
    ``magnitude`` so nobody concludes the sign was verified when it was not.
    """
    integral = float(value).is_integer()
    for known_value in known:
        # 整数 token 只和整数事实比：家数不带小数（893、49），价格都带（1392.05、912.05）。
        # 不分开的话，相对容差在三位数上是 ±4.6，"912 家"会匹配到收在 912.05 的票。
        if integral and not float(known_value).is_integer():
            continue
        for scaled in claims.scales(known_value):
            # 比量级：跌 3.04% 与 −0.030363 是同一个事实，方向由文字表达。
            if _close(abs(value), abs(scaled)):
                return "exact" if value * scaled >= 0 else "magnitude"
    return None
