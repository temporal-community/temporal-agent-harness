# ABOUTME: A model-free stand-in for DagStepAgent, registered under the same workflow type, so a
# flow's run_agent calls start a real subagent without calling OpenAI. It publishes the same
# `profile` state and replies with the spec it was started with and the prompt it was given.

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

    from examples.agent_dag.models import StepProfile, StepSpec
    from examples.agent_dag.step_agent import WORKFLOW_TYPE


@agent.defn(name=WORKFLOW_TYPE)
class EchoStepAgentWorkflow:
    profile = agent.state(StepProfile)

    @agent.init
    def __init__(self, config: AgentConfig, spec: StepSpec) -> None:
        self._spec = spec
        self.profile.set(StepProfile.model_validate(spec.model_dump()))
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo the step's spec and task."""
        spec = self._spec
        return TextReply(text=f"{spec.name}[{','.join(spec.tools)}|{spec.model}]: {message.text}")
