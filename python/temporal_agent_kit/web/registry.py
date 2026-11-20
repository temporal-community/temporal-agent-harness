"""The agent registry's formats: ``agents.toml`` files, registrations, and the server's view.

A client registers agents by sending :class:`AgentRegistration` objects, each carrying its
schema document itself. An ``agents.toml`` file is one way to write them down:
:func:`load_registrations` reads one and inlines the schema files its entries name. The server
checks each registration with :func:`registered_agent` before it reaches the registry store, and
reads the stored registry through :func:`registry_view`, which puts each task queue under this
process's ``TASK_QUEUE_PREFIX``.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
)
from pydantic.json_schema import SkipJsonSchema

from temporal_agent_kit.agent_schema import agent_init_data_schema_from_document
from temporal_agent_kit.utils.task_queue import task_queue_name
from temporal_agent_kit.web.registry_changes import registered
from temporal_agent_kit.web.registry_store import (
    AgentStart,
    RegisteredAgent,
    RegistrySnapshot,
)


@dataclass
class AgentDescriptor:
    """One agent in the server's view of its registry."""

    key: str
    workflow_type: str
    # Under this process's TASK_QUEUE_PREFIX, as the agent's workers poll it.
    task_queue: str
    label: str
    description: str
    # The agent's start interface, read from its schema document. ``None`` when it was
    # registered without one; the agent still validates its start.
    start: AgentStart | None = None
    # A retired agent cannot be started, but its sessions are still listed.
    retired: bool = False


@dataclass
class AgentRegistry:
    """The agents a server launches and lists sessions of, as one request reads them."""

    agents: list[AgentDescriptor] = field(default_factory=list)

    def by_workflow_type(self, workflow_type: str) -> AgentDescriptor | None:
        return next(
            (agent for agent in self.agents if agent.workflow_type == workflow_type),
            None,
        )

    def by_key(self, key: str) -> AgentDescriptor | None:
        return next((agent for agent in self.agents if agent.key == key), None)


def registry_view(snapshot: RegistrySnapshot) -> AgentRegistry:
    """``snapshot`` as this server routes to it: each task queue under ``TASK_QUEUE_PREFIX``,
    ordered by key."""
    return AgentRegistry(
        agents=[
            AgentDescriptor(
                key=agent.key,
                workflow_type=agent.workflow_type,
                task_queue=task_queue_name(agent.task_queue),
                label=agent.label,
                description=agent.description,
                start=agent.start,
                retired=agent.retired,
            )
            for agent in sorted(snapshot.agents.values(), key=lambda agent: agent.key)
        ]
    )


_Required = Annotated[str, StringConstraints(min_length=1)]


class AgentRegistration(BaseModel):
    """One agent to register, as a client sends it to ``POST /api/agents``."""

    model_config = ConfigDict(populate_by_name=True)

    key: _Required = Field(description="Unique within the registry.")
    workflow_type: _Required = Field(description="The agent's workflow type.")
    task_queue: _Required = Field(
        description="The task queue the agent's worker polls, without TASK_QUEUE_PREFIX."
    )
    label: _Required = Field(description="The name the UI shows.")
    description: _Required = Field(description="What the agent does, for the UI.")
    # `schema` would shadow BaseModel.schema.
    schema_document: dict[str, JsonValue] | None = Field(
        default=None,
        alias="schema",
        description="The agent's schema document (`temporal-agent-kit schema`). Its init data "
        "becomes the agent's start interface.",
    )


class InvalidRegistration(ValueError):
    """A registration whose contents do not describe an agent."""


