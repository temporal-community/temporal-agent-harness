import pytest
from pydantic_ai.models.test import TestModel
from temporalio.exceptions import ApplicationError

from sdlc_builder.activities import decide
from sdlc_builder.models import ModelRequest, Profile, TaskInput


@pytest.mark.parametrize("provider", ["openai", "anthropic", "compatible"])
async def test_provider_adapter_structured_decision(monkeypatch, provider):
    """Exercise real SDK validation and usage accounting without provider spend."""
    monkeypatch.setenv("SDLC_TEST_KEY", "fake-unit-test-key")
    created = []

    def model_factory(model_name, **kwargs):
        created.append((model_name, kwargs["provider"]))
        return TestModel(
            custom_output_args={
                "kind": "finish",
                "summary": "An answer grounded in the supplied context.",
            }
        )

    if provider == "anthropic":
        monkeypatch.setattr(
            "pydantic_ai.models.anthropic.AnthropicModel", model_factory
        )
    elif provider == "openai":
        monkeypatch.setattr(
            "pydantic_ai.models.openai.OpenAIResponsesModel", model_factory
        )
    else:
        monkeypatch.setattr("pydantic_ai.models.openai.OpenAIChatModel", model_factory)
    profile = Profile(
        id="test",
        label="Test",
        provider=provider,
        model="test-model",
        credential="env:SDLC_TEST_KEY",
        base_url="http://127.0.0.1:9999/v1" if provider == "compatible" else "",
    )
    request = ModelRequest(
        task=TaskInput(
            task_id="a" * 32, prompt="Explain this code", mode="ask", profile=profile
        ),
        context=[],
        step=0,
    )
    result = await decide(request)
    assert result.action.kind == "finish" and result.input_tokens > 0
    assert created[0][0] == "test-model"
    assert (
        "fake-unit-test-key" not in request.model_dump_json() + result.model_dump_json()
    )


async def test_missing_credential_failure_is_sanitized(monkeypatch):
    monkeypatch.delenv("SDLC_MISSING_KEY", raising=False)
    profile = Profile(
        id="test",
        label="Test",
        provider="openai",
        model="test-model",
        credential="env:SDLC_MISSING_KEY",
    )
    request = ModelRequest(
        task=TaskInput(task_id="a" * 32, prompt="Explain", profile=profile),
        context=[],
        step=0,
    )
    with pytest.raises(ApplicationError, match="No API key") as raised:
        await decide(request)
    assert raised.value.non_retryable
    assert raised.value.details[0]["category"] == "credential_missing"
