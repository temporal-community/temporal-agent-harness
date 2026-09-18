"""V2 execution receipts. No provider credentials or lifecycle decisions live here."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from temporalio import activity

from . import workspaces as ws
from .coding_models import (
    FileChange,
    FileView,
    ProjectMergeReceipt,
    ProjectMergeResult,
    SearchHit,
    ToolCall,
    ToolResult,
)
from .store import Store, data_dir, workspace


def revision(root: Path) -> str:
    # No UI 3,000-file cap: incomplete snapshots must never count as verified.
    names = sorted(
        set(
            ws.git(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split("\0")
        )
        - {""}
    )
    if len(names) > 50000:
        raise ValueError("Repository exceeds the 50,000-file verification limit")
    digest = hashlib.sha256()
    total = 0
    for name in names:
        if not ws.allowed(name):
            continue
        path = root / name
        # Include symlink identity, deleted files, modes and binary/large files,
        # without following symlinks or exposing their contents to the model.
        if any(
            p.is_symlink()
            for p in [path, *path.parents]
            if p != root and p.is_relative_to(root)
        ):
            raw = (
                ("symlink:" + os.readlink(path)).encode()
                if path.is_symlink()
                else b"symlink-parent"
            )
        elif path.is_file():
            size = path.stat().st_size
            total += size
            if total > 256_000_000:
                raise ValueError(
                    "Repository exceeds the 256 MB verification snapshot limit"
                )
            raw = path.read_bytes()
        else:
            raw = b"deleted"
        mode = (
            str(path.lstat().st_mode)
            if path.exists() or path.is_symlink()
            else "missing"
        )
        digest.update(
            json.dumps([name, mode, hashlib.sha256(raw).hexdigest()]).encode()
        )
    return digest.hexdigest()


def patch_files(root: Path, changes: list[FileChange]) -> list[str]:
    if not changes or len({c.path for c in changes}) != len(changes):
        raise ValueError("Patch needs unique file paths")
    if sum(len(c.content.encode()) for c in changes) > 500000:
        raise ValueError("Patch exceeds 500 KB")
    # Preflight every path/hash before changing any file. Each atomic replacement
    # is recoverable after a crash; a multi-file patch is not a filesystem transaction.
    for change in changes:
        current = ws.read_file(root, change.path)
        desired = hashlib.sha256(change.content.encode()).hexdigest()
        if current["hash"] not in {change.expected_hash, desired}:
            raise ValueError(f"{change.path} changed since it was read; read it again")
        if len(change.content.encode()) > ws.MAX_FILE:
            raise ValueError("File exceeds 100 KB")
    for change in changes:
        ws.write_file(root, change.path, change.content, change.expected_hash)
    return [c.path for c in changes]


def save_record(record: Path, value: dict):
    temp = record.with_suffix(".tmp")
    with temp.open("w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    temp.replace(record)


def merge_project(
    task_id: str,
    operation_id: str,
    expected_revision: str,
) -> ProjectMergeResult:
    """Journal and serialize one idempotent merge into a project checkout."""
    root = workspace(task_id)
    if len(operation_id) > 128 or not operation_id.replace("-", "").isalnum():
        return ProjectMergeResult(ok=False, error="Invalid operation ID")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_revision):
        return ProjectMergeResult(ok=False, error="Invalid accepted revision")

    journal = data_dir() / "project-merge-operations" / task_id
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    operation_lock_path = journal / f"{operation_id}.lock"
    operation_lock_path.touch(mode=0o600, exist_ok=True)
    # Concurrent delivery of the same activity must share one journal writer.
    with operation_lock_path.open("r+") as operation_lock:
        fcntl.flock(operation_lock.fileno(), fcntl.LOCK_EX)
        record = journal / f"{operation_id}.json"
        previous = json.loads(record.read_text()) if record.exists() else {}
        if previous:
            if previous["expected_revision"] != expected_revision:
                return ProjectMergeResult(
                    ok=False,
                    error="Operation ID reused with different merge arguments",
                )
            if "result" in previous:
                return ProjectMergeResult.model_validate(previous["result"])
            source_root = Path(previous["project_path"])
            base_revision = previous["base_revision"]
            entry = previous
        else:
            try:
                store = Store()
                task = store.get("tasks", task_id)
                if task.get("engine") != "v2":
                    raise ValueError("Only Coding V2 tasks support project merge")
                base_revision = task.get("base_commit", "")
                if not re.fullmatch(r"[a-f0-9]{40,64}", base_revision):
                    raise ValueError(
                        "This task has no valid base commit. Inspect its workspace locally."
                    )
                project = store.get("projects", task["project_id"])
                source_root = Path(project["path"]).expanduser().resolve()
            except (KeyError, ValueError, OSError) as error:
                return ProjectMergeResult(ok=False, error=str(error)[:1000])
            entry = {
                "expected_revision": expected_revision,
                "project_path": str(source_root),
                "base_revision": base_revision,
            }
            save_record(record, entry)

        lock_root = data_dir() / "project-merge-locks"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_name = hashlib.sha256(str(source_root).encode()).hexdigest() + ".lock"
        lock_path = lock_root / lock_name
        lock_path.touch(mode=0o600, exist_ok=True)

        # Temporal may retry an activity after its worker disappears. The OS lock
        # serializes retries and other tasks targeting this same project checkout.
        with lock_path.open("r+") as project_lock:
            fcntl.flock(project_lock.fileno(), fcntl.LOCK_EX)
            try:
                merged = ws.merge_to_project(
                    root,
                    base_revision,
                    str(source_root),
                    expected_revision,
                )
                result = ProjectMergeResult(
                    receipt=ProjectMergeReceipt(
                        operation_id=operation_id,
                        base_revision=base_revision,
                        merged_at=time.time(),
                        **merged,
                    )
                )
            except (
                ValueError,
                OSError,
                UnicodeError,
                subprocess.SubprocessError,
            ) as error:
                result = ProjectMergeResult(ok=False, error=str(error)[:1000])
            entry["result"] = result.model_dump(mode="json")
            save_record(record, entry)
            return result


@activity.defn
async def project_merge_operation(
    task_id: str,
    operation_id: str,
    expected_revision: str,
) -> ProjectMergeResult:
    """Apply an accepted revision as a retry-safe Temporal activity."""
    return await asyncio.to_thread(
        merge_project,
        task_id,
        operation_id,
        expected_revision,
    )


@activity.defn
async def coding_operation(
    task_id: str, operation_id: str, call: ToolCall
) -> ToolResult:
    root = workspace(task_id)
    if not operation_id.replace("-", "").isalnum():
        raise ValueError("Invalid operation ID")
    journal = data_dir() / "coding-operations" / task_id
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = journal / f"{operation_id}.json"
    fingerprint = hashlib.sha256(call.model_dump_json().encode()).hexdigest()
    previous = json.loads(record.read_text()) if record.exists() else {}
    if previous:
        if previous["fingerprint"] != fingerprint:
            return ToolResult(
                ok=False, error="Operation ID reused with different arguments"
            )
        if "result" in previous:
            return ToolResult.model_validate(previous["result"])
        if call.kind == "check":
            return ToolResult(
                ok=False,
                error="Command outcome is unknown after interruption; it was not run twice. Request a new verification run.",
            )
    entry = {"fingerprint": fingerprint}
    save_record(record, entry)
    result = ToolResult(operation_id=operation_id)
    try:
        if call.kind == "list":
            result.files = await asyncio.to_thread(ws.files, root)
        elif call.kind == "read":
            result.file = FileView.model_validate(
                await asyncio.to_thread(ws.read_file, root, call.path)
            )
        elif call.kind == "search":
            result.matches = [
                SearchHit.model_validate(hit)
                for hit in await asyncio.to_thread(ws.search_files, root, call.query)
            ]
        elif call.kind == "diff":
            delta = await asyncio.to_thread(ws.diff, root)
            result.patch, result.files = delta["patch"], delta["files"]
            if delta["truncated"]:
                result.error = "Diff truncated; read changed files individually"
        elif call.kind in {"patch", "edit"}:
            changes = call.changes
            if call.kind == "edit":
                # Persist the expanded replacement before writing, so a replay of
                # a completed exact edit can recognize its desired content.
                if "changes" in previous:
                    changes = [
                        FileChange.model_validate(c) for c in previous["changes"]
                    ]
                else:
                    edit = call.edit
                    current = await asyncio.to_thread(ws.read_file, root, edit.path)
                    if (
                        current["hash"] != edit.expected_hash
                        or current["content"].count(edit.old_text) != 1
                    ):
                        raise ValueError(
                            "Exact edit requires the current hash and exactly one matching occurrence"
                        )
                    changes = [
                        FileChange(
                            path=edit.path,
                            expected_hash=edit.expected_hash,
                            content=current["content"].replace(
                                edit.old_text, edit.new_text, 1
                            ),
                        )
                    ]
                entry["changes"] = [c.model_dump() for c in changes]
                save_record(record, entry)
            result.before_revision = await asyncio.to_thread(revision, root)
            result.files = await asyncio.to_thread(patch_files, root, changes)
            result.revision = await asyncio.to_thread(revision, root)
        elif call.kind == "revision":
            result.revision = await asyncio.to_thread(revision, root)
        elif call.kind == "check":
            result.before_revision = await asyncio.to_thread(revision, root)
            if result.before_revision != call.expected_revision:
                raise ValueError(
                    "Workspace changed after command approval. Review and approve the new revision."
                )
            checked = await ws.execute_check(root, call.argv)
            result.exit_code, result.output = checked["exit_code"], checked["output"]
            result.revision = await asyncio.to_thread(revision, root)
        else:
            raise ValueError("Unsupported coding operation")
    except (ValueError, OSError, UnicodeError) as error:
        result.ok, result.error = False, str(error)[:1000]
    entry["result"] = result.model_dump(mode="json")
    save_record(record, entry)
    return result