def registered_agent(registration: AgentRegistration) -> RegisteredAgent:
    """``registration`` as the registry stores it, with its init data extracted from its schema
    document.

    Raises :class:`InvalidRegistration` when the schema document describes another workflow or
    has no readable init data.
    """
    document = registration.schema_document
    start: AgentStart | None = None
    if document is not None:
        if document.get("agent") != registration.workflow_type:
            raise InvalidRegistration(
                f"{registration.key!r}: the schema document describes the workflow "
                f"{document.get('agent')!r}, but the registration's workflow_type is "
                f"{registration.workflow_type!r}."
            )
        try:
            start = AgentStart(init_data=agent_init_data_schema_from_document(document))
        except (KeyError, TypeError, AttributeError) as e:
            raise InvalidRegistration(
                f"{registration.key!r}: the schema document's init_data cannot be read ({e!r})."
            ) from e
    return RegisteredAgent(
        key=registration.key,
        workflow_type=registration.workflow_type,
        task_queue=registration.task_queue,
        label=registration.label,
        description=" ".join(registration.description.split()),
        schema_document=document,
        start=start,
    )


def registration_of(agent: RegisteredAgent) -> AgentRegistration:
    """The registration that registers ``agent`` again as it is."""
    return AgentRegistration(
        key=agent.key,
        workflow_type=agent.workflow_type,
        task_queue=agent.task_queue,
        label=agent.label,
        description=agent.description,
        schema_document=agent.schema_document,
    )


class AgentRegistryEntry(BaseModel):
    """One ``[[agents]]`` entry of an ``agents.toml`` registry: a launchable agent.

    Keys this model does not name are ignored, so an SDK may add its own (the TypeScript SDK's
    ``agent`` reference).
    """

    key: _Required = Field(description="Unique within the registry.")
    workflow_type: _Required = Field(description="The agent's workflow type.")
    task_queue: _Required = Field(description="The task queue the agent's worker polls.")
    label: _Required = Field(description="The name the UI shows.")
    description: _Required = Field(description="What the agent does, for the UI.")
    # `schema` would shadow BaseModel.schema. TOML has no null: the key is a string or absent.
    schema_path: str | SkipJsonSchema[None] = Field(
        default=None,
        alias="schema",
        description="The agent's schema document (`temporal-agent-kit schema`), relative "
        "to this file. Its init data becomes the agent's start interface.",
        json_schema_extra=lambda schema: schema.pop("default"),
    )


class AgentRegistryFile(BaseModel):
    """An ``agents.toml`` registry: agents to register with ``temporal-agent-kit``."""

    agents: list[AgentRegistryEntry] = Field(min_length=1)


def registry_schema() -> dict[str, Any]:
    """The JSON Schema of an ``agents.toml`` registry, which every SDK reads."""
    return AgentRegistryFile.model_json_schema(by_alias=True)


def dump_registry_schema() -> str:
    """:func:`registry_schema` as stable, diffable JSON text."""
    return json.dumps(registry_schema(), indent=2, ensure_ascii=False) + "\n"


def load_registrations(path: Path) -> list[AgentRegistration]:
    """The registrations an ``agents.toml`` file describes, each with the schema document its
    ``schema`` names (relative to the file) read in.

    Raises ``ValueError`` for a file that does not match the format, and ``OSError`` for a schema
    file that cannot be read.
    """
    with path.open("rb") as file:
        raw = tomllib.load(file)
    try:
        registry = AgentRegistryFile.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Agent registry {path} is invalid:\n{e}") from e
    return [
        AgentRegistration(
            key=entry.key,
            workflow_type=entry.workflow_type,
            task_queue=entry.task_queue,
            label=entry.label,
            description=entry.description,
            schema_document=(
                None
                if entry.schema_path is None
                else _schema_document(path.parent / entry.schema_path)
            ),
        )
        for entry in registry.agents
    ]


def load_agent_registry(path: Path) -> AgentRegistry:
    """The registry the ``agents.toml`` file at ``path`` registers on its own, as this server
    would read it. Raises ``ValueError`` for a file the registry's rules refuse."""
    agents = [registered_agent(registration) for registration in load_registrations(path)]
    try:
        snapshot = registered(RegistrySnapshot(), agents)
    except ValueError as e:
        raise ValueError(f"Agent registry {path} is invalid: {e}") from e
    return registry_view(snapshot)


def _schema_document(path: Path) -> dict[str, JsonValue]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a JSON object.")
    return document
