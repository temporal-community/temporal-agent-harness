"""Conversational travel agent that gives the model Code Mode over the travel tools.

The user chats in plain text; a *model in the loop* converses to gather what it needs, then
**writes a Python script and runs it** to search and book flights/hotels, and replies in prose.
It does this through the harness Code Mode feature: :func:`agent.code_mode_tool` turns the travel
activity tools into a single ``run_travel_code`` tool that executes a model-authored script in a
sandbox, where each tool is an async host function the script calls. Every host call runs as a
durable, approval-gated activity, and the script can combine many with real control flow (loops,
``asyncio.gather`` concurrency).

The conversational front end is a Gemini Interactions tool-calling loop exposing that one Code
Mode tool. It uses only a custom *function* tool, which chains cleanly across turns via 
``previous_interaction_id`` — so multi-turn conversation works. The Code Mode tool advertises the 
exact host-function signatures + result shapes in its own (generated) description, so the system 
prompt only needs to set the persona and point the model at the tool.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from functools import partial
from typing import Literal, Sequence

from pydantic import BaseModel
from temporalio import workflow
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
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param, google_genai_client
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import activities


TASK_QUEUE = "monty-dynamic-agent"
SUPPORTED_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite")
DEFAULT_MODEL = SUPPORTED_MODELS[0]


class SetModel(BaseModel):
    """Which model this Monty session should use for subsequent turns."""

    # A Literal (not a bare str) so the choice is enforced rather than merely suggested:
    # pydantic rejects anything else at the update boundary, and the same constraint shows up
    # as an enum in this handler's `parameters` JSON schema — which is what lets a generic
    # client render a dropdown without knowing anything about Monty.
    model: Literal[SUPPORTED_MODELS]  # type: ignore[valid-type]


SYSTEM_INSTRUCTION = """\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small async Python scripts and run them with the `run_travel_code` tool, which exposes the \
travel operations as async host functions your script calls. The tool's description gives the \
exact host-function signatures and result shapes — follow them; index results with normal Python.

How to behave:
- Converse naturally. Ask brief clarifying questions when you're missing something essential \
(origin/destination, dates, traveler name) — don't interrogate; make reasonable assumptions \
and state them.
- When you have enough to make progress, WRITE A SCRIPT and call `run_travel_code`. Run \
independent host calls concurrently with `asyncio.gather`; keep each script focused (search, or \
book, or summarize) so you can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns as the \
conversation continues.
- Never invent flight/hotel ids or confirmation codes — only use ones returned by a script."""


@workflow.defn(name="MontyChatAgent")
@agent.defn
class MontyChatAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Demo stance: require human approval for EVERY tool call — both the
            # `run_travel_code` tool and each host call the script makes (search/book flights &
            # hotels), since every call is dispatched through run_tool and gated.
            # always_require_approvals does not auto-approve even inherently_safe tools.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only a function tool (no file_search).
        self._previous_interaction_id: str | None = None
        # The single model-facing tool: Code Mode over the travel tools. The model writes a
        # Python script that calls the travel operations as async host functions; each host call
        # runs as a durable, approval-gated activity via run_tool.
        self._code_tool = agent.code_mode_tool(
            [
                activities.search_flights_activity,
                activities.search_hotels_activity,
                activities.book_flight_activity,
                activities.book_hotel_activity,
                activities.get_trip_summary_activity,
            ],
            name="run_travel_code",
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # The Temporal-aware AsyncClient from the Gemini plugin; the runner is wired in so reply
        # text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes and runs Python
        scripts against a simulated travel backend as needed, and replies with the results."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ACCEPT: reconfiguring the session is not work, so it should not wait behind work. It
    # joins the open turn and applies immediately, which is the whole point of being able to
    # switch models while the agent is mid-conversation. model_callable=False keeps it off a
    # parent agent's generated toolset by default — choosing the model is an operator's call,
    # not something a driving model should do to its own child.
    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_model(self, message: SetModel) -> TextReply:
        """Set the model this session uses for subsequent turns. Takes effect on the next
        model call, so a turn already in flight finishes on the model it started with."""
        self._model = message.model
        return TextReply(text=f"Model set to **{message.model}**.")

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any ``run_travel_code``
        calls, feed results back, and loop until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
        tools = [function_param(self._code_tool)]
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
        """Execute one ``run_travel_code`` call via ``run_tool`` and return its result.

        ``run_tool`` parks the call id so the script's host calls and the tool's own
        lifecycle events correlate with the streaming activity's ``tool_requested``."""
        try:
            if call.name != self._code_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(
                call.id, self._code_tool, **call.arguments
            )
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(result),
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
        step index and ``json.loads``-ed once the stream ends. (Lifted verbatim from the QA
        agent's loop.) Raises :class:`ApplicationError` on stream errors or if the stream
        ends without a completed event."""
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
