# ABOUTME: Runs a harness agent on the vendored Strands integration end to end (time-skipping
# server, a scripted Strands model on the worker) and checks what reaches the turn stream: each
# model call bracketed from the model activity with its usage and reasoning, one tool_id shared by
# tool_requested and tool_start/tool_end for inline, activity and TemporalSandbox tools, a callback
# tool resolved by a client, an MCP tool call gated on a person's approval, a Strands hook
# interrupt refused instead of pausing the run, and mid-turn steering reaching the model with the
# next tool result.

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterable, Callable
from datetime import timedelta
from typing import Any, cast

import pytest
from mcp import Client as MCPClient
from mcp.server.mcpserver import MCPServer
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import Model
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.types.streaming import StreamEvent
from temporalio import workflow
from temporalio.client import WorkflowHandle
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.strands_agents import (
    StrandsPlugin,
    TemporalMCPClient,
    TemporalSandbox,
)
from temporal_agent_harness.ai_sdks.strands_agents_harness import (
    STRANDS_INTERRUPT_NOT_SUPPORTED,
    as_strands_tool,
    harness_observer_factory,
    harness_strands_agent,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    PROVIDE_CALLBACK_RESULT_UPDATE,
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    CallbackResult,
    MessageContext,
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalDecision,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner, Injected
from temporal_agent_harness.plugin import AgentHarnessPlugin


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It's 72°F and sunny in {city}."


@agent.callback_tool_defn()
async def ask_user(question: str) -> str:
    """Ask the user a question and return their answer."""
    ...


@agent.activity_tool_defn(inherently_safe=True)
async def convert_to_celsius(fahrenheit: float) -> float:
    """Convert a temperature from Fahrenheit to Celsius."""
    return round((fahrenheit - 32) * 5 / 9, 1)


def _start(
    config: AgentConfig, stream: WorkflowStream, **agent_kwargs: Any
) -> tuple[AgentWorkflowRunner, Any]:
    runner = AgentWorkflowRunner(
        config,
        stream=stream,
        approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
    )
    strands_agent = harness_strands_agent(
        runner,
        model="scripted",
        start_to_close_timeout=timedelta(seconds=30),
        callback_handler=None,
        tools=[
            as_strands_tool(runner, get_weather),
            as_strands_tool(runner, convert_to_celsius),
            as_strands_tool(runner, ask_user),
        ],
        sandbox=TemporalSandbox("scratch", start_to_close_timeout=timedelta(seconds=30)),
        **agent_kwargs,
    )
    return runner, strands_agent


@agent.defn(name="StrandsHarnessTestAgent")
class StrandsHarnessTestAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        # WorkflowStream must be built in the @workflow.init method itself.
        self._runner, self._agent = _start(config, WorkflowStream())

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        return TextReply(text=str(await self._agent.invoke_async(message.text)))


class _ConfirmEveryToolCall(HookProvider):
    """A Strands approval hook: interrupts before each tool call to ask a person."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._confirm)

    def _confirm(self, event: BeforeToolCallEvent) -> None:
        event.interrupt("confirm-tool", reason="Run this tool?")


@agent.defn(name="StrandsInterruptingHookTestAgent")
class StrandsInterruptingHookTestAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner, self._agent = _start(
            config, WorkflowStream(), hooks=[_ConfirmEveryToolCall()]
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        return TextReply(text=str(await self._agent.invoke_async(message.text)))


@agent.tool_defn(inherently_safe=True)
async def pick_city() -> str:
    """Pick a city, once the agent has been steered."""
    probe = cast("StrandsSteeringTestAgent", workflow.instance())
    await workflow.wait_condition(lambda: probe.steered)
    return "Lisbon"


@agent.defn(name="StrandsSteeringTestAgent")
class StrandsSteeringTestAgent:
    """Steers the way strands_hello does: a message that joins the open turn is steering."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._agent = harness_strands_agent(
            self._runner,
            model="scripted",
            start_to_close_timeout=timedelta(seconds=30),
            callback_handler=None,
            tools=[as_strands_tool(self._runner, pick_city)],
        )
        self.steered = False

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def ask(self, message: TextMessage, ctx: Injected[MessageContext]) -> TextReply:
        """Ask the agent something; sent mid-turn, it steers the turn."""
        if ctx.joined_turn:
            self._runner.steer(message.text)
            self.steered = True
            return TextReply(text="")
        return TextReply(text=str(await self._agent.invoke_async(message.text)))


