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

    OPERATOR_COMMAND_STARTED = "operator_command_started"
    """A human/operator command has begun executing outside the agent turn loop.

    Operator command events are control-plane audit records, not model turns. They use
    ``turn_number=0`` in the :class:`AgentEvent` envelope so clients can replay them
    durably without grouping them into agent-turn summaries. See
    :class:`OperatorCommandStarted`."""

    OPERATOR_COMMAND_COMPLETED = "operator_command_completed"
    """A human/operator command completed successfully. See
    :class:`OperatorCommandCompleted`."""

    OPERATOR_COMMAND_FAILED = "operator_command_failed"
    """A human/operator command failed without creating an agent turn. See
    :class:`OperatorCommandFailed`."""

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

    CALLBACK_REQUESTED = "callback_requested"
    """A callback tool is awaiting a result from an external client.

    Published by a callback tool's in-workflow body (an ``@agent.callback_tool_defn`` tool)
    after it has passed the approval gate and emitted TOOL_START — the tool has no worker-side
    implementation, so it hands the call to an attached client to fulfill on its own machine
    (read a local file, snap a photo, run a shell command). Carries the same ``tool_id`` +
    ``tool_name`` as the surrounding tool lifecycle, plus the call args (``tool_input``) and the
    JSON schema of the expected result (``output_schema``) so the client knows what to return.
    The agent parks on an in-workflow wait condition (with an optional timeout) until a
    ``provide_callback_result`` update resolves it; a client renders a fulfillment affordance off
    this event. Outstanding requests are also discoverable via the ``agent_status`` query
    (``pending_callbacks``) so a client that attaches late can still act on them. Nests between
    TOOL_START and TOOL_END for the same call. See :class:`CallbackRequested`."""

    CALLBACK_RESOLVED = "callback_resolved"
    """A pending callback tool call was resolved — the client returned a result, reported an
    error, or the wait timed out (or the agent closed while it was pending).

    Published once per :data:`CALLBACK_REQUESTED`, carrying the same ``tool_id`` + ``tool_name``.
    On ``ok`` the client-supplied result (validated against the tool's declared output type)
    becomes the tool's return value and TOOL_END follows; on ``error`` / ``timeout`` the call
    fails and TOOL_ERROR follows (the model receives an error result). See
    :class:`CallbackResolved`."""

    SUBAGENT_STARTED = "subagent_started"
    """This agent started a subagent (a child agent it drives), carrying the subagent's
    short ``subagent_id`` and its real ``workflow_id``. Analogous to TOOL_START but for a
    whole child agent's lifecycle rather than one tool call.

    The ``workflow_id`` is the load-bearing field: a consumer that wants a single
    consolidated view can use it to dynamically mount the subagent's OWN event stream
    (the subagent's stream is never mirrored onto this one — see the stream-isolation
    design — so the consumer attaches to the child directly). Mount on this event,
    unmount on SUBAGENT_STOPPED (or when the child workflow completes).
    See :class:`SubagentStarted`."""

    SUBAGENT_STOPPED = "subagent_stopped"
    """This agent stopped a subagent it was driving (sent it the ``close`` signal and
    dropped it). Carries the same ``subagent_id`` / ``workflow_id`` so a consumer can unmount
    the subagent's stream. Terminal for that subagent from this agent's perspective.
    See :class:`SubagentStopped`."""

    SUBAGENT_MESSAGE_SENT = "subagent_message_sent"
    """This agent sent a message (one turn) to a downstream subagent it is driving.
    Published whenever the agent communicates with a subagent, naming the target subagent
    (``subagent_id`` / ``workflow_id``), the ``function`` the message is addressed to, and the
    ``subagent_turn`` it starts (the turn number on the SUBAGENT — distinct from this event's
    envelope ``turn_number``, which is the parent's turn) — so a consumer can correlate it with
    the matching turn on the subagent's OWN stream (mounted via the SUBAGENT_STARTED
    ``workflow_id``). Analogous to
    SUBAGENT_STARTED, but for a single message rather than the subagent's lifecycle. The
    subagent's reply is NOT mirrored onto this stream (stream isolation).
    See :class:`SubagentMessageSent`."""

    SUBAGENT_REPLY_RECEIVED = "subagent_reply_received"
    """This agent received a subagent's reply for one turn it dispatched — the CLOSE marker of
    the ``[subagent_message_sent … subagent_reply_received]`` bracket that
    ``SUBAGENT_MESSAGE_SENT`` opens. Carries the same correlation fields (``subagent_id`` /
    ``agent_key`` / ``workflow_id`` / ``function`` / ``subagent_turn``) plus an ``outcome``
    (``ok``/``error``). Published IN-WORKFLOW by the parent's ``run_subagent_turn`` once the
    activity returns — i.e. once the AGENT (workflow) actually has the reply in hand — and
    published for EVERY accepted child turn (including an accepted-but-errored one, which still
    emitted its own ``turn_end``), but NEVER for a pre-acceptance rejection (no child turn ran,
    so no bracket to close). The reply *payload* is not carried here (it rides the child's own
    ``reply`` event and the send-tool's ``tool_end``); this is a thin close/correlation marker.
    A client merging the parent + subagent streams uses it to guarantee a subagent's whole turn
    is ordered before the reply is observed on the parent. See :class:`SubagentReplyReceived`."""

    SUBAGENT_STREAM_UNAVAILABLE = "subagent_stream_unavailable"
    """A client merging the parent + subagent streams could NOT obtain a subagent's events, so it
    gave up on that subagent's DETAIL and rendered the parent's view without it.

    Unlike every other event here, this one is **never published by a workflow** — it is
    SYNTHESIZED CLIENT-SIDE by the stream merge and injected into the merged logical stream. It is
    emitted when a mounted subagent stream can't be read (today: a stopped/completed subagent, whose
    stream ``workflow_streams`` can't yet replay — an upstream workflow_streams fix is in flight) 
    or stalls without delivering its turn. It is purely informational and **non-fatal**: the parent's
    own stream is self-sufficient (it already carries the subagent's ``subagent_reply_received`` and 
    the send-tool's result), so the parent renders fully; only the subagent's own turn DETAIL is
    missing. The merge does NOT retry — recovery is a fresh ``attach`` (a full page refresh in a
    UI). A drill-in UI keys off the carried ``subagent_id`` to mark that subagent's view degraded.
    See :class:`SubagentStreamUnavailable`."""

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


