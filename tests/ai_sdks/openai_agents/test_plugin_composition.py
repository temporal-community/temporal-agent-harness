# ABOUTME: That OpenAIAgentsPlugin and Temporal's OpenTelemetryPlugin can sit on the same client
# without breaking each other — the prerequisite for tracing an OpenAI Agents SDK agent.
#
# Both plugins install interceptors and both have opinions about the client's configuration, so
# "do they compose?" is not obvious. This pins the answer, and pins the one gotcha found while
# checking: OpenAIAgentsPlugin REQUIRES its own payload converter, so a caller that passes
# data_converter=pydantic_data_converter (as most harness workers do) gets a ValueError. The
# OpenAI examples must let the plugin supply it.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_plugin_composition.py -v

from __future__ import annotations

import asyncio
import uuid

import opentelemetry.trace
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import workflow
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIAgentsPlugin
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)


@agent.tool_defn(inherently_safe=True)
async def probe_tool(city: str) -> str:
    """Stands in for a real tool; no model is involved in this test."""
    return f"sunny in {city}"


@workflow.defn(name="PluginComposeProbe")
@agent.defn
class PluginComposeProbe:
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
    async def ask(self, message: TextMessage) -> TextReply:
        """Run the probe tool."""
        return TextReply(text=await self._runner.run_tool("t1", probe_tool, message.text))


@pytest.fixture
def span_exporter():
    previous = opentelemetry.trace._TRACER_PROVIDER
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        opentelemetry.trace._TRACER_PROVIDER = previous


async def test_harness_spans_survive_the_openai_plugin(span_exporter):
    """An agent driven by the OpenAI Agents SDK still gets the harness's span tree."""
    # No data_converter override — OpenAIAgentsPlugin supplies its own (see the test below).
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[OpenAIAgentsPlugin(), OpenTelemetryPlugin()],
    )
    task_queue = f"plugin-compose-{uuid.uuid4()}"
    try:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PluginComposeProbe],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                PluginComposeProbe.run,
                AgentConfig(),
                id=f"compose-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(type="ask", payload={"text": "Tokyo"}, expected_turn=1),
            )
            async with asyncio.timeout(30):
                while not [
                    s for s in span_exporter.get_finished_spans() if s.name == "agent.turn"
                ]:
                    await asyncio.sleep(0.02)
    finally:
        await env.shutdown()

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    turn = spans["agent.turn"]
    tool = spans["execute_tool probe_tool"]
    # The harness's spans are emitted by the runner and the tool dispatchers, so they are the
    # same for every SDK. This is the standardization pillar, checked rather than asserted.
    assert tool.parent.span_id == turn.context.span_id
    assert tool.context.trace_id == turn.context.trace_id


async def test_openai_plugin_rejects_a_foreign_payload_converter():
    """Documents the gotcha: the OpenAI plugin owns the payload converter.

    Most harness workers pass ``data_converter=pydantic_data_converter``. Doing that alongside
    this plugin fails at client construction, which is why the OpenAI examples must let the
    plugin supply the converter instead. Pinned as a test so the error stays a clear one rather
    than something a reader rediscovers at demo time.
    """
    with pytest.raises(ValueError, match="OpenAIPayloadConverter"):
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            plugins=[OpenAIAgentsPlugin(), OpenTelemetryPlugin()],
        )
