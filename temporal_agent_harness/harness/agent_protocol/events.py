# ABOUTME: The agent's workflow-stream event vocabulary — the AgentEventType enum,
# the typed payload models a producer emits, the AgentStreamItem discriminated union
# over them, the AgentEvent transport envelope the harness wraps them in, and the
# ``turn_events`` topic they are published on.
#
# A producer constructs a StreamEvent payload (e.g. ``ReplyDelta(text=…)``) that
# carries ONLY its ``type`` discriminator and semantic fields — never routing
# metadata. The publisher wraps it in an :class:`AgentEvent` envelope, stamping
# ``turn_id`` / ``turn_number`` / ``timestamp`` — so those can only ever be set by
# the harness, never a caller. :data:`AgentStreamItem` is the discriminated union of
# payloads and the type of ``AgentEvent.event``, so Temporal's Pydantic converter
# reconstructs the concrete payload subtype on read.

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# The pubsub topic the agent publishes its turn events on. The workflow must use
# this exact name when publishing and clients when subscribing.
TURN_EVENTS_TOPIC = "turn_events"


class AgentEventType(StrEnum):
    """Every kind of event the agent publishes on the ``turn_events`` topic.

    The single source of truth for the event vocabulary. Each concrete
    :class:`AgentEvent` subtype pins its ``type`` to one member of this enum (as a
    ``Literal``), and :data:`AgentStreamItem` is the discriminated union over them
    keyed on ``type``. It is a ``StrEnum``, so members compare equal to their wire
    string (``AgentEventType.TOOL_START == "tool_start"``) and serialize as that
    plain string.

    Document the semantics of each event HERE, not in scattered string literals.
    """

    MESSAGE_QUEUED = "message_queued"
    """A user message was accepted but queued behind an active turn. Only
    published when there IS a queue. See :class:`MessageQueued`."""

    TURN_STARTED = "turn_started"
    """A turn has begun processing. If preceded by MESSAGE_QUEUED with the same
    ``turn_id``, that message is now active. See :class:`TurnStarted`."""

    TURN_END = "turn_end"
    """A turn has fully completed and the agent has returned to idle, awaiting the
    next message — closing the outer loop opened by TURN_STARTED. Emitted for EVERY turn,
    success or error: the runner's turn loop publishes it in a ``finally`` after the
    reply (success) or the ERROR event (the handler raised), so it is the single reliable
    end-of-turn signal a consumer can terminate on. See :class:`TurnEnded`."""

    MODEL_INTERACTION_STARTED = "model_interaction_started"
    """The agent has begun ONE interaction with the model — a single streaming
    model call has started.

    Brackets, with MODEL_INTERACTION_ENDED, the spans in which the MODEL itself is
    doing work, as distinct from the AGENT doing other work around it (running
    tools, applying policy, waiting on a worker). A single turn typically contains
    SEVERAL such interactions — model → requested tool calls → model again with the
    results → … — and each model call publishes exactly one started/ended pair. A
    consumer can therefore tell "the model is generating right now" from "the agent
    is busy but the model is idle". See :class:`ModelInteractionStarted`."""

    MODEL_INTERACTION_ENDED = "model_interaction_ended"
    """One interaction with the model has finished — its streaming call completed
    (or failed). After this the agent may execute the tools the model requested, do
    other work, then open the next MODEL_INTERACTION_STARTED, or finish the turn.
    Published exactly once for each MODEL_INTERACTION_STARTED. See
    :class:`ModelInteractionEnded`."""

    TOOL_REQUESTED = "tool_requested"
    """The model has requested a tool call, but execution has NOT begun yet.

    Published when the agent decides to invoke a tool, BEFORE the call is actually
    dispatched to / picked up by a worker — so a UI can show the call as *requested*
    while it waits. The wait may be benign (an activity worker is briefly
    unavailable / redeploying) or deliberate (in future, a tool-approval policy or
    human-in-the-loop decision sits in exactly this gap). A request need not reach
    TOOL_START: one that is rejected by policy, or interrupted before it runs, ends
    without ever executing. Meaningful for harness-scheduled (custom-tool) calls
    that cross an activity boundary; built-in server-side tools have no such gap.
    See :class:`ToolRequested`."""

    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    """A gated tool call is awaiting a human approval decision.

    Published in the gap between TOOL_REQUESTED and TOOL_START, carrying the same
    ``tool_id``, when the agent's :class:`ToolApprovalPolicy` does not auto-approve the
    call (safe-by-default — see :class:`ToolApprovalPolicy`). The agent
    parks on an in-workflow wait condition until a ``tool_approval`` update resolves it;
    a UI renders an approve/deny affordance off this event. Outstanding requests are
    also discoverable via the ``agent_status`` query (``pending_approvals``) so a client
    that attaches late can still act on them. See :class:`ToolApprovalRequested`."""

    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    """A pending tool approval was resolved — approved or denied.

    Published once per :data:`TOOL_APPROVAL_REQUESTED`, including the auto-denial that
    happens if the agent closes while the approval is still pending. On approval,
    TOOL_START follows as the call dispatches; on denial, the call ends here without ever
    executing (and the model receives an error result). See :class:`ToolApprovalResolved`."""

    TOOL_START = "tool_start"
    """A tool invocation has begun EXECUTING. Published ONCE per call with the tool
    name and full args; built-in tools defer it to their call step's completion so
    any streamed args are consolidated. Follows :class:`ToolRequested` and marks the
    transition from requested to actually running. See :class:`ToolStartEvent`."""

    TOOL_END = "tool_end"
    """A tool invocation completed successfully, carrying the final single
    payload. See :class:`ToolEndEvent`."""

    TOOL_PROGRESS_DELTA = "tool_progress_delta"
    """An intermediate progress chunk from a tool mid-execution — for tools doing
    genuine multi-step work that report progress BEFORE their terminal TOOL_END
    payload, NOT for how a transport happens to chunk one logical output. No tool
    emits this yet. See :class:`ToolProgressDelta`."""

    TOOL_ERROR = "tool_error"
    """A tool invocation failed. Terminal for that tool call; the turn itself may
    continue. See :class:`ToolErrorEvent`."""

    SUBAGENT_STARTED = "subagent_started"
    """This agent started a subagent (a child agent it drives), carrying the subagent's
    short ``handle`` and its real ``workflow_id``. Analogous to TOOL_START but for a
    whole child agent's lifecycle rather than one tool call.

    The ``workflow_id`` is the load-bearing field: a consumer that wants a single
    consolidated view can use it to dynamically mount the subagent's OWN event stream
    (the subagent's stream is never mirrored onto this one — see the stream-isolation
    design — so the consumer attaches to the child directly). Mount on this event,
    unmount on SUBAGENT_STOPPED (or when the child workflow completes).
    See :class:`SubagentStarted`."""

    SUBAGENT_STOPPED = "subagent_stopped"
    """This agent stopped a subagent it was driving (sent it the ``close`` signal and
    dropped it). Carries the same ``handle`` / ``workflow_id`` so a consumer can unmount
    the subagent's stream. Terminal for that subagent from this agent's perspective.
    See :class:`SubagentStopped`."""

    SUBAGENT_MESSAGE_SENT = "subagent_message_sent"
    """This agent sent a message (one turn) to a downstream subagent it is driving.
    Published whenever the agent communicates with a subagent, naming the target subagent
    (``handle`` / ``workflow_id``), the ``function`` the message is addressed to, and the
    ``subagent_turn`` it starts (the turn number on the SUBAGENT — distinct from this event's
    envelope ``turn_number``, which is the parent's turn) — so a consumer can correlate it with
    the matching turn on the subagent's OWN stream (mounted via the SUBAGENT_STARTED
    ``workflow_id``). Analogous to
    SUBAGENT_STARTED, but for a single message rather than the subagent's lifecycle. The
    subagent's reply is NOT mirrored onto this stream (stream isolation).
    See :class:`SubagentMessageSent`."""

    REPLY_DELTA = "reply_delta"
    """An incremental text chunk (word/token) of the agent's reply. See
    :class:`ReplyDelta`."""

    THOUGHT_SUMMARY = "thought_summary"
    """A streamed thought-summary delta from the model. See
    :class:`ThoughtSummaryDelta`."""

    TEXT_ANNOTATION = "text_annotation"
    """A streamed citation/annotation delta, interleaved with REPLY_DELTA so each
    citation sits next to the text it supports. See :class:`TextAnnotationDelta`."""

    REPLY = "reply"
    """The agent's final text reply. Terminal for the turn. See
    :class:`AgentReply`."""

    ERROR = "error"
    """The agent encountered an error. Terminal for the turn. See
    :class:`AgentError`."""


