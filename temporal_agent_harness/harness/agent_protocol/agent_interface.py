# ABOUTME: The request/response contract between an agent workflow and its clients —
# the ``send_agent_message`` update and ``agent_status`` / ``agent_interface`` query
# names, and the plain dataclasses exchanged across those update/query/queue boundaries.
# The workflow must expose handlers under these exact names; the client addresses them by
# the same. A message is a name-routed tool-call envelope (``AgentMessage{type, payload}``):
# ``type`` names the target ``@agent.accepts`` handler; ``payload`` is that handler's input
# model JSON.

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ---------------------------------------------------------------------------
# Protocol constants — workflow must use these exact names
# ---------------------------------------------------------------------------

SEND_AGENT_MESSAGE_UPDATE = "send_agent_message"
TOOL_APPROVAL_UPDATE = "tool_approval"
PROVIDE_CALLBACK_RESULT_UPDATE = "provide_callback_result"
AGENT_STATUS_QUERY = "agent_status"
AGENT_INTERFACE_QUERY = "agent_interface"

# Width (hex chars) of ONE segment of an agent's short id. A top-level agent's id is a single
# segment; a subagent's id is its parent's id plus one fresh segment, joined by ``-`` (see
# ``AGENT_ID`` and ``AgentWorkflowRunner.start_subagent``) — short and cheap for a model to
# reproduce, replacing the full ``workflow_id`` as an agent's stream identity.
AGENT_ID_LENGTH = 6

# The constrained form a configured/stamped agent id must take: one or more ``AGENT_ID_LENGTH``-wide
# lowercase-hex segments joined by ``-`` (e.g. ``a1b2c3`` for a root, ``a1b2c3-d4e5f6`` for its
# subagent, ``a1b2c3-d4e5f6-…`` for deeper descendants). The ``-``-prefixing makes an id TREE-UNIQUE:
# each agent rerolls its own children's segments for in-registry uniqueness, and prefixing with the
# (already tree-unique) parent id extends that guarantee across the whole subagent tree — so a
# consumer can group/filter a merged multi-agent stream by ``agent_id`` without collisions. pydantic
# enforces this shape when an ``AgentConfig`` crosses the data converter into a workflow.
_AGENT_ID_SEGMENT = rf"[0-9a-f]{{{AGENT_ID_LENGTH}}}"
AgentId = Annotated[
    str, StringConstraints(pattern=rf"^{_AGENT_ID_SEGMENT}(-{_AGENT_ID_SEGMENT})*$")
]


# ---------------------------------------------------------------------------
# Tool-approval policy
# ---------------------------------------------------------------------------


@dataclass
class ToolApprovalContext:
    """The facts about a single tool call that a custom approval-policy fallback
    evaluates.

    Passed to the developer-supplied predicate given as the runner's
    ``custom_approval_fallback=`` constructor arg — the FINAL approval layer, consulted
    only when the serializable :class:`ToolApprovalPolicy` layers did not already
    auto-approve the call.

    ``tool_name`` is the tool's registered name; ``tool_input`` is the model-facing
    arguments (injected parameters excluded); ``inherently_safe`` is the tool's static
    self-assertion (the decorator's ``inherently_safe=``).
    """

    tool_name: str
    tool_input: dict[str, Any]
    inherently_safe: bool


