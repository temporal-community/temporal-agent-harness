"""Repository boundaries and idempotent writes; host commands require a user gate."""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from .store import data_dir, workspace

MAX_FILE = 100_000
DENIED_PARTS = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".ssh",
    ".aws",
    ".gnupg",
}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[:1000] or "Git command failed")
    return result.stdout


def allowed(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        bool(parts)
        and not Path(relative).is_absolute()
        and all(
            p not in DENIED_PARTS
            and p not in {"..", "."}
            and not p.lower().startswith(".env")
            and not p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
            and p.lower()
            not in {
                "credentials",
                "credentials.json",
                "secrets.json",
                "secrets.yaml",
                "secrets.yml",
                "secrets.toml",
                ".npmrc",
                ".pypirc",
                ".netrc",
                ".git-credentials",
                "id_rsa",
                "id_ed25519",
            }
            for p in parts
        )
    )


def safe_path(root: Path, relative: str) -> Path:
    if not allowed(relative):
        raise ValueError(
            "Path is outside the workspace or excluded as a secret/generated file"
        )
    path = root / relative
    for ancestor in [path, *path.parents]:
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("Symlinks are not supported by workspace tools")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes workspace")
    # Ignored local files (including provider credentials) are never tool inputs.
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative],
        cwd=root,
        capture_output=True,
    )
    if ignored.returncode == 0:
        raise ValueError("Ignored files are excluded")
    return path


def files(root: Path) -> list[str]:
    names = git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ).split("\0")
    return sorted({p for p in names if allowed(p) and not (root / p).is_symlink()})[
        :3000
    ]


def read_file(root: Path, relative: str) -> dict:
    path = safe_path(root, relative)
    if not path.is_file():
        return {"path": relative, "exists": False, "hash": "", "content": ""}
    if path.stat().st_size > MAX_FILE:
        raise ValueError("File exceeds the 100 KB text limit")
    raw = path.read_bytes()
    if b"\0" in raw:
        raise ValueError("Binary file")
    return {
        "path": relative,
        "exists": True,
        "hash": hashlib.sha256(raw).hexdigest(),
        "content": raw.decode("utf-8"),
    }


def write_file(root: Path, relative: str, content: str, expected_hash: str) -> dict:
    path = safe_path(root, relative)
    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE:
        raise ValueError("File exceeds the 100 KB text limit")
    current = read_file(root, relative)
    digest = hashlib.sha256(raw).hexdigest()
    # Recovery after write succeeded but activity completion was lost.
    if current["exists"] and current["hash"] == digest:
        return {"path": relative, "hash": digest, "unchanged": True}
    if current["hash"] != expected_hash:
        raise ValueError(
            "File changed since it was read. Read it again before editing."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp = Path(tmp.name)
    if path.exists():
        temp.chmod(path.stat().st_mode & 0o777)
    temp.replace(path)
    return {"path": relative, "hash": digest}


def prepare_workspace(source: str, task_id: str) -> dict:
    root = workspace(task_id)
    source_path = Path(source).expanduser().resolve()
    base = git(source_path, "rev-parse", "HEAD").strip()
    dirty = bool(git(source_path, "status", "--porcelain"))
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists():
        raise ValueError("Workspace already exists")
    # Independent clone: no worktree metadata/branch changes in the user's checkout.
    git(
        source_path,
        "clone",
        "--no-hardlinks",
        "--no-checkout",
        "--",
        str(source_path),
        str(root),
    )
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "checkout", "--detach", base)
    git(root, "switch", "-c", f"sdlc/{task_id[:8]}")
    return {"workspace": str(root), "base_commit": base, "source_dirty": dirty}


