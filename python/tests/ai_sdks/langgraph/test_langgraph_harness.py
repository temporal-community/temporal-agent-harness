# ABOUTME: Tests the LangGraph glue end to end on a local dev server with a fake chat model
# ABOUTME: (streaming, tool gating, interrupts, heartbeats, replay), plus handler unit tests.

# pyright: reportMissingTypeStubs=false

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.callbacks import AsyncCallbackManager
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.func import entrypoint, task
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, create_react_agent, tools_condition
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Send, interrupt
from pydantic import BaseModel

from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import Injected
    from temporal_agent_harness.harness.agent_client import AgentClient, AgentTurnError
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        AgentEvent,
        AgentEventType,
        ReplyDelta,
        TextMessage,
        TextReply,
        TokenUsage,
        ToolApprovalPolicy,
        ToolRequested,
    )

    from temporal_agent_harness.ai_sdks.langgraph import LangGraphPlugin, graph
    from temporal_agent_harness.ai_sdks.langgraph import entrypoint as registered_entrypoint
    from temporal_agent_harness.ai_sdks.langgraph_harness import (
        HarnessContext,
        LangGraphStreamHandler,
        _non_retryable,
        adopt_prebuilt,
        _token_usage,
        _with_handler,
        harness_tool_call,
        run_turn,
        stream_to_harness,
    )
    from temporal_agent_harness.plugin import AgentHarnessPlugin

T = AgentEventType
pytestmark = pytest.mark.asyncio(loop_scope="module")


def harness_plugins(shape: str) -> list[Any]:
    """LangGraph first, harness last, so the harness data converter wins."""
    if shape == "functional":
        return [functional_plugin(), AgentHarnessPlugin()]
    return [LangGraphPlugin(graphs=build_graphs(shape)), AgentHarnessPlugin()]


def _reply(messages: Sequence[BaseMessage]) -> list[AIMessageChunk]:
    """Choose a reply from the input alone, so retries and replays agree.

    The last human message's words pick the shape:
      "whoami"  -> one whoami call that tries to set the Injected user
      "both"    -> get_weather and ask_user in one reply (ask_user interrupts)
      "weather" -> three get_weather calls, one without an id
      "ask"     -> one ask_user call
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


@agent.tool_defn()
async def ask_user(question: str) -> str:
    """Ask the user a question."""
    return str(interrupt(question))


async def call_model(state: MessagesState) -> dict[str, Any]:
    return {"messages": [await FakeChatModel().ainvoke(state["messages"])]}


async def route(state: dict[str, Any]) -> str:
    return tools_condition(state)


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
    "state": route,
    "batch": send_all,
    "send": send_each,
    "subgraph": route,
}
MODEL_NODE: dict[str, Any] = {
    "execute_in": "activity",
    "stream_to_harness": True,
    "start_to_close_timeout": timedelta(seconds=30),
    "heartbeat_timeout": timedelta(seconds=2),
    "retry_policy": RetryPolicy(maximum_attempts=1),
}


def build_graphs(shape: str = "state") -> dict[str, StateGraph[Any, Any, Any, Any]]:
    model = StateGraph(MessagesState, context_schema=HarnessContext)
    model.add_node("agent", call_model, metadata=MODEL_NODE)
    model.add_edge(START, "agent")
    return {"agent": build_graph(shape), "model": model}


def build_graph(shape: str = "state") -> StateGraph[Any, Any, Any, Any]:
    if shape == "prebuilt":
        compiled = create_react_agent(
            FakeChatModel(),
            tools=[get_weather, ask_user, whoami],
            context_schema=HarnessContext,
        )
        return adopt_prebuilt(compiled, activity_options={"agent": MODEL_NODE})
    g = StateGraph(MessagesState, context_schema=HarnessContext)
    if shape == "subgraph":
        g.add_node("agent", call_subgraph, metadata={"execute_in": "workflow"})
    else:
        g.add_node("agent", call_model, metadata=MODEL_NODE)
    g.add_node(
        "tools",
        ToolNode([get_weather, ask_user, whoami], awrap_tool_call=harness_tool_call),
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


@entrypoint(context_schema=HarnessContext)
async def functional_agent(
    inp: dict[str, Any], *, previous: list[BaseMessage] | None
) -> entrypoint.final[dict[str, Any], list[BaseMessage]]:
    """The graph agent's loop as an entrypoint: tasks stand in for its nodes."""
    messages = [*(previous or []), *inp["messages"]]
    while True:
        # An activity task's value comes back untyped: coerce it as MessagesState would.
        [reply] = convert_to_messages([await model_task(messages)])
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
        )
    return _functional_plugin