class ToolApprovalPolicy(BaseModel):
    """Agent-level, serializable policy deciding which tool calls require human approval.

    Safe-by-default: with every field at its default the policy approves NOTHING
    automatically, so every tool call is gated (step-through). Relax it by opting into
    layers; :meth:`auto_approves` checks them in priority order:

      0. ``dangerously_skip_all_approvals`` — approve EVERYTHING (no call is ever gated).
         The name is a deliberate yellow-flag: this disables the guardrail entirely.
      1. ``auto_approve_inherently_safe`` — approve any tool that statically declared
         itself ``inherently_safe`` (a tool that is *never*, under any input, unsafe).
      2. ``auto_approve_tools`` — approve these specific tools by name (additive on top
         of the layers above).

    A call not approved by any layer here falls through to the runner's custom fallback
    (if one is set), and otherwise is gated. Whether a tool calls itself ``inherently_safe``
    is only ever a *hint*: this policy — not the tool — decides enforcement, so an operator
    can still gate everything regardless of what a tool claims.

    The model is ``frozen`` (value-like): produce the next policy by constructing a new
    one (see :meth:`with_tool_allowed`). Serializable on purpose — a caller may supply one
    at startup via ``AgentConfig.approval_policy`` (overriding the agent's built-in
    default), and the live policy is surfaced on ``AgentStatus.approval_policy`` so a
    client can persist it and replay it into the next session.
    """

    model_config = ConfigDict(frozen=True)

    dangerously_skip_all_approvals: bool = False
    auto_approve_inherently_safe: bool = False
    auto_approve_tools: frozenset[str] = frozenset()

    def auto_approves(self, tool_name: str, *, inherently_safe: bool) -> bool:
        """Whether THIS policy auto-approves the call (skips the human gate).

        Checks the layers in priority order (see the class docstring). Does NOT consult
        the runner's custom fallback — that is applied by the runner only after this
        returns ``False``.
        """
        if self.dangerously_skip_all_approvals:
            return True
        if inherently_safe and self.auto_approve_inherently_safe:
            return True
        return tool_name in self.auto_approve_tools

    def with_tool_allowed(self, tool_name: str) -> "ToolApprovalPolicy":
        """A copy of this policy with ``tool_name`` added to ``auto_approve_tools``.

        Backs the "approve, and stop asking me about this tool" flow (a ``tool_approval``
        decision with ``remember=True``) and any agent-driven runtime allow-listing.
        """
        return self.model_copy(
            update={"auto_approve_tools": self.auto_approve_tools | {tool_name}}
        )

    # -- Named presets (ergonomic constructors; all serialize to this one model) -----

    @classmethod
    def always_require_approvals(cls) -> "ToolApprovalPolicy":
        """Gate EVERY tool call — even inherently-safe ones (step-through). The
        safe-by-default baseline; equivalent to the all-defaults policy."""
        return cls()

    @classmethod
    def allow_inherently_safe(cls) -> "ToolApprovalPolicy":
        """Auto-approve tools that declared ``inherently_safe``; gate everything else."""
        return cls(auto_approve_inherently_safe=True)

    @classmethod
    def allow_tools(
        cls, tool_names: Iterable[str], *, also_inherently_safe: bool = False
    ) -> "ToolApprovalPolicy":
        """Auto-approve the named tools; optionally also auto-approve inherently-safe
        ones (additive)."""
        return cls(
            auto_approve_tools=frozenset(tool_names),
            auto_approve_inherently_safe=also_inherently_safe,
        )

    @classmethod
    def dangerously_skip_all(cls) -> "ToolApprovalPolicy":
        """Auto-approve EVERYTHING — no call is ever gated. Disables the guardrail."""
        return cls(dangerously_skip_all_approvals=True)


# ---------------------------------------------------------------------------
# Standardized agent input
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """The single, harness-defined input contract every agent workflow accepts.

    A pydantic model (not a plain dataclass) so its field constraints — notably the
    :data:`AgentId` shape on ``agent_id`` — are actually VALIDATED when an ``AgentConfig`` crosses
    the data converter into a workflow, rather than being mere annotations.

    The harness enforces a uniform construction shape: an agent ``@workflow.defn``
    class that builds an ``AgentWorkflowRunner`` must declare its ``run``/``__init__``
    to take EITHER no argument OR exactly one argument of this type. That invariant is
    asserted when the runner is built (see
    ``AgentWorkflowRunner._assert_standardized_agent_signature``).

    Standardizing the input is what lets any harness agent be substituted for another —
    as a top-level agent or as a sub-agent — since a caller can always construct one
    knowing only ``AgentConfig``, never a bespoke per-agent input type. Consequently
    this carries ONLY knobs universal to every agent; agent-specific behavior is
    configured at runtime through the agent's own ``@agent.accepts`` handlers, never
    through a custom input type.

    EVERY field is optional, with ``None`` meaning "the caller did not specify this."
    An agent supplies its own default for any unspecified field (via the matching
    ``AgentWorkflowRunner`` ``*_default`` constructor argument); a value the caller *did*
    specify is authoritative and an agent can never override it. So the effective value
    of each knob is: the caller's value if given, else the agent's default, else the
    harness baseline.

    Note there is no knob here for mid-turn behavior. Whether a message may interrupt,
    queue behind, or join an in-flight turn is declared PER HANDLER via
    ``@agent.accepts(mid_turn=...)`` — see :class:`MidTurn`. It is not universal to an
    agent, so it cannot live in this contract.

    ``approval_policy`` — the tool-approval policy to run the agent under (see
    :class:`ToolApprovalPolicy`). ``None`` → use the agent's built-in default (the runner's
    required ``approval_policy_default=`` constructor arg). A caller's
    policy is authoritative and overrides the agent's default — letting an operator, for
    example, start a session that gates every tool call regardless of the agent's default.
    The developer's separate *custom fallback* predicate is not part of this contract and
    is never overridable from the config (it is non-serializable).

    ``agent_id`` — the short, tree-unique id this agent stamps on every event it publishes (and
    reports on its ``agent_status`` query); see :data:`AgentId` for the segment shape. A PARENT sets
    this when starting a subagent — pushing down the same ``handle`` it uses to reference the child
    (its own id plus one fresh segment) — so the child's own event stream is labelled with the
    id the parent (and a UI merging the streams) already knows it by. ``None`` → a top-level agent
    generates its own single-segment id. This is the one ``AgentConfig`` field a parent populates
    per-child rather than passing through unchanged.
    """

    approval_policy: ToolApprovalPolicy | None = None
    agent_id: AgentId | None = None


