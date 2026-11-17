"""Versioned playbook.

Rules change, but never silently. A new version is proposed as a draft, then
activated explicitly; every version keeps its text and note. The agent may read
the playbook and propose a draft, but activation requires a one-time challenge
issued by a human through the local CLI and bound to the exact draft content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from service import settings
from service.adapters.jsonio import write_text_atomic

_META_LOCK = threading.RLock()

MAX_CHARS = settings.integer("playbook.max_chars")
MIN_CHARS = settings.integer("playbook.min_chars")


class PlaybookError(ValueError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _meta_path(root: Path) -> Path:
    return root / "playbook" / "meta.json"


def ensure_meta(root: Path) -> dict:
    """Create meta.json from an existing v1.md, or initialise an empty book."""
    path = _meta_path(root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    folder = root / "playbook"
    folder.mkdir(parents=True, exist_ok=True)
    versions = []
    v1 = folder / "v1.md"
    if v1.exists():
        versions.append({
            "version": "v1",
            "file": "v1.md",
            "created_at": _now(),
            "note": "initial",
        })
        active = "v1"
    else:
        active = None
    meta = {"active": active, "versions": versions, "drafts": []}
    save_meta(root, meta)
    return meta


def save_meta(root: Path, meta: dict) -> None:
    path = _meta_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def current(root: Path) -> dict:
    meta = ensure_meta(root)
    active = meta.get("active")
    if not active:
        return {"version": None, "text": "", "updated_at": None}
    entry = next((v for v in meta["versions"] if v["version"] == active), None)
    if entry is None:
        return {"version": None, "text": "", "updated_at": None}
    text = (root / "playbook" / entry["file"]).read_text(encoding="utf-8")
    return {"version": active, "text": text, "updated_at": entry.get("created_at"),
            "note": entry.get("note", "")}


def history(root: Path) -> dict:
    meta = ensure_meta(root)
    return {"active": meta.get("active"), "versions": meta["versions"],
            "drafts": meta.get("drafts", []),
            "superseded": meta.get("superseded", [])}


def propose(root: Path, text: str, note: str = "", author: str = "agent") -> dict:
    text = text.strip()
    if len(text) < MIN_CHARS:
        raise PlaybookError(f"playbook text must be >= {MIN_CHARS} characters")
    if len(text) > MAX_CHARS:
        raise PlaybookError(f"playbook text must be <= {MAX_CHARS} characters")

    with _META_LOCK:
        meta = ensure_meta(root)
        draft_id = f"pb-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
        rel = f"drafts/{draft_id}.md"
        path = root / "playbook" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # The playbook is read on every turn; a truncated one is a shorter rule
        # set that still parses, so nothing downstream would notice.
        write_text_atomic(path, text)

        draft = {"draft_id": draft_id, "file": rel, "created_at": _now(),
                 "note": note, "author": author, "chars": len(text)}
        meta.setdefault("drafts", []).append(draft)
        save_meta(root, meta)
        # 提草案时就告诉作者「这份是补丁」：等到激活那一刻才发现，四天就白排了。
        return {**draft, "review": draft_review(root, draft)}


def _find_draft(meta: dict, draft_id: str) -> dict | None:
    return next((d for d in meta.get("drafts", []) if d["draft_id"] == draft_id), None)


def _draft_sha256(root: Path, draft: dict) -> str:
    return hashlib.sha256((root / "playbook" / draft["file"]).read_bytes()).hexdigest()


def _challenge_sha256(challenge: str) -> str:
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


_SECTION = re.compile(r"^##\s+(\d+(?:\.\d+)?)\.?\s")


def _sections(text: str) -> list[str]:
    """Top-level section numbers in order of appearance, e.g. ['0','1','9','9.7']."""
    found = []
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            found.append(match.group(1))
    return found


_SECTION_HEAD = re.compile(r"^##\s+([0-9]+(?:\.[0-9]+)?)\.?\s*(.*)$")

#: 一次读回来的正文预算，单位是 UTF-8 字节。
#:
#: 宿主对超过 16,000 B 的工具结果**整份丢弃**，插件的 shrink ladder 于是先把
#: 最长的字符串切短。规则书的 `text` 正好是最长的那个：2026-11-13 实测
#: `GET /playbook` 是 16,301 B，被压到 3,658 B，`text` 只剩 241 字（§0 的前两条），
#: 而 `truncated` 仍然是 false、只有 `degraded=true`——§1 之后的规则全部消失，
#: 连一句提示都没有。这就是「两套仓位口径并存四天」的来源：读的那一端从来
#: 没读到 §2。所以正文按字节预算切片，切了必须写进 `truncated` 与 `next_section`。
TEXT_BUDGET_BYTES = 14_200


def index_sections(text: str) -> list[dict]:
    """Split the rulebook by its `## N.` headers. The preamble stays as ``pre``."""
    rows: list[dict] = []
    head = {"n": "", "title": "开头", "lines": []}
    for line in text.splitlines():
        match = _SECTION_HEAD.match(line)
        if match:
            rows.append(head)
            head = {"n": match.group(1), "title": match.group(2).strip(), "lines": [line]}
        else:
            head["lines"].append(line)
    rows.append(head)
    out = []
    for row in rows:
        body = "\n".join(row["lines"]).strip("\n")
        if not body:
            continue
        out.append({
            "n": row["n"] or "pre",
            "title": row["title"][:40],
            "chars": len(body),
            "bytes": len(body.encode("utf-8")),
            "text": body,
        })
    return out


def select_sections(sections: list[dict], names: set[str] | None = None,
                    budget_bytes: int = TEXT_BUDGET_BYTES) -> dict:
    """Take whole sections in order until the byte budget is spent.

    按章切，不截字符串：截在一句话中间，读者拿到的是半条规则，而半条规则和
    没有规则一样有害——它会照着做。
    """
    wanted = [row for row in sections if names is None or row["n"] in names]
    picked: list[dict] = []
    used = 0
    for row in wanted:
        if picked and used + row["bytes"] > budget_bytes:
            break
        picked.append(row)
        used += row["bytes"]
    covered = {row["n"] for row in picked}
    remaining = [row["n"] for row in wanted if row["n"] not in covered]
    return {
        "text": "\n\n".join(row["text"] for row in picked),
        "included": [row["n"] for row in picked],
        "bytes": used,
        "next_section": remaining[0] if remaining else None,
        "remaining": remaining,
    }


def draft_review(root: Path, draft: dict, current_text: str | None = None) -> dict:
    """Would activating this draft shrink the rulebook?

    草案是自由文本，作者是模型；而 ``activate`` 把草稿**整份复制**成新版本。
    2026-11-09、11-10 两份草案都只写了新增条款（§9.6 / §9.7，3.7–4.4 KB），
    现行 v2 是 12.9 KB、§0–§9 十章。激活其中任何一份，规则书会当场变成两章的
    碎片：账户事实、仓位上限、跳空门、赔率门全部消失，而之后每个窗口仍会引用
    一份已经不存在的条款。这件事没有发生，靠的是人没批，不是代码拦住了。

    所以这里把「会不会丢章节」变成可读的字段，激活前先判一次。
    """
    path = root / "playbook" / draft["file"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if current_text is None:
        current_text = current(root).get("text") or ""
    active = _sections(current_text)
    proposed = _sections(text)
    missing = [section for section in active if section not in proposed]
    covered = len(active) - len(missing)
    # 只在现行版本真有章节结构时才判「补丁」；一份从裸文本起步的第一版
    # （测试与早期 v1）不适用，否则会把合法的第一次激活挡在门外。
    fragment = len(active) >= 3 and covered * 2 < len(active)
    return {
        "draft_chars": len(text),
        "active_chars": len(current_text),
        "active_sections": active,
        "draft_sections": proposed,
        "missing_sections": missing,
        "is_fragment": fragment,
    }


def issue_challenge(root: Path, draft_id: str) -> dict:
    """Issue a human-only, one-time activation challenge for the exact draft bytes.

    Only the digest is persisted. Calling this again for the same draft revokes
    the previous challenge. This function is deliberately exposed through the
    local CLI, not through the token-authenticated HTTP API.
    """
    with _META_LOCK:
        meta = ensure_meta(root)
        draft = _find_draft(meta, draft_id)
        if draft is None:
            raise PlaybookError(f"unknown draft: {draft_id}")
        draft_hash = _draft_sha256(root, draft)
        challenge = secrets.token_urlsafe(32)
        challenges = meta.setdefault("activation_challenges", {})
        challenges[draft_id] = {
            "draft_sha256": draft_hash,
            "challenge_sha256": _challenge_sha256(challenge),
            "created_at": _now(),
        }
        save_meta(root, meta)
    return {"draft_id": draft_id, "draft_sha256": draft_hash, "challenge": challenge}


def activate(root: Path, draft_id: str, challenge: str) -> dict:
    with _META_LOCK:
        meta = ensure_meta(root)
        draft = _find_draft(meta, draft_id)
        if draft is None:
            raise PlaybookError("draft is unknown or already activated")
        # 先判会不会丢章节，再验挑战：失败的激活不该烧掉一次性挑战。
        review = draft_review(root, draft)
        if review["is_fragment"]:
            lost = "、".join(f"§{section}" for section in review["missing_sections"][:12])
            raise PlaybookError(
                f"{draft_id} 是补丁不是完整规则书：它只有 {len(review['draft_sections'])} 章 / "
                f"{review['draft_chars']} B，现行版本有 {len(review['active_sections'])} 章 / "
                f"{review['active_chars']} B。激活会删掉 {lost}。"
                "请用 propose 提交包含全部章节的全文，再激活这一份。"
            )
        record = meta.get("activation_challenges", {}).get(draft_id)
        if not record:
            raise PlaybookError("activation requires a human-issued one-time challenge")
        if not isinstance(challenge, str) or not secrets.compare_digest(
                _challenge_sha256(challenge), record["challenge_sha256"]):
            raise PlaybookError("activation challenge is invalid or already used")
        if not secrets.compare_digest(_draft_sha256(root, draft), record["draft_sha256"]):
            raise PlaybookError("draft content changed after the challenge was issued")

        existing = [int(v["version"][1:]) for v in meta["versions"]
                    if v["version"].startswith("v") and v["version"][1:].isdigit()]
        version = f"v{max(existing, default=0) + 1}"
        src = root / "playbook" / draft["file"]
        text = src.read_text(encoding="utf-8")
        dest_rel = f"{version}.md"
        write_text_atomic(root / "playbook" / dest_rel, text)

        entry = {"version": version, "file": dest_rel, "created_at": _now(),
                 "note": draft.get("note", ""), "author": draft.get("author", "agent")}
        meta["versions"].append(entry)
        meta["active"] = version
        meta["drafts"] = [d for d in meta["drafts"] if d["draft_id"] != draft_id]
        del meta["activation_challenges"][draft_id]
        if not meta["activation_challenges"]:
            meta.pop("activation_challenges")
        save_meta(root, meta)
        try:
            src.unlink()
        except OSError:
            pass
        return entry


def supersede(root: Path, draft_id: str, by_version: str, note: str = "") -> dict:
    """一份草案的内容已被某个正式版本整份吸收，把它移出待批队列。

    草案是**累积快照**：11-05 那份含着 11-04 的 §7，11-06 那份含着 §7+§8。
    激活最新一份之后，前几份的内容已经生效，但它们仍留在 ``drafts`` 里，
    ``/playbook`` 的 ``draft_count`` 于是长期大于 0——读者看到「还有 2 份等着批」，
    而批了不会有任何变化。这跟当初「草案根本看不见」是同一个毛病，只是方向相反。

    内容是否真被吸收无法自动判定（自由文本），所以这是显式动作：``by_version``
    必须真实存在，``note`` 要写清凭什么说它已被吸收。文件不删——它是证据。
    """
    with _META_LOCK:
        meta = ensure_meta(root)
        draft = _find_draft(meta, draft_id)
        if draft is None:
            raise PlaybookError(f"unknown draft: {draft_id}")
        versions = {v["version"] for v in meta["versions"]}
        if by_version not in versions:
            raise PlaybookError(f"unknown version: {by_version}")
        meta["drafts"] = [d for d in meta["drafts"] if d["draft_id"] != draft_id]
        record = {
            "draft_id": draft_id,
            "file": draft["file"],
            "created_at": draft.get("created_at"),
            "note": draft.get("note", ""),
            "superseded_at": _now(),
            "by_version": by_version,
            "supersede_note": note,
        }
        meta.setdefault("superseded", []).append(record)
        save_meta(root, meta)
        return record


def _main() -> None:
    parser = argparse.ArgumentParser(description="Human-only playbook activation helper")
    parser.add_argument("command", choices=["review", "challenge", "activate", "supersede"])
    parser.add_argument("draft_id")
    parser.add_argument("--challenge", default="",
                        help="activate 用：challenge 子命令打印出来的那串")
    parser.add_argument("--by", dest="by_version", default="",
                        help="supersede 用：内容已被哪个正式版本吸收")
    parser.add_argument("--note", default="", help="supersede 用：凭什么判定已被吸收")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        if args.command == "review":
            draft = _find_draft(ensure_meta(args.root), args.draft_id)
            if draft is None:
                raise PlaybookError(f"unknown draft: {args.draft_id}")
            result = {"draft_id": args.draft_id, **draft_review(args.root, draft)}
        elif args.command == "challenge":
            result = issue_challenge(args.root, args.draft_id)
        elif args.command == "activate":
            if not args.challenge:
                parser.error("activate 需要 --challenge <挑战串>")
            result = activate(args.root, args.draft_id, args.challenge)
        else:
            if not args.by_version:
                parser.error("supersede 需要 --by <version>")
            result = supersede(args.root, args.draft_id, args.by_version, args.note)
    except PlaybookError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    _main()
