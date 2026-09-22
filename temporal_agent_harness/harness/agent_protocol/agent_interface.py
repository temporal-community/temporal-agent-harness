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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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

# Width (hex chars) of a pending approval's ``short_id`` — the same 6 as ``AGENT_ID_LENGTH``,
# for the same reason: an id a human or a constrained wire field can carry, standing in for the
# provider-minted ``tool_id`` that is too long to embed. See :attr:`PendingApproval.short_id`.
APPROVAL_SHORT_ID_LENGTH = 6


# ---------------------------------------------------------------------------
# Tool-approval policy
# ---------------------------------------------------------------------------


class AutoApprovalCriteriaSet(BaseModel):
    """One NAMED, reusable set of auto-mode rules. Not bound to any tool.

    The unit an operator writes and a tool is judged against. A set describes a KIND of
    call — "read-only lookups", "anything that moves money" — and tools are pointed at it
    by name (see :class:`AutoApprovalCriteria`). That indirection is the whole design:
    the rules are written once and shared, a tool's code carries only a label, and the
    person whose security posture the rules encode can redefine what a label MEANS without
    touching the agent.

    ``effect`` is prose describing what calls judged by this set actually DO — the blast
    radius, and whether the effect can be taken back. It is the one field worth filling in
    even when no rules follow it, because it is what lets an evaluator reason about a call
    whose arguments no rule obviously covers.

    The three ``*_when`` lists are rules in plain language, one rule per entry, each
    describing a kind of call rather than naming an argument value. Lists rather than one
    paragraph so a rule can be added or removed without rewriting the prose around it, and
    so a client that lets a user edit their own posture can render them as rows.

    ASYMMETRIC ON PURPOSE, and the asymmetry is the safety argument: ``deny_when`` and
    ``escalate_when`` can only ever make a call HARDER to approve, while ``approve_when``
    describes what a routine call looks like and takes effect only inside what the agent's
    :class:`ToolApprovalPolicy` already permits. Auto mode sits BELOW the policy and
    cannot widen it, so a permissive rule here is a description of normal usage, never a
    grant.

    ``min_confidence`` and ``escalate_if_irreversible_above`` are the posture in numbers,
    and they are PER SET on purpose: a refund and a lookup should not share one confidence
    bar. ``None`` on either means "take the agent-wide value" from
    :class:`AutoApprovalCriteria`, so a set opts into a stricter bar only when it needs one.
    An evaluator that cannot produce a calibrated probability should treat
    ``min_confidence`` as a bar it must clear some other way, or escalate.
    """

    model_config = ConfigDict(frozen=True)

    effect: str | None = None
    approve_when: tuple[str, ...] = ()
    deny_when: tuple[str, ...] = ()
    escalate_when: tuple[str, ...] = ()
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    escalate_if_irreversible_above: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def is_empty(self) -> bool:
        """Whether this carries no rule and no effect an evaluator could act on.

        The thresholds are deliberately not counted: a bar with nothing to apply it to
        decides nothing, so a set holding only numbers is still nothing to judge against.
        """
        return not (self.effect or self.approve_when or self.deny_when or self.escalate_when)