# ---------------------------------------------------------------------------
# Typed message base
# ---------------------------------------------------------------------------


class AgentMessage(BaseModel):
    """The payload of the ``send_agent_message`` update — a tool-call envelope.

    ``type`` names the target ``@agent.accepts`` handler (its function name, also the
    handler's tool name in :class:`AcceptedFunction`); ``payload`` is the JSON of that
    handler's declared input model. The runner resolves the handler by ``type`` and
    ``model_validate``s ``payload`` into the handler's input model (coercing it, or
    rejecting a bad shape) before dispatching::

        AgentMessage(type="set_model",
                     payload={"model": "gemini-3.1-flash-lite"},
                     expected_turn=1)

    Routing is **by name**, not by a discriminator on the payload type — so two handlers
    may accept the *same* input model.

    ``expected_turn`` is a STALENESS TOKEN: the caller asserting how far it has observed,
    expressed as the next turn number the agent should hand out
    (``current_turn + len(pending_turns) + 1``). The update validator rejects the message if
    the workflow is not at exactly that point.

    It is deliberately not a slot reservation, and **a caller must not simply increment it
    once per message sent.** A message whose handler declares :attr:`MidTurn.ACCEPT` JOINS a
    turn already in flight instead of getting one of its own, so the agent's turn counter
    does not advance and the *next* message must claim the same number again. Counting sends
    rather than turns over-counts on the first join and then rejects every later message as
    stale.

    The reliable source is :attr:`AgentMessageReply.turn_number`, returned on every accepted
    message: set the next ``expected_turn`` to ``reply.turn_number + 1``, which is correct
    whether the message joined a turn (its number) or reserved one (the new slot). A caller
    that has lost track can always re-derive it from ``agent_status``.

    The check is turn-granular and therefore coarse — it will not catch a caller acting during a turn
    it has not finished observing. That is accepted deliberately: asserting a stream offset
    instead would fail on nearly every busy-agent interaction (the offset advances on every
    streamed delta), and a check people learn to retry past protects nothing.

    It is carried on the envelope itself — the ``send_agent_message`` update takes a bare
    :class:`AgentMessage`, with no separate wrapper.
    """

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_turn: int


# ---------------------------------------------------------------------------
# Built-in free-text input/output models
# ---------------------------------------------------------------------------
#
# There is no implicit ``str`` channel anymore — every accepted message is a
# ``@agent.accepts`` handler with a pydantic input + output. These two models keep the
# common "free text in, free text out" agent trivial: declare
# ``async def ask(self, msg: TextMessage) -> TextReply``.


class TextMessage(BaseModel):
    """Free-form natural-language text from the user (the input to a plain chat handler)."""

    text: str


class TextReply(BaseModel):
    """A free-form natural-language reply (the output of a plain chat handler)."""

    text: str


# ---------------------------------------------------------------------------
# Mid-turn dispatch behavior + the context a handler can ask for
# ---------------------------------------------------------------------------


