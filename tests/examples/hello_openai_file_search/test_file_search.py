# ABOUTME: Proves OpenAI's HOSTED file_search works end-to-end through the harness — the decision
# test for migrating the main (doc-QA) agent to OpenAI instead of GEAP, since GEAP refuses the
# Gemini Interactions API's built-in file_search outright. Drives one real turn that asks for a fact
# stated ONLY in the ingested document; if the reply contains it, retrieval demonstrably happened,
# so the proof needs no instrumentation of OpenAI's server-side tool span.
#
# Also asserts the harness contract on this path: the model-interaction bracket and reply deltas are
# present (unlike the Gemini generate_content path, which publishes neither), and a harness-owned
# tool in the SAME turn still emits its full tool lifecycle while the hosted tool does not.
#
# Skips (never fails) without OPENAI_API_KEY, so it is CI-safe.
#
#     OPENAI_API_KEY=... uv run pytest tests/examples/hello_openai_file_search -v -s
#
# This hits a real model and creates/reuses a real vector store — costs a small amount of money.
# The store is idempotent by name (corpus.VECTOR_STORE_NAME), so repeat runs reuse one store.

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from openai import OpenAI
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
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

from examples.hello_openai_file_search.corpus import ZARNAK_ANSWER, ensure_vector_store
from examples.hello_openai_file_search.workflow import (
    HelloOpenAIFileSearchAgentWorkflow,
)


@pytest_asyncio.fixture
async def client_and_queue(monkeypatch: pytest.MonkeyPatch):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("no OPENAI_API_KEY")

    # Same resolution the worker does — reuse by name so runs don't litter the account.
    store_id = os.environ.get("OPENAI_VECTOR_STORE_ID") or ensure_vector_store(OpenAI())
    print(f"\n[file_search] vector store: {store_id}")
    # Exactly what the worker does at startup. It must be the ENV VAR, not the module global:
    # the workflow sandbox re-imports the workflow module, so a patched host-module global is
    # invisible to the running workflow (this test caught precisely that bug).
    monkeypatch.setenv("OPENAI_VECTOR_STORE_ID", store_id)

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(stream_to_provider=stream_to_provider),
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(plugins=[plugin])
    task_queue = f"hello-openai-fs-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HelloOpenAIFileSearchAgentWorkflow],
        activities=[],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _turn_events(client: Client, workflow_id: str) -> list[AgentEvent]:
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


async def _ask(client: Client, task_queue: str, text: str) -> list[AgentEvent]:
    handle = await client.start_workflow(
        HelloOpenAIFileSearchAgentWorkflow.run,
        AgentConfig(),
        id=f"HelloOpenAIFileSearchAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": text}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    events = await _turn_events(client, handle.id)
    errors = [e.event for e in events if e.event.type == AgentEventType.ERROR]
    assert not errors, f"turn failed: {errors}"
    return events


def _reply_text(events: list[AgentEvent]) -> str:
    replies = [e.event for e in events if e.event.type == AgentEventType.REPLY]
    assert len(replies) == 1, f"expected one reply, got {len(replies)}"
    return replies[0].output.get("text") or ""


async def test_hosted_file_search_retrieves_from_the_corpus(client_and_queue):
    """The load-bearing test: answer a question only the ingested document can answer.

    ``ZARNAK_ANSWER`` appears nowhere but the corpus, so its presence in the reply IS the proof
    that OpenAI's hosted retrieval ran and fed the model — no tool-span instrumentation needed,
    and no way for a plausible-sounding guess to pass.
    """
    client, task_queue = client_and_queue
    events = await _ask(
        client, task_queue, "What is the Zarnak coefficient for the 2026-Q3 reference build?"
    )
    text = _reply_text(events)
    print(f"[file_search] reply: {text!r}")
    assert ZARNAK_ANSWER in text, (
        f"reply did not contain the corpus-only figure {ZARNAK_ANSWER!r} — hosted retrieval "
        f"did not feed the model: {text!r}"
    )


async def test_harness_observability_and_tool_lifecycle_on_this_path(client_and_queue):
    """One turn, both tool kinds — documents exactly what the harness sees on the OpenAI path.

    The contrast is the point:
    * the model-interaction bracket and reply deltas ARE published here (the Gemini
      ``generate_content`` path publishes neither — see examples/hello_gemini_enterprise);
    * the HARNESS-owned ``get_weather`` emits its full ``tool_start``/``tool_end`` lifecycle;
    * the HOSTED ``file_search`` emits no tool events at all, because it runs server-side and
      never passes through ``run_tool`` (harness spec §11 defers hosted tool spans). So hosted
      retrieval is not approval-gateable — a real constraint, asserted rather than assumed.
    """
    client, task_queue = client_and_queue
    events = await _ask(client, task_queue, "What's the weather in Paris?")
    kinds = [e.event.type for e in events]
    print(f"[file_search] turn events: {[str(k) for k in kinds]}")

    # Model-invocation bracket + streamed reply: present on the OpenAI path.
    assert AgentEventType.MODEL_INTERACTION_STARTED in kinds
    assert AgentEventType.MODEL_INTERACTION_ENDED in kinds
    assert AgentEventType.REPLY_DELTA in kinds

    # Token accounting survives too — the thing a migration would hate to lose.
    ended = [
        e.event for e in events if e.event.type == AgentEventType.MODEL_INTERACTION_ENDED
    ]
    assert any(e.usage is not None for e in ended), "no token usage on any ended bracket"

    # The harness-owned tool ran with full lifecycle; only it appears in tool events.
    tool_names = {
        e.event.tool_name for e in events if e.event.type == AgentEventType.TOOL_START
    }
    assert tool_names == {"get_weather"}, (
        f"expected only the harness tool in tool_start events, got {tool_names}"
    )
    assert "72" in _reply_text(events)
