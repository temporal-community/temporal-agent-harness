# ABOUTME: Tests for evals.run_script — replaying a scripted conversation against a freshly
# started agent session, and what happens when a turn (or the infrastructure) fails.
#
# Uses a model-free probe agent so the whole thing is deterministic and runs in CI: the point
# under test is the runner's session lifecycle and failure handling, not any model's behaviour.
#
# Run with: uv run pytest tests/evals/test_run_script.py -v

from __future__ import annotations

import uuid


import pytest_asyncio
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.evals import TurnScript, TurnStep, run_script
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

TASK_QUEUE_PREFIX = "eval-run-script"
WORKFLOW_TYPE = "ScriptProbeAgent"


@workflow.defn(name=WORKFLOW_TYPE)
@agent.defn
class ScriptProbeAgent:
    """Remembers what it has been told, so a multi-turn script can be shown to carry state."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._heard: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo back everything heard so far, or explode on demand."""
        if message.text == "boom":
            raise RuntimeError("handler exploded")
        self._heard.append(message.text)
        return TextReply(text="|".join(self._heard))


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"{TASK_QUEUE_PREFIX}-{uuid.uuid4()}"
    # NOTE the nesting: the worker must be shut down BEFORE the test server. The fixture
    # copy-pasted across this repo puts ``await env.shutdown()`` in a finally *inside* the
    # ``async with Worker(...)``, which kills the server first and leaves the worker retrying
    # against a dead connection — ~10s of dead time per test here (it goes unnoticed in suites
    # whose workflows never complete). Worker first, then env.
    try:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ScriptProbeAgent],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            yield env.client, task_queue
    finally:
        await env.shutdown()


def _script(*texts: str, task_queue: str) -> TurnScript:
    return TurnScript(
        steps=[TurnStep.text(t) for t in texts],
        workflow_type=WORKFLOW_TYPE,
        task_queue=task_queue,
    )


async def test_runs_every_step_against_one_session(client_and_queue):
    client, task_queue = client_and_queue

    result = await run_script(client, _script("one", "two", "three", task_queue=task_queue))

    assert result.ok
    assert len(result.turns) == 3
    # One session across all three steps — the agent accumulated state, which is the whole
    # reason a case is a conversation rather than three independent prompts.
    assert result.final_text == "one|two|three"
    assert [t.turn_number for t in result.turns] == [1, 2, 3]


async def test_each_case_gets_a_fresh_session(client_and_queue):
    client, task_queue = client_and_queue

    first = await run_script(client, _script("a", task_queue=task_queue))
    second = await run_script(client, _script("b", task_queue=task_queue))

    # Distinct workflows, and no bleed-through: cases must not contaminate each other.
    assert first.session_workflow_id != second.session_workflow_id
    assert second.final_text == "b"


async def test_a_failing_turn_stops_the_script_without_raising(client_and_queue):
    client, task_queue = client_and_queue

    result = await run_script(
        client, _script("one", "boom", "three", task_queue=task_queue)
    )

    # A failed case is a result to be scored, not an exception to be handled...
    assert not result.ok
    # ...and the remaining steps are skipped, because the rest of a conversation is
    # meaningless once a turn has gone wrong.
    assert len(result.turns) == 2
    assert "handler exploded" in result.turns[-1].error


async def test_session_is_closed_when_the_script_finishes(client_and_queue):
    client, task_queue = client_and_queue

    result = await run_script(client, _script("one", task_queue=task_queue))

    # An agent session parks forever awaiting the next message, so a dataset run that did not
    # close them would leave one live workflow per case.
    handle = client.get_workflow_handle(result.session_workflow_id)
    await handle.result()


async def test_close_can_be_disabled_for_inspection(client_and_queue):
    client, task_queue = client_and_queue

    result = await run_script(
        client, _script("one", task_queue=task_queue), close_session=False
    )

    status = await client.get_workflow_handle(
        result.session_workflow_id
    ).describe()
    assert status.status.name == "RUNNING"
    await client.get_workflow_handle(result.session_workflow_id).signal("close")


async def test_labels_reach_the_session_and_every_turn(client_and_queue):
    client, task_queue = client_and_queue
    script = _script("one", "two", task_queue=task_queue)

    result = await run_script(
        client, script, labels={"dataset_item_id": "case-1", "run": "r1"}
    )

    for index, turn in enumerate(result.turns):
        assert turn.labels["dataset_item_id"] == "case-1"
        assert turn.labels["run"] == "r1"
        # The runner adds the step index, so a turn's trace says where in the conversation
        # it sat without the reader having to reconstruct it.
        assert turn.labels["step_index"] == str(index)


async def test_usage_is_summed_across_the_whole_script(client_and_queue):
    client, task_queue = client_and_queue

    result = await run_script(client, _script("one", "two", task_queue=task_queue))

    # No model in this probe, so nothing is reported — and the sum must stay None rather than
    # inventing a confident zero.
    assert result.usage.total_tokens is None


async def test_infrastructure_failure_is_recorded_not_raised(client_and_queue):
    client, task_queue = client_and_queue
    script = TurnScript(
        steps=[TurnStep(payload={"text": "hi"}, function="no_such_handler")],
        workflow_type=WORKFLOW_TYPE,
        task_queue=task_queue,
    )

    result = await run_script(client, script)

    # An unknown handler is rejected at the update boundary — one broken case must not take
    # down the whole dataset run.
    assert not result.ok
    assert result.error is not None
    assert result.turns == []
