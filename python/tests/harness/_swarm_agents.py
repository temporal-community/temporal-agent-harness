"""Model-free agents for the swarm tests (``test_swarm.py``).

Kept apart from the test module because the Temporal workflow sandbox re-imports a workflow's
defining module, and the test module imports client-side code the sandbox rejects.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SwarmMembership,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


class Echo(BaseModel):
    """Text to echo back."""

    text: str


class AskPeer(BaseModel):
    """Ask another running agent one thing."""

    peer: str = Field(description="The peer's workflow id.")
    handler: str = Field(description="The peer handler to call.")
    text: str


class Spawn(BaseModel):
    """Start one SwarmTestAgent as a subagent and leave it running."""

    task_queue: str


class BoardWork(BaseModel):
    """Post ``titles`` to the swarm board, then complete one item and release another."""

    titles: list[str]


class BoardReport(BaseModel):
    """What happened on the board during a ``work_board`` turn."""

    posted: list[str]
    completed: str | None
    released: str | None
    empty_claim: bool = Field(description="Whether a claim for an absent tag came back empty.")


@agent.tool_defn()
async def gated_echo(text: str) -> str:
    """Echo ``text`` — gated on a human approval by the agent's policy."""
    return text


@agent.defn(name="SwarmTestAgent")
class SwarmTestAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Only ``gated`` runs a tool, and it exists to be gated.
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )

    @agent.accepts
    async def echo(self, msg: Echo) -> TextReply:
        """Reply with the text."""
        return TextReply(text=f"echo: {msg.text}")

    @agent.accepts
    async def slow_echo(self, msg: Echo) -> TextReply:
        """Reply with the text after a while."""
        await workflow.sleep(2)
        return TextReply(text=f"echo: {msg.text}")

    @agent.accepts
    async def fail(self, msg: Echo) -> TextReply:
        """Always fail."""
        raise RuntimeError(f"failing on purpose: {msg.text}")

    @agent.accepts
    async def ask(self, msg: AskPeer) -> TextReply:
        """Ask a peer and relay its reply."""
        out = await self._runner.ask_peer(msg.peer, msg.handler, {"text": msg.text})
        return TextReply(text=out["text"])

    @agent.accepts
    async def gated(self, msg: Echo) -> TextReply:
        """Run one gated tool call."""
        text = await self._runner.run_tool("gated-1", gated_echo, text=msg.text)
        return TextReply(text=text)

    @agent.accepts
    async def spawn(self, msg: Spawn) -> TextReply:
        """Start a subagent and reply with its handle."""
        handle = await self._runner.start_subagent("worker", "SwarmTestAgent", msg.task_queue)
        return TextReply(text=handle)

    @agent.accepts
    async def spawn_and_fail(self, msg: Spawn) -> TextReply:
        """Start a subagent and send it a turn that fails."""
        handle = await self._runner.start_subagent("worker", "SwarmTestAgent", msg.task_queue)
        await self._runner.run_subagent_turn(handle, "fail", {"text": "child"})
        return TextReply(text="unreachable")

    @agent.accepts
    async def work_board(self, msg: BoardWork) -> BoardReport:
        """Exercise every board operation once."""
        board = agent.SwarmBoard.for_current_agent()
        posted = [(await board.post(title, tags=["test"])).id for title in msg.titles]
        first = await board.claim(tags=["test"])
        completed = None
        if first is not None:
            completed = (await board.complete(first.id, {"by": "test"})).id
        second = await board.claim()
        released = None
        if second is not None:
            released = (await board.release(second.id, "not mine")).id
        empty = await board.claim(tags=["no-such-tag"])
        return BoardReport(
            posted=posted, completed=completed, released=released, empty_claim=empty is None
        )


class Seed(BaseModel):
    """Seed ``lane`` with ``titles`` in one call."""

    lane: str
    titles: list[str]


@agent.defn(name="SwarmFounderAgent")
class SwarmFounderAgent:
    """Founds a swarm named after itself unless its caller placed it in one."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            swarm_default=SwarmMembership(
                swarm_id=workflow.info().workflow_id, role="founder"
            ),
        )

    @agent.accepts
    async def seed(self, msg: Seed) -> TextReply:
        """Post ``titles`` to ``lane`` in one update; reply with the item ids."""
        board = agent.SwarmBoard.for_current_agent(lane=msg.lane)
        items = await board.post_many([(title, None, ()) for title in msg.titles])
        return TextReply(text=",".join(item.id for item in items))
