"""Installed-package development resources; no model SDK or web server needed to copy them."""
from __future__ import annotations

import hashlib
import json
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

SKILL_NAME = "temporal-harness-python"
MANIFEST = ".harness-skill.json"
# Where coding agents discover project skills; `skills install --for <key>` targets one. Claude Code
# reads only .claude/skills; .agents/skills is the shared directory Codex and other agents read.
SKILL_DIRECTORIES = {"claude": ".claude/skills", "agents": ".agents/skills"}
SKILL_TARGETS = {"claude": "Claude Code", "agents": "Codex and other agents that read the shared .agents/skills"}
# How each tool invokes the installed skill from a prompt.
SKILL_PROMPTS = {"Codex": f"${SKILL_NAME}", "Claude Code": f"/{SKILL_NAME}"}


class InstalledSkill(NamedTuple):
    root: Path
    changed: bool


def package_version() -> str:
    try:
        return version("temporal-agent-harness")
    except PackageNotFoundError:
        raise ValueError("Install temporal-agent-harness before using its development commands.") from None


def skill_path() -> Path:
    return Path(__file__).parent / "skills" / SKILL_NAME


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(root: Path, relative: str) -> Path:
    name = Path(relative)
    if name.is_absolute() or not name.parts or any(p in ("..", ".") for p in name.parts):
        raise ValueError(f"Invalid resource path: {relative}")
    target = root / name
    for path in [root, target, *target.parents]:
        if path == root.parent:
            break
        if path.exists() and path != target and not path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        if path.is_symlink():
            raise ValueError(f"Refusing to write through a symlink: {path}")
    return target


def resource_files(source: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file() and path.suffix in (".md", ".yaml", ".json", ".tmpl")
    }


def skill_destinations(tools: list[str] | None, dest: str | None) -> list[Path]:
    """The parent directories `skills install` writes to: one per `--for` tool, plus `--dest`."""
    unknown = sorted(set(tools or []) - SKILL_DIRECTORIES.keys())
    if unknown:
        raise ValueError(f"Unknown coding agent {', '.join(unknown)}; choose from {', '.join(SKILL_DIRECTORIES)}")
    destinations = [SKILL_DIRECTORIES[tool] for tool in tools or []]
    if dest is not None:
        destinations.append(dest)
    if not destinations:
        raise ValueError(f"Choose where to install with --for {{{','.join(SKILL_DIRECTORIES)}}} or --dest <directory>")
    return [Path(d) for d in dict.fromkeys(destinations)]


def choose_skill_tools(ask: Callable[[str], str] = input) -> list[str]:
    """Ask which coding agents to install for; `skills install` does this in a terminal without --for or --dest."""
    keys = list(SKILL_DIRECTORIES)
    menu = "\n".join(f"  {n}) {SKILL_TARGETS[key]}: {SKILL_DIRECTORIES[key]}" for n, key in enumerate(keys, 1))
    numbers = [str(n) for n in range(1, len(keys) + 1)]
    while True:
        try:
            answer = ask(f"Install the skill for which coding agents?\n{menu}\nEnter one or more numbers ({','.join(numbers)}): ")
        except EOFError:
            raise ValueError("No coding agent chosen") from None
        picks = answer.replace(",", " ").split()
        if picks and set(picks) <= set(numbers):
            return [keys[int(n) - 1] for n in dict.fromkeys(picks)]