def diff_entries(root: Path, base_revision: str = "HEAD") -> list[tuple[str, str]]:
    """Return one supported text patch per changed path."""
    output = []
    # Include files deleted by a later local commit when exporting from a task's
    # original base. Agent tools retain their existing HEAD comparison.
    names = set(files(root))
    names.update(
        git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            base_revision,
            "--",
        ).split("\0")
    )
    for name in sorted(name for name in names if allowed(name)):
        try:
            read_file(
                root, name
            )  # Enforce size, binary, secret, and symlink exclusions.
            base = subprocess.run(
                ["git", "show", f"{base_revision}:{name}"],
                cwd=root,
                capture_output=True,
                timeout=5,
            )
            if len(base.stdout) > MAX_FILE:
                continue
            if base.returncode == 0:
                patch = git(
                    root,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    base_revision,
                    "--",
                    name,
                )
            else:
                result = subprocess.run(
                    [
                        "git",
                        "diff",
                        "--no-index",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-color",
                        "--",
                        "/dev/null",
                        name,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                patch = result.stdout
            if patch:
                output.append((name, patch))
        except (ValueError, UnicodeError):
            continue
    return output


def changed_paths(root: Path, base_revision: str) -> list[str]:
    """List every tracked or untracked path changed from the task base."""
    tracked = git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "-z",
        base_revision,
        "--",
    ).split("\0")
    untracked = git(root, "ls-files", "-z", "--others", "--exclude-standard").split(
        "\0"
    )
    return sorted({name for name in [*tracked, *untracked] if name})


def diff(root: Path, base_revision: str = "HEAD") -> dict:
    # Include untracked files without mutating the index; filter secrets even if tracked.
    entries = diff_entries(root, base_revision)
    patch = "".join(value for _, value in entries)
    return {
        "files": [name for name, _ in entries],
        "patch": patch[:500_000],
        "truncated": len(patch) > 500_000,
    }


def git_apply(root: Path, patch: str, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "apply",
            "--whitespace=nowarn",
            *options,
            "--",
        ],
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        timeout=45,
    )


def merge_to_project(
    root: Path,
    base_revision: str,
    source: str,
    expected_revision: str = "",
) -> dict:
    """Apply an accepted task into its source checkout without staging or committing."""
    # Imported lazily because coding_workspace uses these workspace primitives.
    from .coding_workspace import revision

    root = root.resolve()
    source_root = Path(source).expanduser().resolve()
    if source_root == root:
        raise ValueError("The task workspace cannot be its own project checkout")
    actual_root = Path(
        git(source_root, "rev-parse", "--show-toplevel").strip()
    ).resolve()
    if actual_root != source_root:
        raise ValueError("The configured project path is not the Git repository root")
    git(source_root, "cat-file", "-e", f"{base_revision}^{{commit}}")

    observed_revision = revision(root)
    if expected_revision and observed_revision != expected_revision:
        raise ValueError(
            "The task workspace changed after acceptance. Review and accept the current revision before merging."
        )
    paths = changed_paths(root, base_revision)
    entries = diff_entries(root, base_revision)
    if revision(root) != observed_revision:
        raise ValueError(
            "The task workspace changed while preparing the merge. Review and accept it again."
        )
    if not paths:
        raise ValueError("There are no task changes to merge into the project")
    supported = {name for name, _ in entries}
    if set(paths) != supported:
        raise ValueError(
            "Some task changes cannot be merged automatically. Inspect large, binary, linked, secret, or excluded files in the task workspace."
        )
    if sum(len(patch) for _, patch in entries) > 500_000:
        raise ValueError(
            "The task patch exceeds the 500 KB merge limit. Use Git in the task workspace."
        )
    for name in paths:
        safe_path(source_root, name)

    pending = []
    already_present = []
    for name, patch in entries:
        if git_apply(source_root, patch, "--check").returncode == 0:
            pending.append((name, patch))
        elif git_apply(source_root, patch, "--check", "--reverse").returncode == 0:
            already_present.append(name)
        else:
            raise ValueError(
                "The project has overlapping changes in files from this task. Nothing was changed. Resolve those edits in the project, then try again."
            )

    combined = "".join(patch for _, patch in pending)
    if combined:
        # Check the combined patch too, then let git apply perform its own atomic
        # preflight immediately before writing the working tree.
        if git_apply(source_root, combined, "--check").returncode:
            raise ValueError(
                "The project changed while preparing the merge. Nothing was changed. Try again after reviewing the project checkout."
            )
        applied = git_apply(source_root, combined)
        if applied.returncode:
            raise ValueError(
                "The project changed while the merge was starting. Git did not complete the merge; inspect the project checkout before retrying."
            )

    return {
        "ok": True,
        "revision": observed_revision,
        "files": paths,
        "applied_files": [name for name, _ in pending],
        "already_present_files": already_present,
        "already_applied": not pending,
        "project_path": str(source_root),
        "branch": git(source_root, "branch", "--show-current").strip()
        or "detached HEAD",
    }


