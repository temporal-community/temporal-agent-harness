# ABOUTME: A model-free stand-in for DagBuilderAgent's `generate` turn that makes exactly one
# `check_code` call, wired the way the builder wires it, so a test can drive the check (and its
# `export_code` callback) without calling OpenAI.

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from examples.agent_dag.workflow import check_code, run_agent


@agent.defn(name="CheckCodeParent")
class CheckCodeParentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._run_flow = agent.code_mode_tool(
            [run_agent], name="run_flow", injections={"runner": self._runner}
        )

    @agent.accepts
    async def check(self, message: TextMessage) -> TextReply:
        """Run `check_code` once and reply with what it reported."""
        report = await self._runner.run_tool(
            str(workflow.uuid4()),
            check_code,
            injections={"runner": self._runner, "flow_tool": self._run_flow},
        )
        return TextReply(text=report)
