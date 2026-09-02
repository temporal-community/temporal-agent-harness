"""Shared resource schema for the global catalog and account registries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict

ResourceCategory = Literal["agent", "mcp"]
ResourceTransport = Literal["nexus", "external_http"]


def text_agent_card(
    *, name: str, description: str, endpoint: str, transport: ResourceTransport
) -> dict[str, Any]:
    """Return the minimal standard A2A card used by manually registered agents."""

    return {
        "name": name,
        "description": description,
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": endpoint,
                "protocolBinding": (
                    "TEMPORAL_NEXUS" if transport == "nexus" else "HTTP+JSON"
                ),
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "ask",
                "name": "ask",
                "description": "Ask the agent a question.",
                "tags": ["agent"],
            }
        ],
    }


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
    # Official A2A AgentCard JSON. This is the sole agent capability schema shared
    # by the global catalog, account registry, native Nexus binding, and HTTP agents.
    agent_card: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Named projections keep routing code readable while the canonical stored shape
    # remains transport-neutral and defined only once.
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
        return self.service or "A2AService"

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
    if descriptor.category == "agent" and not descriptor.agent_card:
        raise ValueError("agent resources require an A2A agent_card")
    if descriptor.category == "mcp" and descriptor.agent_card is not None:
        raise ValueError("MCP resources cannot declare an A2A agent_card")
    if descriptor.agent_card is not None:
        try:
            card = ParseDict(descriptor.agent_card, AgentCard())
        except Exception as exc:
            raise ValueError("agent_card must be valid A2A AgentCard JSON") from exc
        if not card.name or not card.supported_interfaces or not card.skills:
            raise ValueError(
                "agent_card requires a name, supported interface, and skill"
            )


def descriptor_from_dict(value: dict[str, Any]) -> ResourceDescriptor:
    """Decode JSON-shaped catalog data into the canonical descriptor."""

    descriptor = ResourceDescriptor(
        resource_id=str(value["resource_id"]),
        revision=int(value.get("revision", 1)),
        category=value["category"],
        transport=value["transport"],
        label=str(value["label"]),
        description=str(value.get("description", "")),
        endpoint=str(value["endpoint"]),
        service=value.get("service"),
        agent_card=value.get("agent_card"),
    )
    validate_resource_descriptor(descriptor)
    return descriptor