def install_skill(destination: Path, *, source: Path | None = None, sdk_version: str | None = None) -> InstalledSkill:
    """Update only unchanged, owned files; preflight every conflict before changing anything.

    ``changed`` is False when every bundled file was already in place, so a rerun after an
    unchanged install (or an SDK upgrade that left the skill alone) can say so.
    """
    root = destination / SKILL_NAME
    files = resource_files(source or skill_path())
    if "SKILL.md" not in files:
        raise ValueError("The installed package is missing its SKILL.md")
    marker = _target(root, MANIFEST)
    previous: dict[str, str] = {}
    if marker.exists():
        metadata = json.loads(marker.read_text())
        if not isinstance(metadata, dict) or metadata.get("name") != SKILL_NAME or not isinstance(metadata.get("files"), dict):
            raise ValueError(f"Invalid skill ownership manifest: {marker}")
        previous = metadata["files"]
        if MANIFEST in previous or any(not isinstance(v, str) for v in previous.values()):
            raise ValueError(f"Invalid skill ownership manifest: {marker}")
    conflicts = []
    for name in files.keys() | previous.keys():
        path = _target(root, name)
        if path.exists():
            if not path.is_file():
                conflicts.append(name)
                continue
            data = path.read_bytes()
            if data != files.get(name) and _digest(data) != previous.get(name):
                conflicts.append(name)
    if conflicts:
        raise ValueError("Local skill edits preserved; resolve conflicts or choose --dest: " + ", ".join(sorted(conflicts)))
    sdk_version = sdk_version or package_version()
    stale = {name for name, data in files.items()
             if not _target(root, name).is_file() or _target(root, name).read_bytes() != data}
    obsolete = {name for name in previous.keys() - files.keys() if _target(root, name).exists()}
    for name in stale:
        path = _target(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[name])
    for name in obsolete:
        _target(root, name).unlink()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"name": SKILL_NAME, "version": sdk_version,
                                  "files": {n: _digest(b) for n, b in files.items()}}, indent=2) + "\n")
    return InstalledSkill(root, bool(stale or obsolete))


def project_name(destination: Path) -> str:
    """A package-manager-safe name derived from the starter directory, e.g. `My Agent` -> `my-agent`."""
    slug = re.sub(r"[^a-z0-9]+", "-", destination.resolve().name.lower()).strip("-")
    return slug or "my-agent"


def create_starter(destination: Path, *, source: Path | None = None, sdk_version: str | None = None) -> Path:
    source = source or skill_path() / "assets" / "starter"
    sdk_version = sdk_version or package_version()
    name_bytes = project_name(destination).encode()
    files = {}
    for name, data in resource_files(source).items():
        if not name.endswith(".tmpl"):
            continue
        name = name.removesuffix(".tmpl")
        if name == "gitignore":
            name = ".gitignore"
        files[name] = (data.replace(b"__HARNESS_VERSION__", sdk_version.encode())
                       .replace(b"__PROJECT_NAME__", name_bytes))
    if not files:
        raise ValueError("The installed package is missing its starter")
    for name in files:
        if _target(destination, name).exists():
            raise ValueError(f"Starter would overwrite {destination / name}; choose a new directory")
    for name, data in files.items():
        path = _target(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(data)
    return destination


def api_checks(api_url: str, timeout: float = 5) -> list[dict[str, Any]]:
    """Probe an existing server; a failed request never creates or closes a session."""
    from urllib.parse import urlsplit

    url = urlsplit(api_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
        raise ValueError("API URL must be http(s) without embedded credentials")
    checks: list[dict[str, Any]] = []
    try:
        with urlopen(api_url.rstrip("/") + "/api/agents", timeout=timeout) as response:
            data = json.load(response)
        agents = data["agents"]
        if not isinstance(agents, list):
            raise ValueError("agents is not a list")
        checks.append({"check": "api", "ok": True, "detail": f"{len(agents)} registered agents"})
        for agent in agents:
            if not isinstance(agent, dict) or not isinstance(agent.get("worker"), dict):
                raise ValueError("invalid agent worker status")
            worker = agent.get("worker", {})
            checks.append({"check": f"worker:{agent['workflow_type']}", "ok": worker.get("status") == "ready",
                           "detail": worker.get("status", "availability not reported")})
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not echo response bodies, URLs, or connection configuration containing credentials.
        checks.append({"check": "api", "ok": False, "detail": f"{type(error).__name__}: check the API and session-manager terminals"})
    return checks
