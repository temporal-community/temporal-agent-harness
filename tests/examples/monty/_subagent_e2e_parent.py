"""A minimal, MODEL-FREE parent agent used only by the subagent end-to-end test.

The real conversational parent (``conversational_subagent_workflow.py``) drives the Monty
script-runner subagent through a live Gemini loop — which can't run in CI. This stand-in
strips the model out entirely: its single ``@agent.accepts`` handler drives the subagent
runner methods (``start_subagent`` → ``run_subagent_turn`` → ``stop_subagent``) directly, so a
``WorkflowEnvironment`` test can prove the whole subagent mechanism — the handle indirection,
the ``run_subagent_turn`` activity against a real child, the per-subagent FIFO gate, and the
turn-counter / stream-offset bookkeeping across multiple turns — against a real
:class:`~.workflow.MontyDynamicAgentWorkflow` child, with no model in the loop.

It is kept in its own module (not in the test file) because the Temporal workflow sandbox
re-imports a workflow's defining module; the test module imports ``Worker`` / ``Client`` /
``SubagentActivities`` at top level, which the sandbox would reject. The imports here mirror the
real agents' sandbox-safe set.
"""

from __future__ import annotations

import asyncio

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextReply, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.subagent_toolset import _resolve_workflow_type

    from examples.monty.workflow import MontyDynamicAgentWorkflow


class DriveSubagent(BaseModel):
    """Drive one MontyDynamicAgent subagent end to end.

    Starts a single script-runner instance, runs each script in ``scripts`` on it (each its
    own subagent turn, so this exercises the turn counter + stream-offset resume across turns),
    then stops it. The reply concatenates the per-turn outputs."""

    task_queue: str = Field(
        description="The task queue the child MontyDynamicAgent worker polls (the test's queue)."
    )
    scripts: list[str] = Field(
        description="Scripts to run in order on the one subagent instance, one turn each."
    )
    concurrent: bool = Field(
        default=False,
        description="If true, dispatch all scripts at once via asyncio.gather (exercises the "
        "per-subagent FIFO gate) instead of awaiting them sequentially.",
    )
    stop: bool = Field(
        default=True,
        description="If true (default), stop the subagent at the end of the turn. Set false to "
        "leave it alive+idle — needed by the client stream-merge test, since a stopped subagent "
        "is a COMPLETED workflow whose stream can't yet be read post-completion (a known "
        "workflow_streams limitation with an upstream fix in flight).",
    )


@workflow.defn(name="SubagentE2EParent")
@agent.defn
class SubagentE2EParentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The handler calls the runner's subagent methods directly (not via run_tool), so
            # tool approval never enters the picture; any policy works.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def drive(self, msg: DriveSubagent) -> TextReply:
        """Start a MontyDynamicAgent subagent, run the given scripts on it, then stop it, and
        reply with the concatenated per-turn outputs."""
        handle = await self._runner.start_subagent(
            "monty", _resolve_workflow_type(MontyDynamicAgentWorkflow), msg.task_queue
        )
        try:
            if msg.concurrent:
                outputs = await asyncio.gather(
                    *(self._run_one(handle, script) for script in msg.scripts)
                )
            else:
                outputs = [
                    await self._run_one(handle, script) for script in msg.scripts
                ]
        finally:
            if msg.stop:
                await self._runner.stop_subagent(handle)
        return TextReply(text="\n---\n".join(outputs))

    async def _run_one(self, handle: str, script: str) -> str:
        out = await self._runner.run_subagent_turn(
            handle, "run_script", {"script": script}
        )
        return out.get("text", "")


@workflow.defn(name="ApprovalGatedSubagentParent")
@agent.defn
class ApprovalGatedSubagentParentWorkflow:
    """Like :class:`SubagentE2EParentWorkflow`, but drives the subagent through the GENERATED
    toolset via ``run_tool`` under ``always_require_approvals`` — so each send is gated on a
    real human approval BEFORE its tool body (and thus the FIFO ``take_ticket``) runs. This
    reproduces the real conversational agent's timing, where the approval await precedes the
    ticket and gated calls unblock in approval order, not call order — the path a plain
    ``gather`` of ``run_subagent_turn`` (no gate-before-ticket await) does not exercise."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def drive(self, msg: DriveSubagent) -> TextReply:
        """Start a subagent, then dispatch one gated send per script CONCURRENTLY through the
        generated ``monty_run_script`` tool (tool ids ``send-0``, ``send-1``, …). Blocks until
        each is approved; replies with the concatenated outputs."""
        tools = {
            t.__name__: t
            for t in agent.subagent_toolset(
                MontyDynamicAgentWorkflow, key="monty", task_queue=msg.task_queue
            )
        }
        send = tools["monty_run_script"]
        handle = await self._runner.start_subagent(
            "monty", _resolve_workflow_type(MontyDynamicAgentWorkflow), msg.task_queue
        )
        replies = await asyncio.gather(
            *(
                self._runner.run_tool(
                    f"send-{i}", send, subagent=handle, message={"script": script}
                )
                for i, script in enumerate(msg.scripts)
            )
        )
        return TextReply(text="\n---\n".join(r.text for r in replies))
