# ABOUTME: Tests for AgentClient.run_turn / TurnResult and the pure TurnFold behind it, plus
# the observability labels that flow AgentConfig -> AgentStatus and AgentMessage -> TurnStarted.
#
# run_turn is the batch/eval-facing counterpart to send_message: drive one turn, get the typed
# reply, the summed token usage, and the trace id, without every caller re-implementing the
# same "scan the events for the REPLY" loop. The fold is unit-tested against hand-built events
# (no Temporal), the client against the real time-skipping server.
#
# Run with: uv run pytest tests/harness/test_run_turn.py -v

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness._turn_fold import TurnFold
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    AgentTurnError,
)
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentError,
    AgentEvent,
    AgentReply,
    AgentStreamItem,
    ModelInteractionEnded,
    TextMessage,
    TextReply,
    TokenUsage,
    ToolApprovalPolicy,
    TurnEnded,
    TurnStarted,
)


@workflow.defn
@agent.defn
class RunTurnProbeAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            enable_message_queuing_default=True,
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo, or raise when told to."""
        if message.text == "boom":
            raise RuntimeError("handler exploded")
        return TextReply(text=f"echo:{message.text}")


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"run-turn-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[RunTurnProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(
    client: Client, task_queue: str, config: AgentConfig | None = None
) -> WorkflowHandle:
    return await client.start_workflow(
        RunTurnProbeAgent.run,
        config or AgentConfig(),
        id=f"RunTurnProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


# ---------------------------------------------------------------------------
# run_turn
# ---------------------------------------------------------------------------


async def test_run_turn_returns_the_typed_reply(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    result = await agent_client.run_turn(
        "ask", {"text": "hello"}, output_type=TextReply
    )

    assert result.ok
    assert result.output == {"text": "echo:hello"}
    # The whole point of output_type: the reply comes back as the model, not a dict.
    assert isinstance(result.typed, TextReply)
    assert result.typed.text == "echo:hello"
    assert result.turn_number == 1
    assert result.error is None


async def test_run_turn_derives_expected_turn_across_a_conversation(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    # No expected_turn anywhere: a scripted multi-turn conversation should not force the
    # caller to track turn numbers, which is exactly what a dataset item needs.
    first = await agent_client.run_turn("ask", {"text": "one"})
    second = await agent_client.run_turn("ask", {"text": "two"})

    assert (first.turn_number, second.turn_number) == (1, 2)
    assert second.output == {"text": "echo:two"}


async def test_run_turn_raises_on_a_failed_turn_by_default(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    with pytest.raises(AgentTurnError, match="handler exploded"):
        await agent_client.run_turn("ask", {"text": "boom"})


async def test_run_turn_can_return_a_failure_as_data(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    result = await agent_client.run_turn(
        "ask", {"text": "boom"}, raise_on_error=False
    )

    # A batch runner scoring many cases wants a failed case to be a RESULT, not a crash.
    assert not result.ok
    assert "handler exploded" in result.error
    assert result.output == {}
    assert result.typed is None
    # ...and the session survives it, so the next case can run on the same agent.
    assert (await agent_client.run_turn("ask", {"text": "after"})).ok


async def test_run_turn_can_skip_event_collection(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    kept = await agent_client.run_turn("ask", {"text": "a"})
    dropped = await agent_client.run_turn("ask", {"text": "b"}, collect_events=False)

    assert kept.events
    assert dropped.events == ()
    # The outcome is folded either way — collect_events only controls retention.
    assert dropped.output == {"text": "echo:b"}


async def test_run_turn_carries_labels_onto_the_turn(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(
        client, task_queue, AgentConfig(labels={"experiment": "exp-1", "env": "test"})
    )
    agent_client = AgentClient(client, handle.id)

    result = await agent_client.run_turn(
        "ask", {"text": "hello"}, labels={"dataset_item_id": "item-7", "env": "prod"}
    )

    # Session labels merged with per-turn labels, per-turn winning on a collision.
    assert result.labels == {
        "experiment": "exp-1",
        "env": "prod",
        "dataset_item_id": "item-7",
    }
    # And the session-scoped set is readable in one query, for a late-attaching consumer.
    status = await agent_client.get_status()
    assert status.labels == {"experiment": "exp-1", "env": "test"}


async def test_labels_default_to_empty(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    result = await agent_client.run_turn("ask", {"text": "hello"})

    assert result.labels == {}
    assert (await agent_client.get_status()).labels == {}


# ---------------------------------------------------------------------------
# TurnFold (pure)
# ---------------------------------------------------------------------------


def _ev(turn_id: str, event: AgentStreamItem, turn_number: int = 1) -> AgentEvent:
    return AgentEvent(
        event=event,
        agent_id="a1b2c3",
        turn_id=turn_id,
        turn_number=turn_number,
        timestamp=0.0,
    )


def _usage(**kwargs: Any) -> TokenUsage:
    return TokenUsage(**kwargs)


def test_fold_collects_reply_trace_id_and_labels():
    fold = TurnFold(turn_id="t1")
    assert not fold.feed(
        _ev(
            "t1",
            TurnStarted(
                user_message="hi", otel_trace_id="a" * 32, labels={"env": "test"}
            ),
        )
    )
    assert not fold.feed(_ev("t1", AgentReply(output={"text": "hi back"})))
    assert fold.feed(_ev("t1", TurnEnded())) is True

    assert fold.got_reply
    assert fold.output == {"text": "hi back"}
    assert fold.otel_trace_id == "a" * 32
    assert fold.labels == {"env": "test"}
    assert fold.error is None


def test_fold_ignores_other_turns():
    fold = TurnFold(turn_id="t1")
    # A subagent's events, or the next turn's, ride the same merged stream — the fold must
    # not pick them up, so a caller can pass everything through unfiltered.
    assert fold.feed(_ev("other", AgentReply(output={"text": "not mine"}))) is False
    assert fold.feed(_ev("other", TurnEnded())) is False
    assert not fold.got_reply
    assert fold.output == {}


def test_fold_sums_usage_across_model_calls():
    fold = TurnFold(turn_id="t1")
    fold.feed(
        _ev(
            "t1",
            ModelInteractionEnded(
                model="m", usage=_usage(input_tokens=10, output_tokens=2, total_tokens=12)
            ),
        )
    )
    fold.feed(
        _ev(
            "t1",
            ModelInteractionEnded(
                model="m", usage=_usage(input_tokens=5, output_tokens=3, total_tokens=8)
            ),
        )
    )

    assert fold.model_interactions == 2
    assert fold.usage.input_tokens == 15
    assert fold.usage.output_tokens == 5
    assert fold.usage.total_tokens == 20


def test_fold_keeps_unreported_usage_none_rather_than_zero():
    fold = TurnFold(turn_id="t1")
    fold.feed(
        _ev("t1", ModelInteractionEnded(model="m", usage=_usage(input_tokens=10)))
    )

    assert fold.usage.input_tokens == 10
    # None means "the provider never reported this", which is NOT zero — a confident 0 would
    # silently understate cost for a provider that omits a field.
    assert fold.usage.output_tokens is None
    assert fold.usage.thought_tokens is None


def test_fold_handles_a_model_call_with_no_usage_at_all():
    fold = TurnFold(turn_id="t1")
    fold.feed(_ev("t1", ModelInteractionEnded(model="m", usage=None)))

    assert fold.model_interactions == 1
    assert fold.usage.total_tokens is None


def test_fold_records_a_terminal_error():
    fold = TurnFold(turn_id="t1")
    fold.feed(_ev("t1", AgentError(message="it broke")))
    assert fold.feed(_ev("t1", TurnEnded())) is True

    assert fold.error == "it broke"
    assert not fold.got_reply
