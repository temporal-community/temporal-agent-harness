"""Persistent agents use existing delegation and message each other both ways."""

import asyncio
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
from temporal_agent_harness.plugin import AgentHarnessPlugin


@workflow.defn
@agent.defn
class MessagingChild:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self.inbox = agent.AgentMessageInbox()
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=agent.ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.received: list[str] = []

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self.runner.run(self)

    @workflow.query
    def messages(self) -> list[str]:
        return self.received

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Consume parent instructions and send progress back while running a turn."""
        instruction = await self.inbox.receive(timeout=timedelta(seconds=5))
        self.received.append(instruction.body)
        await self.runner.run_tool(
            "progress",
            agent.send_message_tool(),
            "parent",
            "received:" + instruction.body,
        )
        return TextReply(text=message.text + ";" + instruction.body)


@workflow.defn
@agent.defn
class MessagingParent:
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
        """Start one subagent and drive repeated turns with bidirectional messages."""
        start, ask, stop = agent.subagent_toolset(
            MessagingChild, key="child", task_queue=workflow.info().task_queue
        )
        handle = await self.runner.run_tool("start", start)
        replies = []
        for index, instruction in enumerate(["first", "followup"]):
            await self.runner.run_tool(
                f"send-{index}", agent.send_message_tool(), handle, instruction
            )
            answer = await self.runner.run_tool(
                f"ask-{index}", ask, subagent=handle, message={"text": message.text}
            )
            progress = await self.inbox.receive(timeout=timedelta(seconds=5))
            replies.append(answer.text + ";" + progress.body)
        await self.runner.run_tool("stop", stop, handle)
        self.reply = "|".join(replies)
        return TextReply(text=self.reply)


async def test_existing_subagent_toolset_with_bidirectional_messages():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        queue = "parent-child-messages-" + str(uuid.uuid4())
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[MessagingParent, MessagingChild],
            plugins=[AgentHarnessPlugin()],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                MessagingParent.run,
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
                async with asyncio.timeout(15):
                    while not (result := await handle.query("result")):
                        await asyncio.sleep(0.01)
                assert (
                    result
                    == "why?;first;received:first|why?;followup;received:followup"
                )
                history = await handle.fetch_history()
                starts = [
                    e
                    for e in history.events
                    if e.HasField(
                        "start_child_workflow_execution_initiated_event_attributes"
                    )
                ]
                assert len(starts) == 1
                child_id = starts[
                    0
                ].start_child_workflow_execution_initiated_event_attributes.workflow_id
                child = env.client.get_workflow_handle(child_id)
                await child.result()
                assert await child.query("messages") == ["first", "followup"]
                child_history = await child.fetch_history()
                parent_messages = [
                    e
                    for e in history.events
                    if e.HasField("workflow_execution_signaled_event_attributes")
                    and e.workflow_execution_signaled_event_attributes.signal_name
                    == "temporal_agent_harness.receive_message"
                ]
                child_messages = [
                    e
                    for e in child_history.events
                    if e.HasField("workflow_execution_signaled_event_attributes")
                    and e.workflow_execution_signaled_event_attributes.signal_name
                    == "temporal_agent_harness.receive_message"
                ]
                assert len(parent_messages) == len(child_messages) == 2
            finally:
                await handle.signal("close")
                await handle.result()