class MidTurn(StrEnum):
    """What happens to a message that arrives while a turn is already open.

    Declared per handler via ``@agent.accepts(mid_turn=...)`` — there is no global switch,
    because "a control message must work on a busy agent" and "a user message should queue"
    are the same decision made oppositely, and no single answer serves both.

    All three are IDENTICAL when the agent is idle: the message opens a turn and runs. The
    mode governs only mid-turn arrival.

      * :attr:`ENQUEUE` — queue behind the open turn; it runs as its own turn later.
      * :attr:`REJECT` — fail the ``send_agent_message`` update with a typed
        ``MidTurnRejected`` error, so the caller learns immediately rather than having work
        pile up invisibly behind a turn it cannot see.
      * :attr:`ACCEPT` — JOIN the open turn and run concurrently with it, sharing its
        ``turn_id``. The turn ends when its last participant finishes.

    ``ACCEPT`` is the one that carries a real obligation: such a handler runs concurrently
    with the open turn AND with other ``ACCEPT`` handlers, so any agent state it mutates is
    shared. A read-modify-write split across an ``await`` can lose an update. The harness's
    own state is safe by construction (its registries are partitioned by unique id and its
    status mutators never yield), but agent state is the author's to protect.

    ``REJECT`` is the default precisely because it is the only mode whose consequence is
    visible at the call site during development.
    """

    ENQUEUE = "enqueue"
    REJECT = "reject"
    ACCEPT = "accept"


class MessageContext(BaseModel):
    """Per-message context the WORKFLOW supplies to a handler that asks for it.

    A handler opts in by declaring a second parameter annotated
    ``Injected[MessageContext]``; it is filled at dispatch and hidden from the
    ``parameters`` schema every caller introspects, so the model never sees or chooses it::

        @agent.accepts(mid_turn=MidTurn.ACCEPT)
        async def user_text(self, msg: UserText, ctx: Injected[MessageContext]) -> Reply:
            if ctx.joined_turn:
                self._steering.append(msg.text)   # drained by the running model loop
            else:
                return await self._prompt(msg.text)

    ``joined_turn`` is the distinction most handlers care about: ``False`` when this message
    OPENED the turn it is running in, ``True`` when it joined a turn that was already open
    (only possible for :attr:`MidTurn.ACCEPT`). The motivating case is a chat box that should
    start a new prompt when idle but *steer* the model mid-turn.

    ``turn_id`` / ``turn_number`` identify the turn this message is participating in — the
    same values the caller received from ``send_agent_message`` — so a handler can correlate
    its own work with the event stream without reaching into the runner.
    """

    joined_turn: bool
    turn_id: str
    turn_number: int


# ---------------------------------------------------------------------------
# Wire types — shared between workflow and client
# ---------------------------------------------------------------------------


@dataclass
class AgentMessageReply:
    """Returned by the workflow's ``send_agent_message`` update on acceptance.

    ``turn_number`` is the turn this message actually belongs to — a NEW turn for a queued or
    idle message, or the ALREADY-OPEN turn it joined when its handler declares
    :attr:`MidTurn.ACCEPT`. It is the authoritative input to the caller's next
    ``expected_turn`` (``turn_number + 1``); see :attr:`AgentMessage.expected_turn` for why
    counting sends instead is wrong.

    ``pending`` is True if the message was queued behind an active turn rather than being
    processed immediately. A join reports ``False``: it is running now, inside the turn it
    joined.

    ``accepted_offset`` is the agent's stream offset captured at the instant the update was
    accepted (the log head BEFORE this turn publishes anything). It is internal plumbing for the
    client's stream-merge: a caller starts reading the merged logical stream from here and
    discards events until this turn's ``turn_started`` — a quiescent point with no in-flight
    subagent brackets. It is a read-start *hint* (its only requirement is to be ``<=`` this
    turn's ``turn_started`` offset, which capture-at-acceptance guarantees); the BFF/UI never
    sees or stores it. For a queued message it is the head mid the active prior turn; the
    skip-to-``turn_started`` preamble normalizes that.
    """

    turn_number: int
    turn_id: str
    accepted_offset: int = 0
    pending: bool = False