EventTypeT = TypeVar("EventTypeT", bound=AgentEventType)


class StreamEvent(BaseModel, Generic[EventTypeT]):
    """Base for every stream-event payload the agent emits.

    Generic over the ``type`` discriminator, so each subclass narrows ``type`` to
    its own :class:`AgentEventType` member (as a ``Literal``) — making the
    discriminator both mandatory (you cannot define a payload without it) and
    statically pinned to one value per event. Carries NO routing metadata: that
    lives on the :class:`AgentEvent` envelope and is set only by the harness, so a
    producer cannot set (or accidentally override) it.
    """

    model_config = ConfigDict(frozen=True)

    type: EventTypeT


class MessageQueued(StreamEvent[Literal[AgentEventType.MESSAGE_QUEUED]]):
    """A message was accepted and queued behind pending work."""

    type: Literal[AgentEventType.MESSAGE_QUEUED] = AgentEventType.MESSAGE_QUEUED
    user_message: str = Field(
        description="The user message that was accepted but queued behind an active turn."
    )


class TurnStarted(StreamEvent[Literal[AgentEventType.TURN_STARTED]]):
    """A turn has begun processing."""

    type: Literal[AgentEventType.TURN_STARTED] = AgentEventType.TURN_STARTED
    user_message: str = Field(
        description="The user message this turn is now actively processing."
    )


