"""Registry loading helpers for the harness web API."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, ValidationError
from pydantic.json_schema import SkipJsonSchema

from temporal_agent_harness.harness.agent_schema import agent_init_data_schema_from_document
from temporal_agent_harness.web.session_manager import AgentDescriptor, AgentRegistry, AgentStart

_Required = Annotated[str, StringConstraints(min_length=1)]


class AgentRegistryEntry(BaseModel):
    """One ``[[agents]]`` entry of an ``agents.toml`` registry: a launchable agent.

    Keys this model does not name are ignored, so an SDK may add its own (the TypeScript SDK's
    ``agent`` reference).
    """

    key: _Required = Field(description="Unique within the registries a server merges.")
    workflow_type: _Required = Field(description="The agent's workflow type.")
    task_queue: _Required = Field(description="The task queue the agent's worker polls.")
    label: _Required = Field(description="The name the UI shows.")
    description: _Required = Field(description="What the agent does, for the UI.")
    # `schema` would shadow BaseModel.schema. TOML has no null: the key is a string or absent.
    schema_path: str | SkipJsonSchema[None] = Field(
        default=None,
        alias="schema",
        description="The agent's schema document (`temporal-agent-harness schema`), relative "
        "to this file. Its init data becomes the agent's start interface.",
        json_schema_extra=lambda schema: schema.pop("default"),
    )


class AgentRegistryFile(BaseModel):
    """An ``agents.toml`` registry: the agents ``temporal-agent-harness serve`` offers."""

    agents: list[AgentRegistryEntry] = Field(min_length=1)


def registry_schema() -> dict[str, Any]:
    """The JSON Schema of an ``agents.toml`` registry, which every SDK reads."""
    return AgentRegistryFile.model_json_schema(by_alias=True)


def dump_registry_schema() -> str:
    """:func:`registry_schema` as stable, diffable JSON text."""
    return json.dumps(registry_schema(), indent=2, ensure_ascii=False) + "\n"


def load_agent_registry(path: Path | str) -> AgentRegistry:
    """Parse an agent registry TOML file into an :class:`AgentRegistry`.

    An entry's optional ``schema`` is the path, relative to the TOML file, of the agent's schema
    document (``temporal-agent-harness schema module:Class -o X.schema.json``). Its init data
    becomes the descriptor's :class:`AgentStart`; the agent's code is never imported.
    """

    registry_path = Path(path)
    with registry_path.open("rb") as file:
        raw = tomllib.load(file)
    try:
        registry = AgentRegistryFile.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Agent registry {registry_path} is invalid:\n{e}") from e

    agents: list[AgentDescriptor] = []
    seen_keys: set[str] = set()
    for entry in registry.agents:
        if entry.key in seen_keys:
            raise ValueError(f"Duplicate agent key {entry.key!r} in {registry_path}.")
        seen_keys.add(entry.key)
        agents.append(
            AgentDescriptor(
                key=entry.key,
                workflow_type=entry.workflow_type,
                task_queue=entry.task_queue,
                label=entry.label,
                description=" ".join(entry.description.split()),
                start=(
                    None
                    if entry.schema_path is None
                    else _agent_start(registry_path.parent / entry.schema_path, entry.workflow_type)
                ),
            )
        )

    return AgentRegistry(agents=agents)


def _agent_start(schema_path: Path, workflow_type: str) -> AgentStart:
    document = json.loads(schema_path.read_text())
    if document.get("agent") != workflow_type:
        raise ValueError(
            f"{schema_path} describes the workflow {document.get('agent')!r}, but its registry "
            f"entry's workflow_type is {workflow_type!r}."
        )
    return AgentStart(init_data=agent_init_data_schema_from_document(document))


def load_agent_registries(paths: Iterable[Path | str]) -> AgentRegistry:
    """Load and merge several agent registry TOML files into one :class:`AgentRegistry`.

    Each file is parsed with :func:`load_agent_registry`, then the agents are concatenated. Both
    ``key`` and ``workflow_type`` must be unique across the merged set — ``key`` because the UI /
    ``by_key`` rely on it, and ``workflow_type`` because :meth:`AgentRegistry.by_workflow_type`
    (which session creation routes on) returns the first match, so a duplicate would silently
    shadow. Passing a single path yields the same registry as :func:`load_agent_registry`.
    """
    agents: list[AgentDescriptor] = []
    seen_keys: dict[str, Path] = {}
    seen_types: dict[str, Path] = {}
    for path in paths:
        registry_path = Path(path)
        for descriptor in load_agent_registry(registry_path).agents:
            if descriptor.key in seen_keys:
                raise ValueError(
                    f"Duplicate agent key {descriptor.key!r} across registries "
                    f"({seen_keys[descriptor.key]} and {registry_path})."
                )
            if descriptor.workflow_type in seen_types:
                raise ValueError(
                    f"Duplicate agent workflow_type {descriptor.workflow_type!r} across "
                    f"registries ({seen_types[descriptor.workflow_type]} and {registry_path})."
                )
            seen_keys[descriptor.key] = registry_path
            seen_types[descriptor.workflow_type] = registry_path
            agents.append(descriptor)

    if not agents:
        raise ValueError("No agent registries provided.")

    return AgentRegistry(agents=agents)
