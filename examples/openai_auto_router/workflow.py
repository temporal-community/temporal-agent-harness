"""An OpenAI Agents SDK ``model=\"auto\"`` example."""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.model_routing import (
        ModelRoutingActivityOptions,
        OPENAI_MODEL_ROUTING_CONFIG,
        resolve_model,
    )


TASK_QUEUE = "openai-auto-router"
MODEL_ROUTING_ACTIVITY_OPTIONS = ModelRoutingActivityOptions()
SYSTEM_INSTRUCTION = """\
You are a helpful assistant. Answer the user's request clearly and concisely.
"""


@workflow.defn(name="OpenAIAutoRouterAgent")
@agent.defn
class OpenAIAutoRouterAgentWorkflow:
    """A conversational OpenAI agent with automatic model selection."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Answer the user's request."""
        model_id = await resolve_model(
            "auto",
            message.text,
            OPENAI_MODEL_ROUTING_CONFIG,
            MODEL_ROUTING_ACTIVITY_OPTIONS,
        )
        sdk_agent = OpenAIAgent(
            name="Auto Router",
            instructions=SYSTEM_INSTRUCTION,
            model=model_id,
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
