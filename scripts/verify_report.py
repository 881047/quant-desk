"""Check that a close report is allowed to exist, and that its numbers are real.

Two failures this catches, both from 2026-10-29:

1. **A report written on an unpublished session.** The bar for 10-29 was not in
   the store; the report quoted an intraday snapshot. The facts file records the
   readiness verdict at write time, so this is now checkable after the fact.
2. **A number that no tool produced.** The report said "可买的 6 只"; the stored
   session has 23 names inside the same price cap. Nothing in the pipeline could
   tell those apart, because nothing recorded what the day actually contained.

Since P85 a third one, and it is the same sentence: **a real number under the
wrong label**. Presence is a weak test — "可买的 6 只" passes it whenever 6
appears anywhere in the facts. `service.trading.claims` re-reads the sentence as
(label, number) pairs and says which of four things happened: right, misplaced
(the number belongs to another fact), variant (another vendor's figure for the
same fact), or unbacked (that label never had that number). Claims whose frame
of reference is not the close — intraday readings, my-own-list, another day,
another report, a delta, an N-day window — are printed as INFO and not counted:
a warning that fires on correct prose is worse than no warning.

Exit codes: 0 clean, 1 failed, 2 failed on warnings (``--strict``).

Usage:
    python scripts/verify_report.py 20261029
    python scripts/verify_report.py 20261029 --strict
    python scripts/verify_report.py --all-recent 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.trading.journal_check import values_in  # ROOT 必须先上 sys.path

REPORTS = ("close", "noon", "open")

# Only counts that make a claim about *this session's tradability*. A report also
# says "2 只持仓" and "3 只候选"; those are account facts, not session counts,
# and flagging them trains the reader to ignore the warning.
BUYABLE = re.compile(r"可买[^，。；\n]{0,12}?(\d+)\s*只")
SEALED = re.compile(r"(\d+)\s*只[^，。；\n]{0,6}?(?:非一字|一字|涨停|封板|封死|开板)")


def _facts(day: str) -> dict | None:
    path = ROOT / "reports" / "facts" / f"{day}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check_day(day: str, *, strict: bool = False) -> list[tuple[str, str]]:
    """Return ``(level, message)`` pairs: level is FAIL or WARN.

    Only days that actually carry a report are audited. A day with no report and
    no facts is not a violation — asking "should I have written one" is
    `/readiness`, not this script.
    """
    problems: list[tuple[str, str]] = []
    close = ROOT / "reports" / "close" / f"{day}.md"
    intraday = [ROOT / "reports" / kind / f"{day}.md" for kind in ("noon", "open")]
    existing = [p for p in [close, *intraday] if p.exists()]
    if not existing:
        return problems

    facts = _facts(day)
    if facts is None:
        problems.append(("FAIL",
                         (f"{day} 有报告"
                          f"（{'、'.join(str(p.relative_to(ROOT)) for p in existing)}）"
                          f"但没有事实包 reports/facts/{day}.json；"
                          f"报告里的数字无从复现，先生成事实再写报告")))
        return problems

    ready = facts.get("readiness") or {}

    if not ready.get("ok"):
        code = ready.get("code")
        # An intraday note is legitimate while the session is open; a *close*
        # report is not. The code tells the two apart.
        if close.exists() or code not in ("SESSION_OPEN",):
            problems.append(("FAIL",
                             (f"{day} 的就绪状态是 {code}，"
                              # 三个目录下同名的是三份不同的东西（开盘/午间/收盘），
                              # 只印 basename 会写成"存在报告：20261102.md"三遍。
                              f"但存在报告：{'、'.join(str(p.relative_to(ROOT / 'reports')) for p in existing)}。"
                              f"{(ready.get('detail') or '')[:120]}")))

    if not close.exists():
        # Noon/open notes are written mid-session and judged against the tape as
        # it stood then. Checking them against the closing facts would be wrong
        # by construction — 13 boards had opened at 12:30, 14 by the close.
        return problems

    known = {int(n) for n in values_in(facts, set()) if float(n).is_integer()}
    text = close.read_text(encoding="utf-8", errors="replace")
    cited: set[int] = set()
    for pattern in (BUYABLE, SEALED):
        cited |= {int(m) for m in pattern.findall(text)}

    from service.trading import claims as claims_mod

    verdict = claims_mod.scan(text, facts)
    # `claims` 已经把带时刻/引用/样本口径的读数归到 provisional（下面按 INFO 报）。
    # 存在性检查若照旧把它们算成"事实包里没有"，同一句话会同时得到 WARN 与 INFO：
    # 2026-11-13 的「cap 内 7 只涨停，14:42:25 全部 sealed」是盘中读数，报告写明了
    # 时刻、`claims` 也认了，而这里仍在报警——对着正确的话报警，只会教人忽略报警。
    excused = {int(n) for item in verdict.get("provisional") or []
               for n in re.findall(r"\d+", str(item.get("span") or ""))}
    unknown = sorted(cited - known - excused)
    if unknown:
        level = "FAIL" if strict else "WARN"
        affordable = facts.get("affordable") or {}
        problems.append((level, (f"close/{day}.md 的成交可行性写成 "
                                 f"{'/'.join(str(n) for n in unknown)} 只，"
                                 f"事实包里没有这个数（in_cap={affordable.get('in_cap')}, "
                                 f"one_word={affordable.get('one_word')}, "
                                 f"收盘可买={affordable.get('buyable_at_close')}）。")))

    # 存在性是弱检查："可买的 6 只" 只要 6 在 facts 的某个角落出现就过关，而那句话
    # 错在把 6 贴到了"可买"上。所以再按标签核一遍：`claims` 说得出"数是真的、标签错
    # 了"（893 家跌停，跌停其实是 93），也说得出"这个标签下没有这个数"。
    level = "FAIL" if strict else "WARN"
    def _vendors(item: dict) -> str:
        other = item.get("vendors") or {}
        if not other:
            return ""
        return f"（同日另有 {'、'.join(f'{k} {v}' for k, v in other.items())}；引用要写明用哪一家）"

    for item in verdict.get("variant") or []:
        problems.append((level, (f"close/{day}.md 写「{item['span']}」，"
                                 f"这是 {item['but_that_is']['path']} 的口径；"
                                 f"{item['label']} 用同花顺口径是 {item['should_be']}。"
                                 f"改成哪个都行，但要写明来源。")))
    for item in verdict.get("misplaced") or []:
        problems.append((level, (f"close/{day}.md 写「{item['span']}」，"
                                 f"但 {item['label']} 当天是 "
                                 f"{item['should_be']}；{item['wrote']} 是 "
                                 f"{item['but_that_is']['path']} 的值——数是真的，标签错了。"
                                 f"{_vendors(item)}")))
    for item in verdict.get("unbacked") or []:
        problems.append((level, (f"close/{day}.md 写「{item['span']}」，"
                                 f"事实包在 {item['label']} 下没有 {item['wrote']}"
                                 f"（应为 {item['should_be']}）。{_vendors(item)}")))
    # 带时刻/引用/样本口径的读数照报，但不算问题：它可能正是 10-29 那条"可买的 6 只"
    # （错），也可能是一条合法的盘中记录（"跌停 62 家（14:58）"）。脚本不替人判，
    # 只把它摆出来并说明是哪一种口径。
    why = {"intraday": "盘中读数", "sample": "我那张清单", "elsewhere": "别的交易日",
           "quoted": "引用了另一份报告", "delta": "变化量而非水平值",
           "window": "N 日累计而非 1 日"}
    for item in verdict.get("provisional") or []:
        problems.append(("INFO", (f"close/{day}.md 写「{item['span']}」，"
                                  f"{why.get(item['why'], item['why'])}；若是收盘口径则 "
                                  f"{item['label']} 应为 {item['should_be']}。")))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", nargs="?", help="YYYYMMDD")
    parser.add_argument("--all-recent", type=int, default=0,
                        help="check the N most recent days that have a facts file")
    parser.add_argument("--strict", action="store_true", help="treat WARN as failure")
    args = parser.parse_args(argv)

    if args.all_recent:
        folder = ROOT / "reports" / "facts"
        days = sorted(p.stem for p in folder.glob("*.json"))[-args.all_recent:]
    elif args.day:
        days = [args.day]
    else:
        parser.error("give a day or --all-recent N")

    failed = 0
    for day in days:
        problems = check_day(day, strict=args.strict)
        if not problems:
            print(f"[ok]   {day}")
            continue
        for level, message in problems:
            print(f"[{level}] {day}: {message}")
            if level == "FAIL":
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
