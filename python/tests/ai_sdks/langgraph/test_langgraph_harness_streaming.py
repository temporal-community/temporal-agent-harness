# ABOUTME: Tests the LangGraph harness integration end to end on a local dev server with a fake
# ABOUTME: chat model (streaming, tool gating, callbacks, refusals, heartbeats, replay), plus
# ABOUTME: unit tests of harness_tool_call.

# pyright: reportMissingTypeStubs=false

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.func import entrypoint, task
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.pregel import Pregel
from langgraph.errors import GraphInterrupt
from langgraph.types import Send, interrupt
from pydantic import BaseModel

from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner

    from temporal_agent_harness.ai_sdks.openai_agents import OpenAIAgentsPlugin
    from temporal_agent_harness.ai_sdks.openai_agents.testing import (
        ResponseBuilders,
        TestModel,
        TestModelProvider,
    )
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import Injected
    from temporal_agent_harness.harness.agent_client import AgentClient, AgentTurnError
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        AgentEvent,
        AgentEventType,
        TextMessage,
        TextReply,
        TokenUsage,
        ToolApprovalPolicy,
    )

    from temporal_agent_harness.ai_sdks.langgraph import (
        LANGGRAPH_INTERRUPT_NOT_SUPPORTED,
        LangGraphPlugin,
        entrypoint as registered_entrypoint,
        graph,
        harness_observer_factory,
        harness_tool_call,
    )
    from temporal_agent_harness.ai_sdks.langgraph._harness import (
        _BINDING_KEY,
        _Binding,
        _HarnessGraph,
        _RefuseInterrupts,
    )
    from temporal_agent_harness.plugin import AgentHarnessPlugin

T = AgentEventType
pytestmark = pytest.mark.asyncio(loop_scope="module")


def harness_plugins(shape: str) -> list[Any]:
    """LangGraph first, harness last, so the harness data converter wins."""
    if shape == "functional":
        return [functional_plugin(), AgentHarnessPlugin()]
    return [graph_plugin(build_graphs(shape)), AgentHarnessPlugin()]


def graph_plugin(graphs: dict[str, Any]) -> LangGraphPlugin:
    return LangGraphPlugin(
        graphs=graphs, awrap_tool_call=harness_tool_call, observer_factory=harness_observer_factory
    )