class TurnEnded(StreamEvent[Literal[AgentEventType.TURN_END]]):
    """A turn has fully completed and the agent is idle, awaiting the next message.

    Closes the outer agent loop opened by :class:`TurnStarted`. Published for EVERY turn —
    after :class:`AgentReply` on success, or after :class:`AgentError` when the handler
    raised (the runner emits it in a ``finally``) — so it is the single reliable
    end-of-turn signal. The envelope's ``turn_id`` / ``turn_number`` identify which turn
    ended.
    """

    type: Literal[AgentEventType.TURN_END] = AgentEventType.TURN_END


class TokenUsage(BaseModel):
    """Provider-agnostic token accounting for one model interaction.

    A neutral summary the harness understands without knowing any model SDK — a
    producer (e.g. the Gemini plugin) maps its own usage shape onto these fields. Every
    field is optional: a provider that doesn't report a given count, or an interaction
    that ends before usage is known, simply leaves it ``None``. ``total_tokens`` is the
    provider's own grand total when given (not necessarily the sum of the parts, since
    providers count cached/tool-use tokens differently).

    Every field is optional: a provider that doesn't report a given count, or an
    interaction that ends before usage is known, simply leaves it ``None``."""

    input_tokens: int | None = Field(
        default=None, description="Tokens in the prompt/input to the model, or None if unreported."
    )
    output_tokens: int | None = Field(
        default=None,
        description="Tokens the model generated as output, or None if unreported.",
    )
    thought_tokens: int | None = Field(
        default=None,
        description="Tokens spent on the model's reasoning/thinking, or None if unreported.",
    )
    cached_tokens: int | None = Field(
        default=None,
        description="Input tokens served from the provider's prompt cache, or None if unreported.",
    )
    tool_use_tokens: int | None = Field(
        default=None,
        description="Tokens attributed to tool use, or None if unreported.",
    )
    total_tokens: int | None = Field(
        default=None,
        description="The provider's own grand total when given (not necessarily the sum of the "
        "parts, since providers count cached/tool-use tokens differently), or None if unreported.",
    )


class ModelInteractionStarted(
    StreamEvent[Literal[AgentEventType.MODEL_INTERACTION_STARTED]]
):
    """One streaming model call has begun — opening a span of MODEL work.

    The envelope's ``turn_id`` / ``turn_number`` say which turn it belongs to; within a
    turn there may be many.
    """

    type: Literal[AgentEventType.MODEL_INTERACTION_STARTED] = (
        AgentEventType.MODEL_INTERACTION_STARTED
    )
    model: str | None = Field(
        default=None,
        description="The model the call was issued against (the requested id), when the "
        "producer knows it — None otherwise.",
    )