@agent.defn(name="LangGraphHarnessTestAgent")
class GraphAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._graph = graph("agent").compile(checkpointer=InMemorySaver())

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        context = HarnessContext(runner=self._runner, injections={"user": "alice"})
        return TextReply(text=await run_turn(self._graph, message.text, context))


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
        self._graph = registered_entrypoint("functional").copy(
            {"checkpointer": InMemorySaver()}
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        context = HarnessContext(runner=self._runner, injections={"user": "alice"})
        return TextReply(text=await run_turn(self._graph, message.text, context))


AGENTS = {"functional": "LangGraphFunctionalTestAgent"}


class Session:
    def __init__(
        self, client: Client, handle: WorkflowHandle[Any, Any], shape: str
    ) -> None:
        self.handle = handle
        self.shape = shape
        self.agent = AgentClient(client, handle.id)
        self.pending: list[Any] = []

    async def turn(self, text: str) -> list[Any]:
        """Send one message, approving every gated tool call, and return its events."""
        events: list[Any] = []
        stream = await self.agent.send_message(
            "ask", {"text": text}, on_item=lambda item, _: item, timeout=30
        )
        async for item in stream:
            events.append(item)
            if (
                isinstance(item, AgentEvent)
                and item.event.type == T.TOOL_APPROVAL_REQUESTED
            ):
                await self.agent.approve_tool(item.event.tool_id, approved=True)
        return events

    async def replay(self) -> None:
        await Replayer(
            workflows=[GraphAgent, FunctionalAgent],
            plugins=harness_plugins(self.shape),
        ).replay_workflow(await self.handle.fetch_history())


async def run_session(
    client: Client, config: AgentConfig, *texts: str, shape: str = "state"
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
        session = Session(harness_client, handle, shape)
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


async def test_prebuilt_react_agent_streams_gates_and_resumes(client: Client):
    # create_react_agent sends each call to its ToolNode on its own, so the interrupt resumes.
    session, [asked, resumed] = await run_session(
        client, AgentConfig(), "both", "Paris", shape="prebuilt"
    )

    assert [e.tool_name for e in payloads(asked, T.TOOL_START)].count("get_weather") == 1
    assert [e.text for e in payloads(resumed, T.REPLY_DELTA)][0] == "Answer: "
    assert payloads(resumed, T.MODEL_INTERACTION_ENDED)[-1].usage.total_tokens == 5
    assert [reply(asked), reply(resumed)] == ["Which city?", "Answer: It's sunny in Paris. | Paris"]
    await session.replay()


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
    session, [weather, asked, resumed] = await run_session(
        client, AgentConfig(), "What's the weather?", "ask me", "Paris", shape="functional"
    )

    assert len(payloads(weather, T.TOOL_END)) == 3
    assert [e.text for e in payloads(weather, T.REPLY_DELTA)][0] == "Answer: "
    assert payloads(weather, T.MODEL_INTERACTION_ENDED)[-1].usage.total_tokens == 5
    assert reply(weather).startswith("Answer: It's sunny in Paris.")
    assert [reply(asked), reply(resumed)] == ["Which city?", "Answer: Paris"]
    await session.replay()


async def test_next_message_resumes_pending_interrupt(client: Client):
    session, turns = await run_session(client, AgentConfig(), "ask me", "Paris")

    assert [reply(events) for events in turns] == ["Which city?", "Answer: Paris"]
    # The interrupt ended "call-ask" with a tool_error; the resumed call is a new one.
    asked, resumed = turns
    assert [e.tool_id for e in payloads(asked, T.TOOL_ERROR)] == ["call-ask"]
    [fresh] = [e.tool_id for e in payloads(resumed, T.TOOL_REQUESTED)]
    assert fresh != "call-ask"
    assert [e.tool_id for e in payloads(resumed, T.TOOL_START, T.TOOL_END)] == [fresh, fresh]
    await session.replay()


REFUSED = (
    "Answer: It's sunny in Paris. | Tool 'ask_user' asked the user for input, "
    "which only works when it is the only tool call in a step."
)


@pytest.mark.parametrize(
    ("shape", "texts", "replies"),
    [
        ("state", ["both"], [REFUSED]),
        ("batch", ["both"], [REFUSED]),
        (
            "send",
            ["both", "Paris"],
            ["Which city?", "Answer: It's sunny in Paris. | Paris"],
        ),
    ],
)
async def test_interrupt_never_reruns_a_sibling_call(
    client: Client, shape: str, texts: list[str], replies: list[str]
):
    session, turns = await run_session(client, AgentConfig(), *texts, shape=shape)

    events = [e for turn in turns for e in turn]
    started = payloads(events, T.TOOL_START)
    assert [e.tool_name for e in started].count("get_weather") == 1
    requested = {e.tool_id for e in payloads(events, T.TOOL_APPROVAL_REQUESTED)}
    resolved = {e.tool_id for e in payloads(events, T.TOOL_APPROVAL_RESOLVED)}
    assert requested == resolved
    assert session.pending == []
    assert [reply(turn) for turn in turns] == replies
    await session.replay()


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


async def test_streams_model_calls_in_chains_but_not_under_tools():
    traced = Recorder()
    # A parent the handler never saw, as under a LangSmith trace.
    callbacks = AsyncCallbackManager([LangGraphStreamHandler(traced)], parent_run_id=uuid4())
    await FakeChatModel().ainvoke("hello", config={"callbacks": callbacks})
    chained = Recorder()
    chain = RunnableLambda(lambda x: x) | FakeChatModel()
    await chain.ainvoke("hello", config={"callbacks": [LangGraphStreamHandler(chained)]})
    quiet = Recorder()
    await chain.with_config(tags=["nostream"]).ainvoke(
        "hello", config={"callbacks": [LangGraphStreamHandler(quiet)]}
    )
    in_tool = Recorder()

    @tool
    async def consult(question: str) -> str:
        """A tool that asks a model, whose text is not the reply."""
        return (await chain.ainvoke(question)).text

    await consult.ainvoke({"question": "hello"}, config={"callbacks": [LangGraphStreamHandler(in_tool)]})

    for recorder in (traced, chained):
        assert [e.text for e in recorder.events if isinstance(e, ReplyDelta)] == [
            "You said: hello"
        ]
    assert quiet.events == [] and in_tool.events == []


def test_usage_without_details_maps_to_empty_usage():
    no_details = cast(Any, {"input_token_details": None, "output_token_details": None})
    assert _token_usage(no_details) == TokenUsage()


async def test_with_handler_keeps_existing_callbacks():
    handler = LangGraphStreamHandler(Recorder())
    existing = LangGraphStreamHandler(Recorder())
    assert _with_handler(None, handler) == [handler]
    assert _with_handler([existing], handler) == [existing, handler]
    manager = _with_handler(AsyncCallbackManager([existing]), handler)
    assert isinstance(manager, AsyncCallbackManager)
    assert manager.handlers == [existing, handler]


async def test_tool_calls_are_requested_before_the_interaction_ends():
    recorder = Recorder()
    await FakeChatModel().ainvoke("ask", config={"callbacks": [LangGraphStreamHandler(recorder)]})

    assert [type(e).__name__ for e in recorder.events] == [
        "ModelInteractionStarted",
        "ToolRequested",
        "ModelInteractionEnded",
    ]
    assert recorder.events[1] == ToolRequested(
        tool_id="call-ask", tool_name="ask_user", tool_input={"question": "Which city?"}
    )


async def test_non_streaming_model_publishes_its_reply():
    recorder = Recorder()
    model = FakeChatModel(disable_streaming=True)
    await model.ainvoke("hello", config={"callbacks": [LangGraphStreamHandler(recorder)]})

    assert [type(e).__name__ for e in recorder.events] == [
        "ModelInteractionStarted",
        "ReplyDelta",
        "ModelInteractionEnded",
    ]
    assert recorder.events[1].text == "You said: hello"


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


class FakeRunner(Recorder):
    def __init__(self, error: Exception | None = None, result: Any = None) -> None:
        super().__init__()
        self.error = error
        self.result = result
        self.calls: list[dict[str, Any]] = []

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
    context = HarnessContext.model_construct(runner=cast(Any, runner))
    [base_tool] = ToolNode([tool]).tools_by_name.values()
    request = ToolCallRequest(
        tool_call={"name": "double", "args": args, "id": "call-1", "type": "tool_call"},
        tool=base_tool,
        state={},
        runtime=cast(Any, SimpleNamespace(context=context, config={})),
    )
    return await harness_tool_call(request, cast(Any, None))


@agent.tool_defn()
async def scaled(n: int, factor: int) -> str:
    """Scale a number."""
    return str(n * factor)


class ScaledArgs(BaseModel):
    n: int
    factor: int = 3


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

    [base_tool] = ToolNode([double]).tools_by_name.values()
    request = ToolCallRequest(
        tool_call={"name": "double", "args": {"n": 1}, "id": "call-1", "type": "tool_call"},
        tool=base_tool,
        state={},
        runtime=cast(Any, SimpleNamespace(context=None, config={})),
    )
    with pytest.raises(RuntimeError, match="needs a HarnessContext"):
        await harness_tool_call(request, cast(Any, None))


async def test_stream_to_harness_guards(monkeypatch: pytest.MonkeyPatch):
    def sync_node(state: dict[str, Any]) -> dict[str, Any]:
        return state

    with pytest.raises(TypeError, match="async node"):
        stream_to_harness(cast(Any, sync_node))

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        return state

    target = "temporal_agent_harness.ai_sdks.langgraph_harness.get_runtime"
    context = HarnessContext.model_construct(stream_context=cast(Any, object()))
    monkeypatch.setattr(target, lambda: SimpleNamespace(context=context))
    with pytest.raises(RuntimeError, match="activity node"):
        await stream_to_harness(node)({})

    # Outside a harness turn, with or without options, the node runs unchanged.
    monkeypatch.setattr(target, lambda: SimpleNamespace(context=None))
    streamed = stream_to_harness(batch_interval=timedelta(seconds=1))(node)
    assert await streamed({"a": 1}) == {"a": 1}


def test_only_caller_errors_are_non_retryable():
    def error(status: object) -> Exception:
        return type("ProviderError", (Exception,), {"status_code": status})()

    statuses = [400, 401, 404, 408, 409, 429, 500, 503, None, "401"]
    assert [s for s in statuses if _non_retryable(error(s))] == [400, 401, 404]


def test_citations_and_hosted_tools_reach_the_stream():
    recorder = Recorder()
    handler = LangGraphStreamHandler(recorder)
    cite = {"type": "citation", "url": "https://x.test", "start_index": 0, "end_index": 2}
    handler._publish_content(
        AIMessage(
            content=[
                {"type": "server_tool_call", "id": "ws_1", "name": "web_search", "args": {"query": "q"}},
                {"type": "server_tool_result", "tool_call_id": "ws_1", "status": "success"},
                {"type": "server_tool_result", "tool_call_id": "ws_1", "status": "success"},
                {"type": "text", "text": "hi", "annotations": [cite]},
            ]
        )
    )

    assert [type(e).__name__ for e in recorder.events] == [
        "ToolRequested",
        "ToolStartEvent",
        "ToolEndEvent",
        "ReplyDelta",
        "TextAnnotationDelta",
    ]
    assert recorder.events[0].tool_id == "ws_1" and recorder.events[0].tool_input == {"query": "q"}
    assert recorder.events[2].tool_name == "web_search"
    assert recorder.events[4].delta == cite