class OperatorCommandEvent(StreamEvent[EventTypeT], Generic[EventTypeT]):
    """Base for operator-command audit events.

    Correlates the started/completed/failed records for one out-of-band operator action.
    These events are published by the workflow update that handles the command, but they
    are not agent turns and should not be exposed as model-callable behavior.
    """

    operator_command_id: str = Field(
        description="Stable id correlating the lifecycle events for one operator command."
    )
    command_name: str = Field(
        description="The command payload name executed by the workflow update."
    )
    command_label: str = Field(
        description="The human-facing slash label, such as '/approvals'."
    )
    arg: str | None = Field(
        default=None,
        description="The optional command argument supplied by the operator.",
    )


class OperatorCommandStarted(
    OperatorCommandEvent[Literal[AgentEventType.OPERATOR_COMMAND_STARTED]]
):
    """A human/operator command has begun executing."""

    type: Literal[AgentEventType.OPERATOR_COMMAND_STARTED] = (
        AgentEventType.OPERATOR_COMMAND_STARTED
    )


class OperatorCommandCompleted(
    OperatorCommandEvent[Literal[AgentEventType.OPERATOR_COMMAND_COMPLETED]]
):
    """A human/operator command completed successfully."""

    type: Literal[AgentEventType.OPERATOR_COMMAND_COMPLETED] = (
        AgentEventType.OPERATOR_COMMAND_COMPLETED
    )
    text: str = Field(description="The operator-facing result text returned by the command.")


class OperatorCommandFailed(
    OperatorCommandEvent[Literal[AgentEventType.OPERATOR_COMMAND_FAILED]]
):
    """A human/operator command failed."""

    type: Literal[AgentEventType.OPERATOR_COMMAND_FAILED] = (
        AgentEventType.OPERATOR_COMMAND_FAILED
    )
    message: str = Field(description="The operator-facing failure message.")


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


