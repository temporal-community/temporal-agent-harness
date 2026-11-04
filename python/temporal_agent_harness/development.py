"""Installed-package development resources; no model SDK or web server needed to copy them."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

SKILL_NAME = "temporal-harness-python"
MANIFEST = ".harness-skill.json"


def package_version() -> str:
    try:
        return version("temporal-agent-harness")
    except PackageNotFoundError:
        raise ValueError("Install temporal-agent-harness before creating a starter.") from None


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


def install_skill(destination: Path, *, source: Path | None = None, sdk_version: str | None = None) -> Path:
    """Update only unchanged, owned files; preflight every conflict before changing anything."""
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
    for name, data in files.items():
        path = _target(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in previous.keys() - files.keys():
        path = _target(root, name)
        if path.exists():
            path.unlink()
    marker.write_text(json.dumps({"name": SKILL_NAME, "version": sdk_version,
                                  "files": {n: _digest(b) for n, b in files.items()}}, indent=2) + "\n")
    return root


def create_starter(destination: Path, *, source: Path | None = None, sdk_version: str | None = None) -> Path:
    source = source or skill_path() / "assets" / "starter"
    sdk_version = sdk_version or package_version()
    files = {}
    for name, data in resource_files(source).items():
        if not name.endswith(".tmpl"):
            continue
        name = name.removesuffix(".tmpl")
        if name == "gitignore":
            name = ".gitignore"
        files[name] = data.replace(b"__HARNESS_VERSION__", sdk_version.encode())
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
