"""Answer a pending alert handoff, then mail it.

The monitor runs unattended and cannot reach a model, so it hands the *question*
to this workspace instead. This is the command-line face of
``service/trading/handoff.py``; the same operations are exposed as
``GET /monitor/handoff`` and ``POST /monitor/handoff/answer`` for the agent to
call without a shell.

    python scripts/handoff.py pending
    python scripts/handoff.py show 20261027-093012        # 这就是要发给它的那段话
    python scripts/handoff.py answer 20261027-093012 "三句话的解读"

`answer` writes the analysis back into the handoff, marks it done, and sends the
mail. Nothing here decides anything either: the rules produced the facts, the
agent produced the sentences, this sends them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.trading import handoff as handoff_mod

# Kept as module-level names because the renderer is imported by tests and by
# anything that wants the exact mail body without sending it.
_render_text = handoff_mod.render_text
_render_html = handoff_mod.render_html


def cmd_pending(args) -> int:
    rows = handoff_mod.pending()
    if not rows:
        print("没有待解读的提醒。")
        return 0
    print(f"待解读 {len(rows)} 条：\n")
    for row in rows:
        print(f"  {row['id']}  {row['created_at']}  {row['alerts']} 项触发"
              f"  {row['positions']} 只持仓")
    print("\n看提示词：python scripts/handoff.py show <id>")
    return 0


def cmd_show(args) -> int:
    doc = handoff_mod.load(args.id)
    if doc is None:
        print(f"[fail] 没有 {args.id}")
        return 2
    print(doc.get("prompt", ""))
    return 0


def cmd_answer(args) -> int:
    if handoff_mod.load(args.id) is None:
        print(f"[fail] 没有 {args.id}")
        return 2
    # ``sys.stdin`` is None when the interpreter has no console (pythonw, a
    # scheduled task). ``None.isatty()`` raised AttributeError, so the operator saw
    # a traceback instead of "the reading is empty".
    piped = sys.stdin is not None and not sys.stdin.isatty()
    text = args.text or (sys.stdin.read().strip() if piped else "")
    if not (text or "").strip():
        print("[fail] 解读内容为空")
        return 2
    try:
        result = handoff_mod.answer(args.id, text, dry_run=args.dry_run,
                                    force=args.force)
    except (ValueError, RuntimeError) as exc:
        print(f"[fail] {exc}")
        return 2
    if args.dry_run:
        print(result["text"])
        print(f"\n[dry-run] 主题：{result['subject']}")
        return 0
    if result.get("sent"):
        print(f"[sent] {result['subject']}")
        return 0
    status = result.get("status")
    if status == "send_unknown":
        print("[unknown] 上一次投递结果未知，邮件可能已经发出；不会自动重发。")
        if result.get("error"):
            print(f"  {result['error']}")
        print("  先查邮箱：确认没收到再 `answer --force`；"
              "若认领是进程崩溃留下的，用 `release` 释放后再答。")
        return 3
    if status == "sending":
        print("[busy] 另一个进程正在发送这封邮件；稍后用 `pending` 确认，不要重复提交。")
        return 3
    if result.get("already_answered"):
        print("[ok] 已经回复过，未重复发送。")
        return 0
    if status == "dropped":
        print("[ok] 该提醒已丢弃，未发送。")
        return 0
    print(f"[fail] 未发送：{result}")
    return 2


def cmd_release(args) -> int:
    if not args.yes:
        print("[fail] 释放认领等于承认投递结果未知，之后仍可 force 重发；"
              "确认请加 --yes")
        return 2
    try:
        out = handoff_mod.release_lock(args.id, note=args.note or "")
    except KeyError:
        print(f"[fail] 没有 {args.id}")
        return 2
    except RuntimeError as exc:
        print(f"[fail] {exc}")
        return 2
    lock = out.get("lock") or {}
    age = lock.get("age_seconds")
    detail = f"（认领已存在 {age} 秒，pid={lock.get('pid')}）" if age is not None else ""
    print(f"[ok] 已释放 {args.id} 的发送认领{detail}；状态标记为 send_unknown，不会自动重发")
    return 0


def cmd_drop(args) -> int:
    try:
        handoff_mod.drop(args.id, reason=args.reason or "")
    except KeyError:
        print(f"[fail] 没有 {args.id}")
        return 2
    except RuntimeError as exc:
        print(f"[fail] {exc}")
        return 2
    print(f"[ok] {args.id} 已丢弃（不发信）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="处理待解读的提醒并发出邮件",
        epilog="投递结果未知（send_unknown）时不要重发：先查邮箱；确认没收到用"
               " `answer <id> --force`；若是进程崩溃留下的认领，用"
               " `release <id> --yes` 释放后再答。")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pending", help="列出待解读的提醒").set_defaults(func=cmd_pending)

    show = sub.add_parser("show", help="打印提示词")
    show.add_argument("id")
    show.set_defaults(func=cmd_show)

    answer = sub.add_parser("answer", help="写入解读并发送邮件")
    answer.add_argument("id")
    answer.add_argument("text", nargs="?", default="")
    answer.add_argument("--dry-run", action="store_true")
    answer.add_argument("--force", action="store_true",
                        help="上一次投递结果未知、且已确认没收到时，才允许重发")
    answer.set_defaults(func=cmd_answer)

    release = sub.add_parser("release", help="释放崩溃遗留的发送认领（不重发）")
    release.add_argument("id")
    release.add_argument("--note", default="", help="为什么判断这次投递没完成")
    release.add_argument("--yes", action="store_true", help="明确确认")
    release.set_defaults(func=cmd_release)

    drop = sub.add_parser("drop", help="丢弃，不发信")
    drop.add_argument("id")
    drop.add_argument("--reason", default="", help="为什么丢弃（写进文档，便于日后区分误报与决定不发）")
    drop.set_defaults(func=cmd_drop)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
