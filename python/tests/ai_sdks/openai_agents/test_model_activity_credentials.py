"""A worker must start without OPENAI_API_KEY; only a model call needs the key."""
from __future__ import annotations

import pytest
from openai import OpenAIError

from temporal_agent_harness.ai_sdks.openai_agents._invoke_model_activity import ModelActivity


def test_default_provider_is_built_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    activity = ModelActivity()  # starts without credentials
    with pytest.raises(OpenAIError, match="api_key|OPENAI_API_KEY"):
        activity._get_model_provider()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert activity._get_model_provider() is activity._get_model_provider()


def test_explicit_provider_is_used_as_given() -> None:
    provider = object()
    assert ModelActivity(model_provider=provider)._get_model_provider() is provider  # type: ignore[arg-type]
