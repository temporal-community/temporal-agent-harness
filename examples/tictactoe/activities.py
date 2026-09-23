"""The TypeSafe System One call, as an activity-backed agent TOOL.

This is the agent's *model call*, but the harness sees it as a tool: an
``@agent.activity_tool_defn`` the workflow dispatches through ``runner.run_tool`` like any
other. That is a deliberate choice for now — the harness's model-interaction events carry
only a model id and token usage, whereas a tool's ``tool_start`` publishes its full input and
``tool_end`` its output. Modeling the call as a tool is what puts the exact ``state`` and
``questions`` sent to TypeSafe, and the answers that came back, in the console's activity
detail.

The tool is deliberately generic — it ferries a JSON ``state`` and a map of raw question dicts
to ``client.system_one`` and returns the typed answers — so the workflow owns the question
design (what state to show, which Choice/Noul/Score to ask) and this activity owns only the
I/O. The SDK accepts questions as plain dicts (``{"type": "choice", ...}``), which is what lets
the workflow build them without constructing SDK objects.

The SDK client is built lazily inside the worker process (an injected client could not cross
the activity boundary) and reused for the worker's lifetime. It reads ``TYPESAFE_API_KEY``;
``TYPESAFE_AI_API_KEY`` is accepted as an alias.

NB: no ``from __future__ import annotations`` — the tool's annotations are read concretely to
build its schema and to type the activity's result.
"""

import os
from datetime import timedelta

from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig
from typesafe_sdk import AsyncTypeSafeClient

from temporal_agent_harness.harness import agent

from .models import ChoiceResult, ScoreResult, SystemOneRequest, SystemOneResult

_client: AsyncTypeSafeClient | None = None


def typesafe_api_key() -> str | None:
    return os.environ.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_AI_API_KEY")


def _typesafe_client() -> AsyncTypeSafeClient:
    global _client
    if _client is None:
        _client = AsyncTypeSafeClient(api_key=typesafe_api_key())
    return _client


@agent.activity_tool_defn(
    # A read-only API call that judges a board: never unsafe, so a policy that auto-approves
    # inherently safe tools lets every turn flow without a click.
    inherently_safe=True,
    activity_config=ActivityConfig(
        start_to_close_timeout=timedelta(seconds=60),
        retry_policy=RetryPolicy(maximum_attempts=3),
    ),
)
async def typesafe_system_one(request: SystemOneRequest) -> SystemOneResult:
    """Ask TypeSafe's System One model the given questions about the given state.

    One request, every question answered in parallel over the same state. The input is the
    exact request body sent to the API; the output is the typed answers keyed by question id.
    """
    response = await _typesafe_client().system_one(
        request.state,
        request.questions,  # type: ignore[arg-type]  # raw dicts are accepted; see module notes
        model=request.model,
    )
    return SystemOneResult(
        model=response.model,
        request_id=response.request_id,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        nouls={key: answer.noul for key, answer in response.nouls.items()},
        choices={
            key: ChoiceResult(
                choice=answer.choice,
                confidence=answer.confidence,
                probabilities=dict(answer.probabilities),
            )
            for key, answer in response.choices.items()
        },
        scores={
            key: ScoreResult(
                score=answer.score,
                confidence=answer.confidence,
                probabilities=dict(answer.probabilities),
                legend=dict(answer.legend),
            )
            for key, answer in response.scores.items()
        },
    )


# Handed to `AgentHarnessPlugin(tools=...)` so the worker registers the durable activity body.
ALL_TOOLS = [typesafe_system_one]
