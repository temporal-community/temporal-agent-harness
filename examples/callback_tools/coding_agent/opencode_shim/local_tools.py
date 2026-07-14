"""Local executors for the coding agent's callback tools — the actual work, on THIS machine.

The agent (a durable Temporal workflow) has no disk; when it calls ``bash``/``read``/``write``/
``edit``/``grep``/``glob`` it publishes a ``callback_requested`` event and parks. The shim, running
on the user's laptop, runs the operation here against the project directory and posts the result
back so the agent resumes.

Every function takes the resolved project ``root`` plus the tool's model-facing arguments and
returns a ``str`` — the shape the callback tools declare (see ``coding_agent/tools.py``). A raised
exception is turned into a tool *error* result by :func:`execute` (the turn continues; the model
sees the error). Paths are confined to the project root.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import os
import re
from pathlib import Path
from typing import Any


def unified_diff(before: str, after: str, path: str) -> str:
    """A standard unified diff of ``before`` -> ``after``, for OpenCode's diff viewer."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )

# Directories never walked by grep/glob (and skipped when listing), matching the shim's file
# browse defaults — big, noisy, or not the user's source.
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build"}

MAX_MATCHES = 200
BASH_TIMEOUT_SECONDS = 120


def _resolve(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under the project ``root``, refusing paths that escape it."""
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel!r} escapes the project root")
    return target


async def _bash(root: Path, command: str) -> tuple[str, int]:
    """Run ``command`` in the project root; return (combined output, exit code)."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=BASH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"command timed out after {BASH_TIMEOUT_SECONDS}s")
    return out.decode("utf-8", errors="replace"), proc.returncode if proc.returncode is not None else -1


def _read(root: Path, file_path: str) -> str:
    return _resolve(root, file_path).read_text(encoding="utf-8")


def _write(root: Path, file_path: str, content: str) -> str:
    target = _resolve(root, file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    verb = "overwrote" if existed else "wrote"
    return f"{verb} {len(content)} characters to {file_path}"


def _edit(root: Path, file_path: str, old_string: str, new_string: str) -> tuple[str, str]:
    """Apply the edit; return ``(confirmation, unified_diff)``."""
    target = _resolve(root, file_path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {file_path}")
    if count > 1:
        raise ValueError(
            f"old_string is not unique in {file_path} (found {count} times); add more context"
        )
    after = text.replace(old_string, new_string, 1)
    target.write_text(after, encoding="utf-8")
    return f"edited {file_path}", unified_diff(text, after, file_path)


def _iter_files(root: Path):
    """Yield project files (posix-relative Path, absolute Path), skipping SKIP_DIRS."""
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            abs_path = Path(dirpath) / name
            yield abs_path.relative_to(root_resolved).as_posix(), abs_path


def _grep(root: Path, pattern: str) -> tuple[str, int]:
    """Search file contents; return ``(rendered_matches, match_count)``."""
    regex = re.compile(pattern)
    matches: list[str] = []
    for rel, abs_path in _iter_files(root):
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(matches) >= MAX_MATCHES:
                    matches.append(f"... (truncated at {MAX_MATCHES} matches)")
                    return "\n".join(matches), MAX_MATCHES
    return ("\n".join(matches), len(matches)) if matches else ("(no matches)", 0)


def _glob(root: Path, pattern: str) -> tuple[str, int]:
    """Match file paths; return ``(rendered_paths, file_count)``."""
    hits: list[str] = []
    for rel, _abs in _iter_files(root):
        if fnmatch.fnmatch(rel, pattern):
            hits.append(rel)
            if len(hits) >= MAX_MATCHES:
                hits.sort()
                return "\n".join(hits) + f"\n... (truncated at {MAX_MATCHES} files)", MAX_MATCHES
    hits.sort()
    return ("\n".join(hits), len(hits)) if hits else ("(no files match)", 0)


async def _git(root: Path, *args: str) -> tuple[int, str]:
    """Run a git command in ``root``; return (exit_code, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return (proc.returncode if proc.returncode is not None else -1), out.decode("utf-8", "replace")


async def git_file_diffs(root: Path) -> list[dict[str, Any]]:
    """Working-tree changes vs HEAD as ``{file, before, after, additions, deletions}`` dicts —
    what the agent has changed. Returns ``[]`` if ``root`` isn't a git repo."""
    code, _ = await _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return []
    code, status = await _git(root, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return []
    diffs: list[dict[str, Any]] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:  # rename: take the new path
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        code_b, before = await _git(root, "show", f"HEAD:{path}")
        if code_b != 0:
            before = ""  # new / untracked file
        try:
            after = _resolve(root, path).read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            after = ""  # deleted / binary / escapes root
        if before == after:
            continue
        hunk = [
            ln
            for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
            if not ln.startswith(("+++", "---", "@@"))
        ]
        diffs.append(
            {
                "file": path,
                "before": before,
                "after": after,
                "additions": sum(1 for ln in hunk if ln.startswith("+")),
                "deletions": sum(1 for ln in hunk if ln.startswith("-")),
            }
        )
    return diffs


async def execute(
    root: Path, tool_name: str, tool_input: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Run one callback tool locally.

    Returns ``(output, metadata, error)``: on success ``(output_str, metadata, None)``; on failure
    ``(None, None, error_message)`` — the error is sent back to the agent as the tool's error result
    (the turn continues; the model sees it). ``output`` is the result the model reads; ``metadata``
    carries the extra keys each OpenCode tool CARD renders its result from (they differ per tool):
    bash -> ``output``/``exit``, edit -> ``diff``, write -> ``diagnostics`` (presence enables the
    content preview), grep -> ``matches``, glob -> ``count``.
    """
    try:
        if tool_name == "bash":
            output, exit_code = await _bash(root, tool_input["command"])
            return output, {"output": output, "exit": exit_code}, None
        if tool_name == "read":
            return _read(root, tool_input["file_path"]), {}, None
        if tool_name == "write":
            # `diagnostics` present (even empty) makes OpenCode's Write card show input.content.
            return _write(root, tool_input["file_path"], tool_input["content"]), {"diagnostics": []}, None
        if tool_name == "edit":
            msg, diff = _edit(
                root, tool_input["file_path"], tool_input["old_string"], tool_input["new_string"]
            )
            return msg, {"diff": diff}, None
        if tool_name == "grep":
            out, count = _grep(root, tool_input["pattern"])
            return out, {"matches": count}, None
        if tool_name == "glob":
            out, count = _glob(root, tool_input["pattern"])
            return out, {"count": count}, None
        return None, None, f"this client does not implement the tool {tool_name!r}"
    except Exception as e:  # noqa: BLE001 — any local failure becomes a tool error result
        return None, None, f"{type(e).__name__}: {e}"
