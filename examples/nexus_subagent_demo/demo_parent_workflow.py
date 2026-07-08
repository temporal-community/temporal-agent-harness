"""Demo parent that drives subagents discovered dynamically via the Nexus agent registry.

Two flavors of the same underlying mechanism (discover via the registry, then synthesize real
per-handler tools from the schemas that discovery returns, via ``subagents.registry``'s
``as_tool()``/``discover_and_build_tools()``):

* ``echo_via_subagent``/``list_dynamic_tools``/``dynamic_call`` — model-FREE. A handler calls
  the runner's subagent methods (or a synthesized tool) directly, useful for scripted
  end-to-end validation with no LLM key needed. Mirrors
  ``tests/examples/monty/_subagent_e2e_parent.py``'s "drive it without a model" pattern.
* ``ask`` — model-driven, via the real Gemini Interactions API (mirrors
  ``examples/monty/conversational_subagent_workflow.py``'s shape). Each turn: discover the
  registry fresh, build one real tool per discovered handler via ``as_tool()``, hand those to
  Gemini as its tool list (via ``tool_declaration()`` — NOT ``function_param()``, since these
  tools have no real Python type to introspect, only a JSON Schema fetched over the wire — see
  ``subagents.registry.toolset``), and let the model decide which to call. There is no separate
  "discover_subagents" tool exposed to the model at all: discovery already happened before the
  model ever saw a tool list, exactly the design discussed for making dynamic discovery not
  require the model to remember an extra step.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Sequence
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
    from google.genai._interactions.types.step_delta import DeltaArgumentsDelta, DeltaText
    from google.genai.client import AsyncClient
    from pydantic import BaseModel, Field

    from temporal_agent_harness.ai_sdks.google_genai_plugin import google_genai_client
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from subagents.registry import discover_and_build_tools, start_subagent_from_registry, tool_declaration

# Read once, at workflow-construction time — same operational discipline as the harness's own
# NEXUS_SUBAGENT env read (subagent_toolset.py): a deployment-wide toggle, not meant to change
# mid-flight for a given running workflow.
REGISTRY_ENDPOINT = os.environ.get("AGENT_REGISTRY_NEXUS_ENDPOINT", "agent-registry-endpoint")

DEMO_PARENT_TASK_QUEUE = "subagent-demo-parent"

# The registry key the Echo worker self-registers under (see echo_worker.py).
ECHO_AGENT_KEY = "echo"

DEFAULT_MODEL = "gemini-3.5-flash"

SYSTEM_INSTRUCTION = """\
You are a demo assistant with no fixed capabilities of your own — everything you can do comes \
from subagents discovered dynamically from a Nexus agent registry, moments ago, this turn. \
Each tool in your tool list was synthesized directly from a currently-registered subagent's \
real input/output schema; there is no separate "discover" tool for you to call — that already \
happened before you were shown this list. Call whichever tool fits the user's request, \
matching its schema exactly. If nothing fits, say so plainly — it may simply be that no \
subagent is registered for that capability right now."""


class EchoViaSubagent(BaseModel):
    """Drive one echo subagent turn through the Nexus-brokered path."""

    text: str = Field(description="Text to send to the echo subagent.")


class ListDynamicTools(BaseModel):
    """List every tool the registry's current directory would synthesize right now (see
    subagents.registry.toolset's as_tool()/discover_and_build_tools())."""


class DynamicToolDeclaration(BaseModel):
    """One synthesized tool's model-facing declaration, as built by subagents.registry.toolset's
    tool_declaration()."""

    name: str
    description: str
    parameters: dict[str, Any]


class ListDynamicToolsReply(BaseModel):
    """Every tool discover_and_build_tools() would synthesize from the registry's current
    directory, model-facing-declaration only — no subagent was started to produce this."""

    tools: list[DynamicToolDeclaration]


class DynamicCall(BaseModel):
    """Call one dynamically-synthesized tool directly — simulating what a model-driven tool
    loop would do after seeing list_dynamic_tools' declarations."""

    tool_name: str = Field(description="One of the names from list_dynamic_tools' output.")
    arguments: dict[str, Any] = Field(default_factory=dict)