def continue_workspace(parent_id: str, task_id: str) -> dict:
    parent = workspace(parent_id)
    patch = diff(parent)
    if patch["truncated"]:
        raise ValueError(
            "The prior diff exceeds the continuation limit. Commit changes in its workspace first."
        )
    prepared = prepare_workspace(str(parent), task_id)
    if patch["patch"]:
        result = subprocess.run(
            ["git", "apply", "--"],
            cwd=workspace(task_id),
            input=patch["patch"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode:
            raise ValueError(
                "Could not carry forward the previous patch; the original workspace is preserved"
            )
    prepared["source_dirty"] = False
    prepared["parent_task_id"] = parent_id
    return prepared


async def execute_check(root: Path, argv: list[str]) -> dict:
    if not argv or len(argv) > 100 or any("\0" in arg for arg in argv):
        raise ValueError("Expected a nonempty argument vector")
    scratch = data_dir() / "command-homes" / root.name
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Host mode is explicitly approved in the UI. A cwd is NOT a security sandbox.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks = bytearray()
    reason = ""
    try:
        async with asyncio.timeout(90):
            while block := await process.stdout.read(8192):
                chunks.extend(block)
                if len(chunks) > 100_000:
                    reason = "Stopped at 100 KB output limit"
                    break
            if not reason:
                await process.wait()
    except TimeoutError:
        reason = "Stopped at 90 second limit"
    finally:
        # Also remove surviving children if the main process exited successfully.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    return {
        "exit_code": process.returncode if not reason else -1,
        "output": chunks[:100_000].decode("utf-8", errors="replace")
        + ("\n" + reason if reason else ""),
    }


async def perform(task_id: str, operation_id: str, action: dict) -> dict:
    """Stable operation IDs prevent retrying a write or an uncertain command."""
    root = workspace(task_id)
    journal = data_dir() / "operations" / task_id
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not operation_id.replace("-", "").isalnum():
        raise ValueError("Invalid operation ID")
    record = journal / f"{operation_id}.json"
    fingerprint = hashlib.sha256(
        json.dumps(action, sort_keys=True).encode()
    ).hexdigest()
    if record.exists():
        previous = json.loads(record.read_text())
        if previous["fingerprint"] != fingerprint:
            raise ValueError("Operation ID was reused with different arguments")
        if "result" in previous:
            return previous["result"]
        if action["kind"] == "check":
            return {
                "error": "Command outcome is unknown after interruption; it was not run twice. Request a new check."
            }
    record.write_text(json.dumps({"fingerprint": fingerprint}))
    try:
        kind = action["kind"]
        if kind == "list":
            result = {"files": await asyncio.to_thread(files, root)}
        elif kind == "read":
            result = await asyncio.to_thread(read_file, root, action["path"])
        elif kind == "search":
            result = {
                "matches": await asyncio.to_thread(search_files, root, action["query"])
            }
        elif kind == "write":
            result = await asyncio.to_thread(
                write_file,
                root,
                action["path"],
                action["content"],
                action["expected_hash"],
            )
        elif kind == "check":
            result = await execute_check(root, action["argv"])
        else:
            raise ValueError("Unsupported workspace action")
    except (ValueError, OSError, UnicodeError) as error:
        result = {"error": str(error)[:1000]}
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps({"fingerprint": fingerprint, "result": result}))
    temporary.replace(record)
    return result


def search_files(root: Path, query: str) -> list[dict]:
    import time

    deadline = time.monotonic() + 20
    hits = []
    for name in files(root):
        if time.monotonic() > deadline or len(hits) >= 80:
            break
        try:
            for number, line in enumerate(
                read_file(root, name)["content"].splitlines(), 1
            ):
                if query.lower() in line.lower():
                    hits.append({"path": name, "line": number, "text": line[:400]})
                if len(hits) >= 80:
                    break
        except (ValueError, UnicodeError):
            pass
    return hits


def demo_project() -> Path:
    root = data_dir() / "demo-repository"
    if not root.exists():
        root.mkdir(parents=True)
        (root / "greeting.py").write_text(
            'def greet(name):\n    return f"hello {name}"\n'
        )
        (root / "test_greeting.py").write_text(
            'import unittest\nfrom greeting import greet\n\nclass GreetingTest(unittest.TestCase):\n    def test_greeting(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada!")\n'
        )
        (root / "README.md").write_text(
            "# Greeting workshop\n\nFix greeting.py so the unittest passes.\nRun: python3 -m unittest -v\n"
        )
        git(root, "init", "-b", "main")
        git(root, "add", ".")
        git(
            root,
            "-c",
            "user.name=SDLC Demo",
            "-c",
            "user.email=demo@localhost",
            "commit",
            "-m",
            "Add greeting exercise",
        )
    return root
