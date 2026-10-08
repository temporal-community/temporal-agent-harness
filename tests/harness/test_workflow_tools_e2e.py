"""Exercise shared child execution and Signal inboxes on a real Temporal server."""

import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
)


@workflow.defn
class IndependentChild:
    @workflow.run
    async def run(self, question: str) -> str:
        """Answer without any inner agent SDK."""
        parent = workflow.info().parent
        assert parent is not None
        await workflow.get_external_workflow_handle(parent.workflow_id).signal(
            "temporal_agent_harness.receive_message",
            agent.WorkflowMessage(
                str(workflow.uuid4()),
                workflow.info().workflow_id,
                workflow.info().run_id,
                "working",
            ),
        )
        return "answer:" + question


@workflow.defn
@agent.defn
class WorkflowToolProbe:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self.inbox = agent.AgentMessageInbox()
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=agent.ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.reply = ""

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self.runner.run(self)

    @workflow.query
    def result(self) -> str:
        return self.reply

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Execute a child and consume its asynchronous progress message."""
        answer = await self.runner.run_tool(
            "research",
            agent.child_workflow_as_tool(IndependentChild.run),
            question=message.text,
        )
        progress = await self.inbox.receive(timeout=timedelta(seconds=5))
        self.reply = answer + ";" + progress.body
        return TextReply(text=self.reply)


async def test_shared_child_and_inbox_real_temporal():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    ) as env:
        queue = "workflow-tools-" + str(uuid.uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[WorkflowToolProbe, IndependentChild],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                WorkflowToolProbe.run,
                AgentConfig(),
                id=queue,
                task_queue=queue,
                execution_timeout=timedelta(seconds=30),
            )
            try:
                await handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="ask", payload={"text": "why?"}, expected_turn=1),
                    result_type=AgentMessageReply,
                )
                await workflow_result(handle)
                history = await handle.fetch_history()
                assert any(
                    e.HasField("child_workflow_execution_completed_event_attributes")
                    for e in history.events
                )
                assert any(
                    e.HasField("workflow_execution_signaled_event_attributes")
                    for e in history.events
                )
            finally:
                await handle.signal("close")
                await handle.result()


async def workflow_result(handle):
    # Poll through the external client while the harness completes the accepted turn.
    import asyncio

    async with asyncio.timeout(15):
        while await handle.query(WorkflowToolProbe.result) != "answer:why?;working":
            await asyncio.sleep(0.01)
