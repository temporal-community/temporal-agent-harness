# LangGraph on the harness

Run a [LangGraph](https://www.langchain.com/langgraph) graph as a harness agent. Each node runs
as a Temporal activity or inline in the workflow, and the chat models an activity node calls
stream live onto the agent's turn: replies, reasoning, citations, tool requests and token usage.
Every `ToolNode` call goes through the harness: the approval policy, `tool_start` / `tool_end` /
`tool_error` events, `Injected` parameters, and mid-turn steering.

Everything comes from one package, `temporal_agent_harness.ai_sdks.langgraph`: the Temporal
LangGraph plugin, vendored from `temporalio/ai-integrations` with a tool-call seam and a streaming
seam added, and beside it the harness's own module, `_harness.py`, which the plugin modules never
import. `graph(name, runner)` binds a registered graph to the agent, `harness_tool_call` gates
every tool call, and `harness_observer_factory` streams model calls onto the turn. See
[Vendoring](#vendoring).

`python/examples/langgraph_hello` is a complete agent using everything below.

> **Experimental.** This integration may change in future versions.

## Installation

```sh
uv add 'temporal-agent-harness[langgraph]'
```

The extra brings `langgraph`, `langgraph-prebuilt` and `langchain-core`. Install the model
provider you use too, e.g. `langchain-openai` for `ChatOpenAI`.

## Quickstart

### Workflow

```python
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    from temporal_agent_harness.ai_sdks.langgraph import graph
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MessageContext,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner, Injected


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It's 72°F and sunny in {city}."


async def call_model(state: MessagesState) -> dict:
    model = ChatOpenAI(model="gpt-5.1", max_retries=0).bind_tools([get_weather])
    return {"messages": [await model.ainvoke(state["messages"])]}


def build_graph() -> StateGraph:
    g = StateGraph(MessagesState)
    g.add_node("agent", call_model, metadata={
        "execute_in": "activity",
        "start_to_close_timeout": timedelta(minutes=2),
        "heartbeat_timeout": timedelta(seconds=10),
        "retry_policy": RetryPolicy(maximum_attempts=3),
    })
    g.add_node("tools", ToolNode([get_weather]), metadata={"execute_in": "workflow"})
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, ["tools", END])
    g.add_edge("tools", "agent")
    return g


@agent.defn(name="WeatherAgent")
class WeatherAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._graph = graph("weather", self._runner).compile(checkpointer=InMemorySaver())

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def ask(self, message: TextMessage, ctx: Injected[MessageContext]) -> TextReply:
        """Chat with the assistant; sent mid-turn, the message steers it."""
        if ctx.joined_turn:
            self._runner.steer(message.text)
            return TextReply(text="")
        config = {"recursion_limit": 25, "configurable": {"thread_id": workflow.info().workflow_id}}
        result = await self._graph.ainvoke({"messages": [HumanMessage(message.text)]}, config)
        return TextReply(text=result["messages"][-1].text)
```

Compile the graph once, in the `@agent.init` method. Without a runner, `graph(name)` is the
plugin's own lookup, as upstream documents it. The compiled graph is plain LangGraph: the
checkpointer, its `thread_id`, and one-off or threaded calls are yours. Pass a `recursion_limit`:
a model can re-call a denied tool indefinitely, and reaching the limit fails the turn.

### Worker

```python
from temporal_agent_harness.ai_sdks.langgraph import (
    LangGraphPlugin,
    harness_observer_factory,
    harness_tool_call,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

langgraph = LangGraphPlugin(
    graphs={"weather": build_graph()},
    awrap_tool_call=harness_tool_call,
    observer_factory=harness_observer_factory,
)
client = await Client.connect(..., plugins=[langgraph, AgentHarnessPlugin(tools=[...])])
worker = Worker(client, task_queue="weather", workflows=[WeatherAgent])
```

Put `LangGraphPlugin` on the client, before `AgentHarnessPlugin`. Workers built from that client
pick both up. `LangGraphPlugin` registers each graph's activity nodes and the workflow-sandbox
passthroughs LangGraph needs. `AgentHarnessPlugin(tools=[...])` registers the activity bodies of
your `@agent.activity_tool_defn` tools. Without `awrap_tool_call=harness_tool_call`,
`graph(name, runner).compile()` refuses the graph; without
`observer_factory=harness_observer_factory`, nodes run but nothing streams onto the turn.

## Streaming

`graph(name, runner)` routes every activity call of the graph to the turn in progress. Inside the
activity, the plugin hands the node's LangChain callback events to the harness observer:

| Callback event | Turn event |
|---|---|
| a chat model starts | `model_interaction_started` (its `ls_model_name`) |
| a streamed text chunk | `reply_delta` |
| a reasoning block | `thought_summary` |
| a citation on text | `text_annotation` |
| a tool call in the model's message | one `tool_requested`, keyed by the provider's call id |
| a tool the provider ran itself (web search, …) | `tool_requested`, `tool_start` and `tool_end`, which no approval policy can gate |
| the model finishes or fails | `model_interaction_ended`, with its usage |

A model called directly or through a chain (`prompt | model`) streams; a model called under a
tool run, or tagged `nostream`, doesn't. Tag a structured-output chain
(`model.with_structured_output(...).with_config(tags=["nostream"])`), or any model call that is
not the reply, or its text streams as `reply_delta`. A model that doesn't stream (for example
`disable_streaming=True`) publishes its whole reply when it finishes. Events are batched like
other integrations' (the plugin's `streaming_batch_interval`, 100 ms by default). The workflow
still receives each node's result from the activity, so replay never depends on the stream.

