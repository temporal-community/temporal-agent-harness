# ABOUTME: Proves against a REAL Temporal server that a subagent outlives its parent's
# continue-as-new and is still driveable afterwards — not merely un-terminated, but able to
# receive a message and answer it with everything it was told before the boundary.
#
# tests/harness/test_continue_as_new.py cannot prove this. The time-skipping test server does
# not enforce parent-close policies at all, so a survival assertion passes there under either
# policy; what that suite asserts instead is the policy the parent ASKS the server for. Honouring
# the request is Temporal's contract, and this is where that contract is actually exercised.
#
# Not a pytest test, on purpose. It needs a server nobody's CI has, and a check that fails for
# want of infrastructure teaches people to ignore failures. Named check_* rather than test_* so
# pytest's default collection cannot pick it up even though it sits inside ``testpaths``, which
# is the same reason ui/scripts/check-*.mjs are scripts rather than a test suite.
#
# It creates two workflows on a task queue of its own, both prefixed and both closed on the way
# out, and it talks to nothing that was already running. The only thing it borrows from the
# server is the namespace.
#
# Run with:  just check-rollover-subagent
#      or:   uv run python tests/harness/check_subagent_survives_rollover.py
#            [--address 127.0.0.1:7433] [--namespace agent-harness]

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

from temporalio import workflow
from temporalio.api.enums.v1 import WorkflowExecutionStatus
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    AgentStatus,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.subagent_activities import SubagentActivities

# History length past which the patched suggestion starts saying yes. Any real session is past
# this by the end of its first turn.
_SUGGEST_AFTER = 5

_PARENT_TYPE = "RolloverSurvivalParent"
_CHILD_TYPE = "RolloverSurvivalChild"


@workflow.defn(name=_CHILD_TYPE)
@agent.defn
class _SurvivalChild:
    """The subagent whose life is the question. Remembers what it has been told, so a reply
    after the boundary distinguishes "still running" from "still the same conversation".

    Deliberately hookless: a child that rolled over too would be a second moving part in a check
    about the first one, and a one-shot subagent has nothing to carry anyway.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._heard: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Append this message to the conversation and read the whole thing back."""
        self._heard.append(message.text)
        return TextReply(text=" ".join(self._heard))


@workflow.defn(name=_PARENT_TYPE)
@agent.defn
class _SurvivalParent:
    """Starts one subagent on the first turn and drives the same one on every turn after.

    The handle is snapshotted because the reply to turn two has to come from the child started
    before the rollover. A parent that started a fresh child would answer correctly and prove
    nothing.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._handle: str | None = None

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Drive the subagent one turn, starting it first if this is the first turn."""
        if self._handle is None:
            self._handle = await self._runner.start_subagent(
                "survivor", _CHILD_TYPE, workflow.info().task_queue
            )
        reply = await self._runner.run_subagent_turn(
            self._handle, "ask", {"text": message.text}
        )
        return TextReply(text=str(reply["text"]))

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        return {"handle": self._handle}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._handle = state["handle"]


def _suggest_from_history_length(rolling: dict[str, bool]):
    """Make the rollover suggestion a function of history length rather than server config.

    The real flag is a server setting, and turning it down on a shared dev server means editing
    dynamic config and restarting — which would take the stack out from under whoever else is
    using it. Reading ``get_current_history_length()`` instead is the same quantity the flag
    reports, decided inside the workflow, and replay-consistent because history length is itself
    replayed. Everything this check is actually about — whether the server honours ABANDON, and
    whether a real successor run can reach a real child — is untouched by it.

    ``rolling`` is the off switch: once the one rollover under test has happened, the successor
    must stay put long enough to be driven and observed.
    """

    def is_continue_as_new_suggested(self) -> bool:
        return rolling["yes"] and self.get_current_history_length() >= _SUGGEST_AFTER

    return is_continue_as_new_suggested


async def _say(client: Client, workflow_id: str, text: str, turn: int) -> str:
    """Send one message to the parent and return the reply it got back from its subagent.

    Addressed by workflow id with no run pinned, which is how every real client reaches a
    session and therefore the only way a rollover can be shown to be invisible to one.
    """
    handle = client.get_workflow_handle(workflow_id)
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": text}, expected_turn=turn),
        result_type=AgentMessageReply,
    )
    async with asyncio.timeout(60):
        while True:
            status = await client.get_workflow_handle(workflow_id).query(
                AGENT_STATUS_QUERY, result_type=AgentStatus
            )
            if status.current_turn >= turn and not status.turn_active:
                break
            await asyncio.sleep(0.1)
    return await _last_reply(client, workflow_id, turn)