@dataclass
class ToolApprovalDecision:
    """Update payload sent to the workflow to resolve a pending tool approval.

    ``tool_id`` is the id carried by the :class:`ToolApprovalRequested` event (and listed
    under :attr:`AgentStatus.pending_approvals`). ``approved`` is the human's decision;
    ``reason`` is an optional note surfaced on the resolution event and, on denial, fed
    back to the model as the tool's error result. The workflow's update validator rejects
    a decision for an unknown ``tool_id`` or one already resolved (idempotent — a
    double-submit fails rather than flipping a settled decision).

    ``remember`` applies the decision to *future* calls of the same tool too: an
    ``approved`` + ``remember`` decision adds the tool to the live
    :class:`ToolApprovalPolicy`'s allow-list, so the agent stops asking about it (and any
    other call of that tool currently waiting auto-resolves). This is the "approve, and
    don't ask me about this tool again" affordance. ``remember`` is a no-op on denial for
    now (there is no deny-list yet).
    """

    tool_id: str
    approved: bool
    reason: str | None = None
    remember: bool = False


@dataclass
class ToolApprovalResult:
    """Returned by the workflow's ``tool_approval`` update once the decision is recorded.

    ``accepted`` is always True on a successful return (the update validator rejects
    anything that should not be recorded before the handler runs); it exists so the
    contract has an explicit, evolvable acknowledgement payload.
    """

    tool_id: str
    accepted: bool = True


@dataclass
class CallbackResult:
    """Update payload sent to the workflow to fulfill a pending callback tool call.

    A callback tool (``@agent.callback_tool_defn``) has no worker-side implementation: it
    pauses in-workflow and an attached client executes it on its own machine, then submits
    the outcome through the ``provide_callback_result`` update.

    ``tool_id`` is the id carried by the :class:`CallbackRequested` event (and listed under
    :attr:`AgentStatus.pending_callbacks`). Exactly one of ``result`` / ``error`` is meaningful:

      * ``result`` — the value the client produced, as JSON-native data (a dict for a pydantic
        model output, or a scalar/list). The workflow validates it against the tool's declared
        output type; a mismatch rejects the update (without consuming the one-shot gate) so the
        client can correct and resubmit. On success it becomes the tool's return value.
      * ``error`` — set instead when the client could not fulfill the call (e.g. file not found,
        permission denied). The call fails and the model receives this as the tool's error
        result, rather than crashing the turn.

    The workflow's update validator rejects a submission for an unknown ``tool_id`` or one
    already resolved (idempotent — a double-submit fails rather than overwriting a settled
    result).
    """

    tool_id: str
    result: Any = None
    error: str | None = None


@dataclass
class CallbackResultAck:
    """Returned by the workflow's ``provide_callback_result`` update once the result is recorded.

    ``accepted`` is always True on a successful return (the update validator rejects anything
    that should not be recorded — unknown id, already resolved, or a result that fails output-type
    validation — before the handler runs); it exists so the contract has an explicit, evolvable
    acknowledgement payload.
    """

    tool_id: str
    accepted: bool = True


@dataclass
class PendingApproval:
    """A gated tool call awaiting a human decision, surfaced via ``agent_status``.

    Lets a client that attaches after the :class:`ToolApprovalRequested` event was
    published still discover and act on outstanding approvals. ``tool_input`` is the
    model-facing input (injected parameters excluded).
    """

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    turn_number: int


@dataclass
class PendingCallback:
    """A callback tool call awaiting a client-supplied result, surfaced via ``agent_status``.

    Lets a client that attaches after the :class:`CallbackRequested` event was published still
    discover and fulfill outstanding callback calls. ``tool_input`` is the model-facing input
    (injected parameters excluded); ``output_schema`` is the JSON schema of the result the
    client must return.
    """

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]
    output_schema: dict[str, Any]
    turn_number: int


@dataclass
class PendingTurn:
    """A message waiting in the agent's queue."""

    turn_number: int
    turn_id: str
    message: str


@dataclass
class SubagentInfo:
    """An active subagent this agent is driving, surfaced via ``agent_status``.

    ``subagent_id`` is the short id the agent references it by (the same id the subagent stamps as
    its own ``agent_id``, and the value on the parent's ``subagent_started`` / ``subagent_message_sent``
    / ``subagent_reply_received`` events); ``workflow_id`` is the real child workflow (for an
    operator/UI to drill into). ``next_expected_turn`` reflects how many turns it has run (its next
    is ``next_expected_turn``). The caller-side FIFO gate's internals (its ticket counters) are
    deliberately NOT surfaced — they are an implementation detail of turn ordering, not agent status.
    """

    subagent_id: str
    agent_key: str
    workflow_id: str
    next_expected_turn: int