def _reply(messages: Sequence[BaseMessage]) -> list[AIMessageChunk]:
    """Choose a reply from the input alone, so retries and replays agree.

    The last human message's words pick the shape:
      "whoami"  -> one whoami call that tries to set the Injected user
      "both"    -> get_weather and ask_user in one reply (ask_user is a callback)
      "weather" -> three get_weather calls, one without an id
      "ask"     -> one ask_user call
      "pause"   -> one pause call, a tool that calls LangGraph's interrupt()
      "explode" -> raise after the tool results, non-retryable
      "slowly"  -> stream after a delay longer than the heartbeat timeout
      anything else -> "You said: <text>"
    After tool results, the reply is "Answer: " plus the results joined by " | ".
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    calls = [c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    if any(c["id"] not in answered for c in calls):
        raise ValueError("unanswered tool call in history")
    human = next(m.text for m in reversed(messages) if isinstance(m, HumanMessage))
    usage = AIMessageChunk(
        content="",
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_token_details": {"cache_read": 1},
            "output_token_details": {"reasoning": 1},
        },
    )
    if isinstance(messages[-1], ToolMessage):
        if "explode" in human:
            raise ApplicationError("model unavailable", non_retryable=True)
        step = len(messages) - next(
            i for i, m in enumerate(reversed(messages)) if isinstance(m, AIMessage)
        )
        results = " | ".join(str(m.content) for m in messages[step:])
        return [
            AIMessageChunk(content=[{"type": "reasoning", "reasoning": "Reading it."}]),
            AIMessageChunk(content=[{"type": "text", "text": "Answer: "}]),
            AIMessageChunk(content=results),
            usage,
        ]

    def call(name: str, args: dict[str, str], index: int, id: str | None) -> Any:
        return {"name": name, "args": json.dumps(args), "id": id, "index": index}

    if "whoami" in human:
        # "user" is Injected; the model must not be able to set it.
        chunks = [call("whoami", {"greeting": "hi", "user": "EVIL"}, 0, "call-who")]
        return [AIMessageChunk(content="", tool_call_chunks=chunks), usage]
    if "both" in human:
        chunks = [
            call("get_weather", {"city": "Paris"}, 0, "call-weather"),
            call("ask_user", {"question": "Which city?"}, 1, "call-ask"),
        ]
        return [AIMessageChunk(content="", tool_call_chunks=chunks), usage]
    if "weather" in human:
        # Two calls share a provider id and one has none, as providers can emit.
        ids = ["call-weather", "call-weather", None]
        chunks = [
            call("get_weather", {"city": c}, i, ids[i])
            for i, c in enumerate(["Paris", "London", "Berlin"])
        ]
        return [AIMessageChunk(content="", tool_call_chunks=chunks), usage]
    if "pause" in human:
        chunks = [call("pause", {"question": "Which city?"}, 0, "call-pause")]
        return [AIMessageChunk(content="", tool_call_chunks=chunks), usage]
    if "ask" in human:
        chunks = [call("ask_user", {"question": "Which city?"}, 0, "call-ask")]
        return [AIMessageChunk(content="", tool_call_chunks=chunks), usage]
    return [AIMessageChunk(content=f"You said: {human}"), usage]


class FakeChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self  # the reply script already knows the tools

    def _generate(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> ChatResult:
        return generate_from_stream(
            ChatGenerationChunk(message=chunk) for chunk in _reply(messages)
        )

    async def _astream(  # type: ignore[override]
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        if "slowly" in messages[-1].text:
            await asyncio.sleep(5)
        for chunk in _reply(messages):
            yield ChatGenerationChunk(message=chunk)


@agent.tool_defn()
async def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"It's sunny in {city}."


@agent.tool_defn()
async def whoami(user: Injected[str], greeting: str) -> str:
    """Greet the current user."""
    return f"{greeting} {user}"


@agent.callback_tool_defn(inherently_safe=True)
async def ask_user(question: str) -> str:
    """Ask the user a question; the client answers it within the turn."""
    ...


@agent.tool_defn(inherently_safe=True)
async def pause(question: str) -> str:
    """Ask through LangGraph's interrupt(), which a harness agent refuses."""
    return interrupt(question)


async def call_model(state: MessagesState) -> dict[str, Any]:
    return {"messages": [await FakeChatModel().ainvoke(state["messages"])]}


async def send_all(state: MessagesState) -> Any:
    calls = getattr(state["messages"][-1], "tool_calls", [])
    return [Send("tools", calls)] if calls else END


async def send_each(state: MessagesState) -> Any:
    calls = getattr(state["messages"][-1], "tool_calls", [])
    return [Send("tools", [c]) for c in calls] or END


async def call_subgraph(state: MessagesState) -> dict[str, Any]:
    result = await graph("model").compile().ainvoke(state)
    return {"messages": result["messages"][len(state["messages"]) :]}


# How the tools node receives calls: the whole state, one Send with every call
# (no messages in its input), or one Send per call (one invocation per call).
# "subgraph" calls the model from a child graph.
ROUTES: dict[str, Any] = {
    # tools_condition is sync: the plugin runs sync routers in the workflow.
    "state": tools_condition,
    "batch": send_all,
    "send": send_each,
    "subgraph": tools_condition,
}
MODEL_NODE: dict[str, Any] = {
    "execute_in": "activity",
    "start_to_close_timeout": timedelta(seconds=30),
    "heartbeat_timeout": timedelta(seconds=2),
    "retry_policy": RetryPolicy(maximum_attempts=1),
}


def build_graphs(shape: str = "state") -> dict[str, StateGraph[Any, Any, Any, Any]]:
    model = StateGraph(MessagesState)
    model.add_node("agent", call_model, metadata=MODEL_NODE)
    model.add_edge(START, "agent")
    return {"agent": build_graph(shape), "model": model}


def build_graph(shape: str = "state") -> StateGraph[Any, Any, Any, Any]:
    g = StateGraph(MessagesState)
    if shape == "subgraph":
        g.add_node("agent", call_subgraph, metadata={"execute_in": "workflow"})
    else:
        g.add_node("agent", call_model, metadata=MODEL_NODE)
    g.add_node(
        "tools",
        ToolNode([get_weather, ask_user, whoami, pause]),  # the plugin sets awrap_tool_call
        metadata={"execute_in": "workflow"},
    )
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", ROUTES[shape], ["tools", END])
    g.add_edge("tools", "agent")
    return g