class CallbackRequested(ToolEvent[Literal[AgentEventType.CALLBACK_REQUESTED]]):
    """A callback tool call is awaiting a result from an external client.

    Sits between :class:`ToolStartEvent` and :class:`ToolEndEvent` for the same call, sharing
    their ``tool_id`` + ``tool_name``. The tool has no worker-side body: an attached client
    fulfills it on its own machine and returns the result via a ``provide_callback_result``
    update keyed on ``tool_id``.
    """

    type: Literal[AgentEventType.CALLBACK_REQUESTED] = AgentEventType.CALLBACK_REQUESTED
    tool_input: dict[str, Any] = Field(
        default_factory=dict,
        description="The model-facing arguments of the callback call (injected parameters "
        "excluded), so the fulfilling client sees exactly what the model asked for.",
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON schema of the result the client must return — derived from the "
        "callback tool's declared output type. The client's result is validated against this "
        "before it resolves the call.",
    )


class CallbackResolved(ToolEvent[Literal[AgentEventType.CALLBACK_RESOLVED]]):
    """A pending callback tool call was resolved — result returned, error, or timeout."""

    type: Literal[AgentEventType.CALLBACK_RESOLVED] = AgentEventType.CALLBACK_RESOLVED
    outcome: Literal["ok", "error", "timeout"] = Field(
        description="'ok' if the client returned a result (validated against the tool's output "
        "type) — TOOL_END follows; 'error' if the client reported a failure fulfilling the call; "
        "'timeout' if the wait elapsed (or the agent closed) before any result arrived. On "
        "'error'/'timeout' the call fails and TOOL_ERROR follows."
    )
    error: str | None = Field(
        default=None,
        description="The client-reported failure message ('error'), the timeout/close note "
        "('timeout'), or None on 'ok'.",
    )