@dataclass
class AgentStatus:
    """Queryable status of the agent workflow.

    The workflow exposes this via the ``agent_status`` query. It is the
    single source of truth for ``attach()``'s termination decision and
    for the client to compute ``expected_turn`` before sending.

    ``agent_id`` is this agent's own short id — the value it stamps on every event it publishes
    (:attr:`AgentEvent.agent_id`). A consumer reads it to map a session's events to the agent, and
    a parent can cross-reference it against the ``handle`` it pushed down for a subagent.
    """

    # Empty only as a dataclass default; a live workflow's status always carries its real id.
    agent_id: str = ""
    current_turn: int = 0
    # True while a turn is open — i.e. while ``turn_participants > 0``. A turn is the
    # interval the agent is non-idle, not the span of one message, so this stays True while
    # ANY participant is still running (see ``turn_participants``).
    turn_active: bool = False
    # How many messages are currently running inside the open turn. Normally 1; more when
    # ``MidTurn.ACCEPT`` messages have joined it. 0 exactly when the agent is idle. The turn
    # closes — and ``turn_end`` publishes — when this reaches 0, which is what makes
    # ``turn_active`` an honest answer rather than "the first participant is still going".
    turn_participants: int = 0
    pending_turns: list[PendingTurn] = field(default_factory=list)
    pending_approvals: list[PendingApproval] = field(default_factory=list)
    # Callback tool calls currently awaiting a client-supplied result (its own machine executes
    # them), each a :class:`PendingCallback`. Distinct from ``pending_approvals``: an approval is a
    # human gate BEFORE a tool runs; a pending callback is a client fulfilling the tool's body.
    pending_callbacks: list[PendingCallback] = field(default_factory=list)
    # Subagents this agent is currently driving (its own child agents), each a
    # :class:`SubagentInfo` — short subagent id, agent key, real child workflow id, next turn. Gate
    # internals are excluded by construction.
    subagents: list[SubagentInfo] = field(default_factory=list)
    # The tool-approval policy the agent is currently running under. A client can read
    # this (e.g. after a runtime update) and persist it to replay into a later session
    # via ``AgentConfig.approval_policy``. ``has_custom_approval_fallback`` reports only
    # *whether* a developer fallback predicate is wired (the predicate itself is
    # non-serializable, so it is not — and cannot be — surfaced here).
    approval_policy: ToolApprovalPolicy = field(
        default_factory=ToolApprovalPolicy.always_require_approvals
    )
    has_custom_approval_fallback: bool = False


class AcceptedFunction(BaseModel):
    """One ``@agent.accepts`` handler the agent exposes, described tool-style.

    The element type of the ``agent_interface`` query result (a ``list[AcceptedFunction]``)
    — an agent-level analogue of MCP's ``list_tools`` / a model's function declarations,
    announcing the callable surface this agent accepts. Gemini-tool-shaped:

      * ``name`` — the handler's function name; the value a caller puts in
        :attr:`AgentMessage.type`, and the tool name when this agent is wired as a subagent.
      * ``description`` — the handler method's docstring (when/how to use it).
      * ``parameters`` — the JSON schema of the handler's single input model.
      * ``output`` — the JSON schema of the handler's return model.
      * ``mid_turn`` — what happens if this message arrives while a turn is open, so a
        client can tell the user whether sending will queue, join, or be rejected.
      * ``model_callable`` — the handler author's HINT that a parent agent's model may
        drive this handler. Only a hint: the parent's ``SubagentToolPolicy`` decides, and
        may honor it, narrow it, or ignore it. Never treat this as an access decision.

    This query returns EVERY handler the agent declares. Nothing is hidden from it — the
    real boundary on what a parent model can reach is which tools its toolset was built
    with, not what a discovery query admits to. The two harness-owned updates
    (``tool_approval``, ``provide_callback_result``) are absent because they are not
    ``@agent.accepts`` handlers at all.

    A caller introspects this to construct a valid :class:`AgentMessage` for any handler
    (or, for a parent agent, to model each handler as a tool) without hardcoding the
    contract, so it can evolve without client-side changes. A generic client can render an
    arbitrary agent from this alone: ``parameters`` fully describes the payload, so no
    per-agent knowledge — and no hardcoded handler name — is needed.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    output: dict[str, Any]
    mid_turn: MidTurn = MidTurn.REJECT
    model_callable: bool = True
