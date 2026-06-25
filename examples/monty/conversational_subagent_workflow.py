"""Conversational Monty agent that drives the script-runner as a SUBAGENT.

This is the subagent-flavoured twin of :class:`MontyChatAgentWorkflow`
(``conversational_workflow.py``). Both put a *model in the loop*: the user chats in plain
text, the model converses to gather what it needs, then writes its own Python script and runs
it in the Monty sandbox. The conversational front end — the Gemini Interactions tool-calling
loop and the script-writing system prompt — is IDENTICAL.

The ONE difference is *where the script runs*. :class:`MontyChatAgentWorkflow` runs each
model-authored script inline, via a ``run_monty_script`` ``@agent.tool_defn`` backed by a
:class:`MontyHostDriver` held on the workflow itself. This agent instead drives the barebones
:class:`~.workflow.MontyDynamicAgentWorkflow` — whose sole ``@agent.accepts`` handler,
``run_script(RunScript) -> TextReply``, executes a script in the Monty sandbox — as a
**subagent**. The script-runner is wired with
``agent.subagent_toolset(MontyDynamicAgentWorkflow, key="monty", task_queue=TASK_QUEUE)``,
which generates three model-facing tools: ``start_monty`` (start an instance, returns a short
handle), ``monty_run_script`` (send a script to that instance and get its reply), and
``stop_monty`` (shut it down). So ``monty_run_script`` is a drop-in replacement for the inline
``run_monty_script`` tool — same capability, now across a real parent→subagent boundary.

Why this exists: it's the first real end-to-end exercise of the subagent toolset
(``docs/agents-as-subagents.md``). It validates the handle indirection, multiple turns per
subagent (the per-subagent FIFO gate + turn counter + stream-offset resume), and the
``run_subagent_turn`` activity against a live child workflow.

Approval stance: this agent runs under ``always_require_approvals`` (like
:class:`MontyChatAgentWorkflow`), so it **gates the subagent tools** — every ``start_monty`` /
``monty_run_script`` / ``stop_monty`` call escalates to a human. The script's host calls
(search/book flights & hotels) run *inside the child*, which keeps its own
``dangerously_skip_all`` policy, so — unlike the inline agent — those host calls are NOT gated
in the parent. (Forwarding a gating policy into the child is a possible follow-up.)
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
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param, google_genai_client
    from pydantic import BaseModel

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    # Reuse the script-writing contract verbatim from the inline agent — the rules the model
    # must follow to author a Monty script are identical; only the tool it calls differs.
    from .conversational_workflow import _SCRIPT_CONTRACT
    from .workflow import TASK_QUEUE, MontyDynamicAgentWorkflow


DEFAULT_MODEL = "gemini-3.5-flash"

# The namespace for the wired script-runner subagent. Tool names are derived from it:
# start_monty / monty_run_script / stop_monty.
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

{_SCRIPT_CONTRACT}

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


@workflow.defn(name="MontyChatSubagentAgent")
@agent.defn
class MontyChatSubagentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Gate the subagent tools: every start_monty / monty_run_script / stop_monty call
            # escalates to a human (same stance as the inline MontyChatAgent). The script's
            # host calls run inside the child, which has its own dangerously_skip_all policy.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only function tools (no file_search).
        self._previous_interaction_id: str | None = None
        # The model-facing tools: drive the barebones MontyDynamicAgent script-runner as a
        # subagent. Built statically from its @agent.accepts handlers — no child started here.
        # Yields start_monty / monty_run_script / stop_monty.
        self._tools = agent.subagent_toolset(
            MontyDynamicAgentWorkflow,
            key=SUBAGENT_KEY,
            task_queue=TASK_QUEUE,
        )
        self._callables_by_name = {fn.__name__: fn for fn in self._tools}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # Same Temporal-aware AsyncClient as the QA / inline-Monty agents; the runner is wired
        # in so reply text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the travel assistant. Describe the trip you want (flights, hotels,
        dates, traveler name) in plain text; the assistant converses, writes Python scripts,
        and runs them against a simulated travel backend via a script-runner subagent, then
        replies with the results."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any subagent tool calls,
        feed results back, and loop until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
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
        """Execute one subagent tool call via ``run_tool`` and return its result.

        Dispatches through ``runner.run_tool``, which parks this call's id (so the tool's own
        lifecycle events correlate with the streaming activity's ``tool_requested``) AND parks
        the live runner in ``_CURRENT_RUNNER`` — which is exactly how the generated subagent
        tools reach this runner's subagent registry + start/stop/run-turn methods. The generated
        ``monty_run_script`` tool returns a typed ``TextReply`` (a pydantic ``BaseModel``), so
        serialize it with ``model_dump_json()`` for the model rather than a Python repr."""
        tool_callable = self._callables_by_name.get(call.name)
        try:
            if tool_callable is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            # call.arguments is statically typed `object`; it's a dict once the streamed
            # JSON fragments are parsed (see _execute_agent_interaction). Narrow it so the
            # **-unpack into run_tool type-checks.
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
        """Stream one ``interactions.create`` and reduce it into actionable state.

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from
        ``DeltaText`` events; function calls are captured from each ``StepStart`` whose step
        is a ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per
        step index and ``json.loads``-ed once the stream ends. (Lifted verbatim from the inline
        conversational Monty agent's loop.) Raises :class:`ApplicationError` on stream errors or if the
        stream ends without a completed event."""
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