async def _last_reply(client: Client, workflow_id: str, turn: int) -> str:
    """The text the parent published as its reply to ``turn``, read off its event stream."""
    from contextlib import aclosing
    from datetime import timedelta

    from temporalio.contrib.workflow_streams import WorkflowStreamClient

    from temporal_agent_harness.harness.agent_protocol import (
        TURN_EVENTS_TOPIC,
        AgentEvent,
        AgentEventType,
    )

    events = WorkflowStreamClient.create(client, workflow_id).subscribe(
        TURN_EVENTS_TOPIC,
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=50),
    )
    async with asyncio.timeout(60), aclosing(events):
        async for item in events:
            if (
                item.data.turn_number == turn
                and item.data.event.type == AgentEventType.REPLY
            ):
                return str(item.data.event.output["text"])
    raise AssertionError(f"parent turn {turn} published no reply")


async def _run_id(client: Client, workflow_id: str) -> str:
    return str((await client.get_workflow_handle(workflow_id).describe()).run_id)


async def _status_of(client: Client, workflow_id: str) -> WorkflowExecutionStatus.ValueType:
    return (await client.get_workflow_handle(workflow_id).describe()).status


async def _await_rollover(client: Client, workflow_id: str, original: str) -> str:
    async with asyncio.timeout(60):
        while True:
            current = await _run_id(client, workflow_id)
            if current != original:
                return current
            await asyncio.sleep(0.1)


async def _await_closed(client: Client, workflow_id: str) -> None:
    async with asyncio.timeout(60):
        while await _status_of(client, workflow_id) == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING:
            await asyncio.sleep(0.1)


async def check(address: str, namespace: str) -> None:
    rolling = {"yes": True}
    original_suggestion = workflow.Info.is_continue_as_new_suggested
    workflow.Info.is_continue_as_new_suggested = _suggest_from_history_length(rolling)  # type: ignore[method-assign]

    client = await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )
    task_queue = f"rollover-survival-check-{uuid.uuid4()}"
    parent_id = f"rollover-survival-parent-{uuid.uuid4()}"
    child_id: str | None = None

    try:
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[_SurvivalParent, _SurvivalChild],
            activities=[SubagentActivities(client).run_subagent_turn],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            started = await client.start_workflow(
                _PARENT_TYPE, AgentConfig(), id=parent_id, task_queue=task_queue
            )
            first_run = str(started.result_run_id)
            print(f"  parent   {parent_id}\n  run      {first_run}")

            assert await _say(client, parent_id, "apples", 1) == "apples"
            before = await client.get_workflow_handle(parent_id).query(
                AGENT_STATUS_QUERY, result_type=AgentStatus
            )
            assert len(before.subagents) == 1, before.subagents
            child_id = before.subagents[0].workflow_id
            child_handle = before.subagents[0].subagent_id
            print(f"  child    {child_id}\n  handle   {child_handle}")

            successor = await _await_rollover(client, parent_id, first_run)
            rolling["yes"] = False
            print(f"  rolled   {first_run[:8]} -> {successor[:8]}")

            # 1. The server did not kill it on the parent's close. This is the assertion the
            #    time-skipping server cannot make, and on its own it is not enough.
            assert (
                await _status_of(client, child_id)
                == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
            ), "the subagent did not survive its parent's continue-as-new"

            # 2. And it is not a zombie. The successor re-adopted it by workflow id, reached it,
            #    and it answered with what it was told BEFORE the boundary — so the child is
            #    alive, addressable, and still the same conversation.
            second = await _say(client, parent_id, "bananas", 2)
            assert second == "apples bananas", second

            after = await client.get_workflow_handle(parent_id).query(
                AGENT_STATUS_QUERY, result_type=AgentStatus
            )
            assert [s.workflow_id for s in after.subagents] == [child_id]
            assert [s.subagent_id for s in after.subagents] == [child_handle]
            assert after.current_turn == 2

            # 3. And the successor still owes it a shutdown: closing the session closes the
            #    child too, which is the promise ABANDON transferred from the server to us.
            await client.get_workflow_handle(parent_id).signal("close")
            await _await_closed(client, parent_id)
            await _await_closed(client, child_id)
            print("  closed   parent and child, no orphan left behind")
    finally:
        workflow.Info.is_continue_as_new_suggested = original_suggestion  # type: ignore[method-assign]
        await _terminate_if_running(client, parent_id)
        if child_id is not None:
            await _terminate_if_running(client, child_id)


async def _terminate_if_running(client: Client, workflow_id: str) -> None:
    """Last-resort cleanup for the workflows this check created, and only those."""
    try:
        if await _status_of(client, workflow_id) == (
            WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
        ):
            await client.get_workflow_handle(workflow_id).terminate(
                "rollover survival check aborted"
            )
            print(f"  cleaned  terminated leftover {workflow_id}")
    except Exception as e:  # noqa: BLE001 — cleanup must not mask the failure that caused it
        print(f"  WARNING  could not clean up {workflow_id}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1:7433")
    parser.add_argument("--namespace", default="agent-harness")
    args = parser.parse_args()

    print(f"--- subagent survives its parent's rollover ({args.address}/{args.namespace})")
    try:
        asyncio.run(check(args.address, args.namespace))
    except AssertionError as e:
        print(f"  FAIL     {e}")
        return 1
    print("  PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
