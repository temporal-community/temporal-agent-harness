"""Shared resource schema for the global catalog and account registries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ResourceCategory = Literal["agent", "mcp"]
ResourceTransport = Literal["nexus", "external_http"]


@dataclass(frozen=True)
class AgentHandlerDescriptor:
    """Model-facing contract for one remotely callable agent handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    param_name: str = "input"


TEXT_AGENT_HANDLER = AgentHandlerDescriptor(
    name="ask",
    description="Ask the agent a question.",
    param_name="message",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class ResourceDescriptor:
    """Transport-neutral definition shared by catalog and account state.

    ``endpoint`` is a Nexus endpoint for native resources and an HTTP URL for
    external resources. ``service`` is required only for Nexus resources.
    """

    resource_id: str
    revision: int
    category: ResourceCategory
    transport: ResourceTransport
    label: str
    description: str
    endpoint: str
    service: str | None = None
    handlers: tuple[AgentHandlerDescriptor, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Compatibility views keep the broker/UI readable while the canonical stored
    # shape remains transport-neutral and defined only once.
    @property
    def name(self) -> str:
        return self.resource_id

    @property
    def agent_id(self) -> str:
        return self.resource_id

    @property
    def kind(self) -> str:
        return "harness_nexus" if self.transport == "nexus" else "external_http"

    @property
    def nexus_endpoint(self) -> str | None:
        return self.endpoint if self.transport == "nexus" else None

    @property
    def nexus_service(self) -> str:
        return self.service or "AgentService"

    @property
    def provider_url(self) -> str | None:
        return self.endpoint if self.transport == "external_http" else None


@dataclass(frozen=True)
class AccountResourceRegistration:
    """An account's pinned snapshot of one catalog descriptor."""

    descriptor: ResourceDescriptor
    installed_at: float
    enabled: bool = True


def validate_resource_descriptor(descriptor: ResourceDescriptor) -> None:
    """Validate a resource without coupling either registry to the other."""

    required = {
        "resource_id": descriptor.resource_id,
        "label": descriptor.label,
        "endpoint": descriptor.endpoint,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise ValueError(f"missing required resource field(s): {', '.join(missing)}")
    if descriptor.revision < 1:
        raise ValueError("resource revision must be at least 1")
    if descriptor.category not in {"agent", "mcp"}:
        raise ValueError(f"unsupported resource category {descriptor.category!r}")
    if descriptor.transport not in {"nexus", "external_http"}:
        raise ValueError(f"unsupported resource transport {descriptor.transport!r}")
    if descriptor.transport == "nexus" and not (descriptor.service or "").strip():
        raise ValueError("Nexus resources require a service name")
    if descriptor.category == "agent" and not descriptor.handlers:
        raise ValueError("agent resources require at least one handler contract")
    if descriptor.category == "mcp" and descriptor.handlers:
        raise ValueError("MCP resources cannot declare agent handlers")
    for handler in descriptor.handlers:
        if not handler.name.strip() or not handler.param_name.strip():
            raise ValueError("agent handler name and parameter name are required")
        if handler.input_schema.get("type") != "object":
            raise ValueError("agent handler input schemas must describe an object")
        if handler.output_schema.get("type") != "object":
            raise ValueError("agent handler output schemas must describe an object")


def descriptor_from_dict(value: dict[str, Any]) -> ResourceDescriptor:
    """Decode JSON-shaped catalog data into the canonical descriptor."""

    handlers = tuple(
        AgentHandlerDescriptor(**handler) for handler in value.get("handlers", [])
    )
    descriptor = ResourceDescriptor(
        resource_id=str(value["resource_id"]),
        revision=int(value.get("revision", 1)),
        category=value["category"],
        transport=value["transport"],
        label=str(value["label"]),
        description=str(value.get("description", "")),
        endpoint=str(value["endpoint"]),
        service=value.get("service"),
        handlers=handlers,
    )
    validate_resource_descriptor(descriptor)
    return descriptor