class AutoApprovalCriteria(BaseModel):
    """The agent's auto-mode rulebook: named criteria sets, and which set judges each tool.

    The *criteria* half of auto mode. An
    :data:`~temporal_agent_harness.harness.agent_workflow.AutoModeEvaluator` is the
    MECHANISM — code, possibly a model, that answers approve / deny / escalate — and this
    is the RULEBOOK it applies. Separating them is what makes the evaluator swappable: a
    Jev-backed evaluator, an LLM one, and a hand-rolled rules engine all read the same
    criteria off :class:`AutoApprovalContext`, so an operator's rules survive a change of
    evaluator and a client that edits them needs to know nothing about which one is wired.

    CONFIGURATION, NOT CODE — deliberately, and exactly like :class:`ToolApprovalPolicy`:
    supplied at startup as the runner's ``auto_approval_criteria_default=``, overridable
    per session by the caller via :attr:`AgentConfig.auto_approval_criteria`, and swapped
    at runtime by
    :meth:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner.set_auto_approval_criteria`
    or retargeted one tool at a time by
    :meth:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner.assign_tool_criteria`.
    That matters because the person whose security posture these rules encode is usually
    the END USER of the product the agent powers, not its author — and their posture is
    not knowable when the agent is written.

    Three parts:

      * :attr:`sets` — the rulebook. :class:`AutoApprovalCriteriaSet` keyed by a name the
        operator chooses (``"read_only"``, ``"financial"``, ``"destructive"``).
      * :attr:`tools` — which set judges which tool, keyed by tool name. Addressing tools
        BY NAME here rather than only on the decorator is what lets these assignments cover
        tools the agent author never wrote: an MCP server's tools arrive already defined,
        with no harness decorator to hang anything on, and an end user setting their own
        posture at runtime cannot edit a decorator at all.
      * :attr:`default` — the name of the catch-all set, used for any tool with no
        assignment and no declared default of its own.

    ``context`` is free-form JSON shown to the evaluator alongside the call — the acting
    user, the environment, the tenant, whatever the rules actually turn on. It is
    serializable session configuration; for facts that must be read from live agent state
    on each call, use the evaluator's own hook (``jev_evaluator(extra_state=...)``).

    EMPTY CRITERIA MEAN "ASK A PERSON", never "approve". When nothing resolves for a call
    there is nothing to judge against, so a well-behaved evaluator escalates immediately —
    without spending a model call — and :meth:`for_tool` returning ``None`` is how it
    tells. That makes the default value safe: enabling auto mode without configuring
    criteria changes nothing about which calls reach the human gate.

    NOT SURFACED ON ``AgentStatus``, unlike the policy. A tool's *declared* default set is
    a decorator argument living in code, and the runner holds no tool registry to enumerate
    those from — so any map the agent published would be silently incomplete, which is
    worse than publishing none. A client is anyway the source of truth for criteria it
    supplied in ``AgentConfig``. Whether a call's verdict was reached under these rules at
    all is still readable: ``AgentStatus.approval_policy.auto_mode_enabled`` says
    whether auto mode is on, and each evaluation records the set it applied.

    The model is ``frozen`` (value-like): produce the next criteria with :meth:`with_set`
    or :meth:`with_tool_assigned`.
    """

    model_config = ConfigDict(frozen=True)

    sets: dict[str, AutoApprovalCriteriaSet] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)
    default: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    escalate_if_irreversible_above: float | None = Field(default=0.5, ge=0.0, le=1.0)

    def set_name_for(self, tool_name: str, *, declared: str | None = None) -> str | None:
        """Which criteria set NAME governs ``tool_name``, highest precedence first.

        1. :attr:`tools` — what config or a runtime update assigned;
        2. ``declared`` — the name the tool's decorator picked, passed in by the gate
           (the runner holds no tool registry, so a declared default rides the call);
        3. :attr:`default` — the catch-all.

        Returns the name even when it resolves to nothing in :attr:`sets`, so a caller can
        tell "nobody assigned a set" apart from "the assigned set does not exist" — the
        second is a typo worth reporting, and :meth:`for_tool` is what turns either into a
        safe escalate.
        """
        return self.tools.get(tool_name) or declared or self.default

    def for_tool(
        self, tool_name: str, *, declared: str | None = None
    ) -> AutoApprovalCriteriaSet | None:
        """The criteria set that judges ``tool_name``, or ``None`` if there is nothing to
        judge against.

        ``None`` covers every way that can happen — no assignment anywhere, an assignment
        naming a set that is not registered, or a set that is registered but empty — because
        all three mean the same thing to an evaluator: escalate, and do not spend a model
        call finding out. Use :meth:`set_name_for` when the distinction matters for
        diagnostics.
        """
        name = self.set_name_for(tool_name, declared=declared)
        if name is None:
            return None
        found = self.sets.get(name)
        if found is None or found.is_empty:
            return None
        return found

    def thresholds_for_set(
        self, criteria_set: AutoApprovalCriteriaSet | None
    ) -> tuple[float, float | None]:
        """``(min_confidence, escalate_if_irreversible_above)`` in force for a resolved set.

        The set's own values when it set them, else the agent-wide ones. The ONE place that
        fallback lives, so every evaluator applies the operator's numbers the same way.
        """
        if criteria_set is None:
            return self.min_confidence, self.escalate_if_irreversible_above
        return (
            self.min_confidence
            if criteria_set.min_confidence is None
            else criteria_set.min_confidence,
            self.escalate_if_irreversible_above
            if criteria_set.escalate_if_irreversible_above is None
            else criteria_set.escalate_if_irreversible_above,
        )

    def thresholds_for(
        self, tool_name: str, *, declared: str | None = None
    ) -> tuple[float, float | None]:
        """``(min_confidence, escalate_if_irreversible_above)`` in force for ``tool_name``.

        For offline analysis — re-deriving what a past call would have been judged under.
        The gate itself resolves the set once and uses
        :attr:`AutoApprovalContext.thresholds`."""
        return self.thresholds_for_set(self.for_tool(tool_name, declared=declared))

    def with_set(self, name: str, criteria: AutoApprovalCriteriaSet) -> "AutoApprovalCriteria":
        """A copy with the set ``name`` registered (replacing any already there).

        Redefining a set changes what its label MEANS for every tool pointed at it — which
        is the point of the indirection, and the reason a runtime posture change usually
        needs this rather than touching tools one by one.
        """
        return self.model_copy(update={"sets": {**self.sets, name: criteria}})

    def with_tool_assigned(self, tool_name: str, set_name: str) -> "AutoApprovalCriteria":
        """A copy with ``tool_name`` judged by the set ``set_name``.

        The value-like way to retarget ONE tool — including one defined elsewhere, e.g.
        over MCP, which a decorator could never serve.
        """
        return self.model_copy(update={"tools": {**self.tools, tool_name: set_name}})


