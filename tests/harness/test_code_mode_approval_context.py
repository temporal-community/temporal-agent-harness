# ABOUTME: An auto mode evaluator learns that a gated call is a Code Mode script. Runs a real
# Code Mode tool through the approval gate with a recording evaluator, and checks that the script
# call carries a CodeModeCall (its source and host functions) and the host call it makes doesn't.

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.code_mode import code_mode_host_functions
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AutoApprovalContext,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalDecision,
    AutoApprovalVerdict,
    CodeModeCall,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

pytest.importorskip("pydantic_monty")

SCRIPT = """\
import asyncio
async def main():
    return await lookup_order("A-1")
asyncio.run(main())
"""


@agent.tool_defn()
async def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order:{order_id}"


@agent.tool_defn()
async def cancel_order(order_id: str) -> str:
    """Cancel an order."""
    return f"cancelled:{order_id}"


_SEEN: list[AutoApprovalContext] = []


async def _record_and_approve(ctx: AutoApprovalContext) -> AutoApprovalDecision:
    _SEEN.append(ctx)
    return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)


@agent.defn
class CodeModeContextProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=AutoApprovalCriteria(
                sets={"all": AutoApprovalCriteriaSet(effect="Anything.")}, default="all"
            ),
            auto_mode_evaluator=_record_and_approve,
        )
        self._code_tool = agent.code_mode_tool([lookup_order, cancel_order], name="run_orders")

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run the script in the message."""
        result = await self._runner.run_tool("c1", self._code_tool, script=message.text)
        return TextReply(text=result)


async def test_code_mode_call_reaches_the_evaluator_with_its_script_and_host_functions():
    _SEEN.clear()
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"code-mode-ctx-{uuid.uuid4()}"
    try:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CodeModeContextProbeAgent],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                CodeModeContextProbeAgent.run,
                AgentConfig(),
                id=f"code-mode-ctx-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(type="act", payload={"text": SCRIPT}),
                result_type=AgentMessageReply,
            )
            stream = WorkflowStreamClient.create(env.client, handle.id)
            async with asyncio.timeout(30):
                async for item in stream.subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    if item.data.event.type == AgentEventType.TURN_END:
                        break
    finally:
        await env.shutdown()

    by_tool = {ctx.tool_name: ctx for ctx in _SEEN}
    assert set(by_tool) == {"run_orders", "lookup_order"}
    assert by_tool["run_orders"].code_mode == CodeModeCall(
        script=SCRIPT, host_functions=frozenset({"lookup_order", "cancel_order"})
    )
    assert by_tool["lookup_order"].code_mode is None


def test_code_mode_host_functions_reads_the_tool() -> None:
    tool = agent.code_mode_tool([lookup_order, cancel_order], name="run_orders")
    assert code_mode_host_functions(tool) == {"lookup_order", "cancel_order"}
    with pytest.raises(TypeError):
        code_mode_host_functions(lookup_order)
