# ABOUTME: The hello_gemini_enterprise PoC's empirical answer, as a runnable test: drives ONE real
# chat turn (with a tool call) through the example workflow against whichever Gemini backend the
# environment selects, so the consumer Gemini Developer API and Google's Gemini Enterprise Agent
# Platform (GEAP, formerly Vertex AI) can be A/B'd with identical workflow code. The workflow is
# unchanged between runs; only the worker's genai.Client differs, which is the finding under test.
#
# Skips (never fails) when the selected backend's credentials are absent, so it is safe in CI.
#
# Consumer Gemini Developer API:
#     GEMINI_API_KEY=... uv run pytest tests/examples/hello_gemini_enterprise -v -s
#
# GEAP / Agent Platform (needs ADC + the Agent Platform API enabled on the project):
#     GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=<project> \
#       uv run pytest tests/examples/hello_gemini_enterprise -v -s
#
# EXPECTED RESULT ON GEAP TODAY (measured 2026-08-10): FAILURE, in the model activity, with
#     400 'Unsupported model interaction: <model>'
# GEAP hosts the Interactions API but does not serve model interactions, and a custom Agent can't
# be built over a Gemini model (`base_agent` accepts only 'antigravity-preview-05-2026'). That
# failure is the finding, not a regression — see examples/hello_gemini_enterprise/README.md. The
# test is left un-xfailed on purpose: when Google ships model interactions on GEAP, this goes green
# on its own and tells us so.
#
# This hits a real model and costs real tokens — one short turn per test.

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin._gemini_activity import (
    GeminiApiCaller,
)
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)

from examples.hello_gemini_enterprise.worker import (
    _backend_description,
    _gemini_client,
    _use_geap,
)
from examples.hello_gemini_enterprise.workflow import HelloGeminiEnterpriseAgentWorkflow
from examples.hello_gemini_enterprise.workflow_generate_content import (
    HelloGeminiEnterpriseGenerateContentWorkflow,
)


def _skip_reason() -> str | None:
    """Why this run can't happen, or None if the selected backend is configured."""
    if _use_geap():
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return "GOOGLE_GENAI_USE_VERTEXAI is set but GOOGLE_CLOUD_PROJECT is not"
        return None
    if not os.environ.get("GEMINI_API_KEY"):
        return (
            "no GEMINI_API_KEY (set it for the consumer Gemini API, or set "
            "GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT for GEAP)"
        )
    return None


@pytest_asyncio.fixture
async def client_and_queue():
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)

    # The one real client — exactly what the worker builds, so the test exercises the
    # production construction path rather than a parallel one.
    gemini = _gemini_client()
    print(f"\n[hello_gemini_enterprise] backend: {_backend_description()}")

    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"hello-geap-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[
            HelloGeminiEnterpriseAgentWorkflow,
            HelloGeminiEnterpriseGenerateContentWorkflow,
        ],
        # No tool activities (get_weather is inline); just the Gemini plugin's activities,
        # which is where the real client and the backend decision live.
        activities=list(GeminiApiCaller(gemini).activities()),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _turn_events(client: Client, workflow_id: str) -> list[AgentEvent]:
    """Collect one turn's events, up to and including turn_end."""
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


async def _assert_weather_turn(client, task_queue, workflow_cls, label: str) -> None:
    """Drive one real weather turn through ``workflow_cls`` and assert the harness contract.

    Shared by both surfaces so the two are held to the IDENTICAL bar — that is what makes the
    Interactions-vs-generate_content comparison meaningful rather than anecdotal.

    Asserting on the TOOL CALL, not just on prose, is deliberate: a backend that merely returns
    text would pass a text-only assertion while silently failing at function calling, which is the
    thing the real agent depends on.
    """
    handle = await client.start_workflow(
        workflow_cls.run,
        AgentConfig(),
        id=f"{workflow_cls.__name__}-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="ask",
            payload={"text": "What's the weather in Paris?"},
            expected_turn=1,
        ),
        result_type=AgentMessageReply,
    )

    events = await _turn_events(client, handle.id)
    print(f"[{label}] turn events: {[str(e.event.type) for e in events]}")

    errors = [e.event for e in events if e.event.type == AgentEventType.ERROR]
    assert not errors, f"turn failed: {errors}"

    # The model asked for the tool, and the harness ran it — proving run_tool still owns the
    # approval gate and lifecycle events on this surface.
    tool_starts = [e.event for e in events if e.event.type == AgentEventType.TOOL_START]
    assert [t.tool_name for t in tool_starts] == ["get_weather"], (
        f"expected exactly one get_weather call, got {[t.tool_name for t in tool_starts]}"
    )
    assert any(e.event.type == AgentEventType.TOOL_END for e in events)

    # And it used the result in its answer.
    replies = [e.event for e in events if e.event.type == AgentEventType.REPLY]
    assert len(replies) == 1
    text = replies[0].output.get("text") or ""
    print(f"[{label}] reply: {text!r}")
    assert "72" in text, f"reply did not use the tool result: {text!r}"


async def test_interactions_surface(client_and_queue):
    """The Interactions API agent. Passes on the consumer API; EXPECTED TO FAIL on GEAP (see
    the header) with 400 'Unsupported model interaction'."""
    client, task_queue = client_and_queue
    await _assert_weather_turn(
        client, task_queue, HelloGeminiEnterpriseAgentWorkflow, "interactions"
    )


async def test_generate_content_surface(client_and_queue):
    """The ``models.generate_content`` agent — the port, and the surface that works on GEAP.

    This is the load-bearing test of the migration question: same tool, same persona, same
    harness tool lifecycle, a surface GEAP actually serves. If this passes on GEAP, moving the
    real agent off Interactions is a mechanical port rather than an open question.
    """
    client, task_queue = client_and_queue
    await _assert_weather_turn(
        client,
        task_queue,
        HelloGeminiEnterpriseGenerateContentWorkflow,
        "generate_content",
    )
