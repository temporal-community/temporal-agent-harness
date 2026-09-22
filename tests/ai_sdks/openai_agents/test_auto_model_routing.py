"""Tests for automatic model selection."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

from temporal_agent_harness.model_routing import (
    ModelRoutingActivityOptions,
    ModelRoutingDecision,
    ModelRoutingRequest,
    OPENAI_MODEL_ROUTING_CONFIG,
    ROUTE_MODEL_ACTIVITY,
    resolve_model,
    router,
)
from temporal_agent_harness.model_routing import activities as routing_activities
from temporal_agent_harness.model_routing.activities import route_model
from temporal_agent_harness.model_routing.classifier import Classification


def _classification(
    tier: str, confidence: float = 0.9, stakes: float = 0.0
) -> Classification:
    return Classification(
        tier=tier,
        tier_confidence=confidence,
        stakes_score=stakes,
        stakes_confidence=0.9,
    )


@pytest.mark.parametrize(
    ("tier", "model"),
    [
        ("simple", "gpt-5.6-luna"),
        ("moderate", "gpt-5.6-terra"),
        ("complex", "gpt-5.6-sol"),
        ("frontier", "gpt-6-astra"),
    ],
)
def test_router_selects_the_configured_model(monkeypatch, tier, model):
    monkeypatch.setattr(router, "classify", lambda _prompt: _classification(tier))

    result = router.route("test prompt", OPENAI_MODEL_ROUTING_CONFIG)

    assert result.tier == tier
    assert result.model_id == model
    assert not result.fallback_applied


def test_router_falls_back_on_low_confidence(monkeypatch):
    monkeypatch.setattr(
        router, "classify", lambda _prompt: _classification("simple", confidence=0.2)
    )

    result = router.route("ambiguous prompt", OPENAI_MODEL_ROUTING_CONFIG)

    assert result.tier == OPENAI_MODEL_ROUTING_CONFIG.fallback_tier
    assert result.model_id == "gpt-5.6-terra"
    assert result.fallback_applied


def test_router_uses_the_configured_model_set(monkeypatch):
    monkeypatch.setattr(router, "classify", lambda _prompt: _classification("simple"))
    config = OPENAI_MODEL_ROUTING_CONFIG.model_copy(
        update={
            "tier_to_model": {
                **OPENAI_MODEL_ROUTING_CONFIG.tier_to_model,
                "simple": "custom-small-model",
            }
        }
    )

    result = router.route("test prompt", config)

    assert result.model_id == "custom-small-model"


def test_router_escalates_high_stakes(monkeypatch):
    monkeypatch.setattr(
        router, "classify", lambda _prompt: _classification("complex", stakes=2.0)
    )

    result = router.route("high-stakes task", OPENAI_MODEL_ROUTING_CONFIG)

    assert result.tier == "frontier"
    assert result.model_id == "gpt-6-astra"
    assert result.stakes_override_applied


@pytest.mark.asyncio
async def test_routing_activity_returns_serializable_decision(monkeypatch):
    decision = router.RouteResult(
        model_id="gpt-5.6-sol",
        tier="complex",
        classification=_classification("complex", confidence=0.91, stakes=1.6),
        fallback_applied=False,
        stakes_override_applied=True,
    )
    monkeypatch.setattr(routing_activities, "route", lambda _prompt, _config: decision)

    result = await route_model(
        ModelRoutingRequest(
            prompt="review this migration", config=OPENAI_MODEL_ROUTING_CONFIG
        )
    )

    assert result.model_id == "gpt-5.6-sol"
    assert result.tier_confidence == 0.91
    assert result.stakes_score == 1.6
    assert result.stakes_override_applied


@pytest.mark.asyncio
async def test_routing_activity_falls_back_without_exposing_error(monkeypatch, caplog):
    def fail(_prompt: str, _config: object):
        raise RuntimeError("credential must not be persisted")

    monkeypatch.setattr(routing_activities, "route", fail)
    caplog.set_level(logging.WARNING)

    result = await route_model(
        ModelRoutingRequest(prompt="anything", config=OPENAI_MODEL_ROUTING_CONFIG)
    )

    assert result.model_id == "gpt-5.6-terra"
    assert result.fallback_reason == "router_unavailable"
    assert "credential" not in result.model_dump_json()
    assert "credential" not in caplog.text


@pytest.mark.asyncio
async def test_routing_activity_reports_a_missing_optional_dependency(monkeypatch):
    def missing_dependency(_prompt: str, _config: object):
        raise ModuleNotFoundError(name="typesafe_sdk")

    monkeypatch.setattr(routing_activities, "route", missing_dependency)

    with pytest.raises(ApplicationError, match="model-routing") as error:
        await route_model(
            ModelRoutingRequest(prompt="anything", config=OPENAI_MODEL_ROUTING_CONFIG)
        )

    assert error.value.type == "model_routing_not_installed"


@pytest.mark.asyncio
async def test_resolve_model_schedules_activity(monkeypatch):
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_execute_activity(*args, **kwargs):
        calls.append((args, kwargs))
        return ModelRoutingDecision(
            model_id="gpt-5.6-sol",
            tier="complex",
            fallback_applied=False,
            stakes_override_applied=False,
        )

    from temporal_agent_harness.model_routing import workflow as routing_workflow

    monkeypatch.setattr(
        routing_workflow.workflow, "execute_activity", fake_execute_activity
    )

    assert (
        await resolve_model(
            "auto",
            "implement a multi-module refactor",
            OPENAI_MODEL_ROUTING_CONFIG,
            ModelRoutingActivityOptions(),
        )
        == "gpt-5.6-sol"
    )
    assert len(calls) == 1
    assert calls[0][0][0] == ROUTE_MODEL_ACTIVITY
    request = calls[0][0][1]
    assert request.prompt == "implement a multi-module refactor"
    assert request.config == OPENAI_MODEL_ROUTING_CONFIG
    assert calls[0][1]["result_type"] is ModelRoutingDecision
    assert calls[0][1]["start_to_close_timeout"] == timedelta(seconds=60)
    assert calls[0][1]["task_queue"] is None
    assert calls[0][1]["cancellation_type"] is ActivityCancellationType.TRY_CANCEL


@pytest.mark.asyncio
async def test_explicit_model_bypasses_routing():
    model_id = await resolve_model(
        "gpt-5.6-luna",
        "this must not be routed",
        OPENAI_MODEL_ROUTING_CONFIG,
        ModelRoutingActivityOptions(),
    )

    assert model_id == "gpt-5.6-luna"
