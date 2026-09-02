# ABOUTME: The seam between "catching you up" and "showing you what's happening now". attach stamps
# replay on every event that was already durable when the stream opened, which a consumer uses to
# batch its commits during history and stop batching once it is following. Getting this wrong in the
# permissive direction (over-claiming replay) is survivable; the other way flashes the canvas.

from __future__ import annotations

from unittest.mock import patch

import pytest

import temporal_agent_harness.harness.agent_client as agent_client_mod
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentEvent, AgentReply
from temporal_agent_harness.harness.stream_merge import StreamPosition

pytestmark = pytest.mark.asyncio


def _event(agent_id: str, offset: int) -> AgentEvent:
    return AgentEvent(
        agent_id=agent_id,
        turn_id="t1",
        turn_number=1,
        timestamp=0.0,
        event=AgentReply(text=f"e{offset}"),
    )


async def _stamps(scripted: list[StreamPosition], *, head: int) -> list[bool]:
    """Drive _merged_attach over scripted positions and collect the replay flag it stamps."""

    async def fake_merge(**_kwargs):
        for position in scripted:
            yield (_event("root", position.event_offset), position)

    client = AgentClient(temporal=None, workflow_id="wf-root")
    with patch.object(agent_client_mod, "merge_stream", fake_merge):
        stream = client._merged_attach(
            on_item=lambda _item, position: position.replay,
            from_offset=0,
            stop_at_root_offset=head,
            stall_grace_seconds=1.0,
        )
        return [flag async for flag in stream]


async def test_events_already_durable_are_replay_and_later_ones_are_not():
    # The seam is the head as of attach: at or below it the event was already in the log.
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 7)]
    assert await _stamps(scripted, head=3) == [True, True, True, False, False, False]


async def test_nothing_is_replay_when_the_stream_opened_empty():
    # head 0 means the log was empty, so every event arriving is live. This is the case that a
    # stamp hardcoded to True would pass and this asserts against.
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 4)]
    assert await _stamps(scripted, head=0) == [False, False, False]


async def test_everything_is_replay_when_attaching_to_a_finished_run():
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 4)]
    assert await _stamps(scripted, head=99) == [True, True, True]


async def test_a_subagents_events_inherit_its_dispatch_position():
    # A child's resume_offset stands still for its turn, so every event of a turn dispatched at or
    # before the seam is stamped replay even if the child published it live. Deliberate: it errs
    # toward "still catching up", and a consumer must survive a long catch-up anyway.
    scripted = [
        StreamPosition(resume_offset=3, event_offset=0),
        StreamPosition(resume_offset=3, event_offset=1),
        StreamPosition(resume_offset=3, event_offset=2),
    ]
    assert await _stamps(scripted, head=3) == [True, True, True]