class AutoApprovalVerdict(StrEnum):
    """What an auto mode evaluator decided about one gated tool call.

    Three-valued on purpose. An evaluator is not merely an "extra yes": it is a
    *decision-maker*, and the interesting case — an AI auto-approver judging a call it has
    never seen before — needs to be able to say "no" as loudly as it says "yes", and to
    abstain when it is not sure enough to say either.

      * ``APPROVE`` — dispatch the call now, with no human gate.
      * ``DENY`` — the call ends here. It never executes; the tool raises
        :class:`~temporal_agent_harness.harness.agent_workflow.ToolApprovalDenied`, which
        the agent loop surfaces to the model as an error result, so the turn continues.
      * ``ESCALATE`` — abstain. The call stays PENDING and waits for a human decision on
        the ``tool_approval`` update, exactly as if auto mode were off.

    ``ESCALATE`` — not ``DENY`` — is the safe answer to "I don't know": denying on
    uncertainty trains operators to disable the guardrail, while escalating puts the call
    in front of the person who can actually judge it.
    """

    APPROVE = "approve"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass
class AutoApprovalDecision:
    """An auto mode evaluator's verdict on one gated call, plus why.

    The one shape an auto mode evaluator returns. A bare ``bool`` is deliberately NOT
    accepted: ``False`` would have to mean "escalate", not "deny", and a two-shaped
    contract whose falsy value means neither no nor yes is exactly the ambiguity the
    three-valued verdict exists to remove.

    ``reason`` is the one-line summary. It is published on this evaluation's
    :class:`~temporal_agent_harness.harness.agent_protocol.events.AutoApprovalEvaluationEnded`
    and, when the verdict settles the gate, on the resulting
    :class:`~temporal_agent_harness.harness.agent_protocol.events.ToolApprovalResolved` —
    so it is the audit trail for a decision no human made. Supply one, especially on
    ``DENY``, where it is also the error text the model sees and therefore the model's only
    chance to understand what it did wrong and try something allowed instead.

    ``details`` is the structured form of the same thing, free-form by design so an
    evaluator can record whatever makes its decision reviewable later. It is published on
    ``AutoApprovalEvaluationEnded`` for EVERY verdict, including ``ESCALATE`` — which
    resolves nothing and therefore has no other trace at all. Record the raw judgments AND
    the thresholds applied to them, not just the conclusion: that is what lets a stored
    decision be re-read against different thresholds without re-running inference. It lands
    in workflow history on every gated call, so keep it to what an audit would actually
    read.
    """

    verdict: AutoApprovalVerdict
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutoApprovalContext:
    """The facts about a single tool call that an auto mode evaluator judges.

    Passed to the evaluator given as the runner's ``auto_mode_evaluator=`` constructor
    arg — auto mode, layer 3 — and reached only for calls the
    :class:`ToolApprovalPolicy` layers did not already auto-approve AND whose policy has
    ``auto_mode_enabled``.

    THE SCHEMA EVERY EVALUATOR READS. Everything an implementation needs to decide is here,
    which is what makes them interchangeable: a Jev-backed evaluator, an LLM one, and a
    hand-rolled rules engine all take this one argument, so swapping one for another
    strands no configuration and needs no change to the tools or the policy.

    ``tool_name`` is the tool's registered name; ``tool_input`` is the model-facing
    arguments (injected parameters excluded); ``inherently_safe`` is the tool's static
    self-assertion (the decorator's ``inherently_safe=``), a hint the policy may already
    have acted on and never an instruction to this layer; ``tool_description`` is the
    tool's docstring — the same prose the model was shown when it chose to make this call,
    and the main thing an AI approver has to reason about what the call actually does.

    ``criteria_set`` is THE RULES THAT GOVERN THIS CALL, already resolved by the harness,
    and ``criteria_set_name`` is the name they were registered under. Both are REQUIRED and
    non-optional, which encodes a guarantee an evaluator may rely on absolutely:

        **The harness only ever invokes an evaluator for a call that matched a configured
        criteria set** — a per-tool assignment, the tool's own declared default, or the
        catch-all. A call that matched none is escalated to the human gate by the harness
        itself, and no evaluator is called at all.

    That enforcement lives in the gate (see
    ``AgentWorkflowRunner._auto_mode_context``) rather than in any evaluator,
    because it is a safety property of auto mode and not a courtesy each implementation
    should have to remember. An `Optional` here would invite exactly the defensive
    ``if ctx.criteria_set is None: escalate`` branch that an LLM-backed or hand-rolled
    evaluator could forget — or, worse, get wrong in the approving direction.

    ``criteria`` is the agent's LIVE :class:`AutoApprovalCriteria` — the whole rulebook as
    of this call, for the agent-wide ``context`` and as the fallback behind the governing
    set's thresholds. An evaluator should judge against ``criteria_set``, not go looking
    through ``criteria`` for other tools' rules.

    ``criteria_version`` counts how many times the criteria have been swapped at runtime
    (0 = still the ones the session started with). An evaluator should record it alongside
    its verdict: criteria change over the life of an agent, so "which rules decided this
    call" is only answerable if the decision names the generation it applied.
    """

    tool_name: str
    tool_input: dict[str, Any]
    inherently_safe: bool
    tool_description: str | None = None
    criteria: AutoApprovalCriteria = field(default_factory=AutoApprovalCriteria)
    criteria_version: int = 0
    # Keyword-only so they can be REQUIRED while following the defaulted fields above.
    # Required is the point: see the guarantee in the class docstring.
    criteria_set: AutoApprovalCriteriaSet = field(kw_only=True)
    criteria_set_name: str = field(kw_only=True)

    @property
    def thresholds(self) -> tuple[float, float | None]:
        """``(min_confidence, escalate_if_irreversible_above)`` in force for this call —
        the governing set's own numbers when it set them, else the agent-wide ones."""
        return self.criteria.thresholds_for_set(self.criteria_set)


