"""Human review of bounded text snapshots, independent of agent workflow history.

Checkpoints record what the developer actually reviewed. They neither approve a
blueprint nor certify checks. All paths use the same exclusions as agent tools.
"""

import difflib
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import workspaces as ws

MAX_REVIEW_FILES = 500
MAX_COMMENTS = 200
MAX_CHECKPOINT_BYTES = 20_000_000


class ReviewFile(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    token: str
    reviewed: bool = True


class AddComment(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    path: str = Field(min_length=1, max_length=4000)
    token: str
    comparison: str = Field(default="all", pattern="^(all|reviewed)$")
    side: str = Field(default="file", pattern="^(file|old|new)$")
    line: int = Field(default=0, ge=0)
    text: str = Field(min_length=1, max_length=2000)


class ResolveComment(BaseModel):
    resolved: bool


class FixRequest(BaseModel):
    version: int = Field(ge=0)
    comment_ids: list[str] = Field(min_length=1, max_length=MAX_COMMENTS)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(raw: bytes = b"", *, exists=False, mode="", issue="") -> dict:
    content = ""
    if exists and not issue:
        try:
            if b"\0" in raw:
                raise UnicodeError()
            content = raw.decode("utf-8")
        except UnicodeError:
            issue = "Binary or non-UTF-8 file. Inspect this file in your local editor."
    value = {
        "exists": exists,
        "mode": mode,
        "content": content,
        "issue": issue,
        "hash": hashlib.sha256(raw).hexdigest() if exists else "",
    }
    value["token"] = digest(value)
    return value


def source_snapshot(root: Path, path: str) -> dict:
    # Never dereference links, including links in parent directories.
    target = ws.safe_path(root, path)
    if not target.exists():
        return snapshot()
    if not target.is_file():
        return snapshot(exists=True, issue="Not a regular file; inspect locally.")
    stat = target.stat()
    mode = "100755" if stat.st_mode & 0o111 else "100644"
    if stat.st_size > ws.MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="File exceeds the 100 KB review limit."
        )
    with target.open("rb") as stream:
        raw = stream.read(ws.MAX_FILE + 1)
    if len(raw) > ws.MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="File exceeds the 100 KB review limit."
        )
    return snapshot(raw, exists=True, mode=mode)


def base_revision(task: dict) -> str:
    base = task.get("base_commit", "")
    if not re.fullmatch(r"[a-f0-9]{40,64}", base):
        raise ValueError(
            "This task has no valid base commit. Inspect its workspace locally."
        )
    return base


