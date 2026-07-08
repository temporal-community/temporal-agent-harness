"""Trivial, model-free harness agent used to demo the Nexus-brokered subagent path (agent
registry discovery + sendAgentMessage/pollMessages) end to end, with no LLM involved. Its one
capability, ``echo``, just upper-cases the input text — the point is exercising the Nexus
transport, not doing real work. See ``demo_parent_workflow.py`` for the parent that drives
this as a subagent, and ``echo_worker.py`` for how it registers itself with the agent registry
so that parent can discover it.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

ECHO_AGENT_TASK_QUEUE = "echo-agent"


class EchoRequest(BaseModel):
    """Text to echo back."""

    text: str = Field(description="The text to echo back, uppercased.")


class EchoReply(BaseModel):
    """The echoed text."""

    text: str = Field(description="`text` from the request, uppercased.")


@workflow.defn(name="EchoSubagent")
@agent.defn
class EchoSubagentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def echo(self, msg: EchoRequest) -> EchoReply:
        """Echo `text` back, uppercased."""
        return EchoReply(text=msg.text.upper())