class ToolApprovalPolicy(BaseModel):
    """Agent-level, serializable policy deciding which tool calls require human approval.

    THE WHOLE STORY: a tool call is either allow-listed by this policy, or it goes to a human.
    That is the only branch. There are three ways to allow-list, and a call that none of them
    covers lands on the ``tool_approval`` update for a person to answer:

      1. ``auto_approve_inherently_safe`` — statically, by the tool's own claim that it is
         *never* unsafe under any input.
      2. ``auto_approve_tools`` — statically, by name. This is what "approve, and stop asking
         me about this tool" grows at runtime; see :meth:`with_tool_allowed`.
      3. ``auto_mode_enabled`` — DYNAMICALLY, per call, by the agent's auto mode evaluator
         judging it against the operator's criteria. Auto mode is an allow-list mechanism like
         the other two, just one that reads the arguments instead of only the name — and the
         only one that can also DENY a call outright rather than merely decline to allow it.

    Plus one escape hatch that is not allow-listing at all:

      0. ``dangerously_skip_all_approvals`` — nothing is ever gated. The name is a deliberate
         yellow-flag: this disables the guardrail rather than scoping it.

    A call auto mode declines to approve — ``ESCALATE``, a verdict below the confidence bar, an
    evaluator failure, a tool with no criteria configured — is simply a call that was not
    allow-listed, so it goes to a human exactly as if auto mode were off. Enabling auto mode
    can therefore only reduce how many calls reach a person; it never removes the person.

    SAFE BY DEFAULT: with every field at its default there is no allow-listing of any kind, so
    every call goes to a human (step-through). That state is :meth:`always_require_human_approval`,
    and it is the absence of settings rather than a mode of its own — which is why it cannot be
    "combined" with auto mode. Auto mode IS allow-listing; a policy doing it is not a policy
    that sends everything to a human. Turning it on means you have chosen a different policy.

    The model is ``frozen`` (value-like) and ``extra="forbid"``: produce the next policy by
    constructing a new one — :meth:`with_changes` for any field, :meth:`with_tool_allowed` and
    :meth:`with_auto_mode` for the two common single-field edits. Never ``model_copy``, which
    skips the cross-field validator below. Extras are
    forbidden because a silently-ignored field in a SECURITY policy is the worst kind of typo —
    ``ToolApprovalPolicy(auto_mode_enbaled=True)`` would otherwise hand back a policy with auto
    mode OFF while the caller believed they had turned it on. Unknown keys raise instead.

    Serializable on purpose — a caller may supply one at startup via
    ``AgentConfig.approval_policy`` (overriding the agent's built-in default), and the live
    policy is surfaced on ``AgentStatus.approval_policy`` so a client can persist it, replay it
    into the next session, and show a user whether auto mode is currently on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dangerously_skip_all_approvals: bool = False
    auto_approve_inherently_safe: bool = False
    auto_approve_tools: frozenset[str] = frozenset()
    auto_mode_enabled: bool = False

    @model_validator(mode="after")
    def _reject_vacuous_auto_mode(self) -> "ToolApprovalPolicy":
        """Auto mode over ``dangerously_skip_all_approvals`` is unrepresentable.

        Layer 0 approves every call outright, so nothing is ever gated for auto mode to
        decide — the switch would be on while no call could ever reach an evaluator. That is
        worse than an error: the policy READS as though a machine were guarding something,
        and both a human reading it and a client rendering it would be misled.

        Enforced on the TYPE rather than by whoever builds a policy. An invalid combination
        of fields is the model's business, not something every message handler, config
        loader, and UI should have to re-check and be trusted to get right.
        """
        if self.dangerously_skip_all_approvals and self.auto_mode_enabled:
            raise ValueError(
                "dangerously_skip_all_approvals=True approves every call outright, so no "
                "call is ever gated and auto mode could never be consulted. Set one or the "
                "other: skip approvals entirely, or enable auto mode over a policy that "
                "actually gates."
            )
        return self

    def auto_approves(self, tool_name: str, *, inherently_safe: bool) -> bool:
        """Whether layers 0-2 auto-approve the call outright — dispatch with no gate and no
        approval events at all.

        Checks those layers in priority order (see the class docstring). Deliberately says
        nothing about AUTO MODE: an auto-mode verdict takes a round-trip to reach, can be
        a DENY, and is published as a normal requested/resolved pair, so it cannot be
        folded into this cheap synchronous boolean. Ask :meth:`consults_auto_mode`
        for that layer.
        """
        if self.dangerously_skip_all_approvals:
            return True
        if inherently_safe and self.auto_approve_inherently_safe:
            return True
        return tool_name in self.auto_approve_tools

    def consults_auto_mode(self, tool_name: str, *, inherently_safe: bool) -> bool:
        """Whether a call this policy did not auto-approve should be put to the agent's
        auto mode evaluator (layer 3) rather than straight to the human gate.

        False whenever layers 0-2 already approved the call, so an ungated call never pays
        for an evaluation it cannot act on. Otherwise it is simply
        :attr:`auto_mode_enabled` — auto mode is on or off, agent-wide, and which
        RULES apply to a given tool is :class:`AutoApprovalCriteria`'s business, not this
        policy's. Keeping the two apart is what lets an operator toggle the machine's
        involvement without rewriting their rules, and rewrite their rules without
        toggling the machine.
        """
        if self.auto_approves(tool_name, inherently_safe=inherently_safe):
            return False
        return self.auto_mode_enabled

    def with_changes(
        self,
        *,
        dangerously_skip_all_approvals: bool | None = None,
        auto_approve_inherently_safe: bool | None = None,
        auto_approve_tools: Iterable[str] | None = None,
        auto_mode_enabled: bool | None = None,
    ) -> "ToolApprovalPolicy":
        """A copy with the named fields changed, RE-VALIDATED. ``None`` leaves one as-is.

        THE general way to derive the next policy, and the reason there is no need to reach
        for ``model_copy``. Pydantic skips validators on a copy, so ``model_copy(update=...)``
        on a model with a cross-field invariant punches a hole straight through it —
        ``dangerously_skip_all().model_copy(update={"auto_mode_enabled": True})`` yields the
        vacuous policy :meth:`_reject_vacuous_auto_mode` exists to refuse. Reconstructing
        instead means every route into a policy value runs the same validation, including the
        value-like helpers below, which are just this method with one field named.

        Every field is reachable from here ON PURPOSE. A partial set of single-field helpers
        would leave whoever needs the remaining field with ``model_copy`` and no warning that
        it skips the invariant — so the safe path has to cover the whole model, not the cases
        that happened to come up first.

        ``None`` means LEAVE AS-IS rather than "clear", because a policy is almost always
        edited one field at a time against a live value: ``auto_approve_tools`` grows every
        time a human answers a gate with "approve and stop asking", and a call that meant to
        flip auto mode must not silently discard that. Pass an explicit empty iterable to
        clear the allow-list."""
        changes: dict[str, Any] = {
            field_name: value
            for field_name, value in (
                ("dangerously_skip_all_approvals", dangerously_skip_all_approvals),
                ("auto_approve_inherently_safe", auto_approve_inherently_safe),
                ("auto_approve_tools", auto_approve_tools),
                ("auto_mode_enabled", auto_mode_enabled),
            )
            if value is not None
        }
        return type(self)(**{**self.model_dump(), **changes})

    def with_tool_allowed(self, tool_name: str) -> "ToolApprovalPolicy":
        """A copy of this policy with ``tool_name`` added to ``auto_approve_tools``.

        Backs the "approve, and stop asking me about this tool" flow (a ``tool_approval``
        decision with ``remember=True``) and any agent-driven runtime allow-listing. A tool
        allow-listed this way is approved by layer 2 from then on, so auto mode stops being
        consulted for it at all — an explicit human "always allow" outranks the machine.
        """
        return self.with_changes(auto_approve_tools=self.auto_approve_tools | {tool_name})

    def with_auto_mode(self, enabled: bool) -> "ToolApprovalPolicy":
        """A copy of this policy with auto mode switched on or off.

        The value-like way for a message handler to let an end user hand decisions to the
        machine, or take them back, without disturbing the explicit allow-list they have
        built up in ``auto_approve_tools``.
        """
        return self.with_changes(auto_mode_enabled=enabled)

    # -- Named presets (ergonomic constructors; all serialize to this one model) -----

    @classmethod
    def always_require_human_approval(cls) -> "ToolApprovalPolicy":
        """EVERY tool call goes to a human. No allow-listing of any kind.

        Clears all three allow-list mechanisms explicitly — inherently-safe, by-name, and auto
        mode — rather than leaning on the field defaults, so the guarantee is legible at the
        call site and cannot quietly weaken if a default ever changes. A worker that has an
        auto mode evaluator wired and criteria configured still consults neither under this
        policy: there is nothing to consult them for, because nothing is being allow-listed.

        This is the safe-by-default baseline, and it is the ABSENCE of settings rather than a
        mode of its own. That is exactly why it does not combine with auto mode: auto mode is
        allow-listing, and a policy that allow-lists is not one that sends everything to a
        person. Enabling auto mode does not modify this policy — it selects a different one.
        """
        return cls(
            dangerously_skip_all_approvals=False,
            auto_approve_inherently_safe=False,
            auto_approve_tools=frozenset(),
            auto_mode_enabled=False,
        )

    @classmethod
    def allow_inherently_safe(cls) -> "ToolApprovalPolicy":
        """Auto-approve tools that declared ``inherently_safe``; everything else goes to a
        human. Auto mode stays off."""
        return cls(auto_approve_inherently_safe=True)

    @classmethod
    def allow_tools(
        cls, tool_names: Iterable[str], *, also_inherently_safe: bool = False
    ) -> "ToolApprovalPolicy":
        """Auto-approve the named tools; optionally also auto-approve inherently-safe
        ones (additive). Auto mode stays off — everything else goes to a human."""
        return cls(
            auto_approve_tools=frozenset(tool_names),
            auto_approve_inherently_safe=also_inherently_safe,
        )

    @classmethod
    def auto_mode(
        cls,
        *,
        pre_approved_tools: Iterable[str] = (),
        pre_approve_inherently_safe: bool = False,
    ) -> "ToolApprovalPolicy":
        """Allow-list DYNAMICALLY: every call the static allow-lists did not cover is put to
        the agent's auto mode evaluator, which may allow it, deny it outright, or leave it to
        a human.

        The arguments are the two STATIC allow-lists, named for what they do to auto mode: a
        pre-approved call is allow-listed before auto mode is reached, so it never costs a
        model call and the evaluator never sees it. Use them for what should not be judged at
        all; leave them empty to have auto mode judge everything.

        Needs an evaluator wired on the runner (``auto_mode_evaluator=``); the runner REJECTS
        this policy without one, rather than report auto mode on while nothing judges. Each
        tool also needs criteria configured (:class:`AutoApprovalCriteria`) — a tool without
        any is not allow-listed dynamically, so its calls go to a human, the same place they
        would have gone under :meth:`always_require_human_approval`."""
        return cls(
            auto_approve_tools=frozenset(pre_approved_tools),
            auto_approve_inherently_safe=pre_approve_inherently_safe,
            auto_mode_enabled=True,
        )

    @classmethod
    def dangerously_skip_all(cls) -> "ToolApprovalPolicy":
        """Auto-approve EVERYTHING — no call is ever gated. Disables the guardrail.

        Not allow-listing but the absence of a gate: there is no set of calls this scopes, so
        nothing reaches a human and auto mode has nothing to judge (which is why enabling both
        is refused — see :meth:`_reject_vacuous_auto_mode`)."""
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
    Auto mode's on/off switch lives on this policy, so a caller's policy also decides
    whether a machine decides anything. The *evaluator* itself is not part of this contract
    and is never overridable from the config (it is non-serializable) — a caller supplies the
    posture, never the code that applies it.

    ``auto_approval_criteria`` — the named criteria sets an auto mode evaluator judges
    gated calls against, and which set judges which tool (see
    :class:`AutoApprovalCriteria`). ``None`` → use the agent's built-in default (the
    runner's ``auto_approval_criteria_default=``). Resolved exactly like
    ``approval_policy``, and for the same reason: the security posture these rules encode
    usually belongs to the END USER of the product the agent powers, so a caller must be
    able to start a session under their own — including redefining what a set the agent's
    own tools point at MEANS. Supplying criteria does not by itself put a model in the
    loop: that takes ``approval_policy.auto_mode_enabled`` as well. The *evaluator*
    itself remains non-serializable and non-overridable — a caller supplies the rules,
    never the mechanism, and so can never introduce code that decides.

    ``agent_id`` — the short, tree-unique id this agent stamps on every event it publishes (and
    reports on its ``agent_status`` query); see :data:`AgentId` for the segment shape. A PARENT sets
    this when starting a subagent — pushing down the same ``handle`` it uses to reference the child
    (its own id plus one fresh segment) — so the child's own event stream is labelled with the
    id the parent (and a UI merging the streams) already knows it by. ``None`` → a top-level agent
    generates its own single-segment id. This is the one ``AgentConfig`` field a parent populates
    per-child rather than passing through unchanged.
    """

    approval_policy: ToolApprovalPolicy | None = None
    auto_approval_criteria: AutoApprovalCriteria | None = None
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
                     payload={"model": "gemini-3.1-flash-lite"})

    Routing is **by name**, not by a discriminator on the payload type — so two handlers
    may accept the *same* input model.

    The envelope carries nothing about what the sender has observed. Admission is FIFO and
    every admitted message is visible on the stream (``message_accepted``), so a message that
    arrives after work the sender has not seen simply runs after it — the same outcome as
    sending a moment later. A sender learns what became of its message from the reply
    (``turn_number`` / ``disposition``); one that wants to act only on an idle agent reads
    ``agent_status`` first, which is a UX choice rather than part of the protocol.

    Two submits of an identical envelope are two messages. A client could make their sends
    idempotent with a Temporal update id.

    It is carried on the envelope itself — the ``send_agent_message`` update takes a bare
    :class:`AgentMessage`, with no separate wrapper.
    """

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


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


class MessageDisposition(StrEnum):
    """What an admitted message actually did to the agent's turn — the OUTCOME half of
    :class:`MidTurn`'s declaration.

    ``MidTurn`` is what the handler's author declared should happen mid-turn; this is what
    happened to one particular message, which also depends on whether the agent was busy at
    all. An idle agent gives every mode the same answer (:attr:`OPENED`), so a handler
    declaring ``ENQUEUE`` yields ``OPENED`` or ``QUEUED``, one declaring ``ACCEPT`` yields
    ``OPENED`` or ``JOINED``, and one declaring ``REJECT`` only ever yields ``OPENED`` (a
    mid-turn arrival never gets admitted, so it has no disposition at all).

    Reported in two places for two audiences: on :attr:`AgentMessageReply.disposition`, so the
    SENDER learns what became of its own message, and on the ``message_accepted`` event, so
    every OBSERVER of the stream learns the same thing.
    """

    OPENED = "opened"
    """The agent was idle: this message opened a new turn and runs in it alone (so far)."""

    JOINED = "joined"
    """A turn was already open and this message's handler declares :attr:`MidTurn.ACCEPT`:
    it joined that turn, sharing its ``turn_id``, and runs concurrently with it. The turn
    counter did NOT advance."""

    QUEUED = "queued"
    """The agent was busy and this message's handler declares :attr:`MidTurn.ENQUEUE`: it
    holds a reserved slot and will open a turn of its own once the queue reaches it."""


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

    ``turn_id`` / ``turn_number`` identify the turn this message is participating in, and
    ``message_id`` identifies the message itself — the same three values the caller received
    from ``send_agent_message`` — so a handler can correlate its own work with the event
    stream without reaching into the runner. When a turn is shared, ``message_id`` is the one
    that distinguishes THIS handler's events from a concurrent participant's.
    """

    joined_turn: bool
    turn_id: str
    turn_number: int
    message_id: str