Script = list[str | dict[str, Any]]

# A script step that answers with exactly the text of the last tool result the model was sent.
ECHO_TOOL_RESULT = "<echo the last tool result>"


_echo_server = MCPServer("echo-server")


@_echo_server.tool()
def echo(message: str) -> str:
    """Return the input message unchanged."""
    return message


@agent.defn(name="StrandsMCPApprovalTestAgent")
class StrandsMCPApprovalTestAgent:
    """Calls an MCP server's tool under a policy that asks a person to approve every call."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )
        self._agent = harness_strands_agent(
            self._runner,
            model="scripted",
            start_to_close_timeout=timedelta(seconds=30),
            callback_handler=None,
            tools=[TemporalMCPClient("echo", start_to_close_timeout=timedelta(seconds=30))],
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        return TextReply(text=str(await self._agent.invoke_async(message.text)))


def _last_tool_result_text(messages: list[dict[str, Any]]) -> str:
    for block in messages[-1]["content"]:
        if "toolResult" in block:
            return "".join(c.get("text", "") for c in block["toolResult"]["content"])
    raise AssertionError("the last message carries no tool result")


class _ScriptedModel(Model):
    """Each ``stream()`` call plays the next entry: text replies, or a tool call by name.

    Every call streams a reasoning fragment first and ends with a usage ``metadata`` event,
    like a real provider.
    """

    def __init__(self, script: Script) -> None:
        self._script = list(script)
        self._calls = 0

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-model"}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, messages: Any, *_args: Any, **_kwargs: Any) -> AsyncIterable[StreamEvent]:
        step = self._script.pop(0)
        if step == ECHO_TOOL_RESULT:
            step = _last_tool_result_text(messages)
        self._calls += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "Thinking."}}}}
        yield {"contentBlockStop": {}}
        if isinstance(step, str):
            yield {"contentBlockDelta": {"delta": {"text": step}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": f"tu-{self._calls}", "name": step["name"]}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step["input"])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13},
                "metrics": {"latencyMs": 1},
            }
        }


OnEvent = Callable[[WorkflowHandle[Any, Any], AgentEvent], Any]


async def _one_turn(
    script: Script,
    text: str,
    on_event: OnEvent | None = None,
    workflow_class: type[Any] = StrandsHarnessTestAgent,
) -> tuple[list[AgentEvent], AgentMessageReply | BaseException]:
    """Run one turn against a fresh server; return its events and the reply (or its failure)."""
    plugin = StrandsPlugin(
        models={"scripted": lambda: _ScriptedModel(script)},
        sandboxes={"scratch": lambda _context: NotASandboxLocalEnvironment()},
        mcp_clients={"echo": lambda: MCPClient(_echo_server)},
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[plugin, AgentHarnessPlugin(tools=[convert_to_celsius])]
    )
    try:
        task_queue = f"strands-harness-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=task_queue, workflows=[workflow_class]):
            handle = await env.client.start_workflow(
                workflow_class.run,
                AgentConfig(),
                id=f"strands-harness-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            reply_task = asyncio.create_task(
                handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="ask", payload={"text": text}),
                    result_type=AgentMessageReply,
                )
            )
            events: list[AgentEvent] = []
            stream = WorkflowStreamClient.create(env.client, handle.id)
            async with asyncio.timeout(30):
                async for item in stream.subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    events.append(item.data)
                    if on_event is not None:
                        await on_event(handle, item.data)
                    if item.data.event.type == AgentEventType.TURN_END:
                        break
                reply: AgentMessageReply | BaseException
                try:
                    reply = await reply_task
                except BaseException as exc:  # noqa: BLE001 - the failure is what some tests check
                    reply = exc
            return events, reply
    finally:
        await env.shutdown()


def _of(events: list[AgentEvent], kind: AgentEventType) -> list[Any]:
    return [e.event for e in events if e.event.type == kind]


async def test_model_calls_are_bracketed_and_every_tool_kind_shares_its_tool_use_id():
    events, reply = await _one_turn(
        [
            {"name": "get_weather", "input": {"city": "Paris"}},
            {"name": "convert_to_celsius", "input": {"fahrenheit": 72}},
            {"name": "sandbox_shell", "input": {"command": "echo forecast-saved"}},
            "Sunny and 22.2°C in Paris; I saved the forecast.",
        ],
        "Weather in Paris in Celsius, then save it.",
    )

    assert isinstance(reply, AgentMessageReply)
    started = _of(events, AgentEventType.MODEL_INTERACTION_STARTED)
    ended = _of(events, AgentEventType.MODEL_INTERACTION_ENDED)
    assert len(started) == len(ended) == 4
    assert {s.model for s in started} == {"scripted-model"}
    assert all(e.usage is not None and e.usage.total_tokens == 13 for e in ended)
    assert len(_of(events, AgentEventType.THOUGHT_SUMMARY)) == 4
    assert "".join(d.text for d in _of(events, AgentEventType.REPLY_DELTA)) == (
        "Sunny and 22.2°C in Paris; I saved the forecast."
    )

    calls = [("tu-1", "get_weather"), ("tu-2", "convert_to_celsius"), ("tu-3", "sandbox_shell")]
    requested = _of(events, AgentEventType.TOOL_REQUESTED)
    assert [(r.tool_id, r.tool_name) for r in requested] == calls
    assert requested[0].tool_input == {"city": "Paris"}
    assert [(s.tool_id, s.tool_name) for s in _of(events, AgentEventType.TOOL_START)] == calls
    ends = _of(events, AgentEventType.TOOL_END)
    assert [e.tool_id for e in ends] == ["tu-1", "tu-2", "tu-3"]
    assert ends[1].tool_output == 22.2
    # The sandbox tool ran the command through the worker-side sandbox, as an activity.
    assert "forecast-saved" in str(ends[2].tool_output)


async def test_a_callback_tool_waits_in_the_workflow_until_a_client_answers_it():
    async def answer(handle: WorkflowHandle[Any, Any], event: AgentEvent) -> None:
        if event.event.type == AgentEventType.CALLBACK_REQUESTED:
            await handle.execute_update(
                PROVIDE_CALLBACK_RESULT_UPDATE,
                CallbackResult(tool_id=event.event.tool_id, result="Lisbon"),
            )

    events, reply = await _one_turn(
        [{"name": "ask_user", "input": {"question": "Which city?"}}, "Lisbon it is."],
        "Pick a city with me.",
        on_event=answer,
    )

    assert isinstance(reply, AgentMessageReply)
    callback = _of(events, AgentEventType.CALLBACK_REQUESTED)
    assert [(c.tool_id, c.tool_name, c.tool_input) for c in callback] == [
        ("tu-1", "ask_user", {"question": "Which city?"})
    ]
    # One bracket for the whole wait: started once, ended once with the client's answer.
    assert [s.tool_id for s in _of(events, AgentEventType.TOOL_START)] == ["tu-1"]
    assert [(e.tool_id, e.tool_output) for e in _of(events, AgentEventType.TOOL_END)] == [
        ("tu-1", "Lisbon")
    ]
    assert len(_of(events, AgentEventType.MODEL_INTERACTION_STARTED)) == 2


def _answer_approvals(approved: bool) -> OnEvent:
    async def answer(handle: WorkflowHandle[Any, Any], event: AgentEvent) -> None:
        if event.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
            await handle.execute_update(
                TOOL_APPROVAL_UPDATE,
                ToolApprovalDecision(
                    tool_id=event.event.tool_id,
                    approved=approved,
                    reason=None if approved else "not allowed",
                ),
            )

    return answer


async def test_an_mcp_tool_call_waits_for_approval_then_runs_under_its_tool_use_id():
    events, reply = await _one_turn(
        [{"name": "echo", "input": {"message": "hello"}}, ECHO_TOOL_RESULT],
        "Echo hello.",
        on_event=_answer_approvals(approved=True),
        workflow_class=StrandsMCPApprovalTestAgent,
    )

    assert isinstance(reply, AgentMessageReply)
    [handler_end] = _of(events, AgentEventType.MESSAGE_HANDLER_END)
    assert handler_end.output["text"].strip() == "hello"
    lifecycle = [
        (e.event.type, e.event.tool_id)
        for e in events
        if e.event.type
        in (
            AgentEventType.TOOL_REQUESTED,
            AgentEventType.TOOL_APPROVAL_REQUESTED,
            AgentEventType.TOOL_APPROVAL_RESOLVED,
            AgentEventType.TOOL_START,
            AgentEventType.TOOL_END,
        )
    ]
    assert lifecycle == [
        (AgentEventType.TOOL_REQUESTED, "tu-1"),
        (AgentEventType.TOOL_APPROVAL_REQUESTED, "tu-1"),
        (AgentEventType.TOOL_APPROVAL_RESOLVED, "tu-1"),
        (AgentEventType.TOOL_START, "tu-1"),
        (AgentEventType.TOOL_END, "tu-1"),
    ]
    [end] = _of(events, AgentEventType.TOOL_END)
    assert (end.tool_name, end.tool_output) == ("echo", "hello")


async def test_a_denied_mcp_tool_call_never_reaches_the_server_and_the_model_hears_why():
    events, reply = await _one_turn(
        [{"name": "echo", "input": {"message": "hello"}}, ECHO_TOOL_RESULT],
        "Echo hello.",
        on_event=_answer_approvals(approved=False),
        workflow_class=StrandsMCPApprovalTestAgent,
    )

    assert isinstance(reply, AgentMessageReply)
    [resolved] = _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert resolved.approved is False
    assert _of(events, AgentEventType.TOOL_START) == []
    [handler_end] = _of(events, AgentEventType.MESSAGE_HANDLER_END)
    assert "not allowed" in handler_end.output["text"]


async def test_a_strands_hook_interrupt_is_refused_and_points_at_callback_tools():
    events, reply = await _one_turn(
        [{"name": "get_weather", "input": {"city": "Paris"}}, "unreachable"],
        "Weather in Paris?",
        workflow_class=StrandsInterruptingHookTestAgent,
    )

    # The message is accepted, then its handler fails: the turn ends instead of waiting to
    # be resumed.
    assert isinstance(reply, AgentMessageReply)
    handler_errors = _of(events, AgentEventType.MESSAGE_HANDLER_ERROR)
    assert len(handler_errors) == 1
    assert handler_errors[0].message.startswith(f"{STRANDS_INTERRUPT_NOT_SUPPORTED}:")
    assert "callback_tool_defn" in handler_errors[0].message
    assert events[-1].event.type == AgentEventType.TURN_END
    # The hook interrupted before the tool ran, so it never started.
    assert [r.tool_name for r in _of(events, AgentEventType.TOOL_REQUESTED)] == ["get_weather"]
    assert not _of(events, AgentEventType.TOOL_START)


async def test_a_message_sent_mid_turn_steers_the_model_with_the_next_tool_result():
    script: Script = [{"name": "pick_city", "input": {}}, ECHO_TOOL_RESULT]
    plugin = StrandsPlugin(
        models={"scripted": lambda: _ScriptedModel(script)},
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(plugins=[plugin, AgentHarnessPlugin()])
    try:
        task_queue = f"strands-steering-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=task_queue, workflows=[StrandsSteeringTestAgent]):
            handle = await env.client.start_workflow(
                StrandsSteeringTestAgent.run,  # type: ignore[attr-defined]  # added by @agent.defn
                AgentConfig(),
                id=f"strands-steering-{uuid.uuid4()}",
                task_queue=task_queue,
            )

            async def send(text: str) -> str:
                reply = await handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="ask", payload={"text": text}),
                    result_type=AgentMessageReply,
                )
                return reply.message_id

            events: list[AgentEvent] = []
            async with asyncio.timeout(30):
                asker = await send("Pick a city for my trip.")
                steerer = await send("Only cities by the sea.")
                async for item in WorkflowStreamClient.create(env.client, handle.id).subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    events.append(item.data)
                    if item.data.event.type == AgentEventType.TURN_END:
                        break
    finally:
        await env.shutdown()

    # The model answered with the tool result it was sent: the tool's output, then the steering.
    reply = next(
        e.event.output["text"]
        for e in events
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END and e.message_id == asker
    )
    tool_result, block = reply.split("\n\n", 1)
    assert tool_result == "Lisbon"
    assert block.startswith("<steering_messages>")
    assert "<message>Only cities by the sea.</message>" in block
    # tool_end keeps the tool's own output; the steering went only to the model.
    tool_end = next(e.event for e in events if e.event.type == AgentEventType.TOOL_END)
    assert tool_end.tool_output == "Lisbon"
    enqueued = next(e for e in events if e.event.type == AgentEventType.STEERING_ENQUEUED)
    assert enqueued.message_id == steerer
    claimed = next(e for e in events if e.event.type == AgentEventType.STEERING_CLAIMED)
    assert claimed.event.steering_ids == [enqueued.event.steering_id]
    assert claimed.event.tool_id == "tu-1"
    assert claimed.message_id == asker