class SubagentStarted(StreamEvent[Literal[AgentEventType.SUBAGENT_STARTED]]):
    """This agent started a subagent (a child agent it drives)."""

    type: Literal[AgentEventType.SUBAGENT_STARTED] = AgentEventType.SUBAGENT_STARTED
    subagent_id: str = Field(
        description="The short id of the SUBAGENT this event is about — the id the parent "
        "references it by AND the ``agent_id`` the subagent stamps on its own events (the parent "
        "pushes it down as the child's id). Distinct from the enclosing envelope's ``agent_id``, "
        "which is THIS (the parent) agent."
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
    subagent_id: str = Field(
        description="The short id of the stopped SUBAGENT (matches the prior SubagentStarted). "
        "Distinct from the envelope's ``agent_id`` (this parent agent)."
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
    subagent_id: str = Field(
        description="The short id of the SUBAGENT being messaged (matches its SubagentStarted, and "
        "the ``agent_id`` on that subagent's own events). Distinct from the envelope's ``agent_id`` "
        "(this parent agent)."
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
    from_offset: int = Field(
        default=0,
        description="The offset in the SUBAGENT's OWN stream at which this turn's events begin "
        "(the child stream position the parent resumes consumption from for this turn). A client "
        "merging the parent + subagent streams positions the child cursor here the first time it "
        "mounts the child — so a merge that starts mid-session (resuming at a parent turn that is "
        "not the child's first) skips the child's pre-resume history, whose own message_sent "
        "markers are not on the merged stream and could otherwise never be ordered. Unrelated "
        "address space from the parent stream's offsets.",
    )


class SubagentReplyReceived(
    StreamEvent[Literal[AgentEventType.SUBAGENT_REPLY_RECEIVED]]
):
    """This agent received a subagent's reply for one turn it dispatched.

    The CLOSE marker of the ``[subagent_message_sent … subagent_reply_received]`` bracket
    (mirrors :class:`SubagentMessageSent`'s correlation fields), published in-workflow by the
    parent's ``run_subagent_turn`` once the activity returns and the agent (workflow) actually
    holds the reply — for EVERY accepted child turn (``outcome`` distinguishes success from an
    accepted-but-errored turn, which still emitted its own ``turn_end``), but never for a
    pre-acceptance rejection. Carries no reply payload (that rides the child's own ``reply``
    event + the send-tool's ``tool_end``); it is a thin close/correlation signal a stream-merge
    uses to order a subagent's whole turn ahead of the parent observing its reply.
    """

    type: Literal[AgentEventType.SUBAGENT_REPLY_RECEIVED] = (
        AgentEventType.SUBAGENT_REPLY_RECEIVED
    )
    subagent_id: str = Field(
        description="The short id of the SUBAGENT that replied (matches its SubagentMessageSent, "
        "and the ``agent_id`` on that subagent's own events). Distinct from the envelope's "
        "``agent_id`` (this parent agent)."
    )
    agent_key: str = Field(description="Which wired agent type the replying subagent is.")
    workflow_id: str = Field(
        description="The replying subagent's real child workflow id (matches the opening "
        "SubagentMessageSent) — the close-gate key a stream-merge correlates against."
    )
    function: str = Field(
        description="The function this reply answers (the same send_agent_message envelope "
        "'type' as the opening SubagentMessageSent)."
    )
    subagent_turn: int = Field(
        description="The turn number ON THE SUBAGENT this reply closes — pairs with the opening "
        "SubagentMessageSent's subagent_turn, NOT the enclosing envelope's (parent's) turn_number."
    )
    outcome: Literal["ok", "error"] = Field(
        description="'ok' if the child turn produced a reply; 'error' if it was accepted but then "
        "errored / produced no reply (it still emitted its own turn_end, so the bracket closes "
        "either way). Never published for a pre-acceptance rejection."
    )


class SubagentStreamUnavailable(StreamEvent[Literal[AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]]):
    """The stream merge gave up obtaining a subagent's events; its DETAIL is degraded (non-fatal).

    SYNTHESIZED CLIENT-SIDE by the merge (never workflow-published) and injected into the merged
    logical stream when a mounted subagent stream is unreadable/stalled. The merge stamps the
    enclosing :class:`AgentEvent`'s ``agent_id`` with this same ``subagent_id`` — so a UI that
    groups events by ``agent_id`` routes this straight to the affected subagent's view, and a
    root-only consumer (which filters to the root ``agent_id``) ignores it. The parent stream is
    unaffected; only the subagent's own turn detail is missing. No retry — a fresh attach recovers.
    """

    type: Literal[AgentEventType.SUBAGENT_STREAM_UNAVAILABLE] = (
        AgentEventType.SUBAGENT_STREAM_UNAVAILABLE
    )
    subagent_id: str = Field(
        description="The short id of the subagent whose events couldn't be obtained (matches the "
        "``subagent_id`` on the parent's SubagentStarted/SubagentMessageSent, and the ``agent_id`` "
        "that subagent stamps on its own events)."
    )
    workflow_id: str = Field(
        description="The subagent's real child workflow id (the stream the merge couldn't read)."
    )
    reason: str = Field(
        default="",
        description="A short, human-facing note on why the subagent's events were unavailable "
        "(e.g. the child workflow has completed and its stream isn't yet replayable).",
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
    | OperatorCommandStarted
    | OperatorCommandCompleted
    | OperatorCommandFailed
    | ModelInteractionStarted
    | ModelInteractionEnded
    | ToolRequested
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolStartEvent
    | ToolEndEvent
    | ToolProgressDelta
    | ToolErrorEvent
    | CallbackRequested
    | CallbackResolved
    | SubagentStarted
    | SubagentStopped
    | SubagentMessageSent
    | SubagentReplyReceived
    | SubagentStreamUnavailable
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
    it — so ``agent_id`` / ``turn_id`` / ``turn_number`` / ``timestamp`` can only ever be set
    by the harness. Being a concrete type (not a bare union), it is also a clean
    ``type`` for the workflow-stream topic and subscribe ``result_type``.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(
        description="The short, TREE-UNIQUE id of the agent that PUBLISHED this event "
        "(``AgentConfig.agent_id`` / ``AgentStatus.agent_id`` — see ``AgentId`` for the segment "
        "shape; NOT the full workflow_id) — stamped by the harness at publish time; producers never "
        "set it. Every event carries it so a merged multi-agent stream is self-describing: because "
        "the id is unique across the whole subagent tree, a consumer can filter to one agent's "
        "events, or group the merged stream by agent, with no risk of conflating two agents. For a "
        "subagent it equals the ``handle`` its parent references it by (the parent's own id plus a "
        "fresh segment, pushed down as the child's ``agent_id``), so a consumer can attribute child "
        "events to the subagent shown in the parent's status/events."
    )
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
