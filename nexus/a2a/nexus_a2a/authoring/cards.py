"""Helpers for advertising A2A agents through the Nexus binding."""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from nexus_a2a.service import A2A_NEXUS_BINDING, A2A_PROTOCOL_VERSION


def make_agent_card(
    *,
    name: str,
    description: str,
    endpoint: str,
    skills: tuple[tuple[str, str], ...] = (("ask", "Ask the agent a question."),),
    tags: tuple[str, ...] = (),
    version: str = "1.0.0",
    streaming: bool = True,
) -> AgentCard:
    """Build an A2A AgentCard for an agent exposed through Nexus."""

    return AgentCard(
        name=name,
        description=description,
        version=version,
        supported_interfaces=[
            AgentInterface(
                url=endpoint,
                protocol_binding=A2A_NEXUS_BINDING,
                protocol_version=A2A_PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=streaming),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id=skill_id,
                name=skill_id,
                description=skill_description,
                tags=list(tags),
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
            )
            for skill_id, skill_description in skills
        ],
    )
