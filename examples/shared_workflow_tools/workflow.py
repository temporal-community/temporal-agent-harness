"""Persistent parent and child agents exchanging messages through shared tools."""

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent, RunConfig, Runner

    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tools,
    )
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )

TASK_QUEUE = "shared-tools-openai"


@workflow.defn(name="MessagingResearchAgent")
@agent.defn
class ResearchAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self.inbox = agent.AgentMessageInbox()
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.instructions: list[str] = []

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @workflow.query
    def received_messages(self) -> list[str]:
        return self.instructions

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Research a question, using the parent's queued instructions."""
        instruction = await self.inbox.receive(timeout=timedelta(seconds=10))
        self.instructions.append(instruction.body)
        child = OpenAIAgent(
            name="Researcher",
            tools=as_openai_agent_tools(self._runner, [agent.send_message_tool()]),
        )
        result = await Runner.run(
            child,
            f"{message.text}\nParent instruction: {instruction.body}",
            run_config=RunConfig(tracing_disabled=True),
        )
        return TextReply(text=str(result.final_output))


@workflow.defn(name="MessagingParentAgent")
@agent.defn
class ParentAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self.inbox = agent.AgentMessageInbox()
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.last_result = ""
        self.child_messages: list[agent.WorkflowMessage] = []

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @workflow.query
    def result(self) -> str:
        return self.last_result

    @workflow.query
    def received_messages(self) -> list[agent.WorkflowMessage]:
        return self.child_messages

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Start a child, exchange messages, and drive two turns on the same agent."""
        tools = [
            *agent.subagent_toolset(
                ResearchAgent, key="researcher", task_queue=TASK_QUEUE
            ),
            agent.send_message_tool(),
        ]
        parent = OpenAIAgent(
            name="Coordinator",
            tools=as_openai_agent_tools(self._runner, tools),
        )
        result = await Runner.run(
            parent,
            message.text,
            run_config=RunConfig(tracing_disabled=True),
        )
        self.child_messages.extend(self.inbox.drain())
        self.last_result = str(result.final_output)
        return TextReply(text=self.last_result)
