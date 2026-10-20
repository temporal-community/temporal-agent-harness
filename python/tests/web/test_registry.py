"""agents.toml registries: the format's committed schema (contract/registry.schema.json) and the
loader that validates against the same model."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jsonschema
import pytest

from temporal_agent_harness.web.registry import load_agent_registry

# Relative to the runfiles root the test starts in.
REGISTRY_SCHEMA = json.loads(Path("contract/registry.schema.json").read_text())
EXAMPLE_REGISTRIES = sorted(Path("python/examples").rglob("agents.toml"))

ENTRY = """
[[agents]]
key = "hello"
workflow_type = "HelloAgent"
task_queue = "hello"
label = "Hello"
description = "Says hello."
"""


def _registry(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agents.toml"
    path.write_text(text)
    return path


def test_examples_have_registries_to_check() -> None:
    assert len(EXAMPLE_REGISTRIES) >= 10


@pytest.mark.parametrize("registry", EXAMPLE_REGISTRIES, ids=lambda path: str(path.parent))
def test_every_example_registry_matches_the_committed_schema_and_loads(registry: Path) -> None:
    jsonschema.validate(tomllib.loads(registry.read_text()), REGISTRY_SCHEMA)
    assert load_agent_registry(registry).agents


def test_keys_the_format_does_not_name_are_accepted(tmp_path: Path) -> None:
    """Another SDK's own keys, like the TypeScript SDK's `agent` reference, do not break Python."""
    text = ENTRY + 'agent = "./agent.ts#HelloAgent"\n'

    jsonschema.validate(tomllib.loads(text), REGISTRY_SCHEMA)
    (agent,) = load_agent_registry(_registry(tmp_path, text)).agents
    assert agent.workflow_type == "HelloAgent"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="no agents"),
        pytest.param("agents = []\n", id="empty agents"),
        pytest.param(ENTRY.replace('label = "Hello"\n', ""), id="missing label"),
        pytest.param(ENTRY.replace('label = "Hello"', 'label = ""'), id="empty label"),
        pytest.param(ENTRY.replace('task_queue = "hello"', "task_queue = 7"), id="non-string task_queue"),
    ],
)
def test_invalid_registries_are_rejected_by_the_loader_and_the_schema(tmp_path: Path, text: str) -> None:
    path = _registry(tmp_path, text)

    with pytest.raises(ValueError, match=str(path)):
        load_agent_registry(path)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(tomllib.loads(text), REGISTRY_SCHEMA)


def test_duplicate_keys_in_one_registry_are_rejected(tmp_path: Path) -> None:
    path = _registry(tmp_path, ENTRY + ENTRY.replace('workflow_type = "HelloAgent"', 'workflow_type = "Other"'))

    with pytest.raises(ValueError, match="Duplicate agent key 'hello'"):
        load_agent_registry(path)