# ---------------------------------------------------------------------------
# Wire types — shared between workflow and client
# ---------------------------------------------------------------------------


@dataclass
class AgentMessageReply:
    """Returned by the workflow's ``send_agent_message`` update on acceptance.

    ``message_id`` identifies THIS message for the whole rest of its life: the harness mints
    it during admission and stamps it on the envelope of every event the message's dispatch
    produces (:attr:`AgentEvent.message_id`), so a sender can correlate its own reply,
    deltas and tool calls without parsing the stream for them. It is returned here — rather
    than left to be read off the ``message_accepted`` event — so a caller that never opens a
    stream still has it.

    ``turn_number`` is the turn this message actually belongs to — a NEW turn for a queued or
    idle message, or the ALREADY-OPEN turn it joined when its handler declares
    :attr:`MidTurn.ACCEPT`.

    ``disposition`` says what the message did to that turn — opened it, joined it, or queued
    behind it (see :class:`MessageDisposition`). It is what a caller needs to decide how to
    observe the work: only ``OPENED`` gets a turn of its own to stream, which is the
    precondition :meth:`AgentClient.send_message` enforces.

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
    message_id: str
    disposition: MessageDisposition
    accepted_offset: int = 0


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
    # A short, session-unique alias for ``tool_id``, ``APPROVAL_SHORT_ID_LENGTH`` hex chars.
    #
    # ``tool_id`` is minted by the model provider, not by the harness, so its length is not ours
    # to bound: it ranges from a ~29-char provider call id to ``openai_agents:{tool_name}:{uuid4}``
    # (51 chars plus the tool name). Chat platforms round-trip an approve/deny decision through a
    # single fixed-width field — Discord's ``custom_id`` caps at 100 characters, Telegram's
    # ``callback_data`` at 64 BYTES — and overflow there is a hard error at post time, so a card
    # built from a long ``tool_id`` fails to post at all and the gate is left waiting with no way
    # for anyone to answer it. An integration puts THIS on the button instead and resolves it back
    # to ``tool_id`` against this list on click.
    #
    # Unique across every approval of the session, resolved AND pending — not merely across the
    # pending ones — so that a stale card for a long-since-resolved call can never be matched to a
    # different call that happened to reuse the alias.
    short_id: str


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
    """A message waiting in the agent's queue.

    ``message_id`` is the same id the sender got back on :attr:`AgentMessageReply.message_id`
    and that every event of this message's dispatch will carry — so a client that lost track
    can find its own queued message here rather than guessing from position.
    """

    turn_number: int
    turn_id: str
    message_id: str
    message: str


@dataclass
class SubagentInfo:
    """An active subagent this agent is driving, surfaced via ``agent_status``.

    ``subagent_id`` is the short id the agent references it by (the same id the subagent stamps as
    its own ``agent_id``, and the value on the parent's ``subagent_started`` / ``subagent_message_sent``
    / ``subagent_reply_received`` events); ``workflow_id`` is the real child workflow (for an
    operator/UI to drill into, and to query its own ``agent_status``). The caller-side FIFO
    gate's internals (its ticket counters) are deliberately NOT surfaced — they are an
    implementation detail of turn ordering, not agent status.
    """

    subagent_id: str
    agent_key: str
    workflow_id: str


@dataclass
class AgentStatus:
    """Queryable status of the agent workflow.

    The workflow exposes this via the ``agent_status`` query. It is the
    single source of truth for ``attach()``'s termination decision, and what a client reads
    when it wants to know whether the agent is idle before sending.

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
    # via ``AgentConfig.approval_policy``. ``has_auto_approval_evaluator`` reports only
    # *whether* an auto mode evaluator is wired — code, possibly an AI approver, that can
    # approve or DENY a call this policy gated. It is non-serializable, so neither it nor
    # its rules are — or can be — surfaced here; an operator reading the policy alone is
    # therefore not seeing everything that decides. That bit is the flag that says so.
    approval_policy: ToolApprovalPolicy = field(
        default_factory=ToolApprovalPolicy.always_require_human_approval
    )
    has_auto_approval_evaluator: bool = False
    # The live ``AutoApprovalCriteria`` are deliberately NOT surfaced here, even though
    # they are serializable. A tool's *declared* criteria set is a decorator argument
    # living in code, and the runner holds no tool registry to enumerate those from — so
    # any per-tool map published here would be silently incomplete for exactly the tools
    # whose author already picked a sensible set, which is worse than publishing none. A
    # client is anyway the source of truth for criteria it supplied in ``AgentConfig``. The
    # part a client actually needs to render — whether the machine is deciding at all —
    # rides along on ``approval_policy.auto_mode_enabled``. Revisit if tools ever
    # register themselves with the runner up front.


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
