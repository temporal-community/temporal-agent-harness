"""What each registered agent takes to start: its init data, read from its class.

A registry entry names its agent's class with ``agent = "module.path:ClassName"``. The web
server imports those classes once at startup (outside any workflow) so it can tell a caller
whether an agent takes init data, whether it is required, and what shape it has, and can
reject a bad start with a 422 instead of letting the agent workflow fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from temporalio import workflow

from temporal_agent_harness.harness.agent_schema import agent_init_data_schema, load_agent_class
from temporal_agent_harness.harness.agent_workflow import AgentInitData, agent_init_data
from temporal_agent_harness.web.session_manager import AgentRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentStart:
    """One agent's start interface: the init data its ``@agent.init`` takes, if any."""

    init_data: AgentInitData | None
    # agent_init_data_schema(cls): {"required": bool, "schema": {...}}, or None.
    init_data_schema: dict[str, Any] | None


class InvalidStartData(ValueError):
    """A session start's ``data`` does not fit the agent's init data."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def resolve_agent_starts(registry: AgentRegistry) -> dict[str, AgentStart]:
    """Import each registry entry's ``agent`` class and read its start interface, keyed by
    workflow type. Entries without ``agent`` are left out: their start interface is unknown.

    A class that fails to import is logged and left out too, rather than stopping the server:
    agent modules can need things at import time (an API key, an optional SDK) that this
    process lacks. Raises ``ValueError`` if a class is registered under a different workflow
    type than the entry's ``workflow_type``, since the entry would then start some other agent
    than the one it describes."""
    starts: dict[str, AgentStart] = {}
    for descriptor in registry.agents:
        if descriptor.agent is None:
            continue
        try:
            cls = load_agent_class(descriptor.agent)
        except Exception as e:  # noqa: BLE001 — an agent module may fail to import for any reason
            logger.warning(
                "Agent %r: could not import agent = %r (%s: %s); its init data is unknown, "
                "so sessions for it start without validation.",
                descriptor.key,
                descriptor.agent,
                type(e).__name__,
                e,
            )
            continue
        definition = workflow._Definition.must_from_class(cls)
        if definition.name != descriptor.workflow_type:
            raise ValueError(
                f"Agent {descriptor.key!r}: {descriptor.agent} is the workflow "
                f"{definition.name!r}, but the entry's workflow_type is "
                f"{descriptor.workflow_type!r}."
            )
        starts[descriptor.workflow_type] = AgentStart(
            init_data=agent_init_data(cls),
            init_data_schema=agent_init_data_schema(cls),
        )
    return starts


def validated_start_data(
    workflow_type: str, start: AgentStart | None, data: dict[str, Any] | None
) -> dict[str, Any] | None:
    """``data`` checked against the agent's init data and normalized to JSON, or passed through
    untouched when the agent's start interface is unknown (the agent still validates it).

    Raises :class:`InvalidStartData` for data sent to an agent that takes none, missing
    required data, or data that does not validate."""
    if start is None:
        return data
    spec = start.init_data
    if spec is None:
        if data is not None:
            raise InvalidStartData(f"{workflow_type} takes no init data.")
        return None
    if data is None:
        if spec.required:
            raise InvalidStartData(
                f"{workflow_type} requires init data ({spec.model.__name__})."
            )
        return None
    try:
        return spec.model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        raise InvalidStartData(
            f"Invalid init data for {workflow_type} ({spec.model.__name__}).",
            errors=e.errors(include_url=False, include_context=False),
        ) from None