class ModelInteractionEnded(
    StreamEvent[Literal[AgentEventType.MODEL_INTERACTION_ENDED]]
):
    """One streaming model call has ended, closing a MODEL_INTERACTION_STARTED.

    Published exactly once for each :class:`ModelInteractionStarted`.
    """

    type: Literal[AgentEventType.MODEL_INTERACTION_ENDED] = (
        AgentEventType.MODEL_INTERACTION_ENDED
    )
    model: str | None = Field(
        default=None,
        description="The model the call was issued against (the same id as on the matching "
        "ModelInteractionStarted), or None if the producer didn't report it.",
    )
    usage: TokenUsage | None = Field(
        default=None,
        description="The call's TokenUsage when the provider reported it, or None if unavailable "
        "— e.g. the stream errored before completing.",
    )


class ToolEvent(StreamEvent[EventTypeT], Generic[EventTypeT]):
    """Base for every tool-lifecycle event.

    Correlates all the events of ONE tool invocation — requested → start →
    progress → end/error — via a shared :attr:`tool_id` chosen once when the call is
    requested and echoed by every later event. ``tool_name`` ALONE cannot
    disambiguate two parallel calls to the same tool, so every tool event carries
    both. The id must be stable and re-derivable wherever the events are produced
    (e.g. the same value in the streaming activity that emits ``tool_requested`` and
    in the workflow that emits ``tool_start``/``tool_end``); for the Gemini
    integration it is the API's per-call id.
    """

    tool_id: str = Field(
        description="Stable correlation id chosen once when the call is requested and echoed by "
        "every later event of the SAME tool invocation (requested → start → progress → end/error)."
    )
    tool_name: str = Field(
        description="The name of the invoked tool. Cannot ALONE disambiguate two parallel calls "
        "to the same tool, hence the separate tool_id."
    )


class ToolRequested(ToolEvent[Literal[AgentEventType.TOOL_REQUESTED]]):
    """The model has requested a tool call that has not begun executing yet.

    Precedes :class:`ToolStartEvent` in the tool lifecycle (requested → start →
    end/error). Carries the same ``tool_id`` + ``tool_name`` as the eventual start,
    so a UI can render the pending call and a future approval policy can evaluate it
    before it runs.
    """

    type: Literal[AgentEventType.TOOL_REQUESTED] = AgentEventType.TOOL_REQUESTED
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="The full arguments of the requested call, so a UI can render the pending "
        "call and a future approval policy can evaluate it before it runs.",
    )


class ToolApprovalRequested(ToolEvent[Literal[AgentEventType.TOOL_APPROVAL_REQUESTED]]):
    """A gated tool call is awaiting a human approval decision.

    Sits between :class:`ToolRequested` and :class:`ToolStartEvent`, sharing their
    ``tool_id`` + ``tool_name``.
    """

    type: Literal[AgentEventType.TOOL_APPROVAL_REQUESTED] = (
        AgentEventType.TOOL_APPROVAL_REQUESTED
    )
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="The model-facing arguments of the gated call (injected parameters are "
        "excluded — they are not the model's choice) so an approver sees exactly what the model "
        "asked for.",
    )


class ToolApprovalResolved(ToolEvent[Literal[AgentEventType.TOOL_APPROVAL_RESOLVED]]):
    """A pending tool approval was resolved — approved or denied."""

    type: Literal[AgentEventType.TOOL_APPROVAL_RESOLVED] = (
        AgentEventType.TOOL_APPROVAL_RESOLVED
    )
    approved: bool = Field(
        description="True if the call is approved (it will now execute), False if denied (it "
        "ends here without ever executing and the model receives an error result)."
    )
    reason: str | None = Field(
        default=None,
        description="An optional human-supplied note, also set to the auto-denial reason when "
        "the agent closes while the approval is still pending.",
    )
    remember: bool = Field(
        default=False,
        description="Whether THIS decision asked to be remembered — i.e. an 'approve, and stop "
        "asking me about this tool' decision that allow-lists the tool on the live "
        "ToolApprovalPolicy. False for a one-off decision and for a call swept up by a policy "
        "change (e.g. the cascade that auto-approves other pending calls of a now-allow-listed "
        "tool, whose reason is the auto-approval note).",
    )


class ToolStartEvent(ToolEvent[Literal[AgentEventType.TOOL_START]]):
    """A tool invocation has begun executing."""

    type: Literal[AgentEventType.TOOL_START] = AgentEventType.TOOL_START
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="The full arguments the tool began executing with.",
    )