An observed activity heartbeats while it runs, so give model nodes a `heartbeat_timeout`: a lost
worker is retried after it, not after `start_to_close_timeout`. Give every activity node a
`retry_policy` too: Temporal retries forever by default, and each attempt streams again. A model
error carrying a 4xx `status_code` other than 408, 409 and 429 fails without retrying.

Temporal is the only retry layer: turn off retries in the model client you build in a node
(`ChatOpenAI(..., max_retries=0)`, for example), and configure them with the node's
`retry_policy` instead. A client that retries inside the activity repeats a call Temporal can't
see, and streams it again.

## Tools

Every call a `ToolNode` makes in a harness graph gets the agent's approval policy, lifecycle
events and steering:

| Tool | Declare with | Runs |
|---|---|---|
| Workflow tool | `@agent.tool_defn` | inline, in the workflow |
| Activity tool | `@agent.activity_tool_defn` | on a worker, as a durable activity; register it with `AgentHarnessPlugin(tools=[...])` |
| Callback tool | `@agent.callback_tool_defn` | on a client: the call waits in the workflow until a client posts the result |

Give the tools to a `ToolNode` that runs with `execute_in="workflow"`, as you would plain
LangChain tools; the plugin sets `harness_tool_call` as its `awrap_tool_call`. The model sees each
tool's model-facing signature, so `Injected[...]` parameters stay hidden, and arguments are
validated against the tool's schema before it runs. A failed, invalid or denied call reaches the
model as an error `ToolMessage`. A call the model gave no id, or a repeated one, runs under a fresh
id, published as its own `tool_requested`.

A tool that isn't a harness tool, a plain LangChain `@tool` for example, is refused. Write it as a
harness tool instead, such as an `@agent.activity_tool_defn` that calls it. In the Functional API,
a `ToolNode` built inside a task takes `awrap_tool_call=harness_tool_call` itself.

### Injected parameters

A tool parameter annotated `Injected[...]` is filled by the workflow, not the model. Pass the
values, keyed by parameter name, when you bind the graph:

```python
@agent.tool_defn()
async def remember(note: str, notes: Injected[StateRef[Notes]]) -> str:
    """Save a note the user wants kept."""
    with notes.mutate() as draft:
        draft.items.append(note)
    return f"Saved: {note}"


# in @agent.init
self._graph = graph("weather", self._runner, injections={"notes": self.notes}).compile(
    checkpointer=InMemorySaver()
)
```

Every call hands the mapping to `runner.run_tool(..., injections=...)`. The mapping is bound when
the graph is compiled, so inject long-lived values rather than per-turn ones; an activity tool's
injected values must be serializable, since they travel in its activity input.

## Human input and approvals

LangGraph pauses a graph for a person with `interrupt()`: the run stops, and the caller resumes it
with `Command(resume=...)`. A harness agent uses the harness's own mechanisms instead, which keep
the turn running:

- **Ask a person or a client for something:** an `@agent.callback_tool_defn` tool. Its call
  publishes `callback_requested`, waits durably in the workflow, and returns the client's answer
  as the tool's result.
- **Approve a tool call:** the agent's `ToolApprovalPolicy`.

