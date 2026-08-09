"""A hello-world Gemini agent that runs against EITHER Gemini backend, unchanged.

A conversational agent that answers in plain text and can call a single tool
(``get_weather``), driven by the Gemini **Interactions API** tool-calling loop — the same
API surface the bigger Gemini examples (Monty, the wiki keeper) and the internal QA
prototype use, reduced to the smallest thing that still exercises it end to end.

Its reason for existing is the *backend* question, not the agent: it is the proof-of-concept
for pointing a harness Gemini agent at Google's **Gemini Enterprise Agent Platform** (GEAP —
the 2026 rebrand of Vertex AI) instead of the consumer Gemini Developer API.

**Nothing in this file knows which backend it is talking to, and that is the point.** The
workflow calls ``gemini.interactions.create(...)`` on the harness's Temporal-aware shim,
which forwards the kwargs to an activity; the real ``genai.Client`` living in that activity
is what resolves the endpoint and the credentials. So the entire consumer-vs-GEAP switch is
worker-side configuration (see ``worker.py``) — no workflow change, no new workflow history,
and a running session cannot tell the difference. That separation is what makes the migration
cheap, and this example is here to demonstrate it.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is
registered in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from functools import partial
from typing import Sequence

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import (
        ErrorEvent,
        FunctionCallStep,
        InteractionCompletedEvent,
        StepDelta,
        StepStart,
        ToolParam,
    )
    from google.genai._interactions.types.error_event import Error
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )
    from google.genai._interactions.types.interaction_create_params import Input
    from google.genai._interactions.types.step_delta import (
        DeltaArgumentsDelta,
        DeltaText,
    )
    from google.genai.client import AsyncClient

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


TASK_QUEUE = "hello-gemini-enterprise"

# The model id is passed through to whichever backend the worker configured. Model
# availability is NOT identical across the two — a model served on the Gemini Developer API is
# not automatically served on GEAP, and vice versa — so this is the one thing besides the
# client construction that a migration has to re-check even though the code is unchanged. Edit
# it here (not via an env var: reading the environment in workflow code would make the model a
# non-deterministic input that isn't recorded in history).
DEFAULT_MODEL = "gemini-3.5-flash"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

You have one tool, `get_weather`, which returns the current weather for a city. When the user
asks about the weather somewhere, call it (don't guess), then tell them the answer in a sentence
or two. For anything else, just reply directly."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Canned lookup — a hello-world, not a real weather service. Being an inline
    # `tool_defn` (not an activity tool) keeps the worker free of tool activities, so the
    # only thing the worker registers is the Gemini plugin whose client we are testing.
    return f"It's 72°F and sunny in {city}."


@workflow.defn(name="HelloGeminiEnterpriseAgent")
@agent.defn
class HelloGeminiEnterpriseAgentWorkflow:
    """A one-tool conversational Gemini agent, backend-agnostic by construction."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls. A caller can tighten this per
            # session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn, so the
        # model keeps context without us resending the transcript. Safe to chain here because
        # this agent uses only function tools (no file_search).
        self._previous_interaction_id: str | None = None
        self._tools_by_name = {get_weather.__name__: get_weather}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # The Temporal-aware AsyncClient from the Gemini plugin. Note what is NOT passed:
        # no vertexai/project/location. Those matter only to the SDK's URL formatting for the
        # `models.*` path, which this agent never touches — every interactions call is
        # forwarded verbatim to the worker's activity, where the real client owns the
        # endpoint. The runner is wired in so reply text streams to the turn stream live.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
                # Bounded retries, unlike the other Gemini examples' unlimited default. This
                # example's whole job is to probe a backend, so its most likely failure is a
                # PERMANENT config error — API not enabled, model not served here, endpoint
                # doesn't exist. Retrying those forever turns a clear answer into a hang. Three
                # attempts still absorb a transient 429/503, then surface the real message.
                retry_policy=RetryPolicy(maximum_attempts=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask it anything; ask about the weather in a city and it
        calls its `get_weather` tool and tells you what it found."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any tool calls, feed the
        results back, and loop until the model replies with no further calls. Updates
        ``self._previous_interaction_id`` for chaining the next turn.

        The Interactions API has no automatic function calling, so this loop — not the SDK —
        is the agent's inner loop. It is the same shape as the other Gemini examples; only
        the toolset is smaller.
        """
        tools = [function_param(get_weather)]
        next_input: Input = user_text
        while True:
            (
                reply_text,
                pending_calls,
                self._previous_interaction_id,
            ) = await self._execute_agent_interaction(
                gemini=gemini,
                model=self._model,
                input=next_input,
                tools=tools,
                system_instruction=SYSTEM_INSTRUCTION,
                previous_interaction_id=self._previous_interaction_id,
            )

            if not pending_calls:
                return reply_text

            next_input = await asyncio.gather(
                *(self._run_one_tool(fc) for fc in pending_calls)
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Execute one tool call through ``run_tool`` and shape its result for the model.

        Going through ``run_tool`` (rather than calling the function directly) is what keeps
        the harness in charge: approval-policy evaluation and the tool's
        ``tool_start`` / ``tool_end`` / ``tool_error`` turn-stream events. A failure is
        returned to the model as an error result rather than raised, so one bad call does not
        end the turn.
        """
        try:
            tool = self._tools_by_name.get(call.name)
            if tool is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(call.id, tool, **call.arguments)
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": _render_tool_result(result),
            }
            if call.signature:
                response["signature"] = call.signature
            return response
        except Exception as e:
            response = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(e),
                "is_error": True,
            }
            if call.signature:
                response["signature"] = call.signature
            return response

    async def _execute_agent_interaction(
        self,
        *,
        gemini: AsyncClient,
        model: str,
        input: Input,
        tools: Sequence[ToolParam],
        system_instruction: str,
        previous_interaction_id: str | None,
    ) -> tuple[str, list[FunctionCallStep], str]:
        """Stream one ``interactions.create`` and reduce it into actionable state.

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from
        ``DeltaText`` events; function calls are captured from each ``StepStart`` whose step
        is a ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per
        step index and ``json.loads``-ed once the stream ends. Raises
        :class:`ApplicationError` on a stream error, or if the stream ends without a completed
        event.

        A backend mismatch (endpoint not serving Interactions, model not available on this
        backend, API not enabled on the project) surfaces as the activity failing before this
        reduction ever runs — look at the activity failure in the Temporal UI, not here.
        """
        interactions_create_fn = partial(
            gemini.interactions.create,
            model=model,
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            stream=True,
        )
        if previous_interaction_id:
            stream = await interactions_create_fn(
                previous_interaction_id=previous_interaction_id
            )
        else:
            stream = await interactions_create_fn()

        text_parts: list[str] = []
        calls_by_index: dict[int, FunctionCallStep] = {}
        arg_buffers: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=Error(message=msg, code=code)):
                    raise ApplicationError(
                        msg or "stream error", type=code or "stream_error"
                    )
                case ErrorEvent():
                    raise ApplicationError("unknown stream error", type="stream_error")
                case StepStart(index=idx, step=FunctionCallStep() as call):
                    calls_by_index[idx] = call
                case StepDelta(
                    index=idx, delta=DeltaArgumentsDelta(arguments=args)
                ) if args:
                    arg_buffers[idx] = arg_buffers.get(idx, "") + args
                case StepDelta(delta=DeltaText(text=text)) if text:
                    text_parts.append(text)
                case InteractionCompletedEvent(interaction=interaction):
                    interaction_id = interaction.id

        if interaction_id is None:
            raise ApplicationError(
                "stream ended without interaction.completed event",
                type="stream_error",
            )

        function_calls = [
            calls_by_index[idx].model_copy(
                update={"arguments": json.loads(arg_buffers[idx])}
            )
            if arg_buffers.get(idx)
            else calls_by_index[idx]
            for idx in sorted(calls_by_index)
        ]
        return "".join(text_parts), function_calls, interaction_id


def _render_tool_result(result: object) -> str:
    """Render a tool's return value to text for the model."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)
