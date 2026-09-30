"""DagStepAgent: one step of a flow, an agent whose whole configuration arrives as init data.

The builder's ``run_agent`` host function starts one of these per step, as a subagent, with a
:class:`~.models.StepSpec` naming the step, its system prompt, which of the step tools it gets
and which model runs it. It sends the step's task to ``ask`` and stops the agent once it replies.
So the agent carries nothing of its own: two instances can be a flight scout and a judge.

The spec is also published as the ``profile`` state, so a client watching the flow can label
each step without knowing how the flow started it.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .models import StepProfile, StepSpec
    from .step_tools import STEP_TOOLS


TASK_QUEUE = "agent-dag"
WORKFLOW_TYPE = "DagStepAgent"

# Appended to every step's own instructions: a step's reply is read by the next step or the
# user, never answered, so it must stand on its own.
STEP_RULES = """

You are one step in a larger flow of agents. Do your task with the tools you have, then reply \
with your result in a few sentences or a short list. Your reply is handed to the next step as \
is, so state the concrete facts it needs (ids, prices, codes) and don't ask questions."""


@agent.defn(name=WORKFLOW_TYPE)
class DagStepAgentWorkflow:
    profile = agent.state(StepProfile)

    @agent.init
    def __init__(self, config: AgentConfig, spec: StepSpec) -> None:
        self._spec = spec
        self.profile.set(
            StepProfile(
                name=spec.name,
                instructions=spec.instructions,
                tools=list(spec.tools),
                model=spec.model,
            )
        )
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Every step tool is simulated, so nothing here needs a person's approval.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TResponseInputItem] = []

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Give this step its task. It works on it with its tools and replies with the result."""
        sdk_agent = OpenAIAgent(
            name=self._spec.name,
            instructions=self._spec.instructions + STEP_RULES,
            model=self._spec.model,
            tools=[as_openai_agent_tool(self._runner, STEP_TOOLS[name]) for name in self._spec.tools],
        )
        result = Runner.run_streamed(
            sdk_agent,
            input=[*self._conversation, {"role": "user", "content": message.text}],
            context=self._runner,
        )
        async for _event in result.stream_events():
            pass
        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
