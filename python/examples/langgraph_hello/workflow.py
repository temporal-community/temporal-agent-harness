"""A hello-world LangGraph agent on the harness: a weather chat with one of each harness tool kind.

The turn is one run of a two-node ``StateGraph``: the ``agent`` node calls a reasoning chat model
in an activity, where its ``stream_to_harness`` option publishes the stream as
``model_interaction_started``, ``thought_summary``, ``reply_delta`` and ``model_interaction_ended``
(with token usage); the
``tools`` node is a prebuilt ``ToolNode`` whose calls go through ``harness_tool_call``, so the
harness owns approval and the ``tool_start`` / ``tool_end`` / ``tool_error`` events. Its tools:

* ``get_weather``: an activity tool (``@agent.activity_tool_defn``), inherently safe.
* ``remember``: an inline tool that writes the agent's declared ``Notes`` state; the default
  policy gates it, so the console asks before it runs and shows the state patch after.
* ``ask_user``: a callback tool with no worker body; the console draws an answer box.

``temporalio-langgraph`` runs the graph durably: each node with ``execute_in="activity"`` is an
activity, the rest run in the workflow, and the graph is checkpointed in workflow memory with the
workflow id as its thread. ``graph(name, runner, ...)`` binds each harness turn to that thread.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is registered
in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    from temporal_agent_harness.ai_sdks.langgraph import HarnessContext, graph
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.state import HarnessState, StateRef


TASK_QUEUE = "langgraph-hello"
GRAPH = "langgraph-hello"
DEFAULT_MODEL = "gpt-5.1"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

Tools: `get_weather` returns the current weather for a city; when asked about the weather
somewhere, call it (don't guess) and report the answer in a sentence or two. `remember` saves a
note the user asks you to keep; confirm what you saved. `ask_user` asks the user one question
when a request is ambiguous, such as which city they mean. For anything else, reply directly."""


class Notes(HarnessState):
    """What the user asked the agent to remember, shown live in the console's state pane."""

    items: list[str] = []


@agent.activity_tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    return f"It's 72°F and sunny in {city}."


@agent.tool_defn()
async def remember(note: str, notes: agent.Injected[StateRef[Notes]]) -> str:
    """Save a short note the user wants kept, e.g. "prefers Celsius"."""
    with notes.mutate() as draft:
        draft.items.append(note)
    return f"Saved: {note}"


@agent.callback_tool_defn(inherently_safe=True)
async def ask_user(question: str) -> str:
    """Ask the user ONE question and wait for the answer, without ending your turn."""
    ...


TOOLS = [get_weather, remember, ask_user]


async def call_model(state: MessagesState) -> dict[str, Any]:
    """The model activity: one chat completion over the conversation, streamed to the harness."""
    model = ChatOpenAI(
        model=DEFAULT_MODEL, reasoning={"effort": "medium", "summary": "auto"}
    ).bind_tools(TOOLS)
    messages = [SystemMessage(SYSTEM_INSTRUCTION), *state["messages"]]
    return {"messages": [await model.ainvoke(messages)]}


async def route(state: MessagesState) -> str:
    """``tools_condition`` is sync, and the workflow can only run async routers."""
    return tools_condition(state)


def summarize_model_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Label each model activity in the Temporal UI; ``args[0]`` is the node's state."""
    return f"model call, {len(args[0]['messages'])} messages"


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    g = StateGraph(MessagesState, context_schema=HarnessContext)
    g.add_node(
        "agent",
        call_model,
        metadata={
            "execute_in": "activity",
            "stream_to_harness": True,
            "start_to_close_timeout": timedelta(minutes=2),
            # A streamed node heartbeats, so a lost worker is retried after this, not after
            # start_to_close_timeout.
            "heartbeat_timeout": timedelta(seconds=10),
            # Temporal retries forever by default, and every attempt streams again;
            # A streamed node fails a 4xx (bad key, bad request) without retrying.
            "retry_policy": RetryPolicy(maximum_attempts=3),
            "summary_fn": summarize_model_call,
        },
    )
    g.add_node(
        "tools",
        ToolNode(TOOLS),  # the plugin routes its calls through harness_tool_call
        metadata={"execute_in": "workflow"},
    )
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["tools", END])
    g.add_edge("tools", "agent")
    return g


@agent.defn(name="LangGraphHelloAgent")
class LangGraphHelloAgentWorkflow:
    """A conversational weather agent driven by LangGraph."""

    notes = agent.state(Notes)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # get_weather and ask_user are inherently safe; remember goes to the user first.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._checkpointer = InMemorySaver()
        self._thread_id = str(workflow.uuid4())  # one conversation per workflow

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant: ask about the weather in a city, ask it to remember something,
        or ask something ambiguous and it will ask you back."""
        # graph() binds the turn: the conversation's checkpoint thread, so each turn continues
        # it, and the context that carries the runner and injections.
        g = (
            graph(GRAPH, self._runner, injections={"notes": self.notes}, thread_id=self._thread_id)
            .compile(checkpointer=self._checkpointer)
            .with_config(recursion_limit=25)
        )
        # ask_user is a callback tool: the harness holds the turn open until the console
        # answers it, so one message is one graph run.
        result = await g.ainvoke({"messages": [HumanMessage(message.text)]})
        return TextReply(text=result["messages"][-1].text)
