"""Gemini conversational Monty agent that drives the script-runner as a SUBAGENT.

This is the Gemini counterpart to ``conversational_subagent_workflow.py``. It exposes
the same parent->subagent Monty flow as the OpenAI subagent workflow, but drives the
model with Gemini Interactions and owns the streamed function-call loop directly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import timedelta
from functools import partial
from typing import Any, cast

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
    from pydantic import BaseModel

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .conversational_gemini_workflow import (
        GEMINI_DEFAULT_MODEL,
        GEMINI_MODEL_OPERATOR_COMMAND,
        GEMINI_SUPPORTED_MODELS,
        SET_MODEL_COMMAND,
        _SCRIPT_AUTHORING_RULES,
    )
    from .workflow import TASK_QUEUE, MontyDynamicAgentWorkflow


SUBAGENT_KEY = "monty"


SYSTEM_INSTRUCTION = f"""\
You are a friendly travel-booking assistant. You help users search and book flights and \
hotels and assemble trip itineraries. You don't have these abilities directly — instead you \
write small **async** Python scripts and run them in a Monty sandbox. Every script MUST be \
async: the host functions are coroutines you `await`, you run independent ones concurrently \
with `asyncio.gather`, and you wrap the body in `asyncio.run(main())` (full rules below).

You run scripts through a dedicated **script-runner subagent**, using these tools:
- `start_{SUBAGENT_KEY}`: start a script-runner and get back a short `subagent` handle. Call \
this ONCE at the start of the conversation, then reuse the same handle for every script.
- `{SUBAGENT_KEY}_run_script`: send a script to a running script-runner. Pass the handle from \
`start_{SUBAGENT_KEY}` as `subagent`, and the script in `message` (a RunScript object with a \
`script` field). The reply carries the script's printed output and final value.

{_SCRIPT_AUTHORING_RULES}

How to behave:
- Converse naturally. Ask brief clarifying questions when you're missing something essential \
(origin/destination, dates, traveler name) — don't interrogate; make reasonable assumptions \
and state them.
- Before running your first script, call `start_{SUBAGENT_KEY}` to get a handle. Keep using \
that one handle for the rest of the conversation; don't start a new script-runner per script.
- When you have enough to make progress, WRITE A SCRIPT and run it with \
`{SUBAGENT_KEY}_run_script`. Keep each script focused (search, or book, or summarize) so you \
can react to results.
- After a tool result, read it and reply to the user in plain, friendly prose — summarize \
options, prices, confirmations. You may run more scripts in follow-up turns.
- Never invent flight_ids/hotel_ids/confirmation codes — only use ones returned by a script."""


@workflow.defn(name="MontyChatGeminiSubagentAgent")
@agent.defn
class MontyChatGeminiSubagentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            operator_commands=[GEMINI_MODEL_OPERATOR_COMMAND],
            operator_command_handler=self._handle_operator_command,
        )
        self._model: str = GEMINI_DEFAULT_MODEL
        self._previous_interaction_id: str | None = None
        self._tools = agent.subagent_toolset(
            MontyDynamicAgentWorkflow,
            key=SUBAGENT_KEY,
            task_queue=TASK_QUEUE,
        )
        self._callables_by_name = {fn.__name__: fn for fn in self._tools}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the Gemini-backed travel assistant via a script-runner subagent."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        reply = self._handle_operator_command(command)
        if reply is not None:
            return reply
        return TextReply(
            text=(
                f"Unknown Monty slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _handle_operator_command(self, command: SlashCommand) -> TextReply | None:
        if command.name == SET_MODEL_COMMAND:
            return self._set_model(command.arg)
        return None

    def _set_model(self, model: str | None) -> TextReply:
        if model is None or model not in GEMINI_SUPPORTED_MODELS:
            choices = ", ".join(f"`{model}`" for model in GEMINI_SUPPORTED_MODELS)
            return TextReply(text=f"Choose one of: {choices}.")
        self._model = model
        return TextReply(text=f"Model set to **{self._model}**.")

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one Gemini conversational turn and execute requested subagent tools."""
        tools = [
            function_param(fn)
            for fn in self._tools
            if fn.__name__ != f"stop_{SUBAGENT_KEY}"
        ]
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
        """Execute one subagent tool call via ``run_tool`` and return its result."""
        tool_callable = self._callables_by_name.get(call.name)
        try:
            if tool_callable is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            arguments = (
                cast("dict[str, Any]", call.arguments)
                if isinstance(call.arguments, dict)
                else {}
            )
            result = await self._runner.run_tool(call.id, tool_callable, **arguments)
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": result.model_dump_json()
                if isinstance(result, BaseModel)
                else str(result),
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
        """Stream one ``interactions.create`` and reduce it into actionable state."""
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
