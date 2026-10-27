# ABOUTME: End-to-end test of request_access inside a Code Mode script, as the agent runs it: the
# workflow injects the access board and the runner, the tool asks a person through the
# ask_for_access callback, and the board shows the answer. No model and no policy: a probe
# agent runs the script it is sent, with approvals off.

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

from examples.governed_analytics.access import TableAccessBoard, request_access

SCRIPT = """\
import asyncio
async def main():
    answer = await request_access(table="risk.loan_applications", reason="default rates")
    return answer["access"]
asyncio.run(main())
"""


@agent.defn
class AccessProbeAgent:
    table_access = agent.state(TableAccessBoard)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._code_tool = agent.code_mode_tool(
            [request_access],
            name="run_analysis",
            injections={"access": self.table_access, "runner": self._runner},
        )

    @agent.accepts
    async def script(self, message: TextMessage) -> TextReply:
        """Run the script in the message, then report the board."""
        result = await self._runner.run_tool("t1", self._code_tool, script=message.text)
        board = {row.table: row.status for row in self.table_access.current.tables}
        return TextReply(text=f"{result} | {board}")


@pytest_asyncio.fixture
async def env():
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    yield env
    await env.shutdown()


async def _run_script(env: WorkflowEnvironment, answer: dict[str, Any]) -> tuple[str, list[str]]:
    """Send SCRIPT, answer its callback with ``answer``, and return the reply and the tools
    that failed."""
    task_queue = f"access-probe-{uuid.uuid4()}"
    # Unsandboxed, as the harness's own probe agents run: this module imports the harness
    # directly rather than through the sandbox's pass-through, as workflow.py does.
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[AccessProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            AccessProbeAgent.run,
            AgentConfig(),
            id=f"access-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="script", payload={"text": SCRIPT}),
            result_type=AgentMessageReply,
        )
        reply = None
        seen: list[str] = []
        failed: list[str] = []
        stream = WorkflowStreamClient.create(env.client, handle.id)
        async with asyncio.timeout(30):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                event = item.data.event
                seen.append(f"{event.type}:{getattr(event, 'tool_name', '')}")
                if event.type == AgentEventType.TOOL_ERROR:
                    failed.append(event.tool_name)
                if event.type == AgentEventType.CALLBACK_REQUESTED:
                    assert event.tool_name == "ask_for_access"
                    assert event.tool_input == {
                        "table": "risk.loan_applications",
                        "reason": "default rates",
                    }
                    await AgentClient(env.client, handle.id).provide_callback_result(
                        event.tool_id, **answer
                    )
                if event.type == AgentEventType.MESSAGE_HANDLER_END:
                    reply = event.output["text"]
                    break
        await handle.terminate()
    assert reply is not None, seen
    return reply, failed


async def test_a_grant_opens_the_table_on_the_board(env: WorkflowEnvironment) -> None:
    reply, failed = await _run_script(env, {"result": {"access": "Grant for this session"}})
    assert failed == []
    assert "result: 'Grant for this session'" in reply
    assert "'risk.loan_applications': 'granted'" in reply
    assert "'risk.credit_scores': 'needs access'" in reply


async def test_a_decline_is_an_answer_not_a_failure(env: WorkflowEnvironment) -> None:
    reply, failed = await _run_script(env, {"result": {"access": "Decline"}})
    assert "result: 'Decline'" in reply
    assert "'risk.loan_applications': 'declined'" in reply
    assert failed == []


async def test_the_consoles_decline_button_declines_too(env: WorkflowEnvironment) -> None:
    # The console's own Decline button answers the callback with an error.
    reply, failed = await _run_script(env, {"error": "Declined by the user."})
    assert "result: 'Decline'" in reply
    assert "'risk.loan_applications': 'declined'" in reply
    assert failed == ["ask_for_access"]