async def model_call(messages: list[BaseMessage]) -> AIMessage:
    return await FakeChatModel().ainvoke(messages)


async def tool_calls(message: AIMessage) -> list[ToolMessage]:
    node = ToolNode([get_weather, ask_user, whoami], awrap_tool_call=harness_tool_call)
    return (await node.ainvoke({"messages": [message]}))["messages"]


model_task = task(model_call)
tools_task = task(tool_calls)


@entrypoint()
async def functional_agent(
    inp: dict[str, Any], *, previous: list[BaseMessage] | None
) -> entrypoint.final[dict[str, Any], list[BaseMessage]]:
    """The graph agent's loop as an entrypoint: tasks stand in for its nodes."""
    messages = [*(previous or []), *inp["messages"]]
    while True:
        reply = await model_task(messages)
        messages.append(reply)
        if not isinstance(reply, AIMessage) or not reply.tool_calls:
            return entrypoint.final(value={"messages": messages}, save=messages)
        messages.extend(await tools_task(reply))


_functional_plugin: Any = None


def functional_plugin() -> Any:
    """The plugin wraps a task in place, so the functional agent is registered once."""
    global _functional_plugin
    if _functional_plugin is None:
        _functional_plugin = LangGraphPlugin(
            entrypoints={"functional": functional_agent},
            tasks=[model_task, tools_task],
            activity_options={"model_call": MODEL_NODE, "tool_calls": {"execute_in": "workflow"}},
            observer_factory=harness_observer_factory,
        )
    return _functional_plugin


async def turn(g: Pregel[Any, Any, Any, Any], text: str) -> str:
    """One harness turn, as the hello example's handler: one message is one graph run."""
    config = {"configurable": {"thread_id": workflow.info().workflow_id}}
    result = await g.ainvoke({"messages": [HumanMessage(text)]}, config)
    return result["messages"][-1].text


