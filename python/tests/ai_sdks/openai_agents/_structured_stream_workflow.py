# ABOUTME: The agent workflow for test_structured_streaming. Lives in its own module because the
# test runs under the WORKFLOW SANDBOX, which re-imports the workflow's module.

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner
    from pydantic import BaseModel

    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextMessage


class Answer(BaseModel):
    """A structured answer."""

    done: bool
    note: str


@agent.defn
class StructuredStreamingAgent:
    """Streams one model call whose reply is a structured ``Answer``."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> Answer:
        """Ask; the model answers with an ``Answer``."""
        sdk_agent = OpenAIAgent(
            name="Structured", instructions="Answer.", model="gpt-5.1", output_type=Answer
        )
        result = Runner.run_streamed(sdk_agent, input=message.text, context=self._runner)
        async for _ in result.stream_events():
            pass
        return Answer.model_validate(result.final_output)
