"""Shared child workflow and messaging tools in one OpenAI Agents example."""

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent, Runner, RunConfig

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
    from .models import ANSWER

TASK_QUEUE = "shared-tools-openai"
MAILBOX_ID = "shared-tools-openai-mailbox"


@workflow.defn(name="SharedToolsMailbox")
class MailboxWorkflow:
    def __init__(self):
        self.inbox = agent.AgentMessageInbox()
        self.received: list[agent.WorkflowMessage] = []
        self.closed = False

    @workflow.run
    async def run(self, adapter: str) -> list[agent.WorkflowMessage]:
        while not self.closed:
            await workflow.wait_condition(
                lambda: bool(self.inbox.messages) or self.closed
            )
            self.received.extend(self.inbox.drain())
        return self.received

    @workflow.query
    def messages(self) -> list[agent.WorkflowMessage]:
        return [*self.received, *self.inbox.messages]

    @workflow.signal
    def close(self):
        self.closed = True


@workflow.defn(name="SharedToolsResearcher")
class ResearchWorkflow:
    @workflow.run
    async def run(self, question: str) -> str:
        """Research the demonstration question in a separate durable workflow."""
        parent = workflow.info().parent
        if parent is not None:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(
                "temporal_agent_harness.receive_message",
                agent.WorkflowMessage(
                    str(workflow.uuid4()),
                    workflow.info().workflow_id,
                    workflow.info().run_id,
                    "Research child is working.",
                ),
            )
        await workflow.sleep(
            timedelta(seconds=2), summary="Research demonstration pause"
        )
        return ANSWER


def shared_tools():
    return [
        agent.child_workflow_as_tool(
            ResearchWorkflow.run,
            tool_name="research",
            tool_description="Research the sky question in a child workflow.",
        ),
        agent.send_message_tool({"mailbox": MAILBOX_ID}),
    ]


@workflow.defn(name="SharedToolsOpenAIAgent")
@agent.defn
class OpenAIDemo:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.inbox = agent.AgentMessageInbox()
        self.last_result = ""

    @workflow.query
    def result(self) -> str:
        return self.last_result

    async def finish(self, output: str) -> TextReply:
        progress = await self.inbox.receive(timeout=timedelta(seconds=10))
        self.last_result = output + "\nChild progress: " + progress.body
        return TextReply(text=self.last_result)

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Run the local OpenAI model fixture through both shared tools."""
        sdk_agent = OpenAIAgent(
            name="Shared tools demo",
            tools=as_openai_agent_tools(self._runner, shared_tools()),
        )
        result = await Runner.run(
            sdk_agent, message.text, run_config=RunConfig(tracing_disabled=True)
        )
        return await self.finish(str(result.final_output))