def base_snapshot(root: Path, base: str, path: str) -> dict:
    # --literal-pathspecs in ws.git protects unusual Git filenames.
    record = ws.git(root, "ls-tree", "-l", "-z", base, "--", path)
    if not record:
        return snapshot()
    meta, name = record.rstrip("\0").split("\t", 1)
    mode, kind, oid, size = meta.split()
    if name != path or kind != "blob" or mode == "120000":
        return snapshot(
            exists=True,
            mode=mode,
            issue="Link or non-regular base file; inspect locally.",
        )
    if int(size) > ws.MAX_FILE:
        return snapshot(
            exists=True, mode=mode, issue="Base file exceeds the 100 KB review limit."
        )
    raw = subprocess.run(
        ["git", "cat-file", "blob", oid],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    return snapshot(raw, exists=True, mode=mode)


def file_pair(root: Path, task: dict, review: dict, path: str, comparison="all"):
    current = source_snapshot(root, path)
    checkpoint = review["checkpoints"].get(path)
    previous = (
        checkpoint["file"]
        if comparison == "reviewed" and checkpoint
        else base_snapshot(root, base_revision(task), path)
    )
    return previous, current, checkpoint


def summary(root: Path, task: dict, review: dict) -> dict:
    base = base_revision(task)
    names = set(
        ws.git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base,
            "--",
        ).split("\0")
    )
    names.update(
        ws.git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    )
    names.update(review["checkpoints"])
    names.update(c["path"] for c in review["comments"])
    names = sorted(name for name in names if ws.allowed(name))
    rows, tokens = [], {}
    for path in names[:MAX_REVIEW_FILES]:
        try:
            original, current, checkpoint = file_pair(root, task, review, path)
        except ValueError as error:
            # Excluded symlinks/ignored files remain visibly unavailable, never read.
            rows.append(
                {
                    "path": path,
                    "status": "unavailable",
                    "issue": str(error),
                    "reviewed": False,
                    "since_review": False,
                    "has_checkpoint": False,
                    "token": "",
                    "additions": 0,
                    "deletions": 0,
                }
            )
            continue
        tokens[path] = current["token"]
        changed = current["token"] != original["token"]
        if (
            not changed
            and not checkpoint
            and not any(c["path"] == path for c in review["comments"])
        ):
            continue
        issue = current["issue"] or original["issue"]
        plus, minus = (
            counts(original["content"], current["content"]) if not issue else (0, 0)
        )
        review_plus, review_minus = (
            counts(checkpoint["file"]["content"], current["content"])
            if checkpoint and not issue
            else (plus, minus)
        )
        rows.append(
            {
                "path": path,
                "token": current["token"],
                "issue": issue,
                "status": "unchanged"
                if not changed
                else "deleted"
                if not current["exists"]
                else "added"
                if not original["exists"]
                else "modified",
                "reviewed": bool(
                    checkpoint
                    and checkpoint["file"]["token"] == current["token"]
                    and not issue
                ),
                "has_checkpoint": bool(checkpoint),
                "since_review": bool(
                    checkpoint and checkpoint["file"]["token"] != current["token"]
                ),
                "additions": plus,
                "deletions": minus,
                "review_additions": review_plus,
                "review_deletions": review_minus,
            }
        )
    return {
        "version": review["version"],
        "base_commit": base,
        "files": rows,
        "omitted": max(0, len(names) - MAX_REVIEW_FILES),
        "comments": [
            {**c, "outdated": tokens.get(c["path"]) != c["source_token"]}
            for c in review["comments"]
        ],
    }


def counts(before: str, after: str) -> tuple[int, int]:
    plus = minus = 0
    for tag, a, b, c, d in difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines()
    ).get_opcodes():
        if tag in {"insert", "replace"}:
            plus += d - c
        if tag in {"delete", "replace"}:
            minus += b - a
    return plus, minus


def diff_lines(before: str, after: str) -> list[dict]:
    old, new = before.splitlines(), after.splitlines()
    lines = []
    for group in difflib.SequenceMatcher(None, old, new).get_grouped_opcodes(3):
        lines.append(
            {
                "kind": "hunk",
                "text": f"@@ -{group[0][1] + 1},{group[-1][2] - group[0][1]} +{group[0][3] + 1},{group[-1][4] - group[0][3]} @@",
                "old": 0,
                "new": 0,
            }
        )
        for tag, a, b, c, d in group:
            if tag == "equal":
                lines.extend(
                    {"kind": "context", "old": i + 1, "new": j + 1, "text": old[i]}
                    for i, j in zip(range(a, b), range(c, d))
                )
            if tag in {"replace", "delete"}:
                lines.extend(
                    {"kind": "deletion", "old": i + 1, "new": 0, "text": old[i]}
                    for i in range(a, b)
                )
            if tag in {"replace", "insert"}:
                lines.extend(
                    {"kind": "addition", "old": 0, "new": j + 1, "text": new[j]}
                    for j in range(c, d)
                )
    return lines


def detail(root: Path, task: dict, review: dict, path: str, comparison: str) -> dict:
    before, current, checkpoint = file_pair(root, task, review, path, comparison)
    issue = before["issue"] or current["issue"]
    lines = diff_lines(before["content"], current["content"]) if not issue else []
    return {
        "path": path,
        "token": digest([before["token"], current["token"]]),
        "source_token": current["token"],
        "comparison": comparison,
        "baseline": "Last reviewed version"
        if comparison == "reviewed" and checkpoint
        else "Task base commit",
        "reviewed_at": checkpoint["at"] if checkpoint else None,
        "issue": issue,
        "lines": lines[:6000],
        "truncated": len(lines) > 6000,
        "old_mode": before["mode"],
        "new_mode": current["mode"],
        "old_exists": before["exists"],
        "new_exists": current["exists"],
        "old_final_newline": before["content"].endswith("\n"),
        "new_final_newline": current["content"].endswith("\n"),
        "changed": before["token"] != current["token"],
    }