@workflow.defn(name="SubagentDemoParent")
@agent.defn
class SubagentDemoParentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # agent_key -> handle, shared across every discover_and_build_tools()/as_tool() call
        # this parent makes, so a dynamically-synthesized tool reuses one running instance per
        # agent_key across turns instead of leaking a fresh one on every call.
        self._dynamic_handle_cache: dict[str, str] = {}
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only function tools (no file_search).
        self._previous_interaction_id: str | None = None

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # Same Temporal-aware AsyncClient as the Monty conversational agents; the runner is
        # wired in so reply text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def echo_via_subagent(self, msg: EchoViaSubagent) -> TextReply:
        """Discover the registered "echo" subagent via the Nexus agent registry, start an
        instance of it, send it `text`, stop it, and reply with what it echoed back."""
        handle = await start_subagent_from_registry(
            self._runner, ECHO_AGENT_KEY, REGISTRY_ENDPOINT
        )
        try:
            result = await self._runner.run_subagent_turn(handle, "echo", {"text": msg.text})
        finally:
            await self._runner.stop_subagent(handle)
        return TextReply(text=result.get("text", ""))

    @agent.accepts
    async def list_dynamic_tools(self, msg: ListDynamicTools) -> ListDynamicToolsReply:
        """Force a fresh discover_subagents call and synthesize a real per-handler tool for
        each discovered handler via as_tool(), then report each tool's model-facing
        declaration (name, description, parameters) exactly as it would be shown to a model.
        No subagent is actually invoked."""
        tools = await discover_and_build_tools(
            self._runner, REGISTRY_ENDPOINT, self._dynamic_handle_cache
        )
        return ListDynamicToolsReply(
            tools=[
                DynamicToolDeclaration(
                    name=decl["name"],
                    description=decl["description"],
                    parameters=decl["parameters"],
                )
                for decl in (tool_declaration(fn) for fn in tools)
            ]
        )

    @agent.accepts
    async def dynamic_call(self, msg: DynamicCall) -> TextReply:
        """Force a fresh discover_subagents call, synthesize tools via as_tool(), find the
        one named `tool_name`, and call it with `arguments` — simulating what a model-driven
        tool loop would do after seeing list_dynamic_tools' declarations."""
        tools = await discover_and_build_tools(
            self._runner, REGISTRY_ENDPOINT, self._dynamic_handle_cache
        )
        by_name = {fn.__name__: fn for fn in tools}
        fn = by_name.get(msg.tool_name)
        if fn is None:
            raise ApplicationError(
                f"Unknown dynamic tool {msg.tool_name!r}. Currently available: "
                f"{sorted(by_name)}.",
                {"tool_name": msg.tool_name, "known": sorted(by_name)},
                type="UnknownDynamicTool",
                non_retryable=True,
            )
        result = await self._runner.run_tool(f"dynamic-{msg.tool_name}", fn, **msg.arguments)
        return TextReply(text=json.dumps(result))

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the demo assistant. It has no fixed abilities of its own — on every
        turn it discovers the Nexus agent registry fresh, builds one real tool per
        discovered subagent handler (via as_tool(), from that handler's actual schema), and
        lets the model decide which to call. Replies once the model has no further tool
        calls to make."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: discover + synthesize this turn's tools ONCE (not
        re-discovered on every model round-trip within the turn — just once per user
        message), stream the model, dispatch any tool calls, feed results back, and loop
        until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
        tool_fns = await discover_and_build_tools(
            self._runner, REGISTRY_ENDPOINT, self._dynamic_handle_cache
        )
        callables_by_name = {fn.__name__: fn for fn in tool_fns}
        # tool_declaration(), NOT function_param(): these tools have no real Python type to
        # introspect (their schema was fetched live over Nexus, not derived from a pydantic
        # class) — see subagents.registry.toolset's module docstring.
        tools: list[ToolParam] = [tool_declaration(fn) for fn in tool_fns]

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
                *(self._run_one_tool(fc, callables_by_name) for fc in pending_calls)
            )

    async def _run_one_tool(
        self, call: FunctionCallStep, callables_by_name: dict[str, Callable[..., Any]]
    ) -> FunctionResultStepParam:
        """Execute one dynamically-synthesized tool call via ``run_tool`` and return its
        result. Any failure (unknown tool name, or the subagent turn itself erroring) is
        caught and returned as an ``is_error`` function result rather than raised — the
        model sees the error and can react, instead of the turn failing outright."""
        try:
            tool_callable = callables_by_name.get(call.name)
            if tool_callable is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            # call.arguments is statically typed `object`; it's a dict once the streamed
            # JSON fragments are parsed (see _execute_agent_interaction).
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
                # as_tool()-built tools return a raw dict (the subagent's reply JSON), not a
                # pydantic model — plain json.dumps, no model_dump_json() to reach for.
                "result": json.dumps(result),
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
        """Drive one Gemini Interactions API call to completion and reduce its SSE stream.

        Lifted verbatim from ``examples/monty/conversational_subagent_workflow.py`` (see that
        file for the full rationale) — the streaming-reduction logic itself has nothing to do
        with subagents or Nexus, so it's unchanged: text deltas accumulate into the reply,
        function-call steps + their streamed argument-JSON fragments reassemble into
        ``FunctionCallStep``s, and ``interaction.completed`` supplies the chaining id for the
        next call."""
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