class ToolEndEvent(ToolEvent[Literal[AgentEventType.TOOL_END]]):
    """A tool invocation has completed."""

    type: Literal[AgentEventType.TOOL_END] = AgentEventType.TOOL_END
    tool_output: str = Field(
        default="", description="The tool's final, single result payload."
    )


class ToolProgressDelta(ToolEvent[Literal[AgentEventType.TOOL_PROGRESS_DELTA]]):
    """An intermediate progress chunk emitted by a tool mid-execution.

    Distinct from :class:`ToolEndEvent`'s final payload: this is for tools doing
    genuine multi-step work (e.g. a custom tool that connects to a service,
    issues several requests, then returns) that want to report progress as they
    go. Whether a tool streams these is a property of the tool's semantics, not
    of how a transport happens to chunk its output. No tool emits this yet; see
    the producer note in ``google_genai_plugin/_interactions_activity.py``.
    """

    type: Literal[AgentEventType.TOOL_PROGRESS_DELTA] = AgentEventType.TOOL_PROGRESS_DELTA
    progress_delta: str = Field(
        description="One intermediate progress chunk emitted mid-execution, before the terminal "
        "TOOL_END payload."
    )


class ToolErrorEvent(ToolEvent[Literal[AgentEventType.TOOL_ERROR]]):
    """A tool invocation has failed.

    Terminal for that specific tool call — the UI can resolve the card out of its
    "running" state. The turn itself may continue if the caller (e.g. Gemini AFC)
    decides to retry or surface the failure back to the model.
    """

    type: Literal[AgentEventType.TOOL_ERROR] = AgentEventType.TOOL_ERROR
    message: str = Field(description="A description of why the tool invocation failed.")


class SubagentStarted(StreamEvent[Literal[AgentEventType.SUBAGENT_STARTED]]):
    """This agent started a subagent (a child agent it drives)."""

    type: Literal[AgentEventType.SUBAGENT_STARTED] = AgentEventType.SUBAGENT_STARTED
    handle: str = Field(
        description="The short id this agent references the subagent by (the value it passes "
        "to its send_<function> / stop_<key> tools)."
    )
    agent_key: str = Field(description="Which wired agent type this subagent is.")
    workflow_id: str = Field(
        description="The subagent's real child workflow id — what a consumer uses to "
        "dynamically mount the subagent's OWN event stream for a consolidated view (subagent "
        "streams are never mirrored onto this one)."
    )


class SubagentStopped(StreamEvent[Literal[AgentEventType.SUBAGENT_STOPPED]]):
    """This agent stopped a subagent it was driving (signalled ``close`` + dropped it)."""

    type: Literal[AgentEventType.SUBAGENT_STOPPED] = AgentEventType.SUBAGENT_STOPPED
    handle: str = Field(
        description="The short id of the stopped subagent (matches the prior SubagentStarted)."
    )
    agent_key: str = Field(description="Which wired agent type the stopped subagent was.")
    workflow_id: str = Field(
        description="The stopped subagent's real child workflow id — what a consumer uses to "
        "unmount the subagent's event stream (matches the prior SubagentStarted)."
    )


class SubagentMessageSent(StreamEvent[Literal[AgentEventType.SUBAGENT_MESSAGE_SENT]]):
    """This agent dispatched one message (a turn) to a subagent it is driving."""

    type: Literal[AgentEventType.SUBAGENT_MESSAGE_SENT] = (
        AgentEventType.SUBAGENT_MESSAGE_SENT
    )
    handle: str = Field(
        description="The short id of the subagent being messaged (matches its SubagentStarted)."
    )
    agent_key: str = Field(description="Which wired agent type the messaged subagent is.")
    workflow_id: str = Field(
        description="The messaged subagent's real child workflow id — what a consumer uses to "
        "correlate this dispatch with the matching turn on the subagent's OWN event stream "
        "(mounted via SubagentStarted; subagent streams are never mirrored onto this one)."
    )
    function: str = Field(
        description="The function this message is addressed to on the subagent (the "
        "send_agent_message envelope 'type' — which of the subagent's accepted messages it is)."
    )
    subagent_turn: int = Field(
        description="The turn number ON THE SUBAGENT that this dispatch starts — distinct from "
        "the enclosing AgentEvent's `turn_number`, which is THIS agent's (the parent's) turn. "
        "Several dispatches in one parent turn share that envelope turn_number but get distinct "
        "subagent_turn values; pairs with the turn_number on the subagent's OWN stream events."
    )


