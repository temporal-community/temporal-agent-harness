"""Export a single-agent, single-message harness recording as Harbor ATIF."""

import json
from datetime import datetime, timezone
from typing import Any

from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.observation import Observation
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory


class UnsupportedRecording(ValueError):
    """Evidence cannot be represented faithfully by this prototype exporter."""


def export_atif(record: dict[str, Any]) -> Trajectory:
    steps = [Step(step_id=1, source="user", message=record["instruction"])]
    active: Step | None = None
    calls: dict[str, Step] = {}
    resolved: set[str] = set()
    agent_ids: set[str] = set()
    message_ids: set[str] = set()
    model_steps: list[Step] = []
    reply_seen = False
    turn_ended = False
    terminal_error = False

    def add_step(**kwargs: Any) -> Step:
        step = Step(step_id=len(steps) + 1, source="agent", **kwargs)
        steps.append(step)
        return step

    for index, envelope in enumerate(record["events"]):
        agent_ids.add(envelope["agent_id"])
        if len(agent_ids) > 1:
            raise UnsupportedRecording("Subagent recordings are not supported yet")
        if envelope.get("message_id"):
            message_ids.add(envelope["message_id"])
        if len(message_ids) > 1:
            raise UnsupportedRecording("Only one inbound message is supported per recording")
        event = envelope["event"]
        kind = event["type"]
        stamp = datetime.fromtimestamp(envelope["timestamp"], timezone.utc).isoformat()
        if kind == "subagent_stream_unavailable":
            raise UnsupportedRecording("Subagent evidence is incomplete")
        if kind == "model_interaction_started":
            if active is not None:
                raise UnsupportedRecording("Overlapping model calls cannot be correlated")
            active = add_step(
                timestamp=stamp,
                message="",
                model_name=event.get("model"),
                llm_call_count=1,
                extra={"event_index": index, "message_id": envelope.get("message_id")},
            )
            model_steps.append(active)
        elif kind == "reply_delta":
            if active is None:
                raise UnsupportedRecording("Reply delta has no model-call bracket")
            active.message += event["text"]
        elif kind == "model_interaction_ended":
            if active is None:
                raise UnsupportedRecording("Model end has no matching start")
            usage = event.get("usage")
            if usage is not None:
                active.metrics = Metrics(
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                    extra={k: v for k, v in usage.items() if v is not None},
                )
            active = None
        elif kind in {"tool_requested", "tool_start"}:
            tool_id = event["tool_id"]
            if tool_id not in calls:
                step = active or add_step(
                    timestamp=stamp,
                    message="",
                    llm_call_count=0,
                    extra={"event_index": index},
                )
                step.tool_calls = (step.tool_calls or []) + [
                    ToolCall(
                        tool_call_id=tool_id,
                        function_name=event["tool_name"],
                        arguments=event.get("tool_input", {}),
                    )
                ]
                calls[tool_id] = step
        elif kind in {"tool_end", "tool_error"}:
            tool_id = event["tool_id"]
            if tool_id not in calls or tool_id in resolved:
                raise UnsupportedRecording("Tool result has no unique matching call")
            resolved.add(tool_id)
            step = calls[tool_id]
            output = event.get("tool_output") if kind == "tool_end" else event["message"]
            result = ObservationResult(
                source_call_id=tool_id,
                content=output if isinstance(output, str) else json.dumps(output),
                extra={"error": kind == "tool_error", "event_index": index},
            )
            if step.observation is None:
                step.observation = Observation(results=[])
            step.observation.results.append(result)
        elif kind == "message_handler_end":
            reply_seen = True
            output = event["output"]
            reply = output.get("text", json.dumps(output))
            if model_steps and not model_steps[-1].tool_calls:
                last = model_steps[-1]
                if not last.message:
                    last.message = reply
                elif last.message != reply:
                    add_step(message=reply, llm_call_count=0, extra={"handler_output": output})
            else:
                add_step(message=reply, extra={"handler_output": output})
        elif kind == "message_handler_error":
            terminal_error = True
        elif kind == "turn_end":
            turn_ended = True

    complete = bool(record.get("complete"))
    if complete and (
        active or calls.keys() != resolved or not reply_seen or not turn_ended or terminal_error
    ):
        raise UnsupportedRecording("Recording marked complete has unfinished or failed execution")

    def total(field: str) -> int | None:
        values = [getattr(s.metrics, field) if s.metrics else None for s in model_steps]
        return sum(values) if values and all(v is not None for v in values) else None

    return Trajectory(
        session_id=record["session_id"],
        agent=Agent(name=record["workflow_type"], version=record["harness_version"]),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total("prompt_tokens"),
            total_completion_tokens=total("completion_tokens"),
            total_cached_tokens=total("cached_tokens"),
            total_steps=len(steps),
        ),
        notes="Observed harness interactions; full provider request prompts are not captured.",
        extra={
            "complete": complete,
            "error": record.get("error"),
            "message_id": record.get("message_id"),
            "recording_schema_version": 1,
        },
    )
