"""Backend-agnostic implementations of the coding tools — the actual work against a project ``root``.

Shared by BOTH coding-agent examples, which differ only in WHERE these run:

  * ``examples/callback_tools/coding_agent`` — its OpenCode shim calls these on the USER's laptop to
    fulfill callback tools (via ``opencode_shim/local_tools.py``'s ``execute``).
  * ``examples/sandbox_tools/coding_agent`` — its ``@agent.activity_tool_defn(sandboxed=True)`` tools
    call these INSIDE a cloud sandbox, against a project that lives in the box.

Every function takes the resolved project ``root`` plus the tool's arguments and returns a plain
result (``str`` or a small tuple). No harness/remote imports and only the stdlib, so this module is
cheap to bake into the sandbox image and safe to import anywhere. Paths are confined to ``root``.
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
    """A standard unified diff of ``before`` -> ``after`` (used for the OpenCode diff viewer and the
    sandboxed ``edit`` tool's result)."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


# Directories never walked by grep/glob (big, noisy, or not the user's source).
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build"}

MAX_MATCHES = 200
BASH_TIMEOUT_SECONDS = 120


def resolve_in_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under the project ``root``, refusing paths that escape it."""
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel!r} escapes the project root")
    return target


async def bash_exec(root: Path, command: str) -> tuple[str, int]:
    """Run ``command`` in the project root; return (combined stdout+stderr, exit code)."""
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


def read_file(root: Path, file_path: str) -> str:
    return resolve_in_root(root, file_path).read_text(encoding="utf-8")


def write_file(root: Path, file_path: str, content: str) -> str:
    target = resolve_in_root(root, file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    verb = "overwrote" if existed else "wrote"
    return f"{verb} {len(content)} characters to {file_path}"


def edit_file(root: Path, file_path: str, old_string: str, new_string: str) -> tuple[str, str]:
    """Apply the edit; return ``(confirmation, unified_diff)``."""
    target = resolve_in_root(root, file_path)
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


def iter_files(root: Path):
    """Yield project files (posix-relative str, absolute Path), skipping SKIP_DIRS."""
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            abs_path = Path(dirpath) / name
            yield abs_path.relative_to(root_resolved).as_posix(), abs_path


def grep_files(root: Path, pattern: str) -> tuple[str, int]:
    """Search file contents; return ``(rendered_matches, match_count)``."""
    regex = re.compile(pattern)
    matches: list[str] = []
    for rel, abs_path in iter_files(root):
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


def glob_files(root: Path, pattern: str) -> tuple[str, int]:
    """Match file paths; return ``(rendered_paths, file_count)``."""
    hits: list[str] = []
    for rel, _abs in iter_files(root):
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
    what the agent has changed. Returns ``[]`` if ``root`` isn't a git repo. (Used by the callback
    example's OpenCode diff viewer.)"""
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
            after = resolve_in_root(root, path).read_text(encoding="utf-8")
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