class ReplyDelta(StreamEvent[Literal[AgentEventType.REPLY_DELTA]]):
    """An incremental text chunk of the agent's reply."""

    type: Literal[AgentEventType.REPLY_DELTA] = AgentEventType.REPLY_DELTA
    text: str = Field(
        description="One incremental text chunk (word/token) of the agent's reply."
    )


class ThoughtSummaryDelta(StreamEvent[Literal[AgentEventType.THOUGHT_SUMMARY]]):
    """A streamed thought-summary chunk from the model."""

    type: Literal[AgentEventType.THOUGHT_SUMMARY] = AgentEventType.THOUGHT_SUMMARY
    delta: dict[str, Any] = Field(
        default_factory=dict,
        description="The raw ``DeltaThoughtSummary`` payload dumped to a dict.",
    )


class TextAnnotationDelta(StreamEvent[Literal[AgentEventType.TEXT_ANNOTATION]]):
    """A streamed annotation/citation delta interleaved with reply text."""

    type: Literal[AgentEventType.TEXT_ANNOTATION] = AgentEventType.TEXT_ANNOTATION
    delta: dict[str, Any] = Field(
        default_factory=dict,
        description="The raw ``DeltaTextAnnotationDelta`` payload dumped to a dict so consumers "
        "can position citations next to the surrounding ``ReplyDelta`` chunks.",
    )


class AgentReply(StreamEvent[Literal[AgentEventType.REPLY]]):
    """The agent's final reply for a turn — the handler's return value. Terminal."""

    type: Literal[AgentEventType.REPLY] = AgentEventType.REPLY
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="The handler's return model serialized to JSON (the harness publishes "
        '``result.model_dump(mode="json")``). Carrying a plain dict keeps the reply trivially '
        "round-trippable on the stream regardless of the concrete output type; a consumer that "
        "knows the expected type (e.g. from ``agent_interface``) re-validates the dict into it.",
    )


class AgentError(StreamEvent[Literal[AgentEventType.ERROR]]):
    """A terminal error for the turn (e.g. an unhandled workflow exception)."""

    type: Literal[AgentEventType.ERROR] = AgentEventType.ERROR
    message: str = Field(
        description="A description of the terminal error that ended the turn."
    )


# Discriminated union of every concrete payload, keyed on ``type``. This is the
# type of :attr:`AgentEvent.event`, so Temporal's Pydantic converter reconstructs
# the right payload subtype when it deserializes an envelope off the stream.
AgentStreamItem = Annotated[
    MessageQueued
    | TurnStarted
    | TurnEnded
    | ModelInteractionStarted
    | ModelInteractionEnded
    | ToolRequested
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolStartEvent
    | ToolEndEvent
    | ToolProgressDelta
    | ToolErrorEvent
    | SubagentStarted
    | SubagentStopped
    | SubagentMessageSent
    | ReplyDelta
    | ThoughtSummaryDelta
    | TextAnnotationDelta
    | AgentReply
    | AgentError,
    Field(discriminator="type"),
]


class AgentEvent(BaseModel):
    """Transport envelope for one stream event.

    The single concrete type published on — and subscribed to from — the
    ``turn_events`` topic. Composes over a :data:`AgentStreamItem` payload
    (``event``) and adds the routing metadata the harness stamps at publish time.
    Producers never build this — they construct a payload and the publisher wraps
    it — so ``turn_id`` / ``turn_number`` / ``timestamp`` can only ever be set by
    the harness. Being a concrete type (not a bare union), it is also a clean
    ``type`` for the workflow-stream topic and subscribe ``result_type``.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: str = Field(
        description="The id of the turn this event belongs to — stamped by the harness at "
        "publish time; producers never set it."
    )
    turn_number: int = Field(
        description="The monotonic number of the turn this event belongs to — stamped by the "
        "harness at publish time; producers never set it."
    )
    timestamp: float = Field(
        description="When the harness published this envelope (epoch seconds)."
    )
    event: AgentStreamItem = Field(
        description="The wrapped stream-event payload — a discriminated union over ``type`` that "
        "Temporal's Pydantic converter reconstructs to the concrete payload subtype on read."
    )
