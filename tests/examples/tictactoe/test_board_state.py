# ABOUTME: End-to-end test that the tic-tac-toe board, declared as `board = agent.state(Board)`,
# reaches a consumer: one snapshot at construction under the attribute's name, and a
# `new_game` that publishes one patch replaying into a fresh board. `new_game` with the human
# opening makes no TypeSafe call, so this runs without an API key, on the real sandbox.
#
# Run with: uv run pytest tests/examples/tictactoe/test_board_state.py -v

from __future__ import annotations

import uuid

import jsonpatch
import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.tictactoe import activities
from examples.tictactoe.workflow import TicTacToeAgentWorkflow


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"tictactoe-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TicTacToeAgentWorkflow],
        plugins=[AgentHarnessPlugin(tools=activities.ALL_TOOLS)],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def test_new_game_streams_the_declared_board(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        TicTacToeAgentWorkflow.run,
        AgentConfig(),
        id=f"TicTacToeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="new_game", payload={}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        events.append(item.data)
        if item.data.event.type == AgentEventType.TURN_END:
            break

    snapshots = [e.event for e in events if e.event.type == AgentEventType.STATE_SNAPSHOT]
    patches = [e.event for e in events if e.event.type == AgentEventType.STATE_PATCH]
    assert [(s.state_id, s.version) for s in snapshots] == [("board", 0)]
    assert [p.version for p in patches] == [1]

    document = jsonpatch.apply_patch(snapshots[0].value, patches[0].ops)
    assert document == {
        "cells": [None] * 9,
        "to_move": "X",
        "agent_mark": "O",
        "status": "playing",
        "moves": [],
    }