A harness graph refuses interrupts. If a node or a tool calls `interrupt()`, the call fails with
a non-retryable `ApplicationError` of type `LangGraphInterruptNotSupported`, the turn publishes
`message_handler_error`, and the turn ends. `graph(name, runner).compile()` refuses
`interrupt_before` and `interrupt_after`.

## Mid-turn steering

Accept messages mid-turn (`@agent.accepts(mid_turn=MidTurn.ACCEPT)`) and, when
`ctx.joined_turn`, queue the text with `runner.steer(text)`, as in the quickstart. The harness
appends queued steering to the next tool result the model receives. Don't run the graph for a
joined message: two runs at once on one thread overwrite each other's checkpoint.

## The Functional API

Register an `@entrypoint` and its `@task`s with the plugin, then bind the entrypoint with
`entrypoint(name, runner, injections=...)`. An entrypoint is built once at import, so give
each workflow its own checkpointer: `.copy({"checkpointer": InMemorySaver()})`. The plugin wraps a
task in place, so construct the plugin once per process.

## History and replay

- Each activity attempt is its own model interaction. A failed attempt's deltas stay on the
  stream, and one lost to a crash or timeout leaves its `model_interaction_started` unmatched.
  Tools and approvals survive crashes: no tool runs twice.
- An `InMemorySaver` checkpoint lives in workflow memory, and harness agents don't
  continue-as-new. Streaming dominates history, roughly 3.7 events per token watched, and each
  activity node receives the whole state as its input, so long conversations approach Temporal's
  history limits.
- A failed turn keeps its completed steps in the checkpoint; the next message starts a new run
  with the failed message still in the conversation.
- Adding, removing or reordering activity nodes on an executed path breaks replay of running
  agents; use `workflow.patched()` or Worker Versioning.

## Not available in a harness agent

- `interrupt()`, `interrupt_before` and `interrupt_after`: see
  [Human input and approvals](#human-input-and-approvals).
- Prebuilt agents such as `create_react_agent`, which LangGraph deprecates in favor of LangChain's
  `create_agent`: their nodes carry no `execute_in`. Build the graph yourself.
- A LangGraph `Store` inside activity nodes, which can't cross the activity boundary.

The upstream plugin's other features work as upstream documents them: activity options and
summaries, `Send`, subgraphs, sync nodes and routers, typed node values, and raw
`streaming_topic` streaming beside the turn stream. See
[upstream's README at the vendored commit](https://github.com/temporalio/ai-integrations/blob/dfc455836f717ba9c43795f3b93216604671609a/python/langgraph/README.md).

## Vendoring

This package is `temporalio/ai-integrations` `python/langgraph` at
`dfc455836f717ba9c43795f3b93216604671609a` (after PyPI `temporalio-langgraph` 0.1.0; the next
commit needs temporalio 1.36). `_interceptor.py`, `_langgraph_config.py`, `_task_cache.py` and
`_workflow.py` match upstream exactly; `_harness.py` is the harness's own module, not upstream's.

- **Imports:** `temporalio.langgraph` imports are rewritten to
  `temporal_agent_harness.ai_sdks.langgraph`. Gazelle derives Bazel deps only from absolute
  imports. Upstream's `py.typed` is not copied.
- **Local changes:** every local change is marked with a `# Harness:` comment. They are:
  - the tool-call seam: `LangGraphPlugin(awrap_tool_call=...)` sets the wrapper on every Graph
    API `ToolNode` that has none;
  - the streaming seam: `LangGraphPlugin(observer_factory=...)`, `graph()` /
    `entrypoint(stream_route=...)`, a `stream_to` token on the activity input, and the bridge
    that hands an observed activity's LangChain callback events to the observer;
  - a `ToolNode` fix not yet upstream: a `ToolNode` with an `awrap_tool_call` runs through its
    async path, the only one that calls it, and a node function bound to its own node is unbound
    so `compile()` returns. `tests/ai_sdks/langgraph/test_tool_node.py` covers it;
  - the workflow-sandbox passthrough of this package (upstream gets it under `temporalio.*`);
  - `LangGraphPlugin.version()` naming the harness distribution;
  - `__init__.py` exporting `_harness.py`'s `graph()` and `entrypoint()`, which take an agent's
    runner and otherwise call the plugin's, beside `harness_tool_call` and
    `harness_observer_factory`.
- **Syncing upstream:** copy the new upstream source, repeat the import rewrite, and re-apply the
  marked hunks. Upstream's own test suite passes against this copy once its imports are rewritten
  the same way, except its installed-package check and its package-version test (`version()` names
  the harness here).
