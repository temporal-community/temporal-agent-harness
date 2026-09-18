"""App registration boundary for harness SDK integrations.

Each backend supplies its client/worker plugins, durable decision implementation,
and an ephemeral connection probe. Providers are enabled only when registered;
adding Pydantic AI later does not change the SDLC lifecycle or state schema.
"""

from typing import Protocol

from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from ..models import ModelRequest, ModelResult, Profile
from .openai import OpenAIIntegration


class SdkIntegration(Protocol):
    def client_plugins(self) -> list: ...
    def worker_plugins(self) -> list: ...
    async def decide(
        self, request: ModelRequest, runner: AgentWorkflowRunner
    ) -> ModelResult: ...
    async def probe(self, profile: Profile, key: str, prompt: str) -> ModelResult: ...
    async def run_role(
        self, profile, role, prompt, tools, runner, dispatch, max_calls
    ): ...


def registered_integrations() -> dict[str, SdkIntegration]:
    return {"openai": OpenAIIntegration()}


def integration_for(profile: Profile) -> SdkIntegration:
    integration = registered_integrations().get(profile.provider)
    if integration is None:
        raise ValueError(
            "Only OpenAI is enabled for new tasks and connection tests. Add an OpenAI profile; existing tasks and profiles are preserved."
        )
    return integration


def profile_available(profile: dict) -> bool:
    return (
        profile["provider"] == "demo"
        or profile["provider"] in registered_integrations()
    )
