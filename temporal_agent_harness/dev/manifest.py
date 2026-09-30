"""The project manifest, `harness.toml`: one description of a harness project.

    [project]
    name = "my-agents"
    env_file = ".env.local"                      # loaded into every process `dev` starts
    registries = ["examples/monty/agents.toml"]  # agents.toml files to include

    [dev]
    gateway_port = 8000

    [[agents]]                                   # agents declared here, same shape as agents.toml
    key = "my-agent"
    workflow_type = "MyAgent"
    task_queue = "my-agent"
    label = "My Agent"
    description = "What it does."

    [[workers]]                                  # how to start each worker process
    name = "my-agent"
    command = ["python", "-m", "my_app.worker"]
    requires = ["OPENAI_API_KEY"]
    watch = ["my_app"]

Paths are relative to the manifest's directory.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from temporal_agent_harness.web.registry import load_agent_registries
from temporal_agent_harness.web.session_manager import AgentRegistry

MANIFEST_NAME = "harness.toml"


class ProjectSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "harness-project"
    env_file: str | None = None
    """A dotenv file loaded into every process `dev` starts (relative to the manifest)."""
    registries: list[str] = []
    """agents.toml files whose agents this project includes."""


class DevSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_port: int = 8000
    temporal_port: int = 7233
    temporal_ui_port: int = 8233
    state_dir: str = ".harness"
    """Where `dev` keeps its Temporal database (relative to the manifest)."""


class WorkerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    command: list[str] = Field(min_length=1)
    """The process to run, from the manifest's directory. A leading `python` runs under the
    same interpreter as `dev`, so a worker sees the same environment."""
    requires: list[str] = []
    """Environment variables it can't start without. Missing any, `dev` skips it and says so."""
    watch: list[str] = []
    """Paths whose changes restart it."""
    env: dict[str, str] = {}
    """Extra environment for this process only."""

    @field_validator("name")
    @classmethod
    def _name_is_a_label(cls, value: str) -> str:
        if not value or any(c.isspace() for c in value):
            raise ValueError("a worker name is a short label with no spaces")
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectSection = ProjectSection()
    dev: DevSection = DevSection()
    agents: list[dict[str, object]] = []
    workers: list[WorkerSpec] = []

    path: Path = Field(default=Path(MANIFEST_NAME), exclude=True)
    """The manifest file itself (absolute once loaded)."""

    @property
    def root(self) -> Path:
        return self.path.parent

    def resolve(self, relative: str) -> Path:
        return (self.root / relative).resolve()

    # ------------------------------------------------------------------ agents

    def registry_paths(self) -> list[Path]:
        """Every registry file the project's agents come from: included files, then this
        manifest when it declares agents of its own."""
        paths = [self.resolve(r) for r in self.project.registries]
        if self.agents:
            paths.append(self.path)
        return paths

    def registry(self) -> AgentRegistry:
        """The merged agent registry. Keys and workflow types must be unique across it."""
        return load_agent_registries(self.registry_paths())

    # ------------------------------------------------------------------ environment

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The environment for processes `dev` starts: `base` (default: this process's), with
        the project's env file layered under it, so an exported variable still wins."""
        env = dict(os.environ if base is None else base)
        if self.project.env_file:
            file = self.resolve(self.project.env_file)
            if file.is_file():
                for key, value in parse_env_file(file.read_text()).items():
                    env.setdefault(key, value)
        return env

    def missing_requirements(self, worker: WorkerSpec, env: dict[str, str]) -> list[str]:
        return [name for name in worker.requires if not env.get(name)]


def load_manifest(path: Path | str = MANIFEST_NAME) -> Manifest:
    """Read and validate a `harness.toml`."""
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} at {manifest_path}")
    with manifest_path.open("rb") as f:
        raw = tomllib.load(f)
    manifest = Manifest.model_validate({**raw, "path": manifest_path})
    names = [w.name for w in manifest.workers]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{manifest_path}: duplicate worker name(s) {duplicates}")
    return manifest


def parse_env_file(text: str) -> dict[str, str]:
    """A dotenv file's `KEY=value` lines. Blank lines and `#` comments are skipped; `export `
    prefixes and matching surrounding quotes are removed."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values