def checkpoint(root: Path, task: dict, review: dict, body: ReviewFile):
    original, current, _ = file_pair(root, task, review, body.path)
    if current["token"] != body.token:
        raise HTTPException(
            409,
            "This file changed while you were reviewing it. Refresh and inspect the new diff.",
        )
    if not body.reviewed:
        review["checkpoints"].pop(body.path, None)
        return
    if current["issue"] or original["issue"]:
        raise HTTPException(
            422, "This file cannot be reviewed in the app. Inspect it locally."
        )
    size = sum(
        len(item["file"]["content"].encode())
        for path, item in review["checkpoints"].items()
        if path != body.path
    )
    if size + len(current["content"].encode()) > MAX_CHECKPOINT_BYTES:
        raise HTTPException(
            422,
            "Review checkpoints reached the 20 MB task limit. Inspect remaining files locally.",
        )
    review["checkpoints"][body.path] = {"file": current, "at": time.time()}


def add_comment(root: Path, task: dict, review: dict, body: AddComment) -> bool:
    fingerprint = digest(body.model_dump())
    existing = next((c for c in review["comments"] if c["id"] == body.id), None)
    if existing:
        if existing["request_hash"] != fingerprint:
            raise HTTPException(
                409, "This comment ID already belongs to another comment."
            )
        return False
    if len(review["comments"]) >= MAX_COMMENTS:
        raise HTTPException(422, "This task has reached the 200 review comment limit.")
    if not body.text.strip():
        raise HTTPException(422, "Write a comment before saving.")
    before, current, _ = file_pair(root, task, review, body.path, body.comparison)
    if digest([before["token"], current["token"]]) != body.token:
        raise HTTPException(
            409, "The diff changed. Refresh it and select your comment location again."
        )
    if before["issue"] or current["issue"]:
        raise HTTPException(422, "Comments require an inspectable text diff.")
    excerpt = ""
    if body.side != "file":
        lines = (before if body.side == "old" else current)["content"].splitlines()
        if not 1 <= body.line <= len(lines):
            raise HTTPException(
                422, "That line does not exist in this version of the file."
            )
        excerpt = lines[body.line - 1][:1000]
    elif body.line:
        raise HTTPException(422, "File comments cannot specify a line.")
    review["comments"].append(
        {
            **body.model_dump(exclude={"token"}),
            "request_hash": fingerprint,
            "source_token": current["token"],
            "base_token": before["token"],
            "excerpt": excerpt,
            "created_at": time.time(),
            "resolved": False,
        }
    )
    return True


def fix_prompt(review: dict, body: FixRequest) -> str:
    if body.version != review["version"]:
        raise HTTPException(
            409, "Review comments changed. Refresh and select the comments again."
        )
    selected = [
        c
        for c in review["comments"]
        if c["id"] in body.comment_ids and not c["resolved"]
    ]
    if len(selected) != len(set(body.comment_ids)):
        raise HTTPException(
            409, "A selected comment is missing or resolved. Refresh your review."
        )
    prompt = (
        "Address these human review comments in this task's existing workspace. "
        "Read the current files first: the line numbers and excerpts refer to the versions "
        "I reviewed and may have changed. Propose a focused plan for approval, implement "
        "the agreed corrections, and verify them. Explain which comments were addressed "
        "and any remaining issues.\n\n"
    )
    for c in selected:
        prompt += f"File: {json.dumps(c['path'])}\n"
        if c["line"]:
            prompt += f"{c['side']} side, line {c['line']} ({c['comparison']} comparison)\nExcerpt: {json.dumps(c['excerpt'])}\n"
        prompt += (
            f"Reviewed file version: {c['source_token'][:12]}\nRequest: {c['text']}\n\n"
        )
    if len(prompt) > 18000:
        raise HTTPException(
            422,
            "These comments exceed one message. Select fewer comments for this request.",
        )
    return prompt.strip()
