# ABOUTME: Translation between our internal Task/Message/Part (subagents.transport) and real
# A2A wire shapes (a2a_types.py). Bridges the gap where our contract dispatches to a named
# @agent.accepts handler but a real A2A caller sends a generic, handler-less Message.

from __future__ import annotations

import json

from temporal_agent_harness.harness.agent_protocol import AgentEvent, AgentEventType

from subagents.transport import Artifact as InternalArtifact
from subagents.transport import Message as InternalMessage
from subagents.transport import Part as InternalPart
from subagents.transport import Task as InternalTask

from . import a2a_types as a2a


def _internal_part_to_a2a(part: InternalPart) -> a2a.Part:
    if part.kind == "text":
        return a2a.TextPart(text=part.text or "")
    return a2a.DataPart(data=json.loads(part.data) if part.data else {})


def _internal_artifact_to_a2a(artifact: InternalArtifact, *, index: int) -> a2a.Artifact:
    return a2a.Artifact(
        artifact_id=f"artifact-{index}",
        name=artifact.name,
        parts=[_internal_part_to_a2a(p) for p in artifact.parts],
    )


def internal_task_to_a2a(task: InternalTask) -> a2a.Task:
    """Render our internal Task as the A2A wire shape.

    ``status.message`` is dropped: internally it's just a correlation stub (a message_id, no
    real content), and surfacing it as if it were an A2A prompt would be misleading."""
    return a2a.Task(
        id=task.id,
        context_id=task.context_id,
        status=a2a.TaskStatus(state=task.status.state, message=None),  # type: ignore[arg-type]
        artifacts=[
            _internal_artifact_to_a2a(a, index=i) for i, a in enumerate(task.artifacts)
        ],
    )


def incoming_message_to_internal(
    message: a2a.Message, *, task_id: str, default_handler: str
) -> InternalMessage:
    """Translate an external A2A Message into our {handler, input} convention.

    A DataPart's dict is used verbatim as input (only the first one). Otherwise, TextParts join
    with newlines into {"text": ...} — fine for a single-text-field handler, wrong otherwise (use
    a DataPart instead). ``default_handler`` is always the target: A2A has no handler-selection
    concept, so one gateway instance fronts exactly one handler."""
    data_parts = [p for p in message.parts if isinstance(p, a2a.DataPart)]
    if data_parts:
        payload = data_parts[0].data
    else:
        text = "\n".join(p.text for p in message.parts if isinstance(p, a2a.TextPart))
        payload = {"text": text}

    return InternalMessage(
        role="user",
        parts=[InternalPart(kind="data", data=json.dumps({"handler": default_handler, "input": payload}))],
        task_id=task_id,
    )


def agent_event_to_a2a_stream_event(
    event: AgentEvent, *, task_id: str, context_id: str
) -> a2a.TaskStatusUpdateEvent | a2a.TaskArtifactUpdateEvent | None:
    """Translate one decoded AgentEvent into an A2A streaming event, or None if it has no
    A2A-meaningful counterpart. Deliberately coarse — only turn lifecycle + final reply cross
    this boundary; a Temporal-native caller wanting full fidelity uses NexusTransport instead."""
    et = event.event.type
    if et == AgentEventType.TURN_STARTED:
        return a2a.TaskStatusUpdateEvent(
            task_id=task_id, context_id=context_id, status=a2a.TaskStatus(state="working")
        )
    if et == AgentEventType.REPLY:
        output = getattr(event.event, "output", {}) or {}
        return a2a.TaskArtifactUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            artifact=a2a.Artifact(
                artifact_id="reply",
                parts=[a2a.DataPart(data=output)],
            ),
        )
    if et == AgentEventType.TURN_END:
        return a2a.TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=a2a.TaskStatus(state="completed"),
            final=True,
        )
    if et == AgentEventType.ERROR:
        return a2a.TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=a2a.TaskStatus(state="failed"),
            final=True,
        )
    return None
