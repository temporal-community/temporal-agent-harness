"""Worker-side LiteLLM configuration. Credentials never enter workflow payloads."""

import os
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel

from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider
from temporalio import activity

from .models import ModelNames


@dataclass(frozen=True)
class LiteLLMSettings:
    base_url: str
    api_key: str = field(repr=False)
    models: ModelNames

    @classmethod
    def from_env(cls) -> "LiteLLMSettings":
        base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
        model = os.environ.get("LITELLM_MODEL", "").strip()
        if not base_url or not model:
            raise ValueError(
                "Set LITELLM_BASE_URL and LITELLM_MODEL; see examples/code_review/.env.example"
            )
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                "LITELLM_BASE_URL must be an HTTP(S) OpenAI-compatible API URL"
            )
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=os.environ.get("LITELLM_API_KEY") or "not-used",
            models=ModelNames(
                coordinator=os.environ.get("LITELLM_COORDINATOR_MODEL") or model,
                correctness=os.environ.get("LITELLM_CORRECTNESS_MODEL") or model,
                security=os.environ.get("LITELLM_SECURITY_MODEL") or model,
            ),
        )


@activity.defn
async def review_model_names() -> ModelNames:
    """Resolve model aliases once per review; return no gateway credentials."""
    return LiteLLMSettings.from_env().models


class LiteLLMSecurityModel(Model):
    """A statically registered model whose gateway client is created only in Activities.

    Pydantic AI's Temporal wrapper requires a Model instance at registration.
    This proxy keeps credentials and environment reads out of workflow execution.
    """

    @property
    def model_name(self) -> str:
        return "litellm-security"

    @property
    def system(self) -> str:
        return "openai"

    def configured_model(self) -> OpenAIChatModel:
        settings = LiteLLMSettings.from_env()
        return OpenAIChatModel(
            settings.models.security,
            provider=PydanticOpenAIProvider(
                base_url=settings.base_url, api_key=settings.api_key
            ),
        )

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.configured_model() as model:
            return await model.request(
                messages, model_settings, model_request_parameters
            )

    @asynccontextmanager
    async def request_stream(
        self, messages, model_settings, model_request_parameters, run_context=None
    ):
        async with self.configured_model() as model:
            async with model.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