@agent.defn(name="LangGraphHarnessTestAgent")
class GraphAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._graph = graph("agent", self._runner, injections={"user": "alice"}).compile(
            checkpointer=InMemorySaver()
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        return TextReply(text=await turn(self._graph, message.text))


@agent.defn(name="LangGraphFunctionalTestAgent")
class FunctionalAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        # An entrypoint is built once at import, so give this workflow its own checkpointer.
        self._graph = registered_entrypoint(
            "functional", self._runner, injections={"user": "alice"}
        ).copy({"checkpointer": InMemorySaver()})

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        return TextReply(text=await turn(self._graph, message.text))


@agent.defn(name="LangGraphNeighborOpenAIAgent")
class OpenAINeighbor:
    """An OpenAI Agents agent, to share a worker with the LangGraph ones."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        sdk_agent = OpenAIAgent(name="neighbor", model="scripted")
        result = await Runner.run(sdk_agent, input=message.text, context=self._runner)
        return TextReply(text=str(result.final_output))


AGENTS = {"functional": "LangGraphFunctionalTestAgent"}


class Session:
    def __init__(
        self, client: Client, handle: WorkflowHandle[Any, Any], shape: str, approve: bool
    ) -> None:
        self.handle = handle
        self.shape = shape
        self.approve = approve
        self.agent = AgentClient(client, handle.id)
        self.pending: list[Any] = []

    async def turn(self, text: str) -> list[Any]:
        """Send one message, approving gated calls and answering callbacks, and return its events."""
        events: list[Any] = []
        stream = await self.agent.send_message(
            "ask", {"text": text}, on_item=lambda item, _: item, timeout=30
        )
        async for item in stream:
            events.append(item)
            if not isinstance(item, AgentEvent):
                continue
            if item.event.type == T.TOOL_APPROVAL_REQUESTED:
                await self.agent.approve_tool(item.event.tool_id, approved=self.approve)
            elif item.event.type == T.CALLBACK_REQUESTED:
                await self.agent.provide_callback_result(item.event.tool_id, result="Paris")
        return events

    async def replay(self) -> None:
        await Replayer(
            workflows=[GraphAgent, FunctionalAgent],
            plugins=harness_plugins(self.shape),
        ).replay_workflow(await self.handle.fetch_history())


async def run_session(
    client: Client,
    config: AgentConfig,
    *texts: str,
    shape: str = "state",
    approve: bool = True,
) -> tuple[Session, list[list[Any]]]:
    client_config = client.config()
    client_config["plugins"] = harness_plugins(shape)
    harness_client = Client(**client_config)
    task_queue = f"langgraph-harness-{uuid4()}"
    async with Worker(
        harness_client,
        task_queue=task_queue,
        workflows=[GraphAgent, FunctionalAgent],
    ):
        handle = await harness_client.start_workflow(
            AGENTS.get(shape, "LangGraphHarnessTestAgent"),
            config,
            id=f"agent-{uuid4()}",
            task_queue=task_queue,
        )
        session = Session(harness_client, handle, shape, approve)
        turns = [await session.turn(text) for text in texts]
        session.pending = await session.agent.get_pending_approvals()
    return session, turns


def payloads(events: list[Any], *types: AgentEventType) -> list[Any]:
    return [
        e.event for e in events if isinstance(e, AgentEvent) and e.event.type in types
    ]


def reply(events: list[Any]) -> str:
    return payloads(events, T.MESSAGE_HANDLER_END)[-1].output["text"]


def in_order(seq: list[Any], expected: list[Any]) -> bool:
    """Whether ``expected`` appears in ``seq`` in that order, other items in between allowed."""
    rest = iter(seq)
    return all(item in rest for item in expected)


async def test_turn_streams_model_output_and_gates_tools(client: Client):
    session, [events] = await run_session(client, AgentConfig(), "What's the weather?")

    order = [
        T.MODEL_INTERACTION_STARTED,
        T.TOOL_REQUESTED,
        T.MODEL_INTERACTION_ENDED,
        T.TOOL_START,
        T.TOOL_END,
        T.MODEL_INTERACTION_STARTED,
        T.THOUGHT_SUMMARY,
        T.REPLY_DELTA,
        T.REPLY_DELTA,
        T.MODEL_INTERACTION_ENDED,
        T.MESSAGE_HANDLER_END,
    ]
    assert in_order([e.type for e in payloads(events, *order)], order)

    approvals = {e.tool_id for e in payloads(events, T.TOOL_APPROVAL_REQUESTED)}
    assert len(approvals) == 3 and "call-weather" in approvals
    # The stream requested the calls as the model gave them; the ToolNode re-requested the
    # repeated id and the missing one under fresh ids, which are the ones that ran.
    requested = [e.tool_id for e in payloads(events, T.TOOL_REQUESTED)]
    assert requested[:2] == ["call-weather", "call-weather"] and len(requested) == 4
    assert set(requested[2:]) < approvals
    assert {e.tool_id for e in payloads(events, T.TOOL_END)} == approvals
    assert [e.delta for e in payloads(events, T.THOUGHT_SUMMARY)] == [
        {"text": "Reading it.", "content": "Reading it."}
    ]
    assert [e.text for e in payloads(events, T.REPLY_DELTA)] == [
        "Answer: ",
        "It's sunny in Paris. | It's sunny in London. | It's sunny in Berlin.",
    ]
    assert payloads(events, T.MODEL_INTERACTION_ENDED)[-1].usage == TokenUsage(
        input_tokens=3,
        output_tokens=2,
        total_tokens=5,
        cached_tokens=1,
        thought_tokens=1,
    )
    # The fake model rejects any AIMessage tool call without a matching
    # ToolMessage, so this reply also shows the filled-in id agrees.
    assert reply(events) == (
        "Answer: It's sunny in Paris. | It's sunny in London. | It's sunny in Berlin."
    )
    await session.replay()


async def test_a_denied_tool_call_never_runs_and_the_model_hears_why(client: Client):
    session, [events] = await run_session(
        client, AgentConfig(), "What's the weather?", approve=False
    )

    resolved = payloads(events, T.TOOL_APPROVAL_RESOLVED)
    assert len(resolved) == 3 and not any(e.approved for e in resolved)
    assert payloads(events, T.TOOL_START) == []
    assert reply(events).count("was not approved") == 3
    assert "sunny" not in reply(events)
    await session.replay()


async def test_an_interrupt_is_refused_and_points_at_callback_tools(client: Client):
    session, [events] = await run_session(client, AgentConfig(), "pause")

    [error] = [item for item in events if isinstance(item, AgentTurnError)]
    assert "callback_tool_defn" in str(error), events
    await session.replay()


async def test_only_an_interrupt_is_refused():
    refuse = _RefuseInterrupts()
    with pytest.raises(ApplicationError) as refused:
        await refuse.on_chain_error(GraphInterrupt(()))
    assert refused.value.type == LANGGRAPH_INTERRUPT_NOT_SUPPORTED
    assert refused.value.non_retryable
    await refuse.on_chain_error(ValueError("not an interrupt"))


def test_compile_refuses_static_interrupts_and_ungated_tool_nodes():
    unregistered = _HarnessGraph(SimpleNamespace(builder=build_graph()), {})
    for key in ("interrupt_before", "interrupt_after"):
        with pytest.raises(ValueError, match=key):
            unregistered.compile(**{key: ["tools"]})
    # A graph the plugin didn't register with awrap_tool_call=harness_tool_call.
    with pytest.raises(ValueError, match="awrap_tool_call=harness_tool_call"):
        unregistered.compile()


def test_a_positional_cache_is_not_taken_for_a_runner():
    for lookup in (graph, registered_entrypoint):
        with pytest.raises(TypeError, match="cache="):
            lookup("agent", cast(Any, {}))


def test_compile_refuses_a_tool_that_is_not_a_harness_tool():
    async def lookup(city: str) -> str:
        """A plain LangChain tool, which the approval policy would never see."""
        return city

    g = StateGraph(MessagesState)
    g.add_node(
        "tools",
        ToolNode([get_weather, StructuredTool.from_function(coroutine=lookup)], awrap_tool_call=harness_tool_call),
        metadata={"execute_in": "workflow"},
    )
    g.add_edge(START, "tools")
    with pytest.raises(TypeError, match=r"\['lookup'\] are not harness tools"):
        _HarnessGraph(SimpleNamespace(builder=g), {}).compile()


async def test_a_langgraph_and_an_openai_agent_share_one_worker(client: Client):
    model = TestModel.returning_responses([ResponseBuilders.output_message("Hi from OpenAI")])
    client_config = client.config()
    client_config["plugins"] = [
        graph_plugin(build_graphs()),
        OpenAIAgentsPlugin(model_provider=TestModelProvider(model)),
        AgentHarnessPlugin(),
    ]
    harness_client = Client(**client_config)
    task_queue = f"langgraph-neighbors-{uuid4()}"
    replies = []
    async with Worker(harness_client, task_queue=task_queue, workflows=[GraphAgent, OpenAINeighbor]):
        for name in ("LangGraphHarnessTestAgent", "LangGraphNeighborOpenAIAgent"):
            handle = await harness_client.start_workflow(
                name, AgentConfig(), id=f"agent-{uuid4()}", task_queue=task_queue
            )
            replies.append(reply(await Session(harness_client, handle, "state", True).turn("hello")))

    assert replies == ["You said: hello", "Hi from OpenAI"]


async def test_model_node_in_a_subgraph_streams(client: Client):
    session, [events] = await run_session(
        client, AgentConfig(), "What's the weather?", shape="subgraph"
    )

    assert len(payloads(events, T.TOOL_END)) == 3
    assert [e.text for e in payloads(events, T.REPLY_DELTA)][0] == "Answer: "
    assert reply(events).startswith("Answer: It's sunny in Paris.")
    await session.replay()


async def test_model_cannot_override_injected_arguments(client: Client):
    session, [events] = await run_session(client, AgentConfig(), "whoami")

    [approval] = payloads(events, T.TOOL_APPROVAL_REQUESTED)
    assert approval.tool_input == {"greeting": "hi"}
    assert reply(events) == "Answer: hi alice"
    await session.replay()


async def test_slow_model_call_outlives_heartbeat_timeout(client: Client):
    # The model node has a 2s heartbeat timeout and one attempt; the call takes 5s.
    session, [events] = await run_session(client, AgentConfig(), "slowly, hello")

    assert reply(events) == "You said: slowly, hello"
    await session.replay()


async def test_functional_api_entrypoint_streams_and_gates_tools(client: Client):
    session, [weather, asked] = await run_session(
        client, AgentConfig(), "What's the weather?", "ask me", shape="functional"
    )

    assert len(payloads(weather, T.TOOL_END)) == 3
    assert [e.text for e in payloads(weather, T.REPLY_DELTA)][0] == "Answer: "
    assert payloads(weather, T.MODEL_INTERACTION_ENDED)[-1].usage.total_tokens == 5
    assert reply(weather).startswith("Answer: It's sunny in Paris.")
    assert reply(asked) == "Answer: Paris"
    await session.replay()


async def test_callback_tool_is_answered_within_the_turn(client: Client):
    session, [events] = await run_session(client, AgentConfig(), "ask me")

    # The harness holds the turn open on callback_requested until the client answers.
    assert [e.tool_id for e in payloads(events, T.CALLBACK_REQUESTED)] == ["call-ask"]
    assert [e.tool_id for e in payloads(events, T.TOOL_START, T.TOOL_END)] == ["call-ask"] * 2
    assert reply(events) == "Answer: Paris"
    await session.replay()


@pytest.mark.parametrize("shape", ["state", "batch", "send"])
async def test_callback_tool_runs_beside_other_calls(client: Client, shape: str):
    session, [events] = await run_session(client, AgentConfig(), "both", shape=shape)

    assert [e.tool_name for e in payloads(events, T.TOOL_START)].count("get_weather") == 1
    requested = {e.tool_id for e in payloads(events, T.TOOL_APPROVAL_REQUESTED)}
    resolved = {e.tool_id for e in payloads(events, T.TOOL_APPROVAL_RESOLVED)}
    assert requested == resolved
    assert session.pending == []
    assert reply(events) == "Answer: It's sunny in Paris. | Paris"
    await session.replay()


async def test_turn_after_failed_model_call(client: Client):
    config = AgentConfig(approval_policy=ToolApprovalPolicy.dangerously_skip_all())
    session, [failed, after] = await run_session(
        client, config, "weather, then explode", "hello"
    )

    assert any(isinstance(item, AgentTurnError) for item in failed)
    assert reply(after) == "You said: hello"
    await session.replay()


@agent.tool_defn()
async def double(n: int) -> str:
    """Double a number."""
    return str(n * 2)


class FakeRunner:
    def __init__(
        self, error: Exception | None = None, result: Any = None, steering: Any = None
    ) -> None:
        self.events: list[Any] = []
        self.error = error
        self.result = result
        self.steering = steering
        self.calls: list[dict[str, Any]] = []
        self.claimed: list[str | None] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)

    def claim_steering(self, *, tool_id: str | None = None) -> Any:
        self.claimed.append(tool_id)
        return self.steering

    async def run_tool(self, *_: Any, **kwargs: Any) -> Any:
        if self.error:
            raise self.error
        kwargs.pop("injections")
        self.calls.append(kwargs)
        return self.result or str(kwargs["n"] * 2)


async def call_tool(
    runner: FakeRunner,
    args: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tool: Any = double,
) -> Any:
    monkeypatch.setattr(workflow, "uuid4", uuid4)
    token = var_child_runnable_config.set(
        {"configurable": {_BINDING_KEY: _Binding(cast(Any, runner), {})}}
    )
    try:
        return await harness_tool_call(tool_request(tool, args), cast(Any, None))
    finally:
        var_child_runnable_config.reset(token)


def tool_request(tool: Any, args: dict[str, Any]) -> ToolCallRequest:
    [base_tool] = ToolNode([tool]).tools_by_name.values()
    return ToolCallRequest(
        tool_call={"name": "double", "args": args, "id": "call-1", "type": "tool_call"},
        tool=base_tool,
        state={},
        runtime=cast(Any, SimpleNamespace(context=None, config={})),
    )


@agent.tool_defn()
async def scaled(n: int, factor: int) -> str:
    """Scale a number."""
    return str(n * factor)


class ScaledArgs(BaseModel):
    n: int
    factor: int = 3


async def test_tool_result_carries_queued_steering(monkeypatch: pytest.MonkeyPatch):
    steering = SimpleNamespace(append_to=lambda text: f"{text}\n\n<steer>Go north.</steer>")
    runner = FakeRunner(steering=steering)
    message = await call_tool(runner, {"n": 2}, monkeypatch)

    assert runner.claimed == ["call-1"]
    assert message.content == "4\n\n<steer>Go north.</steer>"

    down = FakeRunner(error=RuntimeError("down"), steering=steering)
    failed = await call_tool(down, {"n": 2}, monkeypatch)
    assert failed.status == "error" and failed.content.endswith("</steer>")


async def test_schema_defaults_are_passed(monkeypatch: pytest.MonkeyPatch):
    runner = FakeRunner()
    tool = StructuredTool(
        name="scaled",
        description="Scale a number.",
        args_schema=ScaledArgs,
        coroutine=scaled,
    )
    await call_tool(runner, {"n": 5}, monkeypatch, tool)

    assert runner.calls == [{"n": 5, "factor": 3}]


async def test_dict_schema_tool_cannot_override_injected_arguments(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = FakeRunner(result="ok")
    tool = StructuredTool(
        name="whoami",
        description="Greet the current user.",
        args_schema={"type": "object", "properties": {"greeting": {"type": "string"}}},
        coroutine=whoami,
    )
    await call_tool(runner, {"greeting": "hi", "user": "EVIL"}, monkeypatch, tool)

    assert runner.calls == [{"greeting": "hi"}]


async def test_tool_arguments_are_validated(monkeypatch: pytest.MonkeyPatch):
    runner = FakeRunner()
    ok = await call_tool(runner, {"n": "5"}, monkeypatch)
    bad = await call_tool(runner, {"n": "five"}, monkeypatch)

    assert runner.calls == [{"n": 5}] and ok.content == "10"
    assert bad.status == "error" and "n" in bad.content


async def test_cancelled_tool_is_not_an_error_message(monkeypatch: pytest.MonkeyPatch):
    cancelled = ActivityError(
        "Activity task cancelled",
        scheduled_event_id=1,
        started_event_id=2,
        identity="",
        activity_type="double",
        activity_id="1",
        retry_state=None,
    )
    cancelled.__cause__ = CancelledError("cancelled")
    failed = await call_tool(
        FakeRunner(ApplicationError("boom")), {"n": 1}, monkeypatch
    )

    assert failed.status == "error"
    with pytest.raises(ActivityError):
        await call_tool(FakeRunner(cancelled), {"n": 1}, monkeypatch)


class ApiClient:
    def __init__(self) -> None:
        self._client = {"api_key": "sk-secret"}

    def __str__(self) -> str:
        return f"ApiClient(key={self._client['api_key']})"


class Opaque:
    def __init__(self) -> None:
        self._token = "sk-secret"


async def test_unserializable_result_hides_private_attributes(
    monkeypatch: pytest.MonkeyPatch,
):
    result = await call_tool(
        FakeRunner(result={"client": ApiClient(), "kind": object, "opaque": Opaque()}),
        {"n": 1},
        monkeypatch,
    )

    assert result.status == "success"
    assert json.loads(result.content) == {
        "client": "<ApiClient>",
        "kind": "<type>",
        "opaque": "<Opaque>",
    }


async def test_circular_result_hides_addresses_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    circular: dict[str, object] = {"opaque": Opaque()}
    circular["self"] = circular

    result = await call_tool(FakeRunner(result=circular), {"n": 1}, monkeypatch)

    assert result.status == "success"
    assert result.content == "<dict>"


async def test_harness_tool_call_guards(monkeypatch: pytest.MonkeyPatch):
    async def plain(n: int) -> str:
        """Not a harness tool."""
        return str(n)

    with pytest.raises(TypeError, match="not a harness tool"):
        await call_tool(FakeRunner(), {"n": 1}, monkeypatch, plain)

    # Outside a harness agent, with no binding, the call runs as LangGraph runs it.
    async def execute(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage("ran", tool_call_id="call-1")

    token = var_child_runnable_config.set({"configurable": {}})
    try:
        ran = await harness_tool_call(tool_request(double, {"n": 1}), execute)
    finally:
        var_child_runnable_config.reset(token)
    assert ran.content == "ran"
