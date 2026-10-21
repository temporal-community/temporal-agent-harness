# ABOUTME: The scripted integration: a test-only agent whose inner loop plays a Script instead
# of calling a model. It publishes the events a model integration would (model interactions,
# reply deltas, tool requests), exactly as the script states them, and leaves everything else to
# the harness core, through the public authoring API only.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    MidTurn,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_protocol.events import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    ToolRequested,
)
from temporal_agent_harness.harness.state import HarnessState
from tests.conformance.script import (
    ModelStep,
    RaiseStep,
    Script,
    ScriptReply,
    StateDeleteStep,
    StateSetStep,
    SubagentSendStep,
    SubagentStartStep,
    SubagentStopStep,
    ToolStep,
)

SCRIPTED_AGENT = "ScriptedAgent"


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
)
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@agent.tool_defn()
async def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@agent.tool_defn()
async def fail(message: str) -> str:
    """Fail with the message."""
    raise ValueError(message)


@agent.tool_defn()
async def gated(action: str) -> str:
    """An action the approval policy gates."""
    return f"done: {action}"


@agent.callback_tool_defn()
async def ask_client(question: str) -> str:
    """Ask the attached client a question."""
    ...


TOOLS: dict[str, Callable[..., Awaitable[Any]]] = {
    "add": add,
    "echo": echo,
    "fail": fail,
    "gated": gated,
    "ask_client": ask_client,
}


class Entries(HarnessState):
    values: dict[str, str] = {}


@agent.defn(name=SCRIPTED_AGENT)
class ScriptedAgent:
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
        self._subagents: dict[str, str] = {}

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
        reply: list[str] = []
        for step in script.steps:
            match step:
                case ModelStep():
                    self._runner.publish(ModelInteractionStarted(model=step.model))
                    for chunk in step.reply:
                        self._runner.publish(ReplyDelta(text=chunk))
                    reply.extend(step.reply)
                    self._runner.publish(ModelInteractionEnded(model=step.model, usage=step.usage))
                case ToolStep():
                    self._runner.publish(
                        ToolRequested(tool_id=step.id, tool_name=step.name, tool_input=step.args)
                    )
                    try:
                        await self._runner.run_tool(step.id, TOOLS[step.name], **step.args)
                    except Exception:  # noqa: BLE001 — a failed call is the model's next input
                        pass
                case StateSetStep():
                    with self.entries.mutate() as d:
                        d.values[step.key] = step.value
                case StateDeleteStep():
                    with self.entries.mutate() as d:
                        del d.values[step.key]
                case SubagentStartStep():
                    info = workflow.info()
                    self._subagents[step.name] = await self._runner.start_subagent(
                        "scripted", info.workflow_type, info.task_queue
                    )
                case SubagentSendStep():
                    try:
                        await self._runner.run_subagent_turn(
                            self._subagents[step.name],
                            step.handler,
                            step.script.model_dump(mode="json"),
                        )
                    except Exception:  # noqa: BLE001 — a failed turn is the model's next input
                        pass
                case SubagentStopStep():
                    await self._runner.stop_subagent(self._subagents.pop(step.name))
                case RaiseStep():
                    raise RuntimeError(step.message)
        return ScriptReply(text="".join(reply))
