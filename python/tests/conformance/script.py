# ABOUTME: The script the scripted integration plays in place of a model: one message's steps,
# and the events the integration itself must publish for them. Shared by the agent that plays a
# script and by the driver that checks the integration against it.

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from temporal_agent_harness.harness.agent_protocol.events import (
    AgentEventType,
    AgentStreamItem,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TokenUsage,
    ToolRequested,
)

Handler = Literal["run_reject", "run_enqueue", "run_accept"]
"""The scripted agent's handlers, one per ``MidTurn`` mode."""

ToolName = Literal["add", "echo", "fail", "gated", "ask_client"]

INTEGRATION_EVENT_TYPES = frozenset(
    {
        AgentEventType.MODEL_INTERACTION_STARTED,
        AgentEventType.MODEL_INTERACTION_ENDED,
        AgentEventType.REPLY_DELTA,
        AgentEventType.TOOL_REQUESTED,
    }
)
"""The event types the integration publishes; the core publishes every other one."""


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelStep(_Step):
    """One model interaction streaming ``reply``."""

    step: Literal["model"] = "model"
    model: str = "scripted"
    reply: list[str] = []
    usage: TokenUsage | None = None


class ToolStep(_Step):
    """One tool call the model requests."""

    step: Literal["tool"] = "tool"
    id: str
    name: ToolName
    args: dict[str, JsonValue] = {}


class StateSetStep(_Step):
    step: Literal["state_set"] = "state_set"
    key: str
    value: str


class StateDeleteStep(_Step):
    step: Literal["state_delete"] = "state_delete"
    key: str


class SubagentStartStep(_Step):
    step: Literal["subagent_start"] = "subagent_start"
    name: str


class SubagentSendStep(_Step):
    step: Literal["subagent_send"] = "subagent_send"
    name: str
    handler: Handler = "run_reject"
    script: Script


class SubagentStopStep(_Step):
    step: Literal["subagent_stop"] = "subagent_stop"
    name: str


class RaiseStep(_Step):
    """The handler raises ``message``; later steps never run."""

    step: Literal["raise"] = "raise"
    message: str


Step = Annotated[
    ModelStep
    | ToolStep
    | StateSetStep
    | StateDeleteStep
    | SubagentStartStep
    | SubagentSendStep
    | SubagentStopStep
    | RaiseStep,
    Field(discriminator="step"),
]


class Script(BaseModel):
    """The steps one message plays, in order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: list[Step]


SubagentSendStep.model_rebuild()


class ScriptReply(BaseModel):
    """The text every model step of the script streamed."""

    text: str


def integration_events(script: Script) -> list[AgentStreamItem]:
    """The events the integration publishes playing ``script``, in order."""
    events: list[AgentStreamItem] = []
    for step in script.steps:
        match step:
            case ModelStep():
                events.append(ModelInteractionStarted(model=step.model))
                events.extend(ReplyDelta(text=chunk) for chunk in step.reply)
                events.append(ModelInteractionEnded(model=step.model, usage=step.usage))
            case ToolStep():
                events.append(ToolRequested(tool_id=step.id, tool_name=step.name, tool_input=step.args))
            case RaiseStep():
                break
            case _:
                pass
    return events
