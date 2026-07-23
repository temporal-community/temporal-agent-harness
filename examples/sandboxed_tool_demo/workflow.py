# ABOUTME: A real Gemini-backed conversational agent with exactly one tool: run_bash, a
# sandboxed, arbitrary-bash-command tool that requires human approval on EVERY call (see
# approval_policy_default below) — never auto-approved, since a bash command can do anything a
# shell can. The bash command itself runs inside the agent's sandbox (tools.py's SANDBOX), never
# on the worker's own machine.

import asyncio
import json
from datetime import timedelta
from functools import partial
from typing import Sequence

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

# remote-box (transitively imported by tools.py, via httpx/e2b/daytona) needs pass-through
# treatment — Temporal's workflow sandbox restricts modules like httpx's on import. EVERY
# temporal_agent_harness import must live in this SAME block, including agent_protocol and the
# Gemini plugin glue: even though agent/AgentWorkflowRunner/tools were already wrapped together,
# importing agent_protocol separately (unwrapped, above this block) was enough on its own to
# split agent_workflow.py into two distinct loaded copies — the sandbox's restricted one and the
# pass-through one — each with its OWN `_CURRENT_RUNNER` contextvar, so `run_tool` (set on one
# copy) became invisible to `_apply_approval_policy` (read on the other). Confirmed by direct
# repro: moving just one import above the block reproduces "tool ... has no active runner" every
# time; moving it back in fixes it. Mirrors `harness/code_mode/tool.py`/`driver.py`'s own
# established pattern of wrapping EVERY harness-module import together in one block for exactly
# this reason — take "every" fully literally, not just the ones that look obviously necessary.
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
    from google.genai._interactions.types.step_delta import DeltaArgumentsDelta, DeltaText
    from google.genai.client import AsyncClient

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )

    from examples.sandboxed_tool_demo.tools import SANDBOX, run_bash

TASK_QUEUE = "sandboxed-tool-demo"
DEFAULT_MODEL = "gemini-3.6-flash"

SYSTEM_INSTRUCTION = """\
You are a helpful assistant with exactly one tool, `run_bash`, which runs an arbitrary bash \
command inside an isolated sandbox (never on any machine the user or operator directly controls) \
and returns its stdout, stderr, and exit code.

EVERY call to run_bash pauses for a human to explicitly approve it before it runs — there is no \
way around this gate, by design, since a bash command can do anything. Before calling it, briefly \
say what you're about to run and why, so the approval makes sense to whoever grants it. If a call \
is denied, don't just retry the same thing — ask the user what they'd prefer instead.

Keep replies brief and concrete: say what you ran (or tried to run) and what happened."""


@workflow.defn(name="SandboxedToolDemoAgent")
@agent.defn
class SandboxedToolDemoAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # run_bash executes arbitrary shell commands inside the sandbox — gate EVERY call,
            # no exceptions. (run_bash also isn't inherently_safe, so allow_inherently_safe
            # wouldn't auto-approve it either — always_require_approvals states the intent
            # directly: nothing here is ever eligible for auto-approval.)
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            sandbox=SANDBOX,
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn.
        self._previous_interaction_id: str | None = None

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        # The Temporal-aware AsyncClient from the Gemini plugin; the runner is wired in so reply
        # text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. It has one tool, run_bash, which pauses for your explicit
        approval every single time before it runs."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any run_bash call (pausing
        for approval, then executing in the sandbox), feed the result back, and loop until the
        model replies with no further calls. Updates ``self._previous_interaction_id`` for
        chaining the next turn."""
        tools = [function_param(run_bash)]
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
        """Execute one run_bash call via ``run_tool`` and return its result.

        ``run_tool`` applies the agent's approval policy (always_require_approvals here — the
        call pauses, published as ``ToolApprovalRequested``, until a human resolves it), then
        dispatches the durable, sandboxed activity. A denial (``ToolApprovalDenied``) or any
        other failure is reported back to the model as an error result rather than failing the
        turn, so the model can react (apologize, ask what to do instead, etc.)."""
        try:
            result = await self._runner.run_tool(call.id, run_bash, **call.arguments)
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": result.model_dump_json(),
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

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from ``DeltaText``
        events; function calls are captured from each ``StepStart`` whose step is a
        ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per step
        index and ``json.loads``-ed once the stream ends. Raises :class:`ApplicationError` on
        stream errors or if the stream ends without a completed event."""
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
