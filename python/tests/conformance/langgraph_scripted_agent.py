# ABOUTME: The scripted conformance agent rebuilt on LangGraph: a workflow node plays the script,
# ABOUTME: model steps go to a streaming activity node and tool steps to a gated ToolNode.

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import get_runtime
from langgraph.types import Command
from pydantic import Field
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_harness.ai_sdks.langgraph import HarnessContext, graph
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    MidTurn,
    ToolApprovalPolicy,
    ToolRequested,
)
from temporal_agent_harness.harness.state import HarnessState
from tests.conformance.script import Script, ScriptReply
from tests.conformance.scripted_agent import SCRIPTED_AGENT, TOOLS, add

GRAPH = "scripted"


class PlayState(MessagesState):
    steps: list[dict[str, Any]]
    cursor: int
    step: dict[str, Any]
    reply: list[str]


class ScriptedChatModel(BaseChatModel):
    """Streams one `model` step's reply chunks, then its usage."""

    step: dict[str, Any]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:  # what LangChain reports as ls_model_name
        return self.step["model"]

    def _chunks(self) -> list[AIMessageChunk]:
        chunks = [AIMessageChunk(content=text) for text in self.step["reply"]]
        usage = self.step.get("usage")
        last = AIMessageChunk(content="")
        if usage:
            # UsageMetadata requires a total and no None counts; an unreported total is
            # input + output, and unreported counts are left out.
            counts: dict[str, Any] = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens")
                or (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            }
            if (cached := usage.get("cached_tokens")) is not None:
                counts["input_token_details"] = {"cache_read": cached}
            if (thought := usage.get("thought_tokens")) is not None:
                counts["output_token_details"] = {"reasoning": thought}
            last.usage_metadata = {k: v for k, v in counts.items() if v is not None}  # type: ignore[typeddict-item]
        return [*chunks, last]

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        return generate_from_stream(ChatGenerationChunk(message=c) for c in self._chunks())

    async def _astream(  # type: ignore[override]
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks():
            yield ChatGenerationChunk(message=chunk)


class Entries(HarnessState):
    values: dict[str, str] = {}


class PlayContext(HarnessContext):
    agent: Any = Field(default=None, exclude=True, repr=False)


async def begin(state: PlayState) -> dict[str, Any]:
    """Start the turn's script: the last human message carries it."""
    text = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)).text
    return {"steps": json.loads(text)["steps"], "cursor": 0, "reply": []}


async def play(state: PlayState) -> Command[Any]:
    """Play the next step: model and tool steps go to their nodes, the rest run here."""
    i = state["cursor"]
    if i >= len(state["steps"]):
        return Command(goto=END)
    step = state["steps"][i]
    update: dict[str, Any] = {"cursor": i + 1}
    ctx = get_runtime(PlayContext).context
    runner, owner = ctx.runner, ctx.agent
    assert runner is not None
    match step["step"]:
        case "model":
            return Command(goto="model", update={**update, "step": step})
        case "tool":
            call = {"name": step["name"], "args": step["args"], "id": step["id"]}
            # The scripted model stands in for a streamed one, which requests its calls.
            runner.publish(ToolRequested(tool_id=call["id"], tool_name=call["name"], tool_input=call["args"]))
            message = AIMessage(content="", tool_calls=[call])
            return Command(goto="tools", update={**update, "messages": [message]})
        case "state_set":
            with owner.entries.mutate() as d:
                d.values[step["key"]] = step["value"]
        case "state_delete":
            with owner.entries.mutate() as d:
                del d.values[step["key"]]
        case "subagent_start":
            info = workflow.info()
            owner.subagents[step["name"]] = await runner.start_subagent(
                "scripted", info.workflow_type, info.task_queue
            )
        case "subagent_send":
            try:
                await runner.run_subagent_turn(
                    owner.subagents[step["name"]], step["handler"], step["script"]
                )
            except Exception:  # noqa: BLE001 — a failed turn is the model's next input
                pass
        case "subagent_stop":
            await runner.stop_subagent(owner.subagents.pop(step["name"]))
        case "raise":
            raise RuntimeError(step["message"])
    return Command(goto="play", update=update)


async def model(state: PlayState) -> dict[str, Any]:
    message = await ScriptedChatModel(step=state["step"]).ainvoke(state["messages"])
    return {"messages": [message], "reply": [*state["reply"], message.text]}


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    g = StateGraph(PlayState, context_schema=PlayContext)
    g.add_node("begin", begin, metadata={"execute_in": "workflow"})
    g.add_node("play", play, metadata={"execute_in": "workflow"})
    g.add_node(
        "model",
        model,
        metadata={
            "execute_in": "activity",
            "stream_to_harness": True,
            "start_to_close_timeout": timedelta(seconds=30),
            "heartbeat_timeout": timedelta(seconds=5),
        },
    )
    g.add_node(
        "tools",
        ToolNode(list(TOOLS.values())),
        metadata={"execute_in": "workflow"},
    )
    g.add_edge(START, "begin")
    g.add_edge("begin", "play")
    g.add_edge("model", "play")
    g.add_edge("tools", "play")
    return g


@agent.defn(name=SCRIPTED_AGENT)
class LangGraphScriptedAgent:
    entries = agent.state(Entries)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_tools(
                ["add", "echo", "fail", "ask_client"]
            ),
        )
        self.subagents: dict[str, str] = {}
        self._checkpointer = InMemorySaver()
        self._thread = {"configurable": {"thread_id": workflow.info().workflow_id}}

    @agent.accepts(mid_turn=MidTurn.REJECT)
    async def run_reject(self, script: Script) -> ScriptReply:
        """Play the script; refused while a turn is open."""
        return await self._play(script)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def run_enqueue(self, script: Script) -> ScriptReply:
        """Play the script; queued behind an open turn."""
        return await self._play(script)

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def run_accept(self, script: Script) -> ScriptReply:
        """Play the script; joins an open turn."""
        return await self._play(script)

    async def _play(self, script: Script) -> ScriptReply:
        g = (
            graph(GRAPH, self._runner, agent=self)
            .compile(checkpointer=self._checkpointer)
            .with_config(self._thread)
        )
        await g.ainvoke({"messages": [HumanMessage(script.model_dump_json())]})
        state = await g.aget_state(g.config or {})
        return ScriptReply(text="".join(state.values["reply"]))


ACTIVITY_TOOLS = [add]
