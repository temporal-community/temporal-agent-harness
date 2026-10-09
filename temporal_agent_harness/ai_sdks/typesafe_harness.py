"""Harness events around the canonical ``temporalio-typesafe`` durable proxy.

The provider plugin owns execution, retries and native response decoding. This
adapter publishes one workflow-side span per call, including all its retries.
TypeSafe is included in the base harness install. Register ``TypeSafePlugin``
before ``AgentHarnessPlugin``. Jev approvals use the same provider activity, with
their own approval events and threshold logic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.typesafe.workflow import SystemOneResult, TemporalTypeSafe
    from typesafe_sdk import Choice, Noul, Score

from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    TokenUsage,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

__all__ = ["HarnessTypeSafe"]


class HarnessTypeSafe:
    """A per-agent durable TypeSafe proxy publishing into its active turn.

    Constructor and call defaults follow ``TemporalTypeSafe``. The explicit
    runner keeps concurrent sessions isolated. Native results are returned
    unchanged, including answers, confidence, served model and request ID.
    """

    def __init__(
        self,
        *,
        runner: AgentWorkflowRunner,
        model: str | None = None,
        activity_config: ActivityConfig | None = None,
        response_model: str | None = None,
    ) -> None:
        self._runner = runner
        self._model = model
        self._proxy = TemporalTypeSafe(
            model=model,
            activity_config=activity_config,
            response_model=response_model,
        )

    async def system_one(
        self,
        state: Mapping[str, Any] | Sequence[Any] | str,
        questions: Mapping[str, Choice | Noul | Score | dict[str, Any]],
        *,
        model: str | None = None,
        activity_config: ActivityConfig | None = None,
        response_model: str | None = None,
    ) -> SystemOneResult:
        """Ask typed questions durably, closing the span on every outcome.

        The span names the requested model on both events; the served model
        remains in the native response. Unreported token counts stay absent.
        Call inside an active harness handler, as required by runner.publish.
        """
        requested_model = model or self._model
        usage = None
        self._runner.publish(ModelInteractionStarted(model=requested_model))
        try:
            result = await self._proxy.system_one(
                state,
                questions,
                model=model,
                activity_config=activity_config,
                response_model=response_model,
            )
            native_usage = result.response.usage
            if native_usage is not None:
                usage = TokenUsage(
                    input_tokens=native_usage.input_tokens,
                    output_tokens=native_usage.output_tokens,
                )
            return result
        finally:
            self._runner.publish(
                ModelInteractionEnded(model=requested_model, usage=usage)
            )
